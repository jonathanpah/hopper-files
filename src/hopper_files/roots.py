"""The single filesystem base and race-resistant address resolution.

Every instance navigates the filesystem from ``/`` with the Linux permissions
of its service account (HF-NAV-001). Addresses travel as the fixed base
identifier ``fs`` plus a path relative to ``/`` (HF-NAV-005); the identity of
every address is the absolute canonical path ``"/" + relative``. There are no
protected namespaces (HF-NAV-009): what the account can read is listed and
opens, and what it can write can be changed. Resolution walks directory
descriptors without following symbolic links (HF-API-007).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import ctypes
import time
from dataclasses import dataclass, replace
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

from hopper_files.config import InstanceConfig
from hopper_files.filetimes import birth_time

BASE_ID = "fs"
BASE_LABEL = "/"
BASE_PATH = Path("/")
# Fixed internal generation. There is no root set to republish; the value
# stays in cursors and plans so a future change of the transport is visible.
BASE_GENERATION = 1
OPERATION_STAGE_NAME = re.compile(r"^\.hopper-stage-[0-9a-f]{32}\.(?:tmp|dir)$")
# Top-level pseudo-filesystem mount points that bounded traversals never enter
# (HF-NAV-011). Manual navigation still lists and opens them (HF-NAV-002).
TRAVERSAL_EXCLUDED_TOP = frozenset({"proc", "sys", "dev"})
TRAVERSAL_MAX_DEPTH = 256
SET_ID_BITS = stat.S_ISUID | stat.S_ISGID
# New objects request these modes; the process umask (UMask=0002 in the unit)
# and a default POSIX ACL of the parent decide the result, as in a terminal
# (HF-NAV-010).
NEW_FILE_MODE = 0o666
NEW_DIRECTORY_MODE = 0o777


class AddressRejected(Exception):
    """A typed address cannot be used. code is invalid, not_found, unavailable, forbidden, or conflict."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GenerationChanged(AddressRejected):
    def __init__(self) -> None:
        super().__init__("conflict")


@dataclass(frozen=True)
class DocumentRoot:
    """The single filesystem base. Kept as a record for the fixed transport."""

    root_id: str
    label: str
    path: Path
    enabled: bool = True


@dataclass(frozen=True)
class RootCatalog:
    generation: int
    documents: tuple[DocumentRoot, ...]
    state_directory: Path
    config_path: Path

    def active_ids(self) -> set[str]:
        return {item.root_id for item in self.documents}

    def document_roots(self) -> tuple[DocumentRoot, ...]:
        return self.documents

    @property
    def trash_directory(self) -> Path:
        return self.state_directory / "trash"


@dataclass(frozen=True)
class Inspection:
    allowed: bool
    code: str
    is_directory: bool = False
    is_regular: bool = False


@dataclass(frozen=True)
class Walked:
    """One descriptor walk. directory_ids includes the base and every directory entered."""

    fd: int
    directory_ids: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class DirectoryEntry:
    """One child exactly as the service account lists it (HF-NAV-002).

    ``name`` is the address component when ``addressable`` is true. For a name
    that is not valid UTF-8 it is the lossless display form and never an
    address. ``kind`` is ``directory``, ``file``, ``link``, or ``other``.
    """

    name: str
    kind: str
    size: int | None
    openable: bool = False
    addressable: bool = True
    target: str | None = None
    resolved: str | None = None
    resolved_kind: str | None = None
    writable: bool = False
    links: int = 1
    reason: str | None = None
    modified: float | None = None
    created: float | None = None


@dataclass(frozen=True)
class SearchEntry:
    """A traversal child observation."""

    name: str
    kind: str
    size: int | None
    version: tuple[int, int, int, int, int, int, int]
    excluded: str | None = None


@dataclass(frozen=True)
class SearchDirectory:
    """Children and aggregate exclusions from one pinned directory."""

    entries: tuple[SearchEntry, ...]
    exclusions: dict[str, int]
    complete: bool = True


@dataclass(frozen=True)
class ScannedDirectory:
    """One directory visited by :func:`scan_tree`.

    ``fd`` is a readable descriptor of the directory. It is valid only until the
    generator resumes. ``stable`` is false when the directory changed while it
    was listed. ``writable`` reports whether the account can create entries in
    it, which defines the note corpus (HF-NAV-004).
    """

    path: str
    fd: int
    version: str
    stable: bool
    writable: bool
    entries: tuple[SearchEntry, ...]
    exclusions: dict[str, int]


@dataclass
class StagedRegular:
    """An operation-owned, unpublished regular file in a destination parent.

    The parent descriptor stays open until the caller publishes or discards the
    stage.  This prevents a later pathname resolution from changing the parent
    selected during the containment check.
    """

    root_id: str
    destination: str
    temporary_name: str
    destination_name: str
    parent_fd: int
    digest: str
    size: int
    identity: tuple[int, int]


@dataclass
class StagedDirectory:
    """An operation-owned, unpublished directory."""

    root_id: str
    destination: str
    temporary_name: str
    destination_name: str
    parent_fd: int
    identity: tuple[int, int]


@dataclass
class StageWriter:
    """Incremental writer used by HTTP request streams without buffering them."""

    root_id: str
    destination: str
    temporary_name: str
    destination_name: str
    parent_fd: int
    file_fd: int
    maximum: int | None
    digest: object
    size: int = 0

    def write(self, block: bytes) -> None:
        if not isinstance(block, bytes):
            raise AddressRejected("invalid")
        self.size += len(block)
        if self.maximum is not None and self.size > self.maximum:
            raise AddressRejected("limit")
        self.digest.update(block)
        view = memoryview(block)
        while view:
            written = os.write(self.file_fd, view)
            if written <= 0:
                raise OSError("stage write was incomplete")
            view = view[written:]

    def finish(self) -> StagedRegular:
        try:
            os.fsync(self.file_fd)
            details = os.fstat(self.file_fd)
            return StagedRegular(
                root_id=self.root_id,
                destination=self.destination,
                temporary_name=self.temporary_name,
                destination_name=self.destination_name,
                parent_fd=self.parent_fd,
                digest=self.digest.hexdigest(),
                size=self.size,
                identity=(details.st_dev, details.st_ino),
            )
        finally:
            os.close(self.file_fd)
            self.file_fd = -1

    def abort(self) -> None:
        identity = None
        try:
            if self.file_fd >= 0:
                try:
                    details = os.fstat(self.file_fd)
                    identity = (details.st_dev, details.st_ino)
                except OSError:
                    pass
                finally:
                    os.close(self.file_fd)
                    self.file_fd = -1
            if identity is not None:
                _unlink_if_regular(self.temporary_name, self.parent_fd, identity=identity)
                os.fsync(self.parent_fd)
        finally:
            if self.parent_fd >= 0:
                os.close(self.parent_fd)
                self.parent_fd = -1


