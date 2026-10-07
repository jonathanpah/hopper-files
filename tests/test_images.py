from __future__ import annotations

import os
import json
import time
import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pathlib import Path
from starlette.requests import Request

import hopper_files.api.images as images_api
from hopper_files.api.request_body import CONTROL_BODY_MAX_BYTES

from hopper_files.image_refs import (
    ImageIdentity,
    ReferenceScanError,
    _parse_markdown,
    image_destination_spans,
    resolve_destination,
)
from hopper_files.roots import build_catalog
from hopper_files.trash import TrashCrash

from conftest import CLOCK_START, PASSWORD, ManualClock, boot, D, DOCUMENTS, ROOT, doc


def _signed(world):
    login = world.post_login(PASSWORD)
    assert login.status_code == 200, login.text
    return {"origin": world.config.origin, "x-csrf-token": login.json()["csrfToken"]}


def _root(world) -> Path:
    return DOCUMENTS


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_pending_resolution_bounds_stream_without_trusting_content_length(
    monkeypatch, declared_length: str | None
) -> None:
    maximum = CONTROL_BODY_MAX_BYTES
    chunk_size = 4096
    total_size = maximum * 4
    consumed = 0

    async def receive() -> dict[str, object]:
        nonlocal consumed
        if consumed >= total_size:
            return {"type": "http.request", "body": b"", "more_body": False}
        size = min(chunk_size, total_size - consumed)
        consumed += size
        return {"type": "http.request", "body": b"x" * size, "more_body": consumed < total_size}

    headers = [(b"content-type", b"application/json")]
    if declared_length is not None:
        headers.append((b"content-length", declared_length.encode("ascii")))
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/images/pending",
        "raw_path": b"/api/images/pending",
        "query_string": b"",
        "headers": headers,
        "server": ("example.test", 80),
        "client": ("127.0.0.1", 40000),
        "root_path": "",
    }, receive)
    monkeypatch.setattr(images_api, "_mutation_authorized", lambda _request: True)
    response = asyncio.run(images_api.post_resolve_pending(request))

    assert response.status_code == 413
    assert response.body == b'{"error":"limit_exceeded"}'
    assert consumed <= maximum + chunk_size
    assert consumed < total_size


def test_pending_resolution_accepts_exact_control_body_boundary(tmp_path) -> None:
    maximum = CONTROL_BODY_MAX_BYTES
    prefix = ('{"rootId": "fs", "path": "%s"}' % D).encode()
    body = prefix + b" " * (maximum - len(prefix))
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        response = world.client.post(
            "/api/images/pending",
            content=body,
            headers={"content-type": "application/json", **csrf},
        )

    assert len(body) == maximum
    assert response.status_code == 200
    assert response.json() == {"resolved": False}


def _move(world, headers, source, destination):
    issued = world.client.post("/api/files/token", headers=headers)
    assert issued.status_code == 201, issued.text
    return world.client.post(
        "/api/files",
        headers=headers,
        json={
            "operationToken": issued.json()["operationToken"],
            "actions": [{
                "action": "move",
                "source": source,
                "destination": destination,
            }],
        },
    )


def test_image_scanner_resolves_inline_reference_and_ignores_code(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "attachments").mkdir()
        (root / "attachments" / "a b.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        (root / "note.md").write_text(
            "![inline](attachments/a%20b.png)\n"
            "![ref][pic]\n"
            "[pic]: <attachments/a%20b.png>\n"
            "`![](attachments/missing.png)`\n"
            "```md\n![](attachments/missing.png)\n```\n",
            encoding="utf-8",
        )
        catalog = build_catalog(world.config, strict=False)
        actual = _parse_markdown(catalog, ROOT, doc("note.md"), (root / "note.md").read_bytes())
        assert actual == frozenset({ImageIdentity(ROOT, doc("attachments/a b.png"))})


def test_relative_destination_with_parent_components_maps_to_absolute_identity(tmp_path) -> None:
    """HF-IMG-002/HF-IMG-003: `..` resolves against the note directory to an absolute path."""
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        target = DOCUMENTS / "second" / "attachments" / "photo.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        catalog = build_catalog(world.config, strict=False)
        resolved = resolve_destination(
            catalog, ROOT, doc("first/notes/today.md"), "../../second/attachments/photo.png"
        )
        assert resolved == ImageIdentity(ROOT, doc("second/attachments/photo.png"))


def test_authenticated_http_image_reference_resolution_uses_absolute_paths(tmp_path) -> None:
    workspace = DOCUMENTS / "workspace"
    archive = DOCUMENTS / "archive"
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        note = workspace / "notes" / "today.md"
        note.parent.mkdir(parents=True)
        note.write_text("synthetic note", encoding="utf-8")
        same_image = workspace / "attachments" / "same.png"
        same_image.parent.mkdir()
        same_image.write_bytes(b"synthetic raster")
        other_image = archive / "attachments" / "other.png"
        other_image.parent.mkdir(parents=True)
        other_image.write_bytes(b"synthetic raster")
        (note.parent / "alias.png").symlink_to(other_image)
        params = {"rootId": ROOT, "path": doc("workspace/notes/today.md")}

        denied = world.client.get("/api/images/resolve", params={**params, "reference": "../attachments/same.png"})
        assert denied.status_code == 401
        _signed(world)

        same = world.client.get("/api/images/resolve", params={**params, "reference": "../attachments/same.png"})
        assert same.status_code == 200
        assert same.json() == {"resolved": True, "rootId": ROOT, "path": doc("workspace/attachments/same.png")}

        other = world.client.get("/api/images/resolve", params={**params, "reference": "../../archive/attachments/other.png"})
        assert other.status_code == 200
        assert other.json() == {"resolved": True, "rootId": ROOT, "path": doc("archive/attachments/other.png")}

        external = world.client.get("/api/images/resolve", params={**params, "reference": "https://images.example.test/secret.png"})
        assert external.status_code == 200
        assert external.json() == {"resolved": False}

        for reference in (
            "/etc/passwd",
            "%252e%252e/escape.png",
            "../" * 64 + "escape.png",
            "alias.png",
        ):
            rejected = world.client.get("/api/images/resolve", params={**params, "reference": reference})
            assert rejected.status_code == 422, (reference, rejected.text)
            assert rejected.json()["error"] == "invalid_reference"

        invalid_root = world.client.get(
            "/api/images/resolve", params={"rootId": "system", "path": doc("workspace/notes/today.md"), "reference": "../attachments/same.png"}
        )
        assert invalid_root.status_code == 404


def test_unsafe_double_encoded_or_outside_reference_is_inconclusive(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        catalog = build_catalog(world.config, strict=False)
        for destination in ("%252e%252e/escape.png", "../" * 64 + "etc/passwd", "/absolute.png"):
            try:
                resolve_destination(catalog, ROOT, doc("note.md"), destination)
            except ReferenceScanError:
                pass
            else:
                raise AssertionError(f"unsafe reference accepted: {destination}")


def test_missing_image_identity_is_valid_but_dangling_symlink_is_inconclusive(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        catalog = build_catalog(world.config, strict=False)
        missing = _parse_markdown(catalog, ROOT, doc("note.md"), b"![missing](future/photo.png)\n")
        assert missing == frozenset({ImageIdentity(ROOT, doc("future/photo.png"))})
        (root / "alias").symlink_to(root / "missing-directory", target_is_directory=True)
        try:
            _parse_markdown(catalog, ROOT, doc("note.md"), b"![unsafe](alias/photo.png)\n")
        except ReferenceScanError:
            pass
        else:
            raise AssertionError("a dangling symlink reference was treated as a missing image")


def test_rewrite_spans_include_reference_definitions_and_reject_shared_link_labels(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "picture.png").write_bytes(b"synthetic")
        (root / "note.md").write_text(
            "![photo][pic]\n[pic]: <picture.png>\n",
            encoding="utf-8",
        )
        catalog = build_catalog(world.config, strict=False)
        spans = image_destination_spans(catalog, ROOT, doc("note.md"), (root / "note.md").read_text())
        assert len(spans) == 1
        assert spans[0].destination == "picture.png"
        shared = "![photo][pic]\n[link][pic]\n[pic]: <picture.png>\n"
        try:
            image_destination_spans(catalog, ROOT, doc("note.md"), shared)
        except ReferenceScanError:
            pass
        else:
            raise AssertionError("shared image/link reference definition was accepted for rewrite")


def test_angle_destination_with_title_scans_and_rewrites_preserving_title(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "attachments").mkdir()
        image = root / "attachments" / "a b.png"
        image.write_bytes(b"synthetic")
        note = root / "note.md"
        original = '![alt](<attachments/a%20b.png> "caption")\n'
        note.write_text(original, encoding="utf-8")
        catalog = build_catalog(world.config, strict=False)
        scanned = _parse_markdown(catalog, ROOT, doc("note.md"), original.encode())
        assert scanned == frozenset({ImageIdentity(ROOT, doc("attachments/a b.png"))})
        spans = image_destination_spans(catalog, ROOT, doc("note.md"), original)
        assert len(spans) == 1
        assert spans[0].destination == "attachments/a%20b.png"

        (root / "archive").mkdir()
        response = _move(
            world,
            _signed(world),
            {"rootId": ROOT, "path": doc("attachments/a b.png")},
            {"rootId": ROOT, "path": doc("archive/a b.png")},
        )
        assert response.status_code == 200, response.text
        assert note.read_text(encoding="utf-8") == '![alt](<archive/a%20b.png> "caption")\n'


def test_file_move_ack_is_served_on_the_same_asgi_event_loop(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        note = root / "note.md"
        note.write_text("# Async buffer\n", encoding="utf-8")
        headers = _signed(world)
        document_response = world.client.get(
            "/api/file", params={"rootId": ROOT, "path": doc("note.md")}
        )
        assert document_response.status_code == 200, document_response.text
        document = {
            "rootId": ROOT, "path": doc("note.md"),
            "version": document_response.json()["version"],
            "dirty": False,
        }
        token_response = world.client.post("/api/files/token", headers=headers)
        assert token_response.status_code == 201, token_response.text
        payload = {
            "operationToken": token_response.json()["operationToken"],
            "actions": [{
                "action": "move",
                "source": {"rootId": ROOT, "path": doc("note.md")},
                "destination": {"rootId": ROOT, "path": doc("archive/note.md")},
            }],
        }

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=world.client.app),
                base_url=world.config.origin,
                trust_env=False,
                cookies=dict(world.client.cookies),
            ) as client:
                registered = await client.post(
                    "/api/buffers",
                    headers=headers,
                    json={"documents": [document], "acknowledgements": []},
                )
                assert registered.status_code == 200, registered.text
                move_task = asyncio.create_task(
                    client.post("/api/files", headers=headers, json=payload)
                )
                deadline = time.monotonic() + 2
                hold = None
                latest = None
                while time.monotonic() < deadline and hold is None:
                    latest = await asyncio.wait_for(
                        client.post(
                            "/api/buffers",
                            headers=headers,
                            json={"documents": [document], "acknowledgements": []},
                        ),
                        timeout=1,
                    )
                    assert latest.status_code == 200, latest.text
                    hold = next(
                        (command for command in latest.json()["commands"] if command["type"] == "hold"),
                        None,
                    )
                    if hold is None:
                        await asyncio.sleep(0.01)
                assert hold is not None, latest.text if latest is not None else "move produced no buffer hold"
                acked = await asyncio.wait_for(
                    client.post(
                        "/api/buffers",
                        headers=headers,
                        json={
                            "documents": [document],
                            "acknowledgements": [{
                                "moveId": hold["id"],
                                "accepted": True,
                                "documents": [document],
                            }],
                        },
                    ),
                    timeout=1,
                )
                assert acked.status_code == 200, acked.text
                return await asyncio.wait_for(move_task, timeout=3)

        response = asyncio.run(exercise())
        assert response.status_code == 200, response.text
        assert not note.exists()
        assert (root / "archive" / "note.md").read_text(encoding="utf-8") == "# Async buffer\n"


def test_read_only_referenced_markdown_rejects_move_before_image_publication(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "attachments").mkdir()
        (root / "archive").mkdir()
        image = root / "attachments" / "a.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        note = root / "note.md"
        note.write_text("![](attachments/a.png)\n", encoding="utf-8")
        note.chmod(0o400)
        response = _move(
            world,
            _signed(world),
            {"rootId": ROOT, "path": doc("attachments/a.png")},
            {"rootId": ROOT, "path": doc("archive/a.png")},
        )
        assert response.status_code == 403, response.text
        assert response.json()["status"] == "failed"
        assert response.json()["uncommitted"][0]["error"] == "not_editable"
        assert image.read_bytes() == b"\x89PNG\r\n\x1a\nsynthetic"
        assert not (root / "archive" / "a.png").exists()
        assert note.read_text(encoding="utf-8") == "![](attachments/a.png)\n"


@pytest.mark.parametrize("mutation", ["touch", "rewrite_same_bytes"])
def test_external_markdown_revision_change_after_plan_blocks_move_rewrite(
    tmp_path, monkeypatch, mutation: str
) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "attachments").mkdir()
        (root / "archive").mkdir()
        image = root / "attachments" / "a.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        note = root / "note.md"
        original = b"![](attachments/a.png)\n"
        note.write_bytes(original)
        initial = note.stat()
        runtime = world.client.app.state.runtime
        original_move = runtime.trash.move_in_place
        observed = []

        def mutate_after_staging(*args, **kwargs):
            before = note.stat()
            if mutation == "touch":
                os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            else:
                with note.open("r+b") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                after_write = note.stat()
                os.utime(note, ns=(after_write.st_atime_ns, initial.st_mtime_ns + 2_000_000_000))
            after = note.stat()
            observed.append((after.st_mtime_ns, after.st_ctime_ns))
            return original_move(*args, **kwargs)

        monkeypatch.setattr(runtime.trash, "move_in_place", mutate_after_staging)
        response = _move(
            world,
            _signed(world),
            {"rootId": ROOT, "path": doc("attachments/a.png")},
            {"rootId": ROOT, "path": doc("archive/a.png")},
        )

        assert observed and observed[0] != (initial.st_mtime_ns, initial.st_ctime_ns)
        assert response.status_code == 207, response.text
        assert response.json()["status"] == "indeterminate"
        assert response.json()["indeterminate"][0]["error"] == "conflict"
        assert note.read_bytes() == original
        assert not image.exists()
        assert (root / "archive" / "a.png").read_bytes() == b"\x89PNG\r\n\x1a\nsynthetic"


