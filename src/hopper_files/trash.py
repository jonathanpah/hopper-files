"""Per-instance trash, durable delete, and durable restore.

Publication order is the product contract. A same-filesystem delete renames the
inode into ``STATE_DIR/trash/<uuid>/payload`` and keeps its metadata. A
cross-filesystem delete copies into an exclusive stage, verifies bytes and the
supported access metadata, syncs, and only then publishes the trash directory.
The source is removed only after that publication. Restore runs the reverse
order and publishes active UI marks before removing the trash copy.

A crash may leave two copies. Recovery never deletes both. Before a restore
rename, the journal records the kernel file handle of the exact payload or
destination stage. Recovery adopts an unrecorded publication only when the
live destination has that same file handle and content digest; device/inode
values alone are not identity. Any other occupant keeps the journal and both
copies. Invalid metadata is quarantined: it is not restored and it is not
purged. Purge is an explicit local task and uses the server clock against the
protected deletion time.
"""

from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

import fcntl

from hopper_files.roots import (
    BASE_ID,
    AddressRejected,
    RootCatalog,
    availability,
    device_is_pseudo,
    open_delete_source,
    open_existing_directory,
    open_restore_parent,
    rename_exclusive,
    root_record,
)
from hopper_files.image_refs import ImageIdentity, ReferenceScanError, parse_markdown_stream
from hopper_files.state import (
    LABELS,
    StateError,
    atomic_write,
    commit_ui_state_locked,
    load_ui_state_locked,
    read_signing_secret,
    ui_state_lock,
)

RETENTION_SECONDS = 30 * 24 * 60 * 60
_UUID = uuid.UUID
_SUPPORTED_XATTRS = frozenset({"system.posix_acl_access", "system.posix_acl_default"})
META_VERSION = 2
# Version 1 metadata recorded sourceRootId. Only the HF-NAV-012 migration reads
# it; this release keeps such an entry quarantined.
LEGACY_META_VERSION = 1
_META_FIELDS = frozenset(
    {"version", "id", "sourcePath", "deletedAt", "kind", "size", "reason", "uiMetadata"}
)
_LEGACY_META_FIELDS = frozenset(
    {"version", "id", "sourceRootId", "sourcePath", "deletedAt", "kind", "size", "reason", "uiMetadata"}
)
_MARK_FIELDS = frozenset({"relativePath", "labelIds", "favorite", "emoji"})
_ROOT_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_SPACE_MARGIN = 65536


