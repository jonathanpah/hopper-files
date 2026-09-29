"""Long-poll route that reports changes in the directories a view shows."""

from __future__ import annotations

import asyncio
import json

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from hopper_files.api.files import _address_error, _catalog, _json
from hopper_files.api.request_body import BodyTooLarge, read_limited_body
from hopper_files.changes import Collected
from hopper_files.config import ConfigError
from hopper_files.responses import json_error
from hopper_files.roots import AddressRejected, root_record
from hopper_files.sessions import csrf_matches
from hopper_files.state import StateError

CHANGES_BODY_MAX_BYTES = 64 * 1024
CHANGES_MAX_PATHS = 256
# A request without news is answered after this many seconds, well below the
# idle timeouts of common proxies.
HOLD_SECONDS = 20.0
# Notices that arrive together are answered together.
GATHER_SECONDS = 0.2
_EPOCH_MAX_LENGTH = 128


async def post_changes(request: Request) -> Response:
    session = request.scope["state"].get("session")
    if session is None:
        return json_error(401, "authentication_required")
    if not csrf_matches(session, request.headers.get("x-csrf-token", "")):
        return json_error(403, "forbidden")
    try:
        body = await read_limited_body(request, CHANGES_BODY_MAX_BYTES)
    except BodyTooLarge:
        return json_error(413, "limit_exceeded")
    parsed = _parse(body)
    if parsed is None:
        return json_error(422, "invalid_request")
    root_id, paths, epoch, seq = parsed
    try:
        catalog = _catalog(request)
        if root_record(catalog, root_id) is None:
            raise AddressRejected("not_found")
    except (AddressRejected, ConfigError, StateError) as exc:
        return _address_error(exc)
    watcher = request.app.state.runtime.changes
    if not watcher.available():
        return _json(200, _payload(False, watcher.epoch, watcher.current_seq(), False, (), []))
    rejected = await run_in_threadpool(watcher.watch, catalog, root_id, paths)
    watched = set(paths) - set(rejected)
    try:
        collected = await _wait(request, watcher, epoch, seq, watched)
    finally:
        watcher.mention(watched)
    return _json(200, _payload(watcher.active(), watcher.epoch, collected.seq, collected.resync, collected.changes, rejected))


def _parse(body: bytes) -> tuple[str, list[str], str | None, int | None] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"rootId", "paths", "epoch", "seq"}:
        return None
    root_id, paths, epoch, seq = payload["rootId"], payload["paths"], payload["epoch"], payload["seq"]
    if not isinstance(root_id, str) or not root_id:
        return None
    if not isinstance(paths, list) or len(paths) > CHANGES_MAX_PATHS or not all(isinstance(item, str) for item in paths):
        return None
    if epoch is not None and (not isinstance(epoch, str) or len(epoch) > _EPOCH_MAX_LENGTH):
        return None
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        return None
    return root_id, list(dict.fromkeys(paths)), epoch, seq


async def _wait(request: Request, watcher, epoch: str | None, seq: int | None, paths: set[str]) -> Collected:
    """Answer at once, after gathering fresh notices, or when the hold ends."""
    loop = asyncio.get_running_loop()
    woken = asyncio.Event()

    def wake() -> None:
        loop.call_soon_threadsafe(woken.set)

    watcher.subscribe(wake)
    gone = asyncio.ensure_future(_disconnected(request))
    try:
        deadline = loop.time() + HOLD_SECONDS
        ready_at: float | None = None
        while True:
            woken.clear()
            collected = watcher.collect(epoch, seq, paths)
            now = loop.time()
            if collected.resync or not watcher.active():
                return collected
            if collected.changes:
                if ready_at is None:
                    ready_at = now + GATHER_SECONDS
                if now >= ready_at:
                    return collected
                timeout = ready_at - now
            else:
                if now >= deadline:
                    return collected
                timeout = deadline - now
            waiting = asyncio.ensure_future(woken.wait())
            done, _ = await asyncio.wait({waiting, gone}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            if gone in done:
                return watcher.collect(epoch, seq, paths)
    finally:
        watcher.unsubscribe(wake)
        gone.cancel()


async def _disconnected(request: Request) -> None:
    """Return when the client goes away. The body has been read already."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _payload(
    supported: bool,
    epoch: str,
    seq: int,
    resync: bool,
    changes: tuple[tuple[str, str | None], ...],
    rejected: list[str],
) -> dict[str, object]:
    return {
        "supported": supported,
        "epoch": epoch,
        "seq": seq,
        "resync": resync,
        "changes": [{"path": path, "name": name} for path, name in changes],
        "rejected": rejected,
    }
