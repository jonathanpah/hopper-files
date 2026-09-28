"""Persistent login token buckets. Time continues across a restart."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import fcntl

# One token is 60_000 units. Origin refills 1 unit per millisecond (1/60s).
# Global refills 4 units per millisecond (1/15s).
TOKEN_UNITS = 60_000
ORIGIN_CAPACITY = 5 * TOKEN_UNITS
ORIGIN_PER_MS = 1
GLOBAL_CAPACITY = 20 * TOKEN_UNITS
GLOBAL_PER_MS = 4
MAX_ORIGINS = 256


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    retry_after: int


def consume_login_attempt(state_directory: Path, origin: str, now: float) -> LimitDecision:
    """Consume one origin token and one global token, persisting the result."""
    path = state_directory / "limiter.json"
    lock_path = state_directory / "limiter.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _read(path)
        now_ms = int(now * 1000)
        global_bucket = _refill(
            state["global"],
            now_ms,
            GLOBAL_CAPACITY,
            GLOBAL_PER_MS,
        )
        origins = state["origins"]
        origin_bucket = _refill(
            origins.get(origin),
            now_ms,
            ORIGIN_CAPACITY,
            ORIGIN_PER_MS,
        )
        if origin_bucket["tokens"] < TOKEN_UNITS or global_bucket["tokens"] < TOKEN_UNITS:
            origins[origin] = origin_bucket
            state["global"] = global_bucket
            state["origins"] = _trim(origins)
            _write(path, state)
            return LimitDecision(
                allowed=False,
                retry_after=_retry_after(origin_bucket, global_bucket),
            )
        origin_bucket["tokens"] -= TOKEN_UNITS
        global_bucket["tokens"] -= TOKEN_UNITS
        origins[origin] = origin_bucket
        state["global"] = global_bucket
        state["origins"] = _trim(origins)
        _write(path, state)
        return LimitDecision(allowed=True, retry_after=0)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _retry_after(origin_bucket: dict[str, int], global_bucket: dict[str, int]) -> int:
    waits = (
        _wait_ms(origin_bucket["tokens"], ORIGIN_PER_MS),
        _wait_ms(global_bucket["tokens"], GLOBAL_PER_MS),
    )
    delay = max(waits) / 1000
    if delay <= 0:
        return 1
    return max(1, math.ceil(delay - 1e-12))


def _wait_ms(tokens: int, per_ms: int) -> int:
    if tokens >= TOKEN_UNITS:
        return 0
    needed = TOKEN_UNITS - tokens
    return math.ceil(needed / per_ms)


def _refill(
    bucket: dict[str, int] | None,
    now_ms: int,
    capacity: int,
    per_ms: int,
) -> dict[str, int]:
    if bucket is None:
        return {"tokens": capacity, "updated_ms": now_ms}
    updated = int(bucket["updated_ms"])
    tokens = int(bucket["tokens"])
    if now_ms <= updated:
        return {"tokens": min(capacity, tokens), "updated_ms": updated}
    gained = (now_ms - updated) * per_ms
    return {"tokens": min(capacity, tokens + gained), "updated_ms": now_ms}


def _trim(origins: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    if len(origins) <= MAX_ORIGINS:
        return origins
    ordered = sorted(origins, key=lambda key: origins[key]["tokens"], reverse=True)
    for key in ordered[: len(origins) - MAX_ORIGINS]:
        del origins[key]
    return origins


def _read(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"version": 1, "global": None, "origins": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"version": 1, "global": None, "origins": {}}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {"version": 1, "global": None, "origins": {}}
    origins = payload.get("origins")
    if not isinstance(origins, dict):
        origins = {}
    cleaned: dict[str, dict[str, int]] = {}
    for key, value in origins.items():
        parsed = _bucket(value)
        if isinstance(key, str) and parsed is not None:
            cleaned[key] = parsed
    return {
        "version": 1,
        "global": _bucket(payload.get("global")),
        "origins": cleaned,
    }


def _bucket(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    tokens = value.get("tokens")
    updated = value.get("updated_ms")
    if isinstance(tokens, bool) or isinstance(updated, bool):
        return None
    if not isinstance(tokens, int) or not isinstance(updated, int):
        return None
    if tokens < 0 or updated < 0:
        return None
    return {"tokens": tokens, "updated_ms": updated}


def _write(path: Path, state: dict[str, object]) -> None:
    from hopper_files.state import atomic_write

    global_bucket = state["global"]
    if global_bucket is None:
        global_bucket = {"tokens": GLOBAL_CAPACITY, "updated_ms": 0}
    payload = {
        "version": 1,
        "global": global_bucket,
        "origins": state["origins"],
    }
    atomic_write(
        path,
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii"),
    )
