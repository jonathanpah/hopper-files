"""Bounded literal search snapshots and Markdown tag derivation."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hopper_files.roots import (
    BASE_ID,
    AddressRejected,
    RootCatalog,
    SearchEntry,
    directory_version,
    open_scanned_regular,
    relative_address,
    scan_tree,
    search_object_version,
)
from hopper_files.state import StateError, atomic_write, read_signing_secret

SEARCH_TIME_LIMIT_SECONDS = 10.0
SEARCH_RESULT_LIMIT = 10_000
SEARCH_SNAPSHOT_LIMIT = 64 * 1024 * 1024
SEARCH_TEXT_LIMIT = 5 * 1024 * 1024
SEARCH_CURSOR_LIFETIME_SECONDS = 15 * 60
_CURSOR_DIRECTORY = "search-cursors"
_CURSOR_RECORD = re.compile(r"[0-9a-f]{32}\.json\Z")
_HEX_COLOR = re.compile(r"(?i)(?<![0-9a-f])#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})(?![0-9a-f])")
_URL = re.compile(r"(?i)(?:https?://|mailto:)[^\s<>()]+")


class SearchError(ValueError):
    """A search request failure with an HTTP-compatible outcome."""

    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SearchParameters:
    mode: str
    query: str
    normalized_query: str
    scope: str
    limit: int

    @property
    def scope_address(self) -> str:
        return "/" + self.scope


def normalize_search_text(value: str) -> str:
    return unicodedata.normalize(
        "NFC", unicodedata.normalize("NFC", value).casefold()
    )


def parse_search_parameters(query_params: object) -> SearchParameters:
    """Parse ``mode``, ``q``, ``scope`` (an absolute directory address), and ``limit``."""
    for key in ("mode", "q", "limit", "scope"):
        if len(query_params.getlist(key)) > 1:
            raise SearchError(400, "invalid_request")
    mode = query_params.get("mode", "name")
    raw_query = query_params.get("q", "")
    if not isinstance(mode, str) or mode not in {"name", "text"}:
        raise SearchError(400, "invalid_request")
    if not isinstance(raw_query, str) or not _unicode_scalar(raw_query) or "\x00" in raw_query:
        raise SearchError(400, "invalid_request")
    if len(raw_query) > 256:
        raise SearchError(413, "limit_exceeded")
    query = raw_query.strip()
    if len(query) < (2 if mode == "text" else 1):
        raise SearchError(400, "invalid_request")
    normalized = normalize_search_text(query)
    raw_scope = query_params.get("scope")
    if not isinstance(raw_scope, str) or not _unicode_scalar(raw_scope):
        raise SearchError(400, "invalid_request")
    try:
        scope = relative_address(raw_scope)
    except AddressRejected as exc:
        raise SearchError(400, "invalid_request") from exc
    raw_limit = query_params.get("limit")
    if raw_limit is None:
        limit = 100
    elif not raw_limit.isdecimal() or len(raw_limit) > 3:
        raise SearchError(400, "invalid_request")
    else:
        limit = int(raw_limit)
    if not 1 <= limit <= 200:
        raise SearchError(400, "invalid_request")
    return SearchParameters(mode, query, normalized, scope, limit)


class SearchCursorStore:
    """Persist private immutable search snapshots below one instance cache."""

    def __init__(self, state_directory: Path, instance_id: str) -> None:
        self.state_directory = state_directory
        self.instance_id = instance_id

    def create(self, record: dict[str, object], now: float) -> str:
        encoded = _encode_record(record)
        if len(encoded) > SEARCH_SNAPSHOT_LIMIT:
            raise SearchError(413, "limit_exceeded")
        directory = self._directory()
        lock_fd = self._lock(directory)
        try:
            self._prune_locked(directory, now)
            candidate = record.get("cursorId")
            identifier = candidate if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{32}", candidate) else secrets.token_hex(16)
            atomic_write(directory / f"{identifier}.json", encoded)
            return identifier
        except OSError as exc:
            raise StateError("search cursor state is unavailable") from exc
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def load(self, identifier: str, now: float) -> dict[str, object]:
        directory = self._directory()
        path = directory / f"{identifier}.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise SearchError(409, "conflict") from exc
        except OSError as exc:
            raise StateError("search cursor state is unavailable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > SEARCH_SNAPSHOT_LIMIT:
                raise StateError("search cursor state is invalid")
            chunks = bytearray()
            while len(chunks) <= SEARCH_SNAPSHOT_LIMIT:
                block = os.read(fd, 65536)
                if not block:
                    break
                chunks.extend(block)
            if len(chunks) > SEARCH_SNAPSHOT_LIMIT:
                raise StateError("search cursor state exceeds its limit")
        finally:
            os.close(fd)
        try:
            payload = json.loads(bytes(chunks).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StateError("search cursor state is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("instanceId") != self.instance_id:
            raise StateError("search cursor state belongs to another instance")
        expiry = payload.get("expiresAt")
        if isinstance(expiry, bool) or not isinstance(expiry, int):
            raise StateError("search cursor expiry is invalid")
        if now >= expiry:
            raise SearchError(410, "cursor_expired")
        return payload

    def sign(self, identifier: str, offset: int, expiry: int) -> str:
        payload = {"id": identifier, "offset": offset, "expiresAt": expiry}
        raw = json.dumps(payload, separators=(",", ":")).encode("ascii")
        token = _b64(raw)
        signature = hmac.new(
            read_signing_secret(self.state_directory),
            (self.instance_id + "\n" + token).encode("ascii"),
            hashlib.sha256,
        ).digest()
        return token + "." + _b64(signature)

    def open_token(self, token: str, now: float) -> tuple[dict[str, object], int]:
        if len(token) > 512 or token.count(".") != 1:
            raise SearchError(400, "invalid_cursor")
        encoded, signature = token.split(".", 1)
        try:
            payload_bytes = _unb64(encoded)
            presented = _unb64(signature)
            payload = json.loads(payload_bytes.decode("ascii"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise SearchError(400, "invalid_cursor") from exc
        expected = hmac.new(
            read_signing_secret(self.state_directory),
            (self.instance_id + "\n" + encoded).encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(presented, expected):
            raise SearchError(400, "invalid_cursor")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"id", "offset", "expiresAt"}
            or not isinstance(payload["id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["id"]) is None
            or isinstance(payload["offset"], bool)
            or not isinstance(payload["offset"], int)
            or payload["offset"] < 0
            or isinstance(payload["expiresAt"], bool)
            or not isinstance(payload["expiresAt"], int)
        ):
            raise SearchError(400, "invalid_cursor")
        if now >= payload["expiresAt"]:
            self._remove_expired(payload["id"])
            raise SearchError(410, "cursor_expired")
        record = self.load(payload["id"], now)
        if record.get("expiresAt") != payload["expiresAt"]:
            raise SearchError(400, "invalid_cursor")
        return record, payload["offset"]

    def _directory(self) -> Path:
        state = self.state_directory
        cache = state / "cache"
        cursors = cache / _CURSOR_DIRECTORY
        try:
            state_info = state.lstat()
            cache_info = cache.lstat()
            if (
                stat.S_ISLNK(state_info.st_mode)
                or not stat.S_ISDIR(state_info.st_mode)
                or stat.S_ISLNK(cache_info.st_mode)
                or not stat.S_ISDIR(cache_info.st_mode)
                or stat.S_IMODE(cache_info.st_mode) != 0o700
            ):
                raise StateError("search cursor directory is invalid")
            try:
                cursors.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = cursors.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                raise StateError("search cursor directory is invalid")
            return cursors
        except OSError as exc:
            raise StateError("search cursor directory is unavailable") from exc

    def _lock(self, directory: Path) -> int:
        path = directory / ".lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                os.close(fd)
                raise StateError("search cursor lock is invalid")
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        except OSError as exc:
            raise StateError("search cursor lock is unavailable") from exc

    def _prune_locked(self, directory: Path, now: float) -> None:
        for entry in directory.iterdir():
            if entry.name == ".lock":
                continue
            if _CURSOR_RECORD.fullmatch(entry.name) is None:
                raise StateError("unexpected search cursor state")
            try:
                info = entry.lstat()
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > SEARCH_SNAPSHOT_LIMIT
                ):
                    raise StateError("search cursor state is invalid")
                payload = json.loads(entry.read_text(encoding="utf-8"))
                expires = payload.get("expiresAt") if isinstance(payload, dict) else None
                if isinstance(expires, bool) or not isinstance(expires, int):
                    raise StateError("search cursor expiry is invalid")
                if now >= expires:
                    entry.unlink()
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise StateError("search cursor state is unreadable") from exc

    def _remove_expired(self, identifier: str) -> None:
        directory = self._directory()
        lock_fd = self._lock(directory)
        try:
            path = directory / f"{identifier}.json"
            try:
                info = path.lstat()
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StateError("search cursor state is invalid")
            path.unlink()
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def search(
    catalog: RootCatalog,
    parameters: SearchParameters,
    store: SearchCursorStore,
    *,
    instance_id: str,
    now: float,
    cursor: str | None = None,
) -> dict[str, object]:
    """Create a bounded immutable search snapshot or return one of its pages."""
    if cursor is not None:
        record, offset = store.open_token(cursor, now)
        if (
            record.get("mode") != parameters.mode
            or record.get("query") != parameters.normalized_query
            or record.get("scope") != parameters.scope
            or record.get("limit") != parameters.limit
        ):
            raise SearchError(400, "cursor_parameters_changed")
        if record.get("rootGeneration") != catalog.generation:
            raise SearchError(409, "conflict")
        items = record.get("items")
        if not isinstance(items, list) or offset > len(items):
            raise SearchError(400, "invalid_cursor")
        _validate_page(record, catalog, offset, parameters.limit)
        return _page(record, items, offset, parameters.limit, store)

    snapshot = _materialize(catalog, parameters, now=now)
    snapshot["instanceId"] = instance_id
    encoded = _encode_record(snapshot)
    if len(encoded) > SEARCH_SNAPSHOT_LIMIT:
        # Keep the bounded prefix and make the inability to page explicit.
        snapshot["omissions"]["snapshot_limit"] = 1
        snapshot["complete"] = False
        snapshot["items"] = snapshot["items"][: parameters.limit]
        return {
            "items": _public_items(snapshot["items"]),
            "nextCursor": None,
            "cursorExpiresAt": None,
            "complete": False,
            "omissions": _counts(snapshot["omissions"]),
            "exclusions": _counts(snapshot["exclusions"]),
        }
    if len(snapshot["items"]) <= parameters.limit:
        return {
            "items": _public_items(snapshot["items"]),
            "nextCursor": None,
            "cursorExpiresAt": None,
            "complete": bool(snapshot["complete"]),
            "omissions": _counts(snapshot["omissions"]),
            "exclusions": _counts(snapshot["exclusions"]),
        }
    identifier = store.create(snapshot, now)
    return _page(snapshot, snapshot["items"], 0, parameters.limit, store, identifier=identifier)


def _page(
    record: dict[str, object],
    items: list[object],
    offset: int,
    limit: int,
    store: SearchCursorStore,
    *,
    identifier: str | None = None,
) -> dict[str, object]:
    page_items = items[offset : offset + limit]
    next_offset = offset + len(page_items)
    has_next = next_offset < len(items)
    expiry = record.get("expiresAt")
    if has_next and identifier is None:
        identifier = record.get("cursorId")
    token = None
    if has_next and isinstance(identifier, str) and isinstance(expiry, int):
        token = store.sign(identifier, next_offset, expiry)
    return {
        "items": _public_items(page_items),
        "nextCursor": token,
        "cursorExpiresAt": _timestamp(expiry) if token is not None and isinstance(expiry, int) else None,
        "complete": bool(record.get("complete")) and not has_next,
        "omissions": _counts(record.get("omissions", {})),
        "exclusions": _counts(record.get("exclusions", {})),
    }


def _materialize(catalog: RootCatalog, parameters: SearchParameters, now: float | None = None) -> dict[str, object]:
    """Descend from the scope under HF-NAV-011 and materialize ordered results.

    Each result records its own version and its parent directory version so a
    later page can revalidate the objects it serves (HF-API-004).
    """
    started = time.monotonic()
    deadline = started + SEARCH_TIME_LIMIT_SECONDS
    expiry = math.ceil((time.time() if now is None else now) + SEARCH_CURSOR_LIFETIME_SECONDS)
    snapshot: dict[str, object] = {
        "version": 2,
        "instanceId": "",
        "cursorId": secrets.token_hex(16),
        "expiresAt": expiry,
        "mode": parameters.mode,
        "query": parameters.normalized_query,
        "scope": parameters.scope,
        "limit": parameters.limit,
        "rootGeneration": catalog.generation,
        "items": [],
        "omissions": {},
        "exclusions": {},
        "complete": True,
    }
    matches: list[dict[str, object]] = []
    total_matches = 0
    seen_bytes = 0
    counts: dict[str, int] = {}
    omissions = snapshot["omissions"]
    exclusions = snapshot["exclusions"]

    def add_match(item: dict[str, object]) -> None:
        nonlocal total_matches
        total_matches += 1
        if len(matches) < SEARCH_RESULT_LIMIT:
            matches.append(item)

    try:
        tree = scan_tree(
            catalog,
            BASE_ID,
            parameters.scope,
            deadline=deadline,
            skip_internal_trash=False,
            counts=counts,
        )
        for directory in tree:
            if not directory.stable:
                raise SearchError(409, "conflict")
            for category, count in directory.exclusions.items():
                if category == "execution_budget":
                    continue
                _add_count(exclusions, category, count)
            stopped = False
            for entry in directory.entries:
                if time.monotonic() >= deadline:
                    _add_count(counts, "execution_budget")
                    stopped = True
                    break
                child_path = f"{directory.path}/{entry.name}" if directory.path else entry.name
                seen_bytes += len(child_path.encode("utf-8")) + 64
                if seen_bytes >= SEARCH_SNAPSHOT_LIMIT * 3 // 4:
                    _add_count(omissions, "snapshot_limit")
                    stopped = True
                    break
                if entry.kind == "other":
                    _add_count(exclusions, "special_file")
                    continue
                if parameters.mode == "name":
                    if _contains(entry.name, parameters.normalized_query):
                        add_match(_item(child_path, entry.name, entry.kind, entry.version, directory.version))
                    continue
                if entry.kind == "link":
                    _add_count(exclusions, "symbolic_link")
                    continue
                if entry.kind != "file":
                    continue
                if entry.size is not None and entry.size > SEARCH_TEXT_LIMIT:
                    _add_count(exclusions, "too_large")
                    continue
                try:
                    matched = _text_file_match(directory.fd, entry, parameters.normalized_query)
                except AddressRejected as exc:
                    if exc.code == "conflict":
                        raise SearchError(409, "conflict") from exc
                    _add_count(omissions, "read_error")
                    continue
                if matched is None:
                    continue
                if matched[0] == "excluded":
                    _add_count(exclusions, matched[1])
                    continue
                add_match(
                    _item(child_path, entry.name, "file", entry.version, directory.version, matched[1], matched[2])
                )
            if stopped:
                break
    except AddressRejected as exc:
        if exc.code == "conflict":
            raise SearchError(409, "conflict") from exc
        # HF-API-004: a scope that is not an openable directory fails the request.
        raise SearchError(404 if exc.code in {"not_found", "forbidden"} else 409, "not_found" if exc.code in {"not_found", "forbidden"} else "conflict") from exc
    for category, count in counts.items():
        if category in {"execution_budget", "entry_race", "depth_limit"}:
            _add_count(omissions, category, count)
        else:
            _add_count(exclusions, category, count)
    if omissions:
        snapshot["complete"] = False
    ordered = sorted(matches, key=_result_key)
    snapshot["items"] = ordered[:SEARCH_RESULT_LIMIT]
    if total_matches > SEARCH_RESULT_LIMIT:
        _add_count(omissions, "result_limit")
        snapshot["complete"] = False
    return snapshot


def _text_file_match(
    parent_fd: int,
    entry: SearchEntry,
    query: str,
) -> tuple[str, int, str] | tuple[str, str] | None:
    fd = open_scanned_regular(parent_fd, entry)
    try:
        before = os.fstat(fd)
        body = bytearray()
        while len(body) <= SEARCH_TEXT_LIMIT:
            chunk = os.read(fd, min(65536, SEARCH_TEXT_LIMIT + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        after = os.fstat(fd)
    except OSError as exc:
        raise AddressRejected("unavailable") from exc
    finally:
        os.close(fd)
    if _version(after) != entry.version or _version(before) != entry.version:
        raise AddressRejected("conflict")
    if len(body) > SEARCH_TEXT_LIMIT:
        return "excluded", "too_large"
    if len(body) != before.st_size and not _pseudo_size(before):
        raise AddressRejected("conflict")
    if b"\x00" in body:
        return "excluded", "nul"
    try:
        text = bytes(body).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "excluded", "invalid_utf8"
    for number, line in enumerate(text.split("\n"), start=1):
        if line.endswith("\r"):
            line = line[:-1]
        normalized = normalize_search_text(line)
        position = normalized.find(query)
        if position >= 0:
            snippet = _literal_snippet(line, position, len(query))
            return "match", number, snippet
    return None


def _pseudo_size(info: os.stat_result) -> bool:
    """Files on pseudo filesystems report a size that is not their content length."""
    return info.st_size == 0


_SNIPPET_WINDOW = 1024


def _literal_snippet(line: str, normalized_start: int, normalized_length: int) -> str:
    """Return at most 200 source code points around a normalized match.

    A long line is first narrowed, with a binary search over normalized
    prefixes, to a window around the match, so a single 5 MiB line does not
    cost a per-character Python loop.
    """
    if len(line) <= 2 * _SNIPPET_WINDOW:
        return _window_snippet(line, normalized_start, normalized_length)
    source = _source_index(line, normalized_start)
    window_start = max(0, source - _SNIPPET_WINDOW)
    while window_start > 0 and unicodedata.combining(line[window_start]):
        window_start -= 1
    window_end = min(len(line), source + _SNIPPET_WINDOW + max(1, normalized_length))
    while window_end < len(line) and unicodedata.combining(line[window_end]):
        window_end += 1
    offset = len(normalize_search_text(line[:window_start]))
    return _window_snippet(line[window_start:window_end], max(0, normalized_start - offset), normalized_length)


def _source_index(line: str, normalized_index: int) -> int:
    """Largest source prefix whose normalized length does not exceed the index."""
    low, high = 0, len(line)
    while low < high:
        middle = (low + high + 1) // 2
        if len(normalize_search_text(line[:middle])) <= normalized_index:
            low = middle
        else:
            high = middle - 1
    return low


def _window_snippet(line: str, normalized_start: int, normalized_length: int) -> str:
    normalized_parts: list[str] = []
    source_spans: list[tuple[int, int]] = []
    segment_start = 0
    index = 0
    while index < len(line):
        end = index + 1
        while end < len(line) and unicodedata.combining(line[end]):
            end += 1
        folded = normalize_search_text(line[index:end])
        normalized_parts.append(folded)
        source_spans.extend([(index, end)] * len(folded))
        index = end
    if not source_spans:
        return ""
    start = source_spans[min(normalized_start, len(source_spans) - 1)][0]
    match_end_index = min(normalized_start + max(1, normalized_length) - 1, len(source_spans) - 1)
    match_end = source_spans[match_end_index][1]
    left = max(0, start - 80)
    right = min(len(line), left + 200)
    if right - left == 200 and match_end > right:
        left = max(0, min(start - 80, len(line) - 200))
        right = min(len(line), left + 200)
    return line[left:right]


def _item(
    path: str,
    name: str,
    kind: str,
    version: tuple[int, ...],
    parent_version: str,
    line: int | None = None,
    snippet: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "rootId": BASE_ID,
        "path": path,
        "name": name,
        "type": kind,
        "version": list(version),
        "parentVersion": parent_version,
    }
    if line is not None:
        item["line"] = line
        item["snippet"] = snippet or ""
    return item


def _public_items(items: list[object]) -> list[dict[str, object]]:
    """Strip private versions; expose the transport pair and the absolute address."""
    public = []
    for item in items:
        if not isinstance(item, dict):
            continue
        visible = {key: value for key, value in item.items() if key not in {"version", "parentVersion"}}
        visible["address"] = "/" + str(item["path"])
        public.append(visible)
    return public


def _contains(value: str, query: str) -> bool:
    return query in normalize_search_text(value)


def _result_key(item: dict[str, object]) -> bytes:
    # Absolute canonical path order, ascending by UTF-8 bytes (HF-API-004).
    return ("/" + str(item["path"])).encode("utf-8")


def _version(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
        int(info.st_nlink),
        int(stat.S_IFMT(info.st_mode)),
    )


def _validate_page(record: dict[str, object], catalog: RootCatalog, offset: int, limit: int) -> None:
    """Revalidate the result objects and parent directories a page serves."""
    items = record.get("items")
    if not isinstance(items, list):
        raise StateError("search cursor snapshot is invalid")
    try:
        for item in items[offset : offset + limit]:
            if not isinstance(item, dict):
                raise StateError("search cursor item is invalid")
            path = item.get("path")
            version = item.get("version")
            parent_version = item.get("parentVersion")
            if not isinstance(path, str) or not isinstance(version, list) or len(version) != 7 or not isinstance(parent_version, str):
                raise StateError("search cursor item is invalid")
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if directory_version(catalog, BASE_ID, parent) != parent_version:
                raise SearchError(409, "conflict")
            if list(search_object_version(catalog, BASE_ID, path)) != version:
                raise SearchError(409, "conflict")
    except AddressRejected as exc:
        raise SearchError(409, "conflict") from exc


def _encode_record(record: dict[str, object]) -> bytes:
    try:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (UnicodeError, TypeError, ValueError) as exc:
        raise StateError("search snapshot cannot be encoded") from exc


def _counts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        return []
    return [
        {"category": category, "count": count}
        for category, count in sorted(value.items())
        if isinstance(category, str) and isinstance(count, int) and count > 0
    ]


def _add_count(counts: object, category: str, count: int = 1) -> None:
    if isinstance(counts, dict):
        counts[category] = int(counts.get(category, 0)) + count


def _timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _b64(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    import base64

    if re.fullmatch(r"[A-Za-z0-9_-]*", value) is None:
        raise ValueError("invalid base64url")
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if _b64(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _unicode_scalar(value: str) -> bool:
    return not any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def extract_markdown_tags(text: str) -> set[str]:
    """Return unique normalized tags outside code, link destinations, and URLs."""
    found: set[str] = set()
    fence_char: str | None = None
    fence_length = 0
    normalized_text = unicodedata.normalize("NFC", text)
    masked_lines: list[str] = []
    for line in normalized_text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        stripped = line.lstrip(" ")
        if fence_char is not None:
            stripped_body = body.lstrip(" ")
            close = len(stripped_body) - len(stripped_body.lstrip(fence_char))
            if close >= fence_length and stripped_body[close:].strip() == "":
                fence_char = None
                fence_length = 0
            masked_lines.append(" " * len(body) + newline)
            continue
        stripped_body = body.lstrip(" ")
        if stripped_body.startswith(("```", "~~~")):
            marker = stripped_body[0]
            length = len(stripped_body) - len(stripped_body.lstrip(marker))
            if length >= 3:
                fence_char, fence_length = marker, length
                masked_lines.append(" " * len(body) + newline)
                continue
        masked_lines.append(line)
    masked = _mask_markdown_nontext("".join(masked_lines))
    for match in re.finditer(r"(?<![\w#\\])#", masked, re.UNICODE):
        start = match.start()
        if start + 1 >= len(masked) or not _is_letter(masked[start + 1]):
            continue
        end = start + 2
        while end < len(masked) and (
            _is_letter(masked[end]) or masked[end].isdecimal() or masked[end] == "-"
        ):
            end += 1
        raw = masked[start + 1 : end]
        if end < len(masked) and (
            masked[end] == "_"
            or masked[end].isalnum()
            or unicodedata.category(masked[end]).startswith("M")
        ):
            continue
        if _HEX_COLOR.fullmatch("#" + raw):
            continue
        if not _valid_tag_body(raw):
            continue
        found.add(unicodedata.normalize("NFC", raw))
    return {normalize_search_text(item) for item in found}


def _mask_markdown_nontext(text: str) -> str:
    chars = list(text)
    blocked = [False] * len(chars)
    for match in _URL.finditer(text):
        for index in range(match.start(), match.end()):
            blocked[index] = True
    index = 0
    while index < len(text):
        if text[index] == "`":
            end = index
            while end < len(text) and text[end] == "`":
                end += 1
            delimiter = text[index:end]
            close = text.find(delimiter, end)
            if close >= 0:
                for position in range(index, close + len(delimiter)):
                    blocked[position] = True
                index = close + len(delimiter)
                continue
            index = end
            continue
        if text[index] == "]" and index + 1 < len(text) and text[index + 1] == "(":
            depth = 1
            end = index + 2
            while end < len(text) and depth:
                if text[end] == "(":
                    depth += 1
                elif text[end] == ")":
                    depth -= 1
                end += 1
            if depth == 0:
                for position in range(index + 2, end - 1):
                    blocked[position] = True
            index = end
            continue
        index += 1
    for index, is_blocked in enumerate(blocked):
        if is_blocked:
            chars[index] = " "
    return "".join(chars)


def _valid_tag_body(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    if not _is_letter(value[0]) or not (_is_letter(value[-1]) or value[-1].isdecimal()):
        return False
    return all(_is_letter(character) or character.isdecimal() or character == "-" for character in value)


def _is_letter(value: str) -> bool:
    return unicodedata.category(value).startswith("L")


def derive_tag_index(
    catalog: RootCatalog,
    *,
    requested_tag: str | None = None,
    ignored: frozenset[str] = frozenset(),
    folders: tuple[str, ...] = (),
) -> dict[str, object]:
    """Derive the tag index of the monitored folders without modifying Markdown files.

    ``folders`` are the monitored folders relative to ``/``; with none, the
    index is empty (HF-META-002). Tags the instance chose to ignore are left
    out of the list, the counts, and the per-tag lookup; the notes themselves
    are never touched.
    """
    if requested_tag is not None:
        if not _unicode_scalar(requested_tag) or "\x00" in requested_tag or len(requested_tag) > 80:
            raise SearchError(400, "invalid_request")
        target = normalize_search_text(requested_tag.strip().removeprefix("#"))
        if not target:
            raise SearchError(400, "invalid_request")
    else:
        target = None
    from hopper_files.note_index import observe_tag_notes

    try:
        view = observe_tag_notes(catalog, folders)
    except AddressRejected as exc:
        raise SearchError(409, "conflict") from exc
    scan = view.scan
    omissions: dict[str, int] = {}
    indexed: dict[str, list[dict[str, str]]] = {}
    notes: list[dict[str, object]] = []
    for child_path, record in sorted(view.notes.items(), key=lambda pair: pair[0].encode("utf-8")):
        if record.tags is None:
            _add_count(omissions, record.tag_error or "invalid_markdown")
            continue
        tags = list(record.tags)
        if len(tags) > 64:
            _add_count(omissions, "tag_count_limit")
            tags = tags[:64]
        tags = [tag for tag in tags if tag not in ignored]
        if not tags:
            continue
        notes.append({"rootId": BASE_ID, "path": child_path, "tags": tags})
        for tag in tags:
            indexed.setdefault(tag, []).append({"rootId": BASE_ID, "path": child_path})
    for category, count in scan.omissions().items():
        _add_count(omissions, category, count)
    exclusions = scan.exclusions()
    complete = not omissions
    result: dict[str, object] = {
        "rootGeneration": catalog.generation,
        "complete": complete,
        "omissions": _counts(omissions),
        "exclusions": _counts(exclusions),
        "tags": [
            {"tag": tag, "count": len(paths)}
            for tag, paths in sorted(indexed.items(), key=lambda pair: pair[0].encode("utf-8"))
            if target is None or tag == target
        ],
    }
    if target is not None:
        selected = [note for note in notes if target in note["tags"]]
        selected.sort(key=lambda item: ("/" + item["path"]).encode("utf-8"))
        result["items"] = selected[:SEARCH_RESULT_LIMIT]
        if len(selected) > SEARCH_RESULT_LIMIT:
            result["complete"] = False
            result["omissions"] = _counts({**omissions, "result_limit": len(selected) - SEARCH_RESULT_LIMIT})
    return result
