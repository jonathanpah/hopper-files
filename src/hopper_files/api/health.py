"""Data-free process status. This route grants no document access."""

from __future__ import annotations

from starlette.responses import JSONResponse

from hopper_files.responses import SECURITY_HEADERS


def health_payload() -> dict[str, str]:
    return {"service": "hopper-files", "status": "up"}


async def get_health() -> JSONResponse:
    return JSONResponse(health_payload(), headers=SECURITY_HEADERS)
