from __future__ import annotations

import concurrent.futures
import errno
import os
import stat
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import hopper_files.editor as editor_module
from conftest import CLOCK_START, PASSWORD, ManualClock, boot, D, DOCUMENTS, ROOT, doc


def _signed(world):
    login = world.post_login(PASSWORD)
    assert login.status_code == 200
    return {"origin": world.config.origin, "x-csrf-token": login.json()["csrfToken"]}


def _address(path: str = "note.md"):
    return {"rootId": ROOT, "path": doc(path)}


def _documents(world) -> Path:
    return DOCUMENTS


def test_file_revision_save_and_stale_conflict_preserve_metadata(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "note.md"
        path.write_text("# antes\n", encoding="utf-8")
        os.chmod(path, 0o640)
        before = path.stat()
        headers = _signed(world)
        first = world.client.get("/api/file", params=_address())
        assert first.status_code == 200, first.text
        assert first.json()["content"] == "# antes\n"
        assert first.json()["version"].startswith("v2.")
        assert first.json()["editable"] is True
        assert first.json()["readOnlyReason"] is None
        assert world.client.put("/api/file", json={"content": "sem precondição"}, headers=headers, params=_address()).status_code == 428

        saved = world.client.put(
            "/api/file", params=_address(), headers=headers,
            json={"baseVersion": first.json()["version"], "content": "# depois\n"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["content"] == "# depois\n"
        assert saved.json()["savedVersion"] != first.json()["version"]
        after = path.stat()
        assert path.read_bytes() == b"# depois\n"
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)
        )

        stale = world.client.put(
            "/api/file", params=_address(), headers=headers,
            json={"baseVersion": first.json()["version"], "content": "tentativa obsoleta"},
        )
        assert stale.status_code == 409
        assert stale.json()["current"]["content"] == "# depois\n"
        assert path.read_bytes() == b"# depois\n"


def test_revision_is_bound_to_path_and_detects_uncooperative_writer(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _documents(world)
        first_path = root / "one.md"
        second_path = root / "two.md"
        first_path.write_bytes(b"same")
        second_path.write_bytes(b"same")
        headers = _signed(world)
        first = world.client.get("/api/file", params=_address("one.md")).json()
        second = world.client.get("/api/file", params=_address("two.md")).json()
        assert first["version"] != second["version"]
        first_path.write_bytes(b"diff")
        conflict = world.client.put(
            "/api/file", params=_address("one.md"), headers=headers,
            json={"baseVersion": first["version"], "content": "local edit"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["current"]["content"] == "diff"
        assert first_path.read_bytes() == b"diff"
        replacement = root / "replacement.tmp"
        replacement.write_bytes(b"diff")
        os.replace(replacement, first_path)
        replaced = world.client.put(
            "/api/file", params=_address("one.md"), headers=headers,
            json={"baseVersion": conflict.json()["current"]["version"], "content": "still stale"},
        )
        assert replaced.status_code == 409
        assert replaced.json()["current"]["content"] == "diff"


def test_unsupported_xattrs_and_invalid_utf8_are_refused_without_rewrite(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _documents(world)
        invalid = root / "invalid.txt"
        invalid.write_bytes(b"\xff\xfe")
        unsupported = root / "metadata.txt"
        unsupported.write_bytes(b"keep")
        try:
            os.setxattr(unsupported, "user.hopper.synthetic", b"marker")
        except OSError as exc:
            pytest.skip(f"filesystem xattrs unavailable: {exc}")
        headers = _signed(world)
        invalid_response = world.client.get("/api/file", params=_address("invalid.txt"))
        assert invalid_response.status_code == 422
        assert invalid_response.json() == {"error": "invalid_encoding"}
        loaded = world.client.get("/api/file", params=_address("metadata.txt")).json()
        assert loaded["editable"] is False
        assert loaded["readOnlyReason"] == "extended_attributes"
        refused = world.client.put(
            "/api/file", params=_address("metadata.txt"), headers=headers,
            json={"baseVersion": loaded["version"], "content": "changed"},
        )
        assert refused.status_code == 403
        assert refused.json() == {"error": "not_editable", "reason": "extended_attributes"}
        assert unsupported.read_bytes() == b"keep"
        assert os.getxattr(unsupported, "user.hopper.synthetic") == b"marker"


def test_posix_access_acl_is_preserved_on_atomic_save(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "acl.md"
        path.write_bytes(b"before")
        os.chmod(path, 0o640)
        undefined = 0xFFFFFFFF
        entries = [(1, 6, undefined), (4, 4, undefined), (16, 4, undefined), (32, 0, undefined)]
        acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
        try:
            os.setxattr(path, "system.posix_acl_access", acl)
        except OSError as exc:
            pytest.skip(f"POSIX access ACL unavailable: {exc}")
        before_acl = os.getxattr(path, "system.posix_acl_access")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address("acl.md")).json()
        saved = world.client.put(
            "/api/file", params=_address("acl.md"), headers=headers,
            json={"baseVersion": loaded["version"], "content": "after"},
        )
        assert saved.status_code == 200, saved.text
        assert path.read_bytes() == b"after"
        assert os.getxattr(path, "system.posix_acl_access") == before_acl
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_exact_five_mibibytes_are_editable_but_larger_content_is_rejected(tmp_path) -> None:
    content = "x" * (5 * 1024 * 1024)
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "limit.txt"
        path.write_bytes(b"start")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address("limit.txt")).json()
        exact = world.client.put(
            "/api/file", params=_address("limit.txt"), headers=headers,
            json={"baseVersion": loaded["version"], "content": content},
        )
        assert exact.status_code == 200, exact.text[:100]
        assert exact.json()["size"] == 5 * 1024 * 1024
        reread = world.client.get("/api/file", params=_address("limit.txt"))
        assert reread.status_code == 200 and reread.json()["content"] == content
        loaded = exact.json()
        too_large = world.client.put(
            "/api/file", params=_address("limit.txt"), headers=headers,
            json={"baseVersion": loaded["savedVersion"], "content": content + "x"},
        )
        assert too_large.status_code == 413
        assert path.stat().st_size == 5 * 1024 * 1024
        path.write_bytes(content.encode("utf-8") + b"x")
        assert world.client.get("/api/file", params=_address("limit.txt")).status_code == 413


def test_preview_requires_supported_signatures_and_enforces_viewer_limits(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _documents(world)
        (root / "small.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        (root / "small.pdf").write_bytes(b"%PDF-1.7\nsynthetic")
        (root / "active.svg").write_text("<svg><script>alert(1)</script></svg>", encoding="utf-8")
        large_png = root / "large.png"
        large_png.write_bytes(b"\x89PNG\r\n\x1a\n")
        with large_png.open("r+b") as stream:
            stream.truncate(20 * 1024 * 1024 + 1)
        large_pdf = root / "large.pdf"
        large_pdf.write_bytes(b"%PDF-1.7\n")
        with large_pdf.open("r+b") as stream:
            stream.truncate(100 * 1024 * 1024 + 1)

        assert world.client.get("/api/preview", params=_address("small.png")).status_code == 401
        _signed(world)
        for path, media in (("small.png", "image/png"), ("small.pdf", "application/pdf")):
            response = world.client.get("/api/preview", params=_address(path))
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(media)
            assert response.headers["content-disposition"].startswith("inline;")
        assert world.client.get("/api/preview", params=_address("large.png")).status_code == 413
        assert world.client.get("/api/preview", params=_address("large.pdf")).status_code == 413
        assert world.client.get("/api/preview", params=_address("active.svg")).status_code == 415


def test_concurrent_cooperative_saves_allow_only_one_base_revision(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "parallel.md"
        path.write_bytes(b"base")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address("parallel.md")).json()

        def write(text: str):
            return world.client.put(
                "/api/file", params=_address("parallel.md"), headers=headers,
                json={"baseVersion": loaded["version"], "content": text},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(write, ["first", "second"]))
        assert sorted(response.status_code for response in responses) == [200, 409]
        assert path.read_text(encoding="utf-8") in {"first", "second"}


def test_directory_sync_failure_reports_indeterminate_without_rollback(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "uncertain.md"
        path.write_bytes(b"before")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address("uncertain.md")).json()
        real_fsync = editor_module.os.fsync
        failed = False

        def fail_directory_sync(fd: int) -> None:
            nonlocal failed
            if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
                failed = True
                raise OSError(5, "synthetic directory sync failure")
            real_fsync(fd)

        monkeypatch.setattr(editor_module.os, "fsync", fail_directory_sync)
        result = world.client.put(
            "/api/file", params=_address("uncertain.md"), headers=headers,
            json={"baseVersion": loaded["version"], "content": "published"},
        )
        assert result.status_code == 503
        assert result.json()["error"] == "indeterminate"
        assert result.json()["current"]["content"] == "published"
        assert path.read_bytes() == b"published"
        assert failed


def test_prepublication_replace_failure_keeps_original_file(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "before-replace.md"
        path.write_bytes(b"original")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address("before-replace.md")).json()

        def fail_replace(*_args, **_kwargs) -> None:
            raise OSError(errno.EIO, "synthetic prepublication failure")

        monkeypatch.setattr(editor_module.os, "replace", fail_replace)
        result = world.client.put(
            "/api/file", params=_address("before-replace.md"), headers=headers,
            json={"baseVersion": loaded["version"], "content": "new bytes"},
        )
        assert result.status_code == 503
        assert result.json() == {"error": "io_error"}
        assert path.read_bytes() == b"original"
        assert not list(path.parent.glob(".hopper-stage-*.tmp"))


def test_noncooperating_writer_can_race_after_final_validation(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "outside-writer.md"
        path.write_bytes(b"original")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address("outside-writer.md")).json()
        real_replace = editor_module.os.replace
        raced = False

        def race_after_check(source, destination, *args, **kwargs) -> None:
            nonlocal raced
            if not raced and str(source).startswith(".hopper-stage-"):
                fd = os.open(
                    destination,
                    os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=kwargs["dst_dir_fd"],
                )
                try:
                    os.write(fd, b"outside")
                finally:
                    os.close(fd)
                raced = True
            real_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr(editor_module.os, "replace", race_after_check)
        result = world.client.put(
            "/api/file", params=_address("outside-writer.md"), headers=headers,
            json={"baseVersion": loaded["version"], "content": "local edit"},
        )
        assert raced
        assert result.status_code == 200
        assert path.read_bytes() == b"local edit"
        assert result.json()["content"] == "local edit"


def test_two_authenticated_sessions_conflict_on_the_second_stale_save(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "sessions.md"
        path.write_bytes(b"base")
        first_headers = _signed(world)
        second_client = TestClient(
            world.client.app,
            base_url=f"http://{world.config.host_header}",
            client=("127.0.0.1", 40001),
            headers={"origin": world.config.origin},
        )
        with second_client:
            nonce = second_client.get("/login", headers={"accept": "application/json"}).json()["nonce"]
            login = second_client.post("/login", json={"nonce": nonce, "password": PASSWORD})
            second_headers = {"origin": world.config.origin, "x-csrf-token": login.json()["csrfToken"]}
            loaded_first = world.client.get("/api/file", params=_address("sessions.md")).json()
            loaded_second = second_client.get("/api/file", params=_address("sessions.md")).json()
            assert loaded_first["version"] == loaded_second["version"]
            first = world.client.put(
                "/api/file", params=_address("sessions.md"), headers=first_headers,
                json={"baseVersion": loaded_first["version"], "content": "first session"},
            )
            second = second_client.put(
                "/api/file", params=_address("sessions.md"), headers=second_headers,
                json={"baseVersion": loaded_second["version"], "content": "second session"},
            )
        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["current"]["content"] == "first session"
        assert path.read_bytes() == b"first session"


def test_default_directory_acl_is_not_published_when_original_has_no_acl(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        directory = _documents(world) / "default-acl"
        directory.mkdir(mode=0o700)
        target = directory / "note.md"
        target.write_bytes(b"before")
        os.chmod(target, 0o640)
        assert "system.posix_acl_access" not in os.listxattr(target)
        undefined = 0xFFFFFFFF
        default_acl = struct.pack(
            "<I", 2
        ) + b"".join(struct.pack("<HHI", *entry) for entry in [
            (1, 7, undefined), (2, 6, 65534), (4, 0, undefined),
            (16, 6, undefined), (32, 0, undefined),
        ])
        try:
            os.setxattr(directory, "system.posix_acl_default", default_acl)
        except OSError as exc:
            pytest.skip(f"POSIX default ACL unavailable: {exc}")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address("default-acl/note.md")).json()
        saved = world.client.put(
            "/api/file", params=_address("default-acl/note.md"), headers=headers,
            json={"baseVersion": loaded["version"], "content": "after"},
        )
        assert saved.status_code == 200, saved.text
        assert target.read_bytes() == b"after"
        assert "system.posix_acl_access" not in os.listxattr(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_post_replace_external_write_is_indeterminate_and_cannot_be_replayed_as_success(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _documents(world)
        target = root / "post-replace-race.md"
        target.write_bytes(b"before")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address(target.name)).json()
        real_fsync = editor_module.os.fsync
        raced = False

        def external_writer_after_replace(fd: int) -> None:
            nonlocal raced
            if not raced and stat.S_ISDIR(os.fstat(fd).st_mode):
                raced = True
                external = root / "outside.tmp"
                external.write_bytes(b"external writer")
                os.replace(external, target)
            real_fsync(fd)

        monkeypatch.setattr(editor_module.os, "fsync", external_writer_after_replace)
        saved = world.client.put(
            "/api/file", params=_address(target.name), headers=headers,
            json={"baseVersion": loaded["version"], "content": "my edit"},
        )
        assert raced
        assert saved.status_code == 503
        assert saved.json()["error"] == "indeterminate"
        assert saved.json()["current"]["content"] == "external writer"
        assert "savedVersion" not in saved.json()
        replay = world.client.put(
            "/api/file", params=_address(target.name), headers=headers,
            json={"baseVersion": saved.json().get("savedVersion"), "content": "my edit"},
        )
        assert replay.status_code == 428
        assert target.read_bytes() == b"external writer"


def test_read_only_file_and_missing_file_have_distinct_http_errors(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        readonly = _documents(world) / "readonly.md"
        readonly.write_bytes(b"keep")
        os.chmod(readonly, 0o444)
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address(readonly.name)).json()
        assert (loaded["editable"], loaded["readOnlyReason"]) == (False, "file_not_writable")
        denied = world.client.put(
            "/api/file", params=_address(readonly.name), headers=headers,
            json={"baseVersion": loaded["version"], "content": "replace"},
        )
        assert denied.status_code == 403
        assert denied.json() == {"error": "not_editable", "reason": "file_not_writable"}
        assert readonly.read_bytes() == b"keep"
        assert not list(readonly.parent.glob(".hopper-stage-*.tmp"))

        missing_get = world.client.get("/api/file", params=_address("missing.md"))
        missing_put = world.client.put(
            "/api/file", params=_address("missing.md"), headers=headers,
            json={"baseVersion": "v1.synthetic", "content": "replace"},
        )
        assert missing_get.status_code == 404 and missing_get.json() == {"error": "not_found"}
        assert missing_put.status_code == 404 and missing_put.json() == {"error": "not_found"}
        assert not (_documents(world) / "missing.md").exists()


def test_postpublication_space_exhaustion_is_not_reported_as_size_limit(tmp_path, monkeypatch) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "disk-full.md"
        path.write_bytes(b"before")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address(path.name)).json()

        def no_space(_fd, _data):
            raise OSError(errno.ENOSPC, "synthetic disk full")

        monkeypatch.setattr(editor_module, "_write_all", no_space)
        result = world.client.put(
            "/api/file", params=_address(path.name), headers=headers,
            json={"baseVersion": loaded["version"], "content": "after"},
        )
        assert result.status_code == 503
        assert result.json() == {"error": "storage_unavailable"}
        assert path.read_bytes() == b"before"
        assert not list(path.parent.glob(".hopper-stage-*.tmp"))


@pytest.mark.parametrize("bits", [stat.S_ISUID, stat.S_ISGID])
def test_setid_metadata_is_refused_without_rewrite(tmp_path, bits) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "setid.md"
        path.write_bytes(b"keep")
        os.chmod(path, 0o640 | bits)
        if not stat.S_IMODE(path.stat().st_mode) & bits:
            pytest.skip("filesystem or service account does not retain set-ID bits")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address(path.name)).json()
        assert (loaded["editable"], loaded["readOnlyReason"]) == (False, "set_id")
        refused = world.client.put(
            "/api/file", params=_address(path.name), headers=headers,
            json={"baseVersion": loaded["version"], "content": "changed"},
        )
        assert refused.status_code == 403
        assert refused.json() == {"error": "not_editable", "reason": "set_id"}
        assert path.read_bytes() == b"keep"
        assert stat.S_IMODE(path.stat().st_mode) & bits


@pytest.mark.parametrize("fault", ["fchown", "fchmod", "setxattr", "verify_acl"])
def test_access_policy_faults_refuse_before_publication(tmp_path, monkeypatch, fault) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        path = _documents(world) / "policy.md"
        path.write_bytes(b"keep")
        os.chmod(path, 0o640)
        undefined = 0xFFFFFFFF
        acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in [
            (1, 6, undefined), (4, 4, undefined), (16, 4, undefined), (32, 0, undefined),
        ])
        try:
            os.setxattr(path, "system.posix_acl_access", acl)
        except OSError as exc:
            pytest.skip(f"POSIX access ACL unavailable: {exc}")
        headers = _signed(world)
        loaded = world.client.get("/api/file", params=_address(path.name)).json()
        if fault == "verify_acl":
            real_getxattr = editor_module.os.getxattr
            target_identity = (path.stat().st_dev, path.stat().st_ino)

            def wrong_temp_acl(fd, name, **kwargs):
                info = os.fstat(fd) if isinstance(fd, int) else None
                if name == "system.posix_acl_access" and info and (info.st_dev, info.st_ino) != target_identity:
                    return b"wrong ACL"
                return real_getxattr(fd, name, **kwargs)

            monkeypatch.setattr(editor_module.os, "getxattr", wrong_temp_acl)
        else:
            def fail(*_args, **_kwargs):
                raise OSError(errno.EPERM, "synthetic metadata failure")
            monkeypatch.setattr(editor_module.os, fault, fail)
        refused = world.client.put(
            "/api/file", params=_address(path.name), headers=headers,
            json={"baseVersion": loaded["version"], "content": "changed"},
        )
        assert refused.status_code == 409
        assert refused.json() == {"error": "metadata_unsupported"}
        assert path.read_bytes() == b"keep"
        assert os.getxattr(path, "system.posix_acl_access") == acl
        assert not list(path.parent.glob(".hopper-stage-*.tmp"))


def test_dotfiles_and_hard_links_are_readable_while_links_and_missing_files_are_not(tmp_path) -> None:
    """HF-NAV-009: no protected names. HF-NAV-007: a hard link reads but is not editable."""
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        root = _documents(world)
        (root / ".env").write_bytes(b"synthetic-env")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_bytes(b"synthetic-config")
        linked = root / "linked.md"
        linked.write_bytes(b"synthetic-linked")
        os.link(linked, root / "hardlink.md")
        outside = tmp_path / "outside.md"
        outside.write_bytes(b"synthetic-outside")
        os.symlink(outside, root / "symlink.md")
        headers = _signed(world)
        env = world.client.get("/api/file", params=_address(".env"))
        git = world.client.get("/api/file", params=_address(".git/config"))
        hard = world.client.get("/api/file", params=_address("hardlink.md"))
        hard_put = world.client.put(
            "/api/file", params=_address("hardlink.md"), headers=headers,
            json={"baseVersion": hard.json()["version"], "content": "overwrite"},
        )
        for path in ("symlink.md", "missing.md"):
            got = world.client.get("/api/file", params=_address(path))
            put = world.client.put(
                "/api/file", params=_address(path), headers=headers,
                json={"baseVersion": "v2.synthetic", "content": "overwrite"},
            )
            assert got.status_code != 200 and "synthetic-" not in got.text
            assert put.status_code != 200 and "synthetic-" not in put.text

    assert env.status_code == git.status_code == hard.status_code == 200
    assert env.json()["content"] == "synthetic-env" and env.json()["editable"] is True
    assert git.json()["content"] == "synthetic-config"
    assert hard.json()["content"] == "synthetic-linked"
    assert (hard.json()["editable"], hard.json()["readOnlyReason"]) == (False, "hard_link")
    assert hard_put.status_code == 403
    assert hard_put.json() == {"error": "not_editable", "reason": "hard_link"}
    assert linked.read_bytes() == b"synthetic-linked"
    assert outside.read_bytes() == b"synthetic-outside"
    assert not (root / "missing.md").exists()
