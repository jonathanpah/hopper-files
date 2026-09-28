"""Synthetic trash, retention, metadata, and durable delete/restore checks."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import struct
import threading
import time
from pathlib import Path

import pytest

from hopper_files.config import load_config
from hopper_files.roots import build_catalog
from hopper_files.state import initialize_state, load_ui_state, replace_ui_state
from hopper_files.trash import RETENTION_SECONDS, TrashCrash, TrashError, TrashStore
import hopper_files.trash as trash_module

from conftest import DOCUMENTS, PASSWORD, boot, D, ROOT, doc


def _r(root: Path, relative: str) -> str:
    """Address path relative to / of an entry below a test directory."""
    return str(root / relative)[1:]


def _proposal(items: list[dict[str, object]]) -> dict[str, object]:
    from hopper_files.state import default_ui_state

    payload = default_ui_state()
    payload.pop("stateRevision")
    payload["items"] = items
    return payload


# The directory the current test deletes from; _prepare sets it. UI marks use
# absolute paths (HF-META-004), so they depend on where the fixture lives.
_ROOT: list[Path] = [DOCUMENTS]


@pytest.fixture(autouse=True)
def _documents_root():
    _ROOT[0] = DOCUMENTS
    yield


def _item(path: str, *, labels: list[str] | None = None, favorite: bool = False, emoji: str | None = None, inode: int | None = None, device: int | None = None) -> dict[str, object]:
    return {
        "path": str(_ROOT[0] / path),
        "labelIds": labels or [],
        "favorite": favorite,
        "emoji": emoji,
        "inode": inode,
        "device": device,
    }


def _store(config) -> TrashStore:
    return TrashStore(config.state_directory, config.instance_id)


def _catalog(config):
    return build_catalog(config, strict=False)


def _ext4(tmp_path: Path) -> Path:
    path = Path("/var/tmp") / f"hf-trash-{os.getpid()}-{time.monotonic_ns()}"
    path.mkdir(mode=0o700)
    if path.stat().st_dev == tmp_path.stat().st_dev:
        shutil.rmtree(path)
        pytest.skip("this host has no second filesystem for EXDEV")
    return path


def _named_acl() -> bytes:
    user_obj, named_user, group_obj, mask, other = 1, 2, 4, 16, 32

    def entry(tag: int, perm: int, ident: int) -> bytes:
        return struct.pack("<HHI", tag, perm, ident)

    return struct.pack("<I", 2) + b"".join(
        (
            entry(user_obj, 6, 0xFFFFFFFF),
            entry(named_user, 4, os.geteuid()),
            entry(group_obj, 4, 0xFFFFFFFF),
            entry(mask, 4, 0xFFFFFFFF),
            entry(other, 0, 0xFFFFFFFF),
        )
    )


def _default_acl() -> bytes:
    user_obj, named_user, group_obj, mask, other = 1, 2, 4, 16, 32

    def entry(tag: int, perm: int, ident: int) -> bytes:
        return struct.pack("<HHI", tag, perm, ident)

    return struct.pack("<I", 2) + b"".join(
        (
            entry(user_obj, 7, 0xFFFFFFFF),
            entry(named_user, 7, os.geteuid()),
            entry(group_obj, 5, 0xFFFFFFFF),
            entry(mask, 7, 0xFFFFFFFF),
            entry(other, 0, 0xFFFFFFFF),
        )
    )


def _prepare(tmp_path: Path, *, cross: bool = False):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    if cross:
        root = _ext4(tmp_path)
    else:
        root = DOCUMENTS
    _ROOT[0] = root
    from conftest import write_config

    config_path = write_config(tmp_path / "cfg", state=state, instance_id="trash")
    config = load_config(config_path)
    initialize_state(config.state_directory, config.instance_id)
    return config, root


def test_same_filesystem_file_keeps_inode_and_set_id(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    source = root / "note.txt"
    source.write_bytes(b"synthetic")
    os.chmod(source, 0o4640)
    before = source.stat()
    store = _store(config)
    deleted = store.delete(_catalog(config), ROOT, _r(root, "note.txt"), 1_700_000_000)
    payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"
    info = payload.stat()
    assert info.st_ino == before.st_ino and info.st_dev == before.st_dev
    assert stat.S_IMODE(info.st_mode) == 0o4640
    assert payload.read_bytes() == b"synthetic"
    assert not source.exists()
    restored = store.restore(_catalog(config), str(deleted["id"]), 1_700_000_000)
    assert restored["path"] == _r(root, "note.txt")
    assert source.stat().st_ino == before.st_ino
    assert stat.S_IMODE(source.stat().st_mode) == 0o4640


def test_same_filesystem_directory_keeps_children_and_symlink(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    folder = root / "nest"
    folder.mkdir()
    (folder / "child.txt").write_bytes(b"child")
    os.symlink("child.txt", folder / "link")
    child_inode = (folder / "child.txt").stat().st_ino
    directory_inode = folder.stat().st_ino
    store = _store(config)
    deleted = store.delete(_catalog(config), ROOT, _r(root, "nest"), 1_700_000_000)
    payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"
    assert payload.stat().st_ino == directory_inode
    assert (payload / "child.txt").stat().st_ino == child_inode
    assert (payload / "link").is_symlink()
    assert os.readlink(payload / "link") == "child.txt"
    store.restore(_catalog(config), str(deleted["id"]), 1_700_000_000)
    assert (root / "nest" / "child.txt").read_bytes() == b"child"
    assert os.readlink(root / "nest" / "link") == "child.txt"


def test_cross_filesystem_preserves_bytes_mode_owner_and_acl(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        source = root / "wide.txt"
        source.write_bytes(b"cross-bytes")
        os.chmod(source, 0o640)
        os.setxattr(source, "system.posix_acl_access", _named_acl())
        before = source.stat()
        store = _store(config)
        deleted = store.delete(_catalog(config), ROOT, _r(root, "wide.txt"), 1_700_000_000)
        payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"
        info = payload.stat()
        assert info.st_dev != before.st_dev
        assert payload.read_bytes() == b"cross-bytes"
        assert info.st_uid == before.st_uid and info.st_gid == before.st_gid
        assert stat.S_IMODE(info.st_mode) == 0o640
        assert os.getxattr(payload, "system.posix_acl_access") == _named_acl()
        folder = root / "tree"
        folder.mkdir()
        os.chmod(folder, 0o750)
        os.setxattr(folder, "system.posix_acl_default", _default_acl())
        (folder / "inner.txt").write_bytes(b"inner")
        os.chmod(folder / "inner.txt", 0o604)
        directory = store.delete(_catalog(config), ROOT, _r(root, "tree"), 1_700_000_000)
        copied = config.state_directory / "trash" / str(directory["id"]) / "payload"
        assert os.getxattr(copied, "system.posix_acl_default") == _default_acl()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o750
        assert (copied / "inner.txt").read_bytes() == b"inner"
        assert stat.S_IMODE((copied / "inner.txt").stat().st_mode) == 0o604
        store.restore(_catalog(config), str(directory["id"]), 1_700_000_000)
        assert os.getxattr(root / "tree", "system.posix_acl_default") == _default_acl()
        assert (root / "tree" / "inner.txt").read_bytes() == b"inner"
        assert not (config.state_directory / "trash" / str(directory["id"])).exists()
    finally:
        shutil.rmtree(root)


def test_cross_filesystem_refuses_unsupported_metadata_before_publication(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        store = _store(config)
        catalog = _catalog(config)
        setid = root / "setid.txt"
        setid.write_bytes(b"setid")
        os.chmod(setid, 0o4644)
        with pytest.raises(TrashError) as raised:
            store.delete(catalog, ROOT, _r(root, "setid.txt"), 1_700_000_000)
        assert raised.value.code == "unsupported_metadata"
        assert setid.read_bytes() == b"setid"
        assert store.list_entries(catalog, 1_700_000_000) == []
        extra = root / "extra.txt"
        extra.write_bytes(b"extra")
        os.setxattr(extra, "user.synthetic", b"nope")
        with pytest.raises(TrashError) as raised:
            store.delete(catalog, ROOT, _r(root, "extra.txt"), 1_700_000_000)
        assert raised.value.code == "unsupported_metadata"
        assert os.getxattr(extra, "user.synthetic") == b"nope"
        assert _cross_capability_is_unsupported()
    finally:
        shutil.rmtree(root)


def _cross_capability_is_unsupported() -> bool:
    node = {"mode": 0o644, "xattrs": {"security.capability": b"\x00"}, "children": []}
    return trash_module._cross_unsupported(node)


def test_cross_filesystem_refuses_when_metadata_cannot_be_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        source = root / "owned.txt"
        source.write_bytes(b"owned")
        def refuse(fd: int, uid: int, gid: int) -> None:
            raise OSError(errno.EPERM, "unsupported")

        monkeypatch.setattr(trash_module.os, "fchown", refuse)
        store = _store(config)
        with pytest.raises(TrashError) as raised:
            store.delete(_catalog(config), ROOT, _r(root, "owned.txt"), 1_700_000_000)
        assert raised.value.code == "unsupported_metadata"
        assert source.read_bytes() == b"owned"
        assert store.list_entries(_catalog(config), 1_700_000_000) == []
    finally:
        shutil.rmtree(root)


def test_capacity_refusal_leaves_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        source = root / "big.txt"
        source.write_bytes(b"abc" * 1000)
        store = _store(config)
        catalog = _catalog(config)
        monkeypatch.setattr(trash_module.os, "statvfs", lambda path: type("Usage", (), {"f_bavail": 0, "f_frsize": 4096})())
        with pytest.raises(TrashError) as raised:
            store.delete(catalog, ROOT, _r(root, "big.txt"), 1_700_000_000)
        assert raised.value.code == "limit"
        assert source.read_bytes() == b"abc" * 1000
        monkeypatch.undo()

        def no_space(fd: int, offset: int, length: int) -> None:
            raise OSError(errno.ENOSPC, "no space")

        monkeypatch.setattr(trash_module.os, "posix_fallocate", no_space)
        with pytest.raises(TrashError) as raised:
            store.delete(catalog, ROOT, _r(root, "big.txt"), 1_700_000_000)
        assert raised.value.code == "limit"
        assert source.read_bytes() == b"abc" * 1000
        assert store.list_entries(catalog, 1_700_000_000) == []
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize(
    "point",
    ["delete_during_copy", "delete_after_stage", "delete_after_publish", "delete_after_ui"],
)
def test_delete_crash_keeps_a_recoverable_copy(tmp_path: Path, point: str) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        source = root / "crash.txt"
        source.write_bytes(b"crash-me")
        store = _store(config)
        catalog = _catalog(config)
        store.fail_at.add(point)
        with pytest.raises(TrashCrash):
            store.delete(catalog, ROOT, _r(root, "crash.txt"), 1_700_000_000)
        store.fail_at.clear()
        store.recover(catalog, 1_700_000_000)
        entries = [entry for entry in store.list_entries(catalog, 1_700_000_000) if not entry["quarantined"]]
        payloads = list((config.state_directory / "trash").glob("*/payload"))
        source_bytes = source.read_bytes() if source.exists() else None
        trash_bytes = [path.read_bytes() for path in payloads if path.is_file()]
        assert b"crash-me" in trash_bytes or source_bytes == b"crash-me"
        assert not (source_bytes is None and not trash_bytes)
        if point in {"delete_during_copy", "delete_after_stage"}:
            assert source_bytes == b"crash-me"
            assert trash_bytes == []
        else:
            assert source_bytes is None
            assert trash_bytes == [b"crash-me"]
            assert entries[0]["recovery"] == "ready"
        store.recover(catalog, 1_700_000_000)
        assert [path.read_bytes() for path in (config.state_directory / "trash").glob("*/payload") if path.is_file()] == (
            [] if point in {"delete_during_copy", "delete_after_stage"} else [b"crash-me"]
        )
    finally:
        shutil.rmtree(root)


def test_directory_removal_crash_is_idempotent_and_does_not_delete_changed_bytes(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        folder = root / "box"
        folder.mkdir()
        (folder / "a.txt").write_bytes(b"A")
        (folder / "b.txt").write_bytes(b"B")
        store = _store(config)
        catalog = _catalog(config)
        store.fail_at.add("delete_during_source_removal")
        with pytest.raises(TrashCrash):
            store.delete(catalog, ROOT, _r(root, "box"), 1_700_000_000)
        store.fail_at.clear()
        assert folder.exists()
        remaining = next(folder.iterdir())
        remaining.write_bytes(b"CHANGED")
        store.recover(catalog, 1_700_000_000)
        assert remaining.read_bytes() == b"CHANGED"
        payloads = [path for path in (config.state_directory / "trash").glob("*/payload") if path.is_dir()]
        assert payloads
        assert (payloads[0] / "a.txt").read_bytes() == b"A"
        assert (payloads[0] / "b.txt").read_bytes() == b"B"
        listed = store.list_entries(catalog, 1_700_000_000)
        assert next(entry["recovery"] for entry in listed if entry.get("sourcePath") == _r(root, "box")) == "indeterminate"
        other = root / "clean"
        other.mkdir()
        (other / "a.txt").write_bytes(b"A")
        (other / "b.txt").write_bytes(b"B")
        store.fail_at.add("delete_during_source_removal")
        with pytest.raises(TrashCrash):
            store.delete(catalog, ROOT, _r(root, "clean"), 1_700_000_000)
        store.fail_at.clear()
        store.recover(catalog, 1_700_000_000)
        assert not other.exists()
    finally:
        shutil.rmtree(root)


def test_restore_crash_publishes_one_copy_and_keeps_marks_on_that_copy(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        source = root / "back.txt"
        source.write_bytes(b"back")
        info = source.stat()
        replace_ui_state(
            config.state_directory,
            base_revision=0,
            document=_proposal([_item("back.txt", labels=["azul"], favorite=True, inode=info.st_ino, device=info.st_dev)]),
        )
        store = _store(config)
        catalog = _catalog(config)
        deleted = store.delete(catalog, ROOT, _r(root, "back.txt"), 1_700_000_000)
        assert load_ui_state(config.state_directory)["items"] == []
        for point in ("restore_after_publish", "restore_before_trash_removal"):
            store.fail_at.add(point)
            with pytest.raises(TrashCrash):
                store.restore(catalog, str(deleted["id"]), 1_700_000_000)
            store.fail_at.clear()
            store.recover(catalog, 1_700_000_000)
            assert source.read_bytes() == b"back"
            items = load_ui_state(config.state_directory)["items"]
            assert items[0]["path"] == str(root / "back.txt") and items[0]["labelIds"] == ["azul"]
            assert not (config.state_directory / "trash" / str(deleted["id"])).exists()
            if point != "restore_before_trash_removal":
                deleted = store.delete(catalog, ROOT, _r(root, "back.txt"), 1_700_000_000)
    finally:
        shutil.rmtree(root)


def test_restore_does_not_give_marks_to_a_replaced_inode(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        source = root / "swap.txt"
        source.write_bytes(b"original")
        store = _store(config)
        catalog = _catalog(config)
        replace_ui_state(
            config.state_directory,
            base_revision=0,
            document=_proposal([_item("swap.txt", labels=["verde"], favorite=True)]),
        )
        deleted = store.delete(catalog, ROOT, _r(root, "swap.txt"), 1_700_000_000)
        store.fail_at.add("restore_after_publish")
        with pytest.raises(TrashCrash):
            store.restore(catalog, str(deleted["id"]), 1_700_000_000)
        store.fail_at.clear()
        source.unlink()
        source.write_bytes(b"occupant")
        store.recover(catalog, 1_700_000_000)
        assert source.read_bytes() == b"occupant"
        assert load_ui_state(config.state_directory)["items"] == []
        entries = store.list_entries(catalog, 1_700_000_000)
        assert entries[0]["quarantined"] is False
        assert entries[0]["uiMetadata"][0]["labelIds"] == ["verde"]
        assert (config.state_directory / "trash" / entries[0]["id"] / "payload").read_bytes() == b"original"
    finally:
        shutil.rmtree(root)


def test_marks_move_with_descendants_and_not_to_a_new_occupant(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    folder = root / "nest"
    folder.mkdir()
    (folder / "kid.txt").write_bytes(b"kid")
    (root / "untouched.txt").write_bytes(b"u")
    folder_info = folder.stat()
    kid_info = (folder / "kid.txt").stat()
    replace_ui_state(
        config.state_directory,
        base_revision=0,
        document=_proposal(
            [
                _item("nest", labels=["verde"], favorite=True, emoji="📁", inode=folder_info.st_ino, device=folder_info.st_dev),
                _item("nest/kid.txt", labels=["azul"], inode=kid_info.st_ino, device=kid_info.st_dev),
                _item("untouched.txt", labels=["cinza"]),
            ]
        ),
    )
    revision = load_ui_state(config.state_directory)["stateRevision"]
    store = _store(config)
    catalog = _catalog(config)
    deleted = store.delete(catalog, ROOT, _r(root, "nest"), 1_700_000_000)
    current = load_ui_state(config.state_directory)
    assert current["stateRevision"] == revision + 1
    assert [item["path"] for item in current["items"]] == [str(root / "untouched.txt")]
    assert {record["relativePath"] for record in deleted["uiMetadata"]} == {"", "kid.txt"}
    folder.mkdir()
    (folder / "kid.txt").write_bytes(b"new-kid")
    assert [item["path"] for item in load_ui_state(config.state_directory)["items"]] == [str(root / "untouched.txt")]
    from hopper_files.state import UiStateConflict

    with pytest.raises(UiStateConflict) as conflict:
        replace_ui_state(
            config.state_directory,
            base_revision=revision,
            document=_proposal(
                [
                    _item("nest", labels=["verde"], favorite=True, emoji="📁"),
                    _item("nest/kid.txt", labels=["azul"]),
                    _item("untouched.txt", labels=["cinza"]),
                ]
            ),
        )
    assert conflict.value.state_revision == current["stateRevision"]
    shutil.rmtree(folder)
    current = load_ui_state(config.state_directory)
    current_items = [item for item in current["items"] if item["path"] != str(root / "alt")]
    replace_ui_state(
        config.state_directory,
        base_revision=current["stateRevision"],
        document=_proposal(current_items + [_item("alt", labels=["roxo"])]),
    )
    with pytest.raises(TrashError) as raised:
        store.restore(catalog, str(deleted["id"]), 1_700_000_000, _r(root, "alt"))
    assert raised.value.code == "metadata_conflict"
    assert not (root / "alt").exists()
    restored = store.restore(catalog, str(deleted["id"]), 1_700_000_000, _r(root, "nest-alt"))
    assert restored["path"] == _r(root, "nest-alt")
    items = {item["path"]: item for item in load_ui_state(config.state_directory)["items"]}
    assert items[str(root / "nest-alt")]["emoji"] == "📁"
    assert items[str(root / "nest-alt/kid.txt")]["labelIds"] == ["azul"]
    assert str(root / "nest") not in items
    assert (root / "nest-alt" / "kid.txt").read_bytes() == b"kid"


def test_restore_conflict_offers_an_explicit_alternative(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    source = root / "note.txt"
    source.write_bytes(b"first")
    store = _store(config)
    catalog = _catalog(config)
    deleted = store.delete(catalog, ROOT, _r(root, "note.txt"), 1_700_000_000)
    source.write_bytes(b"occupant")
    with pytest.raises(TrashError) as raised:
        store.restore(catalog, str(deleted["id"]), 1_700_000_000)
    assert raised.value.code == "destination_occupied"
    assert source.read_bytes() == b"occupant"
    assert (config.state_directory / "trash" / str(deleted["id"]) / "payload").read_bytes() == b"first"
    restored = store.restore(catalog, str(deleted["id"]), 1_700_000_000, _r(root, "note-2.txt"))
    assert restored["path"] == _r(root, "note-2.txt")
    assert (root / "note-2.txt").read_bytes() == b"first"
    assert not (config.state_directory / "trash" / str(deleted["id"])).exists()


def test_quarantine_blocks_restore_and_purge(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    source = root / "note.txt"
    source.write_bytes(b"keep")
    store = _store(config)
    catalog = _catalog(config)
    deleted = store.delete(catalog, ROOT, _r(root, "note.txt"), 1_000)
    entry = config.state_directory / "trash" / str(deleted["id"])
    meta_path = entry / "meta.json"
    meta_path.write_bytes(meta_path.read_bytes().replace(b"note.txt", b"nope.txt"))
    listed = store.list_entries(catalog, 1_000)
    assert listed == [{"id": deleted["id"], "quarantined": True, "recovery": "ready", "legacy": False}]
    with pytest.raises(TrashError) as raised:
        store.restore(catalog, str(deleted["id"]), 1_000)
    assert raised.value.code == "quarantined"
    assert store.purge(1_000 + RETENTION_SECONDS) == 0
    assert entry.is_dir()
    payload = entry / "payload"
    payload.write_bytes(b"truncated")
    # The tampered metadata is already quarantined; truncating the payload
    # must not make purge treat the entry as valid.
    assert store.purge(1_000 + RETENTION_SECONDS + 10) == 0
    assert payload.read_bytes() == b"truncated"


def test_purge_uses_protected_server_time_and_not_an_early_clock(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    (root / "old.txt").write_bytes(b"old")
    (root / "young.txt").write_bytes(b"young")
    store = _store(config)
    catalog = _catalog(config)
    old = store.delete(catalog, ROOT, _r(root, "old.txt"), 5_000)
    young = store.delete(catalog, ROOT, _r(root, "young.txt"), 5_000 + 10)
    assert store.purge(5_000 - 1) == 0
    assert store.purge(5_000 + RETENTION_SECONDS - 1) == 0
    assert (config.state_directory / "trash" / str(old["id"])).is_dir()
    assert store.purge(5_000 + RETENTION_SECONDS) == 1
    assert not (config.state_directory / "trash" / str(old["id"])).exists()
    assert (config.state_directory / "trash" / str(young["id"])).is_dir()
    assert store.purge(5_000 + 10 + RETENTION_SECONDS) == 1


def test_version_1_metadata_stays_quarantined_and_is_never_purged(tmp_path: Path) -> None:
    """HF-TRASH-001/HF-NAV-012: unconverted version 1 metadata stays in trash, quarantined."""
    config, root = _prepare(tmp_path)
    (root / "note.txt").write_bytes(b"legacy")
    store = _store(config)
    deleted = store.delete(_catalog(config), ROOT, _r(root, "note.txt"), 2_000)
    entry = config.state_directory / "trash" / str(deleted["id"])
    meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    assert meta["version"] == 2 and meta["sourcePath"] == str(root / "note.txt") and "sourceRootId" not in meta
    legacy = {**meta, "version": 1, "sourceRootId": "retired", "sourcePath": "note.txt"}
    raw = trash_module._dump_meta(legacy)
    (entry / "meta.json").write_bytes(raw)
    (entry / "meta.seal").write_bytes(trash_module._dump_seal(store._mac(raw, trash_module._seal_digest_at(entry))))
    catalog = _catalog(config)
    listed = store.list_entries(catalog, 2_000)
    assert listed == [{"id": deleted["id"], "quarantined": True, "recovery": "ready", "legacy": True}]
    with pytest.raises(TrashError) as raised:
        store.restore(catalog, str(deleted["id"]), 2_000)
    assert raised.value.code == "quarantined"
    assert store.purge(2_000 + RETENTION_SECONDS) == 0
    assert (entry / "payload").read_bytes() == b"legacy"


def test_dotfiles_and_hard_links_move_to_trash_like_other_files(tmp_path: Path) -> None:
    """HF-NAV-009: no protected names. A same-filesystem trash keeps the inode and other links."""
    config, root = _prepare(tmp_path)
    secret = root / ".env"
    secret.write_bytes(b"synthetic")
    store = _store(config)
    catalog = _catalog(config)
    env = store.delete(catalog, ROOT, _r(root, ".env"), 10)
    assert not secret.exists()
    first = root / "a.txt"
    second = root / "b.txt"
    first.write_bytes(b"link")
    os.link(first, second)
    inode = first.stat().st_ino
    linked = store.delete(catalog, ROOT, _r(root, "a.txt"), 10)
    payload = config.state_directory / "trash" / str(linked["id"]) / "payload"
    assert linked["separatedLinks"] == 0
    assert payload.stat().st_ino == inode and second.stat().st_ino == inode
    assert second.stat().st_nlink == 2
    store.restore(catalog, str(env["id"]), 10)
    assert secret.read_bytes() == b"synthetic"


def test_cross_filesystem_trash_separates_a_hard_link_with_exact_metadata(tmp_path: Path) -> None:
    """HF-FILE-002/HF-TRASH-005: only the addressed name leaves; the copy keeps UID, GID, mode, and ACL."""
    config, root = _prepare(tmp_path, cross=True)
    try:
        first = root / "a.txt"
        second = root / "b.txt"
        first.write_bytes(b"linked bytes")
        os.chmod(first, 0o640)
        os.setxattr(first, "system.posix_acl_access", _named_acl())
        os.link(first, second)
        before = first.stat()
        acl = os.getxattr(first, "system.posix_acl_access")
        store = _store(config)
        deleted = store.delete(_catalog(config), ROOT, _r(root, "a.txt"), 10)
        payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"
        info = payload.stat()
        assert deleted["separatedLinks"] == 1
        assert not first.exists()
        assert second.stat().st_ino == before.st_ino and second.stat().st_nlink == 1
        assert info.st_ino != before.st_ino and info.st_nlink == 1
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode))
        assert os.getxattr(payload, "system.posix_acl_access") == acl
        assert payload.read_bytes() == b"linked bytes"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_delete_without_write_permission_fails_without_change(tmp_path: Path) -> None:
    """HF-NAV-003: the kernel decides; a refused delete leaves the file in place."""
    config, root = _prepare(tmp_path)
    locked = root / "locked"
    locked.mkdir()
    (locked / "doc.txt").write_bytes(b"doc")
    os.chmod(locked, 0o500)
    store = _store(config)
    try:
        with pytest.raises(TrashError) as raised:
            store.delete(_catalog(config), ROOT, _r(root, "locked/doc.txt"), 10)
        assert raised.value.code == "forbidden"
    finally:
        os.chmod(locked, 0o700)
    assert (locked / "doc.txt").read_bytes() == b"doc"
    assert not any((config.state_directory / "trash").glob("*/payload"))


def test_instances_do_not_share_trash(tmp_path: Path) -> None:
    left_config, left_root = _prepare(tmp_path / "left")
    right_config, right_root = _prepare(tmp_path / "right")
    (left_root / "note.txt").write_bytes(b"left")
    (right_root / "note.txt").write_bytes(b"right")
    left = _store(left_config)
    right = _store(right_config)
    deleted = left.delete(_catalog(left_config), ROOT, _r(left_root, "note.txt"), 10)
    assert right.list_entries(_catalog(right_config), 10) == []
    with pytest.raises(TrashError) as raised:
        right.restore(_catalog(right_config), str(deleted["id"]), 10)
    assert raised.value.code == "not_found"
    copied = right_config.state_directory / "trash" / str(deleted["id"])
    shutil.copytree(left_config.state_directory / "trash" / str(deleted["id"]), copied)
    listed = right.list_entries(_catalog(right_config), 10)
    assert listed[0]["id"] == deleted["id"] and listed[0]["quarantined"] is True
    with pytest.raises(TrashError) as raised:
        right.restore(_catalog(right_config), str(deleted["id"]), 10)
    assert raised.value.code == "quarantined"


def test_concurrent_deletes_keep_distinct_entries(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    for name in ("one.txt", "two.txt"):
        (root / name).write_bytes(name.encode())
    store = _store(config)
    catalog = _catalog(config)
    errors: list[BaseException] = []

    def delete(name: str) -> None:
        try:
            store.delete(catalog, ROOT, _r(root, name), 20)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=delete, args=(name,)) for name in ("one.txt", "two.txt")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert not (root / "one.txt").exists() and not (root / "two.txt").exists()
    payloads = sorted(path.read_bytes() for path in (config.state_directory / "trash").glob("*/payload"))
    assert payloads == [b"one.txt", b"two.txt"]


def test_lock_file_created_by_a_concurrent_request_is_accepted(tmp_path: Path, monkeypatch) -> None:
    config, _root = _prepare(tmp_path)
    store = _store(config)
    with store._lock():
        pass
    lock_path = store._lock_path
    original_exists = Path.exists

    def exists(path: Path, *args, **kwargs) -> bool:
        # The other request creates the lock file between the check and the exclusive create.
        return False if path == lock_path else original_exists(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    with store._lock():
        pass
    assert stat.S_ISREG(os.lstat(lock_path).st_mode)


def _occupy_lock_path(lock_path: Path, tmp_path: Path, kind: str) -> None:
    """Put a symlink, FIFO, or directory at a lock path that does not exist yet."""
    if kind == "broken symlink":
        lock_path.symlink_to(tmp_path / "missing-target")
    elif kind == "symlink to a file":
        target = tmp_path / "regular-file"
        target.write_bytes(b"data")
        lock_path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(lock_path)
    elif kind == "directory":
        lock_path.mkdir()
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind, expected_exception",
    [
        ("broken symlink", OSError),
        ("symlink to a file", OSError),
        ("fifo", TrashError),
        ("directory", IsADirectoryError),
    ],
)
def test_lock_refuses_a_non_regular_file_in_its_place(
    tmp_path: Path, kind: str, expected_exception: type[BaseException]
) -> None:
    config, _root = _prepare(tmp_path)
    store = _store(config)
    lock_path = store._lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _occupy_lock_path(lock_path, tmp_path, kind)
    with pytest.raises(expected_exception):
        with store._lock():
            pass


def test_http_trash_requires_session_and_csrf_and_has_no_early_purge(tmp_path: Path) -> None:
    with boot(tmp_path, password=PASSWORD, instance_id="trash-http") as world:
        denied = world.client.get("/api/trash")
        assert denied.status_code == 401
        signed = world.post_login(PASSWORD)
        csrf = signed.json()["csrfToken"]
        missing = world.client.post(
            "/api/trash",
            json={"action": "delete", "source": {"rootId": ROOT, "path": doc("note.txt")}},
            headers={"origin": world.config.origin},
        )
        assert missing.status_code == 403
        (DOCUMENTS / "note.txt").write_bytes(b"http")
        state = world.client.get("/api/state")
        body = {key: value for key, value in state.json().items() if key != "stateRevision"}
        body["items"] = [_item("note.txt", labels=["azul"], favorite=True)]
        updated = world.client.put(
            "/api/state",
            json={"baseRevision": state.json()["stateRevision"], **body},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        assert updated.status_code == 200
        created = world.client.post(
            "/api/trash",
            json={"action": "delete", "source": {"rootId": ROOT, "path": doc("note.txt")}},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        assert created.status_code == 201, created.text
        assert created.json()["uiMetadata"][0]["favorite"] is True
        listed = world.client.get("/api/trash")
        assert listed.status_code == 200
        assert listed.json()["entries"][0]["sourcePath"] == doc("note.txt")
        assert world.client.get("/api/state").json()["items"] == []
        purge = world.client.post(
            "/api/trash",
            json={"action": "purge", "id": created.json()["id"]},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        assert purge.status_code == 422
        (DOCUMENTS / "note.txt").write_bytes(b"occupant")
        conflict = world.client.post(
            "/api/trash",
            json={"action": "restore", "id": created.json()["id"]},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        assert conflict.status_code == 409
        restored = world.client.post(
            "/api/trash",
            json={"action": "restore", "id": created.json()["id"], "alternativePath": doc("note-2.txt")},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        assert restored.status_code == 200, restored.text
        assert (DOCUMENTS / "note-2.txt").read_bytes() == b"http"
        assert world.client.get("/api/state").json()["items"][0]["path"] == "/" + doc("note-2.txt")
        assert world.client.get("/api/roots").status_code == 404


def _marked_source(root: Path, kind: str) -> str:
    if kind == "file":
        (root / "item.txt").write_bytes(b"original-bytes")
        return "item.txt"
    folder = root / "itemdir"
    folder.mkdir()
    (folder / "child.txt").write_bytes(b"child-bytes")
    return "itemdir"


def _replace_restored_bytes(root: Path, relative: str) -> None:
    target = root / relative
    if target.is_dir() and not target.is_symlink():
        (target / "child.txt").write_bytes(b"occupant-bytes")
        return
    target.write_bytes(b"occupant-bytes")


def _recreate_identical(root: Path, relative: str) -> None:
    """Replace the published object with another inode and the same bytes.

    ext4 can recycle the freed inode for the next create. A holder takes that
    inode so the lookalike is a different object, not an identity match.
    """
    target = root / relative
    holder = target.with_name(target.name + "-holder")
    before = target.stat().st_ino
    if target.is_dir() and not target.is_symlink():
        child = (target / "child.txt").read_bytes()
        shutil.rmtree(target)
        holder.mkdir()
        target.mkdir()
        (target / "child.txt").write_bytes(child)
    else:
        data = target.read_bytes()
        target.unlink()
        holder.write_bytes(b"hold-inode")
        target.write_bytes(data)
    assert target.stat().st_ino != before
    if holder.is_dir() and not holder.is_symlink():
        holder.rmdir()
    else:
        holder.unlink()


def _replace_with_other_kind(root: Path, relative: str) -> None:
    target = root / relative
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
        target.write_bytes(b"occupant-file")
        return
    target.unlink()
    target.mkdir()
    (target / "child.txt").write_bytes(b"occupant-dir")


@pytest.mark.parametrize("cross", [False, True])
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_restore_rename_before_identity_record_keeps_marks(tmp_path: Path, cross: bool, kind: str) -> None:
    config, root = _prepare(tmp_path, cross=cross)
    try:
        relative = _marked_source(root, kind)
        info = (root / relative).stat()
        replace_ui_state(
            config.state_directory,
            base_revision=0,
            document=_proposal([_item(relative, labels=["azul"], favorite=True, inode=info.st_ino, device=info.st_dev)]),
        )
        store = _store(config)
        catalog = _catalog(config)
        deleted = store.delete(catalog, ROOT, _r(root, relative), 1_700_000_000)
        assert load_ui_state(config.state_directory)["items"] == []
        store.fail_at.add("restore_after_rename_before_identity")
        with pytest.raises(TrashCrash):
            store.restore(catalog, str(deleted["id"]), 1_700_000_000)
        store.fail_at.clear()
        journal = config.state_directory / "journals" / "trash" / f"{deleted['id']}.json"
        body = json.loads(journal.read_text(encoding="utf-8"))
        assert body["publishedIdentity"] is None
        if cross:
            assert isinstance(body["stagedIdentity"], list) and len(body["stagedIdentity"]) == 2
        store.recover(catalog, 1_700_000_000)
        if kind == "file":
            assert (root / relative).read_bytes() == b"original-bytes"
        else:
            assert (root / relative / "child.txt").read_bytes() == b"child-bytes"
        items = load_ui_state(config.state_directory)["items"]
        assert items[0]["path"] == str(root / relative) and items[0]["favorite"] is True and items[0]["labelIds"] == ["azul"]
        assert not journal.exists()
        assert not (config.state_directory / "trash" / str(deleted["id"])).exists()
    finally:
        if cross:
            shutil.rmtree(root)


def test_restore_fsync_failure_after_same_filesystem_rename_keeps_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, root = _prepare(tmp_path)
    relative = _marked_source(root, "file")
    info = (root / relative).stat()
    replace_ui_state(
        config.state_directory,
        base_revision=0,
        document=_proposal([_item(relative, labels=["azul"], favorite=True, inode=info.st_ino, device=info.st_dev)]),
    )
    store = _store(config)
    catalog = _catalog(config)
    deleted = store.delete(catalog, ROOT, _r(root, relative), 1_700_000_000)
    payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"
    destination = root / relative
    real_fsync = trash_module.os.fsync
    injected = False

    def fail_after_rename(fd: int) -> None:
        nonlocal injected
        if not injected and destination.exists() and not payload.exists():
            injected = True
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(trash_module.os, "fsync", fail_after_rename)
    with pytest.raises(OSError) as raised:
        store.restore(catalog, str(deleted["id"]), 1_700_000_000)
    assert raised.value.errno == errno.EIO and injected
    monkeypatch.setattr(trash_module.os, "fsync", real_fsync)

    journal = config.state_directory / "journals" / "trash" / f"{deleted['id']}.json"
    assert journal.is_file()
    body = json.loads(journal.read_text(encoding="utf-8"))
    assert body["phase"] == "publishing" and body["publishedIdentity"] is None
    store.recover(catalog, 1_700_000_000)
    assert destination.read_bytes() == b"original-bytes"
    assert load_ui_state(config.state_directory)["items"][0]["favorite"] is True
    assert load_ui_state(config.state_directory)["items"][0]["labelIds"] == ["azul"]
    assert not journal.exists() and not payload.exists()


def test_restore_without_file_handle_support_refuses_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, root = _prepare(tmp_path)
    relative = _marked_source(root, "file")
    info = (root / relative).stat()
    replace_ui_state(
        config.state_directory,
        base_revision=0,
        document=_proposal([_item(relative, labels=["verde"], favorite=True, inode=info.st_ino, device=info.st_dev)]),
    )
    store = _store(config)
    catalog = _catalog(config)
    deleted = store.delete(catalog, ROOT, _r(root, relative), 1_700_000_000)
    payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"

    def unavailable_handle(directory: Path) -> dict[str, object]:
        raise TrashError("unsupported_metadata")

    monkeypatch.setattr(trash_module, "_payload_handle", unavailable_handle)
    with pytest.raises(TrashError) as raised:
        store.restore(catalog, str(deleted["id"]), 1_700_000_000)
    assert raised.value.code == "unsupported_metadata"
    assert payload.is_file() and not (root / relative).exists()
    assert store._pending_ids() == set()
    assert load_ui_state(config.state_directory)["items"] == []
    assert b"verde" in (config.state_directory / "trash" / str(deleted["id"]) / "meta.json").read_bytes()
    assert store.list_entries(catalog, 1_700_000_000)[0]["id"] == deleted["id"]


@pytest.mark.parametrize("cross", [False, True])
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_restore_rename_occupant_does_not_receive_marks(tmp_path: Path, cross: bool, kind: str) -> None:
    config, root = _prepare(tmp_path, cross=cross)
    try:
        relative = _marked_source(root, kind)
        replace_ui_state(
            config.state_directory,
            base_revision=0,
            document=_proposal([_item(relative, labels=["verde"], favorite=True)]),
        )
        store = _store(config)
        catalog = _catalog(config)
        deleted = store.delete(catalog, ROOT, _r(root, relative), 1_700_000_000)
        store.fail_at.add("restore_after_rename_before_identity")
        with pytest.raises(TrashCrash):
            store.restore(catalog, str(deleted["id"]), 1_700_000_000)
        store.fail_at.clear()
        _replace_restored_bytes(root, relative)
        store.recover(catalog, 1_700_000_000)
        if kind == "file":
            assert (root / relative).read_bytes() == b"occupant-bytes"
        else:
            assert (root / relative / "child.txt").read_bytes() == b"occupant-bytes"
        assert load_ui_state(config.state_directory)["items"] == []
        journal = config.state_directory / "journals" / "trash" / f"{deleted['id']}.json"
        meta = config.state_directory / "trash" / str(deleted["id"]) / "meta.json"
        assert journal.is_file()
        assert b"verde" in meta.read_bytes()
        listed = store.list_entries(catalog, 1_700_000_000)
        assert listed[0]["id"] == deleted["id"]
        assert listed[0]["recovery"] == "indeterminate"
    finally:
        if cross:
            shutil.rmtree(root)


def test_restore_same_inode_reuse_with_identical_bytes_does_not_receive_marks(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        relative = _marked_source(root, "file")
        replace_ui_state(
            config.state_directory,
            base_revision=0,
            document=_proposal([_item(relative, labels=["verde"], favorite=True)]),
        )
        store = _store(config)
        catalog = _catalog(config)
        deleted = store.delete(catalog, ROOT, _r(root, relative), 1_700_000_000)
        store.fail_at.add("restore_after_rename_before_identity")
        with pytest.raises(TrashCrash):
            store.restore(catalog, str(deleted["id"]), 1_700_000_000)
        store.fail_at.clear()

        target = root / relative
        original = target.stat()
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            original_handle = trash_module._file_handle_at(parent_fd, relative)
        finally:
            os.close(parent_fd)
        data = target.read_bytes()
        target.unlink()
        reused = False
        for _ in range(256):
            target.write_bytes(data)
            current = target.stat()
            if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                reused = True
                break
            target.unlink()
        if not reused:
            pytest.skip("this filesystem did not recycle the test inode")
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            replacement_handle = trash_module._file_handle_at(parent_fd, relative)
        finally:
            os.close(parent_fd)
        assert replacement_handle != original_handle
        assert target.read_bytes() == data

        store.recover(catalog, 1_700_000_000)
        assert load_ui_state(config.state_directory)["items"] == []
        journal = config.state_directory / "journals" / "trash" / f"{deleted['id']}.json"
        meta = config.state_directory / "trash" / str(deleted["id"]) / "meta.json"
        payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"
        assert journal.is_file() and payload.is_file() and b"verde" in meta.read_bytes()
        listed = store.list_entries(catalog, 1_700_000_000)
        assert listed[0]["id"] == deleted["id"] and listed[0]["recovery"] == "indeterminate"
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("cross", [False, True])
@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("occupant", ["inode", "kind"])
def test_restore_rename_external_occupant_keeps_journal(
    tmp_path: Path, cross: bool, kind: str, occupant: str
) -> None:
    config, root = _prepare(tmp_path, cross=cross)
    try:
        relative = _marked_source(root, kind)
        replace_ui_state(
            config.state_directory,
            base_revision=0,
            document=_proposal([_item(relative, labels=["verde"], favorite=True)]),
        )
        store = _store(config)
        catalog = _catalog(config)
        deleted = store.delete(catalog, ROOT, _r(root, relative), 1_700_000_000)
        store.fail_at.add("restore_after_rename_before_identity")
        with pytest.raises(TrashCrash):
            store.restore(catalog, str(deleted["id"]), 1_700_000_000)
        store.fail_at.clear()
        published = root / relative
        published_inode = published.stat().st_ino
        if occupant == "inode":
            _recreate_identical(root, relative)
            assert published.stat().st_ino != published_inode
        else:
            _replace_with_other_kind(root, relative)
        store.recover(catalog, 1_700_000_000)
        assert load_ui_state(config.state_directory)["items"] == []
        journal = config.state_directory / "journals" / "trash" / f"{deleted['id']}.json"
        meta = config.state_directory / "trash" / str(deleted["id"]) / "meta.json"
        body = json.loads(journal.read_text(encoding="utf-8"))
        assert body["publishedIdentity"] is None
        assert journal.is_file()
        assert b"verde" in meta.read_bytes()
        if cross:
            payload = config.state_directory / "trash" / str(deleted["id"]) / "payload"
            assert payload.exists()
            if occupant == "inode":
                assert body["stagedIdentity"] != [published.stat().st_dev, published.stat().st_ino]
        listed = store.list_entries(catalog, 1_700_000_000)
        assert listed[0]["id"] == deleted["id"]
        assert listed[0]["recovery"] == "indeterminate"
    finally:
        if cross:
            shutil.rmtree(root)


def test_symlink_gid_set_failure_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected no-follow chown failure stops publication on every host."""
    link = tmp_path / "alias"
    os.symlink("child", link)
    requested_gid = 27
    actual_gid = os.lstat(link).st_gid
    real_chown = trash_module.os.chown

    def refuse_requested_gid(
        path: str,
        uid: int,
        gid: int,
        *args: object,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if path == "alias" and gid == requested_gid and follow_symlinks is False:
            raise OSError(errno.EINVAL, "injected symlink chown failure")
        real_chown(path, uid, gid, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(trash_module.os, "chown", refuse_requested_gid)
    node = {
        "uid": os.geteuid(),
        "gid": requested_gid,
        "mode": stat.S_IMODE(os.lstat(link).st_mode),
        "xattrs": {},
        "link": "child",
    }
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with pytest.raises(TrashError) as raised:
            trash_module._apply_symlink(parent, "alias", node)
        assert raised.value.code == "unsupported_metadata"
        assert os.lstat(link).st_gid == actual_gid
    finally:
        os.close(parent)


def test_cross_filesystem_symlink_keeps_owner_or_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, root = _prepare(tmp_path, cross=True)
    try:
        folder = root / "linked"
        folder.mkdir()
        (folder / "child.txt").write_bytes(b"child")
        link = folder / "alias"
        os.symlink("child.txt", link)
        gid_limit = None
        try:
            os.chown(link, os.geteuid(), 27, follow_symlinks=False)
        except OSError as exc:
            gid_limit = exc.errno
        before = os.lstat(link)
        before_mode = stat.S_IMODE(before.st_mode)
        before_xattrs = {
            name: os.getxattr(link, name, follow_symlinks=False)
            for name in os.listxattr(link, follow_symlinks=False)
        }
        store = _store(config)
        catalog = _catalog(config)
        deleted = store.delete(catalog, ROOT, _r(root, "linked"), 1_700_000_000)
        payload = config.state_directory / "trash" / str(deleted["id"]) / "payload" / "alias"
        after = os.lstat(payload)
        assert os.readlink(payload) == "child.txt"
        assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
        assert stat.S_IMODE(after.st_mode) == before_mode
        assert {
            name: os.getxattr(payload, name, follow_symlinks=False)
            for name in os.listxattr(payload, follow_symlinks=False)
        } == before_xattrs
        assert not link.exists()
        if gid_limit is not None:
            assert before.st_gid != 27

        other = root / "again"
        other.mkdir()
        (other / "child.txt").write_bytes(b"child")
        os.symlink("child.txt", other / "alias")

        real_chown = trash_module.os.chown

        def refuse_symlink_owner(path: str, uid: int, gid: int, *args: object, dir_fd: int | None = None, follow_symlinks: bool = True) -> None:
            if follow_symlinks is False:
                raise OSError(errno.EPERM, "symlink owner is unsupported")
            real_chown(path, uid, gid, dir_fd=dir_fd, follow_symlinks=True)

        monkeypatch.setattr(trash_module.os, "chown", refuse_symlink_owner)
        with pytest.raises(TrashError) as raised:
            store.delete(catalog, ROOT, _r(root, "again"), 1_700_000_000)
        assert raised.value.code == "unsupported_metadata"
        assert (other / "alias").is_symlink()
        assert os.lstat(other / "alias").st_gid == before.st_gid or os.lstat(other / "alias").st_gid == os.getegid()
        assert not any(
            entry.get("sourcePath") == "again"
            for entry in store.list_entries(catalog, 1_700_000_000)
            if not entry["quarantined"]
        )
    finally:
        shutil.rmtree(root)


def test_cross_filesystem_symlink_acl_preserved_or_refused(tmp_path: Path) -> None:
    """Preserve a symlink access ACL across EXDEV, or refuse before publication.

    This process cannot assign GID 27. The ACL result is whatever this
    filesystem pair actually supports; a refusal must keep the source.
    """
    config, root = _prepare(tmp_path, cross=True)
    try:
        folder = root / "linked"
        folder.mkdir()
        (folder / "child.txt").write_bytes(b"child")
        link = folder / "alias"
        os.symlink("child.txt", link)
        acl = _named_acl()
        try:
            os.setxattr(link, "system.posix_acl_access", acl, follow_symlinks=False)
        except OSError as exc:
            assert exc.errno in {
                errno.EPERM,
                errno.EOPNOTSUPP,
                errno.ENOTSUP,
                errno.EINVAL,
                errno.EACCES,
            }
            _assert_symlink_acl_copy_matches_or_refuses(tmp_path, acl)
            return
        before = os.lstat(link)
        store = _store(config)
        catalog = _catalog(config)
        try:
            deleted = store.delete(catalog, ROOT, _r(root, "linked"), 1_700_000_000)
        except TrashError as exc:
            assert exc.code == "unsupported_metadata"
            assert link.is_symlink()
            assert os.getxattr(link, "system.posix_acl_access", follow_symlinks=False) == acl
            assert os.lstat(link).st_gid == before.st_gid
            assert not any(
                entry.get("sourcePath") == "linked" and not entry["quarantined"]
                for entry in store.list_entries(catalog, 1_700_000_000)
            )
            return
        payload = config.state_directory / "trash" / str(deleted["id"]) / "payload" / "alias"
        after = os.lstat(payload)
        assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
        assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
        assert os.readlink(payload) == "child.txt"
        assert os.getxattr(payload, "system.posix_acl_access", follow_symlinks=False) == acl
        assert not link.exists()
    finally:
        shutil.rmtree(root)


def _assert_symlink_acl_copy_matches_or_refuses(tmp_path: Path, acl: bytes) -> None:
    source = tmp_path / "src-link"
    stage = tmp_path / "stage-link"
    source.mkdir()
    stage.mkdir()
    os.symlink("child", source / "alias")
    node = {
        "kind": "symlink",
        "link": "child",
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "mode": stat.S_IMODE(os.lstat(source / "alias").st_mode),
        "xattrs": {"system.posix_acl_access": acl},
    }
    src = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    dst = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            trash_module._copy_node(src, "alias", dst, "copied", node)
        except TrashError as exc:
            assert exc.code == "unsupported_metadata"
            assert not os.path.lexists(stage / "copied")
            return
        copied = stage / "copied"
        assert os.readlink(copied) == "child"
        info = os.lstat(copied)
        assert (info.st_uid, info.st_gid) == (os.geteuid(), os.getegid())
        assert os.getxattr(copied, "system.posix_acl_access", follow_symlinks=False) == acl
    finally:
        os.close(src)
        os.close(dst)
