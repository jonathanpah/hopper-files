"""State migration between the root-based model and the single base (HF-NAV-012).

This module converts one instance's per-instance stores. It runs as the
service account (HF-INST-003); the administrative installer stops the
instances, calls it, replaces configuration and units, and switches the shared
release selector (see :mod:`hopper_files.lifecycle`).

Forward conversion maps each version 1 ``(rootId, path)`` to an absolute
canonical path using the version 1 configuration in effect before the
procedure. The return maps version 2 absolute paths back under that same saved
configuration. Both directions refuse to start while any store holds a
nonterminal journal, keep what they replace, report their counts, and are
idempotent.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import sys
import time
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path

from hopper_files.state import (
    LABELS,
    LEGACY_UI_STATE_VERSION,
    UI_STATE_VERSION,
    LegacyStateError,
    StateError,
    UiStateValidationError,
    atomic_write,
    encode_ui_state,
    read_signing_secret,
    require_instance_binding,
    ui_state_lock,
    ui_state_path,
    validate_legacy_ui_state,
    write_initialization_witness,
    _validate_ui_state,
)

MIGRATION_DIRECTORY = "migration"
RECORD_NAME = "record.json"
RECORD_VERSION = 1
FORMAT_ROOTS = 1
FORMAT_SINGLE_BASE = 2
BASE_ID = "fs"
_UUID_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OPERATION_NAME = re.compile(r"^op-[0-9a-f]{32}\.json$")
# Version 1 immutable protected defaults and relative rules of the root-based
# release. The return uses them to decide what the previous System view exposed.
_V1_ABSOLUTE_PROTECTED = (
    Path("/etc/shadow"),
    Path("/etc/gshadow"),
    Path("/etc/ssh"),
    Path("/run/credentials"),
    # The previous release also protected its own source, release, selector,
    # and dependencies. The whole shared installation is treated as protected,
    # which can only keep more records in the return report.
    Path("/opt/hopper-files"),
)
_V1_PROTECTED_COMPONENTS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".docker", ".git"})
_V1_PROTECTED_BASENAMES = frozenset({".netrc", ".git-credentials", ".env"})
_V1_STAGE_NAME = re.compile(r"^\.hopper-stage-[0-9a-f]{32}\.(?:tmp|dir)$")


class MigrationError(RuntimeError):
    """The migration or return cannot run without risking state."""


@dataclass(frozen=True)
class LegacyRoot:
    root_id: str
    path: Path


@dataclass(frozen=True)
class LegacyModel:
    """The version 1 configuration that addresses were recorded under."""

    documents: tuple[LegacyRoot, ...]
    system: LegacyRoot | None
    retired: frozenset[str]
    protected_additions: tuple[Path, ...]
    state_directory: Path
    config_path: Path

    def forward(self, root_id: str, path: str) -> tuple[str, str] | None:
        """Map ``(rootId, path)`` to ``(kind, absolute)``; None when unmapped."""
        for root in self.documents:
            if root.root_id == root_id:
                return "document", _join_absolute(root.path, path)
        if self.system is not None and self.system.root_id == root_id:
            return "system", _join_absolute(self.system.path, path)
        return None

    def backward(self, absolute: str) -> tuple[str, str] | None:
        """Map an absolute path to the ``(rootId, path)`` the previous release showed."""
        target = Path(absolute)
        for root in self.documents:
            relative = _relative_under(target, root.path)
            if relative is not None:
                return root.root_id, relative
        if self.system is None:
            return None
        relative = _relative_under(target, self.system.path)
        if relative is None:
            return None
        if self._protected(target):
            return None
        return self.system.root_id, relative

    def _protected(self, target: Path) -> bool:
        protected = (
            *_V1_ABSOLUTE_PROTECTED,
            *self.protected_additions,
            self.state_directory,
            self.config_path,
        )
        if any(_relative_under(target, item) is not None for item in protected):
            return True
        parts = target.parts[1:]
        for index, part in enumerate(parts):
            if part in _V1_PROTECTED_COMPONENTS or _V1_STAGE_NAME.fullmatch(part):
                return True
            if index > 0 and parts[index - 1] == ".config" and part == "gcloud":
                return True
        if parts:
            name = parts[-1]
            if name in _V1_PROTECTED_BASENAMES or name.startswith(".env."):
                return True
        return False


def load_legacy_model(config_path: Path) -> LegacyModel:
    """Read the version 1 configuration saved by the installer."""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"version 1 configuration is unreadable: {config_path}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or isinstance(payload.get("version"), bool):
        raise MigrationError(f"configuration is not version 1: {config_path}")
    roots = payload.get("roots")
    if not isinstance(roots, dict) or not isinstance(roots.get("items"), list):
        raise MigrationError("version 1 configuration has no document roots")
    documents: list[LegacyRoot] = []
    for item in roots["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("rootId"), str) or not isinstance(item.get("path"), str):
            raise MigrationError("version 1 document root is invalid")
        documents.append(LegacyRoot(item["rootId"], _canonical_directory(item["path"])))
    retired = frozenset(
        str(item.get("rootId"))
        for item in roots.get("retired", [])
        if isinstance(item, dict) and isinstance(item.get("rootId"), str)
    )
    system = None
    system_document = payload.get("system")
    if system_document is not None:
        if not isinstance(system_document, dict) or not isinstance(system_document.get("rootId"), str):
            raise MigrationError("version 1 System view is invalid")
        system = LegacyRoot(system_document["rootId"], _canonical_directory(str(system_document.get("base", ""))))
    additions = payload.get("protectedPaths", [])
    if not isinstance(additions, list) or any(not isinstance(item, str) for item in additions):
        raise MigrationError("version 1 protected paths are invalid")
    state = payload.get("stateDirectory")
    if not isinstance(state, str) or not state.startswith("/"):
        raise MigrationError("version 1 state directory is invalid")
    return LegacyModel(
        documents=tuple(documents),
        system=system,
        retired=retired,
        protected_additions=tuple(Path(os.path.abspath(item)) for item in additions),
        state_directory=Path(os.path.abspath(state)),
        config_path=Path(os.path.abspath(config_path)),
    )


def pending_journals(state_directory: Path) -> list[str]:
    """Name every nonterminal journal; the procedure refuses while any exists."""
    pending: list[str] = []
    operations = state_directory / "operations"
    if operations.is_dir():
        for path in sorted(operations.iterdir()):
            if _OPERATION_NAME.fullmatch(path.name) is None:
                if path.name.startswith(".op-"):
                    pending.append(f"operation temporary {path.name}")
                continue
            record = _read_json(path)
            if not isinstance(record, dict):
                pending.append(f"operation journal {path.name} (unreadable)")
                continue
            if record.get("status") in {"pending", "indeterminate"} or _unfinished(record):
                pending.append(f"operation journal {path.name} ({record.get('status')})")
    for label, directory in (
        ("trash journal", state_directory / "journals" / "trash"),
        ("ui-state journal", state_directory / "journals" / "ui-state"),
    ):
        if directory.is_dir():
            pending.extend(f"{label} {path.name}" for path in sorted(directory.iterdir()))
    staging = state_directory / "trash" / ".staging"
    if staging.is_dir():
        pending.extend(f"trash staging {path.name}" for path in sorted(staging.iterdir()))
    moves = state_directory / "attachments" / "moves"
    if moves.is_dir():
        pending.extend(f"attachment move journal {path.name}" for path in sorted(moves.iterdir()))
    buffers = state_directory / "attachments" / "buffers.json"
    if buffers.exists():
        envelope = _read_json(buffers)
        document = envelope.get("document") if isinstance(envelope, dict) else None
        moves_document = document.get("moves") if isinstance(document, dict) else None
        if not isinstance(moves_document, dict):
            pending.append("buffer registry (unreadable)")
        else:
            for move_id, command in sorted(moves_document.items()):
                if not isinstance(command, dict) or not isinstance(command.get("completedAt"), (int, float)):
                    pending.append(f"buffer move command {move_id}")
    return pending


def migrate_forward(config_v1: Path, state_directory: Path, instance_id: str, *, now: float | None = None) -> dict[str, object]:
    """Convert version 1 stores to version 2. Idempotent."""
    return _run("forward", config_v1, state_directory, instance_id, now)


def migrate_return(config_v1: Path, state_directory: Path, instance_id: str, *, now: float | None = None) -> dict[str, object]:
    """Convert version 2 stores back for the previous release. Idempotent."""
    return _run("return", config_v1, state_directory, instance_id, now)


def require_current_format(state_directory: Path) -> None:
    """Refuse to serve an instance whose UI state or trash is still version 1."""
    path = ui_state_path(state_directory)
    document = _read_json(path) if path.exists() else None
    if isinstance(document, dict) and document.get("version") == LEGACY_UI_STATE_VERSION:
        raise LegacyStateError("ui state is version 1; run the HF-NAV-012 migration")
    record = _read_record(state_directory)
    kept = set(record.get("legacyTrash", [])) if record else set()
    trash = state_directory / "trash"
    if not trash.is_dir():
        return
    for entry in trash.iterdir():
        if _UUID_NAME.fullmatch(entry.name) is None:
            continue
        meta = _read_json(entry / "meta.json")
        if isinstance(meta, dict) and meta.get("version") == 1 and entry.name not in kept:
            raise LegacyStateError(f"trash metadata {entry.name} is version 1; run the HF-NAV-012 migration")


def _run(direction: str, config_v1: Path, state_directory: Path, instance_id: str, now: float | None) -> dict[str, object]:
    state_directory = Path(os.path.abspath(state_directory))
    require_instance_binding(state_directory, instance_id)
    model = load_legacy_model(config_v1)
    if model.state_directory != state_directory:
        raise MigrationError("version 1 configuration names another state directory")
    pending = pending_journals(state_directory)
    if pending:
        raise MigrationError(
            "nonterminal journals must be recovered under the release that created them: " + "; ".join(pending)
        )
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    backup = _private_directory(state_directory / MIGRATION_DIRECTORY) / f"{direction}-{stamp}"
    counts: dict[str, int] = {}
    details: dict[str, list[object]] = {"merged": [], "orphaned": [], "unrepresentable": [], "legacyTrash": []}
    with ui_state_lock(state_directory):
        if direction == "forward":
            _forward_locked(model, state_directory, instance_id, backup, counts, details)
        else:
            _return_locked(model, state_directory, instance_id, backup, counts, details)
    report = {
        "direction": direction,
        "instanceId": instance_id,
        "at": stamp,
        "backup": str(backup) if backup.exists() else None,
        "counts": dict(sorted(counts.items())),
        **details,
    }
    record = _read_record(state_directory) or {"version": RECORD_VERSION, "format": FORMAT_ROOTS, "legacyTrash": [], "history": []}
    record["format"] = FORMAT_SINGLE_BASE if direction == "forward" else FORMAT_ROOTS
    record["legacyTrash"] = sorted(str(item) for item in details["legacyTrash"]) if direction == "forward" else []
    # Records the previous release cannot represent wait here for the next
    # forward migration, which restores them (HF-NAV-012).
    if direction == "return":
        kept = [item for item in details["unrepresentable"] if isinstance(item, dict) and item.get("kind") in {"item", "tab", "orphan"}]
        if kept or counts.get("ui_state_already_version_1") is None:
            record["returnKept"] = kept
    elif counts.get("ui_state_already_version_2") is None:
        record["returnKept"] = []
    history = record.get("history") if isinstance(record.get("history"), list) else []
    history.append({"direction": direction, "at": stamp, "counts": report["counts"]})
    record["history"] = history[-50:]
    atomic_write(state_directory / MIGRATION_DIRECTORY / RECORD_NAME, _dump(record))
    if backup.exists():
        atomic_write(backup / "report.json", _dump(report))
    return report


def _forward_locked(
    model: LegacyModel,
    state_directory: Path,
    instance_id: str,
    backup: Path,
    counts: dict[str, int],
    details: dict[str, list[object]],
) -> None:
    def map_address(root_id: object, path: object) -> str | None:
        if not isinstance(root_id, str) or not isinstance(path, str):
            return None
        mapped = model.forward(root_id, path)
        return None if mapped is None else mapped[1]

    _convert_trash_forward(model, state_directory, instance_id, backup, counts, details)
    _convert_operations(state_directory, instance_id, backup, counts, "forward", map_address)
    _convert_pending_images(state_directory, instance_id, backup, counts, "forward", map_address)
    _discard_derived(state_directory, counts)
    path = ui_state_path(state_directory)
    document = _read_json(path)
    if isinstance(document, dict) and document.get("version") == UI_STATE_VERSION:
        _validate_ui_state(document, require_state_revision=True)
        counts["ui_state_already_version_2"] = 1
        return
    try:
        legacy = validate_legacy_ui_state(document)
    except UiStateValidationError as exc:
        raise MigrationError(f"version 1 ui state is invalid: {exc}") from exc
    _backup_file(path, backup / "ui-state.v1.json")
    converted = _ui_state_forward(model, legacy, counts, details)
    _restore_return_kept(state_directory, converted, counts)
    _validate_ui_state(converted, require_state_revision=True)
    atomic_write(path, encode_ui_state(converted))
    write_initialization_witness(state_directory, UI_STATE_VERSION)


def _ui_state_forward(
    model: LegacyModel,
    legacy: dict[str, object],
    counts: dict[str, int],
    details: dict[str, list[object]],
) -> dict[str, object]:
    merged: dict[str, dict[str, object]] = {}
    origin: dict[str, str] = {}
    orphans: list[dict[str, object]] = []
    for item in legacy["items"]:
        assert isinstance(item, dict)
        mapped = model.forward(str(item["rootId"]), str(item["path"]))
        if mapped is None:
            orphans.append({"kind": "item", "record": copy.deepcopy(item)})
            details["orphaned"].append({"kind": "item", "rootId": item["rootId"], "path": item["path"]})
            counts["ui_items_orphaned"] = counts.get("ui_items_orphaned", 0) + 1
            continue
        kind, absolute = mapped
        converted = {
            "path": absolute,
            "labelIds": list(item["labelIds"]),
            "favorite": bool(item["favorite"]),
            "emoji": item["emoji"],
            "inode": item["inode"],
            "device": item["device"],
        }
        if absolute not in merged:
            merged[absolute] = converted
            origin[absolute] = kind
            counts["ui_items_converted"] = counts.get("ui_items_converted", 0) + 1
            continue
        current = merged[absolute]
        preferred, other = (current, converted) if origin[absolute] == "document" or kind != "document" else (converted, current)
        result = {
            "path": absolute,
            "labelIds": [label for label in LABELS if label in set(current["labelIds"]) | set(converted["labelIds"])],
            "favorite": bool(current["favorite"] or converted["favorite"]),
            "emoji": preferred["emoji"] if preferred["emoji"] is not None else other["emoji"],
            "inode": preferred["inode"],
            "device": preferred["device"],
        }
        merged[absolute] = result
        origin[absolute] = "document" if "document" in {origin[absolute], kind} else kind
        details["merged"].append({"path": absolute})
        counts["ui_items_merged"] = counts.get("ui_items_merged", 0) + 1
    tabs: list[dict[str, object]] = []
    for tab in legacy["tabs"]:
        assert isinstance(tab, dict)
        mapped = model.forward(str(tab["rootId"]), str(tab["path"]))
        if mapped is None:
            orphans.append({"kind": "tab", "record": copy.deepcopy(tab)})
            details["orphaned"].append({"kind": "tab", "rootId": tab["rootId"], "path": tab["path"]})
            counts["ui_tabs_orphaned"] = counts.get("ui_tabs_orphaned", 0) + 1
            continue
        tabs.append({"path": mapped[1], "mode": tab["mode"]})
        counts["ui_tabs_converted"] = counts.get("ui_tabs_converted", 0) + 1
    return {
        "version": UI_STATE_VERSION,
        "stateRevision": int(legacy["stateRevision"]) + 1,
        "labels": copy.deepcopy(legacy["labels"]),
        "items": list(merged.values()),
        "tabs": tabs,
        "orphans": orphans,
        "preferences": copy.deepcopy(legacy["preferences"]),
    }


def _restore_return_kept(state_directory: Path, document: dict[str, object], counts: dict[str, int]) -> None:
    """Bring back records a previous return kept in its report."""
    record = _read_record(state_directory) or {}
    kept = record.get("returnKept")
    if not isinstance(kept, list):
        return
    items = document["items"]
    tabs = document["tabs"]
    orphans = document["orphans"]
    present = {str(item["path"]) for item in items}
    for entry in kept:
        if not isinstance(entry, dict) or not isinstance(entry.get("record"), dict):
            continue
        value = copy.deepcopy(entry["record"])
        if entry.get("kind") == "item":
            if value.get("path") in present:
                counts["ui_items_return_duplicates"] = counts.get("ui_items_return_duplicates", 0) + 1
                continue
            present.add(str(value.get("path")))
            items.append(value)
            counts["ui_items_restored_from_return"] = counts.get("ui_items_restored_from_return", 0) + 1
        elif entry.get("kind") == "tab":
            tabs.append(value)
            counts["ui_tabs_restored_from_return"] = counts.get("ui_tabs_restored_from_return", 0) + 1
        elif entry.get("kind") == "orphan":
            orphans.append({"kind": "item", "record": value})
            counts["ui_orphans_restored_from_return"] = counts.get("ui_orphans_restored_from_return", 0) + 1


def _return_locked(
    model: LegacyModel,
    state_directory: Path,
    instance_id: str,
    backup: Path,
    counts: dict[str, int],
    details: dict[str, list[object]],
) -> None:
    def map_address(root_id: object, path: object) -> tuple[str, str] | None:
        if root_id != BASE_ID or not isinstance(path, str):
            return None
        return model.backward("/" + path)

    _convert_trash_return(model, state_directory, instance_id, backup, counts, details)
    _convert_operations(state_directory, instance_id, backup, counts, "return", map_address)
    _convert_pending_images(state_directory, instance_id, backup, counts, "return", map_address)
    _discard_derived(state_directory, counts)
    path = ui_state_path(state_directory)
    document = _read_json(path)
    if isinstance(document, dict) and document.get("version") == LEGACY_UI_STATE_VERSION:
        validate_legacy_ui_state(document)
        counts["ui_state_already_version_1"] = 1
        return
    current = _validate_ui_state(document, require_state_revision=True)
    _backup_file(path, backup / "ui-state.v2.json")
    items: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in current["items"]:
        mapped = model.backward(str(item["path"]))
        if mapped is None:
            details["unrepresentable"].append({"kind": "item", "record": item})
            counts["ui_items_unrepresentable"] = counts.get("ui_items_unrepresentable", 0) + 1
            continue
        seen.add(mapped)
        items.append({
            "rootId": mapped[0],
            "path": mapped[1],
            "labelIds": list(item["labelIds"]),
            "favorite": item["favorite"],
            "emoji": item["emoji"],
            "inode": item["inode"],
            "device": item["device"],
        })
        counts["ui_items_converted"] = counts.get("ui_items_converted", 0) + 1
    tabs: list[dict[str, object]] = []
    for tab in current["tabs"]:
        mapped = model.backward(str(tab["path"]))
        if mapped is None:
            details["unrepresentable"].append({"kind": "tab", "record": tab})
            counts["ui_tabs_unrepresentable"] = counts.get("ui_tabs_unrepresentable", 0) + 1
            continue
        tabs.append({"rootId": mapped[0], "path": mapped[1], "mode": tab["mode"]})
        counts["ui_tabs_converted"] = counts.get("ui_tabs_converted", 0) + 1
    for orphan in current["orphans"]:
        record = copy.deepcopy(orphan["record"])
        if orphan["kind"] == "tab":
            tabs.append(record)
            counts["ui_orphans_restored"] = counts.get("ui_orphans_restored", 0) + 1
            continue
        identity = (str(record["rootId"]), str(record["path"]))
        if identity in seen:
            details["unrepresentable"].append({"kind": "orphan", "record": record})
            counts["ui_orphans_unrepresentable"] = counts.get("ui_orphans_unrepresentable", 0) + 1
            continue
        seen.add(identity)
        items.append(record)
        counts["ui_orphans_restored"] = counts.get("ui_orphans_restored", 0) + 1
    legacy = {
        "version": LEGACY_UI_STATE_VERSION,
        "stateRevision": int(current["stateRevision"]) + 1,
        "labels": copy.deepcopy(current["labels"]),
        "items": items,
        "tabs": tabs,
        "preferences": copy.deepcopy(current["preferences"]),
    }
    validate_legacy_ui_state(legacy)
    atomic_write(path, encode_ui_state(legacy))
    write_initialization_witness(state_directory, LEGACY_UI_STATE_VERSION)


def _convert_trash_forward(
    model: LegacyModel,
    state_directory: Path,
    instance_id: str,
    backup: Path,
    counts: dict[str, int],
    details: dict[str, list[object]],
) -> None:
    from hopper_files.trash import TrashStore, _seal_digest_at, parse_legacy_meta

    store = TrashStore(state_directory, instance_id)
    for entry in _trash_entries(state_directory):
        raw = _read_bytes(entry / "meta.json")
        if raw is None:
            counts["trash_quarantined_kept"] = counts.get("trash_quarantined_kept", 0) + 1
            continue
        payload = _loads(raw)
        if isinstance(payload, dict) and payload.get("version") == 2:
            counts["trash_already_version_2"] = counts.get("trash_already_version_2", 0) + 1
            continue
        meta = parse_legacy_meta(raw, entry.name)
        if meta is None or not _seal_valid(store, entry, raw):
            # Invalid or tampered metadata was already quarantined; keep it.
            counts["trash_quarantined_kept"] = counts.get("trash_quarantined_kept", 0) + 1
            details["legacyTrash"].append(entry.name)
            continue
        mapped = model.forward(str(meta["sourceRootId"]), str(meta["sourcePath"]))
        if mapped is None:
            counts["trash_orphaned"] = counts.get("trash_orphaned", 0) + 1
            details["legacyTrash"].append(entry.name)
            details["orphaned"].append({"kind": "trash", "id": entry.name, "rootId": meta["sourceRootId"], "path": meta["sourcePath"]})
            continue
        converted = {
            "version": 2,
            "id": meta["id"],
            "sourcePath": mapped[1],
            "deletedAt": meta["deletedAt"],
            "kind": meta["kind"],
            "size": meta["size"],
            "reason": meta["reason"],
            "uiMetadata": meta["uiMetadata"],
        }
        _backup_file(entry / "meta.json", backup / "trash" / entry.name / "meta.json")
        _backup_file(entry / "meta.seal", backup / "trash" / entry.name / "meta.seal")
        _reseal(store, entry, converted, _seal_digest_at)
        counts["trash_converted"] = counts.get("trash_converted", 0) + 1


def _convert_trash_return(
    model: LegacyModel,
    state_directory: Path,
    instance_id: str,
    backup: Path,
    counts: dict[str, int],
    details: dict[str, list[object]],
) -> None:
    from hopper_files.trash import TrashStore, _parse_meta, _seal_digest_at

    store = TrashStore(state_directory, instance_id)
    for entry in _trash_entries(state_directory):
        raw = _read_bytes(entry / "meta.json")
        payload = _loads(raw) if raw is not None else None
        if not isinstance(payload, dict) or payload.get("version") != 2:
            counts["trash_left_unchanged"] = counts.get("trash_left_unchanged", 0) + 1
            continue
        meta = _parse_meta(raw, entry.name)
        if meta is None or not _seal_valid(store, entry, raw):
            counts["trash_quarantined_kept"] = counts.get("trash_quarantined_kept", 0) + 1
            continue
        mapped = model.backward("/" + str(meta["sourcePath"]))
        if mapped is None:
            # The previous release quarantines metadata it cannot read; the
            # entry stays in trash and is neither purged nor restored.
            counts["trash_unrepresentable"] = counts.get("trash_unrepresentable", 0) + 1
            details["unrepresentable"].append({"kind": "trash", "id": entry.name, "sourcePath": payload["sourcePath"]})
            continue
        converted = {
            "version": 1,
            "id": meta["id"],
            "sourceRootId": mapped[0],
            "sourcePath": mapped[1],
            "deletedAt": meta["deletedAt"],
            "kind": meta["kind"],
            "size": meta["size"],
            "reason": meta["reason"],
            "uiMetadata": meta["uiMetadata"],
        }
        _backup_file(entry / "meta.json", backup / "trash" / entry.name / "meta.json")
        _backup_file(entry / "meta.seal", backup / "trash" / entry.name / "meta.seal")
        _reseal(store, entry, converted, _seal_digest_at)
        counts["trash_converted"] = counts.get("trash_converted", 0) + 1


def _reseal(store: object, entry: Path, meta: dict[str, object], seal_digest_at: object) -> None:
    from hopper_files.trash import _dump_meta, _dump_seal

    raw = _dump_meta(meta)
    atomic_write(entry / "meta.json", raw)
    digest = seal_digest_at(entry)
    atomic_write(entry / "meta.seal", _dump_seal(store._mac(raw, digest)))


def _seal_valid(store: object, entry: Path, raw: bytes) -> bool:
    from hopper_files.trash import _seal_digest_at

    seal = _read_bytes(entry / "meta.seal")
    if seal is None:
        return False
    try:
        digest = _seal_digest_at(entry)
    except Exception:
        return False
    return store._seal_matches(raw, digest, seal)


def _convert_operations(
    state_directory: Path,
    instance_id: str,
    backup: Path,
    counts: dict[str, int],
    direction: str,
    map_address: object,
) -> None:
    """Convert addresses inside terminal operation results.

    A result that cannot be converted keeps its record unchanged: its token
    stays terminal and can never start a new operation (HF-API-005).
    """
    directory = state_directory / "operations"
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if _OPERATION_NAME.fullmatch(path.name) is None:
            continue
        record = _read_json(path)
        if not isinstance(record, dict) or record.get("instanceId") != instance_id:
            raise MigrationError(f"operation journal is unreadable: {path.name}")
        if record.get("status") == "issued":
            continue
        converted, unmapped = _convert_addresses(record, direction, map_address)
        if unmapped:
            counts["operation_results_kept_terminal"] = counts.get("operation_results_kept_terminal", 0) + 1
            continue
        if converted == record:
            continue
        _backup_file(path, backup / "operations" / path.name)
        _write_private(path, _dump(converted))
        counts["operation_results_converted"] = counts.get("operation_results_converted", 0) + 1


def _convert_pending_images(
    state_directory: Path,
    instance_id: str,
    backup: Path,
    counts: dict[str, int],
    direction: str,
    map_address: object,
) -> None:
    path = state_directory / "attachments" / "pending.json"
    if not path.exists():
        return
    envelope = _read_json(path)
    document = envelope.get("document") if isinstance(envelope, dict) else None
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise MigrationError("image pending registry is unreadable")
    kept: list[object] = []
    for item in document["items"]:
        converted, unmapped = _convert_addresses(item, direction, map_address)
        if unmapped:
            counts["pending_images_unmapped_kept"] = counts.get("pending_images_unmapped_kept", 0) + 1
            kept.append(item)
            continue
        if converted != item:
            counts["pending_images_converted"] = counts.get("pending_images_converted", 0) + 1
        kept.append(converted)
    updated = {**document, "items": kept}
    if updated == document:
        return
    _backup_file(path, backup / "attachments" / "pending.json")
    canonical = json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    mac = hmac.new(read_signing_secret(state_directory), canonical, hashlib.sha256).hexdigest()
    atomic_write(path, json.dumps({"document": updated, "mac": mac}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _convert_addresses(value: object, direction: str, map_address: object) -> tuple[object, bool]:
    """Convert every ``(…RootId, …Path)`` pair in a JSON value."""
    unmapped = False

    def visit(node: object) -> object:
        nonlocal unmapped
        if isinstance(node, list):
            return [visit(child) for child in node]
        if not isinstance(node, dict):
            return node
        result = {key: visit(child) for key, child in node.items()}
        for key in list(node):
            if key != "rootId" and not key.endswith("RootId"):
                continue
            path_key = "path" if key == "rootId" else key[: -len("RootId")] + "Path"
            if path_key not in node or not isinstance(node[key], str) or not isinstance(node[path_key], str):
                continue
            if direction == "forward":
                if node[key] == BASE_ID:
                    continue
                absolute = map_address(node[key], node[path_key])
                if absolute is None:
                    unmapped = True
                    continue
                result[key], result[path_key] = BASE_ID, absolute[1:]
            else:
                if node[key] != BASE_ID:
                    continue
                mapped = map_address(node[key], node[path_key])
                if mapped is None:
                    unmapped = True
                    continue
                result[key], result[path_key] = mapped
        return result

    return visit(value), unmapped


def _discard_derived(state_directory: Path, counts: dict[str, int]) -> None:
    """Discard search cursors, tag-index data, and buffer registrations explicitly.

    They are rebuilt on demand. Buffer registrations belong to connections
    that end when the installer stops the instance.
    """
    cursors = state_directory / "cache" / "search-cursors"
    if cursors.is_dir():
        removed = 0
        for path in cursors.iterdir():
            if path.name == ".lock":
                continue
            path.unlink()
            removed += 1
        counts["search_cursors_discarded"] = counts.get("search_cursors_discarded", 0) + removed
    indexes = state_directory / "indexes"
    if indexes.is_dir():
        removed = 0
        for path in indexes.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        counts["tag_index_entries_discarded"] = counts.get("tag_index_entries_discarded", 0) + removed
    buffers = state_directory / "attachments" / "buffers.json"
    if buffers.exists():
        envelope = _read_json(buffers)
        document = envelope.get("document") if isinstance(envelope, dict) else None
        sessions = document.get("sessions") if isinstance(document, dict) else None
        registered = sum(
            len(session.get("buffers", []))
            for session in (sessions or {}).values()
            if isinstance(session, dict) and isinstance(session.get("buffers"), list)
        )
        buffers.unlink()
        counts["buffer_registrations_expired"] = counts.get("buffer_registrations_expired", 0) + registered


def _trash_entries(state_directory: Path) -> list[Path]:
    trash = state_directory / "trash"
    if not trash.is_dir():
        return []
    return sorted(
        entry
        for entry in trash.iterdir()
        if _UUID_NAME.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()
    )


def _unfinished(record: dict[str, object]) -> bool:
    items = record.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        targets = item.get("children") if isinstance(item.get("children"), list) else [item]
        if any(isinstance(target, dict) and target.get("status") in {"staging", "publishing"} for target in targets):
            return True
    return False


def _join_absolute(base: Path, relative: str) -> str:
    if relative == "":
        return str(base)
    return str(base).rstrip("/") + "/" + relative


def _relative_under(target: Path, base: Path) -> str | None:
    try:
        relative = target.relative_to(base)
    except ValueError:
        return None
    text = relative.as_posix()
    return "" if text == "." else text


def _canonical_directory(value: str) -> Path:
    if not value.startswith("/") or "\x00" in value:
        raise MigrationError("version 1 root path is invalid")
    return Path(os.path.abspath(value))


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise MigrationError(f"migration directory is unsafe: {path}")
    os.chmod(path, 0o700)
    return path


def _backup_file(source: Path, destination: Path) -> None:
    """Keep the replaced bytes; an existing backup of the same run is kept."""
    if destination.exists():
        return
    parents = []
    current = destination.parent
    while not current.exists():
        parents.append(current)
        current = current.parent
    for directory in reversed(parents):
        directory.mkdir(mode=0o700)
    data = _read_bytes(source)
    if data is None:
        raise MigrationError(f"store to back up is unreadable: {source}")
    _write_private(destination, data)


def _write_private(path: Path, data: bytes) -> None:
    atomic_write(path, data)


def _read_bytes(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
    finally:
        os.close(fd)


def _loads(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None


def _read_json(path: Path) -> object:
    raw = _read_bytes(path)
    return None if raw is None else _loads(raw)


def _read_record(state_directory: Path) -> dict[str, object] | None:
    record = _read_json(state_directory / MIGRATION_DIRECTORY / RECORD_NAME)
    return record if isinstance(record, dict) else None


def _dump(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    """Service-account entry point used by the administrative installer."""
    parser = ArgumentParser(prog="hopper-files-migrate-state")
    parser.add_argument("direction", choices=("check", "forward", "return"))
    parser.add_argument("--config-v1", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.direction == "check":
            load_legacy_model(args.config_v1)
            require_instance_binding(args.state, args.instance_id)
            pending = pending_journals(args.state)
            print(json.dumps({"pending": pending}, ensure_ascii=False))
            return 1 if pending else 0
        operation = migrate_forward if args.direction == "forward" else migrate_return
        report = operation(args.config_v1, args.state, args.instance_id)
    except (MigrationError, StateError, OSError) as exc:
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
