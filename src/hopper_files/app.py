"""FastAPI assembly for one configured Hopper Files instance."""

from __future__ import annotations

import http.cookies
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from hopper_files.access import evaluate_access
from hopper_files.api.auth import get_app, get_login, get_password, post_login, post_logout, post_password
from hopper_files.api.buffers import post_buffer_sync
from hopper_files.api.changes import post_changes
from hopper_files.api.editor import get_file, get_preview, put_file
from hopper_files.api.files import get_directory_size, get_list, get_operation_status, get_raw, issue_operation_token, post_files, post_upload
from hopper_files.api.health import get_health
from hopper_files.api.images import get_attachments, get_resolve_reference, post_attachment_upload, post_resolve_pending
from hopper_files.api.trash import get_related_trash_images, get_trash, post_trash
from hopper_files.api.search import get_search, get_tag_folders, get_tags, post_tag_folders
from hopper_files.api.ui_state import get_state, put_state
from hopper_files.clock import SystemClock
from hopper_files.buffers import BufferRegistry
from hopper_files.changes import ChangeWatcher
from hopper_files.config import InstanceConfig, validate_service_identity
from hopper_files.credentials import current_epoch
from hopper_files.files import OperationStore
from hopper_files.kdf import prove_kdf_runtime
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.sessions import csrf_matches, open_session
from hopper_files.migration import require_current_format
from hopper_files.state import ensure_initialized
from hopper_files.trash import TrashStore
from hopper_files.images import ImageStore
from hopper_files.search import SearchCursorStore
from hopper_files.roots import build_catalog

STATIC_DIRECTORY = Path(__file__).with_name("static")


class Runtime:
    def __init__(self, config: InstanceConfig, clock: SystemClock) -> None:
        self.config = config
        self.clock = clock
        self.operations = OperationStore(config.state_directory, config.instance_id, config.time_zone)
        self.trash = TrashStore(config.state_directory, config.instance_id)
        self.images = ImageStore(
            config.state_directory,
            config.instance_id,
            config.time_zone,
            self.trash,
            config.source_path,
        )
        self.operations.move_handler = self.images
        self.buffers = BufferRegistry(config.state_directory, config.instance_id)
        self.operations.buffer_registry = self.buffers
        self.search_cursors = SearchCursorStore(config.state_directory, config.instance_id)
        self.changes = ChangeWatcher()


