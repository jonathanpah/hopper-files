"""Authenticated managed image upload, listing, and pending-state routes."""

from __future__ import annotations

import hashlib
import json
import os
import stat

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from hopper_files.api.files import (
    _catalog,
    _mutation_authorized,
    _mutation_denied,
    _operation_error,
    _operation_result_response,
    _query_address,
    _store,
)
from hopper_files.config import ConfigError
from hopper_files.files import OperationError
from hopper_files.image_refs import ImageIdentity, ReferenceScanError, relative_markdown_destination, resolve_destination
from hopper_files.images import ImageError, MAX_MANAGED_IMAGE_BYTES
from hopper_files.api.request_body import CONTROL_BODY_MAX_BYTES, BodyTooLarge, read_limited_body
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.roots import (
    AddressRejected,
    DocumentRoot,
    begin_stage_regular,
    discard_staged_regular,
    open_regular,
    root_record,
)
from hopper_files.state import StateError
from hopper_files.trash import TrashError


async def get_attachments(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        catalog = _catalog(request)
        root_id, note_path = _query_address(request, nonempty=True)
        if not note_path.lower().endswith((".md", ".markdown")):
            return json_error(422, "invalid_request")
        result = await run_in_threadpool(
            request.app.state.runtime.images.attachments,
            catalog,
            root_id,
            note_path,
            request.app.state.runtime.clock.now(),
        )
    except (AddressRejected, ConfigError, StateError, OSError, ImageError) as exc:
        return _image_error(exc)
    return _json(200, result)


async def get_resolve_reference(request: Request) -> Response:
    """Resolve one Markdown image destination without exposing filesystem paths."""
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        root_id, note_path = _query_address(request, nonempty=True)
    except AddressRejected:
        return json_error(422, "invalid_request")
    references = request.query_params.getlist("reference")
    if len(references) != 1 or not references[0] or not note_path.lower().endswith((".md", ".markdown")):
        return json_error(422, "invalid_request")
    try:
        catalog = _catalog(request)
    except (ConfigError, StateError):
        return json_error(503, "storage_unavailable")
    if not isinstance(root_record(catalog, root_id), DocumentRoot):
        return json_error(404, "not_found")

    def resolve() -> ImageIdentity | None:
        # The source note is read through the same descriptor-safe path as the file API; the
        # resolver below applies the normalization and symbolic-link rules.
        with open_regular(catalog, root_id, note_path) as (fd, info):
            if not stat.S_ISREG(info.st_mode):
                raise AddressRejected("forbidden")
        return resolve_destination(catalog, root_id, note_path, references[0])

    try:
        identity = await run_in_threadpool(resolve)
    except AddressRejected as exc:
        return _image_error(exc)
    except ReferenceScanError:
        return json_error(422, "invalid_reference")
    except (ConfigError, StateError):
        return json_error(503, "storage_unavailable")
    if identity is None:
        return _json(200, {"resolved": False})
    return _json(200, {"resolved": True, "rootId": identity.root_id, "path": identity.path})


async def post_attachment_upload(request: Request) -> Response:
    if not _mutation_authorized(request):
        return _mutation_denied(request)
    try:
        root_id, note_path = _query_address(request, nonempty=True)
    except AddressRejected:
        return json_error(422, "invalid_request")
    if not note_path.lower().endswith((".md", ".markdown")):
        return json_error(422, "invalid_request")
    try:
        catalog = _catalog(request)
    except (ConfigError, StateError):
        return json_error(503, "storage_unavailable")
    if not isinstance(root_record(catalog, root_id), DocumentRoot):
        return json_error(404, "not_found")
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > MAX_MANAGED_IMAGE_BYTES):
        return json_error(413, "limit_exceeded")
    body = bytearray()
    async for block in request.stream():
        if len(body) + len(block) > MAX_MANAGED_IMAGE_BYTES:
            return json_error(413, "limit_exceeded")
        body.extend(block)
    content = bytes(body)
    detected = _detect(content)
    if detected is None:
        return json_error(415, "unsupported_image")
    extension, media_type = detected
    digest = hashlib.sha256(content).hexdigest()
    runtime = request.app.state.runtime
    token = request.headers.get("x-hopper-operation-token")
    now = runtime.clock.now()
    lease = None
    stage = None
    stage_writer = None
    try:
        # Confirm the attachment belongs to an existing Markdown document.
        with open_regular(catalog, root_id, note_path) as (_fd, note_info):
            if not stat.S_ISREG(note_info.st_mode):
                return json_error(422, "invalid_request")
        images = runtime.images
        with images.lock():
            store = _store(request)
            lease = store.acquire_upload_lease(token, now)
            action = images.upload_action(catalog, root_id, note_path, now, extension, digest, len(content))
            started = store.begin_upload(token, action, catalog, now, lease=lease)
            if started.get("stored"):
                result = store.status(token, now)
                committed = result.get("committed")
                if not isinstance(committed, list) or len(committed) != 1:
                    return _operation_result_response(result)
                item = committed[0]
                destination = item.get("destination") if isinstance(item, dict) else None
                if not isinstance(destination, dict) or not isinstance(destination.get("path"), str):
                    return json_error(409, "conflict")
                images.record_upload_locked(root_id, note_path, destination["path"], digest, len(content), now)
                reference = relative_markdown_destination(
                    catalog, root_id, note_path, root_id, destination["path"]
                )
                return _json(200, {
                    "operation": result,
                    "image": {"rootId": root_id, "path": destination["path"], "reference": reference,
                              "mediaType": media_type, "size": len(content), "pending": True},
                })
            upload_action = started.get("action")
            if not isinstance(upload_action, dict) or not isinstance(upload_action.get("files"), list):
                return json_error(409, "conflict")
            file_action = upload_action["files"][0]
            destination = file_action["destination"]
            stage_writer = begin_stage_regular(
                catalog, root_id, destination["path"], maximum=MAX_MANAGED_IMAGE_BYTES
            )
            details = os.fstat(stage_writer.file_fd)
            store.note_upload_stage(token, 0, file_action, stage_writer.temporary_name, (details.st_dev, details.st_ino))
            stage_writer.write(content)
            stage = stage_writer.finish()
            stage_writer = None
            result = store.publish_upload_batch(token, [stage], catalog, now)
            stage = None
            if result.get("status") == "completed" and isinstance(result.get("committed"), list):
                committed = result["committed"]
                if len(committed) != 1 or not isinstance(committed[0].get("destination"), dict):
                    return json_error(409, "conflict")
                destination_path = committed[0]["destination"]["path"]
                images.record_upload_locked(root_id, note_path, destination_path, digest, len(content), now)
                reference = relative_markdown_destination(
                    catalog, root_id, note_path, root_id, destination_path
                )
                return _json(201, {
                    "operation": result,
                    "image": {"rootId": root_id, "path": destination_path, "reference": reference,
                              "mediaType": media_type, "size": len(content), "pending": True},
                })
            return _operation_result_response(result)
    except OperationError as exc:
        return _operation_error(exc)
    except ImageError as exc:
        return _image_error(exc)
    except AddressRejected as exc:
        return _image_error(exc)
    except (ConfigError, StateError, TrashError):
        return json_error(503, "storage_unavailable")
    except OSError:
        return json_error(503, "io_error")
    finally:
        if stage_writer is not None and stage_writer.file_fd >= 0:
            try:
                stage_writer.abort()
            except OSError:
                pass
        if stage is not None:
            try:
                discard_staged_regular(stage)
            except OSError:
                pass
        if lease is not None:
            lease.close()


