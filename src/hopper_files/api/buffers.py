"""Content-free connected editor registry and move acknowledgement route."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import Response

from hopper_files.buffers import BufferConflict
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.sessions import csrf_matches
from hopper_files.state import StateError


async def post_buffer_sync(request: Request) -> Response:
    session = request.scope["state"].get("session")
    if session is None:
        return json_error(401, "authentication_required")
    if not csrf_matches(session, request.headers.get("x-csrf-token", "")):
        return json_error(403, "forbidden")
    body = bytearray()
    async for block in request.stream():
        if len(body) + len(block) > 256 * 1024:
            return json_error(413, "limit_exceeded")
        body.extend(block)
    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return json_error(422, "invalid_request")
    if not isinstance(payload, dict) or set(payload) != {"documents", "acknowledgements"}:
        return json_error(422, "invalid_request")
    runtime = request.app.state.runtime
    try:
        result = runtime.buffers.sync(
            session.path.name,
            session.expires_at,
            payload["documents"],
            payload["acknowledgements"],
            runtime.clock.now(),
        )
    except BufferConflict:
        return json_error(422, "invalid_request")
    except StateError:
        return json_error(503, "storage_unavailable")
    return _json(200, result)


def _json(status: int, payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )
