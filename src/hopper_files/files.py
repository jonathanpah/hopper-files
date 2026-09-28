"""Durable, contained base file operations for one Hopper Files instance.

The HTTP layer supplies authenticated requests.  This module owns operation
tokens, their per-instance journals, and exclusive publication of new regular
files.  It intentionally has no UI state, reference rewrite, trash, or editor
semantics.
"""

from __future__ import annotations

import base64
import copy
import errno
import hashlib
import hmac
import json
import os
import re
import stat
import uuid
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import fcntl

from hopper_files.roots import (
    AddressRejected,
    RootCatalog,
    StagedDirectory,
    StagedRegular,
    create_staged_directory_child,
    create_staged_regular_child,
    destination_capacity,
    destination_exists,
    directory_version,
    discard_operation_stage,
    discard_staged_directory,
    discard_staged_regular,
    inspect_address,
    inspect_operation_object,
    list_directory,
    new_object_modes,
    open_staged_directory,
    open_regular,
    parse_relative_path,
    publish_staged_directory,
    publish_staged_regular,
    stage_directory_exclusive,
    stage_regular,
    begin_stage_regular,
    validate_new_address,
)
from hopper_files.state import StateError, atomic_write, read_signing_secret

TOKEN_LIFETIME_SECONDS = 7 * 24 * 60 * 60
TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_ACTIVE_OPERATIONS = 64
UPLOAD_MAX_FILES = 20
UPLOAD_MAX_TOTAL = 400 * 1024 * 1024
UPLOAD_MAX_FILE = 200 * 1024 * 1024
ZIP_SOURCE_MAX_BYTES = 3 * 1024 * 1024 * 1024
ZIP_SOURCE_MAX_ENTRIES = 10_000
ZIP_MAX_DEPTH = 32
ZIP_EXTRACT_MAX_BYTES = 3 * 1024 * 1024 * 1024
ZIP_EXTRACT_MAX_FILE = 200 * 1024 * 1024
ZIP_MAX_RATIO = 120
ZIP_ARCHIVE_OVERHEAD = 128 * 1024 * 1024
OPERATION_RECORD_MAX_BYTES = 16 * 1024 * 1024

_TOKEN_VERSION = "v1"
_RECORD = "op-"
_SUFFIX = ".json"


class OperationError(ValueError):
    """A known operation outcome that maps to a precise HTTP response."""

    def __init__(self, code: str, *, payload: dict[str, object] | None = None) -> None:
        self.code = code
        self.payload = payload
        super().__init__(code)


@dataclass(frozen=True)
class OperationToken:
    identifier: str
    issued_at: int
    expires_at: int


