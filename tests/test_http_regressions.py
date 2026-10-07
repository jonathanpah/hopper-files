from __future__ import annotations

import base64
import errno
import hashlib
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

import hopper_files.editor as editor_module
import hopper_files.images as images_module
import hopper_files.roots as roots_module
from hopper_files.roots import build_catalog
from hopper_files.trash import TrashCrash
from conftest import CLOCK_START, PASSWORD, ManualClock, boot, D, DOCUMENTS, ROOT, doc


def _headers(world):
    login = world.post_login(PASSWORD)
    assert login.status_code == 200
    return {"origin": world.config.origin, "x-csrf-token": login.json()["csrfToken"]}


def _operation_token(world, headers):
    response = world.client.post("/api/files/token", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["operationToken"]


def _mutate(world, headers, action):
    response = world.client.post(
        "/api/files",
        headers=headers,
        json={"operationToken": _operation_token(world, headers), "actions": [action]},
    )
    return response


def _upload(world, headers, payload, content):
    import json

    manifest = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return world.client.post(
        "/api/files/upload",
        headers={
            **headers,
            "content-type": "application/octet-stream",
            "x-hopper-operation-token": _operation_token(world, headers),
            "x-hopper-upload-manifest": manifest,
        },
        content=content,
    )


@pytest.mark.parametrize("kind", ["create", "extract"])
def test_zip_temporary_name_collision_does_not_replace_external_file(tmp_path, monkeypatch, kind) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        source = root / "source.txt"
        source.write_bytes(b"zip source")
        archive_path = root / "source.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("member.txt", b"archive member")
        temporary = root / (".hopper-stage-" + "a" * 32 + ".tmp")
        temporary.write_bytes(b"foreign temporary marker")
        temporary.chmod(0o640)
        before = (temporary.read_bytes(), temporary.stat().st_ino, temporary.stat().st_uid, temporary.stat().st_gid, stat.S_IMODE(temporary.stat().st_mode))
        headers = _headers(world)
        action = (
            {
                "action": "zip",
                "sources": [{"rootId": ROOT, "path": doc("source.txt")}],
                "destination": {"rootId": ROOT, "path": doc("created.zip")},
            }
            if kind == "create"
            else {
                "action": "extract",
                "source": {"rootId": ROOT, "path": doc("source.zip")},
                "destination": {"rootId": ROOT, "path": D},
            }
        )
        monkeypatch.setattr(roots_module.secrets, "token_hex", lambda length: "a" * (length * 2))
        result = _mutate(world, headers, action)

    assert result.status_code >= 400
    assert temporary.exists()
    after = temporary.stat()
    assert (temporary.read_bytes(), after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == before
    assert not (root / "created.zip").exists()
    assert not (root / "member.txt").exists()
    assert source.read_bytes() == b"zip source"
    assert list(root.glob(".hopper-stage-*.tmp")) == [temporary]


def test_same_size_same_mtime_replacement_conflicts_and_keeps_replacement(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        target = root / "same-stat.txt"
        target.write_bytes(b"first")
        headers = _headers(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc(target.name)})
        assert loaded.status_code == 200
        original = target.stat()
        replacement = root / "replacement.tmp"
        replacement.write_bytes(b"other")
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, target)
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        current = target.stat()
        assert current.st_size == original.st_size
        assert current.st_mtime_ns == original.st_mtime_ns
        assert current.st_ino != original.st_ino

        refused = world.client.put(
            "/api/file",
            params={"rootId": ROOT, "path": doc(target.name)},
            headers=headers,
            json={"baseVersion": loaded.json()["version"], "content": "third"},
        )

    assert refused.status_code == 409
    assert refused.json()["current"]["content"] == "other"
    assert target.read_bytes() == b"other"


@pytest.mark.parametrize("fault", ["listxattr", "getxattr"])
def test_unreadable_access_metadata_fails_without_rewriting_file(tmp_path, monkeypatch, fault) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        target = root / "access-metadata.txt"
        target.write_bytes(b"protected bytes")
        target.chmod(0o600)
        access_acl = struct.pack("<I", 2) + b"".join(
            struct.pack("<HHI", *entry)
            for entry in [
                (1, 6, 0xFFFFFFFF),
                (2, 4, os.geteuid()),
                (4, 4, 0xFFFFFFFF),
                (16, 4, 0xFFFFFFFF),
                (32, 0, 0xFFFFFFFF),
            ]
        )
        if fault == "getxattr":
            try:
                os.setxattr(target, "system.posix_acl_access", access_acl)
            except OSError as exc:
                pytest.skip(f"POSIX access ACL unavailable: {exc}")
        before = target.read_bytes()
        before_info = target.stat()
        before_acl = os.getxattr(target, "system.posix_acl_access") if fault == "getxattr" else None
        headers = _headers(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc(target.name)})
        assert loaded.status_code == 200
        real = getattr(editor_module.os, fault)

        def deny_access_metadata(value, name=None, **kwargs):
            is_target = isinstance(value, int) and (
                os.fstat(value).st_dev,
                os.fstat(value).st_ino,
            ) == (before_info.st_dev, before_info.st_ino)
            if is_target and (fault == "listxattr" or name == "system.posix_acl_access"):
                raise OSError(errno.EACCES, "synthetic metadata read denied")
            return real(value, name, **kwargs) if name is not None else real(value, **kwargs)

        monkeypatch.setattr(editor_module.os, fault, deny_access_metadata)
        refused = world.client.put(
            "/api/file",
            params={"rootId": ROOT, "path": doc(target.name)},
            headers=headers,
            json={"baseVersion": loaded.json()["version"], "content": "replacement bytes"},
        )
        monkeypatch.undo()

    if fault == "listxattr":
        # The HF-NAV-007 rule cannot prove the attributes, so the file is not editable.
        assert refused.status_code == 403
        assert refused.json() == {"error": "not_editable", "reason": "extended_attributes"}
    else:
        assert refused.status_code == 409
        assert refused.json() == {"error": "metadata_unsupported"}
    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(before_info.st_mode)
    if before_acl is not None:
        assert os.getxattr(target, "system.posix_acl_access") == before_acl
    assert not list(root.glob(".hopper-stage-*.tmp"))


def test_successful_editor_save_keeps_explicit_private_mode_0600(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        target = root / "private.txt"
        target.write_bytes(b"before")
        target.chmod(0o600)
        headers = _headers(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc(target.name)})
        saved = world.client.put(
            "/api/file",
            params={"rootId": ROOT, "path": doc(target.name)},
            headers=headers,
            json={"baseVersion": loaded.json()["version"], "content": "after"},
        )

    assert saved.status_code == 200
    assert target.read_bytes() == b"after"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "system.posix_acl_access" not in os.listxattr(target)


@pytest.mark.parametrize("transition", ["move", "delete_restore", "delete_recovery_restore"])
def test_stale_http_state_cannot_overwrite_move_delete_restore_or_recovery(tmp_path, transition) -> None:
    root = DOCUMENTS
    marked = root / "marked.txt"
    marked.write_bytes(b"stable payload")
    (root / "archive").mkdir()

    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _headers(world)
        initial = world.client.get("/api/state")
        assert initial.status_code == 200
        before = initial.json()
        document = {key: value for key, value in before.items() if key != "stateRevision"}
        info = marked.stat()
        document["items"] = [{
            "path": "/" + doc("marked.txt"),
            "labelIds": ["azul"],
            "favorite": True,
            "emoji": "📌",
            "inode": info.st_ino,
            "device": info.st_dev,
        }]
        seeded = world.client.put(
            "/api/state", headers=headers,
            json={"baseRevision": 0, **document},
        )
        assert seeded.status_code == 200
        stale = dict(document)
        stale["items"] = [{**document["items"][0], "favorite": False, "labelIds": ["vermelho"]}]
        stale_payload = {"baseRevision": seeded.json()["stateRevision"], **stale}

        deleted_id = None
        if transition == "move":
            moved = _mutate(world, headers, {
                "action": "move",
                "source": {"rootId": ROOT, "path": doc("marked.txt")},
                "destination": {"rootId": ROOT, "path": doc("archive/marked.txt")},
            })
            assert moved.status_code == 200, moved.text
            expected_item_path = "/" + doc("archive/marked.txt")
        else:
            if transition == "delete_recovery_restore":
                trash = world.client.app.state.runtime.trash
                trash.fail_at.add("delete_after_publish")
                with pytest.raises(TrashCrash):
                    trash.delete(
                        build_catalog(world.config, strict=False),
                        ROOT,
                        doc("marked.txt"),
                        world.clock.now(),
                    )
                trash.fail_at.clear()
                catalog = build_catalog(world.config, strict=False)
                trash.recover(catalog, world.clock.now())
                entries = world.client.get("/api/trash")
                assert entries.status_code == 200
                deleted_id = next(item["id"] for item in entries.json()["entries"] if item.get("sourcePath") == doc("marked.txt"))
            else:
                deleted = world.client.post(
                    "/api/trash", headers=headers,
                    json={"action": "delete", "source": {"rootId": ROOT, "path": doc("marked.txt")}},
                )
                assert deleted.status_code == 201, deleted.text
                deleted_id = deleted.json()["id"]

        state_after_transition = world.client.get("/api/state").json()
        stale_write = world.client.put("/api/state", headers=headers, json=stale_payload)
        state_after_stale = world.client.get("/api/state").json()
        assert stale_write.status_code == 409
        assert state_after_stale == state_after_transition

        if transition != "move":
            restored = world.client.post("/api/trash", headers=headers, json={"action": "restore", "id": deleted_id})
            assert restored.status_code == 200, restored.text
            after_restore = world.client.get("/api/state").json()
            stale_again = world.client.put("/api/state", headers=headers, json=stale_payload)
            assert stale_again.status_code == 409
            assert world.client.get("/api/state").json() == after_restore
            expected_item_path = "/" + doc("marked.txt")

        final = world.client.get("/api/state").json()

    assert final["stateRevision"] >= 2
    assert final["items"][0]["path"] == expected_item_path
    assert final["items"][0]["favorite"] is True
    assert final["items"][0]["labelIds"] == ["azul"]
    if transition == "move":
        assert (root / "archive" / "marked.txt").read_bytes() == b"stable payload"
        assert not marked.exists()
    else:
        assert marked.read_bytes() == b"stable payload"


