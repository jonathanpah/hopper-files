"""Authenticated search, derived Markdown-tag, and monitored-folder routes.

Search and the tag index walk directory trees for up to the traversal budget.
They run in the thread pool, like saves and directory sizes, so listings and
openings are not queued behind them (HF-NAV-011).
"""

from __future__ import annotations

import json
import os

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from hopper_files.api.files import _catalog
from hopper_files.api.request_body import CONTROL_BODY_MAX_BYTES, BodyTooLarge, read_limited_body
from hopper_files.config import ConfigError
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.roots import BASE_ID, AddressRejected, RootCatalog, open_existing_directory, relative_address
from hopper_files.search import (
    SearchError,
    derive_tag_index,
    parse_search_parameters,
    search,
)
from hopper_files.sessions import csrf_matches
from hopper_files.state import (
    StateError,
    TagFolderLimit,
    UiStateValidationError,
    ignored_tags,
    load_tag_folders,
    load_ui_state,
    set_tag_folder,
)


async def get_search(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        parameters = parse_search_parameters(request.query_params)
        cursor_values = request.query_params.getlist("cursor")
        if len(cursor_values) > 1:
            raise SearchError(400, "invalid_cursor")
        cursor = cursor_values[0] if cursor_values else None
        catalog = _catalog(request)
        runtime = request.app.state.runtime
        result = await run_in_threadpool(
            search,
            catalog,
            parameters,
            runtime.search_cursors,
            instance_id=runtime.config.instance_id,
            now=runtime.clock.now(),
            cursor=cursor,
        )
        after = _catalog(request)
        if after.generation != catalog.generation:
            raise SearchError(409, "conflict")
    except SearchError as exc:
        return json_error(exc.status, exc.code)
    except (ConfigError, StateError):
        return json_error(409, "conflict")
    return _json(result)


async def get_tags(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        values = request.query_params.getlist("tag")
        if len(values) > 1:
            raise SearchError(400, "invalid_request")
        catalog = _catalog(request)
        state_directory = request.app.state.runtime.config.state_directory
        ignored = ignored_tags(load_ui_state(state_directory))
        folders = tuple(relative_address(folder) for folder in load_tag_folders(state_directory))
        result = await run_in_threadpool(
            derive_tag_index, catalog, requested_tag=values[0] if values else None, ignored=ignored, folders=folders
        )
        after = _catalog(request)
        if after.generation != catalog.generation:
            raise SearchError(409, "conflict")
    except SearchError as exc:
        return json_error(exc.status, exc.code)
    except (AddressRejected, ConfigError, StateError):
        return json_error(409, "conflict")
    return _json(result)


async def get_tag_folders(request: Request) -> Response:
    """Return the folders whose notes feed the tag index (HF-META-002)."""
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        catalog = _catalog(request)
        folders = load_tag_folders(request.app.state.runtime.config.state_directory)
        return _json({"folders": await run_in_threadpool(_describe_folders, catalog, folders)})
    except (AddressRejected, ConfigError, StateError):
        return json_error(409, "conflict")


async def post_tag_folders(request: Request) -> Response:
    """Mark or unmark one folder as monitored; only a directory the account can list can be marked."""
    session = request.scope["state"].get("session")
    if session is None:
        return json_error(401, "authentication_required")
    if not csrf_matches(session, request.headers.get("x-csrf-token", "")):
        return json_error(403, "forbidden")
    try:
        payload = json.loads((await read_limited_body(request, CONTROL_BODY_MAX_BYTES)).decode("utf-8"))
    except BodyTooLarge:
        return json_error(413, "limit_exceeded")
    except (UnicodeError, json.JSONDecodeError):
        return json_error(422, "invalid_request")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"path", "monitored"}
        or not isinstance(payload["path"], str)
        or not isinstance(payload["monitored"], bool)
    ):
        return json_error(422, "invalid_request")
    address, monitored = payload["path"], payload["monitored"]
    try:
        catalog = _catalog(request)
        relative = relative_address(address)
        if monitored and not await run_in_threadpool(_listable_directory, catalog, relative):
            return json_error(403, "not_directory")
        state_directory = request.app.state.runtime.config.state_directory
        folders = await run_in_threadpool(set_tag_folder, state_directory, address, monitored)
        return _json({"folders": await run_in_threadpool(_describe_folders, catalog, folders)})
    except AddressRejected as exc:
        return json_error(422 if exc.code == "invalid" else 409, "invalid_request" if exc.code == "invalid" else "conflict")
    except TagFolderLimit:
        return json_error(413, "limit_exceeded")
    except UiStateValidationError:
        return json_error(422, "invalid_request")
    except OSError:
        return json_error(503, "storage_unavailable")
    except (ConfigError, StateError):
        return json_error(409, "conflict")


def _describe_folders(catalog: RootCatalog, folders: list[str]) -> list[dict[str, object]]:
    """Each monitored folder and whether the tag index can list it now."""
    return [{"path": folder, "available": _listable_directory(catalog, relative_address(folder))} for folder in folders]


def _listable_directory(catalog: RootCatalog, relative: str) -> bool:
    """Whether ``relative`` is a directory reached without links that the account can list.

    It is the same opening the tag traversal makes at a monitored folder.
    """
    try:
        descriptor = open_existing_directory(catalog, BASE_ID, relative)
    except AddressRejected as exc:
        if exc.code == "unavailable":
            raise
        return False
    os.close(descriptor)
    return True


def _json(payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )
