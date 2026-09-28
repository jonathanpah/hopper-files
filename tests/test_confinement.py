from __future__ import annotations

import ast
import grp
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import hopper_files
from hopper_files.app import create_app
from hopper_files.config import ConfigError, load_config
from hopper_files.runtime import server_options
from hopper_files.state import StateError, initialize_state
from hopper_files.systemd_unit import render_service_unit, service_environment

from conftest import service_account, write_config


def test_unit_has_no_file_restrictions_and_keeps_no_new_privileges(tmp_path) -> None:
    """HF-NAV-010/HF-ACC-026: no filesystem restriction beyond Linux permissions."""
    path = write_config(tmp_path, account=service_account(), instance_id="unit")
    config = load_config(path)
    initialize_state(config.state_directory, config.instance_id)
    text = render_service_unit(config, config_path=path)
    account = pwd.getpwnam(service_account())
    group_name = grp.getgrgid(account.pw_gid).gr_name
    required = [
        f"User={account.pw_name}",
        f"Group={group_name}",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "UMask=0002",
        "MemoryMax=512M",
        "Environment=PATH=/usr/bin:/bin",
        "Environment=LANG=C.UTF-8",
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        f'Environment="HOPPER_FILES_INSTANCE={path}"',
        "UnsetEnvironment=SSH_AUTH_SOCK",
        "UnsetEnvironment=SHELL",
        "ExecStart=/usr/local/libexec/hopper-files-launch",
    ]
    lines = text.splitlines()
    for line in required:
        assert line in lines
    for directive in ("ProtectSystem", "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "PrivateTmp", "ProtectHome"):
        assert not any(line.startswith(directive + "=") for line in lines), directive
    assert "SSH_AUTH_SOCK=" not in text
    assert "sudo" not in text.lower()
    assert service_environment(path) == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOPPER_FILES_INSTANCE": str(path),
    }


def test_component_applies_terminal_umask_and_environment(tmp_path) -> None:
    path = write_config(tmp_path, account=service_account(), instance_id="probe")
    config = load_config(path)
    initialize_state(config.state_directory, config.instance_id)
    env = service_environment(path)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        if key in os.environ:
            env[key] = os.environ[key]
    env["ALLOWED"] = str(config.state_directory)
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report == {
        "forced": 0o600,
        "umask_file": 0o664,
        "umask_dir": 0o775,
        "ssh": False,
        "shell": False,
        "path": "/usr/bin:/bin",
    }


def test_package_does_not_invoke_sudo_or_create_accounts() -> None:
    root = Path(hopper_files.__file__).resolve().parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"system", "popen"}
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                assert "sudo" not in lowered
                assert "useradd" not in lowered
                assert "groupadd" not in lowered


def test_listener_binds_only_loopback(tmp_path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    path = write_config(tmp_path, account=service_account(), instance_id="listen", port=port)
    config = load_config(path)
    initialize_state(config.state_directory, config.instance_id)
    options = server_options(config)
    assert options["proxy_headers"] is False
    assert options["host"] == "127.0.0.1"
    import uvicorn

    live = dict(options)
    live["log_level"] = "error"
    server = uvicorn.Server(uvicorn.Config(create_app(config), **live))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        _assert_loopback(port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            assert response.status == 200
    finally:
        server.should_exit = True
        thread.join(5)


def _assert_loopback(port: int) -> None:
    needle = f":{port:04X} "
    # /proc/net/tcp stores the port in the local_address column as hex.
    lines = Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]
    matches = []
    for line in lines:
        local = line.split()[1]
        address, hex_port = local.split(":")
        if int(hex_port, 16) == port:
            matches.append(address)
    assert matches
    assert all(address == "0100007F" for address in matches), matches
    del needle


_PROBE = r"""
import json, os, stat
from pathlib import Path
os.umask(0)
from hopper_files.confinement import apply_service_umask
from hopper_files.state import atomic_write
apply_service_umask()
root = Path(os.environ["ALLOWED"])
atomic_write(root / "forced", b"ok")
fd = os.open(root / "umask-file", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
os.close(fd)
os.mkdir(root / "umask-dir", 0o777)
print(json.dumps({
    "forced": stat.S_IMODE((root / "forced").stat().st_mode),
    "umask_file": stat.S_IMODE((root / "umask-file").stat().st_mode),
    "umask_dir": stat.S_IMODE((root / "umask-dir").stat().st_mode),
    "ssh": "SSH_AUTH_SOCK" in os.environ,
    "shell": "SHELL" in os.environ,
    "path": os.environ.get("PATH", ""),
}))
"""
