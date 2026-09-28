"""Derived per-note index for the corpus traversals of HF-NAV-011.

The tag index (HF-META-002) reads every note under the monitored folders, and
attachment-reference scans (HF-IMG-004) every note of the corpus. Reading and
parsing all notes on each request does not fit the 10-second budget of
HF-LIMIT-001 once the corpus is every writable directory of the account. This
module keeps, in the instance's private state, what was derived from each note:
its tags and its raw Markdown image destinations, or the reason they could not
be derived. An entry is reused only while the note's identity and version
tuple (device, inode, size, modification and change times) is unchanged; any
write changes the change time, so the note is read again. The index is derived
state: it is never authority over a file, it is discarded by the HF-NAV-012
migration, and it is rebuilt on demand.
"""

from __future__ import annotations

import fcntl
import html
import json
import os
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from hopper_files.corpus import (
    CorpusScan,
    effective_skips,
    iter_corpus_notes,
    iter_tag_notes,
    start_scan,
    under_skip,
)
from hopper_files.roots import BASE_ID, AddressRejected, RootCatalog, SearchEntry, open_scanned_regular
from hopper_files.state import StateError, atomic_write

INDEX_VERSION = 1
INDEX_NAME = "notes.json"
LOCK_NAME = "notes.lock"
TAG_TEXT_LIMIT = 5 * 1024 * 1024


@dataclass
class NoteRecord:
    version: list[int]
    tags: list[str] | None
    tag_error: str | None
    destinations: list[str] | None
    reference_error: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "tags": self.tags,
            "tagError": self.tag_error,
            "destinations": self.destinations,
            "referenceError": self.reference_error,
        }


@dataclass
class CorpusView:
    """Notes observed by one traversal and the traversal's completeness."""

    notes: dict[str, NoteRecord] = field(default_factory=dict)
    scan: CorpusScan | None = None


def observe_corpus(catalog: RootCatalog, *, skip_paths: frozenset[str] = frozenset()) -> CorpusView:
    """Traverse the corpus and return current derived records for every note.

    ``skip_paths`` leaves directories out of this traversal. Their cached
    records stay for the other readers of the index, which still see them, and
    so do the records of notes that still exist in read-only directories, which
    only the tag index reads.
    """
    view = CorpusView(scan=start_scan())
    skips = effective_skips(skip_paths)
    read_only: set[str] = set()
    notes = iter_corpus_notes(catalog, view.scan, skip_paths=skip_paths, read_only_notes=read_only)
    return _observe(
        catalog, view, notes, lambda path: not under_skip(path, skips) and path not in read_only, unreadable_is_exclusion=False
    )


def observe_tag_notes(catalog: RootCatalog, folders: Iterable[str]) -> CorpusView:
    """Traverse the monitored folders of HF-META-002 and return their notes' records.

    ``folders`` are relative to ``/``. Only a record whose directory this
    traversal listed can be found stale here; the rest of the index belongs to
    the corpus traversals and is kept. A note the account cannot read is an
    exclusion, ``unreadable_file``, not a failure of the index.
    """
    view = CorpusView(scan=start_scan())
    visited: set[str] = set()
    notes = iter_tag_notes(catalog, view.scan, folders, visited)
    return _observe(catalog, view, notes, lambda path: path.rpartition("/")[0] in visited, unreadable_is_exclusion=True)


