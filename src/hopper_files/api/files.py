"""Authenticated base file routes without editor, trash, or reference semantics."""

from __future__ import annotations

import json
import errno
import hashlib
import os
import base64
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
import anyio

from hopper_files.config import ConfigError, load_config, same_service_identity
from hopper_files.files import OperationError, OperationStore, UPLOAD_MAX_FILE, UPLOAD_MAX_TOTAL
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.roots import AddressRejected, StagedRegular, begin_stage_regular, discard_staged_regular, build_catalog, directory_version, directory_writable, list_directory, open_regular, parse_relative_path, scan_tree, traversal_excluded
from hopper_files.sessions import csrf_matches
from hopper_files.state import StateError


async def get_list(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        catalog = _catalog(request)
        root_id, path = _query_address(request)
        watch_before_reading(request, catalog, root_id, path)
        before = directory_version(catalog, root_id, path)
        entries = list_directory(catalog, root_id, path)
        writable = directory_writable(catalog, root_id, path)
        after = directory_version(catalog, root_id, path)
        if before != after:
            return json_error(409, "conflict")
    except (AddressRejected, ConfigError, StateError) as exc:
        return _address_error(exc)
    return _json(
        200,
        {
            "rootId": root_id,
            "path": path,
            "address": "/" + path,
            "writable": writable,
            "listingVersion": before,
            "entries": [_entry_payload(entry) for entry in entries],
        },
    )


def _entry_payload(entry) -> dict[str, object]:
    """Describe one listed child (HF-NAV-002). Only addressable names are addresses."""
    payload: dict[str, object] = {
        "name": entry.name,
        "type": entry.kind,
        "size": entry.size,
        "openable": entry.openable,
        "addressable": entry.addressable,
        "writable": entry.writable,
        "links": entry.links,
        "modifiedAt": _utc_stamp(entry.modified),
        "createdAt": _utc_stamp(entry.created),
    }
    if entry.reason is not None:
        payload["reason"] = entry.reason
    if entry.kind == "link":
        payload["target"] = entry.target
        payload["resolved"] = entry.resolved
        payload["resolvedType"] = entry.resolved_kind
    return payload


def _utc_stamp(value: float | None) -> str | None:
    """UTC time as ``YYYY-MM-DDTHH:MM:SSZ``, the form the trash already uses."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


# Directory sizes are measured on demand, for example when a listing shows
# folders. Each measurement has a short execution budget and at most two run at
# once, outside the request loop, so navigation never waits behind them.
DIR_SIZE_BUDGET_SECONDS = 3.0
DIR_SIZE_CONCURRENCY = 2
_dir_size_limiter: anyio.CapacityLimiter | None = None
# Categories that describe a deliberate boundary rather than missing bytes.
_DIR_SIZE_BOUNDARIES = frozenset({"pseudo_filesystem"})


async def get_directory_size(request: Request) -> Response:
    """Measure a directory's visible size, best effort, within a short budget."""
    global _dir_size_limiter
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    if _dir_size_limiter is None:
        _dir_size_limiter = anyio.CapacityLimiter(DIR_SIZE_CONCURRENCY)
    async with _dir_size_limiter:
        # A client that navigated away no longer needs the queued measurement.
        if await request.is_disconnected():
            return Response(status_code=204)
        return await run_in_threadpool(_measure_directory_size, request)


def _measure_directory_size(request: Request) -> Response:
    try:
        catalog = _catalog(request)
        root_id, path = _query_address(request)
        counts: dict[str, int] = {}
        total_bytes = 0
        file_count = 0
        directory_count = 0
        if traversal_excluded(path):
            counts["pseudo_filesystem"] = 1
        else:
            deadline = time.monotonic() + DIR_SIZE_BUDGET_SECONDS
            for scanned in scan_tree(catalog, root_id, path, deadline=deadline, skip_internal_trash=False, counts=counts):
                directory_count += 1
                for entry in scanned.entries:
                    if entry.kind == "file":
                        total_bytes += entry.size or 0
                        file_count += 1
                for category, count in scanned.exclusions.items():
                    counts[category] = counts.get(category, 0) + count
    except AddressRejected as exc:
        return _address_error(exc)
    except (ConfigError, StateError):
        return json_error(409, "conflict")
    omissions = [{"category": key, "count": value} for key, value in sorted(counts.items())]
    return _json(200, {
        "rootId": root_id,
        "path": path,
        "totalBytes": total_bytes,
        "fileCount": file_count,
        "directoryCount": directory_count,
        "complete": not traversal_excluded(path) and all(key in _DIR_SIZE_BOUNDARIES for key in counts),
        "omissions": omissions,
    })


async def get_raw(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        catalog = _catalog(request)
        root_id, path = _query_address(request, nonempty=True)
        handle = open_regular(catalog, root_id, path)
        file_fd, info = handle.__enter__()
    except (AddressRejected, ConfigError, StateError) as exc:
        return _address_error(exc)

    def body() -> Iterator[bytes]:
        try:
            while True:
                block = os.read(file_fd, 64 * 1024)
                if not block:
                    return
                yield block
        finally:
            handle.__exit__(None, None, None)

    name = path.rsplit("/", 1)[-1]
    disposition = "attachment; filename=download; filename*=UTF-8''" + quote(name, safe="")
    headers = dict(SECURITY_HEADERS)
    headers["Content-Disposition"] = disposition
    headers["Content-Length"] = str(info.st_size)
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


async def issue_operation_token(request: Request) -> Response:
    if not _mutation_authorized(request):
        return _mutation_denied(request)
    try:
        result = _store(request).issue(request.app.state.runtime.clock.now())
    except OperationError as exc:
        return _operation_error(exc)
    except StateError:
        return json_error(409, "conflict")
    return _json(201, result)


async def get_operation_status(request: Request, token: str) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        return _json(200, _store(request).status(token, request.app.state.runtime.clock.now()))
    except OperationError as exc:
        return _operation_error(exc)
    except StateError:
        return json_error(409, "conflict")


async def post_files(request: Request) -> Response:
    if not _mutation_authorized(request):
        return _mutation_denied(request)
    try:
        maximum = 1024 * 1024
        content_length = request.headers.get("content-length")
        if content_length is not None and (not content_length.isdecimal() or int(content_length) > maximum):
            return json_error(413, "limit_exceeded")
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > maximum:
                return json_error(413, "limit_exceeded")
            body.extend(block)
        raw = bytes(body)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"operationToken", "actions"}:
            return json_error(422, "invalid_request")
        catalog = _catalog(request)
        runtime = request.app.state.runtime
        submit = lambda: _store(request).submit(
            payload["operationToken"], payload["actions"], catalog, runtime.clock.now()
        )
        actions = payload["actions"]
        has_move = isinstance(actions, list) and any(
            isinstance(action, dict) and action.get("action") in {"move", "rename"}
            for action in actions
        )
        def submit_operation():
            if has_move:
                with runtime.images.lock():
                    return submit()
            return submit()

        # Moves may wait for an authenticated editor buffer acknowledgement.
        # Keep that wait off the ASGI event loop so /api/buffers can be served.
        result = await run_in_threadpool(submit_operation)
    except (UnicodeError, json.JSONDecodeError):
        return json_error(422, "invalid_request")
    except OperationError as exc:
        if isinstance(exc.payload, dict):
            return _operation_result_response(exc.payload)
        return _operation_error(exc)
    except (AddressRejected, ConfigError, StateError):
        return json_error(409, "conflict")
    return _operation_result_response(result)


async def post_upload(request: Request) -> Response:
    """Stream a concatenation of declared files without buffering request bytes."""
    if not _mutation_authorized(request):
        return _mutation_denied(request)
    token = request.headers.get("x-hopper-operation-token")
    store = _store(request)
    catalog = None
    staged: list[StagedRegular | None] = []
    lease = None
    began = False
    try:
        manifest = _upload_manifest(request.headers.get("x-hopper-upload-manifest"))
        length = request.headers.get("content-length")
        catalog = _catalog(request)
        now = request.app.state.runtime.clock.now()
        lease = store.acquire_upload_lease(token, now)
        started = store.begin_upload(token, manifest, catalog, now, lease=lease)
        began = True
        if started["stored"]:
            return _json(200, store.status(token, now))
        action = started["action"]
        assert isinstance(action, dict)
        files = action["files"]
        total = sum(item["contentSize"] for item in files)
        if length is not None and (not length.isdecimal() or int(length) != total):
            store.abort_upload(token, now, "content_length_mismatch")
            return _json(422, store.status(token, now))
        if total > UPLOAD_MAX_TOTAL:
            store.abort_upload(token, now, "limit")
            return json_error(413, "limit_exceeded")
        committed_indexes = started.get("committedIndexes", [])
        committed = set(committed_indexes) if isinstance(committed_indexes, list) else set()
        staged = await _receive_upload_batch(request, store, catalog, token, files, committed)
        result = store.publish_upload_batch(token, staged, catalog, now)
        return _operation_result_response(result)
    except OperationError as exc:
        if lease is None or not began:
            return _operation_error(exc)
        if isinstance(exc.payload, dict):
            return _operation_result_response(exc.payload)
        try:
            if catalog is not None:
                store.recover(
                    catalog,
                    request.app.state.runtime.clock.now(),
                    active_upload_lease=lease,
                )
            store.abort_upload(token, request.app.state.runtime.clock.now(), exc.code)
        except (OperationError, StateError):
            pass
        return _operation_error(exc)
    except AddressRejected as exc:
        if lease is None or not began:
            return _operation_error(OperationError(exc.code))
        try:
            if catalog is not None:
                store.recover(
                    catalog,
                    request.app.state.runtime.clock.now(),
                    active_upload_lease=lease,
                )
            store.abort_upload(token, request.app.state.runtime.clock.now(), exc.code)
        except (OperationError, StateError):
            pass
        return _operation_error(OperationError(exc.code))
    except (ConfigError, StateError, OSError) as exc:
        if lease is None or not began:
            if catalog is not None and manifest is not None:
                try:
                    outcome = store.upload_outcome_after_storage_error(
                        token, manifest, catalog, request.app.state.runtime.clock.now()
                    )
                    return _operation_result_response(outcome)
                except (OperationError, StateError):
                    pass
            return json_error(503, "storage_unavailable")
        for item in staged:
            if item is None:
                continue
            try:
                discard_staged_regular(item)
            except OSError:
                pass
        now = request.app.state.runtime.clock.now()
        try:
            if catalog is not None:
                outcome = store.outcome_after_storage_error(token, catalog, now)
                if (
                    outcome.get("status") == "indeterminate"
                    or outcome.get("committed")
                    or outcome.get("indeterminate")
                ):
                    return _operation_result_response(outcome)
                store.abort_upload(token, now, "storage_unavailable")
                return _operation_result_response(store.status(token, now))
        except (OperationError, StateError):
            pass
        return json_error(503, "indeterminate")
    finally:
        if lease is not None:
            lease.close()


async def _receive_upload_batch(
    request: Request,
    store: OperationStore,
    catalog: object,
    token: object,
    files: list[dict[str, object]],
    committed_indexes: set[int],
) -> list[StagedRegular | None]:
    writers = []
    staged: list[StagedRegular | None] = [None for _ in files]
    digests = [hashlib.sha256() for _ in files]
    try:
        for index, item in enumerate(files):
            if index in committed_indexes:
                writers.append(None)
                continue
            destination = item["destination"]
            writer = begin_stage_regular(
                catalog,
                destination["rootId"],
                destination["path"],
                maximum=UPLOAD_MAX_FILE,
            )
            writers.append(writer)
            info = os.fstat(writer.file_fd)
            store.note_upload_stage(token, index, item, writer.temporary_name, (info.st_dev, info.st_ino))
        sizes = [0 for _ in files]
        completed = [False for _ in files]
        index = 0

        def finish_ready() -> None:
            nonlocal index
            while index < len(files) and sizes[index] == files[index]["contentSize"] and not completed[index]:
                writer = writers[index]
                if writer is None:
                    if digests[index].hexdigest() != files[index]["contentDigest"]:
                        raise OperationError("invalid")
                else:
                    result = writer.finish()
                    staged[index] = result
                    if result.digest != files[index]["contentDigest"] or result.size != files[index]["contentSize"]:
                        raise OperationError("invalid")
                completed[index] = True
                index += 1

        finish_ready()
        async for block in request.stream():
            offset = 0
            while offset < len(block):
                finish_ready()
                if index >= len(files):
                    raise OperationError("invalid")
                needed = files[index]["contentSize"] - sizes[index]
                count = min(needed, len(block) - offset)
                piece = block[offset : offset + count]
                writer = writers[index]
                if writer is None:
                    digests[index].update(piece)
                else:
                    writer.write(piece)
                sizes[index] += count
                offset += count
                finish_ready()
        finish_ready()
        if not all(completed):
            raise OperationError("invalid")
        return staged
    except OSError as exc:
        for writer in writers:
            if writer is not None and writer.file_fd >= 0:
                try:
                    writer.abort()
                except OSError:
                    pass
        for item in staged:
            if item is not None:
                try:
                    discard_staged_regular(item)
                except OSError:
                    pass
        raise OperationError(
            "limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable"
        ) from exc
    except BaseException:
        for writer in writers:
            if writer is not None and writer.file_fd >= 0:
                try:
                    writer.abort()
                except OSError:
                    pass
        for item in staged:
            if item is not None:
                try:
                    discard_staged_regular(item)
                except OSError:
                    pass
        raise


def _upload_manifest(value: str | None) -> object:
    if value is None or len(value) > 64_000:
        raise OperationError("invalid")
    try:
        encoded = value.encode("ascii")
        raw = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationError("invalid") from exc


def _catalog(request: Request):
    running = request.app.state.runtime.config
    reloaded = load_config(running.source_path)
    if not same_service_identity(reloaded, running):
        raise ConfigError("instance identity changed")
    return build_catalog(reloaded, strict=False)


def watch_before_reading(request: Request, catalog, root_id: str, directory: str) -> None:
    """Keep watching a directory a view is about to read, if change notices are in use.

    The watch starts before the read, so a change between the read and the
    view's next change request is reported instead of lost.
    """
    watcher = request.app.state.runtime.changes
    if watcher.active():
        watcher.watch(catalog, root_id, [directory])


def _store(request: Request) -> OperationStore:
    return request.app.state.runtime.operations


def _query_address(request: Request, *, nonempty: bool = False) -> tuple[str, str]:
    root_id = request.query_params.get("rootId")
    path = request.query_params.get("path")
    if root_id is None or path is None or not root_id:
        raise AddressRejected("invalid")
    parts = parse_relative_path(path)
    if nonempty and not parts:
        raise AddressRejected("invalid")
    return root_id, path


def _mutation_authorized(request: Request) -> bool:
    session = request.scope["state"].get("session")
    return session is not None and csrf_matches(session, request.headers.get("x-csrf-token", ""))


def _mutation_denied(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    return json_error(403, "forbidden")


def _operation_error(error: OperationError) -> Response:
    if error.code == "expired":
        return json_error(410, "operation_expired")
    if error.code == "limit":
        return json_error(413, "limit_exceeded")
    if error.code == "conflict":
        return json_error(409, "conflict")
    if error.code == "not_found":
        return json_error(404, "not_found")
    if error.code in {"forbidden", "unavailable"}:
        return json_error(403, "forbidden")
    if error.code == "not_available":
        return json_error(422, "not_available")
    if error.code == "indeterminate":
        if isinstance(error.payload, dict):
            return _operation_result_response(error.payload)
        return json_error(503, "indeterminate")
    return json_error(422, "invalid_request")


def _operation_result_response(result: dict[str, object]) -> Response:
    if result.get("status") == "completed":
        return _json(200, result)
    committed = result.get("committed")
    indeterminate = result.get("indeterminate")
    uncommitted = result.get("uncommitted")
    if (
        result.get("status") == "failed"
        and committed == []
        and indeterminate == []
        and isinstance(uncommitted, list)
    ):
        errors = {
            item.get("error")
            for item in uncommitted
            if isinstance(item, dict) and item.get("error") not in {None, "not_started"}
        }
        if len(errors) == 1:
            code = next(iter(errors))
            status = {
                "conflict": 409,
                "limit": 413,
                "forbidden": 403,
                "not_editable": 403,
                "not_found": 404,
                "invalid": 422,
                "content_mismatch": 422,
                "content_length_mismatch": 422,
                "not_available": 422,
                "unavailable": 503,
                "storage_unavailable": 503,
            }.get(code)
            if status is not None:
                return _json(status, result)
    return _json(207, result)


def _address_error(error: Exception) -> Response:
    if isinstance(error, AddressRejected):
        if error.code == "invalid":
            return json_error(422, "invalid_request")
        if error.code in {"not_found", "unavailable"}:
            return json_error(404, "not_found")
    return json_error(403, "forbidden")


def _json(status: int, payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )
