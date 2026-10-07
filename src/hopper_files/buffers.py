"""Cooperative, content-free editor buffer holds for multi-file publication."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hopper_files.state import StateError, atomic_write, read_signing_secret

BUFFER_FRESH_SECONDS = 8
BUFFER_PREPARE_SECONDS = 4
MAX_OPEN_BUFFERS = 64


class BufferConflict(Exception):
    """An affected editor buffer is dirty, stale, or did not acknowledge."""


class BufferRegistry:
    """Store only addresses, strong versions, dirty flags, and short commands."""

    def __init__(self, state_directory: Path, instance_id: str) -> None:
        self.state_directory = Path(state_directory).resolve()
        self.instance_id = instance_id

    def sync(
        self,
        session_id: str,
        expires_at: float,
        documents: object,
        acknowledgements: object,
        now: float,
    ) -> dict[str, object]:
        if not _session_id(session_id) or not isinstance(documents, list) or len(documents) > MAX_OPEN_BUFFERS:
            raise BufferConflict("invalid buffer registration")
        parsed: dict[str, dict[str, object]] = {}
        for value in documents:
            item = _buffer(value)
            parsed[_key(item["rootId"], item["path"])] = item
        if acknowledgements is None:
            acknowledgements = []
        if not isinstance(acknowledgements, list) or len(acknowledgements) > 64:
            raise BufferConflict("invalid buffer acknowledgement")
        with self._lock():
            state = self._read_locked()
            sessions = state["sessions"]
            session = sessions.get(session_id)
            if not isinstance(session, dict):
                session = {"expiresAt": expires_at, "lastSeen": now, "documents": {}, "acks": {}}
            session.update({"expiresAt": expires_at, "lastSeen": now, "documents": parsed})
            acks = session.get("acks")
            if not isinstance(acks, dict):
                acks = {}
            for ack in acknowledgements:
                if not isinstance(ack, dict) or set(ack) != {"moveId", "accepted", "documents"}:
                    raise BufferConflict("invalid buffer acknowledgement")
                move_id = ack["moveId"]
                if not isinstance(move_id, str) or not _move_id(move_id) or not isinstance(ack["accepted"], bool):
                    raise BufferConflict("invalid buffer acknowledgement")
                versions = ack["documents"]
                if not isinstance(versions, list) or len(versions) > MAX_OPEN_BUFFERS:
                    raise BufferConflict("invalid buffer acknowledgement")
                normalized_versions = []
                for version in versions:
                    if not isinstance(version, dict) or set(version) != {"rootId", "path", "version", "dirty"}:
                        raise BufferConflict("invalid buffer acknowledgement")
                    item = _buffer(version)
                    normalized_versions.append(item)
                acks[move_id] = {"accepted": ack["accepted"], "documents": normalized_versions, "at": now}
            session["acks"] = acks
            sessions[session_id] = session
            self._prune_locked(state, now)
            self._write_locked(state)
            commands = []
            for move_id, command in state["moves"].items():
                if session_id not in command.get("sessions", []):
                    continue
                if command.get("status") == "preparing":
                    docs = [item for item in command["documents"] if item["sessionId"] == session_id]
                    commands.append({"id": move_id, "type": "hold", "documents": [item["buffer"] for item in docs]})
                elif command.get("status") in {"complete", "released"}:
                    docs = command.get("results", {}).get(session_id, [])
                    commands.append({"id": move_id, "type": command["status"], "documents": docs})
            return {"commands": commands}

    def prepare(
        self,
        documents: list[dict[str, object]],
        now: float,
        identifier: str | None = None,
    ) -> str | None:
        """Ask every currently connected session with an affected buffer to hold it."""
        if not documents:
            return None
        identifier = secrets.token_hex(16) if identifier is None else identifier
        if not _move_id(identifier):
            raise BufferConflict("invalid move identifier")
        with self._lock():
            state = self._read_locked()
            self._prune_locked(state, now)
            sessions = state["sessions"]
            expected: list[dict[str, object]] = []
            session_ids: set[str] = set()
            for session_id, session in sessions.items():
                buffers = session.get("documents", {})
                if not isinstance(buffers, dict):
                    raise BufferConflict("buffer registry is invalid")
                for target in documents:
                    key = _key(target["rootId"], target["path"])
                    buffer = buffers.get(key)
                    if not isinstance(buffer, dict):
                        continue
                    if now - float(session.get("lastSeen", 0)) > BUFFER_FRESH_SECONDS:
                        raise BufferConflict("affected editor is unreachable")
                    if buffer.get("dirty") is not False or buffer.get("version") != target["version"]:
                        raise BufferConflict("affected editor is dirty or changed")
                    new_buffer = {
                        "rootId": target["rootId"], "path": target["path"],
                        "version": target["version"], "dirty": False,
                        "newRootId": target["newRootId"], "newPath": target["newPath"],
                    }
                    expected.append({"sessionId": session_id, "buffer": new_buffer})
                    session_ids.add(session_id)
            if not expected:
                return None
            state["moves"][identifier] = {
                "status": "preparing",
                "createdAt": now,
                "sessions": sorted(session_ids),
                "documents": expected,
                "results": {},
            }
            self._write_locked(state)

        deadline = time.monotonic() + BUFFER_PREPARE_SECONDS
        while time.monotonic() < deadline:
            with self._lock():
                state = self._read_locked()
                command = state["moves"].get(identifier)
                if not isinstance(command, dict):
                    raise StateError("buffer hold record is missing")
                rejected = False
                pending = False
                for entry in command["documents"]:
                    session = state["sessions"].get(entry["sessionId"])
                    ack = session.get("acks", {}).get(identifier) if isinstance(session, dict) else None
                    if not isinstance(ack, dict):
                        pending = True
                        continue
                    if ack.get("accepted") is not True:
                        rejected = True
                        break
                    acknowledged = {
                        _key(item["rootId"], item["path"]): item
                        for item in ack.get("documents", []) if isinstance(item, dict)
                    }
                    wanted = entry["buffer"]
                    observed = acknowledged.get(_key(wanted["rootId"], wanted["path"]))
                    if (
                        not isinstance(observed, dict)
                        or observed.get("version") != wanted["version"]
                        or observed.get("dirty") is not False
                    ):
                        rejected = True
                        break
                if rejected:
                    command["status"] = "released"
                    command["results"] = _release_results(command)
                    self._write_locked(state)
                    raise BufferConflict("affected editor refused the move")
                if not pending:
                    return identifier
            time.sleep(0.04)
        self.release(identifier, now)
        raise BufferConflict("affected editor did not acknowledge the move")

    def finish(self, move_id: str | None, results: dict[str, list[dict[str, object]]], now: float) -> None:
        if move_id is None:
            return
        with self._lock():
            state = self._read_locked()
            command = state["moves"].get(move_id)
            if isinstance(command, dict):
                command["status"] = "complete"
                command["completedAt"] = now
                completed: dict[str, list[dict[str, object]]] = {}
                for entry in command.get("documents", []):
                    if not isinstance(entry, dict) or not isinstance(entry.get("buffer"), dict):
                        continue
                    buffer = entry["buffer"]
                    completed.setdefault(str(entry["sessionId"]), []).append({
                        "rootId": buffer["rootId"],
                        "path": buffer["path"],
                        "newRootId": buffer["newRootId"],
                        "newPath": buffer["newPath"],
                    })
                command["results"] = completed if not results else results
                self._write_locked(state)

    def release(self, move_id: str | None, now: float) -> None:
        if move_id is None:
            return
        with self._lock():
            state = self._read_locked()
            command = state["moves"].get(move_id)
            if isinstance(command, dict):
                command["status"] = "released"
                command["completedAt"] = now
                command["results"] = _release_results(command)
                self._write_locked(state)

    def _lock(self):
        return _exclusive(self.state_directory / "buffers.lock")

    def _read_locked(self) -> dict[str, object]:
        path = self.state_directory / "attachments" / "buffers.json"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "instanceId": self.instance_id, "sessions": {}, "moves": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError("buffer registry is unavailable") from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("document"), dict):
            raise StateError("buffer registry is invalid")
        document = envelope["document"]
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(read_signing_secret(self.state_directory), canonical, hashlib.sha256).hexdigest()
        if (
            document.get("version") != 1
            or document.get("instanceId") != self.instance_id
            or not isinstance(document.get("sessions"), dict)
            or not isinstance(document.get("moves"), dict)
            or not isinstance(envelope.get("mac"), str)
            or not hmac.compare_digest(envelope["mac"], expected)
        ):
            raise StateError("buffer registry authentication failed")
        return document

    def _write_locked(self, document: dict[str, object]) -> None:
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        mac = hmac.new(read_signing_secret(self.state_directory), canonical, hashlib.sha256).hexdigest()
        envelope = json.dumps({"document": document, "mac": mac}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        atomic_write(self.state_directory / "attachments" / "buffers.json", envelope)

    def _prune_locked(self, state: dict[str, object], now: float) -> None:
        sessions = state["sessions"]
        for session_id, session in list(sessions.items()):
            if not isinstance(session, dict) or float(session.get("expiresAt", 0)) <= now:
                sessions.pop(session_id, None)
        moves = state["moves"]
        for move_id, command in list(moves.items()):
            completed = command.get("completedAt") if isinstance(command, dict) else None
            if isinstance(completed, (int, float)) and now - completed > 60:
                moves.pop(move_id, None)


class _exclusive:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd = -1

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise StateError("buffer registry lock is unavailable") from exc
        details = os.fstat(self.fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            os.close(self.fd)
            raise StateError("buffer registry lock is invalid")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _buffer(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"rootId", "path", "version", "dirty"}:
        raise BufferConflict("invalid buffer registration")
    root_id, path, version, dirty = value["rootId"], value["path"], value["version"], value["dirty"]
    if (
        not isinstance(root_id, str) or not root_id or len(root_id) > 64
        or not isinstance(path, str) or len(path) > 4096 or path.startswith("/") or "\\" in path
        or not isinstance(version, str) or not version or len(version) > 128
        or not isinstance(dirty, bool)
    ):
        raise BufferConflict("invalid buffer registration")
    return {"rootId": root_id, "path": path, "version": version, "dirty": dirty}


def _key(root_id: object, path: object) -> str:
    return f"{root_id}\0{path}"


def _session_id(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _move_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _release_results(command: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    results: dict[str, list[dict[str, object]]] = {}
    for entry in command.get("documents", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("buffer"), dict):
            continue
        value = entry["buffer"]
        results.setdefault(str(entry["sessionId"]), []).append({
            "rootId": value["rootId"], "path": value["path"], "released": True,
        })
    return results