def create_app(config: InstanceConfig, clock: SystemClock | None = None) -> FastAPI:
    """Build the instance app. Importing this module does not start a listener."""
    runtime = Runtime(config, clock or SystemClock())

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured = application.state.runtime.config
        ensure_initialized(configured.state_directory, configured.instance_id)
        require_current_format(configured.state_directory)
        validate_service_identity(configured)
        prove_kdf_runtime()
        catalog = build_catalog(configured, strict=False)
        now = application.state.runtime.clock.now()
        application.state.runtime.trash.recover(catalog, now)
        application.state.runtime.images.recover_moves(catalog, now, application.state.runtime.operations, application.state.runtime.buffers)
        application.state.runtime.operations.recover(catalog, now)
        try:
            yield
        finally:
            application.state.runtime.changes.close()

    application = FastAPI(
        title="Hopper Files",
        description="Instance access and contained base file-operation layer.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    application.state.runtime = runtime
    application.add_middleware(InstanceGuard, runtime=runtime)
    base = config.base_path
    application.add_api_route(base + "login", get_login, methods=["GET"])
    application.add_api_route(base + "login", post_login, methods=["POST"])
    application.add_api_route(base + "password", get_password, methods=["GET"])
    application.add_api_route(base + "password", post_password, methods=["POST"])
    application.add_api_route(base + "logout", post_logout, methods=["POST"])
    application.add_api_route(base + "app", get_app, methods=["GET"])
    application.add_api_route(base + "healthz", get_health, methods=["GET"])
    application.add_api_route(base + "api/list", get_list, methods=["GET"])
    application.add_api_route(base + "api/dir-size", get_directory_size, methods=["GET"])
    application.add_api_route(base + "api/search", get_search, methods=["GET"])
    application.add_api_route(base + "api/tags", get_tags, methods=["GET"])
    application.add_api_route(base + "api/tag-folders", get_tag_folders, methods=["GET"])
    application.add_api_route(base + "api/tag-folders", post_tag_folders, methods=["POST"])
    application.add_api_route(base + "api/raw", get_raw, methods=["GET"])
    application.add_api_route(base + "api/file", get_file, methods=["GET"])
    application.add_api_route(base + "api/file", put_file, methods=["PUT"])
    application.add_api_route(base + "api/preview", get_preview, methods=["GET"])
    application.add_api_route(base + "api/files/token", issue_operation_token, methods=["POST"])
    application.add_api_route(base + "api/files/{token}", get_operation_status, methods=["GET"])
    application.add_api_route(base + "api/files", post_files, methods=["POST"])
    application.add_api_route(base + "api/files/upload", post_upload, methods=["POST"])
    application.add_api_route(base + "api/images", get_attachments, methods=["GET"])
    application.add_api_route(base + "api/images/resolve", get_resolve_reference, methods=["GET"])
    application.add_api_route(base + "api/images/upload", post_attachment_upload, methods=["POST"])
    application.add_api_route(base + "api/images/pending", post_resolve_pending, methods=["POST"])
    application.add_api_route(base + "api/buffers", post_buffer_sync, methods=["POST"])
    application.add_api_route(base + "api/changes", post_changes, methods=["POST"])
    application.add_api_route(base + "api/state", get_state, methods=["GET"])
    application.add_api_route(base + "api/state", put_state, methods=["PUT"])
    application.add_api_route(base + "api/trash", get_trash, methods=["GET"])
    application.add_api_route(base + "api/trash", post_trash, methods=["POST"])
    application.add_api_route(base + "api/trash/related-images", get_related_trash_images, methods=["GET"])
    application.add_api_route(base + "app.css", _css, methods=["GET"])
    application.add_api_route(base + "app.js", _script, methods=["GET"])
    application.add_api_route(base + "assets/{asset:path}", _asset, methods=["GET"])
    application.add_api_route(base + "third-party-notices.txt", _third_party_notices, methods=["GET"])
    if base == "/":
        application.add_api_route("/", get_login, methods=["GET"])
    else:
        application.add_api_route(base, get_login, methods=["GET"])
    application.add_api_route(
        "/{full_path:path}",
        _unavailable,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    return application


class InstanceGuard:
    """Reject the wrong host, origin, or proxy before a route runs."""

    def __init__(self, app: object, runtime: Runtime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        decision = evaluate_access(scope, self.runtime.config)
        if not decision.allowed:
            response = json_error(403, "forbidden")
            await response(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        config = self.runtime.config
        state["network_origin"] = decision.network_origin
        state["session"] = open_session(
            config.state_directory,
            instance_id=config.instance_id,
            cookie=_cookie(scope, config.cookie_name),
            epoch=current_epoch(config.state_directory),
            now=self.runtime.clock.now(),
        )
        await self.app(scope, receive, _guard_send(config.base_url, send))


async def _unavailable(request: Request, full_path: str) -> Response:
    config = request.app.state.runtime.config
    path = "/" + full_path
    base = config.base_path
    under_base = base == "/" or path == base.rstrip("/") or path.startswith(base)
    if not under_base:
        return json_error(404, "not_found")
    session = request.scope["state"].get("session")
    if session is None:
        return json_error(401, "authentication_required")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        presented = request.headers.get("x-csrf-token", "")
        if not csrf_matches(session, presented):
            return json_error(403, "forbidden")
    return json_error(404, "not_available")


def _css() -> Response:
    return _static("app.css", "text/css; charset=utf-8")


def _script() -> Response:
    return _static("app.bundle.js", "text/javascript; charset=utf-8")


def _asset(asset: str) -> Response:
    import re

    parts = asset.split("/")
    if len(parts) > 2 or any(part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", part) for part in parts):
        return json_error(404, "not_found")
    if len(parts) == 2 and parts[0] not in {"cmaps", "standard_fonts", "wasm", "iccs"}:
        return json_error(404, "not_found")
    path = STATIC_DIRECTORY / "assets" / Path(*parts)
    if not path.is_file() or path.is_symlink():
        return json_error(404, "not_found")
    media = "text/javascript; charset=utf-8" if asset.endswith((".js", ".mjs")) else "application/octet-stream"
    return Response(path.read_bytes(), media_type=media, headers=SECURITY_HEADERS)


def _third_party_notices() -> Response:
    path = STATIC_DIRECTORY / "THIRD_PARTY_NOTICES.txt"
    if not path.is_file():
        return json_error(404, "not_found")
    return _static("THIRD_PARTY_NOTICES.txt", "text/plain; charset=utf-8")


def _static(name: str, media_type: str) -> Response:
    body = (STATIC_DIRECTORY / name).read_bytes()
    return Response(body, media_type=media_type, headers=SECURITY_HEADERS)


def _cookie(scope: dict, name: str) -> str | None:
    raw = None
    for key, value in scope.get("headers", []):
        if key.lower() == b"cookie":
            if raw is not None:
                return None
            raw = value.decode("latin-1")
    if raw is None:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(raw)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(name)
    if morsel is None:
        return None
    return morsel.value


def _guard_send(base_url: str, send: object):
    blocked = False

    async def send_wrapper(message: dict) -> None:
        nonlocal blocked
        if blocked:
            return
        if message["type"] == "http.response.start":
            headers = list(message.get("headers") or [])
            location = _header_value(headers, b"location")
            if location is not None and not location.startswith(base_url):
                blocked = True
                body = b'{"error":"forbidden"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json"), *_encoded_headers()],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            present = {name.lower() for name, _value in headers}
            for name, value in SECURITY_HEADERS.items():
                encoded = name.lower().encode("latin-1")
                if encoded not in present:
                    headers.append((encoded, value.encode("latin-1")))
            message = {**message, "headers": headers}
        await send(message)

    return send_wrapper


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    values = [value.decode("latin-1") for key, value in headers if key.lower() == name]
    if len(values) != 1:
        return None
    return values[0]


def _encoded_headers() -> list[tuple[bytes, bytes]]:
    return [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in SECURITY_HEADERS.items()
    ]
