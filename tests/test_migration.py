"""Migration from the root-based model and return (HF-NAV-012, HF-ACC-027).

Every state here is synthetic: version 1 documents are written in the exact
format of the previous release, and trash metadata is sealed with the
instance's own key.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

import hopper_files.trash as trash_module
from hopper_files.config import load_config
from hopper_files.migration import (
    MigrationError,
    migrate_forward,
    migrate_return,
    pending_journals,
    require_current_format,
)
from hopper_files.state import (
    LegacyStateError,
    encode_ui_state,
    initialize_state,
    load_ui_state,
    ui_state_path,
    write_initialization_witness,
)
from hopper_files.trash import TrashStore
from hopper_files.roots import build_catalog

from conftest import CLOCK_START, DOCUMENTS, PASSWORD, ROOT, ManualClock, boot, write_config

TOKEN = "0123456789abcdef0123456789abcdef"


def _legacy_item(root_id: str, path: str, *, labels: list[str] | None = None, favorite: bool = False,
                 emoji: str | None = None, inode: int | None = None) -> dict[str, object]:
    return {"rootId": root_id, "path": path, "labelIds": labels or [], "favorite": favorite,
            "emoji": emoji, "inode": inode, "device": None if inode is None else 7}


class Legacy:
    """One synthetic instance still in the version 1 format."""

    def __init__(self, tmp_path: Path, instance_id: str = "legacy", prefix: str = "") -> None:
        base = DOCUMENTS / prefix if prefix else DOCUMENTS
        self.a = base / "a"
        self.b = base / "b"
        self.secret = base / "secret"
        for path in (self.a, self.b, self.secret):
            path.mkdir(parents=True)
        self.state = tmp_path / instance_id / "state"
        config = write_config(tmp_path / instance_id, instance_id=instance_id, state=self.state)
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["version"] = 1
        payload["roots"] = {
            "generation": 2,
            "items": [
                {"rootId": "a", "label": "A", "path": str(self.a), "enabled": True},
                {"rootId": "b", "label": "B", "path": str(self.b), "enabled": False},
            ],
            "retired": [{"rootId": "old", "path": str(tmp_path / "old")}],
        }
        payload["system"] = {"rootId": "system", "label": "Sistema", "base": str(DOCUMENTS), "writable": [], "safeTrash": []}
        payload["protectedPaths"] = [str(self.secret)]
        self.v2_config = config.with_name("v2-" + config.name)
        self.v2_config.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
        config.write_text(json.dumps(payload), encoding="utf-8")
        self.config = config
        self.instance_id = instance_id
        initialize_state(self.state, instance_id)
        self.store = TrashStore(self.state, instance_id)

    def write_ui_state(self, items: list[dict[str, object]], tabs: list[dict[str, object]], revision: int = 4) -> None:
        document = {
            "version": 1,
            "stateRevision": revision,
            "labels": load_ui_state_labels(),
            "items": items,
            "tabs": tabs,
            "preferences": {"theme": "dark", "ordering": "name", "density": "comfortable", "ignoredTags": ["rascunho"]},
        }
        ui_state_path(self.state).write_bytes(encode_ui_state(document))
        write_initialization_witness(self.state, 1)

    def trash(self, file: Path, root_id: str, relative: str, marks: list[dict[str, object]] | None = None) -> str:
        catalog = build_catalog(load_config(self.v2_config))
        deleted = self.store.delete(catalog, ROOT, str(file)[1:], CLOCK_START)
        entry = self.state / "trash" / str(deleted["id"])
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        legacy = {**meta, "version": 1, "sourceRootId": root_id, "sourcePath": relative, "uiMetadata": marks}
        self.seal(entry, legacy)
        return str(deleted["id"])

    def seal(self, entry: Path, meta: dict[str, object]) -> None:
        raw = trash_module._dump_meta(meta)
        (entry / "meta.json").write_bytes(raw)
        (entry / "meta.seal").write_bytes(trash_module._dump_seal(self.store._mac(raw, trash_module._seal_digest_at(entry))))

    def operation(self, status: str, destination: dict[str, str], token: str = TOKEN) -> Path:
        record = {
            "version": 1, "instanceId": self.instance_id, "tokenId": token,
            "issuedAt": 1_700_000_000, "expiresAt": 1_700_000_000 + 7 * 24 * 60 * 60,
            "status": status, "fingerprint": "f" * 64,
            "actions": [{"action": "create", "kind": "file", "nameMode": "exact", "destination": destination}],
            "items": [{"status": "committed" if status == "completed" else "staging", "action": "create", "destination": destination}],
            "finishedAt": 1_700_000_100 if status == "completed" else None,
        }
        path = self.state / "operations" / f"op-{token}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, json.dumps(record).encode("utf-8"))
        os.close(fd)
        return path


def load_ui_state_labels() -> dict[str, object]:
    from hopper_files.state import LABELS

    return copy.deepcopy(LABELS)


def _populated(tmp_path: Path) -> tuple[Legacy, dict[str, str]]:
    legacy = Legacy(tmp_path)
    for name in ("from-a.txt", "from-system.txt", "from-old.txt"):
        (DOCUMENTS / name).write_bytes(name.encode())
    ids = {
        "a": legacy.trash(DOCUMENTS / "from-a.txt", "a", "restored-a.txt", [{"relativePath": "", "labelIds": ["azul"], "favorite": True, "emoji": None}]),
        "system": legacy.trash(DOCUMENTS / "from-system.txt", "system", "from-system.txt"),
        "old": legacy.trash(DOCUMENTS / "from-old.txt", "old", "from-old.txt"),
    }
    legacy.write_ui_state(
        items=[
            _legacy_item("a", "note.md", favorite=True, labels=["azul"], inode=11),
            _legacy_item("system", "a/note.md", labels=["verde", "vermelho"], emoji="📌", inode=99),
            _legacy_item("b", "", emoji="📁"),
            _legacy_item("old", "gone.md", favorite=True),
        ],
        tabs=[
            {"rootId": "a", "path": "note.md", "mode": "edit"},
            {"rootId": "a", "path": "note.md", "mode": "preview"},
            {"rootId": "old", "path": "t.md", "mode": "browse"},
            {"rootId": "system", "path": "b/x.md", "mode": "browse"},
        ],
    )
    legacy.operation("completed", {"rootId": "a", "path": "made.txt"})
    legacy.operation("completed", {"rootId": "old", "path": "made.txt"}, token="1" * 32)
    cursors = legacy.state / "cache" / "search-cursors"
    cursors.mkdir(mode=0o700, exist_ok=True)
    (cursors / ("a" * 32 + ".json")).write_text("{}", encoding="utf-8")
    return legacy, ids


def test_version_1_state_is_refused_until_migrated(tmp_path) -> None:
    legacy, _ids = _populated(tmp_path)
    before = ui_state_path(legacy.state).read_bytes()
    with pytest.raises(LegacyStateError):
        require_current_format(legacy.state)
    with pytest.raises(LegacyStateError):
        load_ui_state(legacy.state)
    assert ui_state_path(legacy.state).read_bytes() == before


def test_forward_migration_converts_merges_orphans_and_is_idempotent(tmp_path) -> None:
    legacy, ids = _populated(tmp_path)
    report = migrate_forward(legacy.config, legacy.state, legacy.instance_id)
    document = load_ui_state(legacy.state)
    items = {item["path"]: item for item in document["items"]}
    counts = report["counts"]

    note = str(legacy.a / "note.md")
    assert document["version"] == 2 and document["stateRevision"] == 5
    assert items[note] == {"path": note, "labelIds": ["vermelho", "verde", "azul"], "favorite": True,
                           "emoji": "📌", "inode": 11, "device": 7}
    assert items[str(legacy.b)]["emoji"] == "📁"
    assert report["merged"] == [{"path": note}]
    assert [tab["path"] for tab in document["tabs"]] == [note, note, str(legacy.b / "x.md")]
    assert {orphan["kind"] for orphan in document["orphans"]} == {"item", "tab"}
    assert document["preferences"]["ignoredTags"] == ["rascunho"]
    assert counts["ui_items_converted"] == 2 and counts["ui_items_merged"] == 1
    assert counts["ui_items_orphaned"] == 1 and counts["ui_tabs_orphaned"] == 1
    assert counts["trash_converted"] == 2 and counts["trash_orphaned"] == 1
    assert counts["operation_results_converted"] == 1 and counts["operation_results_kept_terminal"] == 1
    assert counts["search_cursors_discarded"] == 1
    assert Path(report["backup"], "ui-state.v1.json").is_file()

    listed = {entry["id"]: entry for entry in legacy.store.list_entries(build_catalog(load_config(legacy.v2_config)), CLOCK_START)}
    assert listed[ids["a"]]["sourcePath"] == str(legacy.a / "restored-a.txt")[1:]
    assert listed[ids["system"]]["sourcePath"] == str(DOCUMENTS / "from-system.txt")[1:]
    assert listed[ids["old"]] == {"id": ids["old"], "quarantined": True, "recovery": "ready", "legacy": True}
    operation = json.loads((legacy.state / "operations" / f"op-{TOKEN}.json").read_text(encoding="utf-8"))
    assert operation["items"][0]["destination"] == {"rootId": ROOT, "path": str(legacy.a / "made.txt")[1:]}
    require_current_format(legacy.state)

    snapshot = {path: path.read_bytes() for path in legacy.state.rglob("*") if path.is_file() and "migration" not in path.parts}
    again = migrate_forward(legacy.config, legacy.state, legacy.instance_id)
    assert again["counts"].get("ui_state_already_version_2") == 1
    assert "trash_converted" not in again["counts"]
    after = {path: path.read_bytes() for path in legacy.state.rglob("*") if path.is_file() and "migration" not in path.parts}
    assert after == snapshot


def test_migrated_instance_serves_marks_and_restores_converted_trash(tmp_path) -> None:
    legacy, ids = _populated(tmp_path)
    migrate_forward(legacy.config, legacy.state, legacy.instance_id)
    legacy.config.write_text(legacy.v2_config.read_text(encoding="utf-8"), encoding="utf-8")
    with boot(tmp_path / "legacy", ManualClock(CLOCK_START), password=PASSWORD, instance_id="legacy", state=legacy.state) as world:
        signed = world.post_login(PASSWORD)
        headers = {"origin": world.config.origin, "x-csrf-token": signed.json()["csrfToken"]}
        state = world.client.get("/api/state").json()
        restored = world.client.post("/api/trash", headers=headers, json={"action": "restore", "id": ids["a"]})

    assert str(legacy.a / "note.md") in {item["path"] for item in state["items"]}
    assert restored.status_code == 200, restored.text
    assert (legacy.a / "restored-a.txt").read_bytes() == b"from-a.txt"


def test_return_maps_roots_first_keeps_unrepresentable_records_and_migrates_again(tmp_path) -> None:
    legacy, ids = _populated(tmp_path)
    migrate_forward(legacy.config, legacy.state, legacy.instance_id)
    outside = tmp_path / "outside-everything.md"
    outside.write_text("outside", encoding="utf-8")
    (DOCUMENTS / "outside-root.txt").write_bytes(b"free")
    new_trash = legacy.store.delete(build_catalog(load_config(legacy.v2_config)), ROOT, str(DOCUMENTS / "outside-root.txt")[1:], CLOCK_START)
    (tmp_path / "far.txt").write_bytes(b"far")
    far_trash = legacy.store.delete(build_catalog(load_config(legacy.v2_config)), ROOT, str(tmp_path / "far.txt")[1:], CLOCK_START)

    from hopper_files.state import mutate_ui_state

    def add(document: dict[str, object]) -> None:
        document["items"].extend([
            {"path": str(legacy.a / "new.md"), "labelIds": ["roxo"], "favorite": False, "emoji": None, "inode": None, "device": None},
            {"path": str(DOCUMENTS / "free.md"), "labelIds": [], "favorite": True, "emoji": None, "inode": None, "device": None},
            {"path": str(DOCUMENTS / ".ssh" / "key"), "labelIds": ["cinza"], "favorite": False, "emoji": None, "inode": None, "device": None},
            {"path": str(legacy.secret / "inside"), "labelIds": ["cinza"], "favorite": False, "emoji": None, "inode": None, "device": None},
            {"path": str(outside), "labelIds": [], "favorite": True, "emoji": None, "inode": None, "device": None},
        ])
        document["tabs"].append({"path": str(outside), "mode": "edit"})

    mutate_ui_state(legacy.state, add)
    before_return = load_ui_state(legacy.state)
    report = migrate_return(legacy.config, legacy.state, legacy.instance_id)
    returned = json.loads(ui_state_path(legacy.state).read_text(encoding="utf-8"))
    by_identity = {(item["rootId"], item["path"]): item for item in returned["items"]}

    assert returned["version"] == 1
    assert ("a", "new.md") in by_identity  # root before the overlapping System view
    assert ("system", "free.md") in by_identity
    assert ("old", "gone.md") in by_identity  # orphan restored as it was
    unrepresentable = {item["record"]["path"] for item in report["unrepresentable"] if item["kind"] in {"item", "tab"}}
    assert unrepresentable == {str(DOCUMENTS / ".ssh" / "key"), str(legacy.secret / "inside"), str(outside)}
    assert Path(report["backup"], "ui-state.v2.json").is_file()
    trash_meta = {
        entry.name: json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        for entry in (legacy.state / "trash").iterdir() if (entry / "meta.json").is_file()
    }
    assert trash_meta[ids["a"]]["sourceRootId"] == "a" and trash_meta[ids["a"]]["version"] == 1
    assert trash_meta[str(new_trash["id"])]["sourceRootId"] == "system"
    assert trash_meta[str(far_trash["id"])]["version"] == 2  # quarantined by the previous release
    raw = (legacy.state / "trash" / ids["a"] / "meta.json").read_bytes()
    assert trash_module.parse_legacy_meta(raw, ids["a"]) is not None
    assert legacy.store._seal_matches(raw, trash_module._seal_digest_at(legacy.state / "trash" / ids["a"]),
                                      (legacy.state / "trash" / ids["a"] / "meta.seal").read_bytes())
    operation = json.loads((legacy.state / "operations" / f"op-{TOKEN}.json").read_text(encoding="utf-8"))
    assert operation["items"][0]["destination"] == {"rootId": "a", "path": "made.txt"}

    again = migrate_return(legacy.config, legacy.state, legacy.instance_id)
    assert again["counts"].get("ui_state_already_version_1") == 1

    migrate_forward(legacy.config, legacy.state, legacy.instance_id)
    after = load_ui_state(legacy.state)

    def marks(document: dict[str, object]) -> list[tuple[object, ...]]:
        return sorted((item["path"], tuple(item["labelIds"]), item["favorite"], item["emoji"]) for item in document["items"])

    assert marks(after) == marks(before_return)
    assert sorted(tab["path"] for tab in after["tabs"]) == sorted(tab["path"] for tab in before_return["tabs"])
    assert after["orphans"] == before_return["orphans"]


@pytest.mark.parametrize("kind", ["operation", "trash", "attachment_move", "buffer_move", "ui_state"])
def test_pending_journal_blocks_migration_and_return_without_change(tmp_path, kind) -> None:
    legacy, _ids = _populated(tmp_path)
    if kind == "operation":
        legacy.operation("pending", {"rootId": "a", "path": "busy.txt"}, token="2" * 32)
    elif kind == "trash":
        (legacy.state / "journals" / "trash" / "00000000-0000-4000-8000-000000000000.json").write_text("{}", encoding="utf-8")
    elif kind == "attachment_move":
        (legacy.state / "attachments" / "moves" / ("c" * 32)).mkdir(parents=True)
    elif kind == "buffer_move":
        (legacy.state / "attachments" / "buffers.json").write_text(json.dumps({"document": {"moves": {"d" * 32: {"documents": []}}}, "mac": ""}), encoding="utf-8")
    else:
        (legacy.state / "journals" / "ui-state" / ("ui-state-" + "e" * 32 + ".json")).write_text("{}", encoding="utf-8")
    snapshot = {path: path.read_bytes() for path in legacy.state.rglob("*") if path.is_file()}
    assert pending_journals(legacy.state)
    with pytest.raises(MigrationError, match="nonterminal journals"):
        migrate_forward(legacy.config, legacy.state, legacy.instance_id)
    with pytest.raises(MigrationError, match="nonterminal journals"):
        migrate_return(legacy.config, legacy.state, legacy.instance_id)
    assert {path: path.read_bytes() for path in legacy.state.rglob("*") if path.is_file()} == snapshot


def test_service_entry_point_reports_pending_journals_as_json(tmp_path) -> None:
    legacy, _ids = _populated(tmp_path)
    legacy.operation("indeterminate", {"rootId": "a", "path": "busy.txt"}, token="3" * 32)
    completed = subprocess.run(
        ["python3", "-m", "hopper_files.migration", "check", "--config-v1", str(legacy.config),
         "--state", str(legacy.state), "--instance-id", legacy.instance_id],
        capture_output=True, text=True, env={**os.environ},
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["pending"] == [f"operation journal op-{'3' * 32}.json (indeterminate)"]


def test_installer_migrates_every_instance_before_switching_and_returns_on_failure(tmp_path, monkeypatch) -> None:
    """HF-NAV-012: stop all, convert all, switch; one failure returns the converted ones."""
    from hopper_files import lifecycle
    from hopper_files.lifecycle import LifecycleError, migrate_release, return_release
    from test_lifecycle import FakeRunner, _artifact, _layout, _managed_instance

    monkeypatch.setattr(lifecycle, "_require_root", lambda: None)
    layout = _layout(tmp_path / "layout")
    runner = FakeRunner()
    old, old_hash = _artifact(tmp_path / "old.tar.gz", "older-synthetic", b"old\n")
    new, new_hash = _artifact(tmp_path / "new.tar.gz", "newer-synthetic", b"new\n")
    lifecycle.install_shared(old, old_hash, layout=layout, runner=runner)
    instances = [Legacy(tmp_path / name, instance_id=name, prefix=name) for name in ("alpha", "beta")]
    for legacy in instances:
        legacy.write_ui_state([_legacy_item("a", "note.md", favorite=True)], [{"rootId": "a", "path": "note.md", "mode": "edit"}])
        _managed_instance(layout, legacy.instance_id, legacy.config, legacy.state)
    originals = {legacy.instance_id: legacy.config.read_bytes() for legacy in instances}
    units = {legacy.instance_id: (layout.unit_root / f"hopper-files-{legacy.instance_id}.service").read_bytes() for legacy in instances}

    def check_pending(plan, _release, config_v1):
        pending = pending_journals(Path(str(plan["stateDirectory"])))
        if pending:
            raise LifecycleError("; ".join(pending))

    failing: set[str] = set()

    def convert(plan, _release, direction, config_v1):
        if plan["instanceId"] in failing and direction == "forward":
            raise LifecycleError("synthetic conversion failure")
        operation = migrate_forward if direction == "forward" else migrate_return
        return operation(config_v1, Path(str(plan["stateDirectory"])), str(plan["instanceId"]))

    monkeypatch.setattr(lifecycle, "_check_pending", check_pending)
    monkeypatch.setattr(lifecycle, "_migrate_instance_state", convert)
    confirmations: list[str] = []

    failing.add("beta")
    with pytest.raises(LifecycleError, match="returned to older-synthetic"):
        migrate_release(new, new_hash, layout=layout, runner=runner, confirm=lambda text: confirmations.append(text) or True)
    assert layout.selector.resolve().name == "older-synthetic"
    for legacy in instances:
        assert legacy.config.read_bytes() == originals[legacy.instance_id]
        assert json.loads(ui_state_path(legacy.state).read_text(encoding="utf-8"))["version"] == 1
        assert (layout.unit_root / f"hopper-files-{legacy.instance_id}.service").read_bytes() == units[legacy.instance_id]
    assert "alpha" in confirmations[0] and "beta" in confirmations[0]

    failing.clear()
    commands_before = len(runner.commands)
    result = migrate_release(new, new_hash, layout=layout, runner=runner, confirm=lambda _text: True)
    issued = [command for command in runner.commands[commands_before:] if "systemctl" in command[0]]
    stops = [index for index, command in enumerate(issued) if command[1] == "stop"]
    starts = [index for index, command in enumerate(issued) if command[1] == "start"]
    assert result["previous"] == "older-synthetic" and result["current"] == "newer-synthetic"
    assert len(stops) == len(starts) == 2 and max(stops) < min(starts)
    assert layout.selector.resolve().name == "newer-synthetic"
    for legacy in instances:
        config = json.loads(legacy.config.read_text(encoding="utf-8"))
        assert config["version"] == 2 and "roots" not in config and "system" not in config
        backup = legacy.config.with_name(f"{legacy.config.stem}.v1-before-single-base{legacy.config.suffix}")
        assert backup.read_bytes() == originals[legacy.instance_id]
        unit = (layout.unit_root / f"hopper-files-{legacy.instance_id}.service").read_text(encoding="utf-8")
        assert "UMask=0002" in unit and "ReadWritePaths" not in unit and "ProtectSystem" not in unit
        assert load_ui_state(legacy.state)["items"][0]["path"] == str(legacy.a / "note.md")

    with pytest.raises(LifecycleError, match="not confirm"):
        return_release("older-synthetic", layout=layout, runner=runner, confirm=lambda _text: False)
    assert layout.selector.resolve().name == "newer-synthetic"
    back = return_release("older-synthetic", layout=layout, runner=runner, confirm=lambda _text: True)
    assert back["current"] == "older-synthetic"
    assert layout.selector.resolve().name == "older-synthetic"
    for legacy in instances:
        assert legacy.config.read_bytes() == originals[legacy.instance_id]
        assert (layout.unit_root / f"hopper-files-{legacy.instance_id}.service").read_bytes() == units[legacy.instance_id]
        returned = json.loads(ui_state_path(legacy.state).read_text(encoding="utf-8"))
        assert returned["version"] == 1
        assert returned["items"][0]["rootId"] == "a" and returned["items"][0]["path"] == "note.md"
