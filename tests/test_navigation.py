"""Single filesystem base, visibility, and address resolution (HF-NAV-001..005)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

from hopper_files.config import ConfigError, load_config
from hopper_files.roots import AddressRejected, build_catalog, display_name, parse_relative_path, read_regular

from conftest import CLOCK_START, D, DOCUMENTS, PASSWORD, ROOT, ManualClock, boot, doc, write_config


def _signed(world) -> dict[str, str]:
    login = world.post_login(PASSWORD)
    assert login.status_code == 200, login.text
    return {"origin": world.config.origin, "x-csrf-token": login.json()["csrfToken"]}


def _list(world, path: str):
    return world.client.get("/api/list", params={"rootId": ROOT, "path": path})


def _entries(response) -> dict[str, dict[str, object]]:
    return {entry["name"]: entry for entry in response.json()["entries"]}


def test_configuration_version_2_has_no_roots_and_version_1_fails_closed(tmp_path) -> None:
    path = write_config(tmp_path, instance_id="single")
    config = load_config(path)
    assert config.version == 2
    catalog = build_catalog(config)
    assert [(item.root_id, str(item.path)) for item in catalog.documents] == [("fs", "/")]

    payload = json.loads(path.read_text(encoding="utf-8"))
    for field in ("roots", "system", "protectedPaths", "creationPolicies"):
        changed = {**payload, field: {}}
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ConfigError, match="retired"):
            load_config(path)
    path.write_text(json.dumps({**payload, "version": 1}), encoding="utf-8")
    with pytest.raises(ConfigError, match="HF-NAV-012"):
        load_config(path)


def test_invalid_address_forms_are_rejected_before_any_lookup() -> None:
    for relative in (
        "../outside",
        "/etc/passwd",
        "sub/../../outside",
        "a//b",
        "a/",
        "sub/../note.txt",
        "sub/./note.txt",
        "%2e%2e/outside",
        "%252e%252e/outside",
        "sub/%2fnote",
        "sub/%00note",
        "a\x00b",
        "a\\b",
        "bad\udcff",
    ):
        with pytest.raises(AddressRejected) as rejected:
            parse_relative_path(relative)
        assert rejected.value.code == "invalid"


def test_links_are_listed_with_targets_and_reached_only_through_the_real_path(tmp_path) -> None:
    real = DOCUMENTS / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "note.txt").write_bytes(b"visible-note")
    (DOCUMENTS / "dir-link").symlink_to("real", target_is_directory=True)
    (DOCUMENTS / "file-link").symlink_to(real / "sub" / "note.txt")
    (DOCUMENTS / "broken-link").symlink_to(DOCUMENTS / "missing")
    os.mkfifo(DOCUMENTS / "pipe")
    (DOCUMENTS / "pipe-link").symlink_to(DOCUMENTS / "pipe")
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        listing = _entries(_list(world, D))
        through_intermediate = _list(world, doc("dir-link/sub"))
        through_final = world.client.get("/api/raw", params={"rootId": ROOT, "path": doc("file-link")})
        real_path = world.client.get("/api/raw", params={"rootId": ROOT, "path": doc("real/sub/note.txt")})

    dir_link = dict(listing["dir-link"])
    # The link's own dates are reported like any entry's (HF-NAV-002); their values vary per run.
    assert dir_link.pop("modifiedAt").endswith("Z")
    assert dir_link.pop("createdAt") is None or listing["dir-link"]["createdAt"].endswith("Z")
    assert dir_link == {
        "name": "dir-link", "type": "link", "size": None, "openable": True, "addressable": True,
        "writable": False, "links": 1, "target": "real", "resolved": str(real), "resolvedType": "directory",
    }
    assert listing["file-link"]["resolved"] == str(real / "sub" / "note.txt")
    assert listing["file-link"]["resolvedType"] == "file"
    assert listing["broken-link"]["openable"] is False and listing["broken-link"]["reason"] == "broken"
    assert listing["pipe-link"]["openable"] is False and listing["pipe-link"]["reason"] == "special"
    assert listing["pipe"]["type"] == "other" and listing["pipe"]["openable"] is False
    assert through_intermediate.status_code in {403, 404}
    assert through_final.status_code in {403, 404}
    assert "visible-note" not in through_final.text
    assert real_path.status_code == 200 and real_path.content == b"visible-note"


def test_listing_reports_every_entry_including_dotfiles_stages_hard_links_and_invalid_names(tmp_path) -> None:
    (DOCUMENTS / ".ssh").mkdir()
    (DOCUMENTS / ".env").write_bytes(b"synthetic")
    (DOCUMENTS / ".hopper-stage-0123456789abcdef0123456789abcdef.tmp").write_bytes(b"leftover")
    (DOCUMENTS / "hard.txt").write_bytes(b"linked")
    os.link(DOCUMENTS / "hard.txt", DOCUMENTS / "hard-again.txt")
    closed = DOCUMENTS / "closed"
    closed.mkdir()
    readonly = DOCUMENTS / "readonly"
    readonly.mkdir()
    raw_name = b"bad\xff\\name.txt"
    with open(os.path.join(os.fsencode(DOCUMENTS), raw_name), "wb") as handle:
        handle.write(b"x")
    closed.chmod(0)
    readonly.chmod(0o555)
    try:
        with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
            _signed(world)
            response = _list(world, D)
            closed_open = _list(world, doc("closed"))
            terminal = {display_name(name)[0] for name in os.listdir(os.fsencode(DOCUMENTS))}
    finally:
        closed.chmod(0o755)
        readonly.chmod(0o755)

    assert response.status_code == 200
    entries = _entries(response)
    assert set(entries) == terminal
    assert response.json()["writable"] is True
    assert response.json()["address"] == "/" + D
    assert entries[".ssh"]["type"] == "directory" and entries[".ssh"]["openable"] is True
    assert entries[".env"]["type"] == "file" and entries[".env"]["openable"] is True
    assert entries[".hopper-stage-0123456789abcdef0123456789abcdef.tmp"]["type"] == "file"
    assert entries["hard.txt"]["links"] == entries["hard-again.txt"]["links"] == 2
    assert entries["closed"]["openable"] is False and entries["closed"]["reason"] == "permission"
    assert entries["readonly"]["openable"] is True and entries["readonly"]["writable"] is False
    invalid = entries["bad\\xFF\\\\name.txt"]
    assert invalid["addressable"] is False and invalid["openable"] is False
    assert invalid["reason"] == "invalid_name"
    assert closed_open.status_code in {403, 404}


def test_display_name_is_lossless_and_unambiguous() -> None:
    assert display_name("plain ç.md".encode()) == ("plain ç.md", True)
    assert display_name(b"a\\b") == ("a\\b", True)
    assert display_name(b"a\\b\xff") == ("a\\\\b\\xFF", False)
    assert display_name(b"\xc3\x28x") == ("\\xC3(x", False)


def test_root_listing_matches_the_terminal_and_includes_pseudo_filesystems(tmp_path) -> None:
    """HF-ACC-022 (metadata only): the first level of / as this account lists it."""
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        response = _list(world, "")
        terminal = {display_name(name)[0] for name in os.listdir(b"/")}

    assert response.status_code == 200
    entries = _entries(response)
    assert set(entries) == terminal
    assert {"proc", "sys", "dev"} <= set(entries)
    if Path("/bin").is_symlink():
        assert entries["bin"]["type"] == "link"
        assert entries["bin"]["target"] == os.readlink("/bin")
        assert entries["bin"]["resolved"] == os.path.realpath("/bin")
    root_home = Path("/root")
    if root_home.is_dir() and not os.access(root_home, os.R_OK | os.X_OK):
        assert entries["root"]["openable"] is False


def test_pseudo_filesystem_regular_file_reads_but_is_not_editable(tmp_path) -> None:
    source = Path("/proc/sys/kernel/ostype")
    if not source.is_file():
        pytest.skip("procfs kernel type file is unavailable")
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        opened = world.client.get("/api/file", params={"rootId": ROOT, "path": "proc/sys/kernel/ostype"})

    assert opened.status_code == 200, opened.text
    assert opened.json()["content"] == source.read_text()
    assert opened.json()["editable"] is False


def test_writes_follow_linux_permission_without_effect_on_refusal(tmp_path) -> None:
    locked = DOCUMENTS / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
            headers = _signed(world)
            token = world.client.post("/api/files/token", headers=headers).json()["operationToken"]
            refused = world.client.post(
                "/api/files",
                headers=headers,
                json={
                    "operationToken": token,
                    "actions": [{"action": "create", "kind": "file", "nameMode": "exact", "destination": {"rootId": ROOT, "path": doc("locked/new.txt")}}],
                },
            )
            token = world.client.post("/api/files/token", headers=headers).json()["operationToken"]
            allowed = world.client.post(
                "/api/files",
                headers=headers,
                json={
                    "operationToken": token,
                    "actions": [{"action": "create", "kind": "file", "nameMode": "exact", "destination": {"rootId": ROOT, "path": doc("open.txt")}}],
                },
            )
            listing = _list(world, doc("locked"))
    finally:
        locked.chmod(0o755)

    assert refused.status_code == 403
    assert refused.json()["uncommitted"][0]["error"] == "forbidden"
    assert not (locked / "new.txt").exists()
    assert not any(path.name.startswith(".hopper-stage-") for path in locked.iterdir())
    assert allowed.status_code == 200 and (DOCUMENTS / "open.txt").exists()
    assert listing.json()["writable"] is False


def test_concurrent_symlink_replacement_cannot_redirect_a_read(tmp_path) -> None:
    inside = DOCUMENTS / "gate"
    inside.mkdir()
    (inside / "file").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"outside")
    config = load_config(write_config(tmp_path / "cfg"))
    catalog = build_catalog(config)
    relative = doc("gate/file")
    assert read_regular(catalog, ROOT, relative) == b"inside"
    stop = threading.Event()

    def swap() -> None:
        while not stop.is_set():
            try:
                (inside / "file").unlink()
                inside.rmdir()
                inside.symlink_to(outside, target_is_directory=True)
            except OSError:
                pass
            time.sleep(0.001)
            try:
                inside.unlink()
                inside.mkdir()
                # Rename a complete file into place: a reader must never see it empty.
                staged = inside / "file.staged"
                staged.write_bytes(b"inside")
                staged.replace(inside / "file")
            except OSError:
                pass
            time.sleep(0.001)

    worker = threading.Thread(target=swap)
    worker.start()
    successes = rejections = 0
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and (successes < 1 or rejections < 1):
            try:
                content = read_regular(catalog, ROOT, relative)
            except AddressRejected:
                rejections += 1
                continue
            assert content == b"inside"
            successes += 1
    finally:
        stop.set()
        worker.join()
    assert successes >= 1 and rejections >= 1
    assert (outside / "file").read_bytes() == b"outside"


def test_reloaded_config_cannot_change_instance_identity(tmp_path) -> None:
    with boot(tmp_path / "http", ManualClock(CLOCK_START), password=PASSWORD, instance_id="alpha") as world:
        headers = _signed(world)
        saved = world.path.read_bytes()
        beta_state = tmp_path / "beta-state"
        beta = write_config(tmp_path / "beta", instance_id="beta", state=beta_state, port=8876)
        world.path.write_bytes(beta.read_bytes())
        os.chmod(world.path, 0o640)
        base = world.client.get("/api/state").json()
        crossed = world.client.put(
            "/api/state",
            headers=headers,
            json={"baseRevision": base["stateRevision"], **{key: value for key, value in base.items() if key != "stateRevision"}},
        )
        world.path.write_bytes(saved)
    assert crossed.status_code == 409
    assert crossed.json() == {"error": "conflict"}


def test_roots_route_is_removed(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        response = world.client.get("/api/roots")
    assert response.status_code == 404
    assert stat.S_ISDIR(os.stat("/").st_mode)


def _other_filesystem(*avoid: Path) -> Path | None:
    devices = {path.stat().st_dev for path in avoid}
    for parent in (Path("/var/tmp"), Path("/dev/shm"), DOCUMENTS.parent):
        if parent.is_dir() and os.access(parent, os.W_OK) and parent.stat().st_dev not in devices:
            return parent
    return None


@pytest.fixture
def apart(tmp_path):
    """A synthetic directory on a filesystem other than the state directory."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    parent = _other_filesystem(tmp_path)
    if parent is None:
        pytest.skip("no writable filesystem apart from the state directory")
    directory = Path(tempfile.mkdtemp(prefix="hf-apart-", dir=parent))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _address(path: Path) -> dict[str, str]:
    return {"rootId": ROOT, "path": str(path)[1:]}


def _operate(world, headers, action: str, source: Path, destination: Path):
    issued = world.client.post("/api/files/token", headers=headers)
    assert issued.status_code == 201, issued.text
    return world.client.post(
        "/api/files",
        headers=headers,
        json={
            "operationToken": issued.json()["operationToken"],
            "actions": [{"action": action, "source": _address(source), "destination": _address(destination)}],
        },
    )


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_nlink, stat.S_IMODE(info.st_mode)


def test_same_filesystem_rename_and_move_keep_identity_apart_from_the_state(tmp_path, apart) -> None:
    """HF-FILE-002/HF-NAV-003: one rename keeps inode, links, and metadata, whatever the state filesystem."""
    (apart / "renomear.txt").write_bytes(b"rename")
    (apart / "vinculo-a.txt").write_bytes(b"linked")
    os.link(apart / "vinculo-a.txt", apart / "vinculo-b.txt")
    (apart / "setgid.txt").write_bytes(b"set-id")
    os.chmod(apart / "setgid.txt", 0o2664)
    try:
        os.setxattr(apart / "setgid.txt", "user.hopper-test", b"kept")
        xattr = True
    except OSError:
        xattr = False
    (apart / "pasta").mkdir()
    (apart / "pasta" / "filho.txt").write_bytes(b"child")
    (apart / "destino").mkdir()
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        assert world.config.state_directory.stat().st_dev != apart.stat().st_dev
        headers = _signed(world)
        cases = [
            ("rename", "renomear.txt", "renomeado.txt"),
            ("rename", "vinculo-a.txt", "vinculo-c.txt"),
            ("rename", "setgid.txt", "setgid-novo.txt"),
            ("rename", "pasta", "pasta-nova"),
            ("move", "renomeado.txt", "destino/renomeado.txt"),
        ]
        child_before = _identity(apart / "pasta" / "filho.txt")
        for action, source, destination in cases:
            before = _identity(apart / source)
            response = _operate(world, headers, action, apart / source, apart / destination)
            assert response.status_code == 200, response.text
            committed = response.json()["committed"]
            assert committed[0]["separatedLinks"] == 0
            assert not (apart / source).exists()
            assert _identity(apart / destination) == before, (action, source)
        assert _identity(apart / "vinculo-b.txt")[1:3] == (_identity(apart / "vinculo-c.txt")[1], 2)
        assert stat.S_IMODE((apart / "setgid-novo.txt").stat().st_mode) == 0o2664
        if xattr:
            assert os.getxattr(apart / "setgid-novo.txt", "user.hopper-test") == b"kept"
        assert _identity(apart / "pasta-nova" / "filho.txt") == child_before
        # No copy went through the trash.
        assert world.client.get("/api/trash").json()["entries"] == []
        assert not list(apart.glob(".hopper-stage-*"))