def test_move_rewrites_every_corpus_reference_and_waits_for_open_buffers(tmp_path) -> None:
    """HF-FILE-002 with HF-NAV-004: every writable note is rewritten; others are not."""
    import concurrent.futures
    import time

    from hopper_files.buffers import BUFFER_PREPARE_SECONDS
    from hopper_files.image_refs import ImageIdentity, resolve_destination

    notes_a = DOCUMENTS / "notes-a"
    notes_b = DOCUMENTS / "notes-b"
    assets = DOCUMENTS / "assets"
    frozen = DOCUMENTS / "frozen"
    for path in (notes_a, notes_b, assets / "archive", frozen):
        path.mkdir(parents=True)
    image = assets / "photo 1%.png"
    image_bytes = b"\x89PNG\r\n\x1a\nshared-synthetic-image"
    image.write_bytes(image_bytes)
    note_a = notes_a / "a.md"
    note_b = notes_b / "b.md"
    note_a.write_bytes(b"![A](../assets/photo%201%25.png)\n")
    note_b.write_bytes(b"![B](../assets/photo%201%25.png)\n")
    unrelated = notes_b / "unrelated.md"
    unrelated_bytes = b"unchanged bytes\x00\n"
    unrelated.write_bytes(unrelated_bytes)
    outside_corpus = frozen / "frozen.md"
    outside_corpus.write_bytes(b"![frozen](../assets/photo%201%25.png)\n")
    frozen.chmod(0o555)
    blocked_note = notes_b / "unreadable.md"
    blocked_note.write_bytes(b"# unreadable\n")
    blocked_note.chmod(0)
    try:
        with boot(tmp_path / "main-app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            headers = _headers(world)
            action = {
                "action": "move",
                "source": {"rootId": ROOT, "path": doc("assets/photo 1%.png")},
                "destination": {"rootId": ROOT, "path": doc("assets/archive/photo 1%.png")},
            }
            # A corpus note that cannot be read makes the reference scan incomplete.
            unreadable = _mutate(world, headers, action)
            assert unreadable.status_code == 409, unreadable.text
            blocked_note.chmod(0o600)

            loaded_a = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("notes-a/a.md")}, headers=headers)
            loaded_b = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("notes-b/b.md")}, headers=headers)
            assert loaded_a.status_code == loaded_b.status_code == 200
            documents = [
                {"rootId": ROOT, "path": doc("notes-a/a.md"), "version": loaded_a.json()["version"], "dirty": False},
                {"rootId": ROOT, "path": doc("notes-b/b.md"), "version": loaded_b.json()["version"], "dirty": False},
            ]
            runtime = world.client.app.state.runtime
            now = world.clock.now()
            expires = now + 600
            dirty_session = "e" * 64
            runtime.buffers.sync(dirty_session, expires, [{**documents[0], "dirty": True}], [], now)
            image_info = image.stat()
            before_a = note_a.read_bytes()
            before_b = note_b.read_bytes()

            rejected = _mutate(world, headers, action)
            assert rejected.status_code == 409
            assert image.read_bytes() == image_bytes
            assert note_a.read_bytes() == before_a and note_b.read_bytes() == before_b

            runtime.buffers.sync(dirty_session, expires, [], [], world.clock.now())
            session_a, session_b = "a" * 64, "b" * 64
            runtime.buffers.sync(session_a, expires, [documents[0]], [], world.clock.now())
            runtime.buffers.sync(session_b, expires, [documents[1]], [], world.clock.now())
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(_mutate, world, headers, action)
                acknowledged: set[str] = set()
                deadline = time.monotonic() + BUFFER_PREPARE_SECONDS + 1
                while time.monotonic() < deadline and not pending.done():
                    for session_id, document in ((session_a, documents[0]), (session_b, documents[1])):
                        if session_id in acknowledged:
                            continue
                        sync = runtime.buffers.sync(session_id, expires, [document], [], world.clock.now())
                        hold = next((item for item in sync["commands"] if item["type"] == "hold"), None)
                        if hold is not None:
                            ack = {"moveId": hold["id"], "accepted": True, "documents": [document]}
                            runtime.buffers.sync(session_id, expires, [document], [ack], world.clock.now())
                            acknowledged.add(session_id)
                    if not pending.done():
                        time.sleep(0.01)
                moved = pending.result(timeout=BUFFER_PREPARE_SECONDS + 1)

            assert moved.status_code == 200, moved.text
            assert acknowledged == {session_a, session_b}
            destination = assets / "archive" / image.name
            assert not image.exists()
            assert destination.read_bytes() == image_bytes
            assert (destination.stat().st_dev, destination.stat().st_ino) == (image_info.st_dev, image_info.st_ino)
            assert note_a.read_bytes() == b"![A](../assets/archive/photo%201%25.png)\n"
            assert note_b.read_bytes() == b"![B](../assets/archive/photo%201%25.png)\n"
            assert unrelated.read_bytes() == unrelated_bytes
            # A note in a directory the account cannot write is outside the corpus.
            assert outside_corpus.read_bytes() == b"![frozen](../assets/photo%201%25.png)\n"
            catalog = build_catalog(world.config, strict=False)
            assert resolve_destination(catalog, ROOT, doc("notes-a/a.md"), "../assets/archive/photo%201%25.png") == ImageIdentity(
                ROOT, doc("assets/archive/photo 1%.png"),
            )
    finally:
        frozen.chmod(0o755)
        blocked_note.chmod(0o600)


