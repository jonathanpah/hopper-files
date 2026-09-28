"""Small bounded request-body reads for JSON/form control endpoints."""

from __future__ import annotations

from starlette.requests import Request


CONTROL_BODY_MAX_BYTES = 64 * 1024


class BodyTooLarge(ValueError):
    """A control request exceeded its declared or streamed byte limit."""


async def read_limited_body(request: Request, maximum: int) -> bytes:
    """Read no more than one ASGI chunk beyond ``maximum`` bytes.

    Content-Length is only an early rejection hint. The streamed byte count is
    authoritative when the header is absent or dishonest.
    """
    if maximum < 0:
        raise ValueError("maximum must be nonnegative")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isdecimal() or int(content_length) > maximum:
            raise BodyTooLarge("request body exceeds limit")
    body = bytearray()
    async for block in request.stream():
        if len(body) + len(block) > maximum:
            raise BodyTooLarge("request body exceeds limit")
        body.extend(block)
    return bytes(body)