def build_catalog(config: InstanceConfig, *, strict: bool = True) -> RootCatalog:
    """Return the single-base catalog of one instance. ``strict`` is ignored."""
    del strict
    return RootCatalog(
        generation=BASE_GENERATION,
        documents=(DocumentRoot(BASE_ID, BASE_LABEL, BASE_PATH, True),),
        state_directory=config.state_directory,
        config_path=config.source_path,
    )


def require_generation(catalog: RootCatalog, captured: int) -> None:
    if catalog.generation != captured:
        raise GenerationChanged()


def require_complete(catalog: RootCatalog, captured: int) -> None:
    """The single base is always configured; only the generation can differ."""
    require_generation(catalog, captured)


def root_record(catalog: RootCatalog, root_id: str) -> DocumentRoot | None:
    for item in catalog.documents:
        if item.root_id == root_id:
            return item
    return None


def availability(catalog: RootCatalog, root_id: str) -> str:
    return "available" if root_record(catalog, root_id) is not None else "unavailable"


def capabilities(catalog: RootCatalog, root_id: str) -> dict[str, bool]:
    if root_record(catalog, root_id) is None:
        return {"read": False, "write": False, "trash": False}
    return {"read": True, "write": True, "trash": True}


def absolute_address(relative_path: str) -> str:
    """Return the absolute canonical address of a validated relative path."""
    parse_relative_path(relative_path)
    return "/" + relative_path


def relative_address(absolute: str) -> str:
    """Validate an absolute canonical address and return its transport path."""
    if not isinstance(absolute, str) or not absolute.startswith("/"):
        raise AddressRejected("invalid")
    if absolute == "/":
        return ""
    relative = absolute[1:]
    parse_relative_path(relative)
    return relative


