from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import copy
from pathlib import Path

import fastapi
import pytest

from hopper_files import lifecycle, runtime
from hopper_files.config import ConfigError, load_config
from hopper_files.lifecycle import (
    MANAGED_UNIT_MARKER,
    Layout,
    LifecycleError,
    _admin_wrapper_text,
    _validate_pip_report,
    _launcher_text,
    install_shared,
    register_instance,
    remove_instance,
    rollback_release,
    uninstall_shared,
    update_release,
    update_instance,
    verify_artifact,
    verify_installed_release,
)
from hopper_files.systemd_unit import render_service_unit

from conftest import service_account, write_config


REPOSITORY = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.pip_lock_snapshots: list[bytes] = []
        self.fail_restart_number: int | None = None
        self.fail_disable_number: int | None = None

    def __call__(self, command, **kwargs):
        args = tuple(str(item) for item in command)
        self.commands.append(args)
        if args[0].endswith("python3"):
            target = Path(args[args.index("--target") + 1])
            report = Path(args[args.index("--report") + 1])
            lock_path = Path(args[args.index("-r") + 1])
            self.pip_lock_snapshots.append(lock_path.read_bytes())
            target.mkdir(parents=True)
            (target / "synthetic_dependency.txt").write_text("synthetic package payload", encoding="utf-8")
            report.write_text(json.dumps(_pip_report(lock_path.read_bytes())), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        if "/usr/bin/systemctl" in args[0]:
            if args[1] == "restart" and self.fail_restart_number is not None:
                if sum(item[1] == "restart" for item in self.commands) == self.fail_restart_number:
                    return subprocess.CompletedProcess(command, 1, "", "synthetic restart failure")
            if args[1] == "disable" and self.fail_disable_number is not None:
                if sum(item[1] == "disable" for item in self.commands) == self.fail_disable_number:
                    return subprocess.CompletedProcess(command, 1, "", "synthetic disable failure")
        return subprocess.CompletedProcess(command, 0, "active", "")


def _locked_packages(lock: bytes) -> dict[str, tuple[str, set[str]]]:
    result: dict[str, tuple[str, set[str]]] = {}
    active: str | None = None
    for line in lock.decode("utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            result[name] = (match.group(2), set())
            active = name
            continue
        if active is not None:
            digest = re.search(r"--hash=sha256:([0-9a-f]{64})", line)
            if digest:
                result[active][1].add(digest.group(1))
            if "# via" in line:
                active = None
    return result


def _pip_report(lock: bytes) -> dict[str, object]:
    packages = _locked_packages(lock)
    assert packages and all(hashes for _, hashes in packages.values())
    return {
        "version": "1",
        "pip_version": "OS-managed synthetic test pip",
        "install": [
            {
                "download_info": {
                    "url": f"https://files.pythonhosted.org/packages/{name}.whl",
                    "archive_info": {"hashes": {"sha256": sorted(hashes)[0]}},
                },
                "metadata": {"name": name, "version": version},
            }
            for name, (version, hashes) in sorted(packages.items())
        ],
    }


def _synthetic_tree_id(files: dict[str, bytes]) -> str:
    """Return a Git-format tree ID for the tiny synthetic archive source tree."""
    blob_ids = {
        name: hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        for name, data in files.items()
    }

    def tree_id(entries: list[tuple[bytes, bytes, str]]) -> str:
        ordered = sorted(entries, key=lambda entry: entry[1] + (b"/" if entry[0] == b"40000" else b""))
        body = b"".join(mode + b" " + name + b"\0" + bytes.fromhex(oid) for mode, name, oid in ordered)
        return hashlib.sha1(b"tree " + str(len(body)).encode() + b"\0" + body).hexdigest()

    package_tree = tree_id([(b"100644", b"release_probe.py", blob_ids["source/src/hopper_files/release_probe.py"])])
    src_tree = tree_id([(b"40000", b"hopper_files", package_tree)])
    root_tree = tree_id(
        [
            (b"100644", b"requirements.lock", blob_ids["source/requirements.lock"]),
            (b"40000", b"src", src_tree),
        ]
    )
    return root_tree


def _artifact(path: Path, release: str, marker: bytes) -> tuple[Path, str]:
    lock = (REPOSITORY / "requirements.lock").read_bytes()
    files = {
        "source/requirements.lock": lock,
        "source/src/hopper_files/release_probe.py": marker,
    }
    manifest = {
        "schema": 1,
        "releaseId": release,
        "sourceTree": _synthetic_tree_id(files),
        "requirementsLockSha256": hashlib.sha256(lock).hexdigest(),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    dirs = {"source", "source/src", "source/src/hopper_files"}
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(dirs, key=lambda item: (item.count("/"), item)):
            item = tarfile.TarInfo(name + "/")
            item.type = tarfile.DIRTYPE
            item.mode = 0o755
            archive.addfile(item)
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.mode = 0o644
            item.size = len(data)
            archive.addfile(item, fileobj=io.BytesIO(data))
        payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        item = tarfile.TarInfo("release.json")
        item.mode = 0o644
        item.size = len(payload)
        archive.addfile(item, fileobj=io.BytesIO(payload))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _layout(tmp_path: Path) -> Layout:
    for relative in ("opt", "var/lib", "etc/systemd/system", "usr/local/bin", "usr/local/libexec", "run/lock"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    return Layout(
        install_root=tmp_path / "opt/hopper-files",
        metadata_root=tmp_path / "var/lib/hopper-files",
        unit_root=tmp_path / "etc/systemd/system",
        admin_wrapper=tmp_path / "usr/local/bin/hopper-files-admin",
        launcher=tmp_path / "usr/local/libexec/hopper-files-launch",
        lock_file=tmp_path / "run/lock/hopper-files-lifecycle.lock",
    )


def _managed_instance(layout: Layout, iid: str, config: Path, state: Path) -> Path:
    layout.instances.mkdir(parents=True, exist_ok=True)
    layout.unit_root.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema": 1,
        "instanceId": iid,
        "configPath": str(config),
        "serviceAccount": service_account(),
        "stateDirectory": str(state),
        "unit": f"hopper-files-{iid}.service",
    }
    (layout.instances / f"{iid}.json").write_text(json.dumps(metadata), encoding="utf-8")
    unit = layout.unit_root / f"hopper-files-{iid}.service"
    unit.write_text(f"{MANAGED_UNIT_MARKER}\n[Service]\n", encoding="utf-8")
    return unit


def test_two_identified_releases_update_rollback_and_recovery_preserve_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hopper_files.lifecycle._require_root", lambda: None)
    layout = _layout(tmp_path)
    runner = FakeRunner()
    first, first_hash = _artifact(tmp_path / "one.tar.gz", "release-one", b"RELEASE_MARKER = 'one'\n")
    second, second_hash = _artifact(tmp_path / "two.tar.gz", "release-two", b"RELEASE_MARKER = 'two'\n")
    first_source_tree = verify_artifact(first, first_hash)["sourceTree"]
    second_source_tree = verify_artifact(second, second_hash)["sourceTree"]
    assert first_source_tree != second_source_tree
    documents = tmp_path / "documents"
    state = tmp_path / "state"
    config = tmp_path / "config.json"
    documents.mkdir()
    state.mkdir()
    documents_file = documents / "note.md"
    state_file = state / "state.json"
    documents_file.write_bytes(b"synthetic document\n")
    state_file.write_bytes(b"synthetic state\n")
    config.write_bytes(b"synthetic config\n")
    preserved_hashes = tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (documents_file, state_file, config))

    assert install_shared(first, first_hash, layout=layout, runner=runner) == "release-one"
    assert runner.commands[0][:4] == ("/usr/bin/python3", "-I", "-B", "-m")
    first_release = layout.releases / "release-one"
    assert stat.S_IMODE(first_release.stat().st_mode) == 0o555
    source_probe = first_release / "source/src/hopper_files/release_probe.py"
    assert stat.S_IMODE(source_probe.stat().st_mode) == 0o444
    assert verify_installed_release(layout, "release-one")["sourceTree"] == first_source_tree

    _managed_instance(layout, "alpha", config, state)
    runner.fail_restart_number = 1
    with pytest.raises(LifecycleError, match="rolled back to release-one"):
        update_release(second, second_hash, layout=layout, runner=runner)
    assert layout.selector.resolve() == first_release
    assert verify_installed_release(layout, "release-two")["releaseId"] == "release-two"

    runner.fail_restart_number = None
    assert update_release(second, second_hash, layout=layout, runner=runner) == ("release-one", "release-two")
    assert layout.selector.resolve() == layout.releases / "release-two"
    assert rollback_release("release-one", layout=layout, runner=runner) == ("release-two", "release-one")
    assert layout.selector.resolve() == first_release
    assert tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (documents_file, state_file, config)) == preserved_hashes
    assert source_probe.read_bytes() == b"RELEASE_MARKER = 'one'\n"


def test_rollback_of_a_registered_instance_does_not_repeat_registration_preflight(tmp_path, monkeypatch) -> None:
    """A registered unit can roll back to an earlier release without a new registration preflight."""
    if os.geteuid() != 0:
        pytest.skip("isolated lifecycle mutation requires root for the synthetic service identity")
    monkeypatch.setattr("hopper_files.lifecycle._require_root", lambda: None)
    layout = _layout(tmp_path)
    runner = FakeRunner()
    earlier, earlier_hash = _artifact(tmp_path / "earlier.tar.gz", "earlier-release", b"RELEASE_MARKER = 'earlier'\n")
    later, later_hash = _artifact(tmp_path / "later.tar.gz", "later-release", b"RELEASE_MARKER = 'later'\n")
    install_shared(earlier, earlier_hash, layout=layout, runner=runner)

    workspace = tmp_path / "registered-instance"
    workspace.mkdir(mode=0o755)
    documents = workspace / "documents"
    state = workspace / "state"
    for path in (documents, state):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    nobody = __import__("pwd").getpwnam("nobody")
    for path in (documents, state):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
        os.chmod(path, 0o700)
    config = write_config(workspace / "config", instance_id="alice", account="nobody", state=state)
    os.chown(config, 0, nobody.pw_gid)
    os.chmod(config, 0o640)
    document = documents / "preserved.md"
    state_marker = state / "preserved-state.bin"
    document.write_bytes(b"synthetic document\n")
    state_marker.write_bytes(b"synthetic state\n")
    preserved = tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (document, state_marker, config))

    layout.instances.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema": 1,
        "instanceId": "alice",
        "configPath": str(config),
        "serviceAccount": "nobody",
        "stateDirectory": str(state),
        "unit": "hopper-files-alice.service",
    }
    (layout.instances / "alice.json").write_text(json.dumps(metadata), encoding="utf-8")
    unit = layout.unit_root / "hopper-files-alice.service"
    unit.write_text(f"{MANAGED_UNIT_MARKER}\n[Service]\n", encoding="utf-8")

    assert update_release(later, later_hash, layout=layout, runner=runner) == ("earlier-release", "later-release")

    def forbidden_preflight(*_args, **_kwargs):
        raise AssertionError("rollback must not rerun registration preflight")

    monkeypatch.setattr("hopper_files.lifecycle._validate_as_service_account", forbidden_preflight)
    assert rollback_release("earlier-release", layout=layout, runner=runner) == (
        "later-release",
        "earlier-release",
    )
    assert layout.selector.resolve() == layout.releases / "earlier-release"
    assert ("/usr/bin/systemctl", "restart", unit.name) in runner.commands
    assert tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (document, state_marker, config)) == preserved


