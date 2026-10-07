"""Authenticated read and compare-and-swap replacement of UI metadata."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import Response

from hopper_files.config import ConfigError, load_config, same_service_identity
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.sessions import csrf_matches
from hopper_files.state import (
    StateError,
    UiStateConflict,
    UiStatePublicationIndeterminate,
    UiStateValidationError,
    load_ui_state,
    replace_ui_state,
)


async def get_state(request: Request) -> Response:
    """Return only the authenticated instance's complete current UI state."""
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        document = load_ui_state(request.app.state.runtime.config.state_directory)
    except StateError:
        return json_error(409, "conflict")
    return _json(200, document)


async def put_state(request: Request) -> Response:
    """Commit a complete document only when the submitted revision is current."""
    session = request.scope["state"].get("session")
    if session is None:
        return json_error(401, "authentication_required")
    if not csrf_matches(session, request.headers.get("x-csrf-token", "")):
        return json_error(403, "forbidden")
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return json_error(422, "invalid_state")
    if not isinstance(payload, dict):
        return json_error(422, "invalid_state")
    if "baseRevision" not in payload:
        return json_error(428, "precondition_required")
    base_revision = payload.get("baseRevision")
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        return json_error(422, "invalid_state")
    document = {key: value for key, value in payload.items() if key != "baseRevision"}
    try:
        _require_same_instance(request)
        committed = replace_ui_state(
            request.app.state.runtime.config.state_directory,
            base_revision=base_revision,
            document=document,
        )
    except UiStateConflict as exc:
        return _json(409, {"error": "conflict", "stateRevision": exc.state_revision})
    except UiStateValidationError:
        return json_error(422, "invalid_state")
    except UiStatePublicationIndeterminate as exc:
        return _json(503, {"error": "indeterminate", "stateRevision": exc.state_revision})
    except OSError:
        return json_error(503, "storage_unavailable")
    except (ConfigError, StateError):
        return json_error(409, "conflict")
    return _json(200, committed)


def _require_same_instance(request: Request) -> None:
    """Refuse a write when the configuration now names another instance."""
    running = request.app.state.runtime.config
    reloaded = load_config(running.source_path)
    if not same_service_identity(reloaded, running):
        raise ConfigError("instance identity changed")


def _json(status: int, payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )
