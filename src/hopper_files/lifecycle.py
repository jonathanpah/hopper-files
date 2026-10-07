"""Shared installation and release lifecycle for Hopper Files.

Lifecycle operations are deliberately separate from per-instance document and
state cleanup. The module accepts a layout so publication and recovery can be
tested against synthetic directories without touching system paths.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from hopper_files.config import ConfigError, InstanceConfig, load_config
from hopper_files.state import StateError, initialize_state
from hopper_files.systemd_unit import MANAGED_UNIT_MARKER, render_service_unit

INSTALL_ROOT = Path("/opt/hopper-files")
METADATA_ROOT = Path("/var/lib/hopper-files")
UNIT_ROOT = Path("/etc/systemd/system")
ADMIN_WRAPPER = Path("/usr/local/bin/hopper-files-admin")
LAUNCHER = Path("/usr/local/libexec/hopper-files-launch")
LOCK_FILE = Path("/run/lock/hopper-files-lifecycle.lock")
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TREE_ID = re.compile(r"^[0-9a-f]{40,64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_VERSION = 1


class LifecycleError(RuntimeError):
    """A lifecycle operation could not complete without risking existing data."""


@dataclass(frozen=True)
class Layout:
    install_root: Path = INSTALL_ROOT
    metadata_root: Path = METADATA_ROOT
    unit_root: Path = UNIT_ROOT
    admin_wrapper: Path = ADMIN_WRAPPER
    launcher: Path = LAUNCHER
    lock_file: Path = LOCK_FILE

    @property
    def releases(self) -> Path:
        return self.install_root / "releases"

    @property
    def staging(self) -> Path:
        return self.install_root / ".staging"

    @property
    def selector(self) -> Path:
        return self.install_root / "current"

    @property
    def instances(self) -> Path:
        return self.metadata_root / "instances"


@dataclass(frozen=True)
class _VerifiedArtifact:
    archive_bytes: bytes
    archive_sha256: str
    manifest: dict[str, object]


SYSTEM_LAYOUT = Layout()
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def release_id(value: str) -> str:
    if not isinstance(value, str) or RELEASE_ID.fullmatch(value) is None or value in {".", ".."}:
        raise LifecycleError("release identifier is invalid")
    return value


def instance_id(value: str) -> str:
    if not isinstance(value, str) or RELEASE_ID.fullmatch(value) is None or value in {".", ".."}:
        raise LifecycleError("instance identifier is invalid")
    return value


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise LifecycleError(f"release artifact is unreadable: {path}") from exc
    return digest.hexdigest()


def verify_artifact(path: Path, expected_sha256: str) -> dict[str, object]:
    """Check the externally supplied archive digest and its complete manifest."""
    return _load_verified_artifact(path, expected_sha256).manifest


def _load_verified_artifact(path: Path, expected_sha256: str) -> _VerifiedArtifact:
    """Read once, then verify and retain the exact bytes that installation consumes."""
    if SHA256.fullmatch(expected_sha256) is None:
        raise LifecycleError("expected SHA-256 must contain exactly 64 lowercase hexadecimal characters")
    try:
        archive_bytes = path.read_bytes()
    except OSError as exc:
        raise LifecycleError(f"release artifact is unreadable: {path}") from exc
    observed = hashlib.sha256(archive_bytes).hexdigest()
    if observed != expected_sha256:
        raise LifecycleError(f"release artifact SHA-256 mismatch: expected {expected_sha256}, observed {observed}")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for member in archive:
                name = _archive_name(member.name.rstrip("/") if member.isdir() else member.name)
                if name in seen:
                    raise LifecycleError(f"release archive contains a duplicate path: {name}")
                seen.add(name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise LifecycleError(f"release archive contains a non-regular file: {name}")
                source = archive.extractfile(member)
                if source is None:
                    raise LifecycleError(f"release archive member cannot be read: {name}")
                with source:
                    files[name] = source.read()
    except (OSError, tarfile.TarError) as exc:
        raise LifecycleError("release archive is invalid or unreadable") from exc
    manifest_bytes = files.get("release.json")
    if manifest_bytes is None:
        raise LifecycleError("release archive has no release.json manifest")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("release manifest is not valid UTF-8 JSON") from exc
    _validate_release_manifest(manifest)
    declared = manifest["files"]
    actual = set(files) - {"release.json"}
    if actual != set(declared):
        missing = sorted(set(declared) - actual)
        extra = sorted(actual - set(declared))
        raise LifecycleError(f"release file inventory mismatch: missing={missing}, extra={extra}")
    for name, expected in declared.items():
        observed_file_hash = hashlib.sha256(files[name]).hexdigest()
        if observed_file_hash != expected:
            raise LifecycleError(f"release file SHA-256 mismatch: {name}")
    lock = files.get("source/requirements.lock")
    if lock is None or hashlib.sha256(lock).hexdigest() != manifest["requirementsLockSha256"]:
        raise LifecycleError("release dependency lock hash does not match its manifest")
    if b"--index-url https://pypi.org/simple" not in lock:
        raise LifecycleError("release dependency lock does not pin the official Python Package Index")
    return _VerifiedArtifact(archive_bytes, observed, manifest)


def install_shared(
    artifact: Path,
    expected_sha256: str,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    python: str = "/usr/bin/python3",
    runner: CommandRunner = subprocess.run,
) -> str:
    """Create the shared installation from its first verified release."""
    _require_root()
    verified = _load_verified_artifact(artifact, expected_sha256)
    manifest = verified.manifest
    rid = release_id(str(manifest["releaseId"]))
    _preflight_wrapper(layout.admin_wrapper, _admin_wrapper_text(layout))
    _preflight_wrapper(layout.launcher, _launcher_text(layout))
    with _operation_lock(layout.lock_file):
        if layout.selector.exists() or layout.selector.is_symlink():
            raise LifecycleError("a shared installation is already active")
        _ensure_managed_directory(layout.install_root, create=True)
        _ensure_managed_directory(layout.releases, create=True)
        _ensure_managed_directory(layout.staging, create=True)
        _ensure_managed_directory(layout.metadata_root, create=True)
        _ensure_managed_directory(layout.instances, create=True)
        _install_release(verified, layout=layout, python=python, runner=runner)
        _publish_wrapper(layout.admin_wrapper, _admin_wrapper_text(layout))
        _publish_wrapper(layout.launcher, _launcher_text(layout))
        _atomic_select(layout, rid)
    return rid


def update_release(
    artifact: Path,
    expected_sha256: str,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    python: str = "/usr/bin/python3",
    runner: CommandRunner = subprocess.run,
) -> tuple[str, str]:
    """Install an immutable release, atomically select it, and restart services."""
    _require_root()
    verified = _load_verified_artifact(artifact, expected_sha256)
    manifest = verified.manifest
    new_id = release_id(str(manifest["releaseId"]))
    with _operation_lock(layout.lock_file):
        _require_shared_install(layout)
        old_id = _selected_release(layout)
        candidate = layout.releases / new_id
        if candidate.exists() or candidate.is_symlink():
            installed_release = verify_installed_release(layout, new_id)
            installed_record = json.loads((candidate / "installed.json").read_text(encoding="utf-8"))
            if installed_release != manifest or installed_record.get("archiveSha256") != verified.archive_sha256:
                raise LifecycleError(f"release identifier is already bound to different immutable bytes: {new_id}")
        else:
            _install_release(verified, layout=layout, python=python, runner=runner)
        try:
            _atomic_select(layout, new_id)
            _restart_registered(layout, runner)
        except BaseException as exc:
            try:
                _atomic_select(layout, old_id)
                _restart_registered(layout, runner)
            except BaseException as recovery:
                raise LifecycleError(
                    f"update failed and automatic recovery also failed; active selector is {_selected_release(layout)}: {recovery}"
                ) from exc
            raise LifecycleError(f"release update was rolled back to {old_id}: {exc}") from exc
    return old_id, new_id


def rollback_release(
    target_id: str,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    runner: CommandRunner = subprocess.run,
) -> tuple[str, str]:
    """Select a previously installed release after rechecking every file hash."""
    _require_root()
    target_id = release_id(target_id)
    with _operation_lock(layout.lock_file):
        _require_shared_install(layout)
        previous = _selected_release(layout)
        if target_id == previous:
            raise LifecycleError("rollback target is already active")
        verify_installed_release(layout, target_id)
        try:
            _atomic_select(layout, target_id)
            _restart_registered(layout, runner)
        except BaseException as exc:
            try:
                _atomic_select(layout, previous)
                _restart_registered(layout, runner)
            except BaseException as recovery:
                raise LifecycleError(
                    f"rollback failed and automatic recovery also failed; active selector is {_selected_release(layout)}: {recovery}"
                ) from exc
            raise LifecycleError(f"rollback was reversed to {previous}: {exc}") from exc
    return previous, target_id


def migrate_release(
    artifact: Path,
    expected_sha256: str,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    python: str = "/usr/bin/python3",
    runner: CommandRunner = subprocess.run,
    confirm: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    """Move every registered instance from the root-based model (HF-NAV-012).

    The shared selector serves every instance, so the procedure stops them all,
    converts each state as its service account, replaces configuration and
    units, and only then switches the selector and starts them. If one instance
    cannot be converted, the converted ones return and the previous release
    stays selected.
    """
    _require_root()
    verified = _load_verified_artifact(artifact, expected_sha256)
    new_id = release_id(str(verified.manifest["releaseId"]))
    with _operation_lock(layout.lock_file):
        _require_shared_install(layout)
        old_id = _selected_release(layout)
        if old_id == new_id:
            raise LifecycleError("the migration target is already the active release")
        candidate = layout.releases / new_id
        if candidate.exists() or candidate.is_symlink():
            if verify_installed_release(layout, new_id) != verified.manifest:
                raise LifecycleError(f"release identifier is already bound to different immutable bytes: {new_id}")
        else:
            _install_release(verified, layout=layout, python=python, runner=runner)
        plans = [_migration_plan(layout, entry) for entry in _registered_instances(layout)]
        for plan in plans:
            if plan["configVersion"] != 1:
                raise LifecycleError(f"instance configuration is not version 1: {plan['configPath']}")
        _summarize_or_refuse(confirm, "migrate", old_id, new_id, plans)
        for plan in plans:
            _check_pending(plan, layout.releases / new_id, Path(str(plan["configPath"])))
        units = [str(plan["unit"]) for plan in plans]
        for unit in units:
            _run_systemctl(runner, "stop", unit)
        converted: list[dict[str, object]] = []
        reports: dict[str, object] = {}
        try:
            for plan in plans:
                config_path = Path(str(plan["configPath"]))
                backup = _config_backup_path(config_path)
                original = config_path.read_bytes()
                if not backup.exists():
                    _write_like(backup, original, config_path)
                elif backup.read_bytes() != original:
                    raise LifecycleError(f"a different saved version 1 configuration already exists: {backup}")
                unit_path = _unit_path(layout, str(plan["instanceId"]))
                _require_managed_unit(unit_path)
                unit_backup = _unit_backup_path(layout, str(plan["instanceId"]))
                if not unit_backup.exists():
                    _atomic_write(unit_backup, unit_path.read_bytes(), mode=0o644)
                converted.append(plan)
                reports[str(plan["instanceId"])] = _migrate_instance_state(plan, layout.releases / new_id, "forward", backup)
                _write_like(config_path, _single_base_config(original), config_path)
                config = load_config(config_path)
                _atomic_write(unit_path, _render_instance_unit(config, layout).encode("utf-8"), mode=0o644)
            _atomic_select(layout, new_id)
            _run_systemctl(runner, "daemon-reload")
            for unit in units:
                _run_systemctl(runner, "start", unit)
                _run_systemctl(runner, "is-active", unit)
        except BaseException as exc:
            problems = _return_converted(layout, runner, converted, old_id, layout.releases / new_id)
            detail = f"; recovery problems: {'; '.join(problems)}" if problems else ""
            raise LifecycleError(
                f"migration stopped and every instance was returned to {old_id}: {exc}{detail}"
            ) from exc
    return {"previous": old_id, "current": new_id, "reports": reports}


def return_release(
    target_id: str,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    runner: CommandRunner = subprocess.run,
    confirm: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    """Return every instance to the root-based release it came from (HF-NAV-012)."""
    _require_root()
    target_id = release_id(target_id)
    with _operation_lock(layout.lock_file):
        _require_shared_install(layout)
        current_id = _selected_release(layout)
        if current_id == target_id:
            raise LifecycleError("the return target is already the active release")
        verify_installed_release(layout, target_id)
        plans = [_migration_plan(layout, entry) for entry in _registered_instances(layout)]
        for plan in plans:
            backup = _config_backup_path(Path(str(plan["configPath"])))
            if plan["configVersion"] != 2 or not backup.is_file():
                raise LifecycleError(f"instance has no saved version 1 configuration to return to: {plan['configPath']}")
            if not _unit_backup_path(layout, str(plan["instanceId"])).is_file():
                raise LifecycleError(f"instance has no saved previous unit: {plan['instanceId']}")
        _summarize_or_refuse(confirm, "return", current_id, target_id, plans)
        current_release = layout.releases / current_id
        for plan in plans:
            _check_pending(plan, current_release, _config_backup_path(Path(str(plan["configPath"]))))
        units = [str(plan["unit"]) for plan in plans]
        for unit in units:
            _run_systemctl(runner, "stop", unit)
        returned: list[tuple[dict[str, object], bytes, bytes]] = []
        reports: dict[str, object] = {}
        try:
            for plan in plans:
                config_path = Path(str(plan["configPath"]))
                unit_path = _unit_path(layout, str(plan["instanceId"]))
                previous = (plan, config_path.read_bytes(), unit_path.read_bytes())
                returned.append(previous)
                backup = _config_backup_path(config_path)
                reports[str(plan["instanceId"])] = _migrate_instance_state(plan, current_release, "return", backup)
                _write_like(config_path, backup.read_bytes(), config_path)
                _atomic_write(unit_path, _unit_backup_path(layout, str(plan["instanceId"])).read_bytes(), mode=0o644)
            _atomic_select(layout, target_id)
            _run_systemctl(runner, "daemon-reload")
            for unit in units:
                _run_systemctl(runner, "start", unit)
                _run_systemctl(runner, "is-active", unit)
        except BaseException as exc:
            problems: list[str] = []
            for unit in units:
                _best_effort_systemctl(runner, "stop", unit)
            for plan, config_bytes, unit_bytes in reversed(returned):
                try:
                    config_path = Path(str(plan["configPath"]))
                    _migrate_instance_state(plan, current_release, "forward", _config_backup_path(config_path))
                    _write_like(config_path, config_bytes, config_path)
                    _atomic_write(_unit_path(layout, str(plan["instanceId"])), unit_bytes, mode=0o644)
                except BaseException as recovery:
                    problems.append(f"{plan['instanceId']}: {recovery}")
            try:
                _atomic_select(layout, current_id)
            except BaseException as recovery:
                problems.append(f"selector: {recovery}")
            _best_effort_systemctl(runner, "daemon-reload")
            for unit in units:
                _best_effort_systemctl(runner, "start", unit)
            detail = f"; recovery problems: {'; '.join(problems)}" if problems else ""
            raise LifecycleError(f"return stopped and every instance stayed on {current_id}: {exc}{detail}") from exc
    return {"previous": current_id, "current": target_id, "reports": reports}


def _migration_plan(layout: Layout, entry: dict[str, object]) -> dict[str, object]:
    config_path = Path(str(entry["configPath"]))
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"instance configuration is unreadable: {config_path}") from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    return {**entry, "configVersion": version}


def _summarize_or_refuse(
    confirm: Callable[[str], bool] | None,
    direction: str,
    current: str,
    target: str,
    plans: list[dict[str, object]],
) -> None:
    """Show the effective choices and require confirmation (HF-INST-001)."""
    if confirm is None:
        return
    lines = [f"{'Migração' if direction == 'migrate' else 'Retorno'}: {current} -> {target}"]
    lines.extend(
        f"- {plan['instanceId']} (conta {plan['serviceAccount']}; configuração {plan['configPath']}; estado {plan['stateDirectory']})"
        for plan in plans
    )
    if not confirm("\n".join(lines)):
        raise LifecycleError("the administrator did not confirm the procedure; nothing was changed")


def _check_pending(plan: dict[str, object], release: Path, config_v1: Path) -> None:
    completed = _run_as_service_account(
        str(plan["serviceAccount"]),
        release,
        _migration_arguments(release, "check", plan, config_v1),
    )
    if completed.returncode != 0:
        detail = (completed.stdout or completed.stderr or "state check failed").strip()[-2000:]
        raise LifecycleError(f"instance {plan['instanceId']} cannot start the procedure; nothing was changed: {detail}")


def _migrate_instance_state(plan: dict[str, object], release: Path, direction: str, config_v1: Path) -> dict[str, object]:
    completed = _run_as_service_account(
        str(plan["serviceAccount"]),
        release,
        _migration_arguments(release, direction, plan, config_v1),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "state conversion failed").strip()[-2000:]
        raise LifecycleError(f"state {direction} conversion failed for {plan['instanceId']}: {detail}")
    try:
        return json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LifecycleError(f"state conversion for {plan['instanceId']} returned no report") from exc


def _migration_arguments(release: Path, direction: str, plan: dict[str, object], config_v1: Path) -> list[str]:
    code = (
        "import sys; sys.path[:0] = sys.argv[1:3]; "
        "from hopper_files.migration import main; raise SystemExit(main(sys.argv[3:]))"
    )
    return [
        "-c",
        code,
        str(release / "source/src"),
        str(release / "dependencies"),
        direction,
        "--config-v1",
        str(config_v1),
        "--state",
        str(plan["stateDirectory"]),
        "--instance-id",
        str(plan["instanceId"]),
    ]


def _return_converted(
    layout: Layout,
    runner: CommandRunner,
    converted: list[dict[str, object]],
    old_id: str,
    new_release: Path,
) -> list[str]:
    """Bring already converted instances back to the previous release."""
    problems: list[str] = []
    for plan in reversed(converted):
        config_path = Path(str(plan["configPath"]))
        backup = _config_backup_path(config_path)
        iid = str(plan["instanceId"])
        try:
            if json.loads(config_path.read_text(encoding="utf-8")).get("version") == 2 or _state_is_single_base(plan):
                _migrate_instance_state(plan, new_release, "return", backup)
            _write_like(config_path, backup.read_bytes(), config_path)
            _atomic_write(_unit_path(layout, iid), _unit_backup_path(layout, iid).read_bytes(), mode=0o644)
        except BaseException as exc:
            problems.append(f"{iid}: {exc}")
    try:
        if _selected_release(layout) != old_id:
            _atomic_select(layout, old_id)
    except BaseException as exc:
        problems.append(f"selector: {exc}")
    _best_effort_systemctl(runner, "daemon-reload")
    for entry in _registered_instances(layout):
        _best_effort_systemctl(runner, "start", str(entry["unit"]))
    return problems


def _state_is_single_base(plan: dict[str, object]) -> bool:
    record = Path(str(plan["stateDirectory"])) / "migration" / "record.json"
    try:
        return json.loads(record.read_text(encoding="utf-8")).get("format") == 2
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return True


def _single_base_config(original: bytes) -> bytes:
    """Return the version 2 configuration: no roots, System view, or protected paths."""
    payload = json.loads(original.decode("utf-8"))
    for name in ("roots", "system", "protectedPaths", "creationPolicies"):
        payload.pop(name, None)
    payload["version"] = 2
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _config_backup_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}.v1-before-single-base{config_path.suffix}")


def _unit_backup_path(layout: Layout, iid: str) -> Path:
    return layout.metadata_root / "migration" / f"hopper-files-{instance_id(iid)}.v1.service"


def _write_like(path: Path, content: bytes, template: Path) -> None:
    """Atomically write administrative configuration with the owner, group, and mode of template."""
    info = template.stat()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if os.geteuid() == 0:
            os.fchown(fd, info.st_uid, info.st_gid)
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("configuration write was incomplete")
            view = view[count:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def verify_installed_release(layout: Layout, rid: str) -> dict[str, object]:
    """Recheck source, dependency lock, report, and installed file hashes."""
    rid = release_id(rid)
    directory = layout.releases / rid
    try:
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise LifecycleError(f"installed release is not a real directory: {directory}")
        if stat.S_IMODE(details.st_mode) & 0o222:
            raise LifecycleError(f"installed release root is writable: {directory}")
        if os.geteuid() == 0 and details.st_uid != 0:
            raise LifecycleError(f"installed release root is not owned by root: {directory}")
        manifest_path = directory / "release.json"
        installed_path = directory / "installed.json"
        release_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        installed_manifest = json.loads(installed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"installed release metadata is unavailable: {directory}") from exc
    _validate_release_manifest(release_manifest)
    if release_manifest["releaseId"] != rid:
        raise LifecycleError(f"release identifier in manifest does not match directory: {rid}")
    _validate_installed_manifest(installed_manifest, release_manifest)
    observed = _tree_file_hashes(directory, exclude={"installed.json"})
    if observed != installed_manifest["files"]:
        raise LifecycleError(f"installed release file hash mismatch: {rid}")
    source_files = {
        name: digest
        for name, digest in observed.items()
        if name.startswith("source/")
    }
    if source_files != release_manifest["files"]:
        raise LifecycleError(f"installed source does not match its release manifest: {rid}")
    lock_path = directory / "source/requirements.lock"
    if hashlib.sha256(lock_path.read_bytes()).hexdigest() != release_manifest["requirementsLockSha256"]:
        raise LifecycleError(f"installed release dependency lock hash mismatch: {rid}")
    report_path = directory / "dependencies-install-report.json"
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    if report_hash != installed_manifest["pipReportSha256"]:
        raise LifecycleError(f"installed dependency report hash mismatch: {rid}")
    _validate_pip_report(report_path, str(release_manifest["requirementsLockSha256"]))
    for path in directory.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not (stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode)):
            raise LifecycleError(f"installed release contains a non-regular object: {path}")
        if stat.S_IMODE(details.st_mode) & 0o222:
            raise LifecycleError(f"installed release is writable: {path}")
        if os.geteuid() == 0 and details.st_uid != 0:
            raise LifecycleError(f"installed release file is not root-owned: {path}")
    return release_manifest


def register_instance(
    config_path: Path,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    runner: CommandRunner = subprocess.run,
) -> str:
    """Register a configured instance and install/start its systemd unit."""
    _require_root()
    with _operation_lock(layout.lock_file):
        _require_shared_install(layout)
        config = _validate_as_service_account(config_path, layout)
        iid = instance_id(config.instance_id)
        metadata_path = _instance_metadata_path(layout, iid)
        unit_path = _unit_path(layout, iid)
        if metadata_path.exists() or metadata_path.is_symlink() or unit_path.exists() or unit_path.is_symlink():
            raise LifecycleError(f"instance is already registered or its managed path is occupied: {iid}")
        metadata = {
            "schema": SCHEMA_VERSION,
            "instanceId": iid,
            "configPath": str(config.source_path),
            "serviceAccount": config.service_account,
            "stateDirectory": str(config.state_directory),
            "unit": unit_path.name,
        }
        unit_text = _render_instance_unit(config, layout)
        try:
            _atomic_write(metadata_path, _json_bytes(metadata), mode=0o644)
            _atomic_write(unit_path, unit_text.encode("utf-8"), mode=0o644)
            _run_systemctl(runner, "daemon-reload")
            _run_systemctl(runner, "enable", "--now", unit_path.name)
            _run_systemctl(runner, "is-active", unit_path.name)
        except BaseException as exc:
            _best_effort_systemctl(runner, "disable", "--now", unit_path.name)
            unit_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            _best_effort_systemctl(runner, "daemon-reload")
            raise LifecycleError(
                f"instance registration failed; config, documents, and state were preserved: {exc}"
            ) from exc
    return iid


def update_instance(
    config_path: Path,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    runner: CommandRunner = subprocess.run,
) -> str:
    """Apply a configuration edit to its existing service unit."""
    _require_root()
    with _operation_lock(layout.lock_file):
        _require_shared_install(layout)
        try:
            initial = load_config(config_path.absolute())
        except ConfigError as exc:
            raise LifecycleError(f"updated instance configuration was refused: {exc}") from exc
        iid = instance_id(initial.instance_id)
        metadata_path = _instance_metadata_path(layout, iid)
        metadata = _read_instance_metadata(metadata_path, iid)
        if str(initial.source_path) != metadata["configPath"]:
            raise LifecycleError(f"instance configuration path changed for {iid}; re-register explicitly")
        if initial.service_account != metadata["serviceAccount"]:
            raise LifecycleError(f"service account changes require a separate instance registration: {iid}")
        if str(initial.state_directory) != metadata["stateDirectory"]:
            raise LifecycleError(f"state directory changes require a separate instance registration: {iid}")
        config = _validate_as_service_account(config_path, layout)
        unit_path = _unit_path(layout, iid)
        if not unit_path.is_file() or unit_path.is_symlink():
            raise LifecycleError(f"managed service unit is missing or unsafe: {unit_path}")
        old_text = unit_path.read_bytes()
        new_text = _render_instance_unit(config, layout).encode("utf-8")
        try:
            _atomic_write(unit_path, new_text, mode=0o644)
            _run_systemctl(runner, "daemon-reload")
            _run_systemctl(runner, "restart", unit_path.name)
            _run_systemctl(runner, "is-active", unit_path.name)
        except BaseException as exc:
            try:
                _atomic_write(unit_path, old_text, mode=0o644)
                _run_systemctl(runner, "daemon-reload")
                _run_systemctl(runner, "restart", unit_path.name)
            except BaseException as recovery:
                raise LifecycleError(f"instance update failed and prior unit recovery failed: {recovery}") from exc
            raise LifecycleError(f"instance update was reverted; configuration, documents, and state were preserved: {exc}") from exc
    return iid


def remove_instance(
    iid: str,
    *,
    layout: Layout = SYSTEM_LAYOUT,
    runner: CommandRunner = subprocess.run,
) -> tuple[Path, Path, Path]:
    """Stop and unregister an instance without deleting its config, state, or documents."""
    _require_root()
    iid = instance_id(iid)
    with _operation_lock(layout.lock_file):
        metadata_path = _instance_metadata_path(layout, iid)
        metadata = _read_instance_metadata(metadata_path, iid)
        unit_path = _unit_path(layout, iid)
        _require_managed_unit(unit_path)
        previous_unit = unit_path.read_bytes()
        try:
            _run_systemctl(runner, "disable", "--now", unit_path.name)
            unit_path.unlink()
            _run_systemctl(runner, "daemon-reload")
        except BaseException as exc:
            if not unit_path.exists():
                _atomic_write(unit_path, previous_unit, mode=0o644)
                _best_effort_systemctl(runner, "daemon-reload")
                _best_effort_systemctl(runner, "enable", "--now", unit_path.name)
            raise LifecycleError(f"instance removal failed; configuration, state, and documents were preserved: {exc}") from exc
        metadata_path.unlink()
        _fsync_directory(metadata_path.parent)
    return Path(str(metadata["configPath"])), Path(str(metadata["stateDirectory"])), unit_path


def uninstall_shared(
    *,
    layout: Layout = SYSTEM_LAYOUT,
    runner: CommandRunner = subprocess.run,
) -> tuple[int, tuple[Path, ...]]:
    """Remove shared code and units while preserving external config/data/state."""
    _require_root()
    with _operation_lock(layout.lock_file):
        _require_shared_install(layout)
        entries = _registered_instances(layout)
        configs = tuple(Path(str(item["configPath"])) for item in entries)
        units = [_unit_path(layout, str(item["instanceId"])) for item in entries]
        for path in units:
            _require_managed_unit(path)
        _preflight_wrapper(layout.admin_wrapper, _admin_wrapper_text(layout))
        _preflight_wrapper(layout.launcher, _launcher_text(layout))
        _preflight_install_tree(layout)
        old_units = {path: path.read_bytes() for path in units}
        removed: list[Path] = []
        try:
            for path in units:
                _run_systemctl(runner, "disable", "--now", path.name)
                path.unlink()
                removed.append(path)
            _run_systemctl(runner, "daemon-reload")
        except BaseException as exc:
            for path in units:
                if path in removed:
                    _atomic_write(path, old_units[path], mode=0o644)
            _best_effort_systemctl(runner, "daemon-reload")
            for path in units:
                _best_effort_systemctl(runner, "enable", "--now", path.name)
            raise LifecycleError(f"uninstall stopped before deleting shared files; instance units were restored: {exc}") from exc

        _remove_wrapper_if_managed(layout.admin_wrapper, _admin_wrapper_text(layout))
        _remove_wrapper_if_managed(layout.launcher, _launcher_text(layout))
        _safe_remove_install_root(layout)
        for metadata in entries:
            _instance_metadata_path(layout, str(metadata["instanceId"])).unlink(missing_ok=True)
        _remove_empty_managed_directory(layout.instances)
        _remove_empty_managed_directory(layout.metadata_root)
    return len(entries), configs


def _install_release(
    verified: _VerifiedArtifact,
    *,
    layout: Layout,
    python: str,
    runner: CommandRunner,
) -> Path:
    observed = hashlib.sha256(verified.archive_bytes).hexdigest()
    if observed != verified.archive_sha256:
        raise LifecycleError("verified release bytes changed before installation")
    _ensure_managed_directory(layout.releases, create=True)
    _ensure_managed_directory(layout.staging, create=True)
    manifest = verified.manifest
    rid = release_id(str(manifest["releaseId"]))
    destination = layout.releases / rid
    if destination.exists() or destination.is_symlink():
        raise LifecycleError(f"release identifier already exists and immutable releases are never replaced: {rid}")
    staging = layout.staging / f"{rid}.{uuid.uuid4().hex}"
    _make_directory(staging, 0o700)
    published = False
    try:
        _extract_verified_artifact(verified, staging)
        report = staging / "dependencies-install-report.json"
        lock = staging / "source/requirements.lock"
        command = [
            python,
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--require-hashes",
            "--only-binary=:all:",
            "--index-url",
            "https://pypi.org/simple",
            "--target",
            str(staging / "dependencies"),
            "--report",
            str(report),
            "-r",
            str(lock),
        ]
        environment = {
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
        }
        try:
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=900,
            )
        except FileNotFoundError as exc:
            raise LifecycleError(
                "the operating-system Python pip is unavailable; install python3-pip from the signed distribution repository"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LifecycleError("hash-locked dependency installation exceeded 15 minutes") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "no installer output").strip()[-4000:]
            raise LifecycleError(f"pinned dependency installation failed without activating the release: {detail}")
        _validate_pip_report(report, str(manifest["requirementsLockSha256"]))
        installed = {
            "schema": SCHEMA_VERSION,
            "releaseId": rid,
            "sourceTree": manifest["sourceTree"],
            "archiveSha256": verified.archive_sha256,
            "requirementsLockSha256": manifest["requirementsLockSha256"],
            "pipReportSha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "installedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": _tree_file_hashes(staging, exclude={"installed.json"}),
        }
        _atomic_write(staging / "installed.json", _json_bytes(installed), mode=0o600)
        os.rename(staging, destination)
        published = True
        _freeze_release_tree(destination)
        _fsync_directory(layout.releases)
        verify_installed_release(layout, rid)
        return destination
    except BaseException:
        _make_tree_writable(staging)
        shutil.rmtree(staging, ignore_errors=True)
        if published:
            _make_tree_writable(destination)
            shutil.rmtree(destination, ignore_errors=True)
        raise


def _extract_verified_artifact(
    verified: _VerifiedArtifact,
    destination: Path,
) -> None:
    if hashlib.sha256(verified.archive_bytes).hexdigest() != verified.archive_sha256:
        raise LifecycleError("verified release bytes changed before extraction")
    try:
        with tarfile.open(fileobj=io.BytesIO(verified.archive_bytes), mode="r:gz") as archive:
            for member in archive:
                name = _archive_name(member.name.rstrip("/") if member.isdir() else member.name)
                target = destination.joinpath(*PurePosixPath(name).parts)
                if not target.is_relative_to(destination):
                    raise LifecycleError(f"release archive path escapes its staging directory: {name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                if not member.isfile():
                    raise LifecycleError(f"release archive contains a non-regular file: {name}")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                source = archive.extractfile(member)
                if source is None:
                    raise LifecycleError(f"release archive member cannot be read: {name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    except (OSError, tarfile.TarError) as exc:
        raise LifecycleError("verified release archive could not be extracted") from exc
    (destination / "release.json").chmod(0o644)


def _validate_release_manifest(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "releaseId",
        "sourceTree",
        "requirementsLockSha256",
        "files",
    }:
        raise LifecycleError("release manifest fields are invalid")
    if value["schema"] != SCHEMA_VERSION or isinstance(value["schema"], bool):
        raise LifecycleError("release manifest schema is unsupported")
    release_id(value["releaseId"] if isinstance(value["releaseId"], str) else "")
    if not isinstance(value["sourceTree"], str) or TREE_ID.fullmatch(value["sourceTree"]) is None:
        raise LifecycleError("release source tree identifier is invalid")
    if not isinstance(value["requirementsLockSha256"], str) or SHA256.fullmatch(value["requirementsLockSha256"]) is None:
        raise LifecycleError("release dependency lock SHA-256 is invalid")
    files = value["files"]
    if not isinstance(files, dict) or "source/requirements.lock" not in files:
        raise LifecycleError("release file inventory is invalid")
    for name, digest in files.items():
        if not isinstance(name, str) or _archive_name(name) != name or name == "release.json":
            raise LifecycleError("release file inventory contains an invalid path")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise LifecycleError(f"release file inventory has an invalid hash: {name}")


def _validate_installed_manifest(installed: object, release: dict[str, object]) -> None:
    if not isinstance(installed, dict) or set(installed) != {
        "schema",
        "releaseId",
        "sourceTree",
        "archiveSha256",
        "requirementsLockSha256",
        "pipReportSha256",
        "installedAtUtc",
        "files",
    }:
        raise LifecycleError("installed release record is invalid")
    if (
        installed["schema"] != SCHEMA_VERSION
        or installed["releaseId"] != release["releaseId"]
        or installed["sourceTree"] != release["sourceTree"]
        or installed["requirementsLockSha256"] != release["requirementsLockSha256"]
        or not isinstance(installed["archiveSha256"], str)
        or SHA256.fullmatch(installed["archiveSha256"]) is None
        or not isinstance(installed["pipReportSha256"], str)
        or SHA256.fullmatch(installed["pipReportSha256"]) is None
        or not isinstance(installed["installedAtUtc"], str)
        or not isinstance(installed["files"], dict)
    ):
        raise LifecycleError("installed release record does not bind to its source release")


def _validate_pip_report(path: Path, lock_digest: str) -> None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("pip did not produce an installation report") from exc
    installs = report.get("install") if isinstance(report, dict) else None
    if not isinstance(installs, list) or not installs:
        raise LifecycleError("pip installation report has no distributions")
    observed: dict[str, tuple[str, str]] = {}
    for item in installs:
        if not isinstance(item, dict):
            raise LifecycleError("pip installation report contains an invalid entry")
        download = item.get("download_info")
        url = download.get("url") if isinstance(download, dict) else None
        archive = download.get("archive_info") if isinstance(download, dict) else None
        hashes = archive.get("hashes") if isinstance(archive, dict) else None
        if (
            not isinstance(url, str)
            or not url.startswith("https://files.pythonhosted.org/")
            or not isinstance(hashes, dict)
            or not isinstance(hashes.get("sha256"), str)
            or SHA256.fullmatch(hashes["sha256"]) is None
        ):
            raise LifecycleError("dependency report contains a non-official or unhashed package artifact")
        metadata = item.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not isinstance(version, str):
            raise LifecycleError("dependency report omits a package name or version")
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name in observed:
            raise LifecycleError(f"dependency report repeats a package: {name}")
        observed[normalized_name] = (version, hashes["sha256"])
    lock = path.parent / "source/requirements.lock"
    lock_bytes = lock.read_bytes()
    if hashlib.sha256(lock_bytes).hexdigest() != lock_digest:
        raise LifecycleError("dependency lock changed during package installation")
    expected_text: dict[str, tuple[str, set[str]]] = {}
    active: str | None = None
    for name, version in re.findall(rb"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+)", lock_bytes):
        normalized_name = re.sub(rb"[-_.]+", b"-", name).lower().decode("ascii")
        expected_text[normalized_name] = (version.decode("ascii"), set())
        active = normalized_name
    for line in lock_bytes.decode("utf-8").splitlines():
        package = re.match(r"^([A-Za-z0-9_.-]+)==", line)
        if package:
            active = re.sub(r"[-_.]+", "-", package.group(1)).lower()
            continue
        digest = re.search(r"--hash=sha256:([0-9a-f]{64})", line)
        if active is not None and digest:
            expected_text[active][1].add(digest.group(1))
        if active is not None and "# via" in line:
            active = None
    if set(observed) != set(expected_text):
        raise LifecycleError("installed dependency set does not exactly match the hash-locked file")
    for name, (version, digest) in observed.items():
        locked_version, allowed_hashes = expected_text[name]
        if version != locked_version:
            raise LifecycleError(f"installed dependency version does not match lock: {name}=={version}")
        if digest not in allowed_hashes:
            raise LifecycleError(f"installed dependency archive hash is not permitted by lock: {name}")


def _archive_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise LifecycleError(f"release archive contains an unsafe path: {value!r}")
    return value


def _tree_file_hashes(root: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    excluded = exclude or set()
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise LifecycleError(f"release tree contains a non-regular object: {path}")
        if relative in excluded:
            continue
        result[relative] = _file_sha256(path)
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _freeze_release_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda entry: len(entry.parts), reverse=True):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise LifecycleError(f"release contains a non-regular object: {path}")
        if os.geteuid() == 0:
            os.chown(path, 0, 0, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o555)
        else:
            os.chmod(path, 0o555 if info.st_mode & 0o111 else 0o444)
    if os.geteuid() == 0:
        os.chown(root, 0, 0, follow_symlinks=False)
    os.chmod(root, 0o555)


def _make_tree_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda entry: len(entry.parts), reverse=True):
        try:
            if path.is_symlink():
                continue
            if path.is_dir():
                path.chmod(0o700)
            else:
                path.chmod(0o600)
        except OSError:
            pass
    try:
        root.chmod(0o700)
    except OSError:
        pass


def _ensure_managed_directory(path: Path, *, create: bool) -> None:
    if not path.exists() and not path.is_symlink():
        if not create:
            raise LifecycleError(f"managed directory is missing: {path}")
        _make_directory(path, 0o755)
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LifecycleError(f"managed path is not a real directory: {path}")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise LifecycleError(f"managed directory is writable by group or other: {path}")
    if os.geteuid() == 0 and info.st_uid != 0:
        raise LifecycleError(f"managed directory is not owned by root: {path}")


def _make_directory(path: Path, mode: int) -> None:
    path.mkdir(mode=mode, parents=False, exist_ok=False)
    os.chmod(path, mode)
    if os.geteuid() == 0:
        os.chown(path, 0, 0)
    _fsync_directory(path.parent)


def _operation_lock(path: Path):
    """Serialize system lifecycle operations; callers hold this context."""
    class Lock:
        def __enter__(self):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, exc_type, exc, traceback):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)

    return Lock()


def _atomic_select(layout: Layout, rid: str) -> None:
    rid = release_id(rid)
    release_directory = layout.releases / rid
    verify_installed_release(layout, rid)
    _ensure_managed_directory(layout.install_root, create=False)
    selector = layout.selector
    if selector.exists() and not selector.is_symlink():
        raise LifecycleError(f"active selector is not a symlink: {selector}")
    temporary = selector.with_name(f".current.{uuid.uuid4().hex}")
    target = os.path.relpath(release_directory, selector.parent)
    os.symlink(target, temporary)
    try:
        os.replace(temporary, selector)
        _fsync_directory(selector.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _selected_release(layout: Layout) -> str:
    selector = layout.selector
    if not selector.is_symlink():
        raise LifecycleError("active release selector is missing or is not a symlink")
    target = os.readlink(selector)
    expected_parent = Path("releases")
    target_path = PurePosixPath(target)
    if target_path.is_absolute() or len(target_path.parts) != 2 or target_path.parts[0] != expected_parent.name:
        raise LifecycleError("active release selector points outside the managed release directory")
    rid = release_id(target_path.parts[1])
    if selector.resolve(strict=True) != (layout.releases / rid).resolve(strict=True):
        raise LifecycleError("active release selector target is inconsistent")
    verify_installed_release(layout, rid)
    return rid


def _require_shared_install(layout: Layout) -> None:
    _ensure_managed_directory(layout.install_root, create=False)
    _ensure_managed_directory(layout.releases, create=False)
    _ensure_managed_directory(layout.metadata_root, create=False)
    _ensure_managed_directory(layout.instances, create=False)
    _selected_release(layout)


def _validate_as_service_account(config_path: Path, layout: Layout) -> InstanceConfig:
    """Validate configuration, state, and the KDF as the target service UID."""
    config_path = config_path.absolute()
    try:
        initial = load_config(config_path)
        pwd.getpwnam(initial.service_account)
    except (ConfigError, KeyError, OSError) as exc:
        raise LifecycleError(f"instance configuration or service account is invalid: {exc}") from exc
    code = r"""