def test_cross_filesystem_move_separates_a_link_reports_it_and_refuses_set_id(tmp_path, apart) -> None:
    """HF-FILE-002: only a move between filesystems copies, reproduces metadata, and reports separation."""
    other_parent = _other_filesystem(apart)
    if other_parent is None:
        pytest.skip("no second filesystem apart from the fixture")
    other = Path(tempfile.mkdtemp(prefix="hf-other-", dir=other_parent))
    try:
        (apart / "par-a.txt").write_bytes(b"pair")
        os.chmod(apart / "par-a.txt", 0o600)
        os.link(apart / "par-a.txt", apart / "par-b.txt")
        (apart / "setgid.txt").write_bytes(b"set-id")
        os.chmod(apart / "setgid.txt", 0o2664)
        with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            headers = _signed(world)
            before = (apart / "par-a.txt").stat()
            moved = _operate(world, headers, "move", apart / "par-a.txt", other / "par-a.txt")
            assert moved.status_code == 200, moved.text
            assert moved.json()["committed"][0]["separatedLinks"] == 1
            after = (other / "par-a.txt").stat()
            assert after.st_ino != before.st_ino and after.st_nlink == 1
            assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (before.st_uid, before.st_gid, 0o600)
            assert (apart / "par-b.txt").stat().st_ino == before.st_ino
            assert (apart / "par-b.txt").stat().st_nlink == 1

            refused = _operate(world, headers, "move", apart / "setgid.txt", other / "setgid.txt")
            assert refused.status_code == 207, refused.text
            assert refused.json()["uncommitted"][0]["error"] == "unsupported_metadata"
            assert (apart / "setgid.txt").read_bytes() == b"set-id"
            assert not (other / "setgid.txt").exists()
            renamed = _operate(world, headers, "rename", apart / "setgid.txt", apart / "setgid-2.txt")
            assert renamed.status_code == 200, renamed.text
    finally:
        shutil.rmtree(other, ignore_errors=True)


