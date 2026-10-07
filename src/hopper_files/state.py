"""Per-instance private state, durable UI metadata, and publication helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

import fcntl

RESERVED_DIRECTORIES = (
    "cache",
    "indexes",
    "journals",
    "nonces",
    "operations",
    "sessions",
    "trash",
    "ui-state",
    "attachments",
)

LABELS = {
    "vermelho": {"name": "Vermelho", "color": "#ff3b30"},
    "laranja": {"name": "Laranja", "color": "#ff9500"},
    "amarelo": {"name": "Amarelo", "color": "#ffcc00"},
    "verde": {"name": "Verde", "color": "#34c759"},
    "azul": {"name": "Azul", "color": "#0a84ff"},
    "roxo": {"name": "Roxo", "color": "#af52de"},
    "cinza": {"name": "Cinza", "color": "#8e8e93"},
}
UI_STATE_NAME = "ui-state.json"
MAX_IGNORED_TAGS = 256
SIDEBAR_WIDTH_RANGE = (200, 420)
# Optional preferences: absent unless set, because a release that does not know them rejects them.
OPTIONAL_PREFERENCES = frozenset({"ignoredTags", "sidebarWidth"})
# The monitored folders of the tag index (HF-META-002) live in their own document,
# outside ui-state.json, so a return to a release that does not know them leaves
# them untouched instead of rejecting the whole UI state.
TAG_FOLDERS_NAME = "tag-folders.json"
TAG_FOLDERS_VERSION = 1
MAX_TAG_FOLDERS = 64
UI_STATE_VERSION = 2
# Version 1 identified records by (rootId, path). Only the HF-NAV-012
# migration reads it; this release never serves or initializes over it.
LEGACY_UI_STATE_VERSION = 1
ORPHAN_KINDS = frozenset({"item", "tab"})
UI_STATE_LOCK_NAME = "ui-state.lock"
UI_STATE_JOURNAL_DIRECTORY = "ui-state"
UI_STATE_INITIALIZATION_NAME = "ui-state-initialized.json"
UI_STATE_INITIALIZATION_INTENT_NAME = "ui-state-initializing.json"
_ROOT_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_UI_STATE_JOURNAL_NAME = re.compile(r"ui-state-[0-9a-f]{32}\.json\Z")
_UI_STATE_JOURNAL_TEMPORARY_NAME = re.compile(r"\.ui-state-[0-9a-f]{32}\.json\.[0-9a-f]{16}\.tmp\Z")
_UI_STATE_DOCUMENT_TEMPORARY_NAME = re.compile(r"\.ui-state\.json\.[0-9a-f]{16}\.tmp\Z")
_UI_STATE_INITIALIZATION_TEMPORARY_NAMES = (
    re.compile(r"\.ui-state-initialized\.json\.[0-9a-f]{16}\.tmp\Z"),
    re.compile(r"\.ui-state-initializing\.json\.[0-9a-f]{16}\.tmp\Z"),
)


class StateError(ValueError):
    """A state path or publication failed closed."""


class KdfBusy(RuntimeError):
    """Another process already holds this instance's password KDF."""


class UiStateConflict(StateError):
    """A client or internal writer used an obsolete UI-state revision."""

    def __init__(self, state_revision: int) -> None:
        super().__init__("ui state revision conflicts")
        self.state_revision = state_revision


class UiStateValidationError(StateError):
    """A UI-state document does not implement schema version 2."""


class LegacyStateError(StateError):
    """Per-instance state is still version 1 and needs the HF-NAV-012 migration."""


class TagFolderLimit(StateError):
    """Marking one more folder would exceed the monitored-folder limit."""


class UiStatePublicationIndeterminate(StateError):
    """The replacement may be visible, but its directory sync failed."""

    def __init__(self, state_revision: int) -> None:
        super().__init__("ui state publication is indeterminate")
        self.state_revision = state_revision


def require_within(path: Path, root: Path) -> None:
    """Reject a path whose parent resolves outside the instance state tree."""
    root_real = root.resolve()
    parent = path.parent.resolve()
    if parent != root_real and root_real not in parent.parents:
        raise StateError("path escapes instance state")


def atomic_write(path: Path, data: bytes) -> None:
    """Publish bytes by replacing a 0600 temporary file in the same directory.

    os.replace keeps the temporary inode's mode, so the temporary file is
    created as 0600 rather than tightened after publication.
    """
    parent = path.parent
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(file_fd, 0o600)
            if stat.S_IMODE(os.fstat(file_fd).st_mode) != 0o600:
                raise StateError("state temporary permissions are invalid")
            view = memoryview(data)
            while view:
                written = os.write(file_fd, view)
                view = view[written:]
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        os.rename(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)


def ui_state_path(state_directory: Path) -> Path:
    """Return the only UI-state document for one bound instance."""
    return _state_directory(state_directory) / "ui-state" / UI_STATE_NAME