@pytest.mark.parametrize("incomplete_scan", ["malformed", "unreadable", "traversal_budget"])
def test_inconclusive_collection_preserves_image_after_http_save(tmp_path, monkeypatch, incomplete_scan) -> None:
    root = DOCUMENTS
    attachments = root / "attachments"
    attachments.mkdir()
    image = attachments / "kept.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nretained")
    note = root / "note.md"
    note.write_text("![image](attachments/kept.png)\n", encoding="utf-8")

    if incomplete_scan == "malformed":
        # An unparseable note that names the candidate keeps it (HF-IMG-005).
        (root / "malformed.md").write_text("```markdown\n![](attachments/kept.png)\n", encoding="utf-8")
    elif incomplete_scan == "unreadable":
        (root / "unreadable.md").write_text("# private\n", encoding="utf-8")
        (root / "unreadable.md").chmod(0)

    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _headers(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("note.md")})
        assert loaded.status_code == 200, loaded.text

        if incomplete_scan == "traversal_budget":
            import hopper_files.corpus as corpus_module

            monkeypatch.setattr(corpus_module, "CORPUS_TIME_LIMIT_SECONDS", 0.0)

        saved = world.client.put(
            "/api/file",
            params={"rootId": ROOT, "path": doc("note.md")},
            headers=headers,
            json={"baseVersion": loaded.json()["version"], "content": "# Reference removed\n"},
        )

    assert saved.status_code == 200, saved.text
    assert saved.json()["imageCollection"] == {"removed": 0, "inconclusive": True}
    assert note.read_text(encoding="utf-8") == "# Reference removed\n"
    assert image.read_bytes() == b"\x89PNG\r\n\x1a\nretained"