def display_name(raw: bytes) -> tuple[str, bool]:
    """Return ``(display, addressable)`` for one directory entry name.

    A valid UTF-8 name displays as itself and is addressable. Otherwise every
    byte outside valid UTF-8 shows as ``\\xHH`` and every literal backslash as
    ``\\\\`` (HF-NAV-002); such a name has no address.
    """
    try:
        return raw.decode("utf-8", errors="strict"), True
    except UnicodeDecodeError:
        pass
    shown: list[str] = []
    index = 0
    while index < len(raw):
        try:
            text = raw[index:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            valid = raw[index : index + exc.start].decode("utf-8")
            shown.append(valid.replace("\\", "\\\\"))
            bad = raw[index + exc.start : index + exc.end]
            shown.extend(f"\\x{byte:02X}" for byte in bad)
            index += exc.end
            continue
        shown.append(text.replace("\\", "\\\\"))
        break
    return "".join(shown), False


def parse_relative_path(raw: str) -> tuple[str, ...]:
    """Accept one canonical path relative to ``/``. `/` is the only separator."""
    if not _unicode_scalar(raw) or "\x00" in raw or "\\" in raw:
        raise AddressRejected("invalid")
    if unquote(raw) != raw or unquote(unquote(raw)) != unquote(raw):
        raise AddressRejected("invalid")
    if raw.startswith("/"):
        raise AddressRejected("invalid")
    if raw == "":
        return ()
    parts = tuple(raw.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise AddressRejected("invalid")
    return parts


def inspect_address(catalog: RootCatalog, root_id: str, relative_path: str, *, operation: str) -> Inspection:
    """Classify an address without reading file content.

    Symbolic links and special files are rejected. The decision uses the
    descriptors opened for this call, not a later path lookup.
    """
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    try:
        walked = _walk(parts)
    except AddressRejected as exc:
        if exc.code == "unavailable":
            raise
        return Inspection(False, "forbidden")
    try:
        return _judge(walked, operation)
    finally:
        os.close(walked.fd)


def address_has_missing_component(catalog: RootCatalog, root_id: str, relative_path: str) -> bool:
    """Prove an address is absent without following symbolic links.

    This is used only to retain the lexical identity of a dangling Markdown
    destination. Existing or unsafe objects return ``False`` and remain
    subject to the regular address checks.
    """
    try:
        _require_available(catalog, root_id)
        parts = parse_relative_path(relative_path)
        if not parts:
            return False
        fd = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    except (AddressRejected, OSError):
        return False
    try:
        for index, component in enumerate(parts):
            try:
                child = os.open(component, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            child_info = os.fstat(child)
            if stat.S_ISLNK(child_info.st_mode) or _is_special(child_info.st_mode):
                os.close(child)
                return False
            if index == len(parts) - 1 or not stat.S_ISDIR(child_info.st_mode):
                os.close(child)
                return False
            os.close(fd)
            fd = child
        return False
    except OSError:
        return False
    finally:
        os.close(fd)


def read_regular(catalog: RootCatalog, root_id: str, relative_path: str) -> bytes:
    """Read one regular file. Other objects are not read."""
    with open_regular(catalog, root_id, relative_path) as (readable, _info):
        chunks: list[bytes] = []
        while True:
            block = os.read(readable, 65536)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)


@contextmanager
def open_regular(catalog: RootCatalog, root_id: str, relative_path: str) -> Iterator[tuple[int, os.stat_result]]:
    """Yield an opened regular file for streaming callers.

    The caller receives a read-only descriptor, never a filesystem pathname.
    Its identity is checked against the descriptor obtained while walking the
    address, so an external replacement cannot redirect the read.
    """
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    walked = _walk(parts)
    readable = -1
    try:
        decision = _judge(walked, "read")
        if not decision.allowed or not decision.is_regular:
            raise AddressRejected("forbidden" if decision.code == "ok" else decision.code)
        opened = os.fstat(walked.fd)
        try:
            readable = os.open(f"/proc/self/fd/{walked.fd}", os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError as exc:
            raise AddressRejected("forbidden") from exc
        confirmed = os.fstat(readable)
        if (confirmed.st_dev, confirmed.st_ino) != (opened.st_dev, opened.st_ino) or not stat.S_ISREG(confirmed.st_mode):
            raise AddressRejected("forbidden")
        yield readable, confirmed
    finally:
        if readable >= 0:
            os.close(readable)
        os.close(walked.fd)


def list_directory(
    catalog: RootCatalog,
    root_id: str,
    relative_path: str,
    *,
    strict: bool = False,
) -> list[DirectoryEntry]:
    """List every child the service account obtains for this directory.

    With ``strict`` the caller needs a tree it can process as a whole (copy,
    move, delete, reference scans): a link, a special object, a name without
    an address, or an unreadable child refuses the whole listing.
    """
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    walked = _walk(parts)
    try:
        decision = _judge(walked, "read")
        if not decision.allowed or not decision.is_directory:
            raise AddressRejected("forbidden" if decision.code == "ok" else decision.code)
        listing_fd = _reopen_directory(walked.fd)
        listed: list[DirectoryEntry] = []
        try:
            prefix = "/" + "/".join(parts) if parts else ""
            with os.scandir(listing_fd) as entries:
                for entry in entries:
                    child = _describe_entry(listing_fd, entry, prefix)
                    if strict and (child.kind not in {"file", "directory"} or not child.addressable):
                        raise AddressRejected("forbidden")
                    listed.append(child)
        finally:
            os.close(listing_fd)
        return sorted(listed, key=lambda item: item.name.encode("utf-8", "surrogateescape"))
    finally:
        os.close(walked.fd)


def directory_writable(catalog: RootCatalog, root_id: str, relative_path: str) -> bool:
    """Report whether the account may create entries in a directory now.

    This is advice for the interface (HF-NAV-003); the operation itself still
    relies on the kernel at the moment it runs.
    """
    _require_available(catalog, root_id)
    walked = _walk(parse_relative_path(relative_path))
    try:
        if not stat.S_ISDIR(os.fstat(walked.fd).st_mode):
            return False
        return _access_at(walked.fd, ".", os.W_OK | os.X_OK)
    finally:
        os.close(walked.fd)


def _describe_entry(parent_fd: int, entry: os.DirEntry, prefix: str) -> DirectoryEntry:
    raw = os.fsencode(entry.name)
    shown, addressable = display_name(raw)
    try:
        details = entry.stat(follow_symlinks=False)
    except OSError:
        return DirectoryEntry(shown, "other", None, addressable=addressable, reason="unavailable")
    return replace(_describe_kind(parent_fd, entry, prefix, shown, addressable, details),
                   modified=details.st_mtime, created=birth_time(parent_fd, raw))


def _describe_kind(parent_fd: int, entry: os.DirEntry, prefix: str, shown: str, addressable: bool,
                   details: os.stat_result) -> DirectoryEntry:
    mode = details.st_mode
    if not addressable:
        kind = "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "link" if stat.S_ISLNK(mode) else "other"
        return DirectoryEntry(shown, kind, details.st_size if stat.S_ISREG(mode) else None, addressable=False, reason="invalid_name")
    name = entry.name
    if stat.S_ISDIR(mode):
        openable = _access_at(parent_fd, name, os.R_OK | os.X_OK)
        writable = openable and _access_at(parent_fd, name, os.W_OK | os.X_OK)
        return DirectoryEntry(
            name,
            "directory",
            None,
            openable=openable,
            writable=writable,
            links=details.st_nlink,
            reason=None if openable else "permission",
        )
    if stat.S_ISREG(mode):
        openable = _access_at(parent_fd, name, os.R_OK)
        return DirectoryEntry(
            name,
            "file",
            details.st_size,
            openable=openable,
            writable=_access_at(parent_fd, name, os.W_OK),
            links=details.st_nlink,
            reason=None if openable else "permission",
        )
    if stat.S_ISLNK(mode):
        return _describe_link(parent_fd, name, prefix)
    return DirectoryEntry(name, "other", None, links=details.st_nlink, reason="special")


def _describe_link(parent_fd: int, name: str, prefix: str) -> DirectoryEntry:
    try:
        target_bytes = os.readlink(name, dir_fd=parent_fd)
    except OSError:
        return DirectoryEntry(name, "link", None, reason="unavailable")
    target, _valid = display_name(os.fsencode(target_bytes))
    try:
        resolved_raw = os.path.realpath(f"{prefix}/{name}", strict=True)
        info = os.stat(resolved_raw)
    except (OSError, RuntimeError, ValueError):
        return DirectoryEntry(name, "link", None, target=target, reason="broken")
    resolved_text, resolved_valid = display_name(os.fsencode(resolved_raw))
    if not resolved_valid:
        return DirectoryEntry(name, "link", None, target=target, reason="invalid_name")
    if stat.S_ISDIR(info.st_mode):
        openable = os.access(resolved_raw, os.R_OK | os.X_OK)
        return DirectoryEntry(
            name,
            "link",
            None,
            openable=openable,
            target=target,
            resolved=resolved_text,
            resolved_kind="directory",
            reason=None if openable else "permission",
        )
    if stat.S_ISREG(info.st_mode):
        openable = os.access(resolved_raw, os.R_OK)
        return DirectoryEntry(
            name,
            "link",
            info.st_size,
            openable=openable,
            target=target,
            resolved=resolved_text,
            resolved_kind="file",
            reason=None if openable else "permission",
        )
    return DirectoryEntry(name, "link", None, target=target, resolved=resolved_text, resolved_kind="other", reason="special")


def list_search_directory(
    catalog: RootCatalog,
    root_id: str,
    relative_path: str,
    *,
    deadline: float | None = None,
) -> SearchDirectory:
    """Observe the children of one directory for a bounded traversal."""
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    walked = _walk(parts)
    try:
        decision = _judge(walked, "read")
        if not decision.allowed or not decision.is_directory:
            raise AddressRejected("forbidden" if decision.code == "ok" else decision.code)
        listing_fd = _reopen_directory(walked.fd)
        try:
            entries, exclusions, complete = _observe_children(listing_fd, deadline)
        finally:
            os.close(listing_fd)
        return SearchDirectory(entries, exclusions, complete)
    finally:
        os.close(walked.fd)


def _observe_children(
    listing_fd: int,
    deadline: float | None,
) -> tuple[tuple[SearchEntry, ...], dict[str, int], bool]:
    entries: list[SearchEntry] = []
    exclusions: dict[str, int] = {}
    complete = True
    with os.scandir(listing_fd) as children:
        for child in children:
            if deadline is not None and time.monotonic() >= deadline:
                exclusions["execution_budget"] = exclusions.get("execution_budget", 0) + 1
                complete = False
                break
            name = child.name
            if not _unicode_scalar(name):
                exclusions["invalid_name"] = exclusions.get("invalid_name", 0) + 1
                continue
            try:
                details = child.stat(follow_symlinks=False)
            except OSError:
                exclusions["entry_race"] = exclusions.get("entry_race", 0) + 1
                continue
            mode = details.st_mode
            version = _search_stat_version(details)
            if stat.S_ISLNK(mode):
                entries.append(SearchEntry(name, "link", None, version))
            elif stat.S_ISREG(mode):
                entries.append(SearchEntry(name, "file", details.st_size, version))
            elif stat.S_ISDIR(mode):
                entries.append(SearchEntry(name, "directory", None, version))
            else:
                entries.append(SearchEntry(name, "other", None, version, "special_file"))
    entries.sort(key=lambda item: item.name.encode("utf-8"))
    return tuple(entries), exclusions, complete


def scan_tree(
    catalog: RootCatalog,
    root_id: str,
    relative_path: str,
    *,
    deadline: float,
    skip_internal_trash: bool,
    counts: dict[str, int],
    skip_paths: frozenset[str] = frozenset(),
    skip_hidden: bool = False,
) -> Iterator[ScannedDirectory]:
    """Walk a directory tree depth first under HF-NAV-011.

    It never enters ``/proc``, ``/sys``, ``/dev``, pseudo filesystems mounted
    elsewhere, or symbolic links. With ``skip_internal_trash`` it also skips the
    instance trash. A directory whose address relative to ``/`` is in
    ``skip_paths`` is not entered and is counted as ``excluded_path``. With
    ``skip_hidden``, a subdirectory whose name starts with ``.`` is not entered
    and is counted as ``hidden_directory``; the starting directory itself is
    always entered. An unlistable directory is counted as
    ``unreadable_directory`` and skipped. Stops and races are recorded in
    ``counts`` as ``execution_budget``, ``depth_limit``, and ``entry_race``; a
    caller treats those as incomplete.
    """
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    walked = _walk(parts)
    try:
        decision = _judge(walked, "read")
        if not decision.allowed or not decision.is_directory:
            raise AddressRejected("forbidden" if decision.code == "ok" else decision.code)
        start_fd = _reopen_directory(walked.fd)
    finally:
        os.close(walked.fd)
    pseudo = _pseudo_devices()
    trash_identity = _identity_of(catalog.trash_directory) if skip_internal_trash else None
    try:
        start_info = os.fstat(start_fd)
        if start_info.st_dev in pseudo and _pseudo_mount_below(parts):
            return
        yield from _scan_directory_fd(
            start_fd, "/".join(parts), 0, deadline, pseudo, trash_identity, counts, skip_paths, skip_hidden
        )
    finally:
        os.close(start_fd)


def _scan_directory_fd(
    fd: int,
    path: str,
    depth: int,
    deadline: float,
    pseudo: frozenset[int],
    trash_identity: tuple[int, int] | None,
    counts: dict[str, int],
    skip_paths: frozenset[str] = frozenset(),
    skip_hidden: bool = False,
) -> Iterator[ScannedDirectory]:
    if time.monotonic() >= deadline:
        counts["execution_budget"] = counts.get("execution_budget", 0) + 1
        return
    before = os.fstat(fd)
    try:
        entries, exclusions, complete = _observe_children(fd, deadline)
    except OSError:
        counts["entry_race"] = counts.get("entry_race", 0) + 1
        return
    after = os.fstat(fd)
    stable = _directory_state(before) == _directory_state(after)
    yield ScannedDirectory(
        path,
        fd,
        _directory_version_of(BASE_GENERATION, after),
        stable,
        _access_at(fd, ".", os.W_OK | os.X_OK),
        entries,
        exclusions,
    )
    if not complete:
        counts["execution_budget"] = counts.get("execution_budget", 0) + 1
        return
    for entry in entries:
        if entry.kind != "directory":
            continue
        if time.monotonic() >= deadline:
            counts["execution_budget"] = counts.get("execution_budget", 0) + 1
            return
        if path == "" and entry.name in TRAVERSAL_EXCLUDED_TOP:
            counts["pseudo_filesystem"] = counts.get("pseudo_filesystem", 0) + 1
            continue
        if entry.version[0] in pseudo:
            counts["pseudo_filesystem"] = counts.get("pseudo_filesystem", 0) + 1
            continue
        if trash_identity is not None and (entry.version[0], entry.version[1]) == trash_identity:
            counts["internal_trash"] = counts.get("internal_trash", 0) + 1
            continue
        if skip_hidden and entry.name.startswith("."):
            counts["hidden_directory"] = counts.get("hidden_directory", 0) + 1
            continue
        child_path = f"{path}/{entry.name}" if path else entry.name
        if child_path in skip_paths:
            counts["excluded_path"] = counts.get("excluded_path", 0) + 1
            continue
        if depth + 1 >= TRAVERSAL_MAX_DEPTH:
            counts["depth_limit"] = counts.get("depth_limit", 0) + 1
            continue
        try:
            child_fd = os.open(
                entry.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
        except PermissionError:
            counts["unreadable_directory"] = counts.get("unreadable_directory", 0) + 1
            continue
        except OSError:
            counts["entry_race"] = counts.get("entry_race", 0) + 1
            continue
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (entry.version[0], entry.version[1]):
                counts["entry_race"] = counts.get("entry_race", 0) + 1
                continue
            yield from _scan_directory_fd(
                child_fd, child_path, depth + 1, deadline, pseudo, trash_identity, counts, skip_paths, skip_hidden
            )
        finally:
            os.close(child_fd)


def _pseudo_mount_below(parts: tuple[str, ...]) -> bool:
    return bool(parts) and parts[0] in TRAVERSAL_EXCLUDED_TOP


def traversal_excluded(relative_path: str) -> bool:
    """True when a traversal must not enter this directory address."""
    parts = parse_relative_path(relative_path)
    return bool(parts) and parts[0] in TRAVERSAL_EXCLUDED_TOP


def _identity_of(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def open_scanned_regular(parent_fd: int, entry: SearchEntry) -> int:
    """Open one regular child of a scanned directory, checking its identity."""
    try:
        fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent_fd)
    except OSError as exc:
        raise AddressRejected("forbidden") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _search_stat_version(info) != entry.version:
            raise AddressRejected("conflict")
    except BaseException:
        os.close(fd)
        raise
    return fd


def search_object_version(
    catalog: RootCatalog,
    root_id: str,
    relative_path: str,
) -> tuple[int, int, int, int, int, int, int]:
    """Return a private version of the named object without following it.

    A symbolic link as the final component is observed as the link itself, so
    a name search can revalidate a matching link. It never reads contents.
    """
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    if not parts:
        walked = _walk(())
        try:
            return _search_stat_version(os.fstat(walked.fd))
        finally:
            os.close(walked.fd)
    walked = _walk(parts[:-1])
    try:
        try:
            info = os.stat(parts[-1], dir_fd=walked.fd, follow_symlinks=False)
        except OSError as exc:
            raise AddressRejected("conflict") from exc
        return _search_stat_version(info)
    finally:
        os.close(walked.fd)


def _search_stat_version(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
        int(info.st_nlink),
        int(stat.S_IFMT(info.st_mode)),
    )


def _directory_state(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _directory_version_of(generation: int, details: os.stat_result) -> str:
    payload = json.dumps(
        [generation, details.st_dev, details.st_ino, details.st_mtime_ns, details.st_ctime_ns],
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def directory_version(catalog: RootCatalog, root_id: str, relative_path: str) -> str:
    """Return an opaque version for a directory listing."""
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    walked = _walk(parts)
    try:
        decision = _judge(walked, "read")
        if not decision.allowed or not decision.is_directory:
            raise AddressRejected("forbidden" if decision.code == "ok" else decision.code)
        return _directory_version_of(catalog.generation, os.fstat(walked.fd))
    finally:
        os.close(walked.fd)


@contextmanager
def readable_directory(catalog: RootCatalog, root_id: str, relative_path: str) -> Iterator[tuple[str, tuple[int, int]]]:
    """Yield a kernel path and the identity of a directory the account can read.

    The address is resolved as a listing resolves it, without following
    symbolic links. The yielded path names the opened descriptor, so a later
    replacement of the address cannot redirect the caller.
    """
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    walked = _walk(parts)
    try:
        decision = _judge(walked, "read")
        if not decision.allowed or not decision.is_directory:
            raise AddressRejected("forbidden" if decision.code == "ok" else decision.code)
        readable = _reopen_directory(walked.fd)
        try:
            info = os.fstat(readable)
            yield f"/proc/self/fd/{readable}", (info.st_dev, info.st_ino)
        finally:
            os.close(readable)
    finally:
        os.close(walked.fd)


def destination_capacity(catalog: RootCatalog, root_id: str, destination: str) -> tuple[int, int]:
    """Return (filesystem device, available bytes) for a validated destination parent."""
    parts = parse_relative_path(destination)
    if not parts:
        raise AddressRejected("invalid")
    walked = _open_directory_for_create(catalog, root_id, "/".join(parts[:-1]), parts[-1])[1]
    try:
        info = os.fstat(walked.fd)
        capacity = os.fstatvfs(walked.fd)
        return info.st_dev, capacity.f_bavail * capacity.f_frsize
    except OSError as exc:
        raise AddressRejected("unavailable") from exc
    finally:
        os.close(walked.fd)


def destination_exists(catalog: RootCatalog, root_id: str, destination: str) -> bool:
    """Check a parent for an occupied leaf without following it."""
    parts = parse_relative_path(destination)
    if not parts:
        raise AddressRejected("invalid")
    walked = _open_directory_for_create(catalog, root_id, "/".join(parts[:-1]), parts[-1])[1]
    try:
        try:
            os.stat(parts[-1], dir_fd=walked.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(walked.fd)


def validate_new_address(catalog: RootCatalog, root_id: str, relative_path: str) -> None:
    """Validate the base identifier and every lexical component before staging."""
    if root_record(catalog, root_id) is None:
        raise AddressRejected("not_found")
    parts = parse_relative_path(relative_path)
    if not parts:
        raise AddressRejected("invalid")
    for part in parts:
        _reject_component(part)


def inspect_operation_object(
    catalog: RootCatalog,
    root_id: str,
    relative_path: str,
    *,
    expected_kind: str,
) -> tuple[tuple[int, int], str | None, int | None] | None:
    """Observe a regular file or directory through the same walk used by operations."""
    _require_available(catalog, root_id)
    parts = parse_relative_path(relative_path)
    try:
        walked = _walk(parts)
    except AddressRejected as exc:
        if exc.code == "forbidden":
            return None
        raise
    try:
        decision = _judge(walked, "read")
        details = os.fstat(walked.fd)
        if not decision.allowed:
            return None
        if expected_kind == "directory":
            return ((details.st_dev, details.st_ino), None, None) if stat.S_ISDIR(details.st_mode) else None
        if expected_kind != "file" or not stat.S_ISREG(details.st_mode):
            return None
        try:
            fd = os.open(f"/proc/self/fd/{walked.fd}", os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                return None
            digest = hashlib.sha256()
            while True:
                block = os.read(fd, 64 * 1024)
                if not block:
                    break
                digest.update(block)
            return ((opened.st_dev, opened.st_ino), digest.hexdigest(), opened.st_size)
        finally:
            os.close(fd)
    finally:
        os.close(walked.fd)


def discard_operation_stage(
    catalog: RootCatalog,
    root_id: str,
    destination: str,
    stage_name: str,
    identity: tuple[int, int],
    *,
    kind: str,
) -> bool:
    """Remove only a journaled stage whose name, type, and inode still match."""
    if OPERATION_STAGE_NAME.fullmatch(stage_name) is None:
        raise AddressRejected("invalid")
    parts = parse_relative_path(destination)
    if not parts:
        raise AddressRejected("invalid")
    walked = _open_directory_for_create(catalog, root_id, "/".join(parts[:-1]), parts[-1])[1]
    try:
        try:
            details = os.stat(stage_name, dir_fd=walked.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        expected_type = stat.S_ISREG if kind == "file" else stat.S_ISDIR if kind == "directory" else None
        if expected_type is None or not expected_type(details.st_mode):
            return False
        if (details.st_dev, details.st_ino) != identity:
            return False
        if kind == "file":
            os.unlink(stage_name, dir_fd=walked.fd)
        else:
            _remove_tree_at(walked.fd, stage_name, identity)
        os.fsync(walked.fd)
        return True
    finally:
        os.close(walked.fd)


def stage_regular(
    catalog: RootCatalog,
    root_id: str,
    destination: str,
    chunks: Iterable[bytes],
    *,
    maximum: int | None = None,
) -> StagedRegular:
    """Stream bytes into a private stage beside a destination.

    The stage is a new object (HF-NAV-010) created exclusively and fsynced
    before this function returns. It is not a published destination:
    :func:`publish_staged_regular` links it exclusively only after an operation
    journal has recorded the intent.
    """
    writer = begin_stage_regular(catalog, root_id, destination, maximum=maximum)
    try:
        for block in chunks:
            writer.write(block)
        return writer.finish()
    except BaseException:
        writer.abort()
        raise


def begin_stage_regular(
    catalog: RootCatalog,
    root_id: str,
    destination: str,
    *,
    maximum: int | None = None,
) -> StageWriter:
    """Open a private destination stage for an asynchronous streaming writer."""
    parts = parse_relative_path(destination)
    if not parts:
        raise AddressRejected("invalid")
    parent_path = "/".join(parts[:-1])
    name = parts[-1]
    _reject_component(name)
    _record, walked = _open_directory_for_create(catalog, root_id, parent_path, name)
    temporary_name = f".hopper-stage-{secrets.token_hex(16)}.tmp"
    try:
        file_fd = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            NEW_FILE_MODE,
            dir_fd=walked.fd,
        )
        return StageWriter(
            root_id=root_id,
            destination=destination,
            temporary_name=temporary_name,
            destination_name=name,
            parent_fd=walked.fd,
            file_fd=file_fd,
            maximum=maximum,
            digest=hashlib.sha256(),
        )
    except (AddressRejected, OSError) as exc:
        os.close(walked.fd)
        if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
            raise AddressRejected("limit") from exc
        raise AddressRejected("forbidden") from exc


def stage_directory_exclusive(catalog: RootCatalog, root_id: str, destination: str) -> StagedDirectory:
    """Create an empty new directory in staging beside its destination."""
    parts = parse_relative_path(destination)
    if not parts:
        raise AddressRejected("invalid")
    name = parts[-1]
    _reject_component(name)
    _record, walked = _open_directory_for_create(catalog, root_id, "/".join(parts[:-1]), name)
    temporary_name = f".hopper-stage-{secrets.token_hex(16)}.dir"
    child = -1
    try:
        os.mkdir(temporary_name, NEW_DIRECTORY_MODE, dir_fd=walked.fd)
        child = os.open(
            temporary_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=walked.fd,
        )
        os.fsync(child)
        details = os.fstat(child)
        os.fsync(walked.fd)
        return StagedDirectory(root_id, destination, temporary_name, name, walked.fd, (details.st_dev, details.st_ino))
    except FileExistsError as exc:
        os.close(walked.fd)
        raise AddressRejected("conflict") from exc
    except OSError as exc:
        if child >= 0:
            os.close(child)
            child = -1
        _remove_staged_directory(temporary_name, walked.fd)
        os.close(walked.fd)
        if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
            raise AddressRejected("limit") from exc
        raise AddressRejected("forbidden") from exc
    finally:
        if child >= 0:
            os.close(child)


@dataclass
class OpenedChild:
    """Parent descriptor of one delete source. The caller closes it."""

    parent_fd: int
    name: str
    mode: int
    device: int
    inode: int
    uid: int
    gid: int
    size: int
    nlink: int

    def close(self) -> None:
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


@dataclass
class OpenedDestination:
    """Parent descriptor for an exclusive restore name. The caller closes it."""

    parent_fd: int
    name: str
    parent_device: int

    def close(self) -> None:
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


def open_delete_source(catalog: RootCatalog, root_id: str, relative_path: str) -> OpenedChild:
    """Open the parent of a nonempty address to move to trash.

    The returned descriptor is the parent selected by this walk. Callers must
    recheck the child identity before unlinking or renaming it.
    """
    parts = parse_relative_path(relative_path)
    if not parts:
        raise AddressRejected("invalid")
    _require_available(catalog, root_id)
    name = parts[-1]
    _reject_component(name)
    walked = _walk(parts[:-1])
    parent = walked.fd
    keep = False
    child = -1
    try:
        try:
            info = os.lstat(name, dir_fd=parent)
        except FileNotFoundError as exc:
            raise AddressRejected("not_found") from exc
        except OSError as exc:
            raise AddressRejected("forbidden") from exc
        if stat.S_ISLNK(info.st_mode) or _is_special(info.st_mode):
            raise AddressRejected("forbidden")
        try:
            child = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except OSError as exc:
            raise AddressRejected("forbidden") from exc
        current = os.fstat(child)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or stat.S_ISLNK(current.st_mode):
            raise AddressRejected("forbidden")
        # The walk holds an O_PATH descriptor. Reopen it for rename and fsync.
        parent = _reopen_directory(walked.fd)
        os.close(walked.fd)
        keep = True
        return OpenedChild(
            parent,
            name,
            current.st_mode,
            current.st_dev,
            current.st_ino,
            current.st_uid,
            current.st_gid,
            current.st_size,
            current.st_nlink,
        )
    finally:
        if child >= 0:
            os.close(child)
        if not keep:
            os.close(walked.fd)


def open_restore_parent(catalog: RootCatalog, root_id: str, relative_path: str) -> OpenedDestination:
    """Open the parent of a free restore address.

    An occupied final component is a conflict. The caller publishes exclusively.
    """
    parts = parse_relative_path(relative_path)
    if not parts:
        raise AddressRejected("invalid")
    name = parts[-1]
    _record, walked = _open_directory_for_create(catalog, root_id, "/".join(parts[:-1]), name)
    keep = False
    try:
        try:
            os.lstat(name, dir_fd=walked.fd)
        except FileNotFoundError:
            pass
        else:
            raise AddressRejected("conflict")
        parent_info = os.fstat(walked.fd)
        keep = True
        return OpenedDestination(walked.fd, name, parent_info.st_dev)
    finally:
        if not keep:
            os.close(walked.fd)


def open_existing_directory(catalog: RootCatalog, root_id: str, relative_directory: str) -> int:
    """Return a readable descriptor for an existing directory. The caller closes it.

    ``relative_directory`` may be empty for ``/`` itself. The descriptor is the
    directory selected by this walk, not a later path lookup.
    """
    _record, walked = _open_directory_for_create(catalog, root_id, relative_directory, "hopper-parent")
    return walked.fd


def device_is_pseudo(device: int) -> bool:
    """Return whether ``device`` is one of the pseudo filesystems."""
    return device in _pseudo_devices()


def new_object_modes(catalog: RootCatalog, root_id: str) -> tuple[int, int]:
    """Return the requested modes for new regular files and directories.

    The kernel applies the process umask and any default ACL of the parent, as
    it does for the account's terminal (HF-NAV-010).
    """
    if root_record(catalog, root_id) is None:
        raise AddressRejected("not_found")
    return NEW_FILE_MODE, NEW_DIRECTORY_MODE


def open_staged_directory(stage: StagedDirectory) -> int:
    """Open a staging directory by its recorded name and verify its inode."""
    fd = os.open(
        stage.temporary_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=stage.parent_fd,
    )
    details = os.fstat(fd)
    if (details.st_dev, details.st_ino) != stage.identity:
        os.close(fd)
        raise AddressRejected("conflict")
    return fd


def create_staged_directory_child(parent_fd: int, name: str, mode: int) -> int:
    """Create one new child directory beneath a private stage."""
    _reject_component(name)
    os.mkdir(name, mode, dir_fd=parent_fd)
    child_fd = -1
    identity = None
    try:
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        details = os.fstat(child_fd)
        identity = (details.st_dev, details.st_ino)
        os.fsync(child_fd)
        os.fsync(parent_fd)
        return child_fd
    except BaseException:
        if child_fd >= 0:
            os.close(child_fd)
        _remove_new_directory(name, parent_fd, identity)
        raise


def create_staged_regular_child(parent_fd: int, name: str, mode: int) -> int:
    """Create one new empty regular child beneath a private stage."""
    _reject_component(name)
    return os.open(
        name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=parent_fd,
    )


def publish_staged_directory(stage: StagedDirectory) -> tuple[int, int]:
    """Publish a verified directory with an atomic no-replace rename."""
    try:
        try:
            _rename_noreplace(stage.parent_fd, stage.temporary_name, stage.destination_name)
        except FileExistsError as exc:
            discard_staged_directory(stage)
            raise AddressRejected("conflict") from exc
        details = os.stat(stage.destination_name, dir_fd=stage.parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode) or (details.st_dev, details.st_ino) != stage.identity:
            raise AddressRejected("forbidden")
        os.fsync(stage.parent_fd)
        return details.st_dev, details.st_ino
    except OSError as exc:
        code = "limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable"
        raise AddressRejected(code) from exc
    finally:
        if stage.parent_fd >= 0:
            os.close(stage.parent_fd)
            stage.parent_fd = -1


def discard_staged_directory(stage: StagedDirectory) -> None:
    """Remove only the exact staging tree owned by this operation."""
    try:
        try:
            details = os.stat(stage.temporary_name, dir_fd=stage.parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode) and (details.st_dev, details.st_ino) == stage.identity:
                _remove_tree_at(stage.parent_fd, stage.temporary_name, stage.identity)
                os.fsync(stage.parent_fd)
        except FileNotFoundError:
            pass
    finally:
        if stage.parent_fd >= 0:
            os.close(stage.parent_fd)
            stage.parent_fd = -1


def _remove_staged_directory(name: str, parent_fd: int) -> None:
    try:
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        pass


def _remove_tree_at(parent_fd: int, name: str, identity: tuple[int, int] | None = None) -> None:
    """Remove a named tree without following links or crossing its descriptor."""
    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if identity is not None and (details.st_dev, details.st_ino) != identity:
        return
    if stat.S_ISDIR(details.st_mode):
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                return
            for child_name in os.listdir(child_fd):
                _remove_tree_at(child_fd, child_name)
            os.fsync(child_fd)
        finally:
            os.close(child_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (details.st_dev, details.st_ino) and stat.S_ISDIR(current.st_mode):
            os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def publish_staged_regular(stage: StagedRegular) -> tuple[int, int]:
    """Publish one complete stage without overwriting an occupied destination."""
    try:
        try:
            _rename_noreplace(stage.parent_fd, stage.temporary_name, stage.destination_name)
        except FileExistsError as exc:
            _unlink_if_regular(stage.temporary_name, stage.parent_fd, identity=stage.identity)
            os.fsync(stage.parent_fd)
            raise AddressRejected("conflict") from exc
        except OSError as exc:
            code = "limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable"
            raise AddressRejected(code) from exc
        # The operation journal is already durable. A failure below keeps the
        # stage for recovery if publication may have occurred before cleanup.
        published = os.stat(stage.destination_name, dir_fd=stage.parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode) or (published.st_dev, published.st_ino) != stage.identity:
            raise AddressRejected("forbidden")
        os.fsync(stage.parent_fd)
        return published.st_dev, published.st_ino
    except OSError as exc:
        code = "limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable"
        raise AddressRejected(code) from exc
    finally:
        if stage.parent_fd >= 0:
            os.close(stage.parent_fd)
            stage.parent_fd = -1


def discard_staged_regular(stage: StagedRegular) -> None:
    """Discard only the exact unpublished stage owned by this operation."""
    try:
        _unlink_if_regular(stage.temporary_name, stage.parent_fd, identity=stage.identity)
        os.fsync(stage.parent_fd)
    finally:
        if stage.parent_fd >= 0:
            os.close(stage.parent_fd)
            stage.parent_fd = -1


def create_directory_exclusive(catalog: RootCatalog, root_id: str, destination: str) -> None:
    """Create one empty new directory, or leave nothing."""
    parts = parse_relative_path(destination)
    if not parts:
        raise AddressRejected("invalid")
    _create_directory(catalog, root_id, "/".join(parts[:-1]), parts[-1])


def create_regular(catalog: RootCatalog, root_id: str, relative_directory: str, name: str) -> None:
    """Create one new empty file (HF-NAV-010), or leave nothing."""
    _reject_component(name)
    _record, walked = _open_directory_for_create(catalog, root_id, relative_directory, name)
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            file_fd = os.open(name, flags, NEW_FILE_MODE, dir_fd=walked.fd)
        except FileExistsError as exc:
            raise AddressRejected("conflict") from exc
        except OSError as exc:
            raise AddressRejected("forbidden") from exc
        try:
            os.fsync(file_fd)
            os.fsync(walked.fd)
        except OSError as exc:
            raise AddressRejected("unavailable") from exc
        finally:
            os.close(file_fd)
    finally:
        os.close(walked.fd)


def create_directory(catalog: RootCatalog, root_id: str, relative_directory: str, name: str) -> None:
    """Create one new directory (HF-NAV-010), or leave nothing."""
    _create_directory(catalog, root_id, relative_directory, name)


def _create_directory(catalog: RootCatalog, root_id: str, relative_directory: str, name: str) -> None:
    _reject_component(name)
    _record, walked = _open_directory_for_create(catalog, root_id, relative_directory, name)
    try:
        try:
            os.mkdir(name, NEW_DIRECTORY_MODE, dir_fd=walked.fd)
        except FileExistsError as exc:
            raise AddressRejected("conflict") from exc
        except OSError as exc:
            raise AddressRejected("forbidden") from exc
        try:
            os.fsync(walked.fd)
        except OSError as exc:
            raise AddressRejected("unavailable") from exc
    finally:
        os.close(walked.fd)


def _require_available(catalog: RootCatalog, root_id: str) -> DocumentRoot:
    record = root_record(catalog, root_id)
    if record is None:
        raise AddressRejected("not_found")
    return record


def _judge(walked: Walked, operation: str) -> Inspection:
    info = os.fstat(walked.fd)
    is_directory = stat.S_ISDIR(info.st_mode)
    is_regular = stat.S_ISREG(info.st_mode)
    if operation not in {"read", "mutate", "delete"} or not (is_directory or is_regular):
        return Inspection(False, "forbidden", is_directory=is_directory, is_regular=is_regular)
    return Inspection(True, "ok", is_directory=is_directory, is_regular=is_regular)


def _open_directory_for_create(
    catalog: RootCatalog,
    root_id: str,
    relative_directory: str,
    name: str,
) -> tuple[DocumentRoot, Walked]:
    """Return a readable descriptor of an existing directory for a new child.

    Write permission is not checked here: the kernel decides at the concrete
    operation (HF-NAV-003).
    """
    record = _require_available(catalog, root_id)
    parts = parse_relative_path(relative_directory)
    _reject_component(name)
    walked = _walk(parts)
    try:
        if not stat.S_ISDIR(os.fstat(walked.fd).st_mode):
            raise AddressRejected("forbidden")
        readable = _reopen_directory(walked.fd)
    finally:
        os.close(walked.fd)
    return record, Walked(readable, walked.directory_ids)


def _reopen_directory(path_fd: int) -> int:
    """Reopen an O_PATH directory descriptor for reading and verify identity."""
    try:
        readable = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise AddressRejected("forbidden") from exc
    try:
        pinned = os.fstat(path_fd)
        opened = os.fstat(readable)
    except OSError as exc:
        os.close(readable)
        raise AddressRejected("forbidden") from exc
    if (opened.st_dev, opened.st_ino) != (pinned.st_dev, pinned.st_ino):
        os.close(readable)
        raise AddressRejected("conflict")
    return readable


def _access_at(directory_fd: int, name: str, mode: int) -> bool:
    try:
        return os.access(name, mode, dir_fd=directory_fd, follow_symlinks=False)
    except (OSError, NotImplementedError):
        return False


def _reject_component(name: str) -> None:
    if (
        not _unicode_scalar(name)
        or name in {"", ".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise AddressRejected("invalid")


def rename_exclusive(src_dir: int, src_name: str, dst_dir: int, dst_name: str) -> None:
    """Atomically publish ``src_name`` as ``dst_name`` without replacing an occupant.

    ``renameat2(RENAME_NOREPLACE)`` fails with ``EEXIST`` rather than swapping
    the destination. A cross-filesystem attempt fails with ``EXDEV`` and leaves
    both directories unchanged.
    """
    _rename_noreplace(src_dir, src_name, dst_name, dst_dir=dst_dir)


def _rename_noreplace(
    parent_fd: int,
    source: str,
    destination: str,
    dst_dir: int | None = None,
) -> None:
    """Use Linux renameat2 so publication is atomic and cannot replace a name."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    target_dir = parent_fd if dst_dir is None else dst_dir
    result = renameat2(parent_fd, os.fsencode(source), target_dir, os.fsencode(destination), 1)
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), destination)
    raise OSError(code, os.strerror(code), destination)


def _unicode_scalar(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _has_extended_acl(fd: int) -> bool:
    try:
        os.getxattr(f"/proc/self/fd/{fd}", "system.posix_acl_access")
    except OSError as exc:
        if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOENT}:
            return False
        return True
    return True


def _remove_new_directory(
    name: str,
    parent: int,
    identity: tuple[int, int] | None = None,
) -> None:
    try:
        if identity is not None:
            details = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(details.st_mode) or (details.st_dev, details.st_ino) != identity:
                return
        os.rmdir(name, dir_fd=parent)
    except OSError:
        pass


def _unlink_if_regular(name: str, parent: int, *, identity: tuple[int, int] | None = None) -> None:
    """Remove an operation-owned regular entry, never an exchanged object."""
    try:
        details = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode):
        return
    if identity is not None and (details.st_dev, details.st_ino) != identity:
        return
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        return


def _walk(parts: tuple[str, ...]) -> Walked:
    """Open the addressed object from ``/`` by descriptor. Symlinks fail closed.

    Every descriptor is ``O_PATH``: traversing needs only search permission on
    each parent, and reading requires a separate reopen that the kernel checks.
    """
    flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        current = os.open("/", flags | os.O_DIRECTORY)
    except OSError as exc:
        raise AddressRejected("unavailable") from exc
    directory_ids: list[tuple[int, int]] = []
    try:
        info = os.fstat(current)
        directory_ids.append((info.st_dev, info.st_ino))
        for index, name in enumerate(parts):
            try:
                nxt = os.open(name, flags, dir_fd=current)
            except OSError as exc:
                raise AddressRejected("forbidden") from exc
            try:
                nxt_info = os.fstat(nxt)
            except OSError as exc:
                os.close(nxt)
                raise AddressRejected("forbidden") from exc
            if stat.S_ISLNK(nxt_info.st_mode):
                os.close(nxt)
                raise AddressRejected("forbidden")
            if index < len(parts) - 1 and not stat.S_ISDIR(nxt_info.st_mode):
                os.close(nxt)
                raise AddressRejected("forbidden")
            os.close(current)
            current = nxt
            if stat.S_ISDIR(nxt_info.st_mode):
                directory_ids.append((nxt_info.st_dev, nxt_info.st_ino))
        return Walked(current, tuple(directory_ids))
    except BaseException:
        os.close(current)
        raise


def _pseudo_devices() -> frozenset[int]:
    devices: set[int] = set()
    for path in ("/proc", "/sys", "/dev", "/dev/pts", "/dev/shm", "/dev/mqueue", "/sys/fs/cgroup"):
        try:
            devices.add(os.stat(path).st_dev)
        except OSError:
            continue
    return frozenset(devices)


def _is_special(mode: int) -> bool:
    return not stat.S_ISREG(mode) and not stat.S_ISDIR(mode)