def test_in_place_move_transfers_marks_rewrites_references_and_recovers(tmp_path, apart, monkeypatch) -> None:
    """HF-FILE-002: the rename path keeps the journaled plan, marks, and recovery."""
    import hopper_files.corpus

    from hopper_files.trash import TrashCrash

    monkeypatch.setattr(hopper_files.corpus, "CORPUS_START", str(apart)[1:])
    (apart / "attachments").mkdir()
    (apart / "arquivo").mkdir()
    (apart / "attachments" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
    (apart / "nota.md").write_text("![a](attachments/a.png)\n", encoding="utf-8")
    (apart / "marcada.txt").write_bytes(b"marked")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _signed(world)
        state = world.client.get("/api/state").json()
        replacement = {key: value for key, value in state.items() if key != "stateRevision"}
        replacement["items"] = [{
            "path": str(apart / "marcada.txt"), "labelIds": ["azul"], "favorite": True,
            "emoji": None, "inode": None, "device": None,
        }]
        saved = world.client.put("/api/state", json={"baseRevision": state["stateRevision"], **replacement}, headers=headers)
        assert saved.status_code == 200, saved.text

        before = _identity(apart / "marcada.txt")
        response = _operate(world, headers, "rename", apart / "marcada.txt", apart / "marcada-2.txt")
        assert response.status_code == 200, response.text
        assert _identity(apart / "marcada-2.txt") == before
        items = {item["path"]: item for item in world.client.get("/api/state").json()["items"]}
        assert str(apart / "marcada.txt") not in items
        assert items[str(apart / "marcada-2.txt")]["favorite"] is True
        assert items[str(apart / "marcada-2.txt")]["labelIds"] == ["azul"]

        image = _identity(apart / "attachments" / "a.png")
        response = _operate(world, headers, "move", apart / "attachments" / "a.png", apart / "arquivo" / "a.png")
        assert response.status_code == 200, response.text
        assert _identity(apart / "arquivo" / "a.png") == image
        assert (apart / "nota.md").read_text(encoding="utf-8") == "![a](arquivo/a.png)\n"

        runtime = world.client.app.state.runtime
        catalog = build_catalog(world.config, strict=False)
        issued = world.client.post("/api/files/token", headers=headers)
        token = issued.json()["operationToken"]
        note = _identity(apart / "nota.md")
        runtime.trash.fail_at.add("move_after_rename")
        with pytest.raises(TrashCrash):
            with runtime.images.lock():
                runtime.operations.submit(
                    token,
                    [{"action": "move", "source": _address(apart / "nota.md"), "destination": _address(apart / "arquivo" / "nota.md")}],
                    catalog,
                    world.clock.now(),
                )
        runtime.trash.fail_at.clear()
        runtime.trash.recover(catalog, world.clock.now())
        runtime.images.recover_moves(catalog, world.clock.now(), runtime.operations, runtime.buffers)
        runtime.operations.recover(catalog, world.clock.now())
        status = world.client.get("/api/files/" + token, headers=headers)
        assert status.json()["status"] == "completed", status.text
        # The rewritten note is published by an atomic save (HF-SAVE-002).
        assert _identity(apart / "arquivo" / "nota.md")[0] == note[0]
        assert not (apart / "nota.md").exists()
        assert (apart / "arquivo" / "nota.md").read_text(encoding="utf-8") == "![a](a.png)\n"
        assert runtime.trash._pending_ids() == set()


def test_cross_filesystem_move_recovers_after_trash_publication_crash(tmp_path, apart) -> None:
    """HF-FILE-002: the copy path keeps its journaled recovery and link report."""
    from hopper_files.trash import TrashCrash

    other_parent = _other_filesystem(apart)
    if other_parent is None:
        pytest.skip("no second filesystem apart from the fixture")
    other = Path(tempfile.mkdtemp(prefix="hf-other-", dir=other_parent))
    try:
        (apart / "par-a.txt").write_bytes(b"pair")
        os.link(apart / "par-a.txt", apart / "par-b.txt")
        with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            headers = _signed(world)
            runtime = world.client.app.state.runtime
            catalog = build_catalog(world.config, strict=False)
            issued = world.client.post("/api/files/token", headers=headers)
            token = issued.json()["operationToken"]
            runtime.trash.fail_at.add("delete_after_publish")
            with pytest.raises(TrashCrash):
                with runtime.images.lock():
                    runtime.operations.submit(
                        token,
                        [{"action": "move", "source": _address(apart / "par-a.txt"), "destination": _address(other / "par-a.txt")}],
                        catalog,
                        world.clock.now(),
                    )
            runtime.trash.fail_at.clear()
            runtime.trash.recover(catalog, world.clock.now())
            runtime.images.recover_moves(catalog, world.clock.now(), runtime.operations, runtime.buffers)
            runtime.operations.recover(catalog, world.clock.now())
            status = world.client.get("/api/files/" + token, headers=headers)
            assert status.json()["status"] == "completed", status.text
            assert status.json()["committed"][0]["separatedLinks"] == 1
            assert (other / "par-a.txt").read_bytes() == b"pair"
            assert not (apart / "par-a.txt").exists()
            assert (apart / "par-b.txt").stat().st_nlink == 1
    finally:
        shutil.rmtree(other, ignore_errors=True)