class TrashCrash(BaseException):
    """Test seam that stops an operation the way a killed process would."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point


class TrashError(ValueError):
    """A known trash outcome. ``details`` are safe to return to the caller."""

    def __init__(self, code: str, **details: object) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


class TrashStore:
    """Durable trash for one bound instance. Callers supply the server clock."""

    def __init__(self, state_directory: Path, instance_id: str) -> None:
        self.state_directory = Path(state_directory).resolve()
        self.instance_id = instance_id
        self.fail_at: set[str] = set()

    def delete(
        self,
        catalog: RootCatalog,
        root_id: str,
        relative_path: str,
        now: float,
        *,
        reason: str = "user",
        identifier: str | None = None,
    ) -> dict[str, object]:
        with self._session():
            self._recover_locked(catalog, now)
            pending = self._pending_ids()
            try:
                return self._delete_locked(catalog, root_id, relative_path, now, reason=reason, identifier=identifier)
            except PermissionError as exc:
                raise self._permission_error(pending) from exc

    def restore(
        self,
        catalog: RootCatalog,
        identifier: str,
        now: float,
        alternative_path: str | None = None,
        destination_root_id: str | None = None,
        on_intent: Callable[[dict[str, object]], None] | None = None,
        on_publication: Callable[[dict[str, object]], None] | None = None,
        tree_file_paths: set[str] | None = None,
    ) -> dict[str, object]:
        with self._session():
            self._recover_locked(catalog, now)
            pending = self._pending_ids()
            try:
                return self._restore_locked(
                    catalog, identifier, now, alternative_path, destination_root_id, on_intent, on_publication,
                    tree_file_paths,
                )
            except PermissionError as exc:
                raise self._permission_error(pending) from exc

    def _permission_error(self, pending_before: set[str]) -> TrashError:
        """HF-NAV-003: Linux permission refused the action.

        An unpublished attempt was already discarded and reports ``forbidden``.
        A journal left by this attempt means publication may have happened;
        that stays ``indeterminate`` for recovery.
        """
        remaining = self._pending_ids() - pending_before
        if remaining:
            return TrashError("indeterminate", id=sorted(remaining)[0])
        return TrashError("forbidden")

    def related_restore_images(self, catalog: RootCatalog, identifier: str, now: float) -> dict[str, object]:
        """Find collected images referenced by a trashed Markdown note."""
        with self._session():
            self._recover_locked(catalog, now)
            return self._related_restore_images_locked(catalog, identifier)

    def restore_with_images(
        self,
        catalog: RootCatalog,
        identifier: str,
        image_identifiers: list[str],
        now: float,
    ) -> dict[str, object]:
        """Restore a Markdown note and selected collected images without overwrite.

        All destinations are checked before the first publication. If an
        external writer wins a race after preflight, the response lists every
        item already restored and leaves the remaining trash entries intact.
        """
        with self._session():
            self._recover_locked(catalog, now)
            if not isinstance(image_identifiers, list) or len(image_identifiers) > 1000:
                raise TrashError("invalid")
            if any(not isinstance(value, str) or not _is_uuid(value) for value in image_identifiers):
                raise TrashError("invalid")
            if len(set(image_identifiers)) != len(image_identifiers):
                raise TrashError("invalid")
            related = self._related_restore_images_locked(catalog, identifier)
            if not related.get("complete"):
                raise TrashError("inconclusive", id=identifier)
            allowed: dict[str, tuple[str, str]] = {}
            for group in related.get("images", []):
                if not isinstance(group, dict) or not isinstance(group.get("candidates"), list):
                    continue
                root_id, path = group.get("rootId"), group.get("path")
                if not isinstance(root_id, str) or not isinstance(path, str):
                    continue
                for candidate in group["candidates"]:
                    if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                        allowed[candidate["id"]] = (root_id, path)
            if any(value not in allowed for value in image_identifiers):
                raise TrashError("invalid")
            addresses = [allowed[value] for value in image_identifiers]
            if len(set(addresses)) != len(addresses):
                raise TrashError("ambiguous_image", id=identifier)

            identifiers = [identifier, *image_identifiers]
            for restore_id in identifiers:
                self._preflight_restore_locked(catalog, restore_id)

            restored: list[dict[str, object]] = []
            for restore_id in identifiers:
                try:
                    restored.append(self._restore_locked(catalog, restore_id, now, None))
                except TrashCrash:
                    raise
                except Exception as exc:
                    if not restored:
                        raise
                    raise TrashError(
                        "partial_restore",
                        restored=restored,
                        failedId=restore_id,
                        remainingIds=identifiers[len(restored):],
                    ) from exc
            return {
                "restored": restored,
                "relatedImageIds": image_identifiers,
                "stateRevision": restored[-1]["stateRevision"],
            }

    def _preflight_restore_locked(self, catalog: RootCatalog, identifier: str) -> None:
        evaluated = self._evaluate(identifier)
        if evaluated is None:
            raise TrashError("not_found", id=identifier)
        if evaluated["quarantined"]:
            raise TrashError("quarantined", id=identifier)
        meta = evaluated["meta"]
        assert isinstance(meta, dict)
        root_id = str(meta["sourceRootId"])
        if availability(catalog, root_id) != "available":
            raise TrashError("source_unavailable", id=identifier)
        _authorize_delete(catalog, root_id)
        path = str(meta["sourcePath"])
        document = load_ui_state_locked(self.state_directory)
        if _metadata_conflict(document, root_id, path, str(meta["kind"]), meta["uiMetadata"]):
            raise TrashError("metadata_conflict", id=identifier, sourceRootId=root_id, sourcePath=path)
        opened = None
        try:
            try:
                opened = open_restore_parent(catalog, root_id, path)
            except AddressRejected as exc:
                raise _from_address(exc, restore=True) from exc
        finally:
            if opened is not None:
                opened.close()

    def _related_restore_images_locked(self, catalog: RootCatalog, identifier: str) -> dict[str, object]:
        if not _is_uuid(identifier):
            raise TrashError("invalid")
        evaluated = self._evaluate(identifier)
        if evaluated is None:
            raise TrashError("not_found")
        if evaluated["quarantined"]:
            raise TrashError("quarantined", id=identifier)
        meta = evaluated["meta"]
        assert isinstance(meta, dict)
        source_root = str(meta["sourceRootId"])
        source_path = str(meta["sourcePath"])
        if meta["kind"] != "file" or not source_path.lower().endswith((".md", ".markdown")):
            return {"complete": True, "images": []}
        try:
            entry = _open_directory(self._trash_path / identifier)
            try:
                payload = os.open("payload", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=entry)
                try:
                    references = parse_markdown_stream(catalog, source_root, source_path, payload)
                finally:
                    os.close(payload)
            finally:
                os.close(entry)
        except (OSError, TrashError, ReferenceScanError, AddressRejected):
            return {"complete": False, "images": []}

        by_identity: dict[ImageIdentity, list[dict[str, object]]] = {}
        trash_fd = _open_directory(self._trash_path)
        try:
            names = sorted(name for name in os.listdir(trash_fd) if _is_uuid(name) and name != identifier)
        finally:
            os.close(trash_fd)
        for name in names:
            candidate = self._evaluate(name)
            if candidate is None or candidate["quarantined"]:
                continue
            candidate_meta = candidate["meta"]
            assert isinstance(candidate_meta, dict)
            if candidate_meta.get("reason") not in {"user", "image_collection"} or candidate_meta.get("kind") != "file":
                continue
            if not str(candidate_meta["sourcePath"]).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                continue
            identity = ImageIdentity(str(candidate_meta["sourceRootId"]), str(candidate_meta["sourcePath"]))
            if identity not in references:
                continue
            by_identity.setdefault(identity, []).append({
                "id": name,
                "deletedAt": candidate_meta["deletedAt"],
                "size": candidate_meta["size"],
            })
        images = [
            {"rootId": identity.root_id, "path": identity.path, "candidates": sorted(items, key=lambda item: str(item["deletedAt"]))}
            for identity, items in sorted(by_identity.items())
        ]
        return {"complete": True, "images": images}

    def move(
        self,
        catalog: RootCatalog,
        source_root_id: str,
        source_path: str,
        destination_root_id: str,
        destination_path: str,
        now: float,
    ) -> dict[str, object]:
        """Move through the recoverable trash journal, retaining source identity
        on the same filesystem and preserving supported metadata across roots.

        If destination publication cannot complete, the payload remains a
        visible recoverable trash entry and the exception carries its ID.
        """
        with self._session():
            self._recover_locked(catalog, now)
            deleted = self._delete_locked(catalog, source_root_id, source_path, now, reason="move")
            return self._restore_locked(
                catalog, str(deleted["id"]), now, destination_path, destination_root_id
            )

    def preflight_move(
        self,
        catalog: RootCatalog,
        source_root_id: str,
        source_path: str,
        destination_root_id: str,
        destination_path: str,
        now: float,
    ) -> dict[str, object]:
        """Validate both filesystem legs of a move before source publication."""
        with self._session():
            self._recover_locked(catalog, now)
            _authorize_delete(catalog, source_root_id)
            _authorize_delete(catalog, destination_root_id)
            source = None
            destination = None
            try:
                try:
                    source = open_delete_source(catalog, source_root_id, source_path)
                    destination = open_restore_parent(catalog, destination_root_id, destination_path)
                except AddressRejected as exc:
                    raise _from_address(exc, restore=destination is not None) from exc
                node = _capture(source.parent_fd, source.name, "", source_path)
                if (node["device"], node["inode"]) != (source.device, source.inode):
                    raise TrashError("conflict")
                _require_tree_allowed(catalog, source_root_id, node)
                # HF-FILE-002: only a move between filesystems copies; the
                # filesystem of the state directory does not matter.
                crosses_filesystem = node["device"] != destination.parent_device
                if crosses_filesystem and _cross_unsupported(node):
                    raise TrashError("unsupported_metadata")
                return {
                    "kind": node["kind"],
                    "size": _byte_size(node),
                    "identity": [node["device"], node["inode"]],
                    "digest": _content_digest(source.parent_fd, source.name),
                    "crossFilesystem": crosses_filesystem,
                    "linkedFiles": _linked_files(node),
                }
            finally:
                if source is not None:
                    source.close()
                if destination is not None:
                    destination.close()

    def move_in_place(
        self,
        catalog: RootCatalog,
        source_root_id: str,
        source_path: str,
        destination_root_id: str,
        destination_path: str,
        now: float,
        *,
        identifier: str,
        on_intent: Callable[[dict[str, object]], None] | None = None,
        on_publication: Callable[[dict[str, object]], None] | None = None,
        tree_file_paths: set[str] | None = None,
    ) -> dict[str, object] | None:
        """Publish a same-filesystem move by one exclusive rename (HF-FILE-002).

        The inode, its other links, and its metadata stay as they are, and the
        marks follow the object. Returns None with nothing changed when the two
        places cannot be joined by a rename, such as separate mounts; the caller
        then moves through the trash.
        """
        with self._session():
            self._recover_locked(catalog, now)
            pending = self._pending_ids()
            try:
                return self._move_in_place_locked(
                    catalog, source_root_id, source_path, destination_root_id, destination_path,
                    identifier, on_intent, on_publication, tree_file_paths,
                )
            except PermissionError as exc:
                raise self._permission_error(pending) from exc

    def _move_in_place_locked(
        self,
        catalog: RootCatalog,
        source_root_id: str,
        source_path: str,
        destination_root_id: str,
        destination_path: str,
        identifier: str,
        on_intent: Callable[[dict[str, object]], None] | None,
        on_publication: Callable[[dict[str, object]], None] | None,
        tree_file_paths: set[str] | None,
    ) -> dict[str, object] | None:
        _authorize_delete(catalog, source_root_id)
        _authorize_delete(catalog, destination_root_id)
        if not _is_uuid(identifier):
            raise TrashError("invalid")
        source = None
        destination = None
        journaled = False
        published = False
        try:
            try:
                source = open_delete_source(catalog, source_root_id, source_path)
                destination = open_restore_parent(catalog, destination_root_id, destination_path)
            except AddressRejected as exc:
                raise _from_address(exc, restore=destination is not None) from exc
            if source.device != destination.parent_device:
                return None
            node = _capture(source.parent_fd, source.name, "", source_path)
            if (node["device"], node["inode"]) != (source.device, source.inode):
                raise TrashError("conflict")
            _require_tree_allowed(catalog, source_root_id, node)
            kind = "directory" if node["kind"] == "directory" else "file"
            document = load_ui_state_locked(self.state_directory)
            marks = _collect_marks(document, source_root_id, source_path, kind)
            remaining = copy.deepcopy(document)
            _strip_items(remaining, source_root_id, source_path, kind)
            if _metadata_conflict(remaining, destination_root_id, destination_path, kind, marks):
                raise TrashError("metadata_conflict", id=identifier)
            handle = _file_handle_at(source.parent_fd, source.name)
            tree_handles = (
                _tree_file_handles_at(source.parent_fd, source.name, node, tree_file_paths)
                if tree_file_paths
                else {}
            )
            journal: dict[str, object] = {
                "version": 1,
                "instanceId": self.instance_id,
                "operation": "move",
                "id": identifier,
                "phase": "intent",
                "sourceRootId": source_root_id,
                "sourcePath": source_path,
                "destinationRootId": destination_root_id,
                "destinationPath": destination_path,
                "kind": kind,
                "uiMetadata": marks,
                "topIdentity": [node["device"], node["inode"]],
                "restoreHandle": handle,
                "publishedIdentity": None,
                "publishedHandle": None,
            }
            self._save_journal(journal)
            journaled = True
            intent = {
                "rootId": destination_root_id,
                "path": destination_path,
                "handle": handle,
                "identity": [node["device"], node["inode"]],
                "treeFileHandles": tree_handles,
            }
            if on_intent is not None:
                on_intent(intent)
            try:
                rename_exclusive(source.parent_fd, source.name, destination.parent_fd, destination.name)
            except FileExistsError as exc:
                raise TrashError("conflict") from exc
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    self._remove_journal(identifier)
                    journaled = False
                    return None
                if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise TrashError("limit") from exc
                raise
            published = True
            self._crash("move_after_rename")
            os.fsync(destination.parent_fd)
            os.fsync(source.parent_fd)
            info = os.lstat(destination.name, dir_fd=destination.parent_fd)
            published_handle = _file_handle_at(destination.parent_fd, destination.name)
            self._record_restore_publication(journal, (info.st_dev, info.st_ino), published_handle)
            if on_publication is not None:
                on_publication({**intent, "identity": [info.st_dev, info.st_ino], "handle": published_handle})
            self._crash("move_after_publication")
            document = self._commit_move_marks(document, journal, catalog)
            journal["phase"] = "ui_committed"
            self._save_journal(journal)
            self._remove_journal(identifier)
            return {
                "id": identifier,
                "rootId": destination_root_id,
                "path": destination_path,
                "separatedLinks": 0,
                "stateRevision": document["stateRevision"],
            }
        except TrashCrash:
            raise
        except Exception:
            # A rename either happened or not; before it, only the journal exists.
            if journaled and not published:
                self._remove_journal(identifier)
            raise
        finally:
            if source is not None:
                source.close()
            if destination is not None:
                destination.close()

    def _commit_move_marks(
        self,
        document: dict[str, object],
        journal: dict[str, object],
        catalog: RootCatalog,
    ) -> dict[str, object]:
        """Transfer the marks of a renamed object in one UI-state commit."""
        root_id = str(journal["destinationRootId"])
        destination = str(journal["destinationPath"])
        draft = copy.deepcopy(document)
        _strip_items(draft, str(journal["sourceRootId"]), str(journal["sourcePath"]), str(journal["kind"]))
        records = journal.get("uiMetadata")
        if isinstance(records, list) and not _marks_match(draft, root_id, destination, records):
            if _metadata_conflict(draft, root_id, destination, str(journal["kind"]), records):
                raise TrashError("indeterminate", id=journal["id"])
            _apply_marks(draft, root_id, destination, records, _destination_hints(catalog, root_id, destination))
        if draft == document:
            return document
        return commit_ui_state_locked(self.state_directory, document, draft)

    def _recover_move(self, catalog: RootCatalog, journal: dict[str, object]) -> None:
        """Finish or drop an interrupted in-place move.

        The rename is atomic: if the journaled object is not at the destination,
        it was not moved there and only the journal remains to remove.
        """
        identifier = str(journal["id"])
        root_id = str(journal.get("destinationRootId") or "")
        destination = str(journal.get("destinationPath") or "")
        if not _identity_pair(journal.get("publishedIdentity")):
            observed = _lookup_restore_destination(catalog, root_id, destination)
            if (
                observed is None
                or observed.get("conflict") is True
                or not _same_file_handle(observed.get("handle"), journal.get("restoreHandle"))
            ):
                self._remove_journal(identifier)
                return
            self._record_restore_publication(journal, observed["identity"], observed["handle"])
        observed = _lookup_restore_destination(catalog, root_id, destination)
        if (
            observed is None
            or observed.get("conflict") is True
            or not _same_file_handle(observed.get("handle"), journal.get("publishedHandle"))
        ):
            return
        document = load_ui_state_locked(self.state_directory)
        self._commit_move_marks(document, journal, catalog)
        journal["phase"] = "ui_committed"
        self._save_journal(journal)
        self._remove_journal(identifier)

    def verify_move_target(
        self,
        catalog: RootCatalog,
        root_id: str,
        path: str,
        *,
        handle: object,
        digest: object | None = None,
    ) -> dict[str, object] | None:
        """Return observed identity only when a published move matches its handle."""
        opened = None
        try:
            try:
                opened = open_delete_source(catalog, root_id, path)
            except AddressRejected:
                return None
            actual_handle = _file_handle_at(opened.parent_fd, opened.name)
            if not _same_file_handle(actual_handle, handle):
                return None
            actual_digest = _content_digest(opened.parent_fd, opened.name)
            if isinstance(digest, str) and not hmac.compare_digest(actual_digest, digest):
                return None
            info = os.stat(opened.name, dir_fd=opened.parent_fd, follow_symlinks=False)
            return {
                "identity": [info.st_dev, info.st_ino],
                "handle": actual_handle,
                "digest": actual_digest,
                "kind": _kind_of(info),
            }
        except (OSError, TrashError):
            return None
        finally:
            if opened is not None:
                opened.close()

    def list_entries(self, catalog: RootCatalog, now: float) -> list[dict[str, object]]:
        with self._session():
            self._recover_locked(catalog, now)
            return self._list_locked(catalog)

    def recover(self, catalog: RootCatalog, now: float) -> None:
        with self._session():
            self._recover_locked(catalog, now)

    def purge(self, now: float) -> int:
        """Permanently remove valid entries whose protected age is at least 30 days.

        Quarantine, open journals, and younger entries are left in place. This
        is the only permanent-deletion path; there is no early-delete request.
        """
        with self._lock():
            return self._purge_locked(now)

    @contextmanager
    def _session(self) -> Iterator[None]:
        with self._lock():
            with ui_state_lock(self.state_directory):
                yield

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._prepare()
        fd = os.open(self._lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise TrashError("unavailable")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _prepare(self) -> None:
        for path in (self._trash_path, self._staging_path, self._journal_directory):
            _private_dir(path)
        if not self._lock_path.exists():
            try:
                fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
            except FileExistsError:
                # A concurrent request created it first; _lock opens and checks it.
                pass
            else:
                os.close(fd)

    def _crash(self, point: str) -> None:
        if point in self.fail_at:
            raise TrashCrash(point)

    def _delete_locked(
        self,
        catalog: RootCatalog,
        root_id: str,
        relative_path: str,
        now: float,
        *,
        reason: str = "user",
        identifier: str | None = None,
    ) -> dict[str, object]:
        _authorize_delete(catalog, root_id)
        source = None
        published = False
        identifier = str(uuid.uuid4()) if identifier is None else identifier
        if not _is_uuid(identifier):
            raise TrashError("invalid")
        try:
            try:
                source = open_delete_source(catalog, root_id, relative_path)
            except AddressRejected as exc:
                raise _from_address(exc, restore=False) from exc
            node = _capture(source.parent_fd, source.name, "", relative_path)
            if (node["device"], node["inode"]) != (source.device, source.inode):
                raise TrashError("conflict")
            _require_tree_allowed(catalog, root_id, node)
            trash_device = os.stat(self._trash_path).st_dev
            cross = node["device"] != trash_device
            if cross and _cross_unsupported(node):
                raise TrashError("unsupported_metadata")
            document = load_ui_state_locked(self.state_directory)
            kind = "directory" if node["kind"] == "directory" else "file"
            marks = _collect_marks(document, root_id, relative_path, kind)
            journal = {
                "version": 1,
                "instanceId": self.instance_id,
                "operation": "delete",
                "id": identifier,
                "phase": "intent",
                "sourceRootId": root_id,
                "sourcePath": relative_path,
                "destinationPath": relative_path,
                "kind": kind,
                "size": _byte_size(node),
                "reason": reason,
                "deletedAt": _utc(now),
                "uiMetadata": marks,
                "crossFilesystem": cross,
                "separatedLinks": _linked_files(node) if cross else 0,
                "topIdentity": [node["device"], node["inode"]],
                "payloadIdentity": None,
                "publishedIdentity": None,
                "contentDigest": _content_digest(source.parent_fd, source.name),
                "manifest": _manifest(node),
            }
            self._save_journal(journal)
            if cross:
                self._stage_cross_delete(source, node, journal)
                self._crash("delete_after_stage")
                if not _source_still_matches(catalog, root_id, relative_path, journal):
                    raise TrashError("conflict")
                self._publish_staging(identifier)
            else:
                self._rename_same_filesystem(source, identifier, journal)
            published = True
            payload_identity = _payload_identity(self._trash_path / identifier)
            journal["payloadIdentity"] = list(payload_identity)
            journal["publishedIdentity"] = list(payload_identity)
            self._write_publication(identifier, journal)
            self._crash("delete_after_publish")
            document = self._commit_delete_marks(document, journal)
            journal["phase"] = "ui_committed"
            self._save_journal(journal)
            self._crash("delete_after_ui")
            if cross:
                if not self._remove_published_source(catalog, journal):
                    raise TrashError("indeterminate", id=identifier)
            self._remove_journal(identifier)
            return _success(journal, document)
        except TrashCrash:
            raise
        except Exception:
            if not published and not _payload_exists(self._trash_path / identifier):
                self._discard_identifier(identifier)
                self._remove_journal(identifier)
            raise
        finally:
            if source is not None:
                source.close()

    def _stage_cross_delete(self, source: object, node: dict[str, object], journal: dict[str, object]) -> None:
        identifier = str(journal["id"])
        _require_space(self._staging_path, int(journal["size"]))
        staging_fd = _open_directory(self._staging_path)
        created = False
        try:
            os.mkdir(identifier, 0o700, dir_fd=staging_fd)
            created = True
            entry = os.open(identifier, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=staging_fd)
            try:
                os.fchmod(entry, 0o700)
                self._crash("delete_during_copy")
                _copy_node(source.parent_fd, source.name, entry, "payload", node)
                os.fsync(entry)
                if _content_digest(entry, "payload") != journal["contentDigest"]:
                    raise TrashError("conflict")
                if not _metadata_matches(entry, "payload", node):
                    raise TrashError("unsupported_metadata")
                os.fsync(staging_fd)
            finally:
                os.close(entry)
            journal["phase"] = "staged"
            self._save_journal(journal)
        except Exception:
            if created:
                _remove_name(staging_fd, identifier)
                os.fsync(staging_fd)
            raise
        finally:
            os.close(staging_fd)

    def _publish_staging(self, identifier: str) -> None:
        staging_fd = _open_directory(self._staging_path)
        trash_fd = _open_directory(self._trash_path)
        try:
            try:
                rename_exclusive(staging_fd, identifier, trash_fd, identifier)
            except FileExistsError as exc:
                raise TrashError("conflict") from exc
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise TrashError("unavailable") from exc
                if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise TrashError("limit") from exc
                raise
            os.fsync(trash_fd)
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
            os.close(trash_fd)

    def _rename_same_filesystem(self, source: object, identifier: str, journal: dict[str, object]) -> None:
        trash_fd = _open_directory(self._trash_path)
        entry = -1
        moved = False
        try:
            os.mkdir(identifier, 0o700, dir_fd=trash_fd)
            entry = os.open(identifier, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=trash_fd)
            os.fchmod(entry, 0o700)
            try:
                rename_exclusive(source.parent_fd, source.name, entry, "payload")
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise TrashError("unavailable") from exc
                if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise TrashError("limit") from exc
                raise
            moved = True
            os.fsync(entry)
            os.fsync(trash_fd)
            os.fsync(source.parent_fd)
            info = os.lstat("payload", dir_fd=entry)
            if (info.st_dev, info.st_ino) != (source.device, source.inode):
                raise TrashError("indeterminate", id=identifier)
            journal["contentDigest"] = _content_digest(entry, "payload")
        except Exception:
            if entry >= 0:
                os.close(entry)
                entry = -1
            if not moved:
                _remove_name(trash_fd, identifier)
                os.fsync(trash_fd)
            raise
        finally:
            if entry >= 0:
                os.close(entry)
            os.close(trash_fd)

    def _write_publication(self, identifier: str, journal: dict[str, object]) -> None:
        directory = self._trash_path / identifier
        meta = _meta_document(journal)
        raw = _dump_meta(meta)
        atomic_write(directory / "meta.json", raw)
        digest = _seal_digest_at(directory)
        seal = _dump_seal(self._mac(raw, digest))
        atomic_write(directory / "meta.seal", seal)
        journal["phase"] = "published"
        self._save_journal(journal)

    def _commit_delete_marks(self, document: dict[str, object], journal: dict[str, object]) -> dict[str, object]:
        draft = copy.deepcopy(document)
        _strip_items(draft, str(journal["sourceRootId"]), str(journal["sourcePath"]), str(journal["kind"]))
        return commit_ui_state_locked(self.state_directory, document, draft)

    def _remove_published_source(self, catalog: RootCatalog, journal: dict[str, object]) -> bool:
        opened = None
        try:
            try:
                opened = open_delete_source(catalog, str(journal["sourceRootId"]), str(journal["sourcePath"]))
            except AddressRejected as exc:
                if exc.code in {"not_found", "forbidden"}:
                    return _source_identity_gone(catalog, journal, exc.code)
                if exc.code == "unavailable":
                    return False
                return False
            if [opened.device, opened.inode] != journal["topIdentity"]:
                return True
            manifest = journal["manifest"]
            if not isinstance(manifest, list):
                return False
            return _remove_manifest(opened.parent_fd, opened.name, manifest, "", self)
        finally:
            if opened is not None:
                opened.close()

    def _restore_locked(
        self,
        catalog: RootCatalog,
        identifier: str,
        now: float,
        alternative_path: str | None,
        destination_root_id: str | None = None,
        on_intent: Callable[[dict[str, object]], None] | None = None,
        on_publication: Callable[[dict[str, object]], None] | None = None,
        tree_file_paths: set[str] | None = None,
    ) -> dict[str, object]:
        del now
        if not _is_uuid(identifier):
            raise TrashError("invalid")
        evaluated = self._evaluate(identifier)
        if evaluated is None:
            raise TrashError("not_found")
        if evaluated["quarantined"]:
            raise TrashError("quarantined", id=identifier)
        meta = evaluated["meta"]
        assert isinstance(meta, dict)
        source_root_id = str(meta["sourceRootId"])
        root_id = source_root_id if destination_root_id is None else destination_root_id
        if availability(catalog, root_id) != "available":
            raise TrashError("source_unavailable", id=identifier)
        _authorize_delete(catalog, root_id)
        destination = str(meta["sourcePath"] if alternative_path is None else alternative_path)
        try:
            from hopper_files.roots import parse_relative_path

            parse_relative_path(destination)
        except AddressRejected as exc:
            raise TrashError("invalid") from exc
        document = load_ui_state_locked(self.state_directory)
        if _metadata_conflict(document, root_id, destination, str(meta["kind"]), meta["uiMetadata"]):
            raise TrashError(
                "metadata_conflict",
                id=identifier,
                sourceRootId=source_root_id,
                sourcePath=str(meta["sourcePath"]),
            )
        published = False
        journal = {
            "version": 1,
            "instanceId": self.instance_id,
            "operation": "restore",
            "id": identifier,
            "phase": "intent",
            "sourceRootId": source_root_id,
            "destinationRootId": root_id,
            "sourcePath": meta["sourcePath"],
            "destinationPath": destination,
            "kind": meta["kind"],
            "size": meta["size"],
            "reason": meta["reason"],
            "deletedAt": meta["deletedAt"],
            "uiMetadata": meta["uiMetadata"],
            "crossFilesystem": False,
            "topIdentity": None,
            "payloadIdentity": list(_payload_identity(self._trash_path / identifier)),
            "restoreHandle": _payload_handle(self._trash_path / identifier),
            "publishedIdentity": None,
            "publishedHandle": None,
            "contentDigest": _content_digest_at(self._trash_path / identifier),
            "manifest": [],
        }
        self._save_journal(journal)
        if on_intent is not None:
            on_intent({
                "rootId": root_id,
                "path": destination,
                "handle": journal.get("restoreHandle"),
                "digest": journal.get("contentDigest"),
                "identity": journal.get("payloadIdentity"),
            })
        opened = None
        try:
            try:
                opened = open_restore_parent(catalog, root_id, destination)
            except AddressRejected as exc:
                raise _from_address(exc, restore=True) from exc
            payload_device = _payload_identity(self._trash_path / identifier)[0]
            cross = payload_device != opened.parent_device
            journal["crossFilesystem"] = cross
            self._save_journal(journal)
            entry = _open_directory(self._trash_path / identifier)
            try:
                node = _capture(entry, "payload", "", destination)
            finally:
                os.close(entry)
            _require_tree_allowed(catalog, root_id, node)
            if cross and _cross_unsupported(node):
                raise TrashError("unsupported_metadata")
            if cross:
                self._stage_cross_restore(opened, node, journal, on_intent, tree_file_paths)
                self._crash("restore_after_stage")
                self._publish_restore_stage(opened, journal)
            else:
                if tree_file_paths:
                    entry = _open_directory(self._trash_path / identifier)
                    try:
                        journal["treeFileHandles"] = _tree_file_handles_at(
                            entry, "payload", node, tree_file_paths
                        )
                    finally:
                        os.close(entry)
                if on_intent is not None and tree_file_paths:
                    on_intent({
                        "rootId": root_id,
                        "path": destination,
                        "handle": journal.get("restoreHandle"),
                        "digest": journal.get("contentDigest"),
                        "identity": journal.get("payloadIdentity"),
                        "treeFileHandles": journal.get("treeFileHandles", {}),
                    })
                if tree_file_paths:
                    self._save_journal(journal)
                self._rename_restore_same(opened, identifier, journal)
            published = True
            if on_publication is not None:
                on_publication({
                    "rootId": root_id,
                    "path": destination,
                    "identity": journal.get("publishedIdentity"),
                    "digest": journal.get("contentDigest"),
                    "handle": journal.get("publishedHandle"),
                    "treeFileHandles": journal.get("treeFileHandles", {}),
                })
            self._crash("restore_after_publish")
            if not _restored_object_matches(catalog, root_id, destination, journal):
                raise TrashError("indeterminate", id=identifier)
            document = self._commit_restore_marks(document, journal, catalog)
            journal["phase"] = "ui_committed"
            self._save_journal(journal)
            self._crash("restore_after_ui")
            self._crash("restore_before_trash_removal")
            if not _restored_object_matches(catalog, root_id, destination, journal):
                self._withdraw_marks(journal)
                raise TrashError("indeterminate", id=identifier)
            self._remove_trash_copy(identifier, journal["payloadIdentity"])
            self._remove_journal(identifier)
            fresh = load_ui_state_locked(self.state_directory)
            return {
                "id": identifier,
                "rootId": root_id,
                "path": destination,
                "stateRevision": fresh["stateRevision"],
            }
        except TrashCrash:
            raise
        except Exception:
            uncertain_publication = journal.get("phase") in {"publishing", "published", "ui_committed"}
            if not published and not uncertain_publication and opened is not None:
                stage_name = journal.get("stageName")
                if isinstance(stage_name, str):
                    _remove_name(opened.parent_fd, stage_name)
                    os.fsync(opened.parent_fd)
            if not published and not uncertain_publication:
                self._remove_journal(identifier)
            raise
        finally:
            if opened is not None:
                opened.close()

    def _stage_cross_restore(
        self,
        opened: object,
        node: dict[str, object],
        journal: dict[str, object],
        on_intent: Callable[[dict[str, object]], None] | None = None,
        tree_file_paths: set[str] | None = None,
    ) -> None:
        _require_space_fd(opened.parent_fd, int(journal["size"]))
        stage_name = f".hopper-stage-{secrets.token_hex(16)}.{'dir' if node['kind'] == 'directory' else 'tmp'}"
        journal["stageName"] = stage_name
        self._save_journal(journal)
        self._crash("restore_during_copy")
        copied_file_handles: dict[str, dict[str, object]] = {}
        file_handle_collector = copied_file_handles if tree_file_paths else None
        if node["kind"] == "directory":
            os.mkdir(stage_name, 0o700, dir_fd=opened.parent_fd)
            stage = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=opened.parent_fd)
            try:
                _apply_directory_root(stage, node)
                entry = _open_directory(self._trash_path / str(journal["id"]))
                try:
                    source = os.open("payload", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=entry)
                    try:
                        for child in node["children"]:
                            _copy_node(
                                source,
                                str(child["name"]),
                                stage,
                                str(child["name"]),
                                child,
                                file_handle_collector,
                                tree_file_paths,
                            )
                    finally:
                        os.close(source)
                finally:
                    os.close(entry)
                os.fsync(stage)
            except Exception:
                os.close(stage)
                _remove_name(opened.parent_fd, stage_name)
                os.fsync(opened.parent_fd)
                raise
            os.close(stage)
        else:
            entry = _open_directory(self._trash_path / str(journal["id"]))
            try:
                _copy_node(
                    entry, "payload", opened.parent_fd, stage_name, node,
                    file_handle_collector, tree_file_paths,
                )
            except Exception:
                _remove_name(opened.parent_fd, stage_name)
                os.fsync(opened.parent_fd)
                raise
            finally:
                os.close(entry)
        if _content_digest(opened.parent_fd, stage_name) != journal["contentDigest"]:
            _remove_name(opened.parent_fd, stage_name)
            raise TrashError("conflict")
        if not _metadata_matches(opened.parent_fd, stage_name, node):
            _remove_name(opened.parent_fd, stage_name)
            raise TrashError("unsupported_metadata")
        if tree_file_paths:
            stage_file_handles = _tree_file_handles_at(
                opened.parent_fd, stage_name, node, tree_file_paths
            )
            if stage_file_handles != copied_file_handles:
                _remove_name(opened.parent_fd, stage_name)
                raise TrashError("conflict")
        if tree_file_paths:
            journal["treeFileHandles"] = copied_file_handles
        os.fsync(opened.parent_fd)
        staged = os.lstat(stage_name, dir_fd=opened.parent_fd)
        # The opaque file handle includes the filesystem's generation identity;
        # a recycled device/inode pair must not inherit the saved marks.
        journal["stagedIdentity"] = [staged.st_dev, staged.st_ino]
        journal["restoreHandle"] = _file_handle_at(opened.parent_fd, stage_name)
        journal["phase"] = "staged"
        if on_intent is not None:
            on_intent({
                "rootId": journal.get("destinationRootId"),
                "path": journal.get("destinationPath"),
                "handle": journal.get("restoreHandle"),
                "digest": journal.get("contentDigest"),
                "identity": journal.get("payloadIdentity"),
                "treeFileHandles": copied_file_handles,
            })
        self._save_journal(journal)

    def _publish_restore_stage(self, opened: object, journal: dict[str, object]) -> tuple[int, int]:
        stage_name = str(journal["stageName"])
        journal["phase"] = "publishing"
        self._save_journal(journal)
        try:
            rename_exclusive(opened.parent_fd, stage_name, opened.parent_fd, opened.name)
        except FileExistsError as exc:
            _remove_name(opened.parent_fd, stage_name)
            journal["phase"] = "staged"
            raise TrashError("destination_occupied", id=journal["id"], sourceRootId=journal["sourceRootId"], sourcePath=journal["sourcePath"]) from exc
        self._crash("restore_after_rename_before_identity")
        os.fsync(opened.parent_fd)
        info = os.lstat(opened.name, dir_fd=opened.parent_fd)
        handle = _file_handle_at(opened.parent_fd, opened.name)
        self._record_restore_publication(journal, (info.st_dev, info.st_ino), handle)
        return info.st_dev, info.st_ino

    def _rename_restore_same(self, opened: object, identifier: str, journal: dict[str, object]) -> tuple[int, int]:
        entry = _open_directory(self._trash_path / identifier)
        try:
            journal["phase"] = "publishing"
            self._save_journal(journal)
            try:
                rename_exclusive(entry, "payload", opened.parent_fd, opened.name)
            except FileExistsError as exc:
                journal["phase"] = "intent"
                raise TrashError(
                    "destination_occupied",
                    id=identifier,
                    sourceRootId=journal["sourceRootId"],
                    sourcePath=journal["sourcePath"],
                ) from exc
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise TrashError("unavailable") from exc
                raise
            self._crash("restore_after_rename_before_identity")
            os.fsync(opened.parent_fd)
            os.fsync(entry)
            info = os.lstat(opened.name, dir_fd=opened.parent_fd)
            handle = _file_handle_at(opened.parent_fd, opened.name)
            if not _same_file_handle(handle, journal.get("restoreHandle")):
                raise TrashError("indeterminate", id=identifier)
            self._record_restore_publication(journal, (info.st_dev, info.st_ino), handle)
            return info.st_dev, info.st_ino
        finally:
            os.close(entry)

    def _commit_restore_marks(
        self,
        document: dict[str, object],
        journal: dict[str, object],
        catalog: RootCatalog,
    ) -> dict[str, object]:
        records = journal["uiMetadata"]
        if not isinstance(records, list):
            return document
        root_id = str(journal.get("destinationRootId") or journal["sourceRootId"])
        destination = str(journal["destinationPath"])
        if _marks_match(document, root_id, destination, records):
            return document
        if _metadata_conflict(document, root_id, destination, str(journal["kind"]), records):
            raise TrashError("indeterminate", id=journal["id"])
        hints = _destination_hints(catalog, root_id, destination)
        draft = copy.deepcopy(document)
        _apply_marks(draft, root_id, destination, records, hints)
        return commit_ui_state_locked(self.state_directory, document, draft)

    def _withdraw_marks(self, journal: dict[str, object]) -> None:
        """Remove marks from a destination whose published inode was replaced."""
        records = journal["uiMetadata"]
        if not isinstance(records, list):
            return
        document = load_ui_state_locked(self.state_directory)
        root_id = str(journal.get("destinationRootId") or journal["sourceRootId"])
        destination = str(journal["destinationPath"])
        if not _marks_match(document, root_id, destination, records):
            return
        draft = copy.deepcopy(document)
        _strip_exact_marks(draft, root_id, destination, records)
        commit_ui_state_locked(self.state_directory, document, draft)

    def _remove_trash_copy(self, identifier: str, payload_identity: object) -> None:
        entry = _open_directory(self._trash_path / identifier)
        try:
            try:
                info = os.lstat("payload", dir_fd=entry)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not isinstance(payload_identity, list) or [info.st_dev, info.st_ino] != payload_identity:
                    raise TrashError("indeterminate", id=identifier)
                _remove_name(entry, "payload")
            for name in list(os.listdir(entry)):
                child = os.lstat(name, dir_fd=entry)
                if stat.S_ISDIR(child.st_mode) and not stat.S_ISLNK(child.st_mode):
                    raise TrashError("indeterminate", id=identifier)
                os.unlink(name, dir_fd=entry)
            os.fsync(entry)
        finally:
            os.close(entry)
        trash_fd = _open_directory(self._trash_path)
        try:
            os.rmdir(identifier, dir_fd=trash_fd)
            os.fsync(trash_fd)
        finally:
            os.close(trash_fd)

    def _list_locked(self, catalog: RootCatalog) -> list[dict[str, object]]:
        pending = self._pending_ids()
        trash_fd = _open_directory(self._trash_path)
        entries: list[dict[str, object]] = []
        try:
            names = sorted(name for name in os.listdir(trash_fd) if _is_uuid(name))
        finally:
            os.close(trash_fd)
        for name in names:
            evaluated = self._evaluate(name)
            if evaluated is None:
                continue
            recovery = "indeterminate" if name in pending else "ready"
            if evaluated["quarantined"]:
                entries.append(
                    {"id": name, "quarantined": True, "recovery": recovery, "legacy": bool(evaluated.get("legacy"))}
                )
                continue
            meta = evaluated["meta"]
            assert isinstance(meta, dict)
            entries.append(
                {
                    "id": name,
                    "quarantined": False,
                    "recovery": recovery,
                    "sourceRootId": meta["sourceRootId"],
                    "sourcePath": meta["sourcePath"],
                    "deletedAt": meta["deletedAt"],
                    "kind": meta["kind"],
                    "size": meta["size"],
                    "reason": meta["reason"],
                    "uiMetadata": meta["uiMetadata"],
                    "restoreAvailable": availability(catalog, str(meta["sourceRootId"])) == "available",
                }
            )
        entries.sort(key=lambda item: (item.get("deletedAt", ""), item["id"]))
        return entries

    def _recover_locked(self, catalog: RootCatalog, now: float) -> None:
        del now
        self._discard_orphan_staging()
        for identifier in sorted(self._pending_ids()):
            try:
                journal = self._read_journal(identifier)
            except (OSError, StateError, TrashError, ValueError):
                continue
            if not isinstance(journal, dict) or journal.get("instanceId") != self.instance_id:
                continue
            try:
                if journal.get("operation") == "delete":
                    self._recover_delete(catalog, journal)
                elif journal.get("operation") == "restore":
                    self._recover_restore(catalog, journal)
                elif journal.get("operation") == "move":
                    self._recover_move(catalog, journal)
            except TrashCrash:
                raise
            except (TrashError, OSError, StateError, AddressRejected):
                continue

    def _recover_delete(self, catalog: RootCatalog, journal: dict[str, object]) -> None:
        identifier = str(journal["id"])
        phase = journal.get("phase")
        final = self._trash_path / identifier
        staging = self._staging_path / identifier
        if phase == "intent":
            if _is_dir(final) and _payload_exists(final):
                self._ensure_publication(identifier, journal)
                phase = "published"
            else:
                self._discard_identifier(identifier)
                if _source_still_matches(catalog, str(journal["sourceRootId"]), str(journal["sourcePath"]), journal):
                    self._remove_journal(identifier)
                return
        if phase == "staged":
            if _is_dir(final) and not _is_dir(staging):
                self._ensure_publication(identifier, journal)
                phase = "published"
            elif _is_dir(staging) and not _is_dir(final):
                self._discard_identifier(identifier)
                self._remove_journal(identifier)
                return
            else:
                return
        if phase in {"published", "ui_committed"}:
            if not (_is_dir(final) and _payload_exists(final)):
                return
            self._ensure_publication(identifier, journal)
            document = load_ui_state_locked(self.state_directory)
            document = self._commit_delete_marks(document, journal)
            journal["phase"] = "ui_committed"
            self._save_journal(journal)
            if journal.get("crossFilesystem") is True:
                if not self._remove_published_source(catalog, journal):
                    return
            self._remove_journal(identifier)

    def _record_restore_publication(
        self,
        journal: dict[str, object],
        identity: tuple[int, int],
        handle: dict[str, object],
    ) -> None:
        """Remember a restore rename before any later UI-state commit."""
        if not _same_file_handle(handle, journal.get("restoreHandle")):
            raise TrashError("indeterminate", id=journal["id"])
        journal["publishedIdentity"] = [identity[0], identity[1]]
        journal["publishedHandle"] = handle
        journal["phase"] = "published"
        self._save_journal(journal)

    def _recover_restore(self, catalog: RootCatalog, journal: dict[str, object]) -> None:
        identifier = str(journal["id"])
        phase = journal.get("phase")
        destination = str(journal.get("destinationPath") or "")
        root_id = str(journal.get("destinationRootId") or journal.get("sourceRootId") or "")
        if phase in {"intent", "staged", "publishing"} and not _identity_pair(journal.get("publishedIdentity")):
            if not self._adopt_restore_if_published(catalog, journal):
                return
        published = journal.get("publishedIdentity")
        if not _identity_pair(published):
            return
        if not _restored_object_matches(catalog, root_id, destination, journal):
            # The opaque file handle and digest bind recovery to the published
            # object; a recycled device/inode pair is not sufficient.
            if phase == "ui_committed":
                self._withdraw_marks(journal)
            return
        document = load_ui_state_locked(self.state_directory)
        self._commit_restore_marks(document, journal, catalog)
        journal["phase"] = "ui_committed"
        self._save_journal(journal)
        if not _restored_object_matches(catalog, root_id, destination, journal):
            self._withdraw_marks(journal)
            return
        self._remove_trash_copy(identifier, journal.get("payloadIdentity"))
        self._remove_journal(identifier)

    def _adopt_restore_if_published(self, catalog: RootCatalog, journal: dict[str, object]) -> bool:
        """Bind a rename that won the race against its identity record.

        Returns True when the journal now names that published object. A missing
        destination with the trash payload still present is unpublished and may
        drop only the journal. Any other occupant keeps the journal and both copies.
        """
        identifier = str(journal["id"])
        observed = _lookup_restore_destination(
            catalog,
            str(journal.get("destinationRootId") or journal.get("sourceRootId") or ""),
            str(journal.get("destinationPath") or ""),
        )
        if observed is None:
            if self._stage_present(catalog, journal):
                self._remove_stage_name(catalog, journal)
            if _payload_exists(self._trash_path / identifier):
                self._remove_journal(identifier)
            return False
        if observed.get("conflict") is True or not _restore_observation_matches(journal, observed):
            return False
        if journal.get("crossFilesystem") is True and self._stage_present(catalog, journal):
            return False
        identity = observed["identity"]
        handle = observed["handle"]
        assert isinstance(identity, tuple)
        assert isinstance(handle, dict)
        self._record_restore_publication(journal, identity, handle)
        return True

    def _stage_present(self, catalog: RootCatalog, journal: dict[str, object]) -> bool:
        stage_name = journal.get("stageName")
        if not isinstance(stage_name, str) or not stage_name.startswith(".hopper-stage-"):
            return False
        destination = str(journal.get("destinationPath") or "")
        parent = destination.rsplit("/", 1)[0] if "/" in destination else ""
        try:
            directory = open_existing_directory(
                catalog, str(journal.get("destinationRootId") or journal.get("sourceRootId") or ""), parent
            )
        except AddressRejected:
            return False
        try:
            os.lstat(stage_name, dir_fd=directory)
        except FileNotFoundError:
            return False
        else:
            return True
        finally:
            os.close(directory)

    def _ensure_publication(self, identifier: str, journal: dict[str, object]) -> None:
        evaluated = self._evaluate(identifier)
        if evaluated is not None and not evaluated["quarantined"]:
            journal["phase"] = "published"
            self._save_journal(journal)
            return
        if not _payload_exists(self._trash_path / identifier):
            return
        digest = _seal_digest_at(self._trash_path / identifier)
        journal["contentDigest"] = _content_digest_at(self._trash_path / identifier)
        identity = _payload_identity(self._trash_path / identifier)
        journal["payloadIdentity"] = list(identity)
        journal["publishedIdentity"] = list(identity)
        self._write_publication(identifier, journal)
        if digest != _seal_digest_at(self._trash_path / identifier):
            return

    def _purge_locked(self, now: float) -> int:
        pending = self._pending_ids()
        removed = 0
        moment = int(now)
        trash_fd = _open_directory(self._trash_path)
        try:
            names = [name for name in os.listdir(trash_fd) if _is_uuid(name)]
        finally:
            os.close(trash_fd)
        for name in names:
            if name in pending:
                continue
            evaluated = self._evaluate(name)
            if evaluated is None or evaluated["quarantined"]:
                continue
            meta = evaluated["meta"]
            assert isinstance(meta, dict)
            deleted_at = _parse_utc(str(meta["deletedAt"]))
            if deleted_at is None or moment < deleted_at + RETENTION_SECONDS:
                continue
            self._purge_directory(name)
            removed += 1
        return removed

    def _purge_directory(self, identifier: str) -> None:
        trash_fd = _open_directory(self._trash_path)
        try:
            info = os.lstat(identifier, dir_fd=trash_fd)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return
            _remove_name(trash_fd, identifier)
            os.fsync(trash_fd)
        finally:
            os.close(trash_fd)

    def _evaluate(self, identifier: str) -> dict[str, object] | None:
        if not _is_uuid(identifier):
            return None
        trash_fd = _open_directory(self._trash_path)
        try:
            try:
                info = os.lstat(identifier, dir_fd=trash_fd)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return {"id": identifier, "quarantined": True, "meta": None}
        finally:
            os.close(trash_fd)
        try:
            entry = _open_directory(self._trash_path / identifier)
        except TrashError:
            return {"id": identifier, "quarantined": True, "meta": None}
        try:
            meta_bytes = _read_regular(entry, "meta.json")
            seal_bytes = _read_regular(entry, "meta.seal")
            if meta_bytes is None or seal_bytes is None:
                return {"id": identifier, "quarantined": True, "meta": None}
            meta = _parse_meta(meta_bytes, identifier)
            if meta is None:
                legacy = meta_version(meta_bytes) == LEGACY_META_VERSION
                return {"id": identifier, "quarantined": True, "meta": None, "legacy": legacy}
            try:
                payload = os.lstat("payload", dir_fd=entry)
            except FileNotFoundError:
                return {"id": identifier, "quarantined": True, "meta": None}
            if stat.S_ISLNK(payload.st_mode):
                return {"id": identifier, "quarantined": True, "meta": None}
            if meta["kind"] == "file" and not stat.S_ISREG(payload.st_mode):
                return {"id": identifier, "quarantined": True, "meta": None}
            if meta["kind"] == "directory" and not stat.S_ISDIR(payload.st_mode):
                return {"id": identifier, "quarantined": True, "meta": None}
            try:
                digest = _seal_digest(entry, "payload")
            except (OSError, TrashError):
                return {"id": identifier, "quarantined": True, "meta": None}
            if not self._seal_matches(meta_bytes, digest, seal_bytes):
                return {"id": identifier, "quarantined": True, "meta": None}
            return {"id": identifier, "quarantined": False, "meta": meta}
        finally:
            os.close(entry)

    def _seal_matches(self, meta_bytes: bytes, digest: str, seal_bytes: bytes) -> bool:
        try:
            seal = json.loads(seal_bytes.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(seal, dict) or seal.get("version") != 1 or seal.get("algorithm") != "hmac-sha256":
            return False
        mac = seal.get("mac")
        if not isinstance(mac, str):
            return False
        expected = self._mac(meta_bytes, digest)
        return hmac.compare_digest(mac, expected)

    def _mac(self, meta_bytes: bytes, digest: str) -> str:
        secret = read_signing_secret(self.state_directory)
        message = b"\n".join(
            (
                b"hopper-files-trash-v1",
                self.instance_id.encode("utf-8"),
                meta_bytes,
                digest.encode("ascii"),
            )
        )
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    def _save_journal(self, journal: dict[str, object]) -> None:
        raw = json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        atomic_write(self._journal_directory / f"{journal['id']}.json", raw)

    def _read_journal(self, identifier: str) -> dict[str, object]:
        path = self._journal_directory / f"{identifier}.json"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise TrashError("unavailable")
            chunks: list[bytes] = []
            while True:
                block = os.read(fd, 65536)
                if not block:
                    break
                chunks.append(block)
        finally:
            os.close(fd)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, dict):
            raise TrashError("unavailable")
        return payload

    def _remove_journal(self, identifier: str) -> None:
        path = self._journal_directory / f"{identifier}.json"
        try:
            fd = os.open(self._journal_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return
        try:
            try:
                os.unlink(f"{identifier}.json", dir_fd=fd)
                os.fsync(fd)
            except FileNotFoundError:
                return
        finally:
            os.close(fd)
        if path.exists():
            return

    def _pending_ids(self) -> set[str]:
        result: set[str] = set()
        fd = _open_directory(self._journal_directory)
        try:
            for name in os.listdir(fd):
                if name.endswith(".json") and _is_uuid(name[: -len(".json")]):
                    result.add(name[: -len(".json")])
        finally:
            os.close(fd)
        return result

    def _discard_identifier(self, identifier: str) -> None:
        for parent in (self._staging_path, self._trash_path):
            if not parent.is_dir():
                continue
            fd = _open_directory(parent)
            try:
                _remove_name(fd, identifier)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _discard_orphan_staging(self) -> None:
        pending = self._pending_ids()
        fd = _open_directory(self._staging_path)
        try:
            for name in os.listdir(fd):
                if _is_uuid(name) and name not in pending:
                    _remove_name(fd, name)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _remove_stage_name(self, catalog: RootCatalog, journal: dict[str, object]) -> None:
        stage_name = journal.get("stageName")
        if not isinstance(stage_name, str) or not stage_name.startswith(".hopper-stage-"):
            return
        destination = str(journal.get("destinationPath") or "")
        parent = destination.rsplit("/", 1)[0] if "/" in destination else ""
        from hopper_files.roots import open_existing_directory

        try:
            directory = open_existing_directory(
                catalog, str(journal.get("destinationRootId") or journal["sourceRootId"]), parent
            )
        except AddressRejected:
            return
        try:
            _remove_name(directory, stage_name)
            os.fsync(directory)
        finally:
            os.close(directory)

    @property
    def _trash_path(self) -> Path:
        return self.state_directory / "trash"

    @property
    def _staging_path(self) -> Path:
        return self._trash_path / ".staging"

    @property
    def _journal_directory(self) -> Path:
        return self.state_directory / "journals" / "trash"

    @property
    def _lock_path(self) -> Path:
        return self._trash_path / ".lock"


def _authorize_delete(catalog: RootCatalog, root_id: str) -> None:
    """The single base is always present; Linux permission decides the rest."""
    if availability(catalog, root_id) != "available" or root_record(catalog, root_id) is None:
        raise TrashError("unavailable")


def _from_address(exc: AddressRejected, *, restore: bool) -> TrashError:
    if exc.code == "conflict":
        return TrashError("destination_occupied")
    if exc.code == "limit":
        return TrashError("limit")
    if exc.code == "invalid":
        return TrashError("invalid")
    if exc.code == "not_found":
        return TrashError("source_unavailable" if restore else "not_found")
    if exc.code == "unavailable":
        return TrashError("source_unavailable" if restore else "unavailable")
    return TrashError("forbidden")


def _success(journal: dict[str, object], document: dict[str, object]) -> dict[str, object]:
    return {
        "id": journal["id"],
        "sourceRootId": journal["sourceRootId"],
        "sourcePath": journal["sourcePath"],
        "deletedAt": journal["deletedAt"],
        "kind": journal["kind"],
        "size": journal["size"],
        "reason": journal["reason"],
        "uiMetadata": journal["uiMetadata"],
        "separatedLinks": int(journal.get("separatedLinks") or 0),
        "stateRevision": document["stateRevision"],
    }


def _meta_document(journal: dict[str, object]) -> dict[str, object]:
    """Return version 2 metadata with the absolute source address (HF-TRASH-001)."""
    return {
        "version": META_VERSION,
        "id": journal["id"],
        "sourcePath": _absolute(str(journal["sourcePath"])),
        "deletedAt": journal["deletedAt"],
        "kind": journal["kind"],
        "size": journal["size"],
        "reason": journal["reason"],
        "uiMetadata": journal["uiMetadata"],
    }


def _dump_meta(meta: dict[str, object]) -> bytes:
    return json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _dump_seal(mac: str) -> bytes:
    return json.dumps(
        {"algorithm": "hmac-sha256", "mac": mac, "version": 1},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def meta_version(raw: bytes) -> int | None:
    """Return the schema version recorded in raw metadata, or None."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def _parse_meta(raw: bytes, identifier: str) -> dict[str, object] | None:
    """Parse version 2 metadata into the internal fixed-base form.

    Internally a trash entry is addressed as ``(BASE_ID, relative path)``;
    ``meta.json`` records the absolute path.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _META_FIELDS:
        return None
    if payload["version"] != META_VERSION or isinstance(payload["version"], bool):
        return None
    source = payload["sourcePath"]
    if not isinstance(source, str) or not source.startswith("/") or source == "/" or not _canonical(source[1:]):
        return None
    payload = {**payload, "sourceRootId": BASE_ID, "sourcePath": source[1:]}
    return _parse_common(payload, identifier)


def parse_legacy_meta(raw: bytes, identifier: str) -> dict[str, object] | None:
    """Parse version 1 metadata for the HF-NAV-012 migration only."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _LEGACY_META_FIELDS:
        return None
    if payload["version"] != LEGACY_META_VERSION or isinstance(payload["version"], bool):
        return None
    if not _root_text(payload["sourceRootId"]) or not _canonical(payload["sourcePath"]):
        return None
    return _parse_common(payload, identifier)


