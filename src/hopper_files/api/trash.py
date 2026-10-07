"""Authenticated trash listing, deletion, and restoration."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import Response

from hopper_files.config import ConfigError, load_config, same_service_identity
from hopper_files.responses import SECURITY_HEADERS, json_error
from hopper_files.roots import build_catalog
from hopper_files.sessions import csrf_matches
from hopper_files.state import StateError
from hopper_files.trash import TrashError, TrashStore


_STATUS = {
    "invalid": 422,
    "unsupported_metadata": 422,
    "quarantined": 422,
    "not_found": 404,
    "unavailable": 404,
    "forbidden": 403,
    "limit": 413,
    "destination_occupied": 409,
    "metadata_conflict": 409,
    "source_unavailable": 409,
    "indeterminate": 409,
    "conflict": 409,
    "inconclusive": 409,
    "ambiguous_image": 409,
    "partial_restore": 409,
}


async def get_trash(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    try:
        entries = _store(request).list_entries(_catalog(request), request.app.state.runtime.clock.now())
    except (ConfigError, StateError):
        return json_error(409, "conflict")
    except TrashError as exc:
        return _trash_error(exc)
    return _json(200, {"entries": entries})


async def post_trash(request: Request) -> Response:
    if not _mutation_authorized(request):
        return _mutation_denied(request)
    try:
        payload = _body(await _read_body(request))
        store = _store(request)
        catalog = _catalog(request)
        now = request.app.state.runtime.clock.now()
        action = payload.get("action")
        if action == "delete":
            source = payload.get("source")
            if set(payload) != {"action", "source"} or not isinstance(source, dict) or set(source) != {"rootId", "path"}:
                return json_error(422, "invalid_request")
            if not isinstance(source["rootId"], str) or not isinstance(source["path"], str):
                return json_error(422, "invalid_request")
            with request.app.state.runtime.images.lock():
                result = store.delete(catalog, source["rootId"], source["path"], now)
            return _json(201, result)
        if action == "restore":
            allowed = {"action", "id"}
            if "alternativePath" in payload:
                allowed.add("alternativePath")
            if "relatedImageIds" in payload:
                allowed.add("relatedImageIds")
            if set(payload) != allowed or not isinstance(payload.get("id"), str):
                return json_error(422, "invalid_request")
            alternative = payload.get("alternativePath")
            if alternative is not None and not isinstance(alternative, str):
                return json_error(422, "invalid_request")
            image_ids = payload.get("relatedImageIds")
            if image_ids is not None and (
                not isinstance(image_ids, list) or any(not isinstance(value, str) for value in image_ids)
                or alternative is not None
            ):
                return json_error(422, "invalid_request")
            with request.app.state.runtime.images.lock():
                if image_ids is None:
                    result = store.restore(catalog, payload["id"], now, alternative)
                else:
                    result = store.restore_with_images(catalog, payload["id"], image_ids, now)
            return _json(200, result)
        return json_error(422, "invalid_request")
    except (UnicodeError, json.JSONDecodeError):
        return json_error(422, "invalid_request")
    except TrashError as exc:
        return _trash_error(exc)
    except (ConfigError, StateError):
        return json_error(409, "conflict")


async def get_related_trash_images(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    identifier = request.query_params.get("id")
    if not isinstance(identifier, str) or not identifier:
        return json_error(422, "invalid_request")
    try:
        catalog = _catalog(request)
        result = _store(request).related_restore_images(
            catalog, identifier, request.app.state.runtime.clock.now()
        )
    except TrashError as exc:
        return _trash_error(exc)
    except (ConfigError, StateError):
        return json_error(409, "conflict")
    return _json(200, result)


def _store(request: Request) -> TrashStore:
    return request.app.state.runtime.trash


def _catalog(request: Request):
    running = request.app.state.runtime.config
    reloaded = load_config(running.source_path)
    if not same_service_identity(reloaded, running):
        raise ConfigError("instance identity changed")
    return build_catalog(reloaded, strict=False)


def _mutation_authorized(request: Request) -> bool:
    session = request.scope["state"].get("session")
    return session is not None and csrf_matches(session, request.headers.get("x-csrf-token", ""))


def _mutation_denied(request: Request) -> Response:
    if request.scope["state"].get("session") is None:
        return json_error(401, "authentication_required")
    return json_error(403, "forbidden")


async def _read_body(request: Request) -> bytes:
    maximum = 64 * 1024
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > maximum):
        raise TrashError("limit")
    body = bytearray()
    async for block in request.stream():
        if len(body) + len(block) > maximum:
            raise TrashError("limit")
        body.extend(block)
    return bytes(body)


def _body(raw: bytes) -> dict[str, object]:
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TrashError("invalid")
    return payload


def _trash_error(error: TrashError) -> Response:
    status = _STATUS.get(error.code, 422)
    code = {
        "limit": "limit_exceeded",
        "invalid": "invalid_request",
        "unavailable": "not_found",
        "not_found": "not_found",
        "forbidden": "forbidden",
    }.get(error.code, error.code)
    body = {"error": code, **error.details}
    return _json(status, body)


def _json(status: int, payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )
