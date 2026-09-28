"""Read, preview, and save one contained file for the editor."""

from __future__ import annotations

import json
import errno
import os
from collections.abc import Iterator
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from hopper_files.api.files import _catalog, _mutation_authorized, _query_address
from hopper_files.config import ConfigError
from hopper_files.editor import EditorError, MAX_TEXT_BYTES, read_document, save_document
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.roots import AddressRejected, open_regular
from hopper_files.state import StateError

MAX_JSON_BODY = MAX_TEXT_BYTES + 256 * 1024
MAX_RASTER_PREVIEW_BYTES = 20 * 1024 * 1024
MAX_PDF_PREVIEW_BYTES = 100 * 1024 * 1024


async def get_file(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        root_id, path = _query_address(request, nonempty=True)
        runtime = request.app.state.runtime
        result = await run_in_threadpool(
            read_document,
            _catalog(request),
            root_id,
            path,
            runtime.config.instance_id,
            runtime.config.state_directory,
        )
    except EditorError as exc:
        return _editor_error(exc)
    except AddressRejected as exc:
        return _address_error(exc)
    except StateError:
        return json_error(503, "storage_unavailable")
    except ConfigError:
        return json_error(503, "io_error")
    except OSError as exc:
        return _filesystem_error(exc)
    return _json(200, result)


async def put_file(request: Request) -> Response:
    if not _mutation_authorized(request):
        if request.scope["state"].get("session") is None:
            return json_error(401, "authentication_required")
        return json_error(403, "forbidden")
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > MAX_JSON_BODY):
        return json_error(413, "limit_exceeded")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return json_error(422, "invalid_request")
    body = bytearray()
    async for block in request.stream():
        if len(body) + len(block) > MAX_JSON_BODY:
            return json_error(413, "limit_exceeded")
        body.extend(block)
    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return json_error(422, "invalid_request")
    if not isinstance(payload, dict):
        return json_error(422, "invalid_request")
    if "baseVersion" not in payload or payload["baseVersion"] is None:
        return json_error(428, "precondition_required")
    if set(payload) != {"baseVersion", "content"}:
        return json_error(422, "invalid_request")
    try:
        root_id, path = _query_address(request, nonempty=True)
    except AddressRejected:
        return json_error(422, "invalid_request")
    runtime = request.app.state.runtime
    try:
        saved = await run_in_threadpool(
            runtime.images.save,
            _catalog(request),
            root_id,
            path,
            payload["baseVersion"],
            payload["content"],
            runtime.clock.now(),
        )
    except EditorError as exc:
        return _editor_error(exc)
    except AddressRejected as exc:
        return _address_error(exc)
    except StateError:
        return json_error(503, "storage_unavailable")
    except ConfigError:
        return json_error(503, "io_error")
    except OSError as exc:
        return _filesystem_error(exc)
    return _json(200, saved)


async def get_preview(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        catalog = _catalog(request)
        root_id, path = _query_address(request, nonempty=True)
        handle = open_regular(catalog, root_id, path)
        fd, info = handle.__enter__()
    except (AddressRejected, ConfigError, StateError):
        return json_error(403, "forbidden")
    prefix = os.read(fd, 16)
    os.lseek(fd, 0, os.SEEK_SET)
    media_type = _raster_type(prefix)
    if prefix.startswith(b"%PDF-"):
        media_type = "application/pdf"
    if media_type is None:
        handle.__exit__(None, None, None)
        return json_error(415, "preview_unavailable")
    maximum = MAX_PDF_PREVIEW_BYTES if media_type == "application/pdf" else MAX_RASTER_PREVIEW_BYTES
    if info.st_size > maximum:
        handle.__exit__(None, None, None)
        return json_error(413, "limit_exceeded")

    def body() -> Iterator[bytes]:
        try:
            while True:
                block = os.read(fd, 64 * 1024)
                if not block:
                    return
                yield block
        finally:
            handle.__exit__(None, None, None)

    headers = dict(SECURITY_HEADERS)
    headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(path.rsplit("/", 1)[-1], safe="")
    headers["Content-Length"] = str(info.st_size)
    headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return StreamingResponse(body(), media_type=media_type, headers=headers)


def _raster_type(prefix: bytes) -> str | None:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


def _editor_error(error: EditorError) -> Response:
    if error.code == "limit":
        return json_error(413, "limit_exceeded")
    if error.code == "invalid_encoding":
        return json_error(422, "invalid_encoding")
    if error.code == "invalid_request":
        return json_error(422, "invalid_request")
    if error.code == "not_found":
        return json_error(404, "not_found")
    if error.code == "forbidden":
        return json_error(403, "forbidden")
    if error.code == "metadata_unsupported":
        return json_error(409, "metadata_unsupported")
    if error.code == "not_editable":
        # HF-NAV-007: the same reason GET api/file reports as readOnlyReason.
        return _json(403, {"error": "not_editable", "reason": error.reason})
    if error.code == "conflict":
        return _json(409, {"error": "conflict", "current": error.current})
    if error.code == "indeterminate":
        return _json(503, {"error": "indeterminate", "current": error.current})
    if error.code == "storage_unavailable":
        return json_error(503, "storage_unavailable")
    if error.code == "io_error":
        return json_error(503, "io_error")
    return json_error(409, "conflict")


def _address_error(error: AddressRejected) -> Response:
    if error.code == "forbidden" and isinstance(error.__cause__, FileNotFoundError):
        return json_error(404, "not_found")
    if error.code == "invalid":
        return json_error(422, "invalid_request")
    if error.code in {"not_found", "unavailable"}:
        return json_error(404, "not_found")
    if error.code == "conflict":
        return json_error(409, "conflict")
    return json_error(403, "forbidden")


def _filesystem_error(error: OSError) -> Response:
    if error.errno in {errno.ENOENT, errno.ENOTDIR}:
        return json_error(404, "not_found")
    if error.errno in {
        errno.EACCES, errno.EPERM, errno.ELOOP, errno.EMLINK,
    }:
        return json_error(403, "forbidden")
    if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
        return json_error(503, "storage_unavailable")
    return json_error(503, "io_error")


def _json(status: int, payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )
