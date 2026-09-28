from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from conftest import CLOCK_START, PASSWORD, ManualClock, boot, D, DOCUMENTS, ROOT, doc


def _headers(world) -> dict[str, str]:
    login = world.post_login(PASSWORD)
    assert login.status_code == 200, login.text
    return {"origin": world.config.origin, "x-csrf-token": login.json()["csrfToken"]}


def _root(world) -> Path:
    return DOCUMENTS


def _save(world, headers: dict[str, str], path: str, content: str):
    loaded = world.client.get("/api/file", params={"rootId": ROOT, "path": doc(path)})
    assert loaded.status_code == 200, loaded.text
    return world.client.put(
        "/api/file",
        params={"rootId": ROOT, "path": doc(path)},
        headers=headers,
        json={"baseVersion": loaded.json()["version"], "content": content},
    )


def _candidate(root: Path, name: str = "candidate.png") -> tuple[Path, bytes]:
    attachments = root / "attachments"
    attachments.mkdir(exist_ok=True)
    path = attachments / name
    payload = b"\x89PNG\r\n\x1a\nsynthetic-managed-image"
    path.write_bytes(payload)
    return path, payload


def test_collection_resolves_reference_in_2_to_5_mib_markdown_via_http_saves(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        image, image_bytes = _candidate(root)
        (root / "note.md").write_text("![note](attachments/candidate.png)\n", encoding="utf-8")
        medium = root / "medium.md"
        reference = b"![medium](attachments/candidate.png)\n"
        with medium.open("wb") as stream:
            stream.write((b"x" * (32 * 1024) + b"\n") * 96)
            stream.write(reference)
        medium_size = medium.stat().st_size
        assert 2 * 1024 * 1024 <= medium_size <= 5 * 1024 * 1024

        headers = _headers(world)
        removed_note = _save(world, headers, "note.md", "# No reference in this note\n")
        assert removed_note.status_code == 200, removed_note.text
        assert removed_note.json()["imageCollection"] == {"removed": 0, "inconclusive": False}
        assert image.read_bytes() == image_bytes
        assert medium.read_bytes().endswith(reference)

        removed_medium = _save(world, headers, "medium.md", "# Reference removed\n")
        assert removed_medium.status_code == 200, removed_medium.text
        assert removed_medium.json()["imageCollection"] == {"removed": 1, "inconclusive": False}
        assert not image.exists()


@pytest.mark.parametrize("failure", ["short_eof", "read_error", "unreadable_file"])
def test_http_save_preserves_candidate_when_stream_is_incomplete_or_unreadable(
    tmp_path, monkeypatch, failure: str,
) -> None:
    from hopper_files import image_refs

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        image, image_bytes = _candidate(root, f"{failure}.png")
        (root / "note.md").write_text(f"![candidate](attachments/{image.name})\n", encoding="utf-8")
        scan_target = root / "z-scan-target.md"
        scan_target.write_text("# No image reference in this file\n", encoding="utf-8")
        if failure == "unreadable_file":
            scan_target.chmod(0)

        triggered = False
        if failure in {"short_eof", "read_error"}:
            original_read = image_refs.os.read
            target = scan_target.resolve()

            def controlled_read(fd: int, count: int) -> bytes:
                nonlocal triggered
                try:
                    descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()
                except OSError:
                    descriptor_path = None
                if descriptor_path == target:
                    triggered = True
                    if failure == "read_error":
                        raise OSError(errno.EIO, "injected scanner read failure")
                    if not getattr(controlled_read, "returned_partial", False):
                        controlled_read.returned_partial = True
                        return original_read(fd, max(1, min(count, scan_target.stat().st_size // 2)))
                    return b""
                return original_read(fd, count)

            monkeypatch.setattr(image_refs.os, "read", controlled_read)

        headers = _headers(world)
        saved = _save(world, headers, "note.md", "# Candidate reference removed\n")

    assert saved.status_code == 200, saved.text
    assert saved.json()["imageCollection"] == {"removed": 0, "inconclusive": True}
    assert image.read_bytes() == image_bytes
    if failure in {"short_eof", "read_error"}:
        assert triggered
    else:
        assert stat.S_IMODE(scan_target.stat().st_mode) == 0


def test_http_save_preserves_candidate_when_scanned_content_changes_mid_stream(tmp_path, monkeypatch) -> None:
    from hopper_files import image_refs

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _root(world)
        image, image_bytes = _candidate(root, "race.png")
        (root / "note.md").write_text("![candidate](attachments/race.png)\n", encoding="utf-8")
        racing_note = root / "z-race.md"
        racing_note.write_bytes(b"# No reference initially\n")
        late_reference = b"![late reference](attachments/race.png)\n"
        original_read = image_refs.os.read
        target = racing_note.resolve()
        triggered = False

        def race_after_stream_eof(fd: int, count: int) -> bytes:
            nonlocal triggered
            try:
                descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()
            except OSError:
                descriptor_path = None
            block = original_read(fd, count)
            if descriptor_path == target and not block and not triggered:
                racing_note.write_bytes(late_reference)
                triggered = True
            return block

        monkeypatch.setattr(image_refs.os, "read", race_after_stream_eof)
        headers = _headers(world)
        saved = _save(world, headers, "note.md", "# Candidate reference removed\n")

    assert triggered
    assert saved.status_code == 200, saved.text
    assert saved.json()["imageCollection"] == {"removed": 0, "inconclusive": True}
    assert racing_note.read_bytes() == late_reference
    assert image.read_bytes() == image_bytes
