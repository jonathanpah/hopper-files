"""Small HTTP responses that do not echo request secrets."""

from __future__ import annotations

from starlette.responses import HTMLResponse, JSONResponse, Response

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: under no-referrer a browser form POST carries
    # Origin: null, which the host and origin guard refuses. Nothing leaves the origin.
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "img-src 'self' data: blob:; worker-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
}


def json_error(status: int, code: str, **headers: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_headers(headers))


def html_page(status: int, markup: str, **headers: str) -> HTMLResponse:
    return HTMLResponse(markup, status_code=status, headers=_headers(headers))


def no_store(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _headers(extra: dict[str, str]) -> dict[str, str]:
    merged = dict(SECURITY_HEADERS)
    merged.update(extra)
    return merged