async def post_resolve_pending(request: Request) -> Response:
    if not _mutation_authorized(request):
        return _mutation_denied(request)
    try:
        payload = json.loads((await read_limited_body(request, CONTROL_BODY_MAX_BYTES)).decode("utf-8"))
    except BodyTooLarge:
        return json_error(413, "limit_exceeded")
    except (ValueError, json.JSONDecodeError):
        return json_error(422, "invalid_request")
    if not isinstance(payload, dict) or set(payload) != {"rootId", "path"}:
        return json_error(422, "invalid_request")
    if not isinstance(payload["rootId"], str) or not isinstance(payload["path"], str):
        return json_error(422, "invalid_request")
    try:
        catalog = _catalog(request)
    except (ConfigError, StateError):
        return json_error(503, "storage_unavailable")
    if not isinstance(root_record(catalog, payload["rootId"]), DocumentRoot):
        return json_error(404, "not_found")
    try:
        changed = await run_in_threadpool(
            request.app.state.runtime.images.resolve_pending,
            payload["rootId"],
            payload["path"],
        )
    except (StateError, ConfigError):
        return json_error(503, "storage_unavailable")
    return _json(200, {"resolved": changed})


def _detect(content: bytes) -> tuple[str, str] | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _image_error(error: object) -> Response:
    code = getattr(error, "code", "unavailable")
    status = {
        "invalid": 422,
        "invalid_request": 422,
        "not_found": 404,
        "unavailable": 503,
        "forbidden": 403,
        "conflict": 409,
        "limit": 413,
    }.get(code, 503)
    public = "limit_exceeded" if code == "limit" else code
    return _json(status, {"error": public, **getattr(error, "details", {})})


def _json(status: int, payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )
