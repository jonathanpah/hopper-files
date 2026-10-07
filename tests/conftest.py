"""Synthetic instance fixtures. Passwords here are fake test values."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hopper_files.app import create_app
from hopper_files.clock import ManualClock
from hopper_files.config import load_config
from hopper_files.credentials import change_password
from hopper_files.state import initialize_state

# Every instance navigates from / (HF-NAV-001). Tests keep one synthetic
# documents directory at a fixed real path for the session, so addresses can be
# written as constants: ROOT is the fixed base identifier and D is the path of
# the documents directory relative to /. Each test starts with it empty.
ROOT = "fs"
DOCUMENTS = Path(tempfile.gettempdir()).resolve() / f"hopper-files-test-documents-{os.getpid()}"
D = str(DOCUMENTS)[1:]


def doc(relative: str = "") -> str:
    """Address path (relative to /) of an entry inside the test documents directory."""
    return D if relative == "" else f"{D}/{relative}"


def rel(path: str) -> str:
    """Path inside the test documents directory for an address path from the API."""
    if path == D:
        return ""
    assert path.startswith(D + "/"), path
    return path[len(D) + 1 :]


def terminal_mode(kind: str) -> int:
    """Mode a terminal of this process would give a new object (HF-NAV-010)."""
    current = os.umask(0)
    os.umask(current)
    return (0o777 if kind == "directory" else 0o666) & ~current


def set_default_acl(directory: Path, group_id: int) -> None:
    """Give a directory a default POSIX ACL with one named group, without setfacl."""
    import struct

    undefined = 0xFFFFFFFF
    entries = [(0x01, 7, undefined), (0x04, 5, undefined), (0x08, 7, group_id), (0x10, 7, undefined), (0x20, 0, undefined)]
    value = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, perm, ident) for tag, perm, ident in entries)
    os.setxattr(directory, "system.posix_acl_default", value)


def address(relative: str = "") -> dict[str, str]:
    return {"rootId": ROOT, "path": doc(relative)}


@pytest.fixture(autouse=True)
def _fresh_documents(monkeypatch):
    if DOCUMENTS.exists():
        shutil.rmtree(DOCUMENTS)
    DOCUMENTS.mkdir(mode=0o700)
    # Harness isolation: content-reading corpus scans (references, collection)
    # start at the synthetic documents directory, never at the account's real
    # files. The product origin stays / (HF-NAV-004). The tag index reads only
    # monitored folders, and tests mark folders inside this directory.
    import hopper_files.corpus

    monkeypatch.setattr(hopper_files.corpus, "CORPUS_START", D)
    yield
    if DOCUMENTS.exists():
        shutil.rmtree(DOCUMENTS, ignore_errors=True)


def cookie_flags(header: str) -> set[str]:
    return {part.strip().lower() for part in header.split(";")[1:]}


PASSWORD = "synthetic-correct-horse"
OTHER_PASSWORD = "synthetic-other-horse"
CLOCK_START = 1_700_000_000.0


def service_account() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


class World:
    def __init__(self, config, path: Path, clock: ManualClock, client: TestClient) -> None:
        self.config = config
        self.path = path
        self.clock = clock
        self.client = client

    def __enter__(self) -> "World":
        self.client.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.client.__exit__(exc_type, exc, traceback)

    def login_path(self) -> str:
        return self.config.base_path + "login"

    def issue_nonce(self) -> str:
        response = self.client.get(self.login_path(), headers={"accept": "application/json"})
        assert response.status_code == 200, response.text
        return response.json()["nonce"]

    def post_login(self, password: str, nonce: str | None = None, **headers: str):
        request_headers = {"origin": self.config.origin}
        request_headers.update(headers)
        return self.client.post(
            self.login_path(),
            json={"nonce": nonce if nonce is not None else self.issue_nonce(), "password": password},
            headers=request_headers,
            follow_redirects=False,
        )


def write_config(directory: Path, **overrides: object) -> Path:
    port = int(overrides.pop("port", 8765))
    instance_id = str(overrides.pop("instance_id", "alpha"))
    mode = str(overrides.pop("mode", "local"))
    base_path = str(overrides.pop("base_path", "/"))
    state = Path(overrides.pop("state", directory / "state"))
    account = str(overrides.pop("account", service_account()))
    bind = str(overrides.pop("bind", "127.0.0.1"))
    explicit_base_url = overrides.pop("base_url", None)
    if mode == "local":
        base_url = f"http://{bind}:{port}{base_path}"
        proxy = None
    else:
        external_host = str(overrides.pop("external_host", "files.example.test"))
        external_port = int(overrides.pop("external_port", 8443))
        base_url = f"https://{external_host}:{external_port}{base_path}"
        proxy = str(overrides.pop("proxy", "127.0.0.1"))
    if explicit_base_url is not None:
        base_url = str(explicit_base_url)
    payload = {
        "version": 2,
        "instanceId": instance_id,
        "serviceAccount": account,
        "timeZone": str(overrides.pop("timezone", "UTC")),
        "sessionDurationSeconds": int(overrides.pop("duration", 3600)),
        "stateDirectory": str(state),
        "cookieName": str(overrides.pop("cookie_name", f"hf_{instance_id}")),
        "access": {
            "mode": mode,
            "bindHost": bind,
            "bindPort": port,
            "baseUrl": base_url,
            "trustedProxyPeer": proxy,
        },
    }
    if overrides:
        raise AssertionError(f"unknown config overrides: {sorted(overrides)}")
    directory.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    documents = directory / "documents"
    if not documents.exists() and not documents.is_symlink():
        documents.symlink_to(DOCUMENTS, target_is_directory=True)
    path = directory / f"{instance_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def boot(directory: Path, clock: ManualClock | None = None, *, password: str | None = None, client_host: str = "127.0.0.1", **overrides: object) -> World:
    path = write_config(directory, **overrides)
    config = load_config(path)
    initialize_state(config.state_directory, config.instance_id)
    if password is not None:
        change_password(
            config.state_directory,
            password,
            instance_id=config.instance_id,
        )
    app = create_app(config, clock or ManualClock(CLOCK_START))
    headers = {}
    if config.access_mode == "remote":
        headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": config.host_header,
        }
    client = TestClient(
        app,
        base_url=f"http://{config.host_header}",
        client=(client_host, 40000),
        headers=headers,
    )
    return World(config, path, app.state.runtime.clock, client)