def _parse_common(payload: dict[str, object], identifier: str) -> dict[str, object] | None:
    if payload["id"] != identifier or not _is_uuid(identifier):
        return None
    if payload["kind"] not in {"file", "directory"} or not _reason(payload["reason"]):
        return None
    if isinstance(payload["size"], bool) or not isinstance(payload["size"], int) or payload["size"] < 0:
        return None
    if _parse_utc(payload["deletedAt"]) is None:
        return None
    if not _marks_valid(payload["uiMetadata"]):
        return None
    return payload


def _marks_valid(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, list):
        return False
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _MARK_FIELDS:
            return False
        relative = item["relativePath"]
        if not _canonical(relative) or relative in seen:
            return False
        seen.add(relative)
        labels = item["labelIds"]
        if not isinstance(labels, list) or len(labels) != len(set(labels)):
            return False
        if any(not isinstance(label, str) or label not in LABELS for label in labels):
            return False
        if not isinstance(item["favorite"], bool):
            return False
        emoji = item["emoji"]
        if emoji is not None and (not isinstance(emoji, str) or emoji == "" or len(emoji) > 16 or "\x00" in emoji):
            return False
    return True


def _root_text(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    if value[0] not in _ROOT_ID_CHARS - {".", "_", "-"}:
        return False
    return all(character in _ROOT_ID_CHARS for character in value)


def _absolute(relative: str) -> str:
    return "/" + relative


def _canonical(value: object) -> bool:
    if not isinstance(value, str) or "\x00" in value or "\\" in value or value.startswith("/"):
        return False
    if value and any(part in {"", ".", ".."} for part in value.split("/")):
        return False
    return True


def _reason(value: object) -> bool:
    return isinstance(value, str) and value != "" and len(value) <= 32 and "\x00" not in value


def _parse_utc(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp())


def _utc(now: float) -> str:
    return datetime.fromtimestamp(int(now), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        parsed = _UUID(value)
    except ValueError:
        return False
    return str(parsed) == value


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise TrashError("unavailable")
    os.chmod(path, 0o700)


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise TrashError("unavailable") from exc


def _read_regular(parent_fd: int, name: str) -> bytes | None:
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
    finally:
        os.close(fd)


def _capture(parent_fd: int, name: str, relative: str, document_path: str) -> dict[str, object]:
    info = os.lstat(name, dir_fd=parent_fd)
    kind = _kind_of(info)
    node: dict[str, object] = {
        "name": name,
        "relative": relative,
        "documentPath": document_path,
        "kind": kind,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "size": info.st_size if kind == "file" else 0,
        "device": info.st_dev,
        "inode": info.st_ino,
        "nlink": info.st_nlink,
        "link": os.readlink(name, dir_fd=parent_fd) if kind == "symlink" else None,
        "xattrs": _read_xattrs(parent_fd, name, kind),
        "children": [],
        "content": _file_digest(parent_fd, name) if kind == "file" else (
            hashlib.sha256(os.readlink(name, dir_fd=parent_fd).encode("utf-8")).hexdigest() if kind == "symlink" else None
        ),
    }
    if kind == "directory":
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            children = []
            for child in sorted(os.listdir(fd)):
                child_document = child if document_path == "" else document_path + "/" + child
                child_relative = child if relative == "" else relative + "/" + child
                children.append(_capture(fd, child, child_relative, child_document))
            node["children"] = children
        finally:
            os.close(fd)
    if kind == "directory":
        node["size"] = _byte_size(node)
    return node


def _tree_file_handles_at(
    parent_fd: int,
    name: str,
    node: dict[str, object],
    wanted_paths: set[str],
) -> dict[str, dict[str, object]]:
    """Bind each regular file in a restore tree to its kernel handle."""
    handles: dict[str, dict[str, object]] = {}

    def visit(current_parent: int, current_name: str, current: dict[str, object]) -> None:
        kind = current.get("kind")
        relative = current.get("relative")
        if not isinstance(relative, str):
            raise TrashError("conflict")
        if kind == "file":
            if relative not in wanted_paths:
                return
            try:
                fd = os.open(
                    current_name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=current_parent,
                )
            except OSError as exc:
                raise TrashError("conflict") from exc
            try:
                before = os.fstat(fd)
                first_handle = _file_handle_at(current_parent, current_name)
                after = os.fstat(fd)
                second_handle = _file_handle_at(current_parent, current_name)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or before.st_size != current.get("size")
                    or first_handle != second_handle
                ):
                    raise TrashError("conflict")
                handles[relative] = first_handle
            finally:
                os.close(fd)
            return
        if kind != "directory":
            return
        try:
            directory_fd = os.open(
                current_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current_parent,
            )
        except OSError as exc:
            raise TrashError("conflict") from exc
        try:
            children = current.get("children")
            if not isinstance(children, list):
                raise TrashError("conflict")
            child_nodes = [child for child in children if isinstance(child, dict)]
            expected_names = {str(child.get("name")) for child in child_nodes}
            if set(os.listdir(directory_fd)) != expected_names:
                raise TrashError("conflict")
            for child in child_nodes:
                child_name = child.get("name")
                if not isinstance(child_name, str):
                    raise TrashError("conflict")
                visit(directory_fd, child_name, child)
        finally:
            os.close(directory_fd)

    visit(parent_fd, name, node)
    if set(handles) != wanted_paths:
        raise TrashError("conflict")
    return handles


def _kind_of(info: os.stat_result) -> str:
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    return "other"


def _read_xattrs(parent_fd: int, name: str, kind: str) -> dict[str, bytes]:
    if kind == "other":
        return {}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if kind == "directory":
        flags |= os.O_DIRECTORY
    if kind == "symlink":
        flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        proc = f"/proc/self/fd/{fd}"
        follow = kind != "symlink"
        names = os.listxattr(proc, follow_symlinks=follow)
        return {item: os.getxattr(proc, item, follow_symlinks=follow) for item in names}
    finally:
        os.close(fd)


def _require_tree_allowed(catalog: RootCatalog, root_id: str, node: dict[str, object]) -> None:
    top_device = node["device"]

    def visit(current: dict[str, object]) -> None:
        kind = current["kind"]
        if kind == "other" or current["device"] != top_device or device_is_pseudo(int(current["device"])):
            raise TrashError("forbidden")
        for child in current["children"]:
            if isinstance(child, dict):
                visit(child)

    if node["kind"] not in {"file", "directory"}:
        raise TrashError("forbidden")
    visit(node)


def _linked_files(node: dict[str, object]) -> int:
    """Count regular files whose other names keep the original inode (HF-FILE-002)."""
    own = 1 if node["kind"] == "file" and int(node["nlink"]) > 1 else 0
    return own + sum(_linked_files(child) for child in node["children"] if isinstance(child, dict))


def _cross_unsupported(node: dict[str, object]) -> bool:
    mode = int(node["mode"])
    if mode & (stat.S_ISUID | stat.S_ISGID):
        return True
    xattrs = node["xattrs"]
    if isinstance(xattrs, dict) and any(name not in _SUPPORTED_XATTRS for name in xattrs):
        return True
    return any(isinstance(child, dict) and _cross_unsupported(child) for child in node["children"])


def _byte_size(node: dict[str, object]) -> int:
    if node["kind"] == "file":
        return int(node["size"])
    return sum(_byte_size(child) for child in node["children"] if isinstance(child, dict))


def _manifest(node: dict[str, object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []

    def visit(current: dict[str, object]) -> None:
        items.append(
            {
                "relative": current["relative"],
                "kind": current["kind"],
                "device": current["device"],
                "inode": current["inode"],
                "content": current.get("content"),
            }
        )
        for child in current["children"]:
            if isinstance(child, dict):
                visit(child)

    visit(node)
    return items


def _file_digest(parent_fd: int, name: str) -> str:
    hasher = hashlib.sha256()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                return hasher.hexdigest()
            hasher.update(block)
    finally:
        os.close(fd)


def _content_digest(parent_fd: int, name: str) -> str:
    hasher = hashlib.sha256()
    _content_into(parent_fd, name, "", hasher)
    return hasher.hexdigest()


def _content_into(parent_fd: int, name: str, relative: str, hasher: hashlib._Hash) -> None:
    info = os.lstat(name, dir_fd=parent_fd)
    kind = _kind_of(info)
    hasher.update(relative.encode("utf-8") + b"\0" + kind.encode("ascii") + b"\0")
    if kind == "file":
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                hasher.update(block)
        finally:
            os.close(fd)
        hasher.update(b"\0")
    elif kind == "symlink":
        hasher.update(os.readlink(name, dir_fd=parent_fd).encode("utf-8") + b"\0")
    elif kind == "directory":
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            for child in sorted(os.listdir(fd)):
                child_relative = child if relative == "" else relative + "/" + child
                _content_into(fd, child, child_relative, hasher)
        finally:
            os.close(fd)
    else:
        raise TrashError("forbidden")


def _seal_digest(parent_fd: int, name: str) -> str:
    hasher = hashlib.sha256()
    _seal_into(parent_fd, name, "", hasher)
    return hasher.hexdigest()


def _seal_into(parent_fd: int, name: str, relative: str, hasher: hashlib._Hash) -> None:
    info = os.lstat(name, dir_fd=parent_fd)
    kind = _kind_of(info)
    xattrs = _read_xattrs(parent_fd, name, kind)
    hasher.update(relative.encode("utf-8") + b"\0" + kind.encode("ascii") + b"\0")
    hasher.update(f"{stat.S_IMODE(info.st_mode)}\0{info.st_uid}\0{info.st_gid}\0".encode("ascii"))
    for attr in sorted(xattrs):
        hasher.update(attr.encode("utf-8") + b"\0" + xattrs[attr] + b"\0")
    if kind == "file":
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                hasher.update(block)
        finally:
            os.close(fd)
    elif kind == "symlink":
        hasher.update(os.readlink(name, dir_fd=parent_fd).encode("utf-8"))
    elif kind == "directory":
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            for child in sorted(os.listdir(fd)):
                child_relative = child if relative == "" else relative + "/" + child
                _seal_into(fd, child, child_relative, hasher)
        finally:
            os.close(fd)
    else:
        raise TrashError("forbidden")
    hasher.update(b"\0")


def _seal_digest_at(directory: Path) -> str:
    entry = _open_directory(directory)
    try:
        return _seal_digest(entry, "payload")
    finally:
        os.close(entry)


def _content_digest_at(directory: Path) -> str:
    entry = _open_directory(directory)
    try:
        return _content_digest(entry, "payload")
    finally:
        os.close(entry)


def _payload_identity(directory: Path) -> tuple[int, int]:
    entry = _open_directory(directory)
    try:
        info = os.lstat("payload", dir_fd=entry)
    finally:
        os.close(entry)
    return info.st_dev, info.st_ino


def _payload_exists(directory: Path) -> bool:
    if not directory.is_dir() or directory.is_symlink():
        return False
    try:
        entry = _open_directory(directory)
    except TrashError:
        return False
    try:
        os.lstat("payload", dir_fd=entry)
    except FileNotFoundError:
        return False
    else:
        return True
    finally:
        os.close(entry)


def _is_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _copy_node(
    src_parent: int,
    src_name: str,
    dst_parent: int,
    dst_name: str,
    node: dict[str, object],
    file_handles: dict[str, dict[str, object]] | None = None,
    tracked_file_paths: set[str] | None = None,
) -> None:
    kind = node["kind"]
    try:
        if kind == "symlink":
            os.symlink(str(node["link"]), dst_name, dir_fd=dst_parent)
            try:
                _apply_symlink(dst_parent, dst_name, node)
            except Exception:
                try:
                    os.unlink(dst_name, dir_fd=dst_parent)
                except OSError:
                    pass
                raise
            return
        if kind == "directory":
            os.mkdir(dst_name, 0o700, dir_fd=dst_parent)
            dst = os.open(dst_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dst_parent)
            try:
                _apply_directory_root(dst, node)
                src = os.open(src_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=src_parent)
                try:
                    for child in node["children"]:
                        if isinstance(child, dict):
                            _copy_node(
                                src, str(child["name"]), dst, str(child["name"]), child,
                                file_handles, tracked_file_paths,
                            )
                finally:
                    os.close(src)
                os.fsync(dst)
            except Exception:
                os.close(dst)
                _remove_name(dst_parent, dst_name)
                raise
            os.close(dst)
            return
        if kind != "file":
            raise TrashError("forbidden")
        src = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=src_parent)
        try:
            info = os.fstat(src)
            # A file with several links is copied like any other; only the
            # addressed name is removed afterwards (HF-FILE-002).
            if (info.st_dev, info.st_ino) != (node["device"], node["inode"]):
                raise TrashError("conflict")
            dst = os.open(
                dst_name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dst_parent,
            )
            try:
                _reserve(dst, info.st_size)
                _copy_bytes(src, dst)
                _apply_file(dst, node)
                os.fsync(dst)
                if file_handles is not None:
                    relative = node.get("relative")
                    if not isinstance(relative, str):
                        raise TrashError("conflict")
                    if tracked_file_paths is None or relative in tracked_file_paths:
                        opened_info = os.fstat(dst)
                        named_info = os.stat(dst_name, dir_fd=dst_parent, follow_symlinks=False)
                        handle = _file_handle_at(dst_parent, dst_name)
                        final_opened = os.fstat(dst)
                        final_named = os.stat(dst_name, dir_fd=dst_parent, follow_symlinks=False)
                        if (
                            (opened_info.st_dev, opened_info.st_ino)
                            != (named_info.st_dev, named_info.st_ino)
                            or (opened_info.st_dev, opened_info.st_ino)
                            != (final_opened.st_dev, final_opened.st_ino)
                            or (opened_info.st_dev, opened_info.st_ino)
                            != (final_named.st_dev, final_named.st_ino)
                        ):
                            raise TrashError("conflict")
                        file_handles[relative] = handle
            except Exception:
                os.close(dst)
                try:
                    os.unlink(dst_name, dir_fd=dst_parent)
                except OSError:
                    pass
                raise
            os.close(dst)
        finally:
            os.close(src)
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise TrashError("limit") from exc
        raise


def _apply_symlink(parent_fd: int, name: str, node: dict[str, object]) -> None:
    """Preserve a symlink's owner and supported attributes without following it."""
    try:
        os.chown(
            name,
            int(node["uid"]),
            int(node["gid"]),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise TrashError("unsupported_metadata") from exc
    info = os.lstat(name, dir_fd=parent_fd)
    if info.st_uid != int(node["uid"]) or info.st_gid != int(node["gid"]):
        raise TrashError("unsupported_metadata")
    desired = int(node["mode"]) & 0o777
    if stat.S_IMODE(info.st_mode) & 0o777 != desired:
        try:
            os.chmod(name, desired, dir_fd=parent_fd, follow_symlinks=False)
        except (OSError, NotImplementedError) as exc:
            raise TrashError("unsupported_metadata") from exc
        info = os.lstat(name, dir_fd=parent_fd)
        if stat.S_IMODE(info.st_mode) & 0o777 != desired:
            raise TrashError("unsupported_metadata")
    xattrs = node["xattrs"] if isinstance(node["xattrs"], dict) else {}
    link_path = f"/proc/self/fd/{parent_fd}/{name}"
    try:
        existing = set(os.listxattr(link_path, follow_symlinks=False))
        for attr in existing:
            if attr not in xattrs:
                os.removexattr(link_path, attr, follow_symlinks=False)
        for attr, value in xattrs.items():
            os.setxattr(link_path, attr, value, follow_symlinks=False)
        found = {
            attr: os.getxattr(link_path, attr, follow_symlinks=False)
            for attr in os.listxattr(link_path, follow_symlinks=False)
        }
    except OSError as exc:
        raise TrashError("unsupported_metadata") from exc
    if found != xattrs:
        raise TrashError("unsupported_metadata")


def _apply_directory_root(fd: int, node: dict[str, object]) -> None:
    _apply_owner_and_mode(fd, node)
    os.fsync(fd)


def _apply_file(fd: int, node: dict[str, object]) -> None:
    _apply_owner_and_mode(fd, node)


def _apply_owner_and_mode(fd: int, node: dict[str, object]) -> None:
    proc = f"/proc/self/fd/{fd}"
    try:
        os.fchown(fd, int(node["uid"]), int(node["gid"]))
    except OSError as exc:
        raise TrashError("unsupported_metadata") from exc
    existing = set(os.listxattr(proc))
    for attr in existing:
        if attr not in node["xattrs"]:
            try:
                os.removexattr(proc, attr)
            except OSError as exc:
                if exc.errno not in {errno.ENODATA, errno.ENOENT}:
                    raise TrashError("unsupported_metadata") from exc
    try:
        os.fchmod(fd, int(node["mode"]) & 0o777)
    except OSError as exc:
        raise TrashError("unsupported_metadata") from exc
    xattrs = node["xattrs"]
    if isinstance(xattrs, dict):
        for attr, value in xattrs.items():
            try:
                os.setxattr(proc, attr, value)
            except OSError as exc:
                raise TrashError("unsupported_metadata") from exc
    info = os.fstat(fd)
    if info.st_uid != node["uid"] or info.st_gid != node["gid"]:
        raise TrashError("unsupported_metadata")
    if stat.S_IMODE(info.st_mode) & 0o777 != int(node["mode"]) & 0o777:
        raise TrashError("unsupported_metadata")
    if stat.S_IMODE(info.st_mode) & (stat.S_ISUID | stat.S_ISGID):
        raise TrashError("unsupported_metadata")
    found = {attr: os.getxattr(proc, attr) for attr in os.listxattr(proc)}
    if found != xattrs:
        raise TrashError("unsupported_metadata")


def _metadata_matches(parent_fd: int, name: str, node: dict[str, object]) -> bool:
    info = os.lstat(name, dir_fd=parent_fd)
    if _kind_of(info) != node["kind"]:
        return False
    if node["kind"] == "symlink":
        if os.readlink(name, dir_fd=parent_fd) != node["link"]:
            return False
        if info.st_uid != node["uid"] or info.st_gid != node["gid"]:
            return False
        if stat.S_IMODE(info.st_mode) & 0o777 != int(node["mode"]) & 0o777:
            return False
        return _read_xattrs(parent_fd, name, "symlink") == node["xattrs"]
    if info.st_uid != node["uid"] or info.st_gid != node["gid"]:
        return False
    if stat.S_IMODE(info.st_mode) & 0o777 != int(node["mode"]) & 0o777:
        return False
    if stat.S_IMODE(info.st_mode) & (stat.S_ISUID | stat.S_ISGID):
        return False
    if _read_xattrs(parent_fd, name, str(node["kind"])) != node["xattrs"]:
        return False
    if node["kind"] != "directory":
        return True
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        live = set(os.listdir(fd))
        children = [child for child in node["children"] if isinstance(child, dict)]
        if live != {str(child["name"]) for child in children}:
            return False
        return all(_metadata_matches(fd, str(child["name"]), child) for child in children)
    finally:
        os.close(fd)


def _copy_bytes(src: int, dst: int) -> None:
    while True:
        block = os.read(src, 1024 * 1024)
        if not block:
            return
        view = memoryview(block)
        while view:
            written = os.write(dst, view)
            view = view[written:]


def _reserve(fd: int, size: int) -> None:
    if size <= 0:
        return
    try:
        os.posix_fallocate(fd, 0, size)
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise TrashError("limit") from exc
        if exc.errno not in {errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS}:
            raise


def _require_space(path: Path, nbytes: int) -> None:
    usage = os.statvfs(path)
    if usage.f_bavail * usage.f_frsize < nbytes + _SPACE_MARGIN:
        raise TrashError("limit")


def _require_space_fd(fd: int, nbytes: int) -> None:
    usage = os.statvfs(f"/proc/self/fd/{fd}")
    if usage.f_bavail * usage.f_frsize < nbytes + _SPACE_MARGIN:
        raise TrashError("limit")


def _remove_name(parent_fd: int, name: str) -> None:
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return
        for child in os.listdir(fd):
            _remove_name(fd, child)
        os.fsync(fd)
    finally:
        os.close(fd)
    current = os.lstat(name, dir_fd=parent_fd)
    if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino) and stat.S_ISDIR(current.st_mode):
        os.rmdir(name, dir_fd=parent_fd)


def _children_of(manifest: list[object], relative: str) -> list[dict[str, object]]:
    prefix = "" if relative == "" else relative + "/"
    found = []
    for item in manifest:
        if not isinstance(item, dict):
            continue
        rel = item.get("relative")
        if not isinstance(rel, str) or rel == relative or not rel.startswith(prefix):
            continue
        if "/" not in rel[len(prefix) :]:
            found.append(item)
    return found


def _remove_manifest(
    parent_fd: int,
    name: str,
    manifest: list[object],
    relative: str,
    store: TrashStore | None,
) -> bool:
    expected = next(
        (item for item in manifest if isinstance(item, dict) and item.get("relative") == relative),
        None,
    )
    if expected is None:
        return False
    info = os.lstat(name, dir_fd=parent_fd)
    if [info.st_dev, info.st_ino] != [expected.get("device"), expected.get("inode")]:
        return False
    if expected.get("kind") != "directory":
        if stat.S_ISDIR(info.st_mode):
            return False
        if expected.get("kind") == "file" and _file_digest(parent_fd, name) != expected.get("content"):
            return False
        if expected.get("kind") == "symlink":
            digest = hashlib.sha256(os.readlink(name, dir_fd=parent_fd).encode("utf-8")).hexdigest()
            if digest != expected.get("content"):
                return False
        os.unlink(name, dir_fd=parent_fd)
        return True
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return False
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if [opened.st_dev, opened.st_ino] != [expected.get("device"), expected.get("inode")]:
            return False
        children = _children_of(manifest, relative)
        child_names = {str(item["relative"]).rsplit("/", 1)[-1] for item in children}
        live = set(os.listdir(fd))
        if not live <= child_names:
            return False
        for index, item in enumerate(children):
            child_name = str(item["relative"]).rsplit("/", 1)[-1]
            if child_name not in live:
                continue
            if not _remove_manifest(fd, child_name, manifest, str(item["relative"]), None):
                return False
            if store is not None and index == 0 and relative == "":
                store._crash("delete_during_source_removal")
        os.fsync(fd)
    finally:
        os.close(fd)
    current = os.lstat(name, dir_fd=parent_fd)
    if [current.st_dev, current.st_ino] != [expected.get("device"), expected.get("inode")]:
        return False
    os.rmdir(name, dir_fd=parent_fd)
    return True


def _source_still_matches(catalog: RootCatalog, root_id: str, relative_path: str, journal: dict[str, object]) -> bool:
    opened = None
    try:
        opened = open_delete_source(catalog, root_id, relative_path)
    except AddressRejected:
        return False
    try:
        return [opened.device, opened.inode] == journal.get("topIdentity")
    finally:
        opened.close()


def _source_identity_gone(catalog: RootCatalog, journal: dict[str, object], code: str) -> bool:
    del catalog, code
    # A missing or replaced source is not removed again. The trash copy remains
    # the recoverable one. Forbidden here means the original object is no longer
    # the addressed inode, so the replacement is left untouched.
    return journal.get("topIdentity") is not None


def _restored_object_matches(
    catalog: RootCatalog,
    root_id: str,
    relative_path: str,
    journal: dict[str, object],
) -> bool:
    """True only for the exact durable file handle and content digest."""
    observed = _lookup_restore_destination(catalog, root_id, relative_path)
    if observed is None or observed.get("conflict") is True:
        return False
    if not _restore_observation_matches(journal, observed):
        return False
    published_identity = journal.get("publishedIdentity")
    if _identity_pair(published_identity):
        return observed.get("identity") == (int(published_identity[0]), int(published_identity[1]))
    return True


def _identity_pair(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    )


class _FileHandleHeader(ctypes.Structure):
    _fields_ = [("handle_bytes", ctypes.c_uint), ("handle_type", ctypes.c_int)]


def _file_handle_at(parent_fd: int, name: str) -> dict[str, object]:
    """Return the kernel's generation-bearing handle without following links."""
    try:
        function = ctypes.CDLL(None, use_errno=True).name_to_handle_at
    except AttributeError as exc:
        raise TrashError("unsupported_metadata") from exc
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_FileHandleHeader),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    function.restype = ctypes.c_int
    encoded_name = os.fsencode(name)
    capacity = 128
    while capacity <= 4096:
        storage = ctypes.create_string_buffer(ctypes.sizeof(_FileHandleHeader) + capacity)
        header_pointer = ctypes.cast(storage, ctypes.POINTER(_FileHandleHeader))
        header_pointer.contents.handle_bytes = capacity
        mount_id = ctypes.c_int()
        ctypes.set_errno(0)
        result = function(parent_fd, encoded_name, header_pointer, ctypes.byref(mount_id), 0)
        if result == 0:
            header = header_pointer.contents
            value = ctypes.string_at(
                ctypes.addressof(header_pointer.contents) + ctypes.sizeof(_FileHandleHeader),
                header.handle_bytes,
            )
            return {"type": int(header.handle_type), "value": value.hex()}
        error = ctypes.get_errno()
        if error == errno.EOVERFLOW:
            capacity = max(capacity * 2, int(header_pointer.contents.handle_bytes))
            continue
        raise TrashError("unsupported_metadata") from OSError(error, os.strerror(error), name)
    raise TrashError("unsupported_metadata")


def _payload_handle(directory: Path) -> dict[str, object]:
    entry = _open_directory(directory)
    try:
        return _file_handle_at(entry, "payload")
    finally:
        os.close(entry)


def _same_file_handle(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_type = left.get("type")
    right_type = right.get("type")
    left_value = left.get("value")
    right_value = right.get("value")
    if (
        not isinstance(left_type, int)
        or isinstance(left_type, bool)
        or not isinstance(right_type, int)
        or isinstance(right_type, bool)
        or not isinstance(left_value, str)
        or not isinstance(right_value, str)
    ):
        return False
    return left_type == right_type and left_value == right_value


def _lookup_restore_destination(
    catalog: RootCatalog, root_id: str, relative_path: str
) -> dict[str, object] | None:
    """Return the live object at a restore address, or None when it is absent."""
    try:
        opened = open_delete_source(catalog, root_id, relative_path)
    except AddressRejected as exc:
        if exc.code == "not_found":
            return None
        return {"conflict": True}
    try:
        if stat.S_ISDIR(opened.mode):
            kind = "directory"
        elif stat.S_ISREG(opened.mode):
            kind = "file"
        else:
            kind = "other"
        return {
            "kind": kind,
            "digest": _content_digest(opened.parent_fd, opened.name),
            "identity": (opened.device, opened.inode),
            "handle": _file_handle_at(opened.parent_fd, opened.name),
        }
    finally:
        opened.close()


def _restore_observation_matches(journal: dict[str, object], observed: dict[str, object]) -> bool:
    digest = journal.get("contentDigest")
    handle = observed.get("handle")
    identity = observed.get("identity")
    if (
        not isinstance(digest, str)
        or not isinstance(handle, dict)
        or not isinstance(identity, tuple)
        or len(identity) != 2
    ):
        return False
    if observed.get("kind") != journal.get("kind") or observed.get("digest") != digest:
        return False
    identity_hint = (
        journal.get("stagedIdentity")
        if journal.get("crossFilesystem") is True
        else journal.get("payloadIdentity")
    )
    if not _identity_pair(identity_hint) or identity != (int(identity_hint[0]), int(identity_hint[1])):
        return False
    if not _same_file_handle(handle, journal.get("restoreHandle")):
        return False
    published_handle = journal.get("publishedHandle")
    return published_handle is None or _same_file_handle(handle, published_handle)


def _destination_hints(catalog: RootCatalog, root_id: str, relative_path: str) -> dict[str, tuple[int, int]]:
    opened = open_delete_source(catalog, root_id, relative_path)
    try:
        hints: dict[str, tuple[int, int]] = {}

        def visit(parent_fd: int, name: str, relative: str) -> None:
            info = os.lstat(name, dir_fd=parent_fd)
            hints[relative] = (info.st_dev, info.st_ino)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
                try:
                    for child in os.listdir(fd):
                        child_relative = child if relative == "" else relative + "/" + child
                        visit(fd, child, child_relative)
                finally:
                    os.close(fd)

        visit(opened.parent_fd, opened.name, "")
        return hints
    finally:
        opened.close()


def _collect_marks(
    document: dict[str, object],
    root_id: str,
    source_path: str,
    kind: str,
) -> list[dict[str, object]] | None:
    del root_id
    source_path = _absolute(source_path)
    prefix = source_path + "/"
    records = []
    items = document["items"]
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        if path == source_path:
            relative = ""
        elif kind == "directory" and path.startswith(prefix):
            relative = path[len(prefix) :]
        else:
            continue
        if not _item_marked(item):
            continue
        records.append(
            {
                "relativePath": relative,
                "labelIds": list(item["labelIds"]),
                "favorite": bool(item["favorite"]),
                "emoji": item["emoji"],
            }
        )
    if not records:
        return None
    records.sort(key=lambda record: str(record["relativePath"]))
    return records


def _item_marked(item: dict[str, object]) -> bool:
    return bool(item.get("favorite")) or bool(item.get("labelIds")) or item.get("emoji") is not None


def _strip_items(document: dict[str, object], root_id: str, source_path: str, kind: str) -> None:
    del root_id
    source_path = _absolute(source_path)
    prefix = source_path + "/"
    items = document["items"]
    if not isinstance(items, list):
        return
    kept = []
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        path = item.get("path")
        matches = isinstance(path, str) and (
            path == source_path or (kind == "directory" and path.startswith(prefix))
        )
        if not matches:
            kept.append(item)
    document["items"] = kept


def _metadata_conflict(
    document: dict[str, object],
    root_id: str,
    base: str,
    kind: str,
    records: object,
) -> bool:
    items = document["items"]
    if not isinstance(items, list):
        return True
    del root_id
    base = _absolute(base)
    paths = [item["path"] for item in items if isinstance(item, dict)]
    if base in paths:
        return True
    prefix = base + "/"
    if kind == "directory" and any(isinstance(path, str) and path.startswith(prefix) for path in paths):
        return True
    if isinstance(records, list):
        for record in records:
            if isinstance(record, dict) and _mapped(base, str(record.get("relativePath"))) in paths:
                return True
    return False


def _marks_match(document: dict[str, object], root_id: str, base: str, records: list[object]) -> bool:
    items = document["items"]
    if not isinstance(items, list):
        return False
    del root_id
    base = _absolute(base)
    found = {
        item["path"]: item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    for record in records:
        if not isinstance(record, dict):
            return False
        path = _mapped(base, str(record.get("relativePath")))
        item = found.get(path)
        if not isinstance(item, dict):
            return False
        if item.get("labelIds") != record.get("labelIds") or item.get("favorite") != record.get("favorite"):
            return False
        if item.get("emoji") != record.get("emoji"):
            return False
    return True


def _apply_marks(
    document: dict[str, object],
    root_id: str,
    base: str,
    records: list[object],
    hints: dict[str, tuple[int, int]],
) -> None:
    del root_id
    base = _absolute(base)
    items = document["items"]
    if not isinstance(items, list):
        raise TrashError("invalid")
    for record in records:
        if not isinstance(record, dict):
            raise TrashError("quarantined")
        relative = str(record["relativePath"])
        device, inode = hints.get(relative, (None, None))
        items.append(
            {
                "path": _mapped(base, relative),
                "labelIds": list(record["labelIds"]),
                "favorite": record["favorite"],
                "emoji": record["emoji"],
                "inode": inode,
                "device": device,
            }
        )


def _strip_exact_marks(document: dict[str, object], root_id: str, base: str, records: list[object]) -> None:
    del root_id
    base = _absolute(base)
    targets = set()
    for record in records:
        if isinstance(record, dict):
            targets.add(_mapped(base, str(record.get("relativePath"))))
    items = document["items"]
    if not isinstance(items, list):
        return
    document["items"] = [
        item
        for item in items
        if not (
            isinstance(item, dict)
            and item.get("path") in targets
        )
    ]


def _mapped(base: str, relative: str) -> str:
    if relative == "":
        return base
    return base + "/" + relative