def default_ui_state() -> dict[str, object]:
    """Create the revision-zero document without sharing mutable structures."""
    return {
        "version": UI_STATE_VERSION,
        "stateRevision": 0,
        "labels": copy.deepcopy(LABELS),
        "items": [],
        "tabs": [],
        "orphans": [],
        "preferences": {
            "theme": "system",
            "ordering": "name",
            "density": "comfortable",
        },
    }


@contextmanager
def ui_state_lock(state_directory: Path) -> Iterator[None]:
    """Serialize every UI-state mutation across threads and processes.

    The lock is deliberately separate from the KDF and limiter locks.  Future
    file, trash, and recovery code must use this lock through
    :func:`mutate_ui_state` instead of editing ``ui-state.json`` directly.
    """
    directory = _state_directory(state_directory)
    lock_path = directory / UI_STATE_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        file_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise StateError("ui state lock is unavailable") from exc
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise StateError("ui state lock is invalid")
        fcntl.flock(file_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(file_fd, fcntl.LOCK_UN)
        finally:
            os.close(file_fd)


def initialize_ui_state(state_directory: Path) -> None:
    """Create revision zero once and retain a durable initialization witness.

    The witness is distinct from the mutable document.  If a state document is
    later lost, its presence makes that loss explicit instead of turning it
    into an unauthorized revision-zero reset.
    """
    directory = _state_directory(state_directory)
    ui_directory = directory / "ui-state"
    journals = directory / "journals" / UI_STATE_JOURNAL_DIRECTORY
    _private_directory(ui_directory)
    _private_directory(journals)
    with ui_state_lock(directory):
        _ensure_ui_state_initialized_locked(directory)
        _recover_ui_state_locked(directory)


def load_ui_state(state_directory: Path) -> dict[str, object]:
    """Read the complete current document after idempotent recovery."""
    directory = _state_directory(state_directory)
    with ui_state_lock(directory):
        _require_ui_state_initialized_locked(directory)
        _recover_ui_state_locked(directory)
        return _read_ui_state(ui_state_path(directory))


def replace_ui_state(
    state_directory: Path,
    *,
    base_revision: int,
    document: object,
) -> dict[str, object]:
    """CAS-replace one complete UI state and return its durable new revision.

    ``base_revision`` is checked after taking the process-wide lock.  The
    caller cannot supply ``stateRevision``: only this function generates it.
    """
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        raise UiStateValidationError("base revision is invalid")
    directory = _state_directory(state_directory)
    with ui_state_lock(directory):
        _require_ui_state_initialized_locked(directory)
        _recover_ui_state_locked(directory)
        current = _read_ui_state(ui_state_path(directory))
        current_revision = _state_revision(current)
        if base_revision != current_revision:
            raise UiStateConflict(current_revision)
        if isinstance(document, dict) and "orphans" not in document:
            # Orphans exist only for reporting and rollback (HF-META-004).
            # A client never edits them; an omitted list keeps the current one.
            document = {**document, "orphans": copy.deepcopy(current["orphans"])}
        candidate = _validate_ui_state(
            document,
            require_state_revision=False,
        )
        if candidate["orphans"] != current["orphans"]:
            raise UiStateValidationError("ui state orphans are server-owned")
        if _state_content(candidate) == _state_content(current):
            return current
        candidate["stateRevision"] = current_revision + 1
        return _publish_ui_state_locked(directory, current, candidate)


def mutate_ui_state(
    state_directory: Path,
    mutate: object,
) -> dict[str, object]:
    """Commit one internal metadata mutation under the same durable CAS lock.

    Later move, delete, restore, and recovery consumers receive a deep copy of
    the current document.  They may change metadata but cannot choose the next
    revision.  Returning an unchanged document is not a state change.
    """
    if not callable(mutate):
        raise TypeError("ui state mutation must be callable")
    directory = _state_directory(state_directory)
    with ui_state_lock(directory):
        _require_ui_state_initialized_locked(directory)
        _recover_ui_state_locked(directory)
        current = _read_ui_state(ui_state_path(directory))
        draft = copy.deepcopy(current)
        result = mutate(draft)
        if result is not None:
            draft = result
        candidate = _validate_ui_state(
            draft,
            require_state_revision=True,
        )
        if _state_revision(candidate) != _state_revision(current):
            raise UiStateValidationError("internal mutation changed state revision")
        if _state_content(candidate) == _state_content(current):
            return current
        candidate["stateRevision"] = _state_revision(current) + 1
        return _publish_ui_state_locked(directory, current, candidate)


def load_ui_state_locked(state_directory: Path) -> dict[str, object]:
    """Read UI state. The caller must already hold :func:`ui_state_lock`."""
    directory = _state_directory(state_directory)
    _require_ui_state_initialized_locked(directory)
    _recover_ui_state_locked(directory)
    return _read_ui_state(ui_state_path(directory))


def commit_ui_state_locked(
    state_directory: Path,
    current: dict[str, object],
    draft: dict[str, object],
) -> dict[str, object]:
    """Publish one internal draft. The caller must already hold :func:`ui_state_lock`.

    An unchanged document does not increment ``stateRevision``. The draft cannot
    choose the next revision.
    """
    directory = _state_directory(state_directory)
    candidate = _validate_ui_state(draft, require_state_revision=True)
    if _state_revision(candidate) != _state_revision(current):
        raise UiStateValidationError("internal mutation changed state revision")
    if _state_content(candidate) == _state_content(current):
        return current
    candidate["stateRevision"] = _state_revision(current) + 1
    return _publish_ui_state_locked(directory, current, candidate)


def recover_ui_state(state_directory: Path) -> None:
    """Resolve unfinished UI-state publications without replaying a write.

    An intent whose target digest is visible is terminally committed.  One whose
    original digest is still visible is terminally not published.  Any other
    state is deliberately left in place and fails closed for manual diagnosis.
    Repeating this function therefore cannot add, erase, or replay metadata.
    """
    directory = _state_directory(state_directory)
    with ui_state_lock(directory):
        _recover_ui_state_initialization_locked(directory)
        _recover_ui_state_locked(directory)


def _ensure_ui_state_initialized_locked(directory: Path) -> None:
    """Finish a known first initialization or start exactly one new one."""
    if _recover_ui_state_initialization_locked(directory):
        return
    initial_bytes = _initial_ui_state_bytes()
    intent = directory / UI_STATE_INITIALIZATION_INTENT_NAME
    _write_initialization_intent(intent, _digest(initial_bytes))
    try:
        atomic_write(ui_state_path(directory), initial_bytes)
        _write_initialization_marker(directory, _digest(initial_bytes))
    except Exception:
        # The durable intent is intentionally retained.  On the next startup it
        # either proves that no document was published, or it proves the exact
        # revision-zero document that may be completed into a marker.
        raise
    _remove_regular_file(intent)


def _require_ui_state_initialized_locked(directory: Path) -> None:
    """Require a completed initialization without creating replacement state."""
    if not _recover_ui_state_initialization_locked(directory):
        raise StateError("ui state has not been initialized")


def _recover_ui_state_initialization_locked(directory: Path) -> bool:
    """Resolve only a recorded first initialization, without resetting state."""
    path = ui_state_path(directory)
    _refuse_legacy_document(path)
    marker = directory / UI_STATE_INITIALIZATION_NAME
    intent = directory / UI_STATE_INITIALIZATION_INTENT_NAME
    marker_exists = _entry_exists(marker)
    intent_exists = _entry_exists(intent)
    document_exists = _entry_exists(path)
    initial_digest = _digest(_initial_ui_state_bytes())

    if marker_exists:
        _read_initialization_marker(marker, initial_digest)
        if intent_exists:
            _read_initialization_intent(intent, initial_digest)
            _remove_regular_file(intent)
        _discard_initialization_temporaries(directory)
        if not document_exists:
            raise StateError("ui state document is missing after initialization")
        _read_ui_state(path)
        return True

    if document_exists:
        if not intent_exists:
            raise StateError("ui state initialization witness is missing")
        _read_initialization_intent(intent, initial_digest)
        document_bytes = _read_regular_bytes(path)
        if _digest(document_bytes) != initial_digest:
            raise StateError("ui state initialization is indeterminate")
        _read_ui_state(path)
        _discard_initialization_temporaries(directory)
        _write_initialization_marker(directory, initial_digest)
        _remove_regular_file(intent)
        return True

    if intent_exists:
        _read_initialization_intent(intent, initial_digest)
        _discard_initialization_document_temporaries(path.parent)
        _discard_initialization_temporaries(directory)
        _remove_regular_file(intent)
    else:
        # A process can die while atomic_write is still creating the first
        # initialization intent. That temporary was never an intent, so it may
        # be discarded before this call starts a new first initialization.
        _discard_initialization_temporaries(directory)
    return False


def _initial_ui_state_bytes() -> bytes:
    return _encode_ui_state(default_ui_state())


def initial_ui_state_digest(version: int) -> str:
    """Digest of the revision-zero document of one schema version.

    The initialization witness records it. The HF-NAV-012 migration rewrites the
    witness when it converts the document between versions.
    """
    document = default_ui_state()
    if version == LEGACY_UI_STATE_VERSION:
        document["version"] = LEGACY_UI_STATE_VERSION
        document.pop("orphans")
    elif version != UI_STATE_VERSION:
        raise UiStateValidationError("ui state version is invalid")
    return _digest(_encode_ui_state(document))


def write_initialization_witness(state_directory: Path, version: int) -> None:
    """Rewrite the initialization witness for a converted document (HF-NAV-012)."""
    directory = _state_directory(state_directory)
    _write_initialization_marker(directory, initial_ui_state_digest(version))


def encode_ui_state(document: dict[str, object]) -> bytes:
    return _encode_ui_state(document)


def _refuse_legacy_document(path: Path) -> None:
    """Fail closed with a clear report when the document is still version 1."""
    if not _entry_exists(path) or path.is_symlink():
        return
    try:
        payload = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (StateError, UnicodeError, json.JSONDecodeError):
        return
    if (
        isinstance(payload, dict)
        and payload.get("version") == LEGACY_UI_STATE_VERSION
        and not isinstance(payload.get("version"), bool)
    ):
        raise LegacyStateError(
            "ui state is version 1; run the HF-NAV-012 migration with the administrative installer"
        )


def _write_initialization_intent(path: Path, target_digest: str) -> None:
    _write_initialization_record(
        path,
        {
            "version": 1,
            "kind": "ui-state-initialization",
            "targetDigest": target_digest,
        },
    )


def _write_initialization_marker(directory: Path, initial_digest: str) -> None:
    _write_initialization_record(
        directory / UI_STATE_INITIALIZATION_NAME,
        {
            "version": 1,
            "kind": "ui-state-initialized",
            "initialDigest": initial_digest,
        },
    )


def _write_initialization_record(path: Path, payload: dict[str, object]) -> None:
    atomic_write(path, json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii"))


def _read_initialization_marker(path: Path, initial_digest: str) -> None:
    _read_initialization_record(
        path,
        expected={"version", "kind", "initialDigest"},
        kind="ui-state-initialized",
        digest_field="initialDigest",
        digest=initial_digest,
    )


def _read_initialization_intent(path: Path, initial_digest: str) -> None:
    _read_initialization_record(
        path,
        expected={"version", "kind", "targetDigest"},
        kind="ui-state-initialization",
        digest_field="targetDigest",
        digest=initial_digest,
    )


def _read_initialization_record(
    path: Path,
    *,
    expected: set[str],
    kind: str,
    digest_field: str,
    digest: str,
) -> None:
    try:
        payload = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("ui state initialization record is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("version") != 1
        or isinstance(payload.get("version"), bool)
        or payload.get("kind") != kind
        or payload.get(digest_field) != digest
    ):
        raise StateError("ui state initialization record is invalid")


def _discard_initialization_document_temporaries(directory: Path) -> None:
    _discard_ui_state_document_temporaries(directory, initialization=True)


def _discard_ui_state_document_temporaries(directory: Path, *, initialization: bool = False) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if _UI_STATE_DOCUMENT_TEMPORARY_NAME.fullmatch(entry.name):
            error = "ui state initialization temporary is invalid" if initialization else "ui state temporary is invalid"
            _remove_prepublication_temporary(entry, error)
            continue
        if entry.name != UI_STATE_NAME:
            raise StateError("ui state initialization is indeterminate" if initialization else "ui state directory is invalid")


def _discard_initialization_temporaries(directory: Path) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if any(pattern.fullmatch(entry.name) for pattern in _UI_STATE_INITIALIZATION_TEMPORARY_NAMES):
            _remove_prepublication_temporary(entry, "ui state initialization temporary is invalid")
            continue
        if entry.name.startswith(f".{UI_STATE_INITIALIZATION_NAME}.") or entry.name.startswith(
            f".{UI_STATE_INITIALIZATION_INTENT_NAME}."
        ):
            raise StateError("ui state initialization temporary is invalid")


def _publish_ui_state_locked(
    directory: Path,
    current: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    path = ui_state_path(directory)
    current_bytes = _encode_ui_state(current)
    candidate_bytes = _encode_ui_state(candidate)
    journal = _write_ui_state_journal(
        directory,
        previous_revision=_state_revision(current),
        previous_digest=_digest(current_bytes),
        next_revision=_state_revision(candidate),
        target_digest=_digest(candidate_bytes),
    )
    try:
        atomic_write(path, candidate_bytes)
    except OSError as exc:
        try:
            visible_digest = _digest_regular_file(path)
        except (OSError, StateError):
            visible_digest = None
        if visible_digest != _digest(current_bytes):
            raise UiStatePublicationIndeterminate(_state_revision(candidate)) from exc
        raise
    try:
        _remove_journal(journal)
    except OSError:
        # The state document was already file- and directory-synced.  Retaining
        # its intent is safe; a later recovery removes only that terminal record.
        pass
    return candidate


def _recover_ui_state_locked(directory: Path) -> None:
    journal_directory = directory / "journals" / UI_STATE_JOURNAL_DIRECTORY
    if not journal_directory.is_dir() or journal_directory.is_symlink():
        raise StateError("ui state journal directory is unavailable")
    _discard_ui_state_document_temporaries(ui_state_path(directory).parent)
    for journal in sorted(journal_directory.iterdir(), key=lambda item: item.name):
        if _UI_STATE_JOURNAL_TEMPORARY_NAME.fullmatch(journal.name):
            _remove_prepublication_temporary(journal, "ui state journal temporary is invalid")
            continue
        if (
            not _UI_STATE_JOURNAL_NAME.fullmatch(journal.name)
            or journal.is_symlink()
            or not journal.is_file()
        ):
            raise StateError("ui state journal is invalid")
        payload = _read_journal(journal)
        current = _read_ui_state(ui_state_path(directory))
        current_bytes = _encode_ui_state(current)
        current_revision = _state_revision(current)
        if current_revision == payload["nextRevision"] and _digest(current_bytes) == payload["targetDigest"]:
            _remove_journal(journal)
            continue
        if current_revision == payload["previousRevision"] and _digest(current_bytes) == payload["previousDigest"]:
            _remove_journal(journal)
            continue
        raise StateError("ui state recovery is indeterminate")


def _write_ui_state_journal(
    directory: Path,
    *,
    previous_revision: int,
    previous_digest: str,
    next_revision: int,
    target_digest: str,
) -> Path:
    journal_directory = directory / "journals" / UI_STATE_JOURNAL_DIRECTORY
    path = journal_directory / f"ui-state-{secrets.token_hex(16)}.json"
    payload = {
        "version": 1,
        "kind": "ui-state-publication",
        "previousRevision": previous_revision,
        "previousDigest": previous_digest,
        "nextRevision": next_revision,
        "targetDigest": target_digest,
    }
    atomic_write(
        path,
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii"),
    )
    return path


def _read_journal(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("ui state journal is unreadable") from exc
    expected = {
        "version",
        "kind",
        "previousRevision",
        "previousDigest",
        "nextRevision",
        "targetDigest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise StateError("ui state journal is invalid")
    if payload["version"] != 1 or payload["kind"] != "ui-state-publication":
        raise StateError("ui state journal is invalid")
    previous = payload["previousRevision"]
    following = payload["nextRevision"]
    digests = (payload["previousDigest"], payload["targetDigest"])
    if (
        isinstance(previous, bool)
        or isinstance(following, bool)
        or not isinstance(previous, int)
        or not isinstance(following, int)
        or following != previous + 1
        or previous < 0
        or any(not isinstance(value, str) or len(value) != 64 for value in digests)
        or any(any(char not in "0123456789abcdef" for char in value) for value in digests)
    ):
        raise StateError("ui state journal is invalid")
    return payload


def _remove_journal(path: Path) -> None:
    _remove_regular_file(path)


def _remove_prepublication_temporary(path: Path, error: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise StateError(error)
    try:
        _remove_regular_file(path)
    except OSError as exc:
        raise StateError(error) from exc


def _remove_regular_file(path: Path) -> None:
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        details = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode):
            raise StateError("state file is invalid")
        os.unlink(path.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_ui_state(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise StateError("ui state is unavailable")
    try:
        payload = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("ui state is unreadable") from exc
    if (
        isinstance(payload, dict)
        and payload.get("version") == LEGACY_UI_STATE_VERSION
        and not isinstance(payload.get("version"), bool)
    ):
        raise LegacyStateError(
            "ui state is version 1; run the HF-NAV-012 migration with the administrative installer"
        )
    return _validate_ui_state(payload, require_state_revision=True)


def _validate_ui_state(
    document: object,
    *,
    require_state_revision: bool,
) -> dict[str, object]:
    fields = {"version", "labels", "items", "tabs", "orphans", "preferences"}
    if require_state_revision:
        fields.add("stateRevision")
    if not isinstance(document, dict) or set(document) != fields:
        raise UiStateValidationError("ui state fields are invalid")
    if document["version"] != UI_STATE_VERSION or isinstance(document["version"], bool):
        raise UiStateValidationError("ui state version is invalid")
    if require_state_revision:
        _state_revision(document)
    _validate_labels(document["labels"])
    items = document["items"]
    if not isinstance(items, list):
        raise UiStateValidationError("ui state items are invalid")
    identities: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "labelIds", "favorite", "emoji", "inode", "device"}:
            raise UiStateValidationError("ui state item is invalid")
        path = _absolute_path(item["path"])
        if path in identities:
            raise UiStateValidationError("ui state item is duplicated")
        identities.add(path)
        _item_marks(item)
    tabs = document["tabs"]
    if not isinstance(tabs, list):
        raise UiStateValidationError("ui state tabs are invalid")
    for tab in tabs:
        if not isinstance(tab, dict) or set(tab) != {"path", "mode"}:
            raise UiStateValidationError("ui state tab is invalid")
        _absolute_path(tab["path"])
        if not _text(tab["mode"], maximum=32):
            raise UiStateValidationError("ui state tab mode is invalid")
    orphans = document["orphans"]
    if not isinstance(orphans, list):
        raise UiStateValidationError("ui state orphans are invalid")
    for orphan in orphans:
        validate_orphan(orphan)
    _validate_preferences(document["preferences"])
    return copy.deepcopy(document)


def validate_orphan(orphan: object) -> None:
    """Validate one version 1 record kept by the HF-NAV-012 migration."""
    if not isinstance(orphan, dict) or set(orphan) != {"kind", "record"} or orphan["kind"] not in ORPHAN_KINDS:
        raise UiStateValidationError("ui state orphan is invalid")
    record = orphan["record"]
    if orphan["kind"] == "item":
        if not isinstance(record, dict) or set(record) != {"rootId", "path", "labelIds", "favorite", "emoji", "inode", "device"}:
            raise UiStateValidationError("ui state orphan is invalid")
        _item_marks(record)
    else:
        if not isinstance(record, dict) or set(record) != {"rootId", "path", "mode"}:
            raise UiStateValidationError("ui state orphan is invalid")
        if not _text(record["mode"], maximum=32):
            raise UiStateValidationError("ui state orphan is invalid")
    _root_id(record["rootId"], None)
    _canonical_path(record["path"])


def validate_legacy_ui_state(document: object) -> dict[str, object]:
    """Validate a version 1 document for the HF-NAV-012 migration only."""
    fields = {"version", "stateRevision", "labels", "items", "tabs", "preferences"}
    if not isinstance(document, dict) or set(document) != fields:
        raise UiStateValidationError("version 1 ui state fields are invalid")
    if document["version"] != LEGACY_UI_STATE_VERSION or isinstance(document["version"], bool):
        raise UiStateValidationError("version 1 ui state version is invalid")
    _state_revision(document)
    _validate_labels(document["labels"])
    items = document["items"]
    tabs = document["tabs"]
    if not isinstance(items, list) or not isinstance(tabs, list):
        raise UiStateValidationError("version 1 ui state records are invalid")
    seen: set[tuple[str, str]] = set()
    for item in items:
        validate_orphan({"kind": "item", "record": item})
        identity = (item["rootId"], item["path"])
        if identity in seen:
            raise UiStateValidationError("version 1 ui state item is duplicated")
        seen.add(identity)
    for tab in tabs:
        validate_orphan({"kind": "tab", "record": tab})
    _validate_preferences(document["preferences"])
    return copy.deepcopy(document)


def _validate_labels(labels: object) -> None:
    if not isinstance(labels, dict) or set(labels) != set(LABELS):
        raise UiStateValidationError("ui state labels are invalid")
    for label_id, standard in LABELS.items():
        label = labels[label_id]
        if not isinstance(label, dict) or set(label) != {"name", "color"}:
            raise UiStateValidationError("ui state label is invalid")
        if not _text(label["name"], maximum=80) or label["color"] != standard["color"]:
            raise UiStateValidationError("ui state label is invalid")


def _item_marks(item: dict[str, object]) -> None:
    label_ids = item["labelIds"]
    if not isinstance(label_ids, list) or any(not isinstance(label_id, str) or label_id not in LABELS for label_id in label_ids):
        raise UiStateValidationError("ui state item labels are invalid")
    if len(label_ids) != len(set(label_ids)):
        raise UiStateValidationError("ui state item labels are duplicated")
    if not isinstance(item["favorite"], bool):
        raise UiStateValidationError("ui state favorite is invalid")
    emoji = item["emoji"]
    if emoji is not None and not _text(emoji, maximum=16):
        raise UiStateValidationError("ui state emoji is invalid")
    _identity_hint(item["inode"])
    _identity_hint(item["device"])


def _validate_preferences(preferences: object) -> None:
    preference_keys = {"theme", "ordering", "density"}
    if not isinstance(preferences, dict) or not preference_keys <= set(preferences) <= preference_keys | OPTIONAL_PREFERENCES:
        raise UiStateValidationError("ui state preferences are invalid")
    if any(not _text(preferences[key], maximum=32) for key in ("theme", "ordering", "density")):
        raise UiStateValidationError("ui state preferences are invalid")
    if "ignoredTags" in preferences:
        _ignored_tags(preferences["ignoredTags"])
    if "sidebarWidth" in preferences:
        width = preferences["sidebarWidth"]
        minimum, maximum = SIDEBAR_WIDTH_RANGE
        if isinstance(width, bool) or not isinstance(width, int) or not minimum <= width <= maximum:
            raise UiStateValidationError("ui state sidebar width is invalid")


def ignored_tags(document: dict[str, object]) -> frozenset[str]:
    """Return the normalized tags this instance hides from the derived index."""
    preferences = document.get("preferences")
    if not isinstance(preferences, dict):
        return frozenset()
    return frozenset(preferences.get("ignoredTags", ()))


def load_tag_folders(state_directory: Path) -> list[str]:
    """Return the monitored folders as absolute canonical addresses, sorted.

    A missing document means no folder is monitored. An unreadable or invalid
    one fails closed instead of reading as an empty list.
    """
    directory = _state_directory(state_directory)
    with ui_state_lock(directory):
        return _read_tag_folders(directory)


def set_tag_folder(state_directory: Path, address: str, monitored: bool) -> list[str]:
    """Add or remove one monitored folder and return the resulting list.

    Adding a folder already present or removing an absent one changes nothing,
    so a repeated request has the same outcome and needs no revision.
    """
    _absolute_path(address)
    directory = _state_directory(state_directory)
    with ui_state_lock(directory):
        folders = _read_tag_folders(directory)
        if monitored == (address in folders):
            return folders
        if monitored:
            if len(folders) >= MAX_TAG_FOLDERS:
                raise TagFolderLimit("too many monitored folders")
            folders = sorted([*folders, address], key=lambda value: value.encode("utf-8"))
        else:
            folders = [value for value in folders if value != address]
        payload = {"version": TAG_FOLDERS_VERSION, "folders": folders}
        atomic_write(directory / TAG_FOLDERS_NAME, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return folders


def _read_tag_folders(directory: Path) -> list[str]:
    path = directory / TAG_FOLDERS_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StateError("monitored folders are unreadable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise StateError("monitored folders are invalid")
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("monitored folders are unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "folders"}
        or payload["version"] != TAG_FOLDERS_VERSION
        or isinstance(payload["version"], bool)
        or not isinstance(payload["folders"], list)
        or len(payload["folders"]) > MAX_TAG_FOLDERS
        or len(set(map(str, payload["folders"]))) != len(payload["folders"])
    ):
        raise StateError("monitored folders are invalid")
    try:
        for folder in payload["folders"]:
            _absolute_path(folder)
    except UiStateValidationError as exc:
        raise StateError("monitored folders are invalid") from exc
    return sorted(payload["folders"], key=lambda value: value.encode("utf-8"))


def _ignored_tags(value: object) -> None:
    # Optional and absent when empty, so a document without it stays valid.
    if not isinstance(value, list) or not value or len(value) > MAX_IGNORED_TAGS:
        raise UiStateValidationError("ui state ignored tags are invalid")
    if len(value) != len(set(value)) or any(not _normalized_tag(tag) for tag in value):
        raise UiStateValidationError("ui state ignored tags are invalid")


def _normalized_tag(value: object) -> bool:
    # Same body rule and NFC case-folded form as HF-META-001 index keys.
    if not _text(value, maximum=80):
        return False
    if unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold()) != value:
        return False
    if not _letter(value[0]) or not (_letter(value[-1]) or value[-1].isdecimal()):
        return False
    return all(_letter(character) or character.isdecimal() or character == "-" for character in value)


def _letter(character: str) -> bool:
    return unicodedata.category(character).startswith("L")


def _state_revision(document: dict[str, object]) -> int:
    revision = document.get("stateRevision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise UiStateValidationError("ui state revision is invalid")
    return revision


def _state_content(document: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in document.items() if key != "stateRevision"}


def _root_id(value: object, allowed_root_ids: frozenset[str] | None) -> str:
    if not _unicode_scalar_string(value) or not value or len(value) > 64:
        raise UiStateValidationError("ui state root id is invalid")
    if value[0] not in _ROOT_ID_CHARS - {".", "_", "-"} or any(char not in _ROOT_ID_CHARS for char in value):
        raise UiStateValidationError("ui state root id is invalid")
    if allowed_root_ids is not None and value not in allowed_root_ids:
        raise UiStateValidationError("ui state root id is unknown")
    return value


def _absolute_path(value: object) -> str:
    """Accept one absolute canonical address (HF-NAV-005)."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise UiStateValidationError("ui state path is invalid")
    if value != "/":
        _canonical_path(value[1:])
    return value


def _canonical_path(value: object) -> str:
    if not _unicode_scalar_string(value) or "\x00" in value or "\\" in value or value.startswith("/"):
        raise UiStateValidationError("ui state path is invalid")
    if unquote(value) != value or unquote(unquote(value)) != unquote(value):
        raise UiStateValidationError("ui state path is invalid")
    if value and any(component in {"", ".", ".."} for component in value.split("/")):
        raise UiStateValidationError("ui state path is invalid")
    return value


def _identity_hint(value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UiStateValidationError("ui state identity hint is invalid")


def _text(value: object, *, maximum: int) -> bool:
    return _unicode_scalar_string(value) and value != "" and "\x00" not in value and len(value) <= maximum


def _unicode_scalar_string(value: object) -> bool:
    return isinstance(value, str) and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _encode_ui_state(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_bytes(path: Path) -> bytes:
    try:
        file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise StateError("state file is unavailable") from exc
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise StateError("state file is invalid")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(file_fd)


def _digest_regular_file(path: Path) -> str:
    try:
        file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise StateError("state file is unavailable") from exc
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise StateError("state file is invalid")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(file_fd)


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise StateError("state directory is invalid")
    os.chmod(path, 0o700)


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def initialize_state(state_directory: Path, instance_id: str) -> None:
    """Create one instance's private tree, or refuse a directory bound elsewhere.

    The binding is checked before any credential, session, nonce, or limiter
    write. A second instance id, a symlink alias, or a simultaneous claim by
    another id leaves the original stores unchanged.
    """
    directory = _state_directory(state_directory)
    if _marker_present(directory):
        require_instance_binding(directory, instance_id)
    elif _private_material_present(directory):
        raise StateError("instance state is not bound to this instance")
    else:
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        _claim_marker(directory, instance_id)
    for name in RESERVED_DIRECTORIES:
        reserved = directory / name
        reserved.mkdir(mode=0o700, exist_ok=True)
        os.chmod(reserved, 0o700)
    secret_path = directory / "signing-secret"
    if not secret_path.exists():
        atomic_write(secret_path, secrets.token_bytes(32))
    dummy_path = directory / "dummy-salt"
    if not dummy_path.exists():
        atomic_write(dummy_path, secrets.token_bytes(16))
    lock_path = directory / "kdf.lock"
    if not lock_path.exists():
        atomic_write(lock_path, b"")
    initialize_ui_state(directory)


def require_instance_binding(state_directory: Path, instance_id: str) -> None:
    """Fail closed unless this directory is already bound to instance_id.

    This reads only the binding marker. It does not create or modify stores.
    """
    directory = _state_directory(state_directory)
    marker = directory / "instance.json"
    if marker.is_symlink() or not marker.is_file():
        raise StateError("instance state is not bound to this instance")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("instance state binding is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "instanceId"}
        or payload.get("version") != 1
        or isinstance(payload.get("version"), bool)
        or payload.get("instanceId") != instance_id
    ):
        raise StateError("instance state is bound to another instance")


def read_signing_secret(state_directory: Path) -> bytes:
    secret = (state_directory / "signing-secret").read_bytes()
    if len(secret) != 32:
        raise StateError("signing secret is unavailable")
    return secret


def ensure_initialized(state_directory: Path, instance_id: str) -> None:
    """Require a matching binding before any other startup use of the stores."""
    require_instance_binding(state_directory, instance_id)
    directory = _state_directory(state_directory)
    secret = directory / "signing-secret"
    if not secret.is_file() or secret.is_symlink():
        raise StateError("instance state is not initialized")
    if len(secret.read_bytes()) != 32:
        raise StateError("signing secret is unavailable")
    # An existing state directory may lack these private directories. They hold no user content
    # or schema to reinterpret, and are created only after the instance binding was verified
    # above.
    _private_directory(directory / "operations")
    _private_directory(directory / "file-locks")
    initialize_ui_state(directory)


def _state_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise StateError("state directory must be absolute")
    return path.resolve()


def _marker_present(directory: Path) -> bool:
    if not directory.exists():
        return False
    marker = directory / "instance.json"
    return marker.is_symlink() or marker.exists()


def _private_material_present(directory: Path) -> bool:
    if not directory.exists():
        return False
    for name in ("signing-secret", "credential", "dummy-salt", "limiter.json", "kdf.lock"):
        if (directory / name).exists() or (directory / name).is_symlink():
            return True
    for name in ("sessions", "nonces", "operations"):
        store = directory / name
        if store.is_symlink():
            return True
        if store.is_dir() and any(store.iterdir()):
            return True
    return False


def _claim_marker(directory: Path, instance_id: str) -> None:
    payload = json.dumps(
        {"version": 1, "instanceId": instance_id},
        separators=(",", ":"),
    ).encode("ascii")
    marker_name = "instance.json"
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            file_fd = os.open(
                marker_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            require_instance_binding(directory, instance_id)
            return
        try:
            os.write(file_fd, payload)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)
    require_instance_binding(directory, instance_id)


@contextmanager
def kdf_lock(state_directory: Path, *, blocking: bool) -> Iterator[None]:
    """Hold the single password-KDF lock for this instance.

    Requests use a non-blocking lock so a busy verifier becomes a generic
    429 instead of a second concurrent scrypt. The local password command
    waits, then still runs one KDF.
    """
    path = state_directory / "kdf.lock"
    if not path.exists():
        atomic_write(path, b"")
    file_fd = os.open(path, os.O_RDWR)
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(file_fd, flags)
        except BlockingIOError as exc:
            raise KdfBusy("password verifier is busy") from exc
        yield
    finally:
        fcntl.flock(file_fd, fcntl.LOCK_UN)
        os.close(file_fd)
