"""Managed Markdown image uploads, conservative collection, and pending state."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import quote
from zoneinfo import ZoneInfo

from hopper_files.editor import (
    EditorError,
    bind_document_version,
    discard_document_rewrite,
    prepare_document_rewrite,
    publish_document_rewrite,
    read_document,
    save_document,
)
from hopper_files.image_refs import (
    ImageIdentity,
    ReferenceScanError,
    image_destination_spans,
    relative_markdown_destination,
    _parse_markdown,
    scan_corpus,
    scan_document,
)
from hopper_files.roots import (
    AddressRejected,
    DocumentRoot,
    RootCatalog,
    create_directory_exclusive,
    inspect_address,
    list_directory,
    open_delete_source,
    open_regular,
    root_record,
)
from hopper_files.state import StateError, atomic_write, read_signing_secret
from hopper_files.trash import TrashError, TrashStore

MAX_MANAGED_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MOVE_MARKDOWN_BYTES = 200 * 1024 * 1024
MAX_MOVE_REWRITE_BYTES = 400 * 1024 * 1024
MAX_MOVE_DOCUMENTS = 4096
_IMAGE_TYPES = {
    "png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    "jpg": ("image/jpeg", b"\xff\xd8\xff"),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", b"RIFF"),
}


class ImageError(ValueError):
    def __init__(self, code: str, **details: object) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


class ImageStore:
    """One instance's pending upload registry and collection coordinator."""

    def __init__(
        self,
        state_directory: Path,
        instance_id: str,
        time_zone: str,
        trash: TrashStore,
        config_path: Path | None = None,
    ) -> None:
        self.state_directory = Path(state_directory).resolve()
        self.instance_id = instance_id
        self.time_zone = ZoneInfo(time_zone)
        self.trash = trash
        self.config_path = Path(config_path) if config_path is not None else None

    @contextmanager
    def lock(self) -> Iterator[None]:
        self._prepare()
        path = self.state_directory / "images.lock"
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise StateError("image coordinator lock is invalid")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def save(
        self,
        catalog: RootCatalog,
        root_id: str,
        path: str,
        base_version: object,
        content: object,
        now: float,
    ) -> dict[str, object]:
        """Serialize an editor save with image uploads and safe collection."""
        with self.lock():
            before: frozenset[ImageIdentity] = frozenset()
            is_markdown = path.lower().endswith((".md", ".markdown"))
            scan_complete = True
            generation = catalog.generation
            if is_markdown:
                try:
                    if not self._configured_generation_matches(generation):
                        raise ReferenceScanError("document root generation changed")
                    before = scan_document(catalog, root_id, path).references
                except ReferenceScanError:
                    scan_complete = False
            saved = save_document(
                catalog, root_id, path, base_version, content,
                self.instance_id, self.state_directory,
            )
            outcome: dict[str, object] = {"removed": 0, "inconclusive": not scan_complete}
            if is_markdown:
                try:
                    current = _parse_markdown(catalog, root_id, path, saved["content"].encode("utf-8"))
                    parsed_current = True
                except (ReferenceScanError, UnicodeError):
                    current = frozenset()
                    parsed_current = False
                    outcome["inconclusive"] = True
                if parsed_current:
                    try:
                        self._confirm_pending_locked(root_id, path, current)
                    except StateError:
                        # A lost pending-state update must never turn an image
                        # into a collection candidate; retry can resolve it.
                        outcome["inconclusive"] = True
                candidates = {
                    target for target in before - current
                    if _managed_attachment(target)
                } if parsed_current and scan_complete else set()
                if candidates:
                    try:
                        corpus = scan_corpus(catalog)
                        if not self._configured_generation_matches(generation):
                            raise ReferenceScanError("document root generation changed")
                        live = {identity for values in corpus.references.values() for identity in values}
                        pending = _pending_identities(self._read_locked()["items"])
                        removable = candidates - live - pending
                        # HF-IMG-005: a note whose references are unknown and that
                        # names a candidate keeps that candidate.
                        undetermined = {
                            target for target in removable
                            if corpus.blocking(catalog, {target.path.rsplit("/", 1)[-1]})
                        }
                        if undetermined:
                            outcome["inconclusive"] = True
                        removable -= undetermined
                        removed = 0
                        for target in sorted(removable):
                            if not self._configured_generation_matches(generation):
                                raise ReferenceScanError("document root generation changed")
                            if not _is_managed_image(catalog, target):
                                continue
                            try:
                                deleted = self.trash.delete(
                                    catalog, target.root_id, target.path,
                                    now, reason="image_collection",
                                )
                                if not self._configured_generation_matches(generation):
                                    try:
                                        self.trash.restore(catalog, str(deleted["id"]), now)
                                    except TrashError:
                                        pass
                                    raise ReferenceScanError("document root generation changed")
                                removed += 1
                            except TrashError as exc:
                                if exc.code not in {"not_found", "unavailable"}:
                                    raise
                        outcome["removed"] = removed
                    except (ReferenceScanError, AddressRejected, OSError, StateError, TrashError):
                        outcome["inconclusive"] = True
            saved["imageCollection"] = outcome
            return saved

    def upload_action(
        self,
        catalog: RootCatalog,
        root_id: str,
        note_path: str,
        now: float,
        extension: str,
        digest: str,
        size: int,
    ) -> dict[str, object]:
        """Build the existing authenticated operation-store upload manifest."""
        if not isinstance(root_record(catalog, root_id), DocumentRoot):
            raise ImageError("not_found")
        note_id = _note_id(note_path)
        subject = "img-" + note_id
        # HF-IMG-001: the visible attachments/ directory beside the note.
        attachments = attachment_directory(note_path)
        try:
            create_directory_exclusive(catalog, root_id, attachments)
        except AddressRejected as create_error:
            if create_error.code != "conflict":
                raise ImageError(create_error.code) from create_error
            inspect = inspect_address(catalog, root_id, attachments, operation="read")
            if not inspect.allowed or not inspect.is_directory:
                raise ImageError("conflict")
        # The API's existing upload token binds the full action and byte digest.
        return {
            "action": "upload",
            "destination": {"rootId": root_id, "path": attachments},
            "nameMode": "generated",
            "files": [{
                "subject": subject,
                "extension": extension,
                "contentDigest": digest,
                "contentSize": size,
            }],
        }

    def plan_move(
        self,
        catalog: RootCatalog,
        source: dict[str, str],
        destination: dict[str, str],
    ) -> dict[str, object]:
        """Build a complete, content-free plan for moving one file or directory."""
        from hopper_files.editor import EditorError
        from hopper_files.files import OperationError, ZIP_SOURCE_MAX_BYTES, ZIP_SOURCE_MAX_ENTRIES, _copy_source_entries

        generation = catalog.generation
        if not isinstance(source, dict) or not isinstance(destination, dict):
            raise OperationError("invalid")
        source_root = source.get("rootId")
        source_path = source.get("path")
        destination_root = destination.get("rootId")
        destination_path = destination.get("path")
        if not all(isinstance(value, str) and value for value in (source_root, source_path, destination_root, destination_path)):
            raise OperationError("invalid")
        if (source_root, source_path) == (destination_root, destination_path):
            raise OperationError("conflict")
        source_record = root_record(catalog, source_root)
        destination_record = root_record(catalog, destination_root)
        if not isinstance(source_record, DocumentRoot) or not source_record.enabled:
            raise OperationError("unavailable")
        if not isinstance(destination_record, DocumentRoot) or not destination_record.enabled:
            raise OperationError("unavailable")
        inspection = inspect_address(catalog, source_root, source_path, operation="read")
        if not inspection.allowed or not (inspection.is_regular or inspection.is_directory):
            raise OperationError("forbidden")
        kind = "directory" if inspection.is_directory else "file"
        if kind == "directory" and source_root == destination_root and destination_path.startswith(source_path + "/"):
            raise OperationError("conflict")
        try:
            move_checks = self.trash.preflight_move(
                catalog, source_root, source_path, destination_root, destination_path, 0
            )
        except TrashError as exc:
            raise OperationError(exc.code) from exc
        if move_checks.get("kind") != kind:
            raise OperationError("conflict")
        size = int(move_checks["size"])
        if size > ZIP_SOURCE_MAX_BYTES:
            raise OperationError("limit")
        directory_snapshot: str | None = None
        if kind == "directory":
            identity, directory_snapshot, entries, directory_size = _copy_source_entries(
                catalog, {"rootId": source_root, "path": source_path}
            )
            if (
                list(identity) != move_checks["identity"]
                or directory_size != size
                or len(entries) > ZIP_SOURCE_MAX_ENTRIES
            ):
                raise OperationError("conflict")

        try:
            corpus = scan_corpus(catalog)
        except ReferenceScanError as exc:
            raise OperationError("conflict") from exc
        moved_names = {source_path.rsplit("/", 1)[-1]}
        if kind == "directory":
            moved_names |= {str(entry["relativePath"]).rsplit("/", 1)[-1] for entry in entries}
        if corpus.blocking(catalog, moved_names, {source_path}):
            # HF-FILE-002: an incomplete required reference scan refuses the move.
            raise OperationError("conflict")
        all_documents = corpus.references
        documents: list[dict[str, object]] = []
        rewritten_bytes = 0
        for identity in sorted(all_documents):
            references = all_documents[identity]
            moved_note = _map_move_identity(
                identity, source_root, source_path, destination_root, destination_path, kind
            )
            changed_reference = any(
                _map_move_identity(
                    target, source_root, source_path, destination_root, destination_path, kind
                ) is not None
                for target in references
            )
            if moved_note is None and not changed_reference:
                continue
            if len(documents) >= MAX_MOVE_DOCUMENTS:
                raise OperationError("limit")
            new_root, new_path = (
                (moved_note.root_id, moved_note.path)
                if moved_note is not None
                else (identity.root_id, identity.path)
            )
            try:
                snapshot = scan_document(catalog, identity.root_id, identity.path, keep_content=True)
                if snapshot.content is None:
                    raise ReferenceScanError("Markdown snapshot content is unavailable")
                loaded = read_document(
                    catalog, identity.root_id, identity.path,
                    self.instance_id, self.state_directory,
                    max_bytes=MAX_MOVE_MARKDOWN_BYTES,
                )
                content = loaded["content"]
                if (
                    not isinstance(content, str)
                    or hashlib.sha256(content.encode("utf-8")).hexdigest() != snapshot.digest
                ):
                    raise ReferenceScanError("Markdown changed during move planning")
                rewritten = _rewrite_move_document(
                    catalog, identity.root_id, identity.path, new_root, new_path, content,
                    source_root, source_path, destination_root, destination_path, kind,
                )
            except (AddressRejected, EditorError, ReferenceScanError, UnicodeError) as exc:
                if isinstance(exc, EditorError) and exc.code == "limit":
                    raise OperationError("limit") from exc
                raise OperationError("conflict") from exc
            before_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            after_digest = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
            if before_digest != after_digest:
                rewritten_bytes += len(rewritten.encode("utf-8"))
                if rewritten_bytes > MAX_MOVE_REWRITE_BYTES:
                    raise OperationError("limit")
            documents.append({
                "rootId": identity.root_id,
                "path": identity.path,
                "newRootId": new_root,
                "newPath": new_path,
                "version": loaded["version"],
                "beforeDigest": before_digest,
                "afterDigest": after_digest,
                "size": len(content.encode("utf-8")),
                "moved": moved_note is not None,
            })
        if catalog.generation != generation or not self._configured_generation_matches(generation):
            raise OperationError("conflict")
        return {
            "version": 1,
            "generation": generation,
            "source": {
                "rootId": source_root,
                "path": source_path,
                "kind": kind,
                "identity": move_checks["identity"],
                "digest": move_checks["digest"],
                "size": size,
                "directoryVersion": directory_snapshot,
                "crossFilesystem": move_checks["crossFilesystem"],
                "linkedFiles": move_checks["linkedFiles"],
            },
            "destination": {"rootId": destination_root, "path": destination_path},
            "size": size,
            "documents": documents,
        }

    def execute_move(
        self,
        operations: object,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
        now: float,
    ) -> dict[str, object]:
        """Journal one move, rewrite staged Markdown, and coordinate open buffers."""
        from hopper_files.buffers import BufferConflict
        from hopper_files.editor import EditorError
        from hopper_files.files import OperationError

        current = self.plan_move(catalog, action["source"], action["destination"])
        if current != action.get("movePlan"):
            raise OperationError("conflict")
        self._prepare_move_storage()
        move_id = uuid.uuid4().hex
        trash_id = str(uuid.uuid4())
        move_directory = self.state_directory / "attachments" / "moves" / move_id
        try:
            os.mkdir(move_directory, 0o700)
            parent_fd = os.open(move_directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError as exc:
            raise OperationError("unavailable") from exc
        journal: dict[str, object] = {
            "version": 1,
            "instanceId": self.instance_id,
            "moveId": move_id,
            "tokenId": record.get("tokenId"),
            "index": index,
            "phase": "staging",
            "trashId": trash_id,
            "plan": current,
            "stages": {},
            "bufferMoveId": None,
            "restoreHandle": None,
            "publishedObject": None,
        }
        self._write_move_journal(move_directory, journal)
        buffer_move_id: str | None = None
        try:
            for number, document in enumerate(current["documents"]):
                if document["beforeDigest"] == document["afterDigest"]:
                    continue
                rewritten = self._read_rewritten_move_document(catalog, document, current)
                stage_id = f"document-{number:04d}"
                temporary_name = ".hopper-stage-" + secrets.token_hex(16) + ".tmp"
                journal["stages"][stage_id] = {
                    "rootId": document["newRootId"],
                    "path": document["newPath"],
                    "sourceRootId": document["rootId"],
                    "sourcePath": document["path"],
                    "temporaryName": temporary_name,
                    "beforeDigest": document["beforeDigest"],
                    "afterDigest": document["afterDigest"],
                    "size": len(rewritten.encode("utf-8")),
                    "ready": False,
                }
                self._write_move_journal(move_directory, journal)
                prepared = prepare_document_rewrite(
                    catalog,
                    document["rootId"],
                    document["path"],
                    document["newRootId"],
                    document["newPath"],
                    document["version"],
                    rewritten,
                    self.instance_id,
                    self.state_directory,
                    temporary_name,
                    max_bytes=MAX_MOVE_MARKDOWN_BYTES,
                )
                prepared["ready"] = True
                journal["stages"][stage_id] = prepared
                self._write_move_journal(move_directory, journal)
            journal["phase"] = "staged"
            self._write_move_journal(move_directory, journal)
            latest = self.plan_move(catalog, action["source"], action["destination"])
            if latest != current or not self._configured_generation_matches(catalog.generation):
                raise OperationError("conflict")
            targets = [
                {
                    "rootId": item["rootId"],
                    "path": item["path"],
                    "version": item["version"],
                    "newRootId": item["newRootId"],
                    "newPath": item["newPath"],
                }
                for item in current["documents"]
            ]
            buffer_move_id = move_id
            journal["bufferMoveId"] = buffer_move_id
            journal["phase"] = "buffer_preparing"
            self._write_move_journal(move_directory, journal)
            try:
                buffer_move_id = getattr(operations, "buffer_registry").prepare(
                    targets, now, identifier=move_id
                )
            except (BufferConflict, StateError) as exc:
                raise OperationError("conflict") from exc
            journal["bufferMoveId"] = buffer_move_id
            journal["phase"] = "held"
            self._write_move_journal(move_directory, journal)
            latest = self.plan_move(catalog, action["source"], action["destination"])
            if latest != current or not self._configured_generation_matches(catalog.generation):
                raise OperationError("conflict")

            def record_intent(value: dict[str, object]) -> None:
                journal["restoreHandle"] = value.get("handle")
                if isinstance(value.get("treeFileHandles"), dict):
                    journal["restoreTreeFileHandles"] = value["treeFileHandles"]
                journal["phase"] = "renaming" if journal.get("inPlace") else "restoring"
                self._write_move_journal(move_directory, journal)

            def record_publication(value: dict[str, object]) -> None:
                journal["publishedObject"] = value
                self._bind_restored_move_versions(catalog, current, journal)
                journal["phase"] = "object_published"
                self._write_move_journal(move_directory, journal)

            moved_in_place = None
            if current["source"]["crossFilesystem"] is False:
                # HF-FILE-002: one rename keeps the inode, links, and metadata.
                journal["inPlace"] = True
                journal["phase"] = "renaming"
                self._write_move_journal(move_directory, journal)
                moved_in_place = self.trash.move_in_place(
                    catalog,
                    current["source"]["rootId"],
                    current["source"]["path"],
                    current["destination"]["rootId"],
                    current["destination"]["path"],
                    now,
                    identifier=trash_id,
                    on_intent=record_intent,
                    on_publication=record_publication,
                    tree_file_paths=self._move_tree_file_paths(current),
                )
                if moved_in_place is None:
                    journal["inPlace"] = False
                    journal["restoreHandle"] = None
                    journal.pop("restoreTreeFileHandles", None)
            if moved_in_place is None:
                # Across filesystems the trash legs copy, so other links of a
                # multiply linked file keep the original inode.
                journal["separatedLinks"] = int(current["source"].get("linkedFiles") or 0)
                journal["phase"] = "deleting"
                self._write_move_journal(move_directory, journal)
                self.trash.delete(
                    catalog,
                    current["source"]["rootId"],
                    current["source"]["path"],
                    now,
                    reason="move",
                    identifier=trash_id,
                )
                journal["phase"] = "trashed"
                self._write_move_journal(move_directory, journal)
                self.trash.restore(
                    catalog,
                    trash_id,
                    now,
                    current["destination"]["path"],
                    current["destination"]["rootId"],
                    on_intent=record_intent,
                    on_publication=record_publication,
                    tree_file_paths=self._move_tree_file_paths(current),
                )
            journal["phase"] = "rewriting"
            self._write_move_journal(move_directory, journal)
            for stage_name, staged in journal["stages"].items():
                self._apply_move_document(
                    catalog,
                    staged,
                    self._move_document_expected_handle(current, journal, staged),
                )
                staged["saved"] = True
                self._write_move_journal(move_directory, journal)
            return self._finish_move(
                operations, record, index, journal, move_directory, buffer_move_id, now
            )
        except OperationError:
            if journal.get("phase") in {
                "renaming", "deleting", "trashed", "restoring", "object_published", "rewriting", "indeterminate",
            }:
                journal["phase"] = "indeterminate"
                self._write_move_journal(move_directory, journal)
                return _move_result(current, trash_id, "indeterminate", "conflict")
            if buffer_move_id is not None:
                getattr(operations, "buffer_registry").release(buffer_move_id, now)
            self._discard_move_stages(catalog, journal)
            self._remove_move_directory(move_directory)
            raise
        except (TrashError, EditorError, AddressRejected, StateError, OSError, ReferenceScanError) as exc:
            # Once delete/restore has begun, keep the journal and held buffers.
            if journal.get("phase") in {
                "deleting", "trashed", "restoring", "object_published", "rewriting", "indeterminate",
            }:
                journal["phase"] = "indeterminate"
                self._write_move_journal(move_directory, journal)
                return _move_result(current, trash_id, "indeterminate", _safe_move_error(exc))
            if journal.get("phase") == "renaming" and self._source_unmoved(catalog, current):
                # The in-place rename refused before publication; nothing moved.
                if buffer_move_id is not None:
                    getattr(operations, "buffer_registry").release(buffer_move_id, now)
                self._discard_move_stages(catalog, journal)
                self._remove_move_directory(move_directory)
                code = exc.code if isinstance(exc, (TrashError, EditorError, AddressRejected)) else "unavailable"
                raise OperationError(code) from exc
            if journal.get("phase") == "renaming":
                journal["phase"] = "indeterminate"
                self._write_move_journal(move_directory, journal)
                return _move_result(current, trash_id, "indeterminate", _safe_move_error(exc))
            if buffer_move_id is not None:
                getattr(operations, "buffer_registry").release(buffer_move_id, now)
            self._discard_move_stages(catalog, journal)
            self._remove_move_directory(move_directory)
            code = exc.code if isinstance(exc, (TrashError, EditorError, AddressRejected)) else "unavailable"
            raise OperationError(code) from exc

    def _source_unmoved(self, catalog: RootCatalog, plan: dict[str, object]) -> bool:
        """Return whether the planned source object is still at its address."""
        source = plan["source"]
        try:
            opened = open_delete_source(catalog, source["rootId"], source["path"])
        except (AddressRejected, OSError):
            return False
        try:
            return [opened.device, opened.inode] == source["identity"]
        finally:
            opened.close()

    def _read_rewritten_move_document(
        self,
        catalog: RootCatalog,
        document: dict[str, object],
        plan: dict[str, object],
    ) -> str:
        from hopper_files.editor import EditorError

        snapshot = scan_document(catalog, document["rootId"], document["path"], keep_content=True)
        loaded = read_document(
            catalog, document["rootId"], document["path"],
            self.instance_id, self.state_directory,
            max_bytes=MAX_MOVE_MARKDOWN_BYTES,
        )
        content = loaded["content"]
        if (
            snapshot.content is None
            or not isinstance(content, str)
            or snapshot.digest != document["beforeDigest"]
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != document["beforeDigest"]
            or loaded["version"] != document["version"]
        ):
            raise EditorError("conflict")
        rewritten = _rewrite_move_document(
            catalog,
            document["rootId"],
            document["path"],
            document["newRootId"],
            document["newPath"],
            content,
            plan["source"]["rootId"],
            plan["source"]["path"],
            plan["destination"]["rootId"],
            plan["destination"]["path"],
            plan["source"]["kind"],
        )
        if hashlib.sha256(rewritten.encode("utf-8")).hexdigest() != document["afterDigest"]:
            raise EditorError("conflict")
        return rewritten

    def _apply_move_document(
        self,
        catalog: RootCatalog,
        staged: dict[str, object],
        expected_handle: object,
    ) -> None:
        from hopper_files.editor import EditorError

        saved = publish_document_rewrite(
            catalog,
            staged,
            expected_handle,
            self.instance_id,
            self.state_directory,
            max_bytes=MAX_MOVE_MARKDOWN_BYTES,
        )
        if hashlib.sha256(str(saved["content"]).encode("utf-8")).hexdigest() != staged["afterDigest"]:
            raise EditorError("indeterminate")
        staged["savedVersion"] = saved["savedVersion"]

    def _move_document_expected_handle(
        self,
        plan: dict[str, object],
        journal: dict[str, object],
        staged: dict[str, object],
    ) -> object:
        source = plan["source"]
        if (
            source.get("kind") == "file"
            and staged.get("sourceRootId") == source.get("rootId")
            and staged.get("sourcePath") == source.get("path")
        ):
            published = journal.get("publishedObject")
            if isinstance(published, dict) and published.get("handle") is not None:
                return published["handle"]
            if journal.get("restoreHandle") is not None:
                return journal["restoreHandle"]
        if (
            source.get("kind") == "directory"
            and staged.get("sourceRootId") == source.get("rootId")
            and isinstance(source.get("path"), str)
            and isinstance(staged.get("sourcePath"), str)
        ):
            prefix = str(source["path"]).rstrip("/") + "/"
            note_path = str(staged["sourcePath"])
            if note_path.startswith(prefix):
                published = journal.get("publishedObject")
                tree_handles = published.get("treeFileHandles") if isinstance(published, dict) else None
                if not isinstance(tree_handles, dict):
                    tree_handles = journal.get("restoreTreeFileHandles")
                note_handle = tree_handles.get(note_path[len(prefix):]) if isinstance(tree_handles, dict) else None
                if isinstance(note_handle, dict):
                    return note_handle
        return staged.get("targetHandle")

    def _move_document_is_relocated(
        self,
        plan: dict[str, object],
        staged: dict[str, object],
    ) -> bool:
        source = plan["source"]
        if (
            source.get("kind") == "file"
            and staged.get("sourceRootId") == source.get("rootId")
            and staged.get("sourcePath") == source.get("path")
        ):
            return True
        if (
            source.get("kind") == "directory"
            and staged.get("sourceRootId") == source.get("rootId")
            and isinstance(source.get("path"), str)
            and isinstance(staged.get("sourcePath"), str)
        ):
            prefix = str(source["path"]).rstrip("/") + "/"
            return str(staged["sourcePath"]).startswith(prefix)
        return False

    def _bind_restored_move_versions(
        self,
        catalog: RootCatalog,
        plan: dict[str, object],
        journal: dict[str, object],
    ) -> None:
        stages = journal.get("stages")
        if not isinstance(stages, dict):
            return
        for staged in stages.values():
            if not isinstance(staged, dict) or not self._move_document_is_relocated(plan, staged):
                continue
            if isinstance(staged.get("restoredTargetVersion"), str):
                continue
            root_id, path, digest = staged.get("rootId"), staged.get("path"), staged.get("beforeDigest")
            if not all(isinstance(value, str) for value in (root_id, path, digest)):
                raise EditorError("conflict")
            expected_handle = self._move_document_expected_handle(plan, journal, staged)
            staged["restoredTargetVersion"] = bind_document_version(
                catalog,
                root_id,
                path,
                expected_handle,
                digest,
                self.instance_id,
                self.state_directory,
                max_bytes=MAX_MOVE_MARKDOWN_BYTES,
            )

    def _move_tree_file_paths(self, plan: dict[str, object]) -> set[str]:
        source = plan.get("source")
        documents = plan.get("documents")
        if not isinstance(source, dict) or source.get("kind") != "directory" or not isinstance(documents, list):
            return set()
        root_id = source.get("rootId")
        path = source.get("path")
        if not isinstance(root_id, str) or not isinstance(path, str):
            return set()
        prefix = path.rstrip("/") + "/"
        result: set[str] = set()
        for document in documents:
            if not isinstance(document, dict) or document.get("rootId") != root_id:
                continue
            source_path = document.get("path")
            if (
                not isinstance(source_path, str)
                or not source_path.startswith(prefix)
                or document.get("beforeDigest") == document.get("afterDigest")
            ):
                continue
            result.add(source_path[len(prefix):])
        return result

    def _discard_move_stages(self, catalog: RootCatalog, journal: dict[str, object]) -> None:
        stages = journal.get("stages")
        if not isinstance(stages, dict):
            return
        for staged in stages.values():
            if isinstance(staged, dict) and staged.get("ready") is True:
                discard_document_rewrite(catalog, staged)

    def _finish_move(
        self,
        operations: object,
        record: dict[str, object],
        index: int,
        journal: dict[str, object],
        move_directory: Path,
        buffer_move_id: str | None,
        now: float,
    ) -> dict[str, object]:
        from hopper_files.files import OperationError

        result = _move_result(
            journal["plan"], journal["trashId"], "committed", None, int(journal.get("separatedLinks") or 0)
        )
        buffers = getattr(operations, "buffer_registry")
        if buffer_move_id is not None:
            buffers.finish(buffer_move_id, {}, now)
        items = record.get("items")
        if not isinstance(items, list) or not 0 <= int(journal["index"]) < len(items):
            raise StateError("move operation record is invalid")
        items[int(journal["index"])] = result
        getattr(operations, "_write_record_locked")(record)
        journal["phase"] = "complete"
        self._write_move_journal(move_directory, journal)
        try:
            self._remove_move_directory(move_directory)
        except OSError:
            pass
        return result

    def recover_moves(self, catalog: RootCatalog, now: float, operations: object, buffers: object) -> None:
        """Resume or classify outer move journals after trash recovery."""
        from hopper_files.editor import EditorError
        from hopper_files.files import OperationError

        with self.lock():
            self._prepare_move_storage()
            self.trash.recover(catalog, now)
            for move_directory, journal in self._move_journals():
                plan = journal["plan"]
                source = plan["source"]
                destination = plan["destination"]
                phase = journal["phase"]
                buffer_move_id = journal.get("bufferMoveId")
                if phase in {"staging", "staged", "held", "buffer_preparing"}:
                    if _trash_entry_exists(self.trash, journal["trashId"], catalog, now):
                        journal["phase"] = "trashed"
                        self._write_move_journal(move_directory, journal)
                    else:
                        if isinstance(buffer_move_id, str):
                            buffers.release(buffer_move_id, now)
                        self._complete_recovered(
                            operations, journal,
                            _move_result(plan, journal["trashId"], "failed", "conflict"),
                            now,
                        )
                        self._discard_move_stages(catalog, journal)
                        self._remove_move_directory(move_directory)
                        continue
                if journal["phase"] == "deleting" and not _trash_entry_exists(
                    self.trash, journal["trashId"], catalog, now
                ):
                    if isinstance(buffer_move_id, str):
                        buffers.release(buffer_move_id, now)
                    self._complete_recovered(
                        operations, journal,
                        _move_result(plan, journal["trashId"], "failed", "conflict"),
                        now,
                    )
                    self._discard_move_stages(catalog, journal)
                    self._remove_move_directory(move_directory)
                    continue
                published_object = journal.get("publishedObject")
                expected_handle = (
                    published_object.get("handle")
                    if isinstance(published_object, dict)
                    else journal.get("restoreHandle")
                )
                source_markdown = next(
                    (
                        item for item in plan["documents"]
                        if item["rootId"] == source["rootId"] and item["path"] == source["path"]
                    ),
                    None,
                )
                moved = False
                if source["kind"] == "file" and isinstance(source_markdown, dict):
                    moved_stage = next(
                        (
                            value for value in journal.get("stages", {}).values()
                            if isinstance(value, dict)
                            and value.get("sourceRootId") == source["rootId"]
                            and value.get("sourcePath") == source["path"]
                        ),
                        None,
                    )
                    candidates = []
                    if isinstance(moved_stage, dict):
                        if expected_handle is not None:
                            candidates.append((expected_handle, source_markdown["beforeDigest"]))
                        candidates.append((moved_stage.get("stageHandle"), source_markdown["afterDigest"]))
                    elif expected_handle is not None:
                        candidates.append((expected_handle, source_markdown["beforeDigest"]))
                    try:
                        loaded = read_document(
                            catalog,
                            destination["rootId"],
                            destination["path"],
                            self.instance_id,
                            self.state_directory,
                            max_bytes=MAX_MOVE_MARKDOWN_BYTES,
                        )
                        observed_digest = hashlib.sha256(str(loaded["content"]).encode("utf-8")).hexdigest()
                        moved = any(
                            handle is not None
                            and observed_digest == digest
                            and self.trash.verify_move_target(
                                catalog,
                                destination["rootId"],
                                destination["path"],
                                handle=handle,
                            ) is not None
                            for handle, digest in candidates
                        )
                    except (AddressRejected, EditorError, OSError):
                        moved = False
                elif expected_handle is not None:
                    observed = self.trash.verify_move_target(
                        catalog, destination["rootId"], destination["path"],
                        handle=expected_handle,
                    )
                    moved = observed is not None
                if not moved and journal.get("inPlace") is True and self._source_unmoved(catalog, plan):
                    # The in-place rename did not happen: nothing to reconcile.
                    if isinstance(buffer_move_id, str):
                        buffers.release(buffer_move_id, now)
                    self._complete_recovered(
                        operations, journal,
                        _move_result(plan, journal["trashId"], "failed", "conflict"),
                        now,
                    )
                    self._discard_move_stages(catalog, journal)
                    self._remove_move_directory(move_directory)
                    continue
                if not moved and _trash_entry_exists(self.trash, journal["trashId"], catalog, now):
                    def record_intent(value: dict[str, object]) -> None:
                        journal["restoreHandle"] = value.get("handle")
                        if isinstance(value.get("treeFileHandles"), dict):
                            journal["restoreTreeFileHandles"] = value["treeFileHandles"]
                        journal["phase"] = "restoring"
                        self._write_move_journal(move_directory, journal)

                    def record_publication(value: dict[str, object]) -> None:
                        journal["publishedObject"] = value
                        self._bind_restored_move_versions(catalog, plan, journal)
                        journal["phase"] = "object_published"
                        self._write_move_journal(move_directory, journal)

                    try:
                        self.trash.restore(
                            catalog,
                            journal["trashId"],
                            now,
                            destination["path"],
                            destination["rootId"],
                            on_intent=record_intent,
                            on_publication=record_publication,
                            tree_file_paths=self._move_tree_file_paths(plan),
                        )
                        moved = True
                    except TrashError:
                        moved = False
                if not moved:
                    journal["phase"] = "indeterminate"
                    self._write_move_journal(move_directory, journal)
                    self._complete_recovered(
                        operations,
                        journal,
                        _move_result(plan, journal["trashId"], "indeterminate", "conflict"),
                        now,
                    )
                    continue
                try:
                    # A crash after TrashStore's rename but before the outer
                    # callback journaled its published identity is recovered
                    # from the durable on_intent handle. Rebind moved notes
                    # only after the destination handle and digest are proven.
                    self._bind_restored_move_versions(catalog, plan, journal)
                    self._write_move_journal(move_directory, journal)
                    journal["phase"] = "rewriting"
                    self._write_move_journal(move_directory, journal)
                    for stage_name, staged in journal["stages"].items():
                        self._apply_move_document(
                            catalog,
                            staged,
                            self._move_document_expected_handle(plan, journal, staged),
                        )
                        staged["saved"] = True
                        self._write_move_journal(move_directory, journal)
                    result = _move_result(
                        plan, journal["trashId"], "committed", None, int(journal.get("separatedLinks") or 0)
                    )
                    if isinstance(buffer_move_id, str):
                        buffers.finish(buffer_move_id, {}, now)
                    self._complete_recovered(operations, journal, result, now)
                    journal["phase"] = "complete"
                    self._write_move_journal(move_directory, journal)
                    try:
                        self._discard_move_stages(catalog, journal)
                        self._remove_move_directory(move_directory)
                    except OSError:
                        pass
                except (AddressRejected, EditorError, OperationError, ReferenceScanError, StateError, OSError) as exc:
                    journal["phase"] = "indeterminate"
                    self._write_move_journal(move_directory, journal)
                    self._complete_recovered(
                        operations,
                        journal,
                        _move_result(plan, journal["trashId"], "indeterminate", _safe_move_error(exc)),
                        now,
                    )

    def _complete_recovered(
        self,
        operations: object,
        journal: dict[str, object],
        item: dict[str, object],
        now: float,
    ) -> None:
        getattr(operations, "complete_recovered_move")(
            journal["tokenId"], int(journal["index"]), item, now
        )

    def _prepare_move_storage(self) -> None:
        self._prepare()
        _ensure_private_directory(self.state_directory / "attachments" / "moves")

    def _write_move_journal(self, directory: Path, journal: dict[str, object]) -> None:
        canonical = json.dumps(
            journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        mac = hmac.new(read_signing_secret(self.state_directory), canonical, hashlib.sha256).hexdigest()
        envelope = json.dumps(
            {"document": journal, "mac": mac},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        atomic_write(directory / "journal.json", envelope)

    def _move_journals(self) -> list[tuple[Path, dict[str, object]]]:
        root = self.state_directory / "attachments" / "moves"
        results: list[tuple[Path, dict[str, object]]] = []
        for entry in sorted(root.iterdir(), key=lambda path: path.name):
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                raw = _read_private_regular(entry / "journal.json", 8 * 1024 * 1024)
                envelope = json.loads(raw.decode("utf-8"))
                document = envelope["document"]
                mac = envelope["mac"]
                canonical = json.dumps(
                    document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                continue
            expected = hmac.new(read_signing_secret(self.state_directory), canonical, hashlib.sha256).hexdigest()
            if (
                not isinstance(document, dict)
                or document.get("version") != 1
                or document.get("instanceId") != self.instance_id
                or document.get("moveId") != entry.name
                or not isinstance(mac, str)
                or not hmac.compare_digest(mac, expected)
                or not isinstance(document.get("plan"), dict)
            ):
                continue
            results.append((entry, document))
        return results

    def _remove_move_directory(self, directory: Path) -> None:
        if directory.is_symlink() or not directory.is_dir():
            return
        info = directory.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StateError("move staging directory permissions are invalid")
        shutil.rmtree(directory)

    def _configured_generation_matches(self, expected: object) -> bool:
        if self.config_path is None:
            return True
        try:
            from hopper_files.config import load_config
            from hopper_files.roots import build_catalog

            config = load_config(self.config_path)
            return build_catalog(config, strict=False).generation == expected
        except Exception:
            return False

    def record_upload(self, root_id: str, note_path: str, destination: str, digest: str, size: int, now: float) -> None:
        with self.lock():
            self.record_upload_locked(root_id, note_path, destination, digest, size, now)

    def record_upload_locked(self, root_id: str, note_path: str, destination: str, digest: str, size: int, now: float) -> None:
        """Record an upload while the caller already holds ``lock()``."""
        document = self._read_locked()
        identity = ImageIdentity(root_id, destination)
        record = {
            "id": secrets.token_hex(16),
            "rootId": root_id,
            "path": destination,
            "noteRootId": root_id,
            "notePath": note_path,
            "digest": digest,
            "size": size,
            "createdAt": int(now),
            "pending": True,
        }
        items = [item for item in document["items"] if ImageIdentity(item["rootId"], item["path"]) != identity]
        items.append(record)
        document["items"] = items
        self._write_locked(document)

    def attachments(self, catalog: RootCatalog, root_id: str, note_path: str, now: float) -> dict[str, object]:
        """List the attachments/ directory beside one note (HF-IMG-001)."""
        del now
        if not isinstance(root_record(catalog, root_id), DocumentRoot):
            raise ImageError("not_found")
        directory = attachment_directory(note_path)
        with self.lock():
            document = self._read_locked()
            inspection = inspect_address(catalog, root_id, directory, operation="read")
            if not inspection.allowed or not inspection.is_directory:
                return {"rootId": root_id, "path": directory, "entries": []}
            try:
                entries = list_directory(catalog, root_id, directory)
            except AddressRejected as exc:
                raise ImageError(exc.code) from exc
            pending = {
                (item["rootId"], item["path"]): item
                for item in document["items"]
                if item["pending"] is True
            }
            result = []
            for entry in entries:
                if entry.kind != "file" or not entry.addressable:
                    continue
                path = directory + "/" + entry.name
                record = pending.get((root_id, path))
                result.append({
                    "name": entry.name,
                    "path": path,
                    "size": entry.size,
                    "pending": record is not None,
                    "noteRootId": record["noteRootId"] if record else None,
                    "notePath": record["notePath"] if record else None,
                })
            return {"rootId": root_id, "path": directory, "entries": result}

    def resolve_pending(self, root_id: str, path: str) -> bool:
        with self.lock():
            document = self._read_locked()
            changed = False
            for item in document["items"]:
                if item["rootId"] == root_id and item["path"] == path and item["pending"]:
                    item["pending"] = False
                    changed = True
            if changed:
                self._write_locked(document)
            return changed

    def _confirm_pending_locked(self, note_root: str, note_path: str, references: frozenset[ImageIdentity]) -> None:
        document = self._read_locked()
        changed = False
        for item in document["items"]:
            if (
                item["pending"] is True
                and item["noteRootId"] == note_root
                and item["notePath"] == note_path
                and ImageIdentity(item["rootId"], item["path"]) in references
            ):
                item["pending"] = False
                changed = True
        if changed:
            self._write_locked(document)

    def _read_locked(self) -> dict[str, object]:
        path = self.state_directory / "attachments" / "pending.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"version": 1, "instanceId": self.instance_id, "items": []}
        try:
            envelope = json.loads(raw.decode("utf-8"))
            document = envelope["document"]
            mac = envelope["mac"]
            canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateError("image pending registry is invalid") from exc
        expected = hmac.new(read_signing_secret(self.state_directory), canonical, hashlib.sha256).hexdigest()
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or document.get("instanceId") != self.instance_id
            or not isinstance(document.get("items"), list)
            or not isinstance(mac, str)
            or not hmac.compare_digest(mac, expected)
        ):
            raise StateError("image pending registry authentication failed")
        return document

    def _write_locked(self, document: dict[str, object]) -> None:
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        mac = hmac.new(read_signing_secret(self.state_directory), canonical, hashlib.sha256).hexdigest()
        payload = json.dumps({"document": document, "mac": mac}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        atomic_write(self.state_directory / "attachments" / "pending.json", payload)

    def _prepare(self) -> None:
        directory = self.state_directory / "attachments"
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise StateError("image pending directory is unavailable") from exc
        try:
            details = os.fstat(fd)
            if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
                raise StateError("image pending directory permissions are invalid")
        finally:
            os.close(fd)
        lock = self.state_directory / "images.lock"
        if not lock.exists():
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)


def attachment_directory(note_path: str) -> str:
    """Return the attachments/ directory in the note's own directory."""
    parent = note_path.rsplit("/", 1)[0] if "/" in note_path else ""
    return f"{parent}/attachments" if parent else "attachments"


def _managed_attachment(identity: ImageIdentity) -> bool:
    parts = identity.path.split("/")
    return len(parts) >= 2 and parts[-2] == "attachments" and parts[-1].lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


def _is_managed_image(catalog: RootCatalog, identity: ImageIdentity) -> bool:
    try:
        with open_regular(catalog, identity.root_id, identity.path) as (fd, info):
            if not stat.S_ISREG(info.st_mode):
                return False
            prefix = os.read(fd, 16)
    except (AddressRejected, OSError):
        return False
    return _extension_for(prefix) is not None


def _extension_for(prefix: bytes) -> str | None:
    if prefix.startswith(_IMAGE_TYPES["png"][1]):
        return "png"
    if prefix.startswith(_IMAGE_TYPES["jpg"][1]):
        return "jpg"
    if prefix.startswith(_IMAGE_TYPES["gif"][1]):
        return "gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "webp"
    return None


def _pending_identities(items: object) -> set[ImageIdentity]:
    if not isinstance(items, list):
        return set()
    return {
        ImageIdentity(item["rootId"], item["path"])
        for item in items
        if isinstance(item, dict) and item.get("pending") is True
    }


def _note_id(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    slug = "".join(character.lower() if character.isalnum() else "-" for character in name)
    slug = "-".join(part for part in slug.split("-") if part)[:48]
    if not slug:
        slug = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    return slug


def _map_move_identity(
    identity: ImageIdentity,
    source_root: str,
    source_path: str,
    destination_root: str,
    destination_path: str,
    kind: str,
) -> ImageIdentity | None:
    if identity.root_id != source_root:
        return None
    if identity.path == source_path:
        suffix = ""
    elif kind == "directory" and identity.path.startswith(source_path + "/"):
        suffix = identity.path[len(source_path) :]
    else:
        return None
    moved_path = destination_path + suffix
    return ImageIdentity(destination_root, moved_path)


def _rewrite_move_document(
    catalog: RootCatalog,
    root_id: str,
    path: str,
    new_root_id: str,
    new_path: str,
    content: str,
    source_root: str,
    source_path: str,
    destination_root: str,
    destination_path: str,
    kind: str,
) -> str:
    spans = image_destination_spans(catalog, root_id, path, content)
    document_moved = (root_id, path) != (new_root_id, new_path)
    replacements: list[tuple[int, int, str]] = []
    for span in spans:
        if span.identity is None:
            continue
        moved_target = _map_move_identity(
            span.identity, source_root, source_path, destination_root, destination_path, kind
        )
        if moved_target is None and not document_moved:
            continue
        target = moved_target or span.identity
        replacement = relative_markdown_destination(
            catalog, new_root_id, new_path, target.root_id, target.path
        )
        if replacement != span.destination:
            replacements.append((span.start, span.end, replacement))
    rewritten = content
    for start, end, replacement in reversed(replacements):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten


def _move_result(
    plan: dict[str, object],
    trash_id: str,
    status: str,
    error: str | None,
    separated_links: int = 0,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": status,
        "action": "move",
        "id": trash_id,
        "source": plan["source"],
        "destination": plan["destination"],
        "kind": plan["source"]["kind"],
        "size": plan["size"],
        "rewrittenDocuments": sum(
            1
            for item in plan["documents"]
            if item["beforeDigest"] != item["afterDigest"]
        ),
        # HF-FILE-002: other names of a file copied across filesystems keep
        # the original inode; the response says so.
        "separatedLinks": separated_links if status == "committed" else 0,
    }
    if error is not None:
        result["error"] = error
    return result


def _safe_move_error(error: Exception) -> str:
    if isinstance(error, (TrashError, AddressRejected, EditorError)):
        return error.code
    if isinstance(error, StateError):
        return "storage_unavailable"
    return "unavailable"


def _trash_entry_exists(trash: TrashStore, identifier: str, catalog: RootCatalog, now: float) -> bool:
    try:
        return any(entry.get("id") == identifier for entry in trash.list_entries(catalog, now))
    except TrashError as exc:
        raise StateError("move trash state is unavailable") from exc


def _ensure_private_directory(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise StateError("move staging directory is unavailable") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise StateError("move staging directory is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StateError("move staging directory permissions are invalid")


def _read_private_regular(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise StateError("move staging file permissions are invalid")
        blocks: list[bytes] = []
        size = 0
        while True:
            block = os.read(fd, 64 * 1024)
            if not block:
                break
            size += len(block)
            if size > maximum:
                raise StateError("move staging file exceeds its limit")
            blocks.append(block)
        if size != info.st_size:
            raise StateError("move staging file changed while reading")
        return b"".join(blocks)
    finally:
        os.close(fd)