class UploadLease:
    """Exclusive per-token lease held across the entire streamed request."""

    def __init__(self, state_directory: Path, instance_id: str, identifier: str, fd: int) -> None:
        self.state_directory = state_directory
        self.instance_id = instance_id
        self.identifier = identifier
        self.fd = fd

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd < 0:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class OperationStore:
    """Serialize operation records and make a replay's result durable."""

    def __init__(self, state_directory: Path, instance_id: str, time_zone: str) -> None:
        self.state_directory = state_directory
        self.instance_id = instance_id
        self.time_zone = ZoneInfo(time_zone)
        self.move_handler = None

    def complete_recovered_move(
        self,
        token_id: str,
        index: int,
        item: dict[str, object],
        now: float,
    ) -> None:
        """Mark a move completed after its own journal recovered every rewrite."""
        with self._lock():
            for record in self._records_locked():
                if record.get("tokenId") != token_id:
                    continue
                items = record.get("items")
                if not isinstance(items, list) or index < 0:
                    raise StateError("move operation record is invalid")
                while len(items) <= index:
                    items.append({"status": "uncommitted", "action": "unknown", "error": "not_started"})
                items[index] = item
                statuses = [status for value in items if isinstance(value, dict) for status in _item_statuses(value)]
                record["status"] = "indeterminate" if "indeterminate" in statuses else (
                    "completed" if statuses and all(status == "committed" for status in statuses) else (
                        "partial" if "committed" in statuses else "failed"
                    )
                )
                record["finishedAt"] = int(now)
                self._write_record_locked(record)
                return
            raise StateError("move operation token record is missing")

    def acquire_upload_lease(self, token_text: object, now: float) -> UploadLease:
        """Claim one upload token until its request publishes or unwinds."""
        token = self._open(token_text)
        with self._lock():
            record = self._find_record_locked(token)
            if record is None:
                if now >= token.expires_at:
                    raise OperationError("expired")
                raise OperationError("not_found")
            if now >= token.expires_at:
                raise OperationError("expired")
            fd = self._try_upload_lock_locked(token.identifier)
            if fd is None:
                raise OperationError("conflict")
            return UploadLease(self.state_directory, self.instance_id, token.identifier, fd)

    def issue(self, now: float) -> dict[str, object]:
        """Issue one bounded, instance-authenticated operation token."""
        with self._lock():
            self._garbage_collect_locked(now)
            records = self._records_locked()
            if sum(
                record["status"] in {"issued", "pending", "indeterminate"}
                or (record["status"] in {"partial", "failed"} and now < record["expiresAt"])
                for record in records
            ) >= MAX_ACTIVE_OPERATIONS:
                raise OperationError("limit")
            issued_at = int(now)
            token = OperationToken(uuid.uuid4().hex, issued_at, issued_at + TOKEN_LIFETIME_SECONDS)
            record = {
                "version": 1,
                "instanceId": self.instance_id,
                "tokenId": token.identifier,
                "issuedAt": token.issued_at,
                "expiresAt": token.expires_at,
                "status": "issued",
                "fingerprint": None,
                "actions": None,
                "items": [],
                "finishedAt": None,
            }
            self._write_record_locked(record)
            return {
                "operationToken": self._seal(token),
                "issuedAt": token.issued_at,
                "expiresAt": token.expires_at,
            }

    def abort_upload(self, token_text: object, now: float, error: str) -> None:
        """Record that an upload request failed before it published any new item."""
        if not isinstance(token_text, str):
            return
        token = self._open(token_text)
        with self._lock():
            record = self._find_record_locked(token)
            if record is None or record["status"] == "completed" or record["fingerprint"] is None:
                return
            if any(
                isinstance(item, dict) and item.get("status") in {"staging", "publishing"}
                for item in record["items"]
            ):
                return
            for index, item in enumerate(record["items"]):
                if isinstance(item, dict) and item.get("status") in {"committed", "indeterminate"}:
                    continue
                action = {"action": "upload", "destination": item.get("destination") if isinstance(item, dict) else None}
                record["items"][index] = _failure_item(action, error, status="uncommitted")
            statuses = [status for item in record["items"] if isinstance(item, dict) for status in _item_statuses(item)]
            record["status"] = "indeterminate" if "indeterminate" in statuses else ("partial" if any(
                isinstance(item, dict) and item.get("status") == "committed" for item in record["items"]
            ) else "failed")
            record["finishedAt"] = int(now)
            self._write_record_locked(record)

    def status(self, token_text: object, now: float) -> dict[str, object]:
        token = self._open(token_text)
        with self._lock():
            record = self._find_record_locked(token)
            if record is None:
                if now >= token.expires_at:
                    raise OperationError("expired")
                raise OperationError("not_found")
            if record["status"] == "issued" and now >= token.expires_at:
                record["status"] = "failed"
                record["finishedAt"] = token.expires_at
                self._write_record_locked(record)
            return self._result(record)

    def submit(
        self,
        token_text: object,
        actions: object,
        catalog: RootCatalog,
        now: float,
    ) -> dict[str, object]:
        """Bind and run ordinary create/copy actions under one durable journal.

        Every published item has an already-synced ``publishing`` item record.
        Replays skip committed items and return the previous terminal result.
        """
        token = self._open(token_text)
        normalized = _normalize_actions(actions)
        fingerprint = _fingerprint(normalized)
        with self._lock():
            record = self._find_record_locked(token)
            if record is None:
                if now >= token.expires_at:
                    raise OperationError("expired")
                raise OperationError("not_found")
            if now >= token.expires_at:
                raise OperationError("expired")
            old_fingerprint = record["fingerprint"]
            if old_fingerprint is None:
                record["fingerprint"] = fingerprint
                record["actions"] = _plan_actions(normalized, now, self.time_zone)
                record["status"] = "pending"
                try:
                    self._write_record_locked(record)
                except (OSError, StateError) as exc:
                    raise OperationError(
                        "indeterminate",
                        payload=self._outcome_after_storage_error_locked(token, catalog, now, record),
                    ) from exc
            elif old_fingerprint != fingerprint:
                raise OperationError("conflict")
            elif record["status"] == "completed":
                return self._result(record)
            elif record["status"] == "indeterminate":
                return self._result(record)
            elif _record_has_unfinished_publication(record):
                try:
                    self._recover_record_locked(record, catalog, now)
                except (OSError, StateError) as exc:
                    raise OperationError(
                        "indeterminate",
                        payload=self._outcome_after_storage_error_locked(token, catalog, now, record),
                    ) from exc
                if record["status"] in {"completed", "indeterminate"}:
                    return self._result(record)

            supplied = record["actions"]
            if not isinstance(supplied, list) or len(supplied) != len(normalized):
                raise StateError("operation action record is invalid")
            errors = self._preflight_actions(record, catalog, now)
            if any(errors):
                updated: list[dict[str, object]] = []
                for index, action in enumerate(record["actions"]):
                    existing = record["items"][index] if index < len(record["items"]) else None
                    if isinstance(existing, dict) and existing.get("status") == "committed":
                        updated.append(existing)
                    elif isinstance(existing, dict) and isinstance(existing.get("children"), list):
                        children = existing["children"]
                        for child in children:
                            if isinstance(child, dict) and child.get("status") != "committed":
                                child["status"] = "uncommitted"
                                child["error"] = errors[index] or "not_started"
                        existing["status"] = (
                            "partial"
                            if any(isinstance(child, dict) and child.get("status") == "committed" for child in children)
                            else "failed"
                        )
                        existing["error"] = errors[index] or "not_started"
                        updated.append(existing)
                    else:
                        updated.append(_failure_item(action, errors[index] or "not_started", status="uncommitted"))
                record["items"] = updated
                statuses = [status for item in updated for status in _item_statuses(item)]
                record["status"] = "indeterminate" if "indeterminate" in statuses else (
                    "partial" if "committed" in statuses else "failed"
                )
                record["finishedAt"] = int(now)
                try:
                    self._write_record_locked(record)
                except (OSError, StateError) as exc:
                    raise OperationError(
                        "indeterminate",
                        payload=self._outcome_after_storage_error_locked(token, catalog, now, record),
                    ) from exc
                return self._result(record)
            try:
                # This durable checkpoint is the boundary before any action
                # can create a stage or publish a destination.
                self._write_record_locked(record)
                self._run_locked(record, catalog, now)
            except (OSError, StateError) as exc:
                raise OperationError(
                    "indeterminate",
                    payload=self._outcome_after_storage_error_locked(token, catalog, now, record),
                ) from exc
            return self._result(record)

    def recover(
        self,
        catalog: RootCatalog,
        now: float,
        *,
        active_upload_lease: UploadLease | None = None,
    ) -> None:
        """Classify a stopped publication without publishing a second copy."""
        with self._lock():
            for record in self._records_locked():
                if record["status"] not in {"pending", "partial", "failed", "indeterminate"}:
                    continue
                upload_fd = None
                if _is_upload_record(record) and not self._lease_matches(
                    active_upload_lease, record["tokenId"]
                ):
                    upload_fd = self._try_upload_lock_locked(record["tokenId"])
                    if upload_fd is None:
                        continue
                try:
                    previous_status = record["status"]
                    changed = self._classify_record_locked(record, catalog, now)
                    if changed or record["status"] != previous_status:
                        self._write_record_locked(record)
                finally:
                    if upload_fd is not None:
                        self._close_upload_lock(upload_fd)
            self._garbage_collect_locked(now)

    def _recover_upload_record_locked(
        self,
        record: dict[str, object],
        catalog: RootCatalog,
        now: float,
    ) -> None:
        self._classify_record_locked(record, catalog, now)
        self._write_record_locked(record)

    def _recover_record_locked(
        self,
        record: dict[str, object],
        catalog: RootCatalog,
        now: float,
    ) -> None:
        previous_status = record["status"]
        changed = self._classify_record_locked(record, catalog, now)
        if changed or record["status"] != previous_status:
            self._write_record_locked(record)

    def _classify_record_locked(
        self,
        record: dict[str, object],
        catalog: RootCatalog,
        now: float,
    ) -> bool:
        changed = False
        for item in record["items"]:
            if not isinstance(item, dict):
                continue
            children = item.get("children")
            targets = children if isinstance(children, list) else [item]
            for target in targets:
                if isinstance(target, dict) and target.get("status") in {"staging", "publishing"}:
                    self._recover_item(catalog, target)
                    changed = True
            if isinstance(children, list):
                previous_item_status = item.get("status")
                statuses = [child.get("status") for child in children if isinstance(child, dict)]
                if any(status == "indeterminate" for status in statuses):
                    item["status"] = "indeterminate"
                elif statuses and all(status == "committed" for status in statuses):
                    item["status"] = "committed"
                elif any(status == "committed" for status in statuses):
                    item["status"] = "partial"
                else:
                    item["status"] = "failed"
                if item.get("status") != previous_item_status:
                    changed = True
        statuses = [status for item in record["items"] if isinstance(item, dict) for status in _item_statuses(item)]
        if "indeterminate" in statuses:
            record["status"] = "indeterminate"
        elif statuses and all(status == "committed" for status in statuses):
            record["status"] = "completed"
            record["finishedAt"] = int(now)
        elif "committed" in statuses:
            record["status"] = "partial"
            record["finishedAt"] = int(now)
        elif changed or record["status"] == "pending":
            record["status"] = "failed"
            record["finishedAt"] = int(now)
        return changed

    def _outcome_after_storage_error_locked(
        self,
        token: OperationToken,
        catalog: RootCatalog,
        now: float,
        fallback: dict[str, object],
    ) -> dict[str, object]:
        # ``fallback`` is the live record from the operation that hit the
        # storage error. It can be ahead of the last atomic journal replace,
        # and it is the only source for an intent whose first write failed.
        # Start there, then retain any positive durable outcomes discovered in
        # the on-disk copy.
        record = copy.deepcopy(fallback)
        try:
            persisted = self._find_record_locked(token)
        except (OperationError, StateError):
            persisted = None
        if persisted is not None:
            if not isinstance(record.get("actions"), list):
                record["actions"] = persisted.get("actions")
            fallback_items = record.get("items")
            persisted_items = persisted.get("items")
            if not isinstance(fallback_items, list):
                fallback_items = []
            if isinstance(persisted_items, list):
                while len(fallback_items) < len(persisted_items):
                    fallback_items.append(None)
                for index, durable in enumerate(persisted_items):
                    current = fallback_items[index]
                    if (
                        isinstance(durable, dict)
                        and durable.get("status") in {"committed", "indeterminate"}
                        and not (isinstance(current, dict) and current.get("status") in {"committed", "indeterminate"})
                    ):
                        fallback_items[index] = durable
            record["items"] = fallback_items

        actions = record.get("actions")
        items = record.get("items")
        if not isinstance(items, list):
            items = []
            record["items"] = items
        if isinstance(actions, list):
            while len(items) < len(actions):
                items.append(_failure_item(actions[len(items)], "storage_unavailable", status="uncommitted"))

        try:
            self._classify_record_locked(record, catalog, now)
        except (OSError, StateError):
            for item in items:
                if not isinstance(item, dict):
                    continue
                targets = item.get("children") if isinstance(item.get("children"), list) else [item]
                for target in targets:
                    if isinstance(target, dict) and target.get("status") in {"staging", "publishing"}:
                        target.update({"status": "indeterminate", "error": "publication_unknown"})

        # Missing/stale per-item rows are a no-commit storage failure only
        # after recovery has ruled out publication. Keep partial and
        # indeterminate items intact; never turn them into a generic conflict.
        for item in items:
            if not isinstance(item, dict):
                continue
            targets = item.get("children") if isinstance(item.get("children"), list) else [item]
            for target in targets:
                if (
                    isinstance(target, dict)
                    and target.get("status") in {"uncommitted", "failed"}
                ):
                    target["status"] = "uncommitted"
                    target["error"] = "storage_unavailable"

        statuses = [status for item in items if isinstance(item, dict) for status in _item_statuses(item)]
        if "indeterminate" in statuses:
            record["status"] = "indeterminate"
        elif "committed" in statuses:
            if all(status == "committed" for status in statuses):
                # Publication is visible, but the result write did not confirm
                # durable completion. Replays can reconcile it without a
                # second publication.
                record["status"] = "indeterminate"
            else:
                record["status"] = "partial"
        else:
            record["status"] = "failed"
        record["finishedAt"] = int(now)
        return self._result(record)

    def outcome_after_storage_error(
        self,
        token_text: object,
        catalog: RootCatalog,
        now: float,
    ) -> dict[str, object]:
        """Report a bounded in-memory classification when a result write failed."""
        token = self._open(token_text)
        with self._lock():
            record = self._find_record_locked(token)
            if record is None:
                raise StateError("operation journal is unavailable")
            return self._outcome_after_storage_error_locked(token, catalog, now, record)

    def upload_outcome_after_storage_error(
        self,
        token_text: object,
        action: object,
        catalog: RootCatalog,
        now: float,
    ) -> dict[str, object]:
        """Report upload items when the journal fails before begin_upload returns."""
        token = self._open(token_text)
        normalized = _normalize_upload(action)
        with self._lock():
            record = self._find_record_locked(token)
            if record is None:
                raise StateError("operation journal is unavailable")
            fallback = copy.deepcopy(record)
            stored_actions = fallback.get("actions")
            planned: dict[str, object] | None = None
            if isinstance(stored_actions, list) and stored_actions:
                candidate = stored_actions[0]
                if isinstance(candidate, dict) and isinstance(candidate.get("files"), list):
                    planned = candidate
            if planned is None:
                try:
                    planned = _plan_upload(normalized, now, self.time_zone, catalog)
                except OperationError:
                    planned = normalized
                fallback["fingerprint"] = _fingerprint([normalized])
                fallback["actions"] = [planned]
                fallback["status"] = "pending"
            files = planned.get("files") if isinstance(planned, dict) else None
            if not isinstance(files, list):
                files = normalized["files"]
            items = fallback.get("items")
            if not isinstance(items, list):
                items = []
                fallback["items"] = items
            base = normalized["destination"]
            assert isinstance(base, dict)
            while len(items) < len(files):
                file_item = files[len(items)]
                destination = file_item.get("destination") if isinstance(file_item, dict) else None
                if not isinstance(destination, dict):
                    name = file_item.get("originalName") if isinstance(file_item, dict) else None
                    destination = {
                        "rootId": base["rootId"],
                        "path": _join(base["path"], name) if isinstance(name, str) else base["path"],
                    }
                items.append(_failure_item(
                    {"action": "upload", "destination": destination},
                    "storage_unavailable",
                    status="uncommitted",
                ))
            return self._outcome_after_storage_error_locked(token, catalog, now, fallback)

    def _recover_item(self, catalog: RootCatalog, item: dict[str, object]) -> None:
        phase = item.get("status")
        destination = item.get("destination")
        stage = item.get("stage")
        kind = item.get("stageKind", "file")
        stage_identity = None
        if isinstance(stage, dict) and isinstance(stage.get("identity"), list) and len(stage["identity"]) == 2:
            identity = stage["identity"]
            if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in identity):
                stage_identity = (identity[0], identity[1])
        observed = None
        if isinstance(destination, dict):
            try:
                observed = inspect_operation_object(
                    catalog,
                    destination["rootId"],
                    destination["path"],
                    expected_kind="directory" if kind == "directory" else "file",
                )
            except OSError:
                item.update({"status": "indeterminate", "error": "publication_unknown"})
                return
            except (AddressRejected, KeyError, TypeError):
                observed = None
        expected_identity = item.get("publishedIdentity") or (list(stage_identity) if stage_identity else None)
        observed_digest = None
        if (
            observed is not None
            and kind == "directory"
            and item.get("action") == "copy"
            and isinstance(item.get("digest"), str)
            and isinstance(destination, dict)
        ):
            try:
                digest_identity, observed_digest = _directory_digest(catalog, destination)
                if list(digest_identity) != list(observed[0]):
                    observed_digest = None
            except (AddressRejected, OSError, OperationError, KeyError, TypeError):
                observed_digest = None
        committed = (
            observed is not None
            and expected_identity is not None
            and list(observed[0]) == expected_identity
            and (
                (kind == "directory" and item.get("action") != "copy")
                or (kind == "directory" and observed_digest == item.get("digest"))
                or (kind != "directory" and observed[1] == item.get("digest"))
            )
        )
        if committed:
            item["status"] = "committed"
            item["publishedIdentity"] = list(observed[0])
            item.pop("error", None)
            return
        if stage_identity is not None and isinstance(stage, dict) and isinstance(destination, dict):
            try:
                stage_present = discard_operation_stage(
                    catalog,
                    destination["rootId"],
                    destination["path"],
                    stage["name"],
                    stage_identity,
                    kind="directory" if kind == "directory" else "file",
                )
            except OSError:
                item.update({"status": "indeterminate", "error": "publication_unknown"})
                return
            except (AddressRejected, KeyError, TypeError):
                stage_present = False
            if stage_present or phase == "staging":
                item["status"] = "uncommitted"
                item["error"] = "not_published"
                return
        if phase == "staging":
            item["status"] = "uncommitted"
            item["error"] = "not_published"
        else:
            item["status"] = "indeterminate"
            item["error"] = "publication_unknown"

    def begin_upload(
        self,
        token_text: object,
        action: object,
        catalog: RootCatalog,
        now: float,
        *,
        lease: UploadLease | None = None,
    ) -> dict[str, object]:
        """Bind one upload batch while excluding concurrent replays of its token."""
        token = self._open(token_text)
        normalized = _normalize_upload(action)
        fingerprint = _fingerprint([normalized])
        owns_lease = lease is None
        active_lease = lease or self.acquire_upload_lease(token_text, now)
        if not self._lease_matches(active_lease, token.identifier):
            if owns_lease:
                active_lease.close()
            raise OperationError("conflict")
        try:
            with self._lock():
                record = self._find_record_locked(token)
                if record is None:
                    if now >= token.expires_at:
                        raise OperationError("expired")
                    raise OperationError("not_found")
                if now >= token.expires_at:
                    raise OperationError("expired")
                if record["fingerprint"] is not None and record["fingerprint"] != fingerprint:
                    raise OperationError("conflict")
                if record["status"] == "completed":
                    return {"stored": True, "action": record["actions"][0]}
                if record["status"] == "pending":
                    # The exclusive lease proves the prior request stopped. Resolve
                    # its journaled stages before allowing this request to resume.
                    self._recover_upload_record_locked(record, catalog, now)
                    if record["status"] == "completed":
                        return {"stored": True, "action": record["actions"][0]}
                if record["status"] == "indeterminate":
                    raise OperationError("indeterminate", payload=self._result(record))
                if record["fingerprint"] is None:
                    if record["status"] != "issued":
                        raise OperationError("conflict")
                    record["fingerprint"] = fingerprint
                    record["actions"] = [normalized]
                    record["status"] = "pending"
                    self._write_record_locked(record)
                if record["status"] in {"partial", "failed"}:
                    record["status"] = "pending"
                    self._write_record_locked(record)
                elif record["status"] != "pending":
                    raise OperationError("conflict")
                action_record = record["actions"][0]
                if "planned" not in action_record:
                    try:
                        planned = _plan_upload(normalized, now, self.time_zone, catalog)
                    except OperationError as exc:
                        record["status"] = "failed"
                        record["finishedAt"] = int(now)
                        record["items"] = [
                            _failure_item({"action": "upload", "destination": None}, exc.code, status="uncommitted")
                            for _ in normalized["files"]
                        ]
                        self._write_record_locked(record)
                        raise
                    record["actions"] = [{**planned, "planned": True}]
                    record["items"] = [
                        _failure_item({"action": "upload", "destination": item["destination"]}, "not_started", status="uncommitted")
                        for item in planned["files"]
                    ]
                    self._write_record_locked(record)
                    action_record = record["actions"][0]
                committed = [
                    index
                    for index, item in enumerate(record["items"])
                    if isinstance(item, dict) and item.get("status") == "committed"
                ]
                return {"stored": False, "action": action_record, "committedIndexes": committed}
        finally:
            if owns_lease:
                active_lease.close()

    def note_upload_stage(
        self,
        token_text: object,
        index: int,
        action: dict[str, object],
        stage_name: str,
        identity: tuple[int, int],
    ) -> None:
        """Durably bind the private upload stage identity before body writes."""
        token = self._open(token_text)
        with self._lock():
            record = self._find_record_locked(token)
            if record is None or record["status"] != "pending":
                raise OperationError("conflict")
            planned = record["actions"][0]["files"]
            if not isinstance(index, int) or index < 0 or index >= len(planned) or planned[index] != action:
                raise OperationError("conflict")
            existing = record["items"][index]
            if isinstance(existing, dict) and existing.get("status") == "committed":
                return
            if isinstance(existing, dict) and existing.get("status") in {"staging", "publishing"}:
                stage = existing.get("stage")
                stage_identity = stage.get("identity") if isinstance(stage, dict) else None
                if (
                    isinstance(stage, dict)
                    and stage.get("name") == stage_name
                    and stage_identity == [identity[0], identity[1]]
                ):
                    return
                raise OperationError("conflict")
            record["items"][index] = {
                "status": "staging",
                "action": "upload",
                "destination": action["destination"],
                "digest": action["contentDigest"],
                "size": action["contentSize"],
                "stage": {"name": stage_name, "identity": [identity[0], identity[1]]},
                "stageKind": "file",
                "source": None,
                "publishedIdentity": None,
            }
            self._write_record_locked(record)

    def publish_upload_batch(
        self,
        token_text: object,
        staged_items: list[StagedRegular | None],
        catalog: RootCatalog,
        now: float,
    ) -> dict[str, object]:
        """Publish a fully received batch one file at a time with honest results."""
        token = self._open(token_text)
        for staged in staged_items:
            if staged is not None and (staged.digest == "" or staged.size < 0):
                raise OperationError("invalid")
        with self._lock():
            record = self._find_record_locked(token)
            if record is None or record["status"] not in {"pending", "partial", "failed"}:
                raise OperationError("conflict")
            actions = record["actions"][0]["files"]
            if len(staged_items) != len(actions):
                raise OperationError("invalid")
            for index, (action, staged) in enumerate(zip(actions, staged_items, strict=True)):
                existing = record["items"][index]
                if isinstance(existing, dict) and existing.get("status") == "committed":
                    if staged is not None:
                        discard_staged_regular(staged)
                    continue
                if staged is None:
                    raise OperationError("invalid")
                if staged.digest != action["contentDigest"] or staged.size != action["contentSize"]:
                    discard_staged_regular(staged)
                    record["items"][index] = _failure_item({"action": "upload", "destination": action["destination"]}, "content_mismatch", status="uncommitted")
                    record["status"] = "partial" if any(item.get("status") == "committed" for item in record["items"] if isinstance(item, dict)) else "failed"
                    record["finishedAt"] = int(now)
                    self._write_record_locked(record)
                    for remaining in staged_items[index + 1 :]:
                        if remaining is None:
                            continue
                        try:
                            discard_staged_regular(remaining)
                        except OSError:
                            pass
                    for remaining_index in range(index + 1, len(actions)):
                        remaining_item = record["items"][remaining_index]
                        if isinstance(remaining_item, dict) and remaining_item.get("status") != "committed":
                            record["items"][remaining_index] = _failure_item(
                                {"action": "upload", "destination": actions[remaining_index]["destination"]},
                                "not_started",
                                status="uncommitted",
                            )
                    self._write_record_locked(record)
                    raise OperationError("invalid", payload=self._result(record))
                try:
                    result = self._publish_stage_locked(
                        record,
                        index,
                        {"action": "upload", "destination": action["destination"]},
                        staged,
                        source=None,
                        destination=action["destination"],
                    )
                    record["items"][index] = result
                    self._write_record_locked(record)
                except OperationError as exc:
                    current = record["items"][index]
                    if isinstance(current, dict) and current.get("status") in {"staging", "publishing"}:
                        stage = current.get("stage")
                        identity = stage.get("identity") if isinstance(stage, dict) else None
                        if isinstance(stage, dict) and isinstance(identity, list) and len(identity) == 2:
                            self._recover_item(catalog, current)
                    if isinstance(current, dict) and current.get("status") not in {"committed", "indeterminate"}:
                        current["status"] = "uncommitted"
                        current["error"] = exc.code
                    record["finishedAt"] = int(now)
                    for remaining in staged_items[index + 1 :]:
                        if remaining is None:
                            continue
                        try:
                            discard_staged_regular(remaining)
                        except OSError:
                            pass
                    for remaining_index in range(index + 1, len(actions)):
                        remaining_item = record["items"][remaining_index]
                        if isinstance(remaining_item, dict) and remaining_item.get("status") != "committed":
                            record["items"][remaining_index] = _failure_item(
                                {"action": "upload", "destination": actions[remaining_index]["destination"]},
                                "not_started",
                                status="uncommitted",
                            )
                    statuses = [status for item in record["items"] if isinstance(item, dict) for status in _item_statuses(item)]
                    record["status"] = "indeterminate" if "indeterminate" in statuses else (
                        "partial" if "committed" in statuses else "failed"
                    )
                    self._write_record_locked(record)
                    break
            statuses = [status for item in record["items"] if isinstance(item, dict) for status in _item_statuses(item)]
            if "indeterminate" in statuses:
                record["status"] = "indeterminate"
            elif statuses and all(status == "committed" for status in statuses):
                record["status"] = "completed"
            else:
                record["status"] = "partial" if "committed" in statuses else "failed"
            record["finishedAt"] = int(now)
            self._write_record_locked(record)
            return self._result(record)

    def _run_locked(self, record: dict[str, object], catalog: RootCatalog, now: float) -> None:
        actions = record["actions"]
        items = record["items"]
        assert isinstance(actions, list) and isinstance(items, list)
        for index, action in enumerate(actions):
            if index >= len(items):
                _replace_item(items, index, _failure_item(action, "not_started", status="uncommitted"))
        for index, action in enumerate(actions):
            existing = items[index] if index < len(items) else None
            if isinstance(existing, dict) and existing.get("status") == "committed":
                continue
            if isinstance(existing, dict) and existing.get("status") == "indeterminate":
                record["status"] = "indeterminate"
                self._write_record_locked(record)
                return
            try:
                item = self._execute_action_locked(record, index, action, catalog, now)
            except OperationError as exc:
                current = items[index] if index < len(items) and isinstance(items[index], dict) else existing
                if isinstance(current, dict) and current.get("status") in {"staging", "publishing"}:
                    stage = current.get("stage")
                    identity = stage.get("identity") if isinstance(stage, dict) else None
                    if isinstance(stage, dict) and isinstance(identity, list) and len(identity) == 2:
                        self._recover_item(catalog, current)
                        if current.get("status") == "committed":
                            _replace_item(items, index, current)
                            self._write_record_locked(record)
                            continue
                        if current.get("status") == "indeterminate":
                            record["status"] = "indeterminate"
                            self._write_record_locked(record)
                            return
                if isinstance(current, dict) and isinstance(current.get("children"), list):
                    children = current["children"]
                    for child in children:
                        if isinstance(child, dict) and child.get("status") != "committed":
                            child["status"] = "uncommitted"
                            child["error"] = exc.code
                    current["status"] = (
                        "partial"
                        if any(isinstance(child, dict) and child.get("status") == "committed" for child in children)
                        else "failed"
                    )
                    current["error"] = exc.code
                    _replace_item(items, index, current)
                else:
                    failed = _failure_item(action, exc.code, status="uncommitted")
                    if isinstance(current, dict):
                        failed.update({key: current[key] for key in ("destination", "digest", "size", "source") if key in current})
                    _replace_item(items, index, failed)
                statuses = [status for entry in items if isinstance(entry, dict) for status in _item_statuses(entry)]
                record["status"] = "indeterminate" if "indeterminate" in statuses else (
                    "partial" if "committed" in statuses else "failed"
                )
                record["finishedAt"] = int(now)
                # Keep later work explicit so a status response never hides items
                # that were not reached after the first failure.
                for remaining in range(index + 1, len(actions)):
                    if remaining >= len(items) or not isinstance(items[remaining], dict) or items[remaining].get("status") != "committed":
                        _replace_item(items, remaining, _failure_item(actions[remaining], "not_started", status="uncommitted"))
                self._write_record_locked(record)
                return
            _replace_item(items, index, item)
            self._write_record_locked(record)
        statuses = [status for entry in items if isinstance(entry, dict) for status in _item_statuses(entry)]
        record["status"] = "indeterminate" if "indeterminate" in statuses else (
            "completed" if statuses and all(status == "committed" for status in statuses) else (
                "partial" if "committed" in statuses else "failed"
            )
        )
        record["finishedAt"] = int(now)
        self._write_record_locked(record)

    def _execute_action_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
        now: float,
    ) -> dict[str, object]:
        kind = action["action"]
        if kind == "create":
            return self._create_locked(record, index, action, catalog, now)
        if kind == "copy":
            return self._copy_locked(record, index, action, catalog)
        if kind == "zip":
            return self._zip_locked(record, index, action, catalog)
        if kind == "extract":
            return self._extract_locked(record, index, action, catalog)
        if kind == "move":
            if self.move_handler is None:
                raise OperationError("not_available")
            return self.move_handler.execute_move(self, record, index, action, catalog, now)
        # Upload, ZIP and extraction use their streamed/specialized submitters;
        # the normal JSON endpoint refuses to transform request bytes into a
        # buffered pseudo-upload.
        raise OperationError("not_available")

    def _preflight_actions(
        self,
        record: dict[str, object],
        catalog: RootCatalog,
        now: float,
    ) -> list[str | None]:
        actions = record["actions"]
        errors: list[str | None] = [None] * len(actions)
        capacities: dict[int, list[tuple[int, int, dict[str, str]]]] = {}
        reservations: dict[tuple[str, str], int] = {}

        for action in actions:
            if isinstance(action, dict):
                action["plannedTargets"] = []

        def reserve(index: int, addresses: list[dict[str, str]]) -> None:
            for address in addresses:
                planned = actions[index].get("plannedTargets")
                if isinstance(planned, list):
                    planned.append(address)
                key = (address["rootId"], address["path"])
                prior = reservations.get(key)
                if prior is not None:
                    errors[index] = "conflict"
                    errors[prior] = "conflict"
                else:
                    reservations[key] = index

        for index, action in enumerate(actions):
            previous = record["items"][index] if index < len(record["items"]) else None
            if isinstance(previous, dict) and previous.get("status") == "committed":
                continue
            try:
                kind = action["action"]
                output_bytes = 0
                if kind == "create":
                    destination = _destination_for_action(action, now, self.time_zone)
                    if action["nameMode"] == "generated":
                        for proposal in range(1, 31):
                            candidate = _destination_for_action(action, now, self.time_zone, proposal)
                            key = (candidate["rootId"], candidate["path"])
                            if key not in reservations and not destination_exists(catalog, candidate["rootId"], candidate["path"]):
                                destination = candidate
                                action["plannedDestination"] = candidate
                                reserve(index, [candidate])
                                break
                        else:
                            raise OperationError("conflict")
                    elif destination_exists(catalog, destination["rootId"], destination["path"]):
                        raise OperationError("conflict")
                    else:
                        reserve(index, [destination])
                    if action["kind"] == "note":
                        output_bytes = len(("# " + action["title"] + "\n").encode("utf-8"))
                elif kind == "copy":
                    source = action["source"]
                    destination = action["destination"]
                    if destination_exists(catalog, destination["rootId"], destination["path"]):
                        raise OperationError("conflict")
                    reserve(index, [destination])
                    inspected = inspect_address(catalog, source["rootId"], source["path"], operation="read")
                    if not inspected.allowed:
                        raise OperationError("forbidden")
                    if inspected.is_regular:
                        with open_regular(catalog, source["rootId"], source["path"]) as (_fd, info):
                            output_bytes = info.st_size
                        action["sourceKind"] = "file"
                        action.pop("plannedEntries", None)
                    elif inspected.is_directory:
                        root_identity, root_version, entries, output_bytes = _copy_source_entries(catalog, source)
                        action["sourceKind"] = "directory"
                        action["sourceIdentity"] = list(root_identity)
                        action["sourceDirectoryVersion"] = root_version
                        action["plannedEntries"] = entries
                        if len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 8 * 1024 * 1024:
                            raise OperationError("limit")
                    else:
                        raise OperationError("forbidden")
                elif kind == "zip":
                    destination = action["destination"]
                    if destination_exists(catalog, destination["rootId"], destination["path"]):
                        raise OperationError("conflict")
                    reserve(index, [destination])
                    entries, output_bytes = _zip_source_entries(catalog, action["sources"])
                    if len(entries) > ZIP_SOURCE_MAX_ENTRIES or output_bytes > ZIP_SOURCE_MAX_BYTES:
                        raise OperationError("limit")
                    action["plannedEntries"] = entries
                    output_bytes += output_bytes // 1000 + sum(
                        len(str(entry["archiveName"]).encode("utf-8")) * 2 + 256
                        for entry in entries
                    )
                    if output_bytes > ZIP_SOURCE_MAX_BYTES + ZIP_ARCHIVE_OVERHEAD:
                        raise OperationError("limit")
                    action["plannedOutputBytes"] = output_bytes
                elif kind == "extract":
                    entries, output_bytes, source_digest = _extract_plan(catalog, action["source"])
                    if isinstance(action.get("sourceDigest"), str) and action["sourceDigest"] != source_digest:
                        raise OperationError("conflict")
                    if isinstance(action.get("plannedEntries"), list) and action["plannedEntries"] != entries:
                        raise OperationError("conflict")
                    destination = action["destination"]
                    prior_children = previous.get("children") if isinstance(previous, dict) else None
                    committed_paths = {
                        child.get("path")
                        for child in prior_children
                        if isinstance(child, dict) and child.get("status") == "committed"
                    } if isinstance(prior_children, list) else set()
                    _preflight_extraction_destinations(catalog, destination, entries, ignore_paths=committed_paths)
                    reserve(index, [
                        {"rootId": destination["rootId"], "path": _join(destination["path"], entry["path"])}
                        for entry in entries
                        if entry["path"] not in committed_paths
                    ])
                    action["plannedEntries"] = entries
                    action["sourceDigest"] = source_digest
                    if not entries:
                        raise OperationError("invalid")
                    remaining_entries = [entry for entry in entries if entry["path"] not in committed_paths]
                    output_bytes = sum(entry["size"] for entry in remaining_entries)
                    if not remaining_entries:
                        continue
                    destination = {
                        "rootId": destination["rootId"],
                        "path": _join(destination["path"], remaining_entries[0]["path"]),
                    }
                elif kind == "move":
                    if self.move_handler is None:
                        raise OperationError("not_available")
                    plan = self.move_handler.plan_move(catalog, action["source"], action["destination"])
                    action["movePlan"] = plan
                    destination = action["destination"]
                    reserve(index, [destination])
                    # A same-filesystem move is one rename and needs no space.
                    output_bytes = int(plan["size"]) if plan["source"]["crossFilesystem"] else 0
                else:
                    raise OperationError("not_available")
                device, available = destination_capacity(catalog, destination["rootId"], destination["path"])
                capacities.setdefault(device, []).append((index, output_bytes, destination))
                if available < 0:
                    raise OperationError("limit")
            except OperationError as exc:
                errors[index] = exc.code
            except AddressRejected as exc:
                errors[index] = _from_address(exc).code
            except (OSError, zipfile.BadZipFile):
                errors[index] = "unavailable"
        for device, demands in capacities.items():
            available = None
            for _index, _size, probe in demands:
                try:
                    _device, free = destination_capacity(catalog, probe["rootId"], probe["path"])
                    available = free if available is None else min(available, free)
                except (AddressRejected, KeyError, TypeError):
                    continue
            if available is not None and sum(size for _index, size, _probe in demands) > available:
                for index, _size, _probe in demands:
                    errors[index] = "limit"
        return errors

    def _zip_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
    ) -> dict[str, object]:
        sources = action["sources"]
        assert isinstance(sources, list)
        entries, total = _zip_source_entries(catalog, sources)
        if len(entries) > ZIP_SOURCE_MAX_ENTRIES or total > ZIP_SOURCE_MAX_BYTES:
            raise OperationError("limit")
        planned_entries = action.get("plannedEntries")
        if not isinstance(planned_entries, list) or planned_entries != entries:
            raise OperationError("conflict")
        output_limit = action.get("plannedOutputBytes")
        if isinstance(output_limit, bool) or not isinstance(output_limit, int):
            raise OperationError("invalid")
        destination = action["destination"]
        writer = None
        try:
            writer = begin_stage_regular(
                catalog,
                destination["rootId"],
                destination["path"],
                maximum=min(ZIP_SOURCE_MAX_BYTES + ZIP_ARCHIVE_OVERHEAD, output_limit),
            )
            stage_info = os.fstat(writer.file_fd)
            existing = record["items"][index] if index < len(record["items"]) else None
            _replace_item(record["items"], index, {
                "status": "staging",
                "action": "zip",
                "destination": destination,
                "digest": existing.get("digest") if isinstance(existing, dict) else None,
                "size": None,
                "stage": {"name": writer.temporary_name, "identity": [stage_info.st_dev, stage_info.st_ino]},
                "stageKind": "file",
                "source": {"sources": sources},
                "publishedIdentity": None,
            })
            self._write_record_locked(record)
            with zipfile.ZipFile(_StageOutput(writer), mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for entry in entries:
                    member_name = entry["archiveName"]
                    if entry["kind"] == "directory":
                        info = zipfile.ZipInfo(member_name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = (stat.S_IFDIR | 0o700) << 16
                        archive.writestr(info, b"")
                        continue
                    source = entry["address"]
                    with open_regular(catalog, source["rootId"], source["path"]) as (fd, current):
                        expected = entry["identity"]
                        if (
                            [current.st_dev, current.st_ino] != expected
                            or current.st_size != entry["size"]
                            or current.st_mtime_ns != entry["mtimeNs"]
                            or current.st_ctime_ns != entry["ctimeNs"]
                        ):
                            raise OperationError("conflict")
                        info = zipfile.ZipInfo(member_name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | 0o600) << 16
                        with archive.open(
                            info,
                            "w",
                            force_zip64=int(entry["size"]) >= zipfile.ZIP64_LIMIT,
                        ) as member:
                            copied = 0
                            for block in _read_chunks(fd):
                                copied += len(block)
                                member.write(block)
                        final = os.fstat(fd)
                        if (
                            copied != entry["size"]
                            or any(
                                getattr(final, key) != getattr(current, key)
                                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                            )
                        ):
                            raise OperationError("conflict")
            for entry in entries:
                if entry["kind"] == "directory" and directory_version(
                    catalog, entry["address"]["rootId"], entry["address"]["path"]
                ) != entry["directoryVersion"]:
                    raise OperationError("conflict")
            staged = writer.finish()
            writer = None
        except AddressRejected as exc:
            if writer is not None:
                writer.abort()
            raise _from_address(exc) from exc
        except OperationError:
            if writer is not None:
                writer.abort()
            raise
        except OSError as exc:
            if writer is not None:
                writer.abort()
            raise OperationError("limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable") from exc
        except (zipfile.BadZipFile, zlib.error) as exc:
            if writer is not None:
                writer.abort()
            raise OperationError("invalid") from exc
        except BaseException:
            if writer is not None:
                writer.abort()
            raise
        previous = record["items"][index]
        if isinstance(previous, dict) and isinstance(previous.get("digest"), str) and previous["digest"] != staged.digest:
            discard_staged_regular(staged)
            raise OperationError("conflict")
        return self._publish_stage_locked(record, index, action, staged, source={"sources": sources})

    def _extract_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
    ) -> dict[str, object]:
        entries, _total, source_digest = _extract_plan(catalog, action["source"])
        expected_digest = action.get("sourceDigest")
        if not isinstance(expected_digest, str) or source_digest != expected_digest:
            raise OperationError("conflict")
        planned = action.get("plannedEntries")
        if planned != entries:
            raise OperationError("conflict")
        previous = record["items"][index] if index < len(record["items"]) else None
        children = previous.get("children") if isinstance(previous, dict) else None
        if not isinstance(children, list) or len(children) != len(entries):
            children = [
                {
                    "status": "uncommitted",
                    "action": "extract",
                    "path": entry["path"],
                    "kind": entry["kind"],
                    "destination": {"rootId": action["destination"]["rootId"], "path": _join(action["destination"]["path"], entry["path"])},
                    "digest": None,
                    "size": entry["size"],
                    "stage": None,
                    "stageKind": "directory" if entry["kind"] == "directory" else "file",
                    "source": {"archiveDigest": source_digest, "zipIndex": entry["zipIndex"]},
                    "publishedIdentity": None,
                    "error": "not_started",
                }
                for entry in entries
            ]
        parent = {
            "status": "pending",
            "action": "extract",
            "destination": action["destination"],
            "source": action["source"],
            "sourceDigest": source_digest,
            "children": children,
        }
        _replace_item(record["items"], index, parent)
        self._write_record_locked(record)
        with open_regular(catalog, action["source"]["rootId"], action["source"]["path"]) as (fd, _info):
            if _digest_fd(fd, _info.st_size) != source_digest:
                raise OperationError("conflict")
            with os.fdopen(os.dup(fd), "rb", closefd=True) as archive_stream:
                with zipfile.ZipFile(archive_stream, "r") as archive:
                    for child_index, (entry, child) in enumerate(zip(entries, children, strict=True)):
                        if child.get("status") == "committed":
                            continue
                        target = child["destination"]
                        if entry["kind"] == "directory":
                            stage_dir = None
                            try:
                                child.update({"status": "uncommitted", "stage": None, "error": "not_started"})
                                self._write_record_locked(record)
                                stage_dir = stage_directory_exclusive(catalog, target["rootId"], target["path"])
                                child.update({
                                    "status": "publishing",
                                    "stage": {"name": stage_dir.temporary_name, "identity": [stage_dir.identity[0], stage_dir.identity[1]]},
                                    "publishedIdentity": None,
                                })
                                try:
                                    self._write_record_locked(record)
                                except BaseException:
                                    discard_staged_directory(stage_dir)
                                    raise
                                identity = publish_staged_directory(stage_dir)
                                child["status"] = "committed"
                                child["publishedIdentity"] = [identity[0], identity[1]]
                                child["error"] = None
                            except AddressRejected as exc:
                                if exc.code == "conflict" and child.get("status") == "publishing":
                                    child.update({"status": "uncommitted", "stage": None, "error": "conflict"})
                                    parent["status"] = "partial" if any(item.get("status") == "committed" for item in children) else "failed"
                                    self._write_record_locked(record)
                                    break
                                if isinstance(child.get("stage"), dict):
                                    self._recover_item(catalog, child)
                                    if child.get("status") == "committed":
                                        self._write_record_locked(record)
                                        continue
                                    if child.get("status") == "indeterminate":
                                        parent["status"] = "indeterminate"
                                        self._write_record_locked(record)
                                        break
                                child["status"] = "uncommitted"
                                child["error"] = _from_address(exc).code
                                parent["status"] = "partial" if any(item.get("status") == "committed" for item in children) else "failed"
                                self._write_record_locked(record)
                                break
                        else:
                            writer = None
                            try:
                                child.update({"status": "uncommitted", "stage": None, "error": "not_started"})
                                self._write_record_locked(record)
                                writer = begin_stage_regular(catalog, target["rootId"], target["path"], maximum=ZIP_EXTRACT_MAX_FILE)
                                stage_info = os.fstat(writer.file_fd)
                                child.update({
                                    "status": "staging",
                                    "stage": {"name": writer.temporary_name, "identity": [stage_info.st_dev, stage_info.st_ino]},
                                    "publishedIdentity": None,
                                })
                                self._write_record_locked(record)
                                info = archive.infolist()[entry["zipIndex"]]
                                copied = 0
                                with archive.open(info, "r") as member:
                                    while True:
                                        block = member.read(64 * 1024)
                                        if not block:
                                            break
                                        copied += len(block)
                                        if copied > entry["size"]:
                                            raise OperationError("invalid")
                                        writer.write(block)
                                if copied != entry["size"]:
                                    raise OperationError("invalid")
                                staged = writer.finish()
                                writer = None
                                old_digest = child.get("digest")
                                if isinstance(old_digest, str) and old_digest != staged.digest:
                                    discard_staged_regular(staged)
                                    raise OperationError("conflict")
                                child.update({
                                    "status": "publishing",
                                    "digest": staged.digest,
                                    "size": staged.size,
                                    "stage": {"name": staged.temporary_name, "identity": [staged.identity[0], staged.identity[1]]},
                                    "stageKind": "file",
                                })
                                self._write_record_locked(record)
                                identity = publish_staged_regular(staged)
                                child["status"] = "committed"
                                child["publishedIdentity"] = [identity[0], identity[1]]
                                child["error"] = None
                            except AddressRejected as exc:
                                if writer is not None:
                                    writer.abort()
                                if exc.code == "conflict" and child.get("status") == "publishing":
                                    child.update({"status": "uncommitted", "stage": None, "error": "conflict"})
                                    parent["status"] = "partial" if any(item.get("status") == "committed" for item in children) else "failed"
                                    self._write_record_locked(record)
                                    break
                                if isinstance(child.get("stage"), dict):
                                    self._recover_item(catalog, child)
                                    if child.get("status") == "committed":
                                        self._write_record_locked(record)
                                        continue
                                    if child.get("status") == "indeterminate":
                                        parent["status"] = "indeterminate"
                                        self._write_record_locked(record)
                                        break
                                child["status"] = "uncommitted"
                                child["error"] = _from_address(exc).code
                                parent["status"] = "partial" if any(item.get("status") == "committed" for item in children) else "failed"
                                self._write_record_locked(record)
                                break
                            except OperationError as exc:
                                if writer is not None:
                                    writer.abort()
                                if isinstance(child.get("stage"), dict):
                                    self._recover_item(catalog, child)
                                    if child.get("status") == "committed":
                                        self._write_record_locked(record)
                                        continue
                                    if child.get("status") == "indeterminate":
                                        parent["status"] = "indeterminate"
                                        self._write_record_locked(record)
                                        break
                                child["status"] = "uncommitted"
                                child["error"] = exc.code
                                parent["status"] = "partial" if any(item.get("status") == "committed" for item in children) else "failed"
                                self._write_record_locked(record)
                                break
                            except (OSError, zipfile.BadZipFile, zlib.error, EOFError) as exc:
                                if writer is not None:
                                    writer.abort()
                                if isinstance(child.get("stage"), dict):
                                    self._recover_item(catalog, child)
                                    if child.get("status") == "committed":
                                        self._write_record_locked(record)
                                        continue
                                    if child.get("status") == "indeterminate":
                                        parent["status"] = "indeterminate"
                                        self._write_record_locked(record)
                                        break
                                child["status"] = "uncommitted"
                                child["error"] = (
                                    "limit"
                                    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
                                    else "unavailable" if isinstance(exc, OSError)
                                    else "invalid"
                                )
                                parent["status"] = "partial" if any(item.get("status") == "committed" for item in children) else "failed"
                                self._write_record_locked(record)
                                break
                            except BaseException:
                                if writer is not None:
                                    writer.abort()
                                raise
                        self._write_record_locked(record)
        parent["status"] = "indeterminate" if any(item.get("status") == "indeterminate" for item in children) else (
            "completed" if all(item.get("status") == "committed" for item in children) else (
            "partial" if any(item.get("status") == "committed" for item in children) else "failed"
            )
        )
        self._write_record_locked(record)
        return parent

    def _create_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
        now: float,
    ) -> dict[str, object]:
        if action["kind"] == "directory":
            destination = _destination_for_action(action, now, self.time_zone)
            intent = {
                "status": "staging",
                "action": "create",
                "destination": destination,
                "digest": None,
                "size": 0,
                "stage": None,
                "stageKind": "directory",
                "source": None,
                "publishedIdentity": None,
            }
            _replace_item(record["items"], index, intent)
            self._write_record_locked(record)
            try:
                stage = stage_directory_exclusive(catalog, destination["rootId"], destination["path"])
            except AddressRejected as exc:
                raise _from_address(exc) from exc
            intent["stage"] = {"name": stage.temporary_name, "identity": [stage.identity[0], stage.identity[1]]}
            intent["status"] = "publishing"
            try:
                self._write_record_locked(record)
            except BaseException:
                discard_staged_directory(stage)
                raise
            try:
                identity = publish_staged_directory(stage)
            except AddressRejected as exc:
                if exc.code == "conflict":
                    intent.update({"status": "uncommitted", "stage": None, "error": "conflict"})
                    self._write_record_locked(record)
                raise _from_address(exc) from exc
            intent["status"] = "committed"
            intent["publishedIdentity"] = [identity[0], identity[1]]
            return intent
        content = b""
        if action["kind"] == "note":
            content = ("# " + action["title"] + "\n").encode("utf-8")
        return self._publish_bytes_locked(record, index, action, catalog, now, content, source=None)

    def _copy_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
    ) -> dict[str, object]:
        if action.get("sourceKind") == "directory":
            return self._copy_directory_locked(record, index, action, catalog)
        source = action["source"]
        assert isinstance(source, dict)
        destination = action["destination"]
        assert isinstance(destination, dict)
        previous = record["items"][index] if index < len(record["items"]) else None
        prior_source = previous.get("source") if isinstance(previous, dict) else None
        writer = None
        try:
            with open_regular(catalog, source["rootId"], source["path"]) as (source_fd, info):
                initial = os.fstat(source_fd)
                source_record = {
                    "address": source,
                    "identity": [initial.st_dev, initial.st_ino],
                    "size": initial.st_size,
                    "mtimeNs": initial.st_mtime_ns,
                    "ctimeNs": initial.st_ctime_ns,
                }
                if isinstance(prior_source, dict) and any(
                    prior_source.get(key) != source_record.get(key)
                    for key in ("identity", "size", "mtimeNs", "ctimeNs")
                ):
                    raise OperationError("conflict")
                writer = begin_stage_regular(catalog, destination["rootId"], destination["path"])
                stage_info = os.fstat(writer.file_fd)
                _replace_item(record["items"], index, {
                    "status": "staging",
                    "action": "copy",
                    "destination": destination,
                    "digest": previous.get("digest") if isinstance(previous, dict) else None,
                    "size": initial.st_size,
                    "stage": {"name": writer.temporary_name, "identity": [stage_info.st_dev, stage_info.st_ino]},
                    "stageKind": "file",
                    "source": source_record,
                    "publishedIdentity": None,
                })
                self._write_record_locked(record)
                for block in _read_chunks(source_fd):
                    writer.write(block)
                final = os.fstat(source_fd)
                if any(getattr(final, key) != getattr(initial, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
                    raise OperationError("conflict")
                staged = writer.finish()
                writer = None
                if isinstance(previous, dict) and isinstance(previous.get("digest"), str) and previous["digest"] != staged.digest:
                    discard_staged_regular(staged)
                    raise OperationError("conflict")
        except AddressRejected as exc:
            if writer is not None:
                writer.abort()
            raise _from_address(exc) from exc
        except OperationError:
            if writer is not None:
                writer.abort()
            raise
        except OSError as exc:
            if writer is not None:
                writer.abort()
            raise OperationError("limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable") from exc
        except BaseException:
            if writer is not None:
                writer.abort()
            raise
        return self._publish_stage_locked(record, index, action, staged, source=source_record)

    def _copy_directory_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
    ) -> dict[str, object]:
        source = action["source"]
        destination = action["destination"]
        root_identity, root_version, entries, total = _copy_source_entries(catalog, source)
        if (
            action.get("sourceIdentity") != list(root_identity)
            or action.get("sourceDirectoryVersion") != root_version
            or action.get("plannedEntries") != entries
        ):
            raise OperationError("conflict")
        source_record = {
            "address": source,
            "identity": list(root_identity),
            "directoryVersion": root_version,
        }
        previous = record["items"][index] if index < len(record["items"]) else None
        prior_source = previous.get("source") if isinstance(previous, dict) else None
        if isinstance(prior_source, dict) and prior_source != source_record:
            raise OperationError("conflict")
        mode_file, mode_directory = new_object_modes(catalog, destination["rootId"])
        stage = stage_directory_exclusive(catalog, destination["rootId"], destination["path"])
        stage_fd = -1
        digest = hashlib.sha256()
        intent = {
            "status": "staging",
            "action": "copy",
            "destination": destination,
            "digest": None,
            "size": total,
            "stage": {"name": stage.temporary_name, "identity": [stage.identity[0], stage.identity[1]]},
            "stageKind": "directory",
            "source": source_record,
            "publishedIdentity": None,
        }
        try:
            _replace_item(record["items"], index, intent)
            try:
                self._write_record_locked(record)
            except BaseException:
                discard_staged_directory(stage)
                raise
            stage_fd = open_staged_directory(stage)
            for entry in entries:
                relative = entry["relativePath"]
                parent_path = relative.rpartition("/")[0]
                parent_fd = _open_staged_subdirectory(stage_fd, parent_path)
                try:
                    if entry["kind"] == "directory":
                        child_fd = create_staged_directory_child(parent_fd, relative.rsplit("/", 1)[-1], mode_directory)
                        os.close(child_fd)
                        digest.update(b"d\0" + relative.encode("utf-8") + b"\n")
                        continue
                    output_fd = create_staged_regular_child(parent_fd, relative.rsplit("/", 1)[-1], mode_file)
                    content_hash = hashlib.sha256()
                    copied = 0
                    try:
                        with open_regular(catalog, entry["address"]["rootId"], entry["address"]["path"]) as (input_fd, current):
                            if not _matches_file_snapshot(current, entry):
                                raise OperationError("conflict")
                            for block in _read_chunks(input_fd):
                                copied += len(block)
                                content_hash.update(block)
                                _write_all(output_fd, block)
                            final = os.fstat(input_fd)
                            if copied != entry["size"] or not _matches_file_snapshot(final, entry):
                                raise OperationError("conflict")
                        os.fsync(output_fd)
                    finally:
                        os.close(output_fd)
                    os.fsync(parent_fd)
                    digest.update(
                        b"f\0"
                        + relative.encode("utf-8")
                        + b"\0"
                        + str(copied).encode("ascii")
                        + b"\0"
                        + content_hash.hexdigest().encode("ascii")
                        + b"\n"
                    )
                finally:
                    os.close(parent_fd)
            refreshed_identity, refreshed_version, refreshed_entries, refreshed_total = _copy_source_entries(catalog, source)
            if (
                list(refreshed_identity) != list(root_identity)
                or refreshed_version != root_version
                or refreshed_entries != entries
                or refreshed_total != total
            ):
                raise OperationError("conflict")
            if isinstance(previous, dict) and isinstance(previous.get("digest"), str) and previous["digest"] != digest.hexdigest():
                raise OperationError("conflict")
            intent.update({
                "status": "publishing",
                "digest": digest.hexdigest(),
                "size": total,
            })
            self._write_record_locked(record)
            try:
                identity = publish_staged_directory(stage)
            except AddressRejected as exc:
                if exc.code == "conflict":
                    intent.update({"status": "uncommitted", "stage": None, "error": "conflict"})
                    self._write_record_locked(record)
                raise _from_address(exc) from exc
            intent["status"] = "committed"
            intent["publishedIdentity"] = [identity[0], identity[1]]
            return intent
        except AddressRejected as exc:
            raise _from_address(exc) from exc
        except OSError as exc:
            raise OperationError("limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable") from exc
        finally:
            if stage_fd >= 0:
                os.close(stage_fd)
            if stage.parent_fd >= 0:
                discard_staged_directory(stage)

    def _publish_bytes_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        catalog: RootCatalog,
        now: float,
        content: bytes,
        *,
        source: dict[str, object] | None,
    ) -> dict[str, object]:
        last: OperationError | None = None
        reserved = {
            (target.get("rootId"), target.get("path"))
            for other_index, other in enumerate(record["actions"])
            if other_index != index and isinstance(other, dict)
            for target in other.get("plannedTargets", [])
            if isinstance(target, dict)
        }
        for proposal in range(1, 31):
            destination = _destination_for_action(action, now, self.time_zone, proposal)
            if action["nameMode"] == "generated" and (destination["rootId"], destination["path"]) in reserved:
                continue
            try:
                if destination_exists(catalog, destination["rootId"], destination["path"]):
                    if action["nameMode"] == "generated":
                        last = OperationError("conflict")
                        continue
                    raise OperationError("conflict")
            except AddressRejected as exc:
                raise _from_address(exc) from exc
            try:
                staged = stage_regular(catalog, destination["rootId"], destination["path"], (content,))
            except AddressRejected as exc:
                raise _from_address(exc) from exc
            try:
                return self._publish_stage_locked(record, index, action, staged, source=source, destination=destination)
            except OperationError as exc:
                if exc.code != "conflict" or action["nameMode"] != "generated":
                    raise
                last = exc
        raise last or OperationError("conflict")

    def _publish_stage_locked(
        self,
        record: dict[str, object],
        index: int,
        action: dict[str, object],
        staged: StagedRegular,
        *,
        source: dict[str, object] | None,
        destination: dict[str, str] | None = None,
    ) -> dict[str, object]:
        target = destination or action["destination"]
        assert isinstance(target, dict)
        item = {
            "status": "publishing",
            "action": action["action"],
            "destination": target,
            "digest": staged.digest,
            "size": staged.size,
            "stage": {
                "name": staged.temporary_name,
                "identity": [staged.identity[0], staged.identity[1]],
            },
            "source": source,
            "publishedIdentity": None,
        }
        _replace_item(record["items"], index, item)
        try:
            self._write_record_locked(record)
        except BaseException:
            try:
                discard_staged_regular(staged)
            except OSError:
                item.update({"status": "indeterminate", "error": "publication_unknown"})
            else:
                # The staged inode is gone and publish_staged_regular has not
                # run. Keep this live per-item no-commit fact for the response;
                # the failed journal write must not turn it into an empty 207.
                item.update({"status": "uncommitted", "stage": None, "error": "storage_unavailable"})
            raise
        try:
            identity = publish_staged_regular(staged)
        except AddressRejected as exc:
            if exc.code == "conflict":
                item.update({"status": "uncommitted", "stage": None, "error": "conflict"})
                self._write_record_locked(record)
            raise _from_address(exc) from exc
        item["status"] = "committed"
        item["publishedIdentity"] = [identity[0], identity[1]]
        return item

    @contextmanager
    def _lock(self) -> Iterator[None]:
        directory = self._directory()
        lock = self.state_directory / "operations.lock"
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise StateError("operation lock is unavailable") from exc
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.geteuid():
                raise StateError("operation lock is invalid")
            os.fchmod(fd, 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise StateError("operation lock permissions are invalid")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _lease_matches(self, lease: UploadLease | None, identifier: object) -> bool:
        return (
            isinstance(lease, UploadLease)
            and lease.fd >= 0
            and lease.identifier == identifier
            and lease.instance_id == self.instance_id
            and lease.state_directory == self.state_directory
        )

    def _try_upload_lock_locked(self, identifier: object) -> int | None:
        if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise StateError("operation identifier is invalid")
        path = self._directory() / f"upload-{identifier}.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise StateError("upload operation lock is unavailable") from exc
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.geteuid():
                raise StateError("upload operation lock is invalid")
            os.fchmod(fd, 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise StateError("upload operation lock permissions are invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}:
                    os.close(fd)
                    return None
                raise StateError("upload operation lock is unavailable") from exc
            return fd
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    @staticmethod
    def _close_upload_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _upload_lock_path(self, identifier: object) -> Path:
        if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise StateError("operation identifier is invalid")
        return self._directory() / f"upload-{identifier}.lock"

    def _directory(self) -> Path:
        directory = self.state_directory / "operations"
        try:
            details = directory.lstat()
        except OSError as exc:
            raise StateError("operation directory is unavailable") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise StateError("operation directory is unavailable")
        return directory

    def _records_locked(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        try:
            directory = self._directory()
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if re.fullmatch(r"upload-[0-9a-f]{32}\.lock", path.name):
                    try:
                        details = path.lstat()
                    except OSError as exc:
                        raise StateError("upload operation lock is invalid") from exc
                    if (
                        not stat.S_ISREG(details.st_mode)
                        or details.st_nlink != 1
                        or details.st_uid != os.geteuid()
                        or stat.S_IMODE(details.st_mode) != 0o600
                    ):
                        raise StateError("upload operation lock is invalid")
                    continue
                if re.fullmatch(r"\.op-[0-9a-f]{32}\.json\.[0-9a-f]{16}\.tmp", path.name):
                    _remove_operation_temporary(path)
                    continue
                if re.fullmatch(r"op-[0-9a-f]{32}\.json", path.name) is None:
                    raise StateError("operation journal is invalid")
                try:
                    details = path.lstat()
                except OSError as exc:
                    raise StateError("operation journal is invalid") from exc
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_nlink != 1
                    or details.st_uid != os.geteuid()
                    or stat.S_IMODE(details.st_mode) != 0o600
                ):
                    raise StateError("operation journal is invalid")
                records.append(_read_record(path, self.instance_id))
        except (OSError, StateError) as exc:
            # A malformed or unreadable journal can conceal an earlier commit.
            # Do not expose it as a user conflict or continue from a partial scan.
            raise OperationError("indeterminate") from exc
        return records

    def _find_record_locked(self, token: OperationToken) -> dict[str, object] | None:
        try:
            path = self._record_path(token.identifier)
            return _read_record(path, self.instance_id, token)
        except StateError as exc:
            cause: BaseException | None = exc
            while cause is not None:
                if isinstance(cause, FileNotFoundError):
                    # Revalidate the containing directory: a missing journal in
                    # an intact private directory is not-found; a vanished or
                    # inaccessible journal directory is storage indeterminate.
                    try:
                        self._directory()
                    except StateError as directory_error:
                        raise OperationError("indeterminate") from directory_error
                    return None
                cause = cause.__cause__
            raise OperationError("indeterminate") from exc
        except OSError as exc:
            raise OperationError("indeterminate") from exc

    def _write_record_locked(self, record: dict[str, object]) -> None:
        _validate_record(record, self.instance_id)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > OPERATION_RECORD_MAX_BYTES:
            raise StateError("operation record exceeds its size limit")
        atomic_write(self._record_path(record["tokenId"]), payload)

    def _record_path(self, identifier: object) -> Path:
        if not isinstance(identifier, str) or len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            raise StateError("operation identifier is invalid")
        return self._directory() / f"{_RECORD}{identifier}{_SUFFIX}"

    def _garbage_collect_locked(self, now: float) -> None:
        for record in self._records_locked():
            if record["status"] == "issued" and now >= record["expiresAt"]:
                record["status"] = "failed"
                record["finishedAt"] = record["expiresAt"]
                self._write_record_locked(record)
            if record["status"] not in {"completed", "partial", "failed"}:
                continue
            if now < record["expiresAt"] + TERMINAL_RETENTION_SECONDS:
                continue
            path = self._record_path(record["tokenId"])
            upload_fd = self._try_upload_lock_locked(record["tokenId"])
            if upload_fd is None:
                continue
            try:
                path.unlink()
                _sync_directory(path.parent)
                upload_lock_path = self._upload_lock_path(record["tokenId"])
                try:
                    upload_lock_path.unlink()
                except FileNotFoundError:
                    pass
                _sync_directory(path.parent)
            finally:
                self._close_upload_lock(upload_fd)

        retained_ids = {
            match.group(1)
            for path in self._directory().iterdir()
            if (match := re.fullmatch(r"op-([0-9a-f]{32})\.json", path.name)) is not None
        }
        for path in self._directory().iterdir():
            match = re.fullmatch(r"upload-([0-9a-f]{32})\.lock", path.name)
            if match is None or match.group(1) in retained_ids:
                continue
            upload_fd = self._try_upload_lock_locked(match.group(1))
            if upload_fd is None:
                continue
            try:
                if not self._record_path(match.group(1)).exists():
                    path.unlink()
                    _sync_directory(path.parent)
            finally:
                self._close_upload_lock(upload_fd)

    def _seal(self, token: OperationToken) -> str:
        body = f"{_TOKEN_VERSION}.{token.identifier}.{token.issued_at}.{token.expires_at}"
        mac = hmac.new(
            read_signing_secret(self.state_directory),
            (self.instance_id + "|" + body).encode("ascii"),
            hashlib.sha256,
        ).digest()
        return body + "." + _base64(mac)

    def _open(self, value: object) -> OperationToken:
        if not isinstance(value, str):
            raise OperationError("invalid")
        parts = value.split(".")
        if len(parts) != 5 or parts[0] != _TOKEN_VERSION:
            raise OperationError("invalid")
        _version, identifier, issued, expires, presented = parts
        if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            raise OperationError("invalid")
        try:
            issued_at = int(issued)
            expires_at = int(expires)
        except ValueError as exc:
            raise OperationError("invalid") from exc
        if issued_at < 0 or expires_at != issued_at + TOKEN_LIFETIME_SECONDS:
            raise OperationError("invalid")
        body = ".".join(parts[:4])
        expected = hmac.new(
            read_signing_secret(self.state_directory),
            (self.instance_id + "|" + body).encode("ascii"),
            hashlib.sha256,
        ).digest()
        try:
            actual = _unbase64(presented)
        except ValueError as exc:
            raise OperationError("invalid") from exc
        if len(actual) != len(expected) or not hmac.compare_digest(actual, expected):
            raise OperationError("invalid")
        return OperationToken(identifier, issued_at, expires_at)

    def _result(self, record: dict[str, object]) -> dict[str, object]:
        items: list[dict[str, object]] = []
        for index, item in enumerate(record["items"]):
            if not isinstance(item, dict):
                continue
            children = item.get("children")
            if isinstance(children, list):
                items.extend(
                    _public_item(child, index, sub_index=child_index)
                    for child_index, child in enumerate(children)
                    if isinstance(child, dict)
                )
            else:
                items.append(_public_item(item, index))
        return {
            "operationToken": self._seal(OperationToken(record["tokenId"], record["issuedAt"], record["expiresAt"])),
            "issuedAt": record["issuedAt"],
            "expiresAt": record["expiresAt"],
            "status": record["status"],
            "committed": [item for item in items if item["status"] == "committed"],
            "uncommitted": [item for item in items if item["status"] in {"uncommitted", "failed"}],
            "indeterminate": [item for item in items if item["status"] == "indeterminate"],
        }


def _normalize_actions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise OperationError("invalid")
    if len(value) > UPLOAD_MAX_FILES:
        raise OperationError("limit")
    result: list[dict[str, object]] = []
    for action in value:
        if not isinstance(action, dict) or not isinstance(action.get("action"), str):
            raise OperationError("invalid")
        if action["action"] == "create":
            result.append(_normalize_create(action))
        elif action["action"] == "copy":
            if set(action) != {"action", "source", "destination"}:
                raise OperationError("invalid")
            result.append({"action": "copy", "source": _address(action["source"]), "destination": _address(action["destination"], nonempty=True)})
        elif action["action"] == "zip":
            if set(action) != {"action", "sources", "destination"} or not isinstance(action["sources"], list):
                raise OperationError("invalid")
            if not action["sources"] or len(action["sources"]) > ZIP_SOURCE_MAX_ENTRIES:
                raise OperationError("limit" if len(action["sources"]) > ZIP_SOURCE_MAX_ENTRIES else "invalid")
            sources = [_address(source, nonempty=True) for source in action["sources"]]
            if len({(source["rootId"], source["path"]) for source in sources}) != len(sources):
                raise OperationError("invalid")
            result.append({"action": "zip", "sources": sources, "destination": _address(action["destination"], nonempty=True)})
        elif action["action"] == "extract":
            if set(action) != {"action", "source", "destination"}:
                raise OperationError("invalid")
            result.append({
                "action": "extract",
                "source": _address(action["source"], nonempty=True),
                "destination": _address(action["destination"]),
            })
        elif action["action"] in {"move", "rename"}:
            if set(action) != {"action", "source", "destination"}:
                raise OperationError("invalid")
            result.append({
                "action": "move",
                "source": _address(action["source"], nonempty=True),
                "destination": _address(action["destination"], nonempty=True),
            })
        elif action["action"] == "upload":
            raise OperationError("not_available")
        else:
            raise OperationError("invalid")
    return result


def _zip_source_entries(catalog: RootCatalog, sources: list[dict[str, str]]) -> tuple[list[dict[str, object]], int]:
    entries: list[dict[str, object]] = []
    names: set[str] = set()
    total = 0

    def add(address: dict[str, str], archive_name: str, depth: int) -> None:
        nonlocal total
        if depth > ZIP_MAX_DEPTH:
            raise OperationError("limit")
        try:
            inspection = inspect_address(catalog, address["rootId"], address["path"], operation="read")
        except AddressRejected as exc:
            raise _from_address(exc) from exc
        if not inspection.allowed:
            raise OperationError("forbidden")
        if inspection.is_directory:
            plain_name = archive_name.rstrip("/")
            if plain_name in names:
                raise OperationError("invalid")
            names.add(plain_name)
            identity = inspect_operation_object(catalog, address["rootId"], address["path"], expected_kind="directory")
            if identity is None:
                raise OperationError("conflict")
            entries.append({
                "kind": "directory",
                "archiveName": plain_name + "/",
                "address": address,
                "identity": list(identity[0]),
                "directoryVersion": directory_version(catalog, address["rootId"], address["path"]),
            })
            try:
                children = list_directory(catalog, address["rootId"], address["path"], strict=True)
            except AddressRejected as exc:
                raise _from_address(exc) from exc
            for child in children:
                if child.kind == "link":
                    raise OperationError("forbidden")
                _name(child.name)
                child_path = _join(address["path"], child.name)
                child_address = {"rootId": address["rootId"], "path": child_path}
                add(child_address, plain_name + "/" + child.name, depth + 1)
        elif inspection.is_regular:
            name = archive_name.rstrip("/")
            if name in names:
                raise OperationError("invalid")
            names.add(name)
            try:
                with open_regular(catalog, address["rootId"], address["path"]) as (_fd, info):
                    total += info.st_size
                    if total > ZIP_SOURCE_MAX_BYTES:
                        raise OperationError("limit")
                    entries.append({
                        "kind": "file",
                        "archiveName": name,
                        "address": address,
                        "identity": [info.st_dev, info.st_ino],
                        "size": info.st_size,
                        "mtimeNs": info.st_mtime_ns,
                        "ctimeNs": info.st_ctime_ns,
                    })
            except AddressRejected as exc:
                raise _from_address(exc) from exc
        else:
            raise OperationError("forbidden")
        if len(entries) > ZIP_SOURCE_MAX_ENTRIES:
            raise OperationError("limit")

    for source in sources:
        name = source["path"].rsplit("/", 1)[-1]
        _name(name)
        add(source, name, 1)
    return entries, total


def _copy_source_entries(
    catalog: RootCatalog,
    source: dict[str, str],
) -> tuple[tuple[int, int], str, list[dict[str, object]], int]:
    """Capture a bounded, descriptor-checked recursive copy plan."""
    try:
        inspection = inspect_address(catalog, source["rootId"], source["path"], operation="read")
    except AddressRejected as exc:
        raise _from_address(exc) from exc
    if not inspection.allowed or not inspection.is_directory:
        raise OperationError("forbidden")
    identity = inspect_operation_object(catalog, source["rootId"], source["path"], expected_kind="directory")
    if identity is None:
        raise OperationError("conflict")
    root_identity = identity[0]
    root_version = directory_version(catalog, source["rootId"], source["path"])
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    total = 0
    path_bytes = 0

    def visit(address: dict[str, str], relative_parent: str, depth: int) -> None:
        nonlocal total, path_bytes
        before_version = directory_version(catalog, address["rootId"], address["path"])
        try:
            children = list_directory(catalog, address["rootId"], address["path"], strict=True)
        except AddressRejected as exc:
            raise _from_address(exc) from exc
        for child in children:
            _name(child.name)
            relative = _join(relative_parent, child.name)
            child_depth = depth + 1
            if child_depth > ZIP_MAX_DEPTH:
                raise OperationError("limit")
            if relative in seen:
                raise OperationError("invalid")
            seen.add(relative)
            path_bytes += len(relative.encode("utf-8"))
            if path_bytes > 8 * 1024 * 1024:
                raise OperationError("limit")
            child_address = {"rootId": address["rootId"], "path": _join(address["path"], child.name)}
            if child.kind == "directory":
                observed = inspect_operation_object(
                    catalog,
                    child_address["rootId"],
                    child_address["path"],
                    expected_kind="directory",
                )
                if observed is None:
                    raise OperationError("conflict")
                child_version = directory_version(catalog, child_address["rootId"], child_address["path"])
                entries.append({
                    "relativePath": relative,
                    "kind": "directory",
                    "address": child_address,
                    "identity": list(observed[0]),
                    "directoryVersion": child_version,
                    "size": 0,
                })
                visit(child_address, relative, child_depth)
            elif child.kind == "file":
                try:
                    with open_regular(catalog, child_address["rootId"], child_address["path"]) as (_fd, info):
                        total += info.st_size
                        if total > ZIP_SOURCE_MAX_BYTES:
                            raise OperationError("limit")
                        entries.append({
                            "relativePath": relative,
                            "kind": "file",
                            "address": child_address,
                            "identity": [info.st_dev, info.st_ino],
                            "size": info.st_size,
                            "mtimeNs": info.st_mtime_ns,
                            "ctimeNs": info.st_ctime_ns,
                        })
                except AddressRejected as exc:
                    raise _from_address(exc) from exc
            else:
                raise OperationError("forbidden")
            if len(entries) > ZIP_SOURCE_MAX_ENTRIES:
                raise OperationError("limit")
        if directory_version(catalog, address["rootId"], address["path"]) != before_version:
            raise OperationError("conflict")

    visit(source, "", 0)
    if directory_version(catalog, source["rootId"], source["path"]) != root_version:
        raise OperationError("conflict")
    return root_identity, root_version, entries, total


def _directory_digest(
    catalog: RootCatalog,
    address: dict[str, str],
) -> tuple[tuple[int, int], str]:
    """Hash a stable, strict directory tree using the copy publication format."""
    identity, version, entries, total = _copy_source_entries(catalog, address)
    digest = hashlib.sha256()
    for entry in entries:
        relative = entry["relativePath"]
        if entry["kind"] == "directory":
            digest.update(b"d\0" + relative.encode("utf-8") + b"\n")
            continue
        content_hash = hashlib.sha256()
        copied = 0
        with open_regular(catalog, entry["address"]["rootId"], entry["address"]["path"]) as (fd, current):
            if not _matches_file_snapshot(current, entry):
                raise OperationError("conflict")
            for block in _read_chunks(fd):
                copied += len(block)
                content_hash.update(block)
            final = os.fstat(fd)
            if copied != entry["size"] or not _matches_file_snapshot(final, entry):
                raise OperationError("conflict")
        digest.update(
            b"f\0"
            + relative.encode("utf-8")
            + b"\0"
            + str(copied).encode("ascii")
            + b"\0"
            + content_hash.hexdigest().encode("ascii")
            + b"\n"
        )
    refreshed_identity, refreshed_version, refreshed_entries, refreshed_total = _copy_source_entries(catalog, address)
    if (
        refreshed_identity != identity
        or refreshed_version != version
        or refreshed_entries != entries
        or refreshed_total != total
    ):
        raise OperationError("conflict")
    return identity, digest.hexdigest()


def _matches_file_snapshot(info: os.stat_result, entry: dict[str, object]) -> bool:
    return (
        [info.st_dev, info.st_ino] == entry.get("identity")
        and info.st_size == entry.get("size")
        and info.st_mtime_ns == entry.get("mtimeNs")
        and info.st_ctime_ns == entry.get("ctimeNs")
    )


def _open_staged_subdirectory(root_fd: int, relative: str) -> int:
    current = os.dup(root_fd)
    if relative == "":
        return current
    try:
        for part in parse_relative_path(relative):
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _write_all(fd: int, block: bytes) -> None:
    view = memoryview(block)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("staged file write was incomplete")
        view = view[written:]


def _extract_plan(
    catalog: RootCatalog,
    source: dict[str, str],
) -> tuple[list[dict[str, object]], int, str]:
    try:
        handle = open_regular(catalog, source["rootId"], source["path"])
        with handle as (fd, source_info):
            initial = os.fstat(fd)
            source_digest = _digest_fd(fd, source_info.st_size)
            hashed = os.fstat(fd)
            if any(
                getattr(initial, key) != getattr(hashed, key)
                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise OperationError("conflict")
            with os.fdopen(os.dup(fd), "rb", closefd=True) as archive_stream:
                with zipfile.ZipFile(archive_stream, "r") as archive:
                    infos = archive.infolist()
                    if not infos or len(infos) > ZIP_SOURCE_MAX_ENTRIES:
                        raise OperationError("limit" if len(infos) > ZIP_SOURCE_MAX_ENTRIES else "invalid")
                    planned: dict[str, dict[str, object]] = {}
                    explicit: set[str] = set()
                    total_size = 0
                    compressed_total = 0
                    for index, info in enumerate(infos):
                        raw_name = getattr(info, "orig_filename", info.filename)
                        if not isinstance(raw_name, str) or "\x00" in raw_name or "\\" in raw_name or raw_name.startswith("/"):
                            raise OperationError("invalid")
                        directory = raw_name.endswith("/")
                        name = raw_name[:-1] if directory else raw_name
                        try:
                            parts = parse_relative_path(name)
                        except AddressRejected as exc:
                            raise OperationError("invalid") from exc
                        if not parts or any(re.match(r"^[A-Za-z]:", part) for part in parts):
                            raise OperationError("invalid")
                        for part in parts:
                            _name(part)
                        if len(parts) > ZIP_MAX_DEPTH:
                            raise OperationError("limit")
                        mode = info.external_attr >> 16
                        file_type = stat.S_IFMT(mode)
                        expected_type = stat.S_IFDIR if directory else stat.S_IFREG
                        if (
                            file_type not in {0, expected_type}
                            or info.flag_bits & 1
                            or info.compress_type not in {
                                zipfile.ZIP_STORED,
                                zipfile.ZIP_DEFLATED,
                                zipfile.ZIP_BZIP2,
                                zipfile.ZIP_LZMA,
                            }
                        ):
                            raise OperationError("invalid")
                        if name in explicit:
                            raise OperationError("invalid")
                        explicit.add(name)
                        existing_name = planned.get(name)
                        if not directory and existing_name is not None and existing_name["kind"] == "directory":
                            raise OperationError("invalid")
                        if directory:
                            if info.file_size != 0:
                                raise OperationError("invalid")
                            current = {"path": name, "kind": "directory", "size": 0, "zipIndex": index}
                        else:
                            if info.file_size > ZIP_EXTRACT_MAX_FILE:
                                raise OperationError("limit")
                            total_size += info.file_size
                            compressed_total += info.compress_size
                            if total_size > ZIP_EXTRACT_MAX_BYTES:
                                raise OperationError("limit")
                            if info.file_size and (not info.compress_size or info.file_size / info.compress_size > ZIP_MAX_RATIO):
                                raise OperationError("limit")
                            current = {"path": name, "kind": "file", "size": info.file_size, "zipIndex": index}
                        planned[name] = current
                        parents = parts[:-1]
                        for parent_count in range(1, len(parts)):
                            parent = "/".join(parts[:parent_count])
                            existing = planned.get(parent)
                            if existing is not None and existing["kind"] != "directory":
                                raise OperationError("invalid")
                            if existing is None:
                                planned[parent] = {"path": parent, "kind": "directory", "size": 0, "zipIndex": None, "implicit": True}
                    if total_size and (not compressed_total or total_size / compressed_total > ZIP_MAX_RATIO):
                        raise OperationError("limit")
                    if len(planned) > ZIP_SOURCE_MAX_ENTRIES:
                        raise OperationError("limit")
                    result = sorted(
                        planned.values(),
                        key=lambda item: (
                            0 if item["kind"] == "directory" else 1,
                            len(item["path"].split("/")) if item["kind"] == "directory" else 0,
                            item["path"].encode("utf-8"),
                        ),
                    )
                    final = os.fstat(fd)
                    if any(
                        getattr(initial, key) != getattr(final, key)
                        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                    ) or _digest_fd(fd, source_info.st_size) != source_digest:
                        raise OperationError("conflict")
                    return result, total_size, source_digest
    except AddressRejected as exc:
        raise _from_address(exc) from exc
    except OSError as exc:
        raise OperationError(
            "limit" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} else "unavailable"
        ) from exc
    except (zipfile.BadZipFile, EOFError, UnicodeError) as exc:
        raise OperationError("invalid") from exc


def _digest_fd(fd: int, expected_size: int | None = None) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        block = os.pread(fd, 64 * 1024, offset)
        if not block:
            break
        digest.update(block)
        offset += len(block)
        if expected_size is not None and offset > expected_size:
            raise OperationError("conflict")
    if expected_size is not None and offset != expected_size:
        raise OperationError("conflict")
    return digest.hexdigest()


def _preflight_extraction_destinations(
    catalog: RootCatalog,
    destination: dict[str, str],
    entries: list[dict[str, object]],
    *,
    ignore_paths: set[str] | None = None,
) -> None:
    root = inspect_address(catalog, destination["rootId"], destination["path"], operation="mutate")
    if not root.allowed or not root.is_directory:
        raise OperationError("forbidden")
    planned_directories: set[str] = set()
    occupied: set[str] = set()
    ignored = ignore_paths or set()
    for entry in entries:
        relative = entry["path"]
        target = _join(destination["path"], relative)
        validate_new_address(catalog, destination["rootId"], target)
        parents = relative.split("/")[:-1]
        nearest_parent = destination["path"]
        for part in parents:
            nearest_parent = _join(nearest_parent, part)
            if nearest_parent not in planned_directories:
                try:
                    inspected = inspect_address(catalog, destination["rootId"], nearest_parent, operation="mutate")
                except AddressRejected as exc:
                    if exc.code != "not_found":
                        raise _from_address(exc) from exc
                else:
                    if not inspected.allowed or not inspected.is_directory:
                        raise OperationError("conflict")
        if target in occupied:
            raise OperationError("invalid")
        occupied.add(target)
        if relative in ignored:
            if entry["kind"] == "directory":
                planned_directories.add(target)
            continue
        if entry["kind"] == "directory":
            planned_directories.add(target)
            try:
                if destination_exists(catalog, destination["rootId"], target):
                    raise OperationError("conflict")
            except AddressRejected as exc:
                if exc.code not in {"forbidden", "not_found"}:
                    raise _from_address(exc) from exc
        else:
            try:
                if destination_exists(catalog, destination["rootId"], target):
                    raise OperationError("conflict")
            except AddressRejected as exc:
                if exc.code not in {"forbidden", "not_found"}:
                    raise _from_address(exc) from exc


def _plan_actions(actions: list[dict[str, object]], now: float, zone: ZoneInfo) -> list[dict[str, object]]:
    """Persist generated names at server commit time for safe same-token replay."""
    planned: list[dict[str, object]] = []
    for action in actions:
        if action["action"] == "create" and action["nameMode"] == "generated":
            planned.append({
                **action,
                "createdAt": int(now),
                "plannedDestination": _destination_for_action(action, now, zone),
            })
        else:
            planned.append(action)
    return planned


def _normalize_upload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OperationError("invalid")
    if set(value) != {"action", "destination", "nameMode", "files"}:
        raise OperationError("invalid")
    if value["action"] != "upload" or value["nameMode"] not in {"original", "generated"}:
        raise OperationError("invalid")
    destination = _address(value["destination"])
    files = value["files"]
    if not isinstance(files, list) or not files or len(files) > UPLOAD_MAX_FILES:
        raise OperationError("limit" if isinstance(files, list) and len(files) > UPLOAD_MAX_FILES else "invalid")
    mode = value["nameMode"]
    normalized_files: list[dict[str, object]] = []
    total_size = 0
    for item in files:
        if not isinstance(item, dict):
            raise OperationError("invalid")
        required = {"originalName", "contentDigest", "contentSize"} if mode == "original" else {
            "subject", "extension", "contentDigest", "contentSize"
        }
        if set(item) != required:
            raise OperationError("invalid")
        digest = item["contentDigest"]
        size = item["contentSize"]
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise OperationError("invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > UPLOAD_MAX_FILE:
            raise OperationError("limit" if isinstance(size, int) and size > UPLOAD_MAX_FILE else "invalid")
        total_size += size
        if total_size > UPLOAD_MAX_TOTAL:
            raise OperationError("limit")
        if mode == "original":
            normalized_files.append({"originalName": _name(item["originalName"]), "contentDigest": digest, "contentSize": size})
        else:
            if not _text(item["subject"], 1024) or not _extension(item["extension"]):
                raise OperationError("invalid")
            normalized_files.append({
                "subject": item["subject"],
                "extension": item["extension"],
                "contentDigest": digest,
                "contentSize": size,
            })
    return {"action": "upload", "destination": destination, "nameMode": mode, "files": normalized_files}


def _plan_upload(action: dict[str, object], now: float, zone: ZoneInfo, catalog: RootCatalog) -> dict[str, object]:
    parent = action["destination"]
    assert isinstance(parent, dict)
    planned: list[dict[str, object]] = []
    occupied: set[tuple[str, str]] = set()
    capacity_needed: dict[int, int] = {}
    capacity_available: dict[int, int] = {}
    for item in action["files"]:
        assert isinstance(item, dict)
        if action["nameMode"] == "original":
            name = item["originalName"]
            destination = {"rootId": parent["rootId"], "path": _join(parent["path"], name)}
        else:
            single = {
                "nameMode": "generated",
                "destination": parent,
                "subject": item["subject"],
                "extension": item["extension"],
            }
            destination = None
            for proposal in range(1, 31):
                candidate = _destination_for_action(single, now, zone, proposal)
                identity = (candidate["rootId"], candidate["path"])
                if identity in occupied:
                    continue
                try:
                    exists = destination_exists(catalog, candidate["rootId"], candidate["path"])
                except AddressRejected as exc:
                    raise _from_address(exc) from exc
                if not exists:
                    destination = candidate
                    break
            if destination is None:
                raise OperationError("conflict")
        identity = (destination["rootId"], destination["path"])
        if identity in occupied:
            raise OperationError("conflict")
        try:
            if action["nameMode"] == "original" and destination_exists(catalog, destination["rootId"], destination["path"]):
                raise OperationError("conflict")
            device, available = destination_capacity(catalog, destination["rootId"], destination["path"])
        except AddressRejected as exc:
            raise _from_address(exc) from exc
        capacity_needed[device] = capacity_needed.get(device, 0) + item["contentSize"]
        capacity_available[device] = available
        occupied.add(identity)
        planned.append({**item, "destination": destination})
    if any(capacity_needed[device] > capacity_available[device] for device in capacity_needed):
        raise OperationError("limit")
    return {**action, "files": planned}


def _normalize_create(action: dict[str, object]) -> dict[str, object]:
    permitted = {"action", "kind", "nameMode", "destination", "title", "subject", "extension"}
    if not set(action) <= permitted or not {"action", "kind", "nameMode", "destination"} <= set(action):
        raise OperationError("invalid")
    kind = action["kind"]
    mode = action["nameMode"]
    if kind not in {"file", "directory", "note"} or mode not in {"exact", "generated"}:
        raise OperationError("invalid")
    if kind == "directory" and mode != "exact":
        raise OperationError("invalid")
    destination = _address(action["destination"], nonempty=mode == "exact")
    title = action.get("title", "")
    if kind == "note" and not _text(title, 1024):
        raise OperationError("invalid")
    if kind != "note" and "title" in action:
        raise OperationError("invalid")
    if mode == "exact":
        if "subject" in action or "extension" in action:
            raise OperationError("invalid")
        return {"action": "create", "kind": kind, "nameMode": mode, "destination": destination, "title": title}
    subject = action.get("subject")
    extension = action.get("extension")
    if not _text(subject, 1024) or not _extension(extension):
        raise OperationError("invalid")
    if kind == "note" and extension != "md":
        raise OperationError("invalid")
    return {
        "action": "create",
        "kind": kind,
        "nameMode": mode,
        "destination": destination,
        "title": title,
        "subject": subject,
        "extension": extension,
    }


def _address(value: object, *, nonempty: bool = False) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"rootId", "path"}:
        raise OperationError("invalid")
    root_id = value["rootId"]
    path = value["path"]
    if not _text(root_id, 64) or not isinstance(path, str):
        raise OperationError("invalid")
    try:
        parsed = parse_relative_path(path)
    except AddressRejected as exc:
        raise OperationError("invalid") from exc
    if nonempty and not parsed:
        raise OperationError("invalid")
    return {"rootId": root_id, "path": path}


def _destination_for_action(action: dict[str, object], now: float, zone: ZoneInfo, proposal: int = 1) -> dict[str, str]:
    destination = action["destination"]
    assert isinstance(destination, dict)
    if action["nameMode"] == "exact":
        return destination
    if proposal == 1 and isinstance(action.get("plannedDestination"), dict):
        return action["plannedDestination"]
    now = action.get("createdAt", now)
    subject = _slug(action["subject"])
    extension = action["extension"]
    timestamp = datetime.fromtimestamp(now, zone).strftime("%Y%m%d-%H%M%S")
    suffix = "" if proposal == 1 else f"-{proposal}"
    name = f"{subject}-{timestamp}{suffix}.{extension}"
    parent = destination["path"]
    return {"rootId": destination["rootId"], "path": name if parent == "" else parent + "/" + name}


def _slug(value: object) -> str:
    import unicodedata

    assert isinstance(value, str)
    normalized = unicodedata.normalize("NFD", value)
    blocks: list[str] = []
    current: list[str] = []
    for character in normalized:
        if unicodedata.combining(character):
            continue
        lower = character.lower()
        if "a" <= lower <= "z" or "0" <= lower <= "9":
            current.append(lower)
        elif current:
            blocks.append("".join(current))
            current = []
    if current:
        blocks.append("".join(current))
    slug = "-".join(blocks)
    if not slug:
        return "nota"
    if len(slug) <= 60:
        return slug
    cut = slug[:60]
    hyphen = cut.rfind("-")
    return cut[:hyphen] if hyphen > 0 else cut


def _read_chunks(fd: int) -> Iterator[bytes]:
    while True:
        block = os.read(fd, 64 * 1024)
        if not block:
            return
        yield block


class _StageOutput:
    """The non-seekable stream accepted by zipfile for data-descriptor ZIPs."""

    def __init__(self, writer: object) -> None:
        self.writer = writer

    def write(self, value: bytes) -> int:
        self.writer.write(value)
        return len(value)

    def tell(self) -> int:
        return self.writer.size

    def flush(self) -> None:
        return None

    def seekable(self) -> bool:
        return False


def _fingerprint(actions: list[dict[str, object]]) -> str:
    return hashlib.sha256(json.dumps(actions, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _from_address(error: AddressRejected) -> OperationError:
    mapping = {
        "conflict": "conflict",
        "limit": "limit",
        "invalid": "invalid",
        "not_found": "not_found",
        "unavailable": "unavailable",
        "forbidden": "forbidden",
    }
    return OperationError(mapping.get(error.code, "forbidden"))


def _failure_item(action: dict[str, object], code: str, *, status: str = "failed") -> dict[str, object]:
    destination = action.get("destination")
    return {"status": status, "action": action.get("action", "unknown"), "destination": destination, "error": code}


def _is_upload_record(record: dict[str, object]) -> bool:
    actions = record.get("actions")
    return (
        isinstance(actions, list)
        and bool(actions)
        and isinstance(actions[0], dict)
        and actions[0].get("action") == "upload"
    )


def _public_item(item: dict[str, object], index: int, *, sub_index: int | None = None) -> dict[str, object]:
    status = item.get("status")
    if status in {"staging", "publishing"}:
        status = "uncommitted"
    result: dict[str, object] = {
        "index": index,
        "status": status,
        "action": item.get("action", "unknown"),
    }
    if sub_index is not None:
        result["subIndex"] = sub_index
    error = item.get("error")
    if error is not None:
        result["error"] = error
    elif item.get("status") in {"staging", "publishing"}:
        result["error"] = "in_progress"
    if isinstance(item.get("size"), int):
        result["size"] = item["size"]
    if isinstance(item.get("separatedLinks"), int):
        # HF-FILE-002: a move across filesystems reports separated links.
        result["separatedLinks"] = item["separatedLinks"]
    destination = item.get("destination")
    if isinstance(destination, dict) and error not in {"forbidden", "not_found", "unavailable"}:
        result["destination"] = destination
    return result


def _item_statuses(item: dict[str, object]) -> list[str]:
    children = item.get("children")
    if isinstance(children, list):
        return [
            status
            for child in children
            if isinstance(child, dict)
            for status in _item_statuses(child)
        ]
    status = item.get("status")
    return [status] if isinstance(status, str) else []


def _record_has_unfinished_publication(record: dict[str, object]) -> bool:
    items = record.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        targets = item.get("children") if isinstance(item.get("children"), list) else [item]
        if any(
            isinstance(target, dict) and target.get("status") in {"staging", "publishing"}
            for target in targets
        ):
            return True
    return False


def _replace_item(items: object, index: int, value: dict[str, object]) -> None:
    if not isinstance(items, list):
        raise StateError("operation items are invalid")
    while len(items) <= index:
        items.append({"status": "uncommitted", "action": "unknown", "destination": None, "error": "not_started"})
    items[index] = value


def _digest_destination(catalog: RootCatalog, address: object) -> str | None:
    if not isinstance(address, dict):
        return None
    try:
        with open_regular(catalog, address["rootId"], address["path"]) as (fd, _info):
            digest = hashlib.sha256()
            for chunk in _read_chunks(fd):
                digest.update(chunk)
            return digest.hexdigest()
    except (AddressRejected, KeyError, TypeError):
        return None


def _identity_destination(catalog: RootCatalog, address: object) -> list[int] | None:
    if not isinstance(address, dict):
        return None
    try:
        with open_regular(catalog, address["rootId"], address["path"]) as (_fd, info):
            return [info.st_dev, info.st_ino]
    except (AddressRejected, KeyError, TypeError):
        return None


def _read_record(path: Path, instance_id: str, token: OperationToken | None = None) -> dict[str, object]:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > OPERATION_RECORD_MAX_BYTES
        ):
            raise StateError("operation journal permissions are invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(64 * 1024, OPERATION_RECORD_MAX_BYTES + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > OPERATION_RECORD_MAX_BYTES:
                raise StateError("operation record exceeds its size limit")
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("operation journal is unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    _validate_record(payload, instance_id, token)
    assert isinstance(payload, dict)
    return payload


def _remove_operation_temporary(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StateError("operation temporary is invalid") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise StateError("operation temporary is invalid")
    path.unlink()
    _sync_directory(path.parent)


def _validate_record(value: object, instance_id: str, token: OperationToken | None = None) -> None:
    fields = {"version", "instanceId", "tokenId", "issuedAt", "expiresAt", "status", "fingerprint", "actions", "items", "finishedAt"}
    if not isinstance(value, dict) or set(value) != fields:
        raise StateError("operation journal is invalid")
    if value["version"] != 1 or value["instanceId"] != instance_id:
        raise StateError("operation journal is invalid")
    identifier = value["tokenId"]
    issued = value["issuedAt"]
    expires = value["expiresAt"]
    if (
        not isinstance(identifier, str)
        or len(identifier) != 32
        or any(char not in "0123456789abcdef" for char in identifier)
        or isinstance(issued, bool)
        or isinstance(expires, bool)
        or not isinstance(issued, int)
        or not isinstance(expires, int)
        or expires != issued + TOKEN_LIFETIME_SECONDS
        or value["status"] not in {"issued", "pending", "completed", "partial", "failed", "indeterminate"}
        or (value["fingerprint"] is not None and (not isinstance(value["fingerprint"], str) or len(value["fingerprint"]) != 64))
        or (value["actions"] is not None and not isinstance(value["actions"], list))
        or not isinstance(value["items"], list)
        or (value["finishedAt"] is not None and (isinstance(value["finishedAt"], bool) or not isinstance(value["finishedAt"], int)))
    ):
        raise StateError("operation journal is invalid")
    if token is not None and (identifier != token.identifier or issued != token.issued_at or expires != token.expires_at):
        raise StateError("operation journal is invalid")


def _text(value: object, maximum: int) -> bool:
    if not isinstance(value, str) or value == "" or len(value) > maximum or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _extension(value: object) -> bool:
    return _text(value, 32) and "/" not in value and "\\" not in value and "." not in value and "%" not in value


def _name(value: object) -> str:
    if not _text(value, 255) or "/" in value or "\\" in value or value in {".", ".."}:
        raise OperationError("invalid")
    return value


def _join(parent: str, name: str) -> str:
    return name if parent == "" else parent + "/" + name


def _base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unbase64(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("token encoding is invalid") from exc


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