def test_release_verification_fails_closed_on_archive_hash_or_installed_file_change(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hopper_files.lifecycle._require_root", lambda: None)
    layout = _layout(tmp_path)
    runner = FakeRunner()
    artifact, digest = _artifact(tmp_path / "release.tar.gz", "hash-release", b"immutable\n")
    with pytest.raises(LifecycleError, match="SHA-256 mismatch"):
        install_shared(artifact, "0" * 64, layout=layout, runner=runner)
    assert not layout.install_root.exists()

    install_shared(artifact, digest, layout=layout, runner=runner)
    path = layout.releases / "hash-release/source/src/hopper_files/release_probe.py"
    path.chmod(0o644)
    path.write_text("changed after install\n", encoding="utf-8")
    with pytest.raises(LifecycleError, match="file hash mismatch"):
        verify_installed_release(layout, "hash-release")


def test_archive_swap_after_verification_cannot_change_extracted_code_or_pip_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hopper_files.lifecycle._require_root", lambda: None)
    layout = _layout(tmp_path)
    artifact, digest = _artifact(tmp_path / "release.tar.gz", "toctou-release", b"RELEASE_PAYLOAD = 'verified'\n")
    substituted = tmp_path / "substituted.tar.gz"
    invalid_lock = b"--index-url https://pypi.org/simple\nUNVERIFIED_REQUIREMENT==999\n"
    with tarfile.open(artifact, mode="r:gz") as source, tarfile.open(substituted, mode="w:gz") as target:
        for member in source:
            if member.isfile():
                stream = source.extractfile(member)
                assert stream is not None
                with stream:
                    payload = invalid_lock if member.name == "source/requirements.lock" else stream.read()
                changed = copy.copy(member)
                changed.size = len(payload)
                target.addfile(changed, io.BytesIO(payload))
            else:
                target.addfile(member)

    runner = FakeRunner()
    original_extract = lifecycle._extract_verified_artifact

    def swap_after_verification(verified, destination):
        os.replace(substituted, artifact)
        return original_extract(verified, destination)

    monkeypatch.setattr(lifecycle, "_extract_verified_artifact", swap_after_verification)
    assert install_shared(artifact, digest, layout=layout, runner=runner) == "toctou-release"

    installed = layout.releases / "toctou-release"
    probe = installed / "source/src/hopper_files/release_probe.py"
    assert probe.read_bytes() == b"RELEASE_PAYLOAD = 'verified'\n"
    assert runner.pip_lock_snapshots == [(REPOSITORY / "requirements.lock").read_bytes()]
    assert b"UNVERIFIED_REQUIREMENT" not in runner.pip_lock_snapshots[0]
    assert json.loads((installed / "installed.json").read_text())["archiveSha256"] == digest


def test_special_config_path_is_one_escaped_environment_assignment(tmp_path) -> None:
    from hopper_files.config import load_config

    special_directory = tmp_path / 'config space %"\\\nUser=root\nProtectSystem=off'
    config_path = write_config(
        special_directory,
        account=service_account(),
        instance_id="escape-env",
    )
    config = load_config(config_path)
    unit = render_service_unit(config, config_path=config_path)
    lines = unit.splitlines()
    environment_lines = [
        line for line in lines if line.startswith("Environment=") and "HOPPER_FILES_INSTANCE" in line
    ]
    assert len(environment_lines) == 1
    assert environment_lines[0].startswith('Environment="HOPPER_FILES_INSTANCE=')
    assert "\\nUser=root\\nProtectSystem=off" in environment_lines[0]
    assert "%%" in environment_lines[0]
    assert lines[0] == MANAGED_UNIT_MARKER
    assert [line for line in lines if line.startswith("User=")] == [f"User={config.service_account}"]
    assert [line for line in lines if line.startswith("ProtectSystem=")] == []


def test_remove_and_uninstall_preserve_external_config_state_and_documents(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hopper_files.lifecycle._require_root", lambda: None)
    layout = _layout(tmp_path)
    runner = FakeRunner()
    artifact, digest = _artifact(tmp_path / "release.tar.gz", "preserve-release", b"release\n")
    install_shared(artifact, digest, layout=layout, runner=runner)
    monkeypatch.setattr(
        "hopper_files.lifecycle._validate_as_service_account",
        lambda path, _layout: load_config(path),
    )
    snapshots = []
    for iid in ("alpha", "beta", "gamma"):
        docs = tmp_path / iid / "docs"
        state = tmp_path / iid / "state"
        docs.mkdir(parents=True)
        state.mkdir()
        document = docs / "note.md"
        state_file = state / "saved.json"
        document.write_text(f"synthetic document {iid}\n", encoding="utf-8")
        state_file.write_text(f"synthetic state {iid}\n", encoding="utf-8")
        config = write_config(
            tmp_path / iid / "configuration",
            account=service_account(),
            instance_id=iid,
            state=state,
        )
        snapshots.append((config, document, state_file, tuple(path.read_bytes() for path in (config, document, state_file))))
        assert register_instance(config, layout=layout, runner=runner) == iid
        unit = layout.unit_root / f"hopper-files-{iid}.service"
        assert unit.read_text(encoding="utf-8").splitlines()[0] == MANAGED_UNIT_MARKER
        assert unit.read_text(encoding="utf-8").splitlines()[1] == "[Unit]"

    runner.fail_disable_number = 1
    with pytest.raises(LifecycleError, match="removal failed"):
        remove_instance("alpha", layout=layout, runner=runner)
    assert (layout.instances / "alpha.json").exists()
    assert (layout.unit_root / "hopper-files-alpha.service").exists()

    runner.fail_disable_number = None
    removed = remove_instance("alpha", layout=layout, runner=runner)
    assert removed == (snapshots[0][0], tmp_path / "alpha/state", layout.unit_root / "hopper-files-alpha.service")
    assert not (layout.instances / "alpha.json").exists()
    assert not removed[2].exists()

    runner.fail_disable_number = sum(item[1] == "disable" for item in runner.commands) + 2
    with pytest.raises(LifecycleError, match="units were restored"):
        uninstall_shared(layout=layout, runner=runner)
    assert layout.selector.resolve() == layout.releases / "preserve-release"
    assert (layout.unit_root / "hopper-files-beta.service").exists()
    assert (layout.unit_root / "hopper-files-gamma.service").exists()

    runner.fail_disable_number = None
    count, configs = uninstall_shared(layout=layout, runner=runner)
    assert count == 2 and configs == (snapshots[1][0], snapshots[2][0])
    assert not layout.install_root.exists()
    assert not layout.admin_wrapper.exists() and not layout.launcher.exists()
    for config, document, state_file, original in snapshots:
        assert tuple(path.read_bytes() for path in (config, document, state_file)) == original


def test_registration_and_update_publish_a_unit_without_file_restrictions(tmp_path, monkeypatch) -> None:
    """HF-NAV-001/HF-NAV-010: registration needs only instance settings; the unit restricts no path."""
    monkeypatch.setattr("hopper_files.lifecycle._require_root", lambda: None)
    layout = _layout(tmp_path)
    runner = FakeRunner()
    artifact, digest = _artifact(tmp_path / "release.tar.gz", "config-release", b"release\n")
    install_shared(artifact, digest, layout=layout, runner=runner)
    state = tmp_path / "state"
    state.mkdir()
    config_path = write_config(tmp_path / "instance", account=service_account(), instance_id="single-base", state=state)
    monkeypatch.setattr("hopper_files.lifecycle._validate_as_service_account", lambda path, _layout: load_config(path))
    before = config_path.read_bytes()
    assert register_instance(config_path, layout=layout, runner=runner) == "single-base"
    unit = layout.unit_root / "hopper-files-single-base.service"
    original_unit = unit.read_text(encoding="utf-8")
    lines = original_unit.splitlines()
    assert config_path.read_bytes() == before
    assert "UMask=0002" in lines and "NoNewPrivileges=yes" in lines
    for directive in ("ProtectSystem", "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "PrivateTmp"):
        assert not any(line.startswith(directive + "=") for line in lines)
    assert str(layout.selector) in original_unit

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["timeZone"] = "Europe/Lisbon"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    edited_config = config_path.read_bytes()
    runner.fail_restart_number = sum(item[1] == "restart" for item in runner.commands) + 1
    with pytest.raises(LifecycleError, match="instance update was reverted"):
        update_instance(config_path, layout=layout, runner=runner)
    assert unit.read_text(encoding="utf-8") == original_unit
    assert config_path.read_bytes() == edited_config
    runner.fail_restart_number = None
    assert update_instance(config_path, layout=layout, runner=runner) == "single-base"
    assert config_path.read_bytes() == edited_config


def test_service_account_validation_does_not_inherit_private_admin_cwd(tmp_path, monkeypatch) -> None:
    import pwd

    current_uid = os.geteuid()
    account = next(entry for entry in pwd.getpwall() if entry.pw_uid not in {0, current_uid})
    config_path = write_config(
        tmp_path / "config",
        account=account.pw_name,
        instance_id="private-cwd",
    )
    private_cwd = tmp_path / "private-admin-home"
    private_cwd.mkdir(mode=0o700)
    private_cwd.chmod(0o700)
    monkeypatch.chdir(private_cwd)
    assert stat.S_IMODE(private_cwd.stat().st_mode) == 0o700
    assert private_cwd.stat().st_uid != account.pw_uid

    calls = []

    def validate_as_service(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "instanceId": "private-cwd",
                "serviceAccount": account.pw_name,
                "stateDirectory": str(config_path.parent / "state"),
            }),
            "",
        )

    monkeypatch.setattr(lifecycle.os, "geteuid", lambda: 0)
    monkeypatch.setattr(lifecycle.subprocess, "run", validate_as_service)
    config = lifecycle._validate_as_service_account(config_path, lifecycle.SYSTEM_LAYOUT)

    assert config.instance_id == "private-cwd"
    assert len(calls) == 1
    assert calls[0][1]["cwd"] == "/"


def test_runtime_reports_only_code_imported_from_selected_immutable_release(tmp_path, monkeypatch, capsys) -> None:
    release = tmp_path / "releases/release-two"
    package = release / "source/src/hopper_files"
    package.mkdir(parents=True)
    (package / "runtime.py").write_text("# synthetic runtime module\n", encoding="utf-8")
    (release / "release.json").write_text(
        json.dumps({"releaseId": "release-two", "sourceTree": "d1d30c985f7552fabc89f33214bfe0fcc33c09c6"}),
        encoding="utf-8",
    )
    selector = tmp_path / "current"
    selector.symlink_to(Path("releases/release-two"))
    monkeypatch.setattr(runtime, "__file__", str(package / "runtime.py"))
    monkeypatch.setenv("HOPPER_FILES_ACTIVE_ROOT", str(release))
    monkeypatch.setenv("HOPPER_FILES_ACTIVE_SELECTOR", str(selector))
    runtime._report_active_release()
    assert "active release=release-two" in capsys.readouterr().err

    monkeypatch.setenv("HOPPER_FILES_ACTIVE_ROOT", str(tmp_path / "releases/release-one"))
    (tmp_path / "releases/release-one").mkdir()
    with pytest.raises(ValueError, match="does not match"):
        runtime._report_active_release()


def test_launcher_process_imports_the_current_release_and_not_a_development_copy(tmp_path) -> None:
    layout = _layout(tmp_path)
    layout.install_root.mkdir(parents=True)
    layout.releases.mkdir()
    dependency_site = Path(fastapi.__file__).resolve().parent.parent
    assert dependency_site.is_dir()
    releases = {}
    for rid in ("process-release-one", "process-release-two"):
        root = layout.releases / rid
        package = root / "source/src/hopper_files"
        package.parent.mkdir(parents=True)
        shutil.copytree(
            REPOSITORY / "src/hopper_files",
            package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        runtime_source = package / "runtime.py"
        runtime_text = runtime_source.read_text(encoding="utf-8")
        marker = f"\nprint('SYNTHETIC_RELEASE_CODE={rid}', file=sys.stderr, flush=True)\n\ndef main("
        assert "\ndef main(" in runtime_text
        runtime_source.write_text(runtime_text.replace("\ndef main(", marker, 1), encoding="utf-8")
        (root / "release.json").write_text(
            json.dumps({"releaseId": rid, "sourceTree": hashlib.sha1(rid.encode()).hexdigest()}),
            encoding="utf-8",
        )
        (root / "dependencies").symlink_to(dependency_site, target_is_directory=True)
        releases[rid] = root

    development_copy = tmp_path / "development/src/hopper_files"
    development_copy.mkdir(parents=True)
    (development_copy / "runtime.py").write_text("print('DEVELOPMENT_COPY')\n", encoding="utf-8")
    impostor_cwd = tmp_path / "impostor-cwd"
    impostor_package = impostor_cwd / "hopper_files"
    impostor_package.mkdir(parents=True)
    (impostor_package / "__init__.py").write_text("\n", encoding="utf-8")
    (impostor_package / "runtime.py").write_text(
        "print('IMPOSTOR_RUNTIME_EXECUTED', file=sys.stderr)\nraise SystemExit(0)\n",
        encoding="utf-8",
    )
    launcher = layout.launcher
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(_launcher_text(layout), encoding="utf-8")
    launcher.chmod(0o755)

    for rid, release in releases.items():
        layout.selector.symlink_to(Path("releases") / rid)
        completed = subprocess.run(
            [str(launcher)],
            check=False,
            capture_output=True,
            text=True,
            cwd=impostor_cwd,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "PYTHONPATH": str(impostor_cwd),
                "HOPPER_FILES_ACTIVE_SELECTOR": str(layout.selector),
            },
        )
        layout.selector.unlink()
        assert completed.returncode == 1
        assert f"SYNTHETIC_RELEASE_CODE={rid}" in completed.stderr
        assert f"active release={rid}" in completed.stderr
        assert str(release / "source/src/hopper_files/runtime.py") in completed.stderr
        assert "HOPPER_FILES_INSTANCE está ausente" in completed.stderr
        assert "DEVELOPMENT_COPY" not in completed.stderr
        assert "IMPOSTOR_RUNTIME_EXECUTED" not in completed.stderr
        assert not list(release.rglob("*.pyc")), "runtime entrypoint wrote into its immutable release"


def test_admin_entrypoint_uses_selected_release_not_impostor_cwd(tmp_path) -> None:
    layout = _layout(tmp_path)
    release = layout.releases / "admin-source-release"
    package = release / "source/src/hopper_files"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    (package / "admin.py").write_text(
        "def main(argv=None):\n    print('SELECTED_RELEASE_ADMIN')\n    return 23\n",
        encoding="utf-8",
    )
    (release / "release.json").write_text("{}\n", encoding="utf-8")
    layout.selector.symlink_to(Path("releases/admin-source-release"))
    wrapper = layout.admin_wrapper
    wrapper.write_text(_admin_wrapper_text(layout), encoding="utf-8")
    wrapper.chmod(0o755)
    impostor_cwd = tmp_path / "impostor-cwd"
    impostor_package = impostor_cwd / "hopper_files"
    impostor_package.mkdir(parents=True)
    (impostor_package / "__init__.py").write_text("\n", encoding="utf-8")
    (impostor_package / "admin.py").write_text(
        "def main(argv=None):\n    print('IMPOSTOR_ADMIN_EXECUTED')\n    return 0\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(wrapper), "synthetic-argument"],
        check=False,
        capture_output=True,
        text=True,
        cwd=impostor_cwd,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": str(impostor_cwd)},
    )
    assert completed.returncode == 23
    assert "SELECTED_RELEASE_ADMIN" in completed.stdout
    assert "IMPOSTOR_ADMIN_EXECUTED" not in completed.stdout
    assert not list(release.rglob("*.pyc")), "admin entrypoint wrote into its immutable release"


def test_runtime_launcher_disables_bytecode_writes_before_release_import(tmp_path) -> None:
    layout = _layout(tmp_path)
    launcher = _launcher_text(layout)
    assert "PYTHONDONTWRITEBYTECODE=1" in launcher
    assert "exec /usr/bin/python3 -I -B -c" in launcher


def test_dependency_report_binds_official_archive_hashes_to_lock(tmp_path) -> None:
    lock = (REPOSITORY / "requirements.lock").read_bytes()
    packages = _locked_packages(lock)
    report = _pip_report(lock)
    report_path = tmp_path / "release/dependencies-install-report.json"
    report_path.parent.mkdir(parents=True)
    (report_path.parent / "source").mkdir()
    (report_path.parent / "source/requirements.lock").write_bytes(lock)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    _validate_pip_report(report_path, hashlib.sha256(lock).hexdigest())

    report["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(LifecycleError, match="hash is not permitted by lock"):
        _validate_pip_report(report_path, hashlib.sha256(lock).hexdigest())
    assert packages
