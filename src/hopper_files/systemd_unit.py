"""Render an unprivileged systemd unit for the shared active release.

The unit applies no filesystem restriction beyond Linux permissions
(HF-NAV-010): no ProtectSystem, ReadWritePaths, ReadOnlyPaths,
InaccessiblePaths, or PrivateTmp. It keeps NoNewPrivileges and empty
capability sets, and UMask=0002 so new objects match the account's terminal.
"""

from __future__ import annotations

import grp
import pwd
from pathlib import Path

from hopper_files.config import ConfigError, InstanceConfig

SERVICE_MEMORY_MAX = "512M"
SERVICE_MEMORY_MAX_BYTES = 512 * 1024 * 1024
MANAGED_UNIT_MARKER = "# Managed by Hopper Files lifecycle; do not edit."
ENVIRONMENT_ASSIGNMENTS = (
    "PATH=/usr/bin:/bin",
    "LANG=C.UTF-8",
    "PYTHONDONTWRITEBYTECODE=1",
)
UNSET_ENVIRONMENT = (
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SHELL",
    "BASH_ENV",
    "ENV",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PYTHONHOME",
    "HOME",
)
SERVICE_UMASK = "0002"
# Directives HF-NAV-010 forbids in the unit; validation checks the effective unit.
FORBIDDEN_DIRECTIVES = ("ProtectSystem", "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "PrivateTmp")
DEFAULT_ACTIVE_ROOT = Path("/opt/hopper-files/current")
DEFAULT_LAUNCHER = Path("/usr/local/libexec/hopper-files-launch")


def service_environment(config_path: Path) -> dict[str, str]:
    """Environment retained for a service process. No shell or agent variables."""
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOPPER_FILES_INSTANCE": str(config_path),
    }


def render_service_unit(
    config: InstanceConfig,
    *,
    config_path: Path,
    active_root: Path = DEFAULT_ACTIVE_ROOT,
    launcher: Path = DEFAULT_LAUNCHER,
) -> str:
    """Return unit text; selection and installation remain lifecycle operations."""
    try:
        account = pwd.getpwnam(config.service_account)
    except KeyError as exc:
        raise ConfigError("service account does not exist") from exc
    try:
        group_name = grp.getgrgid(account.pw_gid).gr_name
    except KeyError as exc:
        raise ConfigError("service account group does not exist") from exc
    if not active_root.is_absolute() or not launcher.is_absolute():
        raise ConfigError("active release and launcher paths must be absolute")
    environment = service_environment(config_path)
    instance_assignment = _systemd_value(
        "HOPPER_FILES_INSTANCE=" + environment["HOPPER_FILES_INSTANCE"]
    )
    lines = [
        MANAGED_UNIT_MARKER,
        "[Unit]",
        f"Description=Hopper Files instance {config.instance_id}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={account.pw_name}",
        f"Group={group_name}",
        "WorkingDirectory=/",
        f"UMask={SERVICE_UMASK}",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        f"MemoryMax={SERVICE_MEMORY_MAX}",
        *(f"Environment={name}" for name in ENVIRONMENT_ASSIGNMENTS),
        f"Environment={instance_assignment}",
        f"Environment=HOPPER_FILES_ACTIVE_SELECTOR={_systemd_value(str(active_root))}",
        *(f"UnsetEnvironment={name}" for name in UNSET_ENVIRONMENT),
        f"ExecStart={_systemd_value(str(launcher))}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def _systemd_value(value: str) -> str:
    """Quote one systemd value without allowing spaces or specifiers to split it."""
    if value and all(character.isalnum() or character in "/._:+-" for character in value):
        return value
    escaped = (
        value.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'