def test_all_rewrites_are_synced_on_destination_filesystem_before_move_publication(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "attachments").mkdir()
        (root / "archive").mkdir()
        (root / "attachments" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nprestage")
        notes = {
            "one.md": "![one](attachments/a.png)\n",
            "two.md": "![two](attachments/a.png)\n",
        }
        for name, content in notes.items():
            (root / name).write_text(content, encoding="utf-8")
        runtime = world.client.app.state.runtime
        original_move = runtime.trash.move_in_place
        observed = []

        def inspect_stages_before_publication(*args, **kwargs):
            observed.extend(
                (path.name, path.read_text(encoding="utf-8"))
                for path in sorted(root.glob(".hopper-stage-*.tmp"))
            )
            assert len(observed) == len(notes)
            assert {content for _name, content in observed} == {
                "![one](archive/a.png)\n",
                "![two](archive/a.png)\n",
            }
            return original_move(*args, **kwargs)

        monkeypatch.setattr(runtime.trash, "move_in_place", inspect_stages_before_publication)
        response = _move(
            world,
            _signed(world),
            {"rootId": ROOT, "path": doc("attachments/a.png")},
            {"rootId": ROOT, "path": doc("archive/a.png")},
        )
        assert response.status_code == 200, response.text
        assert len(observed) == len(notes)
        assert not list(root.glob(".hopper-stage-*.tmp"))
        for name, expected in (("one.md", "![one](archive/a.png)\n"), ("two.md", "![two](archive/a.png)\n")):
            assert (root / name).read_text(encoding="utf-8") == expected


def test_directory_move_stages_rewrite_for_parent_created_by_the_move(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        (root / "shared.png").write_bytes(b"synthetic")
        bundle = root / "bundle"
        bundle.mkdir()
        note = bundle / "note.md"
        note.write_text("![shared](../shared.png)\n", encoding="utf-8")

        response = _move(
            world,
            _signed(world),
            {"rootId": ROOT, "path": doc("bundle")},
            {"rootId": ROOT, "path": doc("archive/bundle")},
        )

        assert response.status_code == 200, response.text
        moved_note = root / "archive" / "bundle" / "note.md"
        assert moved_note.read_text(encoding="utf-8") == "![shared](../../shared.png)\n"
        assert not list(root.glob(".hopper-stage-*.tmp"))


def test_move_recovery_never_rewrites_same_bytes_external_note(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        (root / "attachments").mkdir()
        (root / "attachments" / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nrecovery")
        original = b"![photo](attachments/photo.png)\n"
        source = root / "note.md"
        source.write_bytes(original)
        headers = _signed(world)
        token_response = world.client.post("/api/files/token", headers=headers)
        assert token_response.status_code == 201, token_response.text
        token = token_response.json()["operationToken"]
        runtime = world.client.app.state.runtime
        catalog = build_catalog(world.config, strict=False)
        runtime.trash.fail_at.add("move_after_publication")
        with pytest.raises(TrashCrash):
            with runtime.images.lock():
                runtime.operations.submit(
                    token,
                    [{
                        "action": "move",
                        "source": {"rootId": ROOT, "path": doc("note.md")},
                        "destination": {"rootId": ROOT, "path": doc("archive/note.md")},
                    }],
                    catalog,
                    world.clock.now(),
                )
        runtime.trash.fail_at.clear()
        destination = root / "archive" / "note.md"
        destination.unlink()
        destination.write_bytes(original)
        external_inode = destination.stat().st_ino

        runtime.trash.recover(catalog, world.clock.now())
        runtime.images.recover_moves(catalog, world.clock.now(), runtime.operations, runtime.buffers)
        runtime.operations.recover(catalog, world.clock.now())

        assert destination.read_bytes() == original
        assert destination.stat().st_ino == external_inode
        assert source.exists() is False
        status = world.client.get("/api/files/" + token, headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "indeterminate", json.dumps(status.json(), indent=2)


def test_managed_upload_stays_pending_until_saved_then_last_reference_collects(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        note = root / "note.md"
        note.write_text("# Test\n", encoding="utf-8")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("note.md")})
        assert loaded.status_code == 200

        issued = world.client.post("/api/files/token", headers=headers)
        assert issued.status_code == 201, issued.text
        image_bytes = b"\x89PNG\r\n\x1a\nsynthetic-png-data"
        uploaded = world.client.post(
            "/api/images/upload",
            params={"rootId": ROOT, "path": doc("note.md")},
            content=image_bytes,
            headers={
                **headers,
                "content-type": "application/octet-stream",
                "x-hopper-operation-token": issued.json()["operationToken"],
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        image = uploaded.json()["image"]
        image_path = Path("/" + image["path"])
        assert image_path.read_bytes() == image_bytes
        gallery = world.client.get("/api/images", params={"rootId": ROOT, "path": doc("note.md")})
        assert gallery.status_code == 200, gallery.text
        assert gallery.json()["entries"][0]["pending"] is True

        saved = world.client.put(
            "/api/file",
            params={"rootId": ROOT, "path": doc("note.md")},
            headers=headers,
            json={
                "baseVersion": loaded.json()["version"],
                "content": f"# Test\n\n![synthetic]({image['reference']})\n",
            },
        )
        assert saved.status_code == 200, saved.text
        gallery = world.client.get("/api/images", params={"rootId": ROOT, "path": doc("note.md")})
        assert gallery.json()["entries"][0]["pending"] is False

        removed = world.client.put(
            "/api/file",
            params={"rootId": ROOT, "path": doc("note.md")},
            headers=headers,
            json={"baseVersion": saved.json()["savedVersion"], "content": "# No image\n"},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["imageCollection"] == {"removed": 1, "inconclusive": False}
        assert not image_path.exists()
        trash = world.client.get("/api/trash")
        assert trash.status_code == 200, trash.text
        assert any(item["reason"] == "image_collection" for item in trash.json()["entries"])


@pytest.mark.parametrize(
    ("media_type", "content", "extension"),
    [
        ("image/png", b"\x89PNG\r\n\x1a\nsynthetic", "png"),
        ("image/jpeg", b"\xff\xd8\xffsynthetic", "jpg"),
        ("image/gif", b"GIF89asynthetic", "gif"),
        ("image/webp", b"RIFF\x04\x00\x00\x00WEBPsynthetic", "webp"),
    ],
)
def test_managed_upload_accepts_supported_raster_signatures(
    tmp_path, media_type: str, content: bytes, extension: str
) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "note.md").write_text("# Test\n", encoding="utf-8")
        headers = _signed(world)
        issued = world.client.post("/api/files/token", headers=headers)
        assert issued.status_code == 201, issued.text
        uploaded = world.client.post(
            "/api/images/upload",
            params={"rootId": ROOT, "path": doc("note.md")},
            content=content,
            headers={
                **headers,
                "content-type": media_type,
                "x-hopper-operation-token": issued.json()["operationToken"],
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        image = uploaded.json()["image"]
        assert image["mediaType"] == media_type
        assert image["path"].lower().endswith("." + extension)
        assert image["pending"] is True
        assert (Path("/" + image["path"])).read_bytes() == content


def test_managed_upload_rejects_svg_without_creating_managed_content(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "note.md").write_text("# Test\n", encoding="utf-8")
        headers = _signed(world)
        issued = world.client.post("/api/files/token", headers=headers)
        assert issued.status_code == 201, issued.text
        rejected = world.client.post(
            "/api/images/upload",
            params={"rootId": ROOT, "path": doc("note.md")},
            content=b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            headers={
                **headers,
                "content-type": "image/svg+xml",
                "x-hopper-operation-token": issued.json()["operationToken"],
            },
        )
        assert rejected.status_code == 415, rejected.text
        assert rejected.json()["error"] == "unsupported_image"
        assert not (root / "attachments").exists()


def test_collection_scans_large_markdown_and_resolves_percent_escaped_aliases(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        attachments = root / "attachments"
        attachments.mkdir()
        image = attachments / "flower.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nlarge-scan")
        (root / "note.md").write_text("![note](attachments/flower.png)\n", encoding="utf-8")
        (root / "alias.md").write_text("![alias](./attachments/%66lower.png)\n", encoding="utf-8")
        medium_line = b"m" * (32 * 1024) + b"\n"
        large_line = b"l" * (32 * 1024) + b"\n"
        (root / "medium.md").write_bytes(medium_line * 64)
        (root / "large.md").write_bytes(large_line * 192)
        headers = _signed(world)

        note = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("note.md")})
        assert note.status_code == 200, note.text
        removed_note = world.client.put(
            "/api/file", params={"rootId": ROOT, "path": doc("note.md")},
            headers=headers, json={"baseVersion": note.json()["version"], "content": "# No ref\n"},
        )
        assert removed_note.status_code == 200, removed_note.text
        assert removed_note.json()["imageCollection"] == {"removed": 0, "inconclusive": False}
        assert image.exists()

        alias = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("alias.md")})
        assert alias.status_code == 200, alias.text
        removed_alias = world.client.put(
            "/api/file", params={"rootId": ROOT, "path": doc("alias.md")},
            headers=headers, json={"baseVersion": alias.json()["version"], "content": "# Alias removed\n"},
        )
        assert removed_alias.status_code == 200, removed_alias.text
        assert removed_alias.json()["imageCollection"] == {"removed": 1, "inconclusive": False}
        assert not image.exists()


def test_scanner_limit_failure_preserves_collection_candidate(tmp_path, monkeypatch) -> None:
    from hopper_files import image_refs

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "attachments").mkdir()
        image = root / "attachments" / "protected.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nlimit-test")
        (root / "note.md").write_text("![still referenced](attachments/protected.png)\n", encoding="utf-8")
        # Exercise the contract's scanner-limit failure on a document above 5 MiB.
        (root / "large.md").write_bytes((b"x" * (32 * 1024) + b"\n") * 192)
        headers = _signed(world)
        note = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("note.md")})
        assert note.status_code == 200, note.text
        monkeypatch.setattr(image_refs, "MAX_MARKDOWN_SCAN_BYTES", 5 * 1024 * 1024)
        saved = world.client.put(
            "/api/file", params={"rootId": ROOT, "path": doc("note.md")},
            headers=headers, json={"baseVersion": note.json()["version"], "content": "# Removed\n"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["imageCollection"] == {"removed": 0, "inconclusive": True}
        assert image.exists()


def test_root_generation_change_marks_collection_inconclusive_and_preserves_image(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "attachments").mkdir()
        image = root / "attachments" / "generation.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\ngeneration")
        (root / "note.md").write_text("![image](attachments/generation.png)\n", encoding="utf-8")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("note.md")})
        assert loaded.status_code == 200, loaded.text
        monkeypatch.setattr(world.client.app.state.runtime.images, "_configured_generation_matches", lambda _value: False)
        saved = world.client.put(
            "/api/file", params={"rootId": ROOT, "path": doc("note.md")},
            headers=headers, json={"baseVersion": loaded.json()["version"], "content": "# Removed\n"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["imageCollection"] == {"removed": 0, "inconclusive": True}
        assert image.exists()


def test_moving_image_rewrites_relative_references_in_other_directories(tmp_path) -> None:
    first = DOCUMENTS / "first"
    second = DOCUMENTS / "second"
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        (second / "attachments").mkdir(parents=True)
        (second / "pics").mkdir()
        first.mkdir()
        (second / "attachments" / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        note = first / "note.md"
        note.write_text("![photo](../second/attachments/photo.png)\n", encoding="utf-8")
        response = _move(
            world,
            _signed(world),
            {"rootId": ROOT, "path": doc("second/attachments/photo.png")},
            {"rootId": ROOT, "path": doc("second/pics/photo.png")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["committed"][0]["status"] == "committed"
        assert not (second / "attachments" / "photo.png").exists()
        assert (second / "pics" / "photo.png").read_bytes().startswith(b"\x89PNG")
        assert note.read_text(encoding="utf-8") == "![photo](../second/pics/photo.png)\n"


def test_undetermined_note_blocks_only_moves_of_files_it_names(tmp_path) -> None:
    """A note whose references cannot be parsed blocks a move only when it names the moved file."""
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        (DOCUMENTS / "attachments").mkdir()
        (DOCUMENTS / "archive").mkdir()
        (DOCUMENTS / "attachments" / "named.png").write_bytes(b"\x89PNG\r\n\x1a\nnamed")
        (DOCUMENTS / "attachments" / "other.png").write_bytes(b"\x89PNG\r\n\x1a\nother")
        (DOCUMENTS / "broken.md").write_text("```\nunterminated fence mentions named.png\n", encoding="utf-8")
        headers = _signed(world)
        blocked = _move(world, headers, {"rootId": ROOT, "path": doc("attachments/named.png")}, {"rootId": ROOT, "path": doc("archive/named.png")})
        allowed = _move(world, headers, {"rootId": ROOT, "path": doc("attachments/other.png")}, {"rootId": ROOT, "path": doc("archive/other.png")})

    assert blocked.status_code == 409, blocked.text
    assert (DOCUMENTS / "attachments" / "named.png").exists()
    assert allowed.status_code == 200, allowed.text
    assert (DOCUMENTS / "archive" / "other.png").exists()


def test_moving_markdown_rebases_image_url_and_keeps_ui_metadata(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        (root / "attachments").mkdir()
        (root / "attachments" / "photo image.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        note = root / "note.md"
        note.write_text("![photo](attachments/photo%20image.png)\n", encoding="utf-8")
        headers = _signed(world)
        state = world.client.get("/api/state", headers=headers)
        assert state.status_code == 200, state.text
        state_doc = state.json()
        info = note.stat()
        state_doc["items"] = [{
            "path": "/" + doc("note.md"),
            "labelIds": ["azul"],
            "favorite": True,
            "emoji": None,
            "inode": info.st_ino,
            "device": info.st_dev,
        }]
        saved_state = world.client.put(
            "/api/state",
            headers={**headers, "content-type": "application/json"},
            json={
                "baseRevision": state_doc["stateRevision"],
                **{key: value for key, value in state_doc.items() if key != "stateRevision"},
            },
        )
        assert saved_state.status_code == 200, saved_state.text
        response = _move(
            world,
            headers,
            {"rootId": ROOT, "path": doc("note.md")},
            {"rootId": ROOT, "path": doc("archive/note.md")},
        )
        assert response.status_code == 200, response.text
        moved = root / "archive" / "note.md"
        assert moved.read_text(encoding="utf-8") == "![photo](../attachments/photo%20image.png)\n"
        state_after = world.client.get("/api/state", headers=headers).json()
        assert any(item["path"] == "/" + doc("archive/note.md") for item in state_after["items"])


def test_moving_directory_rewrites_external_notes_but_keeps_internal_relative_links(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        source = root / "bundle"
        source.mkdir()
        (source / "nested").mkdir()
        (source / "nested" / "photo image.png").write_bytes(b"synthetic")
        internal = source / "nested" / "inside.md"
        internal.write_text("![inside](photo%20image.png)\n", encoding="utf-8")
        external = root / "outside.md"
        external.write_text("![outside](bundle/nested/photo%20image.png)\n", encoding="utf-8")
        response = _move(
            world,
            _signed(world),
            {"rootId": ROOT, "path": doc("bundle")},
            {"rootId": ROOT, "path": doc("archive/bundle")},
        )
        assert response.status_code == 200, response.text
        moved = root / "archive" / "bundle"
        assert (moved / "nested" / "photo image.png").is_file()
        assert (moved / "nested" / "inside.md").read_text(encoding="utf-8") == "![inside](photo%20image.png)\n"
        assert external.read_text(encoding="utf-8") == "![outside](archive/bundle/nested/photo%20image.png)\n"


def test_connected_clean_buffer_must_acknowledge_and_receives_new_address(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        (root / "note.md").write_text("# Buffer\n", encoding="utf-8")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("note.md")})
        assert loaded.status_code == 200, loaded.text
        runtime = world.client.app.state.runtime
        session_id = "a" * 64
        expires_at = world.clock.now() + 600
        document = {
            "rootId": ROOT, "path": doc("note.md"),
            "version": loaded.json()["version"],
            "dirty": False,
        }
        runtime.buffers.sync(session_id, expires_at, [document], [], world.clock.now())
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                _move,
                world,
                headers,
                {"rootId": ROOT, "path": doc("note.md")},
                {"rootId": ROOT, "path": doc("archive/note.md")},
            )
            hold = None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not pending.done():
                sync = runtime.buffers.sync(
                    session_id, expires_at, [document], [], world.clock.now()
                )
                hold = next(
                    (command for command in sync["commands"] if command["type"] == "hold"),
                    None,
                )
                if hold is not None:
                    ack = {
                        "moveId": hold["id"],
                        "accepted": True,
                        "documents": [document],
                    }
                    runtime.buffers.sync(
                        session_id, expires_at, [document], [ack], world.clock.now()
                    )
                    break
                time.sleep(0.01)
            response = pending.result(timeout=5)
        assert hold is not None
        assert response.status_code == 200, response.text
        completed = runtime.buffers.sync(
            session_id, expires_at, [document], [], world.clock.now()
        )
        assert completed["commands"] == [{
            "id": hold["id"],
            "type": "complete",
            "documents": [{
                "rootId": ROOT, "path": doc("note.md"),
                "newRootId": ROOT,
                "newPath": doc("archive/note.md"),
            }],
        }]


def test_dirty_connected_buffer_rejects_move_before_publication(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        note = root / "note.md"
        note.write_text("# Unsaved\n", encoding="utf-8")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("note.md")})
        assert loaded.status_code == 200, loaded.text
        runtime = world.client.app.state.runtime
        session_id = "b" * 64
        document = {
            "rootId": ROOT, "path": doc("note.md"),
            "version": loaded.json()["version"],
            "dirty": True,
        }
        runtime.buffers.sync(
            session_id, world.clock.now() + 600, [document], [], world.clock.now()
        )
        response = _move(
            world,
            headers,
            {"rootId": ROOT, "path": doc("note.md")},
            {"rootId": ROOT, "path": doc("archive/note.md")},
        )
        assert response.status_code == 409, response.text
        assert note.read_text(encoding="utf-8") == "# Unsaved\n"
        assert not (root / "archive" / "note.md").exists()


def test_move_journal_recovers_after_rename_before_identity_record(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        note = root / "note.md"
        note.write_text("# Recover\n", encoding="utf-8")
        headers = _signed(world)
        token_response = world.client.post("/api/files/token", headers=headers)
        assert token_response.status_code == 201, token_response.text
        token = token_response.json()["operationToken"]
        runtime = world.client.app.state.runtime
        catalog = build_catalog(world.config, strict=False)
        runtime.trash.fail_at.add("move_after_rename")
        with pytest.raises(TrashCrash):
            with runtime.images.lock():
                runtime.operations.submit(
                    token,
                    [{
                        "action": "move",
                        "source": {"rootId": ROOT, "path": doc("note.md")},
                        "destination": {"rootId": ROOT, "path": doc("archive/note.md")},
                    }],
                    catalog,
                    world.clock.now(),
                )
        runtime.trash.fail_at.clear()
        runtime.trash.recover(catalog, world.clock.now())
        runtime.images.recover_moves(catalog, world.clock.now(), runtime.operations, runtime.buffers)
        runtime.operations.recover(catalog, world.clock.now())
        assert not note.exists()
        assert (root / "archive" / "note.md").read_text(encoding="utf-8") == "# Recover\n"
        status = world.client.get("/api/files/" + token, headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "completed", json.dumps(status.json(), indent=2)


@pytest.mark.parametrize("crash_point", ["move_after_publication", "after_rewrite"])
def test_move_journal_recovers_after_restore_and_rewrite_publication(tmp_path, monkeypatch, crash_point) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        (root / "archive").mkdir()
        (root / "attachments").mkdir()
        (root / "attachments" / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nrecover")
        note = root / "note.md"
        note.write_text("![photo](attachments/photo.png)\n", encoding="utf-8")
        headers = _signed(world)
        issued = world.client.post("/api/files/token", headers=headers)
        assert issued.status_code == 201, issued.text
        token = issued.json()["operationToken"]
        runtime = world.client.app.state.runtime
        catalog = build_catalog(world.config, strict=False)
        original_apply = runtime.images._apply_move_document
        if crash_point == "move_after_publication":
            runtime.trash.fail_at.add(crash_point)
        else:
            def apply_then_crash(*args, **kwargs):
                original_apply(*args, **kwargs)
                raise TrashCrash(crash_point)

            monkeypatch.setattr(runtime.images, "_apply_move_document", apply_then_crash)
        with pytest.raises(TrashCrash):
            with runtime.images.lock():
                runtime.operations.submit(
                    token,
                    [{
                    "action": "move",
                    "source": {"rootId": ROOT, "path": doc("note.md")},
                    "destination": {"rootId": ROOT, "path": doc("archive/note.md")},
                    }],
                    catalog,
                    world.clock.now(),
                )

        runtime.trash.fail_at.clear()
        monkeypatch.setattr(runtime.images, "_apply_move_document", original_apply)
        runtime.trash.recover(catalog, world.clock.now())
        runtime.images.recover_moves(catalog, world.clock.now(), runtime.operations, runtime.buffers)
        runtime.operations.recover(catalog, world.clock.now())
        assert not note.exists()
        assert (root / "archive" / "note.md").read_text(encoding="utf-8") == "![photo](../attachments/photo.png)\n"
        status = world.client.get("/api/files/" + token, headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "completed", json.dumps(status.json(), indent=2)


@pytest.mark.parametrize(
    ("object_kind", "cross_filesystem", "crash_after_binding", "external_occupant"),
    [
        (object_kind, cross_filesystem, crash_after_binding, False)
        for object_kind in ("file", "directory")
        for cross_filesystem in (False, True)
        for crash_after_binding in (False, True)
    ]
    + [
        (object_kind, cross_filesystem, False, True)
        for object_kind in ("file", "directory")
        for cross_filesystem in (False, True)
    ],
)
def test_move_recovery_rebinds_versions_before_rewrite_across_restore_crashes(
    tmp_path,
    monkeypatch,
    object_kind: str,
    cross_filesystem: bool,
    crash_after_binding: bool,
    external_occupant: bool,
) -> None:
    documents = DOCUMENTS
    archive_temp = None
    if cross_filesystem:
        archive_base = next(
            (
                parent
                for parent in (Path("/var/tmp"), Path("/dev/shm"))
                if parent.is_dir()
                and os.access(parent, os.W_OK)
                and parent.stat().st_dev != documents.stat().st_dev
            ),
            None,
        )
        if archive_base is None:
            pytest.skip("no writable second filesystem is available")
        archive_temp = tempfile.TemporaryDirectory(prefix="hf-images-recovery-", dir=archive_base)
        archive = Path(archive_temp.name)
    else:
        archive = DOCUMENTS / "archive-root"
        archive.mkdir()

    (documents / "attachments").mkdir()
    (documents / "attachments" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nrecovery")
    if object_kind == "file":
        source_path = "note.md"
        note = documents / source_path
        note_content = "![photo](attachments/a.png)\n"
        destination_path = "note.md"
        target_note = archive / destination_path
    else:
        source_path = "bundle"
        bundle = documents / source_path
        bundle.mkdir()
        note = bundle / "note.md"
        note_content = "![photo](../attachments/a.png)\n"
        destination_path = "bundle"
        target_note = archive / destination_path / "note.md"
    note.write_text(note_content, encoding="utf-8")
    expected_reference = os.path.relpath(documents / "attachments" / "a.png", target_note.parent)
    expected_content = f"![photo]({expected_reference})\n"
    try:
        with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
            headers = _signed(world)
            issued = world.client.post("/api/files/token", headers=headers)
            assert issued.status_code == 201, issued.text
            token = issued.json()["operationToken"]
            runtime = world.client.app.state.runtime
            catalog = build_catalog(world.config, strict=False)
            original_bind = runtime.images._bind_restored_move_versions
            if crash_after_binding:
                runtime.trash.fail_at.add("restore_after_publish" if cross_filesystem else "move_after_publication")
            else:
                def crash_before_binding(*_args, **_kwargs):
                    raise TrashCrash("before_version_binding")

                monkeypatch.setattr(runtime.images, "_bind_restored_move_versions", crash_before_binding)

            with pytest.raises(TrashCrash):
                with runtime.images.lock():
                    runtime.operations.submit(
                        token,
                        [{
                            "action": "move",
                            "source": {"rootId": ROOT, "path": doc(source_path)},
                            "destination": {"rootId": ROOT, "path": str(archive / destination_path)[1:]},
                        }],
                        catalog,
                        world.clock.now(),
                    )

            runtime.trash.fail_at.clear()
            monkeypatch.setattr(runtime.images, "_bind_restored_move_versions", original_bind)
            move_directories = runtime.images._move_journals()
            assert len(move_directories) == 1
            move_directory, before_recovery = move_directories[0]
            assert before_recovery["phase"] == (
                "object_published" if crash_after_binding else ("restoring" if cross_filesystem else "renaming")
            )
            before_stages = [value for value in before_recovery["stages"].values() if isinstance(value, dict)]
            assert len(before_stages) == 1
            assert ("restoredTargetVersion" in before_stages[0]) is crash_after_binding

            external_identity = None
            if external_occupant and object_kind == "file":
                target_note.unlink()
                target_note.write_text(note_content, encoding="utf-8")
                external_identity = target_note.stat().st_ino
            elif external_occupant:
                target_directory = archive / "bundle"
                target_directory.rename(archive / "displaced-bundle")
                target_directory.mkdir()
                external_note = target_directory / "note.md"
                external_note.write_text(note_content, encoding="utf-8")
                external_identity = (target_directory.stat().st_ino, external_note.stat().st_ino)

            original_apply = runtime.images._apply_move_document
            persisted_binding_checks = []

            def require_persisted_binding(*args, **kwargs):
                persisted = json.loads((move_directory / "journal.json").read_text(encoding="utf-8"))["document"]
                persisted_stages = [value for value in persisted["stages"].values() if isinstance(value, dict)]
                assert len(persisted_stages) == 1
                assert isinstance(persisted_stages[0].get("restoredTargetVersion"), str)
                persisted_binding_checks.append(persisted_stages[0]["restoredTargetVersion"])
                return original_apply(*args, **kwargs)

            monkeypatch.setattr(runtime.images, "_apply_move_document", require_persisted_binding)
            runtime.trash.recover(catalog, world.clock.now())
            runtime.images.recover_moves(catalog, world.clock.now(), runtime.operations, runtime.buffers)
            runtime.operations.recover(catalog, world.clock.now())

            status = world.client.get("/api/files/" + token, headers=headers)
            if external_occupant:
                assert not persisted_binding_checks
                assert status.status_code == 200, status.text
                assert status.json()["status"] == "indeterminate"
                assert status.json()["indeterminate"][0]["error"] == "conflict"
                assert target_note.read_text(encoding="utf-8") == note_content
                if object_kind == "file":
                    assert target_note.stat().st_ino == external_identity
                else:
                    assert (target_note.parent.stat().st_ino, target_note.stat().st_ino) == external_identity
            else:
                assert persisted_binding_checks, json.dumps({
                    "status": status.json(),
                    "destination": str(target_note),
                    "destinationContent": target_note.read_text(encoding="utf-8") if target_note.exists() else None,
                    "moveJournal": json.loads((move_directory / "journal.json").read_text(encoding="utf-8"))
                    if (move_directory / "journal.json").exists() else None,
                }, indent=2)
                assert not note.exists()
                assert target_note.read_text(encoding="utf-8") == expected_content
            assert status.status_code == 200, status.text
            if not external_occupant:
                assert status.json()["status"] == "completed", json.dumps(status.json(), indent=2)
    finally:
        if archive_temp is not None:
            archive_temp.cleanup()


def _trash_delete(world, headers, path):
    response = world.client.post(
        "/api/trash",
        headers={**headers, "content-type": "application/json"},
        json={"action": "delete", "source": {"rootId": ROOT, "path": doc(path)}},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_restoring_note_offers_and_restores_referenced_trashed_image(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        image_bytes = b"\x89PNG\r\n\x1a\nrestore-test"
        (root / "photo.png").write_bytes(image_bytes)
        note_bytes = b"# Restore\n\n![photo](photo.png)\n"
        (root / "note.md").write_bytes(note_bytes)
        headers = _signed(world)

        image_id = _trash_delete(world, headers, "photo.png")
        note_id = _trash_delete(world, headers, "note.md")
        related = world.client.get("/api/trash/related-images", params={"id": note_id})
        assert related.status_code == 200, related.text
        assert related.json()["complete"] is True
        groups = related.json()["images"]
        assert len(groups) == 1
        assert (groups[0]["rootId"], groups[0]["path"]) == (ROOT, doc("photo.png"))
        assert groups[0]["candidates"][0]["id"] == image_id
        assert groups[0]["candidates"][0]["size"] == len(image_bytes)

        restored = world.client.post(
            "/api/trash",
            headers={**headers, "content-type": "application/json"},
            json={"action": "restore", "id": note_id, "relatedImageIds": [image_id]},
        )
        assert restored.status_code == 200, restored.text
        assert len(restored.json()["restored"]) == 2
        assert (root / "note.md").read_bytes() == note_bytes
        assert (root / "photo.png").read_bytes() == image_bytes


def test_joint_note_image_restore_preflights_every_destination(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        image_bytes = b"\x89PNG\r\n\x1a\noriginal"
        (root / "photo.png").write_bytes(image_bytes)
        note_bytes = b"![photo](photo.png)\n"
        (root / "note.md").write_bytes(note_bytes)
        headers = _signed(world)

        image_id = _trash_delete(world, headers, "photo.png")
        note_id = _trash_delete(world, headers, "note.md")
        external = b"external occupant"
        (root / "photo.png").write_bytes(external)
        restored = world.client.post(
            "/api/trash",
            headers={**headers, "content-type": "application/json"},
            json={"action": "restore", "id": note_id, "relatedImageIds": [image_id]},
        )
        assert restored.status_code == 409, restored.text
        assert restored.json()["error"] == "destination_occupied"
        assert (root / "photo.png").read_bytes() == external
        trash = world.client.get("/api/trash", headers=headers)
        assert {item["id"] for item in trash.json()["entries"]} >= {note_id, image_id}
        assert not (root / "note.md").exists()
