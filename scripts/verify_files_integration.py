#!/usr/bin/env python3
"""Exercise auth, roots, durable state, and file operations over loopback HTTP."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

import httpx

from hopper_files.credentials import change_password
from hopper_files.state import initialize_state


PASSWORD = "synthetic-integration-password"



# The synthetic documents directory of this run, relative to / (HF-NAV-005).
_DOCUMENTS: list[str] = [""]


def _p(relative: str = "") -> str:
    """Address path relative to / of an entry in the synthetic documents directory."""
    return _DOCUMENTS[0] if relative == "" else f"{_DOCUMENTS[0]}/{relative}"


# Harness isolation: the runtime process reads note content (tags, image
# references) only below the synthetic documents directory; the product keeps /.
_ISOLATED_RUNTIME = (
    "import os; import hopper_files.corpus as corpus; "
    "corpus.CORPUS_START = os.environ.pop('HF_HARNESS_CORPUS_START'); "
    "from hopper_files.runtime import main; raise SystemExit(main())"
)

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_instance(directory: Path) -> tuple[Path, Path, str, int]:
    documents = directory / "documents"
    _DOCUMENTS[0] = str(documents)[1:]
    state = directory / "state"
    documents.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config_path = directory / "instance.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "instanceId": "files-integration",
                "serviceAccount": pwd.getpwuid(os.geteuid()).pw_name,
                "timeZone": "UTC",
                "sessionDurationSeconds": 3600,
                "stateDirectory": str(state),
                "cookieName": "hf_files_integration",
                "access": {
                    "mode": "local",
                    "bindHost": "127.0.0.1",
                    "bindPort": port,
                    "baseUrl": f"{origin}/",
                    "trustedProxyPeer": None,
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    initialize_state(state, "files-integration")
    change_password(state, PASSWORD, instance_id="files-integration")
    return config_path, documents, origin, port


def _start_runtime(
    config_path: Path, port: int, log_path: Path
) -> tuple[subprocess.Popen[bytes], BinaryIO]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "PYTHONPATH")
        if key in os.environ
    }
    environment["HOPPER_FILES_INSTANCE"] = str(config_path)
    environment["HF_HARNESS_CORPUS_START"] = str(config_path.parent / "documents")[1:]
    log = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-c", _ISOLATED_RUNTIME],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.flush()
            details = log_path.read_text(errors="replace")
            log.close()
            raise RuntimeError(f"runtime exited with {process.returncode}: {details}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process, log
        except OSError:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    log.flush()
    details = log_path.read_text(errors="replace")
    log.close()
    raise RuntimeError(f"runtime did not listen: {details}")


def _login(client: httpx.Client, origin: str) -> str:
    nonce = client.get("/login", headers={"accept": "application/json"})
    assert nonce.status_code == 200
    response = client.post(
        "/login",
        json={"nonce": nonce.json()["nonce"], "password": PASSWORD},
        headers={"origin": origin},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def _token(client: httpx.Client, csrf: str, origin: str) -> str:
    response = client.post("/api/files/token", headers={"origin": origin, "x-csrf-token": csrf})
    assert response.status_code == 201, response.text
    return response.json()["operationToken"]


def _mutate(client: httpx.Client, csrf: str, origin: str, token: str, actions: list[dict[str, object]]):
    response = client.post(
        "/api/files",
        json={"operationToken": token, "actions": actions},
        headers={"origin": origin, "x-csrf-token": csrf},
    )
    assert response.status_code in {200, 207}, response.text
    return response


def _upload(client: httpx.Client, csrf: str, origin: str, token: str, name: str, payload: bytes):
    manifest = {
        "action": "upload",
        "destination": {"rootId": "fs", "path": _p("")},
        "nameMode": "original",
        "files": [{
            "originalName": name,
            "contentDigest": hashlib.sha256(payload).hexdigest(),
            "contentSize": len(payload),
        }],
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return client.post(
        "/api/files/upload",
        content=payload,
        headers={
            "origin": origin,
            "x-csrf-token": csrf,
            "x-hopper-operation-token": token,
            "x-hopper-upload-manifest": encoded,
            "content-type": "application/octet-stream",
        },
    )


def _exercise(client: httpx.Client, documents: Path, origin: str) -> None:
    csrf = _login(client, origin)
    denied = client.post("/api/files/token", headers={"origin": origin})
    assert denied.status_code == 403

    # HF-NAV-001: there is no root discovery route.
    assert client.get("/api/roots").status_code == 404

    state = client.get("/api/state")
    assert state.status_code == 200
    proposal = {key: value for key, value in state.json().items() if key != "stateRevision"}
    proposal["preferences"]["theme"] = "light"
    headers = {"origin": origin, "x-csrf-token": csrf}
    updated = client.put("/api/state", json={"baseRevision": 0, **proposal}, headers=headers)
    stale = client.put("/api/state", json={"baseRevision": 0, **proposal}, headers=headers)
    assert updated.status_code == 200 and updated.json()["stateRevision"] == 1
    assert stale.status_code == 409 and stale.json()["stateRevision"] == 1

    before = client.get("/api/list", params={"rootId": "fs", "path": _p("")})
    assert before.status_code == 200 and before.json()["entries"] == []
    note_action = {
        "action": "create",
        "kind": "note",
        "nameMode": "exact",
        "destination": {"rootId": "fs", "path": _p(".integration-note.md")},
        "title": "Loopback integration",
    }
    note_token = _token(client, csrf, origin)
    note = _mutate(client, csrf, origin, note_token, [note_action])
    assert note.json()["status"] == "completed"
    assert note.json()["committed"][0]["destination"]["path"] == _p(".integration-note.md")
    assert _mutate(client, csrf, origin, note_token, [note_action]).json()["status"] == "completed"

    directory_token = _token(client, csrf, origin)
    made_directories = _mutate(
        client,
        csrf,
        origin,
        directory_token,
        [
            {"action": "create", "kind": "directory", "nameMode": "exact", "destination": {"rootId": "fs", "path": _p("copies")}},
            {"action": "create", "kind": "directory", "nameMode": "exact", "destination": {"rootId": "fs", "path": _p("unpacked")}},
        ],
    )
    assert made_directories.json()["status"] == "completed"

    payload = (bytes(range(256)) * 512) + b" synthetic streamed payload"
    upload_name = " Original Ω.bin "
    upload_token = _token(client, csrf, origin)
    uploaded = _upload(client, csrf, origin, upload_token, upload_name, payload)
    assert uploaded.status_code == 200 and uploaded.json()["status"] == "completed"
    downloaded = client.get("/api/raw", params={"rootId": "fs", "path": _p(upload_name)})
    assert downloaded.status_code == 200 and downloaded.content == payload

    copy_token = _token(client, csrf, origin)
    copied = _mutate(
        client,
        csrf,
        origin,
        copy_token,
        [{"action": "copy", "source": {"rootId": "fs", "path": _p(upload_name)}, "destination": {"rootId": "fs", "path": _p("copies/clone.bin")}}],
    )
    assert copied.json()["status"] == "completed"

    zip_token = _token(client, csrf, origin)
    zipped = _mutate(
        client,
        csrf,
        origin,
        zip_token,
        [{"action": "zip", "sources": [{"rootId": "fs", "path": _p("copies/clone.bin")}], "destination": {"rootId": "fs", "path": _p("clone.zip")}}],
    )
    assert zipped.json()["status"] == "completed"

    extract_token = _token(client, csrf, origin)
    extracted = _mutate(
        client,
        csrf,
        origin,
        extract_token,
        [{"action": "extract", "source": {"rootId": "fs", "path": _p("clone.zip")}, "destination": {"rootId": "fs", "path": _p("unpacked")}}],
    )
    assert extracted.json()["status"] == "completed"
    final_download = client.get("/api/raw", params={"rootId": "fs", "path": _p("unpacked/clone.bin")})
    assert final_download.status_code == 200 and final_download.content == payload

    after = client.get("/api/list", params={"rootId": "fs", "path": _p("")})
    assert after.status_code == 200
    assert after.json()["listingVersion"] != before.json()["listingVersion"]
    assert {item["name"] for item in after.json()["entries"]} >= {
        ".integration-note.md", " Original Ω.bin ", "clone.zip", "copies", "unpacked",
    }
    assert (documents / ".integration-note.md").read_bytes() == b"# Loopback integration\n"
    # HF-NAV-010: new objects get the terminal result under the service umask 0002.
    assert (documents / "copies" / "clone.bin").stat().st_mode & 0o777 == 0o664
    assert (documents / "copies").stat().st_mode & 0o777 == 0o775


def main() -> int:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="hf-files-loopback-") as temporary:
        root = Path(temporary)
        config, documents, origin, port = _write_instance(root)
        process, log = _start_runtime(config, port, root / "runtime.log")
        client = httpx.Client(base_url=origin, timeout=8, trust_env=False)
        try:
            health = client.get("/healthz")
            assert health.status_code == 200 and health.json() == {"service": "hopper-files", "status": "up"}
            _exercise(client, documents, origin)
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()
    print(f"File operations loopback integration passed in {time.monotonic() - started:.2f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