import sys
source, dependencies, config_text = sys.argv[1:4]
sys.path[:0] = [source, dependencies]
import json, os
from pathlib import Path
from hopper_files.config import load_config, ConfigError
from hopper_files.state import initialize_state, StateError
from hopper_files.kdf import prove_kdf_runtime, KdfUnavailable
from hopper_files.config import require_service_account, validate_service_identity
from hopper_files.migration import require_current_format
try:
    path = Path(config_text)
    config = load_config(path)
    require_service_account(config)
    if not os.access("/", os.R_OK | os.X_OK):
        raise ConfigError("service account cannot read and traverse /")
    try:
        initialize_state(config.state_directory, config.instance_id)
    except (OSError, StateError) as exc:
        raise ConfigError(f"configured per-instance state path {config.state_directory} failed initialization: {exc}; inspect this exact path and restore only the required service-account access while preserving its existing contents") from exc
    require_current_format(config.state_directory)
    try:
        validate_service_identity(config)
    except ConfigError as exc:
        raise ConfigError(f"configured per-instance state path {config.state_directory} failed validation: {exc}") from exc
    prove_kdf_runtime()
    print(json.dumps({"instanceId": config.instance_id, "serviceAccount": config.service_account, "stateDirectory": str(config.state_directory)}))
except (ConfigError, StateError, KdfUnavailable, OSError) as exc:
    print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
