"""Server-side sessions, pre-authentication nonces, and CSRF tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from hopper_files.state import atomic_write, read_signing_secret

NONCE_TTL_SECONDS = 600
NONCE_LIMIT = 128


@dataclass(frozen=True)
class Session:
    path: Path
    csrf_token: str
    epoch: int
    expires_at: float


def issue_nonce(state_directory: Path, now: float) -> str:
    directory = state_directory / "nonces"
    _delete_expired_nonces(directory, now)
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    while len(entries) >= NONCE_LIMIT:
        entries.pop(0).unlink(missing_ok=True)
    token = secrets.token_urlsafe(32)
    expiry = str(now + NONCE_TTL_SECONDS).encode("ascii")
    atomic_write(directory / _digest_name(token), expiry)
    return token


def consume_nonce(state_directory: Path, token: str, now: float) -> bool:
    # Issued nonces are ASCII; anything else cannot match and must not reach the encoder.
    if token == "" or not token.isascii():
        return False
    path = state_directory / "nonces" / _digest_name(token)
    temporary = path.with_name(path.name + ".used")
    try:
        os.rename(path, temporary)
    except FileNotFoundError:
        return False
    try:
        expiry = float(temporary.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError):
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return expiry > now


def create_session(
    state_directory: Path,
    *,
    instance_id: str,
    epoch: int,
    lifetime: int,
    now: float,
) -> tuple[str, str]:
    session_id = secrets.token_bytes(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = now + lifetime
    payload = {
        "version": 1,
        "instanceId": instance_id,
        "epoch": epoch,
        "expiresAt": expires_at,
        "csrfToken": csrf_token,
    }
    atomic_write(
        state_directory / "sessions" / _session_name(session_id),
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    cookie = seal_cookie(read_signing_secret(state_directory), instance_id, session_id)
    return cookie, csrf_token


def open_session(
    state_directory: Path,
    *,
    instance_id: str,
    cookie: str | None,
    epoch: int | None,
    now: float,
) -> Session | None:
    if cookie is None or epoch is None:
        return None
    try:
        session_id = open_cookie(read_signing_secret(state_directory), instance_id, cookie)
    except (ValueError, OSError):
        return None
    path = state_directory / "sessions" / _session_name(session_id)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    if payload.get("instanceId") != instance_id or payload.get("epoch") != epoch:
        return None
    expires_at = payload.get("expiresAt")
    csrf_token = payload.get("csrfToken")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return None
    if not isinstance(csrf_token, str) or csrf_token == "":
        return None
    if expires_at <= now:
        path.unlink(missing_ok=True)
        return None
    return Session(path=path, csrf_token=csrf_token, epoch=epoch, expires_at=float(expires_at))


def csrf_matches(session: Session, presented: str) -> bool:
    if presented == "":
        return False
    left = presented.encode("utf-8")
    right = session.csrf_token.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def revoke_session(session: Session) -> None:
    session.path.unlink(missing_ok=True)


def revoke_all_sessions(state_directory: Path) -> None:
    directory = state_directory / "sessions"
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink():
            path.unlink()


def seal_cookie(secret: bytes, instance_id: str, session_id: bytes) -> str:
    mac = hmac.new(
        secret,
        instance_id.encode("utf-8") + b"|" + session_id,
        hashlib.sha256,
    ).digest()
    return f"{_b64(session_id)}.{_b64(mac)}"


def open_cookie(secret: bytes, instance_id: str, cookie: str) -> bytes:
    session_text, separator, mac_text = cookie.partition(".")
    if separator != "." or session_text == "" or mac_text == "":
        raise ValueError("cookie is malformed")
    session_id = _b64_decode(session_text)
    presented = _b64_decode(mac_text)
    expected = hmac.new(
        secret,
        instance_id.encode("utf-8") + b"|" + session_id,
        hashlib.sha256,
    ).digest()
    if len(presented) != len(expected) or not hmac.compare_digest(presented, expected):
        raise ValueError("cookie signature was rejected")
    return session_id


def _delete_expired_nonces(directory: Path, now: float) -> None:
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            expiry = float(path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if expiry <= now:
            path.unlink(missing_ok=True)


def _digest_name(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_name(session_id: bytes) -> str:
    return hashlib.sha256(session_id).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
