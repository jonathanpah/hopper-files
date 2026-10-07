"""The note corpus and the monitored folders of the tag index.

HF-NAV-004 defines the corpus as the regular ``.md`` files whose parent
directory is writable by the service account, discovered from ``/`` by the
bounded traversal of HF-NAV-011. Note discovery and attachment-reference scans
use this iterator. The tag index reads only the folders the account marks as
monitored (HF-META-002) through :func:`iter_tag_notes`. Neither enters the
instance's internal trash.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from hopper_files.roots import BASE_ID, AddressRejected, RootCatalog, SearchEntry, scan_tree

CORPUS_TIME_LIMIT_SECONDS = 10.0
# The corpus is discovered from / (HF-NAV-004). Tests confine this origin to a
# synthetic directory through the harness so they never read real notes.
CORPUS_START = ""
# Below a monitored folder the tag index leaves temporary directories out (HF-META-002): they
# can be large and rarely hold notes whose tags matter. Like hidden directories, they enter only
# when marked.
TAG_EXCLUDED_PATHS = frozenset({"tmp", "var/tmp"})
# Traversal counts that make a corpus result incomplete (HF-NAV-011).
INCOMPLETE_CATEGORIES = frozenset({"execution_budget", "entry_race", "depth_limit", "read_error"})


@dataclass
class CorpusScan:
    """Counts shared by one corpus traversal and its consumer."""

    deadline: float
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, category: str, count: int = 1) -> None:
        self.counts[category] = self.counts.get(category, 0) + count

    @property
    def complete(self) -> bool:
        return not any(self.counts.get(category) for category in INCOMPLETE_CATEGORIES)

    def omissions(self) -> dict[str, int]:
        return {key: value for key, value in self.counts.items() if key in INCOMPLETE_CATEGORIES and value}

    def exclusions(self) -> dict[str, int]:
        return {key: value for key, value in self.counts.items() if key not in INCOMPLETE_CATEGORIES and value}


def start_scan(limit: float | None = None) -> CorpusScan:
    return CorpusScan(time.monotonic() + (CORPUS_TIME_LIMIT_SECONDS if limit is None else limit))


def effective_skips(skip_paths: frozenset[str]) -> frozenset[str]:
    """The skipped directories that lie below the corpus origin.

    A skip above or beside the origin cannot be reached by the traversal, so it
    neither prunes it nor protects cached records from being found stale.
    """
    start = CORPUS_START
    return frozenset(path for path in skip_paths if not start or path.startswith(start + "/"))


def under_skip(path: str, skips: frozenset[str]) -> bool:
    return any(path == skip or path.startswith(skip + "/") for skip in skips)


def iter_corpus_notes(
    catalog: RootCatalog,
    scan: CorpusScan,
    *,
    skip_paths: frozenset[str] = frozenset(),
    read_only_notes: set[str] | None = None,
) -> Iterator[tuple[str, int, SearchEntry]]:
    """Yield ``(path relative to /, parent descriptor, entry)`` for each note.

    The parent descriptor is valid only until the iterator resumes. A directory
    that changed while it was listed is counted as ``entry_race``. Directories
    in ``skip_paths`` are not entered. ``read_only_notes`` receives the ``.md``
    files listed in directories the account cannot write: they are outside the
    corpus but may be read by the tag index.
    """
    for directory in scan_tree(
        catalog,
        BASE_ID,
        CORPUS_START,
        deadline=scan.deadline,
        skip_internal_trash=True,
        counts=scan.counts,
        skip_paths=effective_skips(skip_paths),
    ):
        if not directory.stable:
            scan.add("entry_race")
        for category, count in directory.exclusions.items():
            scan.add(category, count)
        for entry in directory.entries:
            if entry.kind != "file" or not is_note_name(entry.name):
                continue
            path = f"{directory.path}/{entry.name}" if directory.path else entry.name
            if directory.writable:
                yield path, directory.fd, entry
            elif read_only_notes is not None:
                read_only_notes.add(path)


def is_note_name(name: str) -> bool:
    return name.casefold().endswith(".md")


def tag_starts(folders: Iterable[str]) -> tuple[str, ...]:
    """Monitored folders, relative to ``/``, without repeats and shallowest first.

    An ancestor is traversed before the folders below it, so a folder that its
    traversal already listed can be recognized and is not read twice.
    """
    return tuple(sorted(set(folders), key=lambda path: (path.count("/") + bool(path), path.encode("utf-8"))))


def tag_skips(start: str) -> frozenset[str]:
    """Temporary directories strictly below ``start``; one at or above it was marked."""
    return frozenset(path for path in TAG_EXCLUDED_PATHS if path != start and (not start or path.startswith(start + "/")))


def iter_tag_notes(
    catalog: RootCatalog,
    scan: CorpusScan,
    folders: Iterable[str],
    visited: set[str],
) -> Iterator[tuple[str, int, SearchEntry]]:
    """Yield ``(path relative to /, parent descriptor, entry)`` for each tagged-note candidate.

    Every ``.md`` file under a monitored folder is yielded, hidden or not, and a
    note in a read-only directory too: the account chose the folder. Hidden
    subdirectories are counted as ``hidden_directory`` and temporary ones as
    ``excluded_path``. ``visited`` receives every directory listed; a monitored
    folder already in it was read by an ancestor's traversal and is skipped.
    Any other monitored folder gets its own traversal, including one below a
    directory the account can pass through but not list. A monitored folder
    that is gone, is not a directory, or cannot be opened is counted as
    ``unavailable_folder``. A folder inside the instance's internal trash is not
    read, so deleted notes never return to the index.
    """
    trash = os.path.realpath(catalog.trash_directory)
    for start in tag_starts(folders):
        if time.monotonic() >= scan.deadline:
            scan.add("execution_budget")
            return
        if start in visited:
            continue
        if f"/{start}" == trash or f"/{start}".startswith(trash + "/"):
            scan.add("internal_trash")
            continue
        directories = scan_tree(
            catalog,
            BASE_ID,
            start,
            deadline=scan.deadline,
            skip_internal_trash=True,
            counts=scan.counts,
            skip_paths=tag_skips(start),
            skip_hidden=True,
        )
        try:
            for directory in directories:
                visited.add(directory.path)
                if not directory.stable:
                    scan.add("entry_race")
                for category, count in directory.exclusions.items():
                    scan.add(category, count)
                for entry in directory.entries:
                    if entry.kind != "file" or not is_note_name(entry.name):
                        continue
                    path = f"{directory.path}/{entry.name}" if directory.path else entry.name
                    yield path, directory.fd, entry
        except AddressRejected as exc:
            if exc.code == "unavailable":
                raise
            scan.add("unavailable_folder")