def _observe(
    catalog: RootCatalog,
    view: CorpusView,
    notes: Iterator[tuple[str, int, SearchEntry]],
    prunable: Callable[[str], bool],
    *,
    unreadable_is_exclusion: bool,
) -> CorpusView:
    from hopper_files.image_refs import ReferenceScanError, _MarkdownParser, _read_stable
    from hopper_files.search import extract_markdown_tags

    directory = _index_directory(catalog.state_directory)
    with _lock(directory):
        cached = _load(directory)
        changed = False
        for path, parent_fd, entry in notes:
            if time.monotonic() >= view.scan.deadline:
                # HF-LIMIT-001: stop reading at the budget; what was derived so
                # far is kept, so a later request continues from it.
                view.scan.add("execution_budget")
                break
            version = list(entry.version)
            previous = cached.get(path)
            if previous is not None and previous.version == version:
                view.notes[path] = previous
                continue
            try:
                fd = open_scanned_regular(parent_fd, entry)
            except AddressRejected as exc:
                if exc.code == "conflict":
                    view.scan.add("entry_race")
                elif unreadable_is_exclusion and isinstance(exc.__cause__, PermissionError):
                    view.scan.add("unreadable_file")
                else:
                    view.scan.add("read_error")
                continue
            try:
                parser = _MarkdownParser(catalog, BASE_ID, path)
                keep = entry.size is not None and entry.size <= TAG_TEXT_LIMIT
                try:
                    data, _digest = _read_stable(fd, os.fstat(fd), parser.feed_line, keep_content=keep)
                except ReferenceScanError as exc:
                    if "changed while scanning" in str(exc):
                        view.scan.add("entry_race")
                        continue
                    record = NoteRecord(version, None, "invalid_markdown", None, str(exc))
                else:
                    try:
                        destinations: list[str] | None = parser.destinations()
                        reference_error = None
                    except ReferenceScanError as exc:
                        destinations, reference_error = None, str(exc)
                    if not keep:
                        tags, tag_error = None, "tag_file_limit"
                    elif b"\x00" in data:
                        tags, tag_error = None, "invalid_markdown"
                    else:
                        tags = sorted(extract_markdown_tags(data.decode("utf-8")), key=lambda value: value.encode("utf-8"))
                        tag_error = None
                    record = NoteRecord(version, tags, tag_error, destinations, reference_error)
            except OSError:
                view.scan.add("read_error")
                continue
            finally:
                os.close(fd)
            view.notes[path] = record
            cached[path] = record
            changed = True
        if view.scan.complete:
            stale = {path for path in set(cached) - set(view.notes) if prunable(path)}
            for path in stale:
                cached.pop(path)
            changed = changed or bool(stale)
        if changed:
            _save(directory, cached)
    return view


def mentions_any(catalog: RootCatalog, note_path: str, names: set[str]) -> bool:
    """Whether a note that could not be parsed might still name one of ``names``.

    A Markdown reference to a file contains its final name, possibly percent-,
    entity-, or backslash-escaped. A note whose normalized text contains none of
    the names cannot reference those files, so its parse failure does not make
    their scan inconclusive. Any read failure answers True.
    """
    from hopper_files.roots import open_regular

    try:
        with open_regular(catalog, BASE_ID, note_path) as (fd, _info):
            chunks = []
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                chunks.append(block)
    except (AddressRejected, OSError):
        return True
    text = b"".join(chunks).decode("utf-8", errors="replace")
    forms = {text, html.unescape(text)}
    forms |= {unquote(value) for value in forms}
    forms |= {value.replace("\\", "") for value in forms}
    forms = {unicodedata.normalize("NFC", value) for value in forms}
    wanted = {unicodedata.normalize("NFC", name) for name in names if name}
    return any(name in value for value in forms for name in wanted)


def _index_directory(state_directory: Path) -> Path:
    directory = state_directory / "indexes"
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
    except OSError as exc:
        raise StateError("note index directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise StateError("note index directory is invalid")
    return directory


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load(directory: Path) -> dict[str, NoteRecord]:
    """Read the index; anything unexpected is discarded and rebuilt."""
    try:
        payload = json.loads((directory / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION or not isinstance(payload.get("notes"), dict):
        return {}
    notes: dict[str, NoteRecord] = {}
    for path, value in payload["notes"].items():
        try:
            record = NoteRecord(
                list(value["version"]),
                value["tags"],
                value["tagError"],
                value["destinations"],
                value["referenceError"],
            )
        except (KeyError, TypeError):
            return {}
        if len(record.version) != 7 or not all(isinstance(item, int) for item in record.version):
            return {}
        notes[path] = record
    return notes


def _save(directory: Path, notes: dict[str, NoteRecord]) -> None:
    payload = {"version": INDEX_VERSION, "notes": {path: record.to_json() for path, record in sorted(notes.items())}}
    atomic_write(directory / INDEX_NAME, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
