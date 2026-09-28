from __future__ import annotations

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
import os
import shutil
import stat
import struct
import zipfile
from pathlib import Path

import pytest
import httpx

import hopper_files.files as file_ops
import hopper_files.roots as root_ops
from hopper_files.files import (
    OperationError,
    OperationStore,
    UPLOAD_MAX_FILE,
    UPLOAD_MAX_TOTAL,
    ZIP_EXTRACT_MAX_BYTES,
    ZIP_EXTRACT_MAX_FILE,
    ZIP_SOURCE_MAX_ENTRIES,
    _normalize_actions,
    _normalize_upload,
)
from hopper_files.roots import AddressRejected, build_catalog
from hopper_files.state import StateError

from conftest import CLOCK_START, PASSWORD, ManualClock, boot, D, DOCUMENTS, ROOT, doc, terminal_mode, rel, set_default_acl


def _headers(world, csrf: str) -> dict[str, str]:
    return {"origin": world.config.origin, "x-csrf-token": csrf}


def _signed(world) -> str:
    response = world.post_login(PASSWORD)
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def _token(world, csrf: str) -> str:
    response = world.client.post(world.config.base_path + "api/files/token", headers=_headers(world, csrf))
    assert response.status_code == 201, response.text
    return response.json()["operationToken"]


def _mutate(world, csrf: str, token: str, actions: list[dict[str, object]]):
    return world.client.post(
        world.config.base_path + "api/files",
        json={"operationToken": token, "actions": actions},
        headers=_headers(world, csrf),
    )


def _manifest(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _upload(world, csrf: str, token: str, manifest: dict[str, object], body: bytes):
    return world.client.post(
        world.config.base_path + "api/files/upload",
        content=body,
        headers={
            **_headers(world, csrf),
            "x-hopper-operation-token": token,
            "x-hopper-upload-manifest": _manifest(manifest),
            "content-type": "application/octet-stream",
        },
    )


def _forged_zip(entries: list[tuple[str, int, int]]) -> bytes:
    """Build a small central directory with synthetic declared member sizes."""
    local_records = bytearray()
    central_records = bytearray()
    for name, compressed_size, file_size in entries:
        encoded_name = name.encode("utf-8")
        offset = len(local_records)
        local_records.extend(struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            zipfile.ZIP_STORED,
            0,
            0,
            0,
            compressed_size,
            file_size,
            len(encoded_name),
            0,
        ))
        local_records.extend(encoded_name)
        central_records.extend(struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            (3 << 8) | 20,
            20,
            0,
            zipfile.ZIP_STORED,
            0,
            0,
            0,
            compressed_size,
            file_size,
            len(encoded_name),
            0,
            0,
            0,
            0,
            (stat.S_IFREG | 0o600) << 16,
            offset,
        ))
        central_records.extend(encoded_name)
    local_size = len(local_records)
    entry_count = len(entries)
    end = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        entry_count,
        entry_count,
        len(central_records),
        local_size,
        0,
    )
    return bytes(local_records + central_records + end)


def _file_action(path: str) -> dict[str, object]:
    return {
        "action": "create",
        "kind": "file",
        "nameMode": "exact",
        "destination": {"rootId": ROOT, "path": doc(path)},
    }


def test_file_api_requires_session_csrf_and_instance_bound_tokens(tmp_path) -> None:
    with boot(tmp_path / "alpha", ManualClock(CLOCK_START), password=PASSWORD, instance_id="alpha") as alpha, boot(
        tmp_path / "beta", ManualClock(CLOCK_START), password=PASSWORD, instance_id="beta"
    ) as beta:
        denied = alpha.client.post("/api/files/token", headers={"origin": alpha.config.origin})
        csrf = _signed(alpha)
        no_csrf = alpha.client.post("/api/files/token", headers={"origin": alpha.config.origin})
        token = _token(alpha, csrf)
        cross_instance = beta.post_login(PASSWORD)
        status = beta.client.get("/api/files/" + token)

    assert denied.status_code == 401
    assert no_csrf.status_code == 403
    assert cross_instance.status_code == 200
    assert status.status_code == 422
    assert status.json() == {"error": "invalid_request"}


def test_exact_names_generated_notes_listing_and_default_modes(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD, timezone="UTC") as world:
        csrf = _signed(world)
        before = world.client.get(f"/api/list?rootId=fs&path={D}")
        token = _token(world, csrf)
        result = _mutate(
            world,
            csrf,
            token,
            [
                _file_action(".tool"),
                {
                    "action": "create",
                    "kind": "note",
                    "nameMode": "generated",
                    "destination": {"rootId": ROOT, "path": D},
                    "title": "Crème brûlée",
                    "subject": "Crème brûlée",
                    "extension": "md",
                },
                {
                    "action": "create",
                    "kind": "directory",
                    "nameMode": "exact",
                    "destination": {"rootId": ROOT, "path": doc("Drafts")},
                },
            ],
        )
        after = world.client.get(f"/api/list?rootId=fs&path={D}")
        root = DOCUMENTS

    assert before.status_code == after.status_code == 200
    assert before.json()["listingVersion"] != after.json()["listingVersion"]
    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert (root / ".tool").read_bytes() == b""
    assert stat.S_IMODE((root / ".tool").stat().st_mode) == terminal_mode("file")
    assert (root / "creme-brulee-20231114-221320.md").read_bytes() == "# Crème brûlée\n".encode()
    assert stat.S_IMODE((root / "Drafts").stat().st_mode) == terminal_mode("directory")
    assert {entry["name"] for entry in after.json()["entries"]} >= {
        ".tool",
        "creme-brulee-20231114-221320.md",
        "Drafts",
    }


def test_listing_reports_modification_and_creation_times(tmp_path) -> None:
    import time as _time
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        (root / "dated.txt").write_bytes(b"x")
        stamp = 1_700_000_000  # 2023-11-14T22:13:20Z
        os.utime(root / "dated.txt", (stamp, stamp))
        before = int(_time.time()) - 5
        _signed(world)
        listing = world.client.get(f"/api/list?rootId=fs&path={D}")

    assert listing.status_code == 200
    entry = next(item for item in listing.json()["entries"] if item["name"] == "dated.txt")
    assert entry["modifiedAt"] == "2023-11-14T22:13:20Z"
    # The birth time comes from statx(2). A filesystem that does not record it reports null;
    # when it is present it is the real creation moment, not the adjusted modification time.
    created = entry["createdAt"]
    assert created is None or created.endswith("Z")
    if created is not None:
        from datetime import datetime
        created_at = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").timestamp()
        assert created_at >= before - 86400 and created != entry["modifiedAt"]


def test_directory_size_reports_partial_totals_instead_of_failing(tmp_path, monkeypatch) -> None:
    import hopper_files.api.files as api_files
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        (root / "readable").mkdir()
        (root / "readable" / "a.txt").write_bytes(b"12345")
        closed = root / "closed"
        closed.mkdir()
        (closed / "hidden.txt").write_bytes(b"secret")
        closed.chmod(0)
        try:
            _signed(world)
            partial = world.client.get(f"/api/dir-size?rootId=fs&path={D}")
            virtual = world.client.get("/api/dir-size?rootId=fs&path=proc")
            monkeypatch.setattr(api_files, "DIR_SIZE_BUDGET_SECONDS", 0.0)
            stopped = world.client.get(f"/api/dir-size?rootId=fs&path={D}")
        finally:
            closed.chmod(0o700)

    assert partial.status_code == 200
    body = partial.json()
    if os.geteuid() != 0:
        assert body["complete"] is False
        assert {"category": "unreadable_directory", "count": 1} in body["omissions"]
    assert body["totalBytes"] >= 5 and body["fileCount"] >= 1
    assert virtual.status_code == 200
    assert virtual.json()["complete"] is False
    assert virtual.json()["omissions"] == [{"category": "pseudo_filesystem", "count": 1}]
    assert virtual.json()["totalBytes"] == 0
    assert stopped.status_code == 200
    assert stopped.json()["complete"] is False
    assert any(item["category"] == "execution_budget" for item in stopped.json()["omissions"])


def test_directory_size_counts_visible_regular_files_without_following_links(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        (root / "one.txt").write_bytes(b"abc")
        nested = root / "nested"
        nested.mkdir()
        (nested / "two.txt").write_bytes(b"de")
        (root / "external-link").symlink_to(nested / "two.txt")
        denied = world.client.get(f"/api/dir-size?rootId=fs&path={D}")
        _signed(world)
        total = world.client.get(f"/api/dir-size?rootId=fs&path={D}")
        nested_total = world.client.get(f"/api/dir-size?rootId=fs&path={D}/nested")

    assert denied.status_code == 401
    assert total.status_code == 200
    assert total.json() == {
        "rootId": ROOT, "path": D,
        "totalBytes": 5,
        "fileCount": 2,
        "directoryCount": 2,
        "complete": True,
        "omissions": [],
    }
    assert nested_total.status_code == 200
    assert nested_total.json()["totalBytes"] == 2
    assert nested_total.json()["fileCount"] == 1
    assert nested_total.json()["directoryCount"] == 1


def test_stage_name_lookalikes_and_exact_internal_pattern_are_ordinary_names(tmp_path) -> None:
    literal_name = ".hopper-stage-user-note.md"
    internal_name = ".hopper-stage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp"
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        created = _mutate(
            world,
            csrf,
            _token(world, csrf),
            [{
                "action": "create",
                "kind": "note",
                "nameMode": "exact",
                "destination": {"rootId": ROOT, "path": doc(literal_name)},
                "title": "Visible literal name",
            }],
        )
        listing = world.client.get(f"/api/list?rootId=fs&path={D}")
        raw = world.client.get(f"/api/raw?rootId=fs&path={D}/{literal_name}")
        copied = _mutate(
            world,
            csrf,
            _token(world, csrf),
            [{
                "action": "copy",
                "source": {"rootId": ROOT, "path": doc(literal_name)},
                "destination": {"rootId": ROOT, "path": doc("literal-copy.md")},
            }],
        )
        zipped = _mutate(
            world,
            csrf,
            _token(world, csrf),
            [{
                "action": "zip",
                "sources": [{"rootId": ROOT, "path": doc(literal_name)}],
                "destination": {"rootId": ROOT, "path": doc("literal-name.zip")},
            }],
        )
        reserved_create = _mutate(world, csrf, _token(world, csrf), [_file_action(internal_name)])
        payload = b"reserved internal staging pattern"
        reserved_upload = _upload(
            world,
            csrf,
            _token(world, csrf),
            {
                "action": "upload",
                "destination": {"rootId": ROOT, "path": D},
                "nameMode": "original",
                "files": [{
                    "originalName": internal_name,
                    "contentDigest": hashlib.sha256(payload).hexdigest(),
                    "contentSize": len(payload),
                }],
            },
            payload,
        )

    expected = b"# Visible literal name\n"
    assert created.status_code == 200
    assert listing.status_code == raw.status_code == 200
    assert literal_name in {item["name"] for item in listing.json()["entries"]}
    assert raw.content == expected
    assert copied.status_code == zipped.status_code == 200
    assert (root / "literal-copy.md").read_bytes() == expected
    with zipfile.ZipFile(root / "literal-name.zip") as archive:
        assert archive.namelist() == [literal_name]
        assert archive.read(literal_name) == expected
    # HF-NAV-002/HF-NAV-009: staging names are ordinary names; no namespace is reserved.
    assert reserved_create.status_code == 200
    assert reserved_upload.status_code == 409
    assert (root / internal_name).read_bytes() == b""


def test_generated_collision_race_skips_another_batch_items_reserved_name(tmp_path, monkeypatch) -> None:
    real_publish = file_ops.publish_staged_regular
    occupied_by_race = False

    def publish_after_race(staged):
        nonlocal occupied_by_race
        if not occupied_by_race:
            occupied_by_race = True
            root_path = DOCUMENTS
            (root_path / "report-20231114-221320.csv").write_bytes(b"external writer")
        return real_publish(staged)

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD, timezone="UTC") as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        monkeypatch.setattr(file_ops, "publish_staged_regular", publish_after_race)
        result = _mutate(
            world,
            csrf,
            token,
            [
                {
                    "action": "create",
                    "kind": "file",
                    "nameMode": "generated",
                    "destination": {"rootId": ROOT, "path": D},
                    "subject": "Report",
                    "extension": "csv",
                },
                _file_action("report-20231114-221320-2.csv"),
            ],
        )
        root = DOCUMENTS

    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert (root / "report-20231114-221320.csv").read_bytes() == b"external writer"
    assert (root / "report-20231114-221320-2.csv").read_bytes() == b""
    assert (root / "report-20231114-221320-3.csv").read_bytes() == b""


def test_generated_names_use_thirtieth_proposal_then_report_exhaustion(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD, timezone="UTC") as world:
        csrf = _signed(world)
        root = DOCUMENTS
        for proposal in range(1, 30):
            suffix = "" if proposal == 1 else f"-{proposal}"
            (root / f"bounded-20231114-221320{suffix}.txt").write_bytes(b"occupied")

        def create() -> tuple[object, str]:
            token = _token(world, csrf)
            result = _mutate(
                world,
                csrf,
                token,
                [{
                    "action": "create",
                    "kind": "file",
                    "nameMode": "generated",
                    "destination": {"rootId": ROOT, "path": D},
                    "subject": "Bounded",
                    "extension": "txt",
                }],
            )
            return result, token

        last_available, _ = create()
        exhausted, _ = create()

    assert last_available.status_code == 200
    assert last_available.json()["committed"][0]["destination"]["path"] == doc("bounded-20231114-221320-30.txt")
    assert exhausted.status_code == 409
    assert exhausted.json()["uncommitted"][0]["error"] == "conflict"


def test_raw_download_streams_exact_bytes_and_requires_authentication(tmp_path) -> None:
    payload = bytes(range(256)) * 1024
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        (root / " spaced name.bin ").write_bytes(payload)
        denied = world.client.get(f"/api/raw?rootId=fs&path={D}/%20spaced%20name.bin%20")
        _signed(world)
        downloaded = world.client.get(f"/api/raw?rootId=fs&path={D}/%20spaced%20name.bin%20")

    assert denied.status_code == 401
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["content-length"] == str(len(payload))
    assert "filename*=UTF-8''%20spaced%20name.bin%20" in downloaded.headers["content-disposition"]


def test_operation_json_body_has_a_streamed_one_mibibyte_boundary(tmp_path) -> None:
    maximum = 1024 * 1024
    prefix = b'{"operationToken":"invalid","actions":[]}'
    exactly_at_limit = prefix + b" " * (maximum - len(prefix))
    over_limit = exactly_at_limit + b" "
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        headers = {**_headers(world, csrf), "content-type": "application/json"}
        accepted = world.client.post("/api/files", content=exactly_at_limit, headers=headers)
        rejected = world.client.post("/api/files", content=over_limit, headers=headers)

    assert accepted.status_code == 422
    assert rejected.status_code == 413


def test_known_copy_capacity_limit_returns_413_with_item_result(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        (root / "source.bin").write_bytes(b"copy needs destination bytes")
        token = _token(world, csrf)
        real_capacity = file_ops.destination_capacity

        def exhausted(_catalog, root_id: str, destination: str) -> tuple[int, int]:
            device, _available = real_capacity(build_catalog(world.config), root_id, destination)
            return device, 0

        monkeypatch.setattr(file_ops, "destination_capacity", exhausted)
        response = _mutate(
            world,
            csrf,
            token,
            [{
                "action": "copy",
                "source": {"rootId": ROOT, "path": doc("source.bin")},
                "destination": {"rootId": ROOT, "path": doc("copy.bin")},
            }],
        )

    assert response.status_code == 413
    assert response.json()["status"] == "failed"
    assert response.json()["uncommitted"][0]["error"] == "limit"
    assert not (root / "copy.bin").exists()


def test_preflight_prevents_avoidable_batch_partial_commit_and_fingerprint_replay(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        (root / "occupied.txt").write_bytes(b"keep")
        token = _token(world, csrf)
        rejected = _mutate(world, csrf, token, [_file_action("first.txt"), _file_action("occupied.txt")])
        mismatch = _mutate(world, csrf, token, [_file_action("different.txt")])
        status = world.client.get("/api/files/" + token, headers=_headers(world, csrf))

    assert rejected.status_code == 409
    assert rejected.json()["status"] == "failed"
    assert not (root / "first.txt").exists()
    assert (root / "occupied.txt").read_bytes() == b"keep"
    assert mismatch.status_code == 409
    assert status.status_code == 200
    assert status.json()["status"] == "failed"
    assert len(status.json()["uncommitted"]) == 2


@pytest.mark.parametrize("storage_fault", ["invalid_json", "read_eio"])
def test_unreadable_operation_journal_is_indeterminate_over_http_and_does_not_replay(
    tmp_path, monkeypatch, storage_fault: str
) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        action = [_file_action("already-published.txt")]
        published = _mutate(world, csrf, token, action)
        assert published.status_code == 200, published.text

        root = DOCUMENTS
        target = root / "already-published.txt"
        original_bytes = target.read_bytes()
        original_stat = target.stat()
        journal = next((Path(world.config.state_directory) / "operations").glob("op-*.json"))
        journal_stat = journal.stat()
        if storage_fault == "invalid_json":
            journal.write_bytes(b"{truncated journal")
        else:
            real_read = os.read
            journal_identity = (journal_stat.st_dev, journal_stat.st_ino)

            def fail_journal_read(fd: int, count: int) -> bytes:
                details = os.fstat(fd)
                if (details.st_dev, details.st_ino) == journal_identity:
                    raise OSError(errno.EIO, "synthetic journal read failure")
                return real_read(fd, count)

            monkeypatch.setattr(file_ops.os, "read", fail_journal_read)

        status = world.client.get(
            world.config.base_path + "api/files/" + token,
            headers=_headers(world, csrf),
        )
        replay = _mutate(world, csrf, token, action)
        issue = world.client.post(
            world.config.base_path + "api/files/token",
            headers=_headers(world, csrf),
        )
        upload_bytes = b"must not be published"
        upload_manifest = {
            "action": "upload",
            "destination": {"rootId": ROOT, "path": D},
            "nameMode": "original",
            "files": [{
                "originalName": "must-not-upload.bin",
                "contentDigest": hashlib.sha256(upload_bytes).hexdigest(),
                "contentSize": len(upload_bytes),
            }],
        }
        upload = _upload(world, csrf, token, upload_manifest, upload_bytes)

        after_stat = target.stat()
        after_bytes = target.read_bytes()
        visible_names = {entry.name for entry in root.iterdir()}

    for response in (status, replay, issue, upload):
        assert response.status_code == 503, response.text
        assert response.json() == {"error": "indeterminate"}
    assert after_bytes == original_bytes
    assert (after_stat.st_dev, after_stat.st_ino) == (original_stat.st_dev, original_stat.st_ino)
    assert visible_names == {"already-published.txt"}


def test_partial_commit_is_reported_and_same_token_resumes_only_uncommitted_item(tmp_path, monkeypatch) -> None:
    real_publish = file_ops.publish_staged_regular
    calls = 0

    def fail_second(staged):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Leave the exact stage recorded in the journal. The injected
            # failure is before rename, so recovery has positive evidence that
            # this item was not published and may safely resume it.
            raise AddressRejected("unavailable")
        return real_publish(staged)

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        monkeypatch.setattr(file_ops, "publish_staged_regular", fail_second)
        first = _mutate(world, csrf, token, [_file_action("one.txt"), _file_action("two.txt")])
        monkeypatch.setattr(file_ops, "publish_staged_regular", real_publish)
        replay = _mutate(world, csrf, token, [_file_action("one.txt"), _file_action("two.txt")])
        root = DOCUMENTS

    assert first.status_code == 207
    assert first.json()["status"] == "partial"
    assert [rel(item["destination"]["path"]) for item in first.json()["committed"]] == ["one.txt"]
    assert len(first.json()["uncommitted"]) == 1
    assert first.json()["uncommitted"][0]["error"] == "unavailable"
    assert replay.status_code == 200
    assert replay.json()["status"] == "completed"
    assert (root / "one.txt").read_bytes() == b""
    assert (root / "two.txt").read_bytes() == b""


def test_publication_recovery_recognizes_destination_after_lost_completion_record(tmp_path, monkeypatch) -> None:
    real_publish = file_ops.publish_staged_regular
    raised = False

    def publish_then_crash(staged):
        nonlocal raised
        identity = real_publish(staged)
        if not raised:
            raised = True
            raise KeyboardInterrupt("simulated process stop after publication")
        return identity

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        store = world.client.app.state.runtime.operations
        catalog = build_catalog(world.client.app.state.runtime.config)
        token = store.issue(CLOCK_START)["operationToken"]
        monkeypatch.setattr(file_ops, "publish_staged_regular", publish_then_crash)
        with pytest.raises(KeyboardInterrupt):
            store.submit(token, [_file_action("recovered.txt")], catalog, CLOCK_START)
        monkeypatch.setattr(file_ops, "publish_staged_regular", real_publish)
        store.recover(catalog, CLOCK_START)
        recovered = store.status(token, CLOCK_START)
        root = DOCUMENTS

    assert recovered["status"] == "completed"
    assert [rel(item["destination"]["path"]) for item in recovered["committed"]] == ["recovered.txt"]
    assert (root / "recovered.txt").read_bytes() == b""


@pytest.mark.parametrize("kind", ["file", "directory", "upload"])
def test_recovery_keeps_lost_publishing_destination_indeterminate_and_never_replays(
    tmp_path, monkeypatch, kind: str
) -> None:
    real_publish_file = file_ops.publish_staged_regular
    real_publish_directory = file_ops.publish_staged_directory
    publication_calls = 0

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        store = world.client.app.state.runtime.operations
        catalog = build_catalog(world.config)
        csrf = _signed(world)

        if kind == "file":
            target = root / "uncertain.txt"
            token = _token(world, csrf)
            action = [_file_action("uncertain.txt")]

            def publish_remove_file(staged):
                nonlocal publication_calls
                real_publish_file(staged)
                publication_calls += 1
                target.unlink()
                raise RuntimeError("simulated external removal after publish")

            monkeypatch.setattr(file_ops, "publish_staged_regular", publish_remove_file)
            with pytest.raises(RuntimeError):
                _mutate(world, csrf, token, action)
            monkeypatch.setattr(file_ops, "publish_staged_regular", real_publish_file)

            replay = lambda: _mutate(world, csrf, token, action)
        elif kind == "directory":
            source = root / "source-tree"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "note.txt").write_bytes(b"synthetic source")
            target = root / "uncertain-tree"
            token = _token(world, csrf)
            action = [{
                "action": "copy",
                "source": {"rootId": ROOT, "path": doc("source-tree")},
                "destination": {"rootId": ROOT, "path": doc("uncertain-tree")},
            }]

            def publish_remove_directory(staged):
                nonlocal publication_calls
                real_publish_directory(staged)
                publication_calls += 1
                shutil.rmtree(target)
                raise RuntimeError("simulated external removal after publish")

            monkeypatch.setattr(file_ops, "publish_staged_directory", publish_remove_directory)
            with pytest.raises(RuntimeError):
                _mutate(world, csrf, token, action)
            monkeypatch.setattr(file_ops, "publish_staged_directory", real_publish_directory)

            replay = lambda: _mutate(world, csrf, token, action)
        else:
            payload = b"synthetic upload body"
            target = root / "uncertain-upload.bin"
            token = _token(world, csrf)
            manifest = {
                "action": "upload",
                "destination": {"rootId": ROOT, "path": D},
                "nameMode": "original",
                "files": [{
                    "originalName": target.name,
                    "contentDigest": hashlib.sha256(payload).hexdigest(),
                    "contentSize": len(payload),
                }],
            }

            def publish_remove_upload(staged):
                nonlocal publication_calls
                real_publish_file(staged)
                publication_calls += 1
                target.unlink()
                raise RuntimeError("simulated external removal after publish")

            monkeypatch.setattr(file_ops, "publish_staged_regular", publish_remove_upload)
            with pytest.raises(RuntimeError):
                _upload(world, csrf, token, manifest, payload)
            monkeypatch.setattr(file_ops, "publish_staged_regular", real_publish_file)

            replay = lambda: _upload(world, csrf, token, manifest, payload)

        store.recover(catalog, CLOCK_START)
        recovered = store.status(token, CLOCK_START)
        replay_response = replay()
        after_replay = store.status(token, CLOCK_START)

    assert publication_calls == 1
    assert recovered["status"] == "indeterminate"
    assert after_replay["status"] == "indeterminate"
    assert replay_response.status_code == 207
    assert replay_response.json()["status"] == "indeterminate"
    assert not target.exists()


def test_post_publication_journal_failure_is_indeterminate_and_replay_recovers_without_republishing(
    tmp_path, monkeypatch
) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        target = DOCUMENTS / "result.txt"
        store = world.client.app.state.runtime.operations
        real_write_record = store._write_record_locked
        failed = False

        def fail_after_publication(record):
            nonlocal failed
            if not failed and any(
                isinstance(item, dict) and item.get("status") == "committed"
                for item in record.get("items", [])
            ):
                failed = True
                raise StateError("simulated ENOSPC after publishing target")
            return real_write_record(record)

        monkeypatch.setattr(store, "_write_record_locked", fail_after_publication)
        first = _mutate(world, csrf, token, [_file_action("result.txt")])
        monkeypatch.setattr(store, "_write_record_locked", real_write_record)
        target_identity = (target.stat().st_dev, target.stat().st_ino)
        replay = _mutate(world, csrf, token, [_file_action("result.txt")])
        final = world.client.get("/api/files/" + token, headers=_headers(world, csrf))

    assert failed
    assert first.status_code == 207
    assert first.json()["status"] == "indeterminate"
    assert replay.status_code == 200
    assert replay.json()["status"] == "completed"
    assert final.status_code == 200 and final.json()["status"] == "completed"
    assert (target.stat().st_dev, target.stat().st_ino) == target_identity


def test_initial_operation_journal_failure_returns_each_uncommitted_item_and_replays_once(
    tmp_path, monkeypatch
) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        root = DOCUMENTS
        store = world.client.app.state.runtime.operations
        real_write = store._write_record_locked
        failed = False

        def fail_first_intent(record):
            nonlocal failed
            items = record.get("items")
            if (
                not failed
                and isinstance(items, list)
                and items
                and isinstance(items[0], dict)
                and items[0].get("status") == "publishing"
            ):
                failed = True
                raise OSError("simulated journal ENOSPC before first publication")
            return real_write(record)

        monkeypatch.setattr(store, "_write_record_locked", fail_first_intent)
        first = _mutate(world, csrf, token, [_file_action("first-intent.txt"), _file_action("later.txt")])
        monkeypatch.setattr(store, "_write_record_locked", real_write)
        assert first.status_code == 503
        assert first.json()["status"] == "failed"
        assert first.json()["committed"] == []
        assert first.json()["indeterminate"] == []
        assert [item["error"] for item in first.json()["uncommitted"]] == [
            "storage_unavailable", "storage_unavailable"
        ]
        assert not (root / "first-intent.txt").exists()
        assert not (root / "later.txt").exists()

        replay = _mutate(world, csrf, token, [_file_action("first-intent.txt"), _file_action("later.txt")])
        first_identity = (root / "first-intent.txt").stat().st_ino
        replay_again = _mutate(world, csrf, token, [_file_action("first-intent.txt"), _file_action("later.txt")])

    assert failed
    assert replay.status_code == replay_again.status_code == 200
    assert replay.json()["status"] == replay_again.json()["status"] == "completed"
    assert (root / "first-intent.txt").stat().st_ino == first_identity
    assert (root / "later.txt").exists()


def test_operation_bind_directory_fsync_failure_is_no_commit_not_conflict(tmp_path, monkeypatch) -> None:
    import hopper_files.state as state_module

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        root = DOCUMENTS
        operation_directory = Path(world.config.state_directory) / "operations"
        real_fsync = state_module.os.fsync
        failed = False

        def fail_after_record_replace(fd: int) -> None:
            nonlocal failed
            try:
                descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                descriptor_path = Path()
            if not failed and descriptor_path == operation_directory:
                failed = True
                raise OSError("simulated directory fsync failure after replace")
            real_fsync(fd)

        monkeypatch.setattr(state_module.os, "fsync", fail_after_record_replace)
        failed_write = _mutate(world, csrf, token, [_file_action("replace-failure.txt")])
        monkeypatch.setattr(state_module.os, "fsync", real_fsync)

        assert failed_write.status_code == 503
        assert failed_write.json()["status"] == "failed"
        assert failed_write.json()["committed"] == []
        assert failed_write.json()["indeterminate"] == []
        assert len(failed_write.json()["uncommitted"]) == 1
        assert failed_write.json()["uncommitted"][0]["error"] == "storage_unavailable"
        assert not (root / "replace-failure.txt").exists()
        replay = _mutate(world, csrf, token, [_file_action("replace-failure.txt")])
        first_identity = (root / "replace-failure.txt").stat().st_ino
        replay_again = _mutate(world, csrf, token, [_file_action("replace-failure.txt")])

    assert failed
    assert replay.status_code == replay_again.status_code == 200
    assert replay.json()["status"] == replay_again.json()["status"] == "completed"
    assert (root / "replace-failure.txt").stat().st_ino == first_identity


def test_storage_failure_during_partial_replay_preserves_prior_commits(tmp_path, monkeypatch) -> None:
    real_publish = file_ops.publish_staged_regular
    publish_calls = 0

    def fail_second_publish(staged):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 2:
            raise AddressRejected("unavailable")
        return real_publish(staged)

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        root = DOCUMENTS
        store = world.client.app.state.runtime.operations
        monkeypatch.setattr(file_ops, "publish_staged_regular", fail_second_publish)
        partial = _mutate(world, csrf, token, [_file_action("kept.txt"), _file_action("resumed.txt")])
        monkeypatch.setattr(file_ops, "publish_staged_regular", real_publish)
        kept_inode = (root / "kept.txt").stat().st_ino
        real_write = store._write_record_locked
        fail_before_resume = True

        def fail_resume_checkpoint(record):
            nonlocal fail_before_resume
            if fail_before_resume and record.get("status") == "partial":
                fail_before_resume = False
                raise StateError("simulated journal failure before replay")
            return real_write(record)

        monkeypatch.setattr(store, "_write_record_locked", fail_resume_checkpoint)
        interrupted_replay = _mutate(world, csrf, token, [_file_action("kept.txt"), _file_action("resumed.txt")])
        monkeypatch.setattr(store, "_write_record_locked", real_write)
        kept_after_failure = (root / "kept.txt").stat().st_ino
        assert not (root / "resumed.txt").exists()
        completed = _mutate(world, csrf, token, [_file_action("kept.txt"), _file_action("resumed.txt")])

    assert partial.status_code == 207
    assert partial.json()["status"] == "partial"
    assert interrupted_replay.status_code == 207
    assert interrupted_replay.json()["status"] == "partial"
    assert [rel(item["destination"]["path"]) for item in interrupted_replay.json()["committed"]] == ["kept.txt"]
    assert [rel(item["destination"]["path"]) for item in interrupted_replay.json()["uncommitted"]] == ["resumed.txt"]
    assert interrupted_replay.json()["uncommitted"][0]["error"] == "storage_unavailable"
    assert kept_inode == kept_after_failure == (root / "kept.txt").stat().st_ino
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    assert (root / "kept.txt").stat().st_ino == kept_inode
    assert (root / "resumed.txt").exists()


def test_upload_bind_journal_failure_reports_no_commit_and_allows_safe_replay(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        root = DOCUMENTS
        store = world.client.app.state.runtime.operations
        payload = b"synthetic upload bytes"
        manifest = {
            "action": "upload",
            "destination": {"rootId": ROOT, "path": D},
            "nameMode": "original",
            "files": [{
                "originalName": "journal-upload.bin",
                "contentDigest": hashlib.sha256(payload).hexdigest(),
                "contentSize": len(payload),
            }],
        }
        real_write = store._write_record_locked
        failed = False

        def fail_upload_bind(record):
            nonlocal failed
            if (
                not failed
                and record.get("status") == "pending"
                and record.get("fingerprint") is not None
                and record.get("items") == []
            ):
                failed = True
                raise OSError("simulated upload journal ENOSPC before receive")
            return real_write(record)

        monkeypatch.setattr(store, "_write_record_locked", fail_upload_bind)
        first = _upload(world, csrf, token, manifest, payload)
        monkeypatch.setattr(store, "_write_record_locked", real_write)
        assert first.status_code == 503
        assert first.json()["status"] == "failed"
        assert first.json()["committed"] == []
        assert first.json()["indeterminate"] == []
        assert len(first.json()["uncommitted"]) == 1
        assert first.json()["uncommitted"][0]["destination"]["path"] == doc("journal-upload.bin")
        assert first.json()["uncommitted"][0]["error"] == "storage_unavailable"
        assert not (root / "journal-upload.bin").exists()

        replay = _upload(world, csrf, token, manifest, payload)
        identity = (root / "journal-upload.bin").stat().st_ino
        replay_again = _upload(world, csrf, token, manifest, payload)

    assert failed
    assert replay.status_code == replay_again.status_code == 200
    assert replay.json()["status"] == replay_again.json()["status"] == "completed"
    assert (root / "journal-upload.bin").read_bytes() == payload
    assert (root / "journal-upload.bin").stat().st_ino == identity


def test_copy_file_and_directory_tree_preserve_sources_use_new_object_modes_and_replay(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        source_file = root / "source.bin"
        source_file.write_bytes(b"file copy" * 8192)
        source_file.chmod(0o755)
        source_tree = root / "tree"
        (source_tree / "nested").mkdir(parents=True)
        (source_tree / "empty").mkdir()
        (source_tree / "nested" / "note.md").write_text("ordinary copy\n", encoding="utf-8")
        (source_tree / "nested" / "note.md").chmod(0o644)
        (source_tree / "empty-name").write_bytes(b"")
        token = _token(world, csrf)
        actions = [
            {"action": "copy", "source": {"rootId": ROOT, "path": doc("source.bin")}, "destination": {"rootId": ROOT, "path": doc("copy.bin")}},
            {"action": "copy", "source": {"rootId": ROOT, "path": doc("tree")}, "destination": {"rootId": ROOT, "path": doc("tree-copy")}},
        ]
        copied = _mutate(world, csrf, token, actions)
        replay = _mutate(world, csrf, token, actions)

    assert copied.status_code == replay.status_code == 200
    assert copied.json()["status"] == replay.json()["status"] == "completed"
    assert len(copied.json()["committed"]) == 2
    assert (root / "source.bin").read_bytes() == (root / "copy.bin").read_bytes()
    assert stat.S_IMODE((root / "copy.bin").stat().st_mode) == terminal_mode("file")
    assert (root / "tree" / "nested" / "note.md").read_bytes() == (root / "tree-copy" / "nested" / "note.md").read_bytes()
    assert (root / "tree-copy" / "empty").is_dir()
    assert (root / "tree-copy" / "empty-name").read_bytes() == b""
    assert stat.S_IMODE((root / "tree-copy").stat().st_mode) == terminal_mode("directory")
    assert stat.S_IMODE((root / "tree-copy" / "nested" / "note.md").stat().st_mode) == terminal_mode("file")


@pytest.mark.parametrize(
    ("mutate_after_publish", "expected_status"),
    [(False, "completed"), (True, "indeterminate")],
)
def test_directory_copy_recovery_verifies_tree_digest_after_publication(
    tmp_path,
    monkeypatch,
    mutate_after_publish: bool,
    expected_status: str,
) -> None:
    real_publish = file_ops.publish_staged_directory
    crashed = False

    def publish_then_stop(stage):
        nonlocal crashed
        identity = real_publish(stage)
        if not crashed:
            crashed = True
            if mutate_after_publish:
                (root / "copy" / "nested" / "file.bin").write_bytes(b"different")
            raise KeyboardInterrupt("simulated process stop after directory publication")
        return identity

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        source = root / "source"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "file.bin").write_bytes(b"original")
        catalog = build_catalog(world.client.app.state.runtime.config)
        store = world.client.app.state.runtime.operations
        token = store.issue(CLOCK_START)["operationToken"]
        action = [{"action": "copy", "source": {"rootId": ROOT, "path": doc("source")}, "destination": {"rootId": ROOT, "path": doc("copy")}}]
        monkeypatch.setattr(file_ops, "publish_staged_directory", publish_then_stop)
        with pytest.raises(KeyboardInterrupt):
            store.submit(token, action, catalog, CLOCK_START)
        monkeypatch.setattr(file_ops, "publish_staged_directory", real_publish)
        store.recover(catalog, CLOCK_START)
        result = store.status(token, CLOCK_START)

    assert crashed
    assert result["status"] == expected_status
    if mutate_after_publish:
        assert result["indeterminate"][0]["error"] == "publication_unknown"
    else:
        assert result["committed"][0]["destination"]["path"] == doc("copy")


def test_directory_copy_copies_a_hard_linked_file_as_a_new_object(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        source = root / "source"
        source.mkdir()
        (root / "outside.txt").write_bytes(b"hard linked content")
        os.link(root / "outside.txt", source / "linked.txt")
        result = _mutate(
            world,
            csrf,
            _token(world, csrf),
            [{"action": "copy", "source": {"rootId": ROOT, "path": doc("source")}, "destination": {"rootId": ROOT, "path": doc("copy")}}],
        )

    # HF-NAV-002: a multiply linked file follows the ordinary rules; a copy is a new object.
    assert result.status_code == 200, result.text
    copied = (root / "copy" / "linked.txt").stat()
    assert (root / "copy" / "linked.txt").read_bytes() == b"hard linked content"
    assert copied.st_nlink == 1
    assert copied.st_ino != (root / "outside.txt").stat().st_ino
    assert (root / "outside.txt").stat().st_nlink == 2


def test_directory_copy_rejects_symbolic_links_without_publishing_a_partial_tree(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        source = root / "source"
        source.mkdir()
        (source / "ordinary.txt").write_bytes(b"safe")
        (source / "linked.txt").symlink_to(root / "outside.txt")
        token = _token(world, csrf)
        result = _mutate(
            world,
            csrf,
            token,
            [{"action": "copy", "source": {"rootId": ROOT, "path": doc("source")}, "destination": {"rootId": ROOT, "path": doc("copy")}}],
        )

    assert result.status_code == 403
    assert result.json()["status"] == "failed"
    assert result.json()["uncommitted"][0]["error"] == "forbidden"
    assert not (root / "copy").exists()
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_zip_and_directory_copy_reject_special_files_before_publication(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        source = root / "special-source"
        source.mkdir()
        (source / "regular.txt").write_bytes(b"ordinary")
        os.mkfifo(source / "pipe")
        zip_token = _token(world, csrf)
        zipped = _mutate(
            world,
            csrf,
            zip_token,
            [{"action": "zip", "sources": [{"rootId": ROOT, "path": doc("special-source")}], "destination": {"rootId": ROOT, "path": doc("special.zip")}}],
        )
        copy_token = _token(world, csrf)
        copied = _mutate(
            world,
            csrf,
            copy_token,
            [{"action": "copy", "source": {"rootId": ROOT, "path": doc("special-source")}, "destination": {"rootId": ROOT, "path": doc("special-copy")}}],
        )

    assert zipped.status_code == copied.status_code == 403
    assert zipped.json()["uncommitted"][0]["error"] == copied.json()["uncommitted"][0]["error"] == "forbidden"
    assert not (root / "special.zip").exists()
    assert not (root / "special-copy").exists()


def test_new_objects_match_the_terminal_in_setgid_and_default_acl_directories(tmp_path) -> None:
    """HF-NAV-010: owner, group, mode, and ACL equal what a terminal creates there."""
    def snapshot(path: Path) -> tuple[int, int, int, bytes | None]:
        info = path.stat()
        try:
            acl = os.getxattr(path, "system.posix_acl_access")
        except OSError:
            acl = None
        return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), acl

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        setgid = root / "setgid"
        setgid.mkdir()
        os.chmod(setgid, 0o2770)
        acl = root / "acl"
        acl.mkdir()
        set_default_acl(acl, os.getgid())
        for directory in (setgid, acl):
            created = _mutate(
                world,
                csrf,
                _token(world, csrf),
                [
                    {"action": "create", "kind": "file", "nameMode": "exact", "destination": {"rootId": ROOT, "path": doc(f"{directory.name}/app.txt")}},
                    {"action": "create", "kind": "directory", "nameMode": "exact", "destination": {"rootId": ROOT, "path": doc(f"{directory.name}/app-dir")}},
                ],
            )
            assert created.status_code == 200, created.text
            (directory / "terminal.txt").touch()
            (directory / "terminal-dir").mkdir()
            assert snapshot(directory / "app.txt") == snapshot(directory / "terminal.txt")
            assert snapshot(directory / "app-dir") == snapshot(directory / "terminal-dir")
        assert stat.S_IMODE((setgid / "app-dir").stat().st_mode) & stat.S_ISGID


def test_directory_copy_detects_source_file_change_and_removes_only_its_stage(tmp_path, monkeypatch) -> None:
    real_write = file_ops._write_all
    changed = False

    def write_then_change_source(fd: int, block: bytes) -> None:
        nonlocal changed
        real_write(fd, block)
        if not changed:
            changed = True
            (source / "file.bin").write_bytes(b"mutated" * 1024)

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        source = root / "source"
        source.mkdir()
        (source / "file.bin").write_bytes(b"original" * 1024)
        token = _token(world, csrf)
        monkeypatch.setattr(file_ops, "_write_all", write_then_change_source)
        result = _mutate(
            world,
            csrf,
            token,
            [{"action": "copy", "source": {"rootId": ROOT, "path": doc("source")}, "destination": {"rootId": ROOT, "path": doc("copy")}}],
        )

    assert changed
    assert result.status_code == 409
    assert result.json()["status"] == "failed"
    assert result.json()["uncommitted"][0]["error"] == "conflict"
    assert not (root / "copy").exists()
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_upload_streams_exact_original_names_and_generated_collision_suffixes(tmp_path) -> None:
    payloads = [b"a" * 64_000, b"second-file"]
    hashes = [hashlib.sha256(payload).hexdigest() for payload in payloads]
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD, timezone="UTC") as world:
        csrf = _signed(world)
        root = DOCUMENTS
        (root / "draft.txt").write_bytes(b"old")
        token = _token(world, csrf)
        manifest = {
            "action": "upload",
            "destination": {"rootId": ROOT, "path": D},
            "nameMode": "original",
            "files": [
                {"originalName": "draft.txt", "contentDigest": hashes[0], "contentSize": len(payloads[0])}
            ],
        }
        collision = _upload(world, csrf, token, manifest, payloads[0])
        generated_token = _token(world, csrf)
        generated_manifest = {
            "action": "upload",
            "destination": {"rootId": ROOT, "path": D},
            "nameMode": "generated",
            "files": [
                {"subject": "Relatório final", "extension": "csv", "contentDigest": hashes[i], "contentSize": len(payloads[i])}
                for i in range(2)
            ],
        }
        (root / "relatorio-final-20231114-221320.csv").write_bytes(b"collision")
        uploaded = _upload(world, csrf, generated_token, generated_manifest, b"".join(payloads))
        literal_token = _token(world, csrf)
        literal_manifest = {
            "action": "upload",
            "destination": {"rootId": ROOT, "path": D},
            "nameMode": "original",
            "files": [{
                "originalName": " Mixed Ω.txt ",
                "contentDigest": hashes[1],
                "contentSize": len(payloads[1]),
            }],
        }
        literal_upload = _upload(world, csrf, literal_token, literal_manifest, payloads[1])

    assert collision.status_code == 409
    assert (root / "draft.txt").read_bytes() == b"old"
    assert uploaded.status_code == 200
    assert uploaded.json()["status"] == "completed"
    names = [rel(item["destination"]["path"]) for item in uploaded.json()["committed"]]
    assert names == [
        "relatorio-final-20231114-221320-2.csv",
        "relatorio-final-20231114-221320-3.csv",
    ]
    assert [(root / name).read_bytes() for name in names] == payloads
    assert all(stat.S_IMODE((root / name).stat().st_mode) == terminal_mode("file") for name in names)
    assert literal_upload.status_code == 200
    assert (root / " Mixed Ω.txt ").read_bytes() == payloads[1]


def test_upload_partial_commit_is_explicit_and_same_manifest_resumes_remaining_files(tmp_path, monkeypatch) -> None:
    real_publish = file_ops.publish_staged_regular
    calls = 0
    payloads = [b"first payload", b"second payload"]

    def fail_second(staged):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Keep the stage to prove the item stayed unpublished.
            raise AddressRejected("unavailable")
        return real_publish(staged)

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        token = _token(world, csrf)
        manifest = {
            "action": "upload",
            "destination": {"rootId": ROOT, "path": D},
            "nameMode": "original",
            "files": [
                {
                    "originalName": f"upload-{index}.bin",
                    "contentDigest": hashlib.sha256(payload).hexdigest(),
                    "contentSize": len(payload),
                }
                for index, payload in enumerate(payloads, start=1)
            ],
        }
        monkeypatch.setattr(file_ops, "publish_staged_regular", fail_second)
        first = _upload(world, csrf, token, manifest, b"".join(payloads))
        monkeypatch.setattr(file_ops, "publish_staged_regular", real_publish)
        replay = _upload(world, csrf, token, manifest, b"".join(payloads))

    assert first.status_code == 207
    assert first.json()["status"] == "partial"
    assert [rel(item["destination"]["path"]) for item in first.json()["committed"]] == ["upload-1.bin"]
    assert [(item["index"], item["error"]) for item in first.json()["uncommitted"]] == [(1, "unavailable")]
    assert replay.status_code == 200
    assert replay.json()["status"] == "completed"
    assert (root / "upload-1.bin").read_bytes() == payloads[0]
    assert (root / "upload-2.bin").read_bytes() == payloads[1]
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_upload_lease_prevents_concurrent_replay_and_stage_overwrite_then_recovers(tmp_path) -> None:
    payload = b"one active request owns this stage"
    manifest = {
        "action": "upload",
        "destination": {"rootId": ROOT, "path": D},
        "nameMode": "original",
        "files": [{
            "originalName": "race.bin",
            "contentDigest": hashlib.sha256(payload).hexdigest(),
            "contentSize": len(payload),
        }],
    }
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        root = DOCUMENTS
        store = world.client.app.state.runtime.operations
        catalog = build_catalog(world.config)
        lease = store.acquire_upload_lease(token, CLOCK_START)
        started = store.begin_upload(token, manifest, catalog, CLOCK_START, lease=lease)
        item = started["action"]["files"][0]
        first_writer = root_ops.begin_stage_regular(catalog, ROOT, item["destination"]["path"])
        info = os.fstat(first_writer.file_fd)
        store.note_upload_stage(
            token,
            0,
            item,
            first_writer.temporary_name,
            (info.st_dev, info.st_ino),
        )
        first_stage_path = root / first_writer.temporary_name

        with ThreadPoolExecutor(max_workers=1) as executor:
            replay = executor.submit(store.begin_upload, token, manifest, catalog, CLOCK_START)
            with pytest.raises(OperationError) as concurrent:
                replay.result(timeout=3)
        assert concurrent.value.code == "conflict"

        second_writer = root_ops.begin_stage_regular(catalog, ROOT, item["destination"]["path"])
        second_info = os.fstat(second_writer.file_fd)
        try:
            with pytest.raises(OperationError) as duplicate_stage:
                store.note_upload_stage(
                    token,
                    0,
                    item,
                    second_writer.temporary_name,
                    (second_info.st_dev, second_info.st_ino),
                )
            assert duplicate_stage.value.code == "conflict"
        finally:
            second_writer.abort()

        store.recover(catalog, CLOCK_START)
        assert first_stage_path.is_file()
        assert store.status(token, CLOCK_START)["status"] == "pending"

        # Model process loss: close descriptors but leave the journaled stage
        # name for recovery to remove by its recorded inode identity.
        os.close(first_writer.file_fd)
        first_writer.file_fd = -1
        os.close(first_writer.parent_fd)
        first_writer.parent_fd = -1
        lease.close()
        store.recover(catalog, CLOCK_START)
        recovered = store.status(token, CLOCK_START)
        resumed = store.begin_upload(token, manifest, catalog, CLOCK_START)

    assert not first_stage_path.exists()
    assert recovered["status"] == "failed"
    assert resumed["stored"] is False
    assert not (root / "race.bin").exists()
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_upload_http_replay_conflicts_while_the_first_body_is_streaming(tmp_path) -> None:
    payload = b"stream ownership stays exclusive"
    manifest = {
        "action": "upload",
        "destination": {"rootId": ROOT, "path": D},
        "nameMode": "original",
        "files": [{
            "originalName": "http-race.bin",
            "contentDigest": hashlib.sha256(payload).hexdigest(),
            "contentSize": len(payload),
        }],
    }
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        cookie = world.client.cookies.get(world.config.cookie_name)
        assert cookie is not None
        headers = {
            "origin": world.config.origin,
            "cookie": f"{world.config.cookie_name}={cookie}",
            "x-csrf-token": csrf,
            "x-hopper-operation-token": token,
            "x-hopper-upload-manifest": _manifest(manifest),
            "content-type": "application/octet-stream",
        }

        async def exercise():
            first_chunk_sent = asyncio.Event()
            continue_body = asyncio.Event()

            async def body():
                yield payload[:8]
                first_chunk_sent.set()
                await continue_body.wait()
                yield payload[8:]

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=world.client.app),
                base_url=world.config.origin,
                trust_env=False,
            ) as client:
                first_task = asyncio.create_task(
                    client.post("/api/files/upload", content=body(), headers=headers)
                )
                try:
                    await asyncio.wait_for(first_chunk_sent.wait(), timeout=3)
                    replay = await asyncio.wait_for(
                        client.post("/api/files/upload", content=payload, headers=headers),
                        timeout=3,
                    )
                finally:
                    continue_body.set()
                first = await asyncio.wait_for(first_task, timeout=3)
                return first, replay

        first, replay = asyncio.run(exercise())
        root = DOCUMENTS

    assert replay.status_code == 409
    assert replay.json() == {"error": "conflict"}
    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert (root / "http-race.bin").read_bytes() == payload
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_repeated_upload_begin_recovers_recorded_stage_before_rebinding_it(tmp_path) -> None:
    payload = b"stale upload stage"
    manifest = {
        "action": "upload",
        "destination": {"rootId": ROOT, "path": D},
        "nameMode": "original",
        "files": [{
            "originalName": "replay.bin",
            "contentDigest": hashlib.sha256(payload).hexdigest(),
            "contentSize": len(payload),
        }],
    }
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        root = DOCUMENTS
        store = world.client.app.state.runtime.operations
        catalog = build_catalog(world.config)
        first = store.begin_upload(token, manifest, catalog, CLOCK_START)
        first_item = first["action"]["files"][0]
        first_writer = root_ops.begin_stage_regular(catalog, ROOT, doc("replay.bin"))
        first_info = os.fstat(first_writer.file_fd)
        store.note_upload_stage(
            token,
            0,
            first_item,
            first_writer.temporary_name,
            (first_info.st_dev, first_info.st_ino),
        )
        first_path = root / first_writer.temporary_name

        second = store.begin_upload(token, manifest, catalog, CLOCK_START)
        assert not first_path.exists()
        first_writer.abort()
        second_item = second["action"]["files"][0]
        second_writer = root_ops.begin_stage_regular(catalog, ROOT, doc("replay.bin"))
        second_info = os.fstat(second_writer.file_fd)
        store.note_upload_stage(
            token,
            0,
            second_item,
            second_writer.temporary_name,
            (second_info.st_dev, second_info.st_ino),
        )
        second_path = root / second_writer.temporary_name
        os.close(second_writer.file_fd)
        second_writer.file_fd = -1
        os.close(second_writer.parent_fd)
        second_writer.parent_fd = -1
        store.recover(catalog, CLOCK_START)

    assert not first_path.exists()
    assert not second_path.exists()
    assert not (root / "replay.bin").exists()
    assert store.status(token, CLOCK_START)["status"] == "failed"


def test_upload_manifest_limits_and_mismatched_stream_leave_no_target(tmp_path) -> None:
    digest = "0" * 64
    valid = {
        "action": "upload",
        "destination": {"rootId": ROOT, "path": D},
        "nameMode": "original",
        "files": [{"originalName": "empty.bin", "contentDigest": hashlib.sha256(b"").hexdigest(), "contentSize": 0}],
    }
    assert _normalize_upload(valid)["files"][0]["contentSize"] == 0
    too_many = {**valid, "files": [{"originalName": f"{i}.bin", "contentDigest": digest, "contentSize": 0} for i in range(21)]}
    too_large = {**valid, "files": [{"originalName": "huge.bin", "contentDigest": digest, "contentSize": UPLOAD_MAX_FILE + 1}]}
    over_total = {
        **valid,
        "files": [
            {"originalName": "a.bin", "contentDigest": digest, "contentSize": UPLOAD_MAX_FILE},
            {"originalName": "b.bin", "contentDigest": digest, "contentSize": UPLOAD_MAX_TOTAL - UPLOAD_MAX_FILE + 1},
        ],
    }
    for payload in (too_many, too_large, over_total):
        with pytest.raises(OperationError) as rejected:
            _normalize_upload(payload)
        assert rejected.value.code == "limit"

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        manifest = {
            **valid,
            "files": [{"originalName": "mismatch.bin", "contentDigest": hashlib.sha256(b"expected").hexdigest(), "contentSize": 8}],
        }
        mismatch = _upload(world, csrf, token, manifest, b"changed!")
        status = world.client.get("/api/files/" + token, headers=_headers(world, csrf))
        root = DOCUMENTS

    assert mismatch.status_code == 422
    assert status.status_code == 200
    assert status.json()["status"] == "failed"
    assert not (root / "mismatch.bin").exists()
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_known_capacity_limit_returns_413_without_starting_upload(tmp_path, monkeypatch) -> None:
    payload = b"capacity preflight"
    manifest = {
        "action": "upload",
        "destination": {"rootId": ROOT, "path": D},
        "nameMode": "original",
        "files": [{
            "originalName": "too-large-for-capacity.bin",
            "contentDigest": hashlib.sha256(payload).hexdigest(),
            "contentSize": len(payload),
        }],
    }
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        real_capacity = file_ops.destination_capacity

        def exhausted(_catalog, root_id: str, destination: str) -> tuple[int, int]:
            device, _available = real_capacity(build_catalog(world.config), root_id, destination)
            return device, 0

        monkeypatch.setattr(file_ops, "destination_capacity", exhausted)
        response = _upload(world, csrf, token, manifest, payload)
        status = world.client.get("/api/files/" + token, headers=_headers(world, csrf))
        root = DOCUMENTS

    assert response.status_code == 413
    assert response.json() == {"error": "limit_exceeded"}
    assert status.status_code == 200 and status.json()["status"] == "failed"
    assert not (root / "too-large-for-capacity.bin").exists()
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_upload_enospc_is_reported_without_publishing_or_leaving_stage(tmp_path, monkeypatch) -> None:
    def fail_stage_write(_writer, _block: bytes) -> None:
        raise OSError(errno.ENOSPC, "synthetic full filesystem")

    payload = b"streamed bytes"
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        manifest = {
            "action": "upload",
            "destination": {"rootId": ROOT, "path": D},
            "nameMode": "original",
            "files": [{
                "originalName": "full-disk.bin",
                "contentDigest": hashlib.sha256(payload).hexdigest(),
                "contentSize": len(payload),
            }],
        }
        monkeypatch.setattr(root_ops.StageWriter, "write", fail_stage_write)
        result = _upload(world, csrf, token, manifest, payload)
        status = world.client.get("/api/files/" + token, headers=_headers(world, csrf))
        root = DOCUMENTS

    assert result.status_code == 413
    assert status.status_code == 200
    assert status.json()["status"] == "failed"
    assert status.json()["uncommitted"][0]["error"] == "limit"
    assert not (root / "full-disk.bin").exists()
    assert not any(path.name.startswith(".hopper-stage-") for path in root.iterdir())


def test_zip_create_extract_and_traversal_rejection(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        (root / "folder").mkdir()
        (root / "folder" / "a.md").write_bytes(b"hello")
        token = _token(world, csrf)
        zipped = _mutate(
            world,
            csrf,
            token,
            [{"action": "zip", "sources": [{"rootId": ROOT, "path": doc("folder")}], "destination": {"rootId": ROOT, "path": doc("pack.zip")}}],
        )
        (root / "unpacked").mkdir()
        extraction_token = _token(world, csrf)
        extracted = _mutate(
            world,
            csrf,
            extraction_token,
            [{"action": "extract", "source": {"rootId": ROOT, "path": doc("pack.zip")}, "destination": {"rootId": ROOT, "path": doc("unpacked")}}],
        )
        with zipfile.ZipFile(root / "evil.zip", "w") as archive:
            archive.writestr("../outside.txt", b"escape")
        evil_token = _token(world, csrf)
        evil = _mutate(
            world,
            csrf,
            evil_token,
            [{"action": "extract", "source": {"rootId": ROOT, "path": doc("evil.zip")}, "destination": {"rootId": ROOT, "path": doc("unpacked")}}],
        )

    assert zipped.status_code == extracted.status_code == 200
    assert extracted.json()["status"] == "completed"
    assert (root / "unpacked" / "folder" / "a.md").read_bytes() == b"hello"
    assert evil.status_code == 422
    assert evil.json()["status"] == "failed"
    assert evil.json()["uncommitted"][0]["error"] == "invalid"
    assert not (root.parent / "outside.txt").exists()


def test_zip_creation_forces_zip64_before_streaming_large_member(tmp_path, monkeypatch) -> None:
    payload = b"z" * (1024 * 1024 + 1)
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        (root / "large.bin").write_bytes(payload)
        monkeypatch.setattr(file_ops.zipfile, "ZIP64_LIMIT", 1024 * 1024)
        token = _token(world, csrf)
        zipped = _mutate(
            world,
            csrf,
            token,
            [{"action": "zip", "sources": [{"rootId": ROOT, "path": doc("large.bin")}], "destination": {"rootId": ROOT, "path": doc("large.zip")}}],
        )
        archive_path = root / "large.zip"
        assert zipped.status_code == 200, zipped.text
        raw_archive = archive_path.read_bytes()
        local_header = struct.unpack_from("<IHHHHHIIIHH", raw_archive)
        assert local_header[0] == 0x04034B50
        assert local_header[1] >= 45
        assert local_header[7:9] == (0xFFFFFFFF, 0xFFFFFFFF)
        extra_offset = 30 + local_header[9]
        extra_id, extra_size, uncompressed_size, compressed_size = struct.unpack_from(
            "<HHQQ", raw_archive, extra_offset
        )
        assert extra_id == 0x0001
        assert extra_size == 16
        assert uncompressed_size == compressed_size == 0
        with zipfile.ZipFile(archive_path) as archive:
            info = archive.getinfo("large.bin")
            with archive.open(info) as member:
                assert member.read() == payload
            assert info.file_size == len(payload)


def test_zip_extraction_partial_result_resumes_only_the_uncommitted_member(tmp_path, monkeypatch) -> None:
    real_publish = file_ops.publish_staged_regular
    calls = 0

    def fail_second(staged):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Keep the stage to prove the member stayed unpublished.
            raise AddressRejected("unavailable")
        return real_publish(staged)

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        (root / "unpacked").mkdir()
        with zipfile.ZipFile(root / "pair.zip", "w") as archive:
            archive.writestr("a.txt", b"first")
            archive.writestr("b.txt", b"second")
        token = _token(world, csrf)
        action = [{"action": "extract", "source": {"rootId": ROOT, "path": doc("pair.zip")}, "destination": {"rootId": ROOT, "path": doc("unpacked")}}]
        monkeypatch.setattr(file_ops, "publish_staged_regular", fail_second)
        first = _mutate(world, csrf, token, action)
        monkeypatch.setattr(file_ops, "publish_staged_regular", real_publish)
        replay = _mutate(world, csrf, token, action)

    assert first.status_code == 207
    assert first.json()["status"] == "partial"
    assert [rel(item["destination"]["path"]) for item in first.json()["committed"]] == ["unpacked/a.txt"]
    assert [(item["subIndex"], item["error"]) for item in first.json()["uncommitted"]] == [(1, "unavailable")]
    assert replay.status_code == 200
    assert replay.json()["status"] == "completed"
    assert (root / "unpacked" / "a.txt").read_bytes() == b"first"
    assert (root / "unpacked" / "b.txt").read_bytes() == b"second"


def test_zip_rejects_duplicate_members_links_depth_and_expansion_ratio(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = DOCUMENTS
        duplicate = root / "duplicate.zip"
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("same.txt", b"a")
            archive.writestr("same.txt", b"b")
        linked = root / "link.zip"
        link_info = zipfile.ZipInfo("link")
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(linked, "w") as archive:
            archive.writestr(link_info, "target")
        special = root / "special.zip"
        special_info = zipfile.ZipInfo("pipe")
        special_info.create_system = 3
        special_info.external_attr = (stat.S_IFIFO | 0o600) << 16
        with zipfile.ZipFile(special, "w") as archive:
            archive.writestr(special_info, b"")
        too_deep = root / "deep.zip"
        with zipfile.ZipFile(too_deep, "w") as archive:
            archive.writestr("/".join(["d"] * 33 + ["file.txt"]), b"x")
        expansion = root / "ratio.zip"
        with zipfile.ZipFile(expansion, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("zeros.txt", b"\0" * 1_000_000)
        catalog = build_catalog(world.client.app.state.runtime.config)
        for name in ("duplicate.zip", "link.zip", "special.zip", "deep.zip"):
            with pytest.raises(OperationError):
                file_ops._extract_plan(catalog, {"rootId": ROOT, "path": doc(name)})
        with pytest.raises(OperationError) as rejected:
            file_ops._extract_plan(catalog, {"rootId": ROOT, "path": doc("ratio.zip")})

    assert rejected.value.code == "limit"


def test_zip_source_set_rejects_one_entry_above_the_count_limit() -> None:
    with pytest.raises(OperationError) as rejected:
        _normalize_actions([{
            "action": "zip",
            "sources": [{"rootId": ROOT, "path": doc("same.bin")}] * (ZIP_SOURCE_MAX_ENTRIES + 1),
            "destination": {"rootId": ROOT, "path": doc("archive.zip")},
        }])
    assert rejected.value.code == "limit"


@pytest.mark.parametrize("limit_case", ["entry_size", "total_size", "entry_count"])
def test_zip_extraction_metadata_limits_reject_without_large_payloads(tmp_path, limit_case: str) -> None:
    if limit_case == "entry_size":
        size = ZIP_EXTRACT_MAX_FILE + 1
        entries = [("large.bin", (size + 119) // 120, size)]
    elif limit_case == "total_size":
        count = ZIP_EXTRACT_MAX_BYTES // ZIP_EXTRACT_MAX_FILE + 1
        compressed = (ZIP_EXTRACT_MAX_FILE + 119) // 120
        entries = [(f"part-{index}.bin", compressed, ZIP_EXTRACT_MAX_FILE) for index in range(count)]
    else:
        entries = [(f"item-{index}.bin", 0, 0) for index in range(ZIP_SOURCE_MAX_ENTRIES + 1)]

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = _signed(world)
        root = DOCUMENTS
        (root / "unpacked").mkdir()
        (root / "metadata.zip").write_bytes(_forged_zip(entries))
        token = _token(world, csrf)
        result = _mutate(
            world,
            csrf,
            token,
            [{"action": "extract", "source": {"rootId": ROOT, "path": doc("metadata.zip")}, "destination": {"rootId": ROOT, "path": doc("unpacked")}}],
        )

    assert result.status_code == 413
    assert result.json()["status"] == "failed"
    assert result.json()["uncommitted"][0]["error"] == "limit"
    assert list((root / "unpacked").iterdir()) == []


def test_expired_token_cannot_start_and_terminal_journal_permissions_are_private(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path, clock, password=PASSWORD) as world:
        csrf = _signed(world)
        token = _token(world, csrf)
        clock.advance(file_ops.TOKEN_LIFETIME_SECONDS)
        csrf = _signed(world)
        expired = _mutate(world, csrf, token, [_file_action("late.txt")])
        expired_status = world.client.get("/api/files/" + token, headers=_headers(world, csrf))
        journal_dir = Path(world.config.state_directory) / "operations"
        paths = list(journal_dir.glob("op-*.json"))
        mode = stat.S_IMODE(paths[0].stat().st_mode)
        clock.advance(file_ops.TERMINAL_RETENTION_SECONDS - 1)
        csrf = _signed(world)
        retained = world.client.get("/api/files/" + token, headers=_headers(world, csrf))
        clock.advance(1)
        _token(world, csrf)
        collected = world.client.get("/api/files/" + token, headers=_headers(world, csrf))
        after_collection = _mutate(world, csrf, token, [_file_action("still-late.txt")])
        operation_directory_mode = stat.S_IMODE(journal_dir.stat().st_mode)

    assert expired.status_code == 410
    assert expired_status.status_code == 200
    assert expired_status.json()["status"] == "failed"
    assert mode == 0o600
    assert retained.status_code == 200
    assert collected.status_code == after_collection.status_code == 410
    assert operation_directory_mode == 0o700


def test_operation_token_issuance_enforces_the_active_record_cap(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        store = world.client.app.state.runtime.operations
        tokens = [store.issue(CLOCK_START)["operationToken"] for _ in range(file_ops.MAX_ACTIVE_OPERATIONS)]
        with pytest.raises(OperationError) as rejected:
            store.issue(CLOCK_START)
        journal_count = len(list((Path(world.config.state_directory) / "operations").glob("op-*.json")))

    assert len(tokens) == file_ops.MAX_ACTIVE_OPERATIONS
    assert rejected.value.code == "limit"
    assert journal_count == file_ops.MAX_ACTIVE_OPERATIONS