"""
    completed = _run_as_service_account(
        initial.service_account,
        layout.selector,
        ["-c", code, str(layout.selector / "source/src"), str(layout.selector / "dependencies"), str(config_path)],
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "target account validation failed").strip()[-2000:]
        raise LifecycleError(detail)
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LifecycleError("service-account validation did not return a result") from exc
    if result.get("instanceId") != initial.instance_id:
        raise LifecycleError("instance identity changed during access validation")
    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise LifecycleError(f"validated config changed before unit generation: {exc}") from exc


def _run_as_service_account(account_name: str, release: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the release's Python code as the service UID (HF-INST-003)."""
    del release
    try:
        account = pwd.getpwnam(account_name)
        group_ids = tuple(os.getgrouplist(account.pw_name, account.pw_gid))
    except (KeyError, OSError) as exc:
        raise LifecycleError(f"service account is invalid: {account_name}") from exc
    if os.geteuid() != 0:
        raise LifecycleError("service-account steps must run as root to evaluate the actual service UID")
    if account.pw_uid == 0:
        raise LifecycleError("the Hopper Files service account must be an existing non-root account")
    environment = {
        "HOME": account.pw_dir,
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    groups = sorted(set(group_ids))

    def demote() -> None:
        os.setgroups(groups)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        os.umask(0o002)

    try:
        return subprocess.run(
            ["/usr/bin/python3", "-I", "-B", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            preexec_fn=demote,
            cwd="/",
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f"service-account step could not run for {account_name}: {exc}") from exc


def _render_instance_unit(config: InstanceConfig, layout: Layout) -> str:
    return render_service_unit(
        config,
        config_path=config.source_path,
        active_root=layout.selector,
        launcher=layout.launcher,
    )


def _instance_metadata_path(layout: Layout, iid: str) -> Path:
    return layout.instances / f"{instance_id(iid)}.json"


def _unit_path(layout: Layout, iid: str) -> Path:
    return layout.unit_root / f"hopper-files-{instance_id(iid)}.service"


def _read_instance_metadata(path: Path, expected_id: str) -> dict[str, object]:
    try:
        if path.is_symlink() or not path.is_file():
            raise LifecycleError(f"instance registration record is missing or unsafe: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"instance registration record is unreadable: {path}") from exc
    required = {"schema", "instanceId", "configPath", "serviceAccount", "stateDirectory", "unit"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["schema"] != SCHEMA_VERSION
        or value["instanceId"] != expected_id
        or value["unit"] != _unit_path_from_id(expected_id)
        or not all(isinstance(value[name], str) and value[name] for name in ("configPath", "serviceAccount", "stateDirectory"))
    ):
        raise LifecycleError(f"instance registration record is invalid: {path}")
    return value


def _unit_path_from_id(iid: str) -> str:
    return f"hopper-files-{instance_id(iid)}.service"


def _registered_instances(layout: Layout) -> list[dict[str, object]]:
    entries = []
    for path in sorted(layout.instances.glob("*.json")):
        iid = instance_id(path.stem)
        entries.append(_read_instance_metadata(path, iid))
    return entries


def _restart_registered(layout: Layout, runner: CommandRunner) -> None:
    for metadata in _registered_instances(layout):
        unit = str(metadata["unit"])
        _run_systemctl(runner, "restart", unit)
        _run_systemctl(runner, "is-active", unit)


def _run_systemctl(runner: CommandRunner, *arguments: str) -> None:
    try:
        result = runner(
            ["/usr/bin/systemctl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f"systemctl {' '.join(arguments)} could not complete: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no systemctl output").strip()[-1200:]
        raise LifecycleError(f"systemctl {' '.join(arguments)} failed: {detail}")


def _best_effort_systemctl(runner: CommandRunner, *arguments: str) -> None:
    try:
        _run_systemctl(runner, *arguments)
    except LifecycleError:
        pass


def _require_managed_unit(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_file():
            raise LifecycleError(f"managed unit is missing or unsafe: {path}")
        first = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeError, IndexError) as exc:
        raise LifecycleError(f"managed unit cannot be verified: {path}") from exc
    if first != MANAGED_UNIT_MARKER:
        raise LifecycleError(f"unit is not marked as managed by Hopper Files: {path}")


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, mode)
    try:
        if os.geteuid() == 0:
            os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("atomic lifecycle write was incomplete")
            view = view[count:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _publish_wrapper(path: Path, content: str) -> None:
    _atomic_write(path, content.encode("utf-8"), mode=0o555)


def _preflight_wrapper(path: Path, expected: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != expected:
        raise LifecycleError(f"refusing to overwrite an unrelated executable: {path}")
    info = path.stat()
    if os.geteuid() == 0 and info.st_uid != 0:
        raise LifecycleError(f"existing lifecycle executable is not root-owned: {path}")


def _remove_wrapper_if_managed(path: Path, expected: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _preflight_wrapper(path, expected)
    path.unlink()
    _fsync_directory(path.parent)


def _admin_wrapper_text(layout: Layout) -> str:
    install_root = _shell_quote(str(layout.install_root))
    code = (
        "import sys\n"
        "source, dependencies = sys.argv[1:3]\n"
        "arguments = sys.argv[3:]\n"
        "sys.path[:0] = [source, dependencies]\n"
        "sys.argv = ['hopper-files-admin', *arguments]\n"
        "from hopper_files.admin import main\n"
        "raise SystemExit(main())\n"
    )
    return (
        "#!/bin/sh\nset -eu\n"
        f"install_root={install_root}\n"
        'release=$(/usr/bin/readlink -f "$install_root/current")\n'
        'case "$release" in "$install_root"/releases/*) ;; *) echo "invalid active release selector" >&2; exit 1 ;; esac\n'
        'test -r "$release/release.json"\n'
        "exec /usr/bin/python3 -I -B -c "
        f"{_shell_quote(code)} \"$release/source/src\" \"$release/dependencies\" \"$@\"\n"
    )


def _launcher_text(layout: Layout) -> str:
    root = _shell_quote(str(layout.install_root))
    code = (
        "import sys\n"
        "source, dependencies, release, selector = sys.argv[1:5]\n"
        "sys.path[:0] = [source, dependencies]\n"
        "import os\n"
        "os.environ['HOPPER_FILES_ACTIVE_ROOT'] = release\n"
        "os.environ['HOPPER_FILES_ACTIVE_SELECTOR'] = selector\n"
        "sys.argv = ['hopper-files-runtime']\n"
        "from hopper_files.runtime import main\n"
        "raise SystemExit(main())\n"
    )
    return (
        "#!/bin/sh\nset -eu\n"
        f"install_root={root}\n"
        'release=$(/usr/bin/readlink -f "$install_root/current")\n'
        'case "$release" in "$install_root"/releases/*) ;; *) echo "invalid active release selector" >&2; exit 1 ;; esac\n'
        "test -r \"$release/release.json\"\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        "exec /usr/bin/python3 -I -B -c "
        f"{_shell_quote(code)} \"$release/source/src\" \"$release/dependencies\" \"$release\" \"$install_root/current\"\n"
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _safe_remove_install_root(layout: Layout) -> None:
    _ensure_managed_directory(layout.install_root, create=False)
    _preflight_install_tree(layout)
    if layout.selector.is_symlink():
        layout.selector.unlink()
    _make_tree_writable(layout.releases)
    shutil.rmtree(layout.releases)
    if layout.staging.exists():
        layout.staging.rmdir()
    layout.install_root.rmdir()
    _fsync_directory(layout.install_root.parent)


def _preflight_install_tree(layout: Layout) -> None:
    _ensure_managed_directory(layout.install_root, create=False)
    allowed = {"current", "releases", ".staging"}
    unexpected = {child.name for child in layout.install_root.iterdir()} - allowed
    if unexpected:
        raise LifecycleError(f"shared installation contains unmanaged paths and was preserved: {sorted(unexpected)}")
    for parent in (layout.releases, layout.staging):
        if parent.exists():
            _ensure_managed_directory(parent, create=False)
    if layout.releases.exists():
        for child in layout.releases.iterdir():
            if not child.is_dir() or child.is_symlink():
                raise LifecycleError(f"unmanaged release path was preserved: {child}")
            verify_installed_release(layout, child.name)
    if layout.staging.exists() and any(layout.staging.iterdir()):
        raise LifecycleError("unfinished release staging data was preserved for inspection")


def _remove_empty_managed_directory(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _require_root() -> None:
    if os.geteuid() != 0:
        raise LifecycleError("this lifecycle operation requires local root authorization")
