#!/usr/bin/env python3
"""Exercise file operations and the trash over one synthetic loopback instance.

The file-operation recipe lives in verify_files_integration.py. This
script runs that flow and then deletes, lists, and restores through the trash
API. It does not install a service.
"""

from __future__ import annotations

import importlib.util
import subprocess
import time
from pathlib import Path

import httpx


def _previous():
    path = Path(__file__).with_name("verify_files_integration.py")
    spec = importlib.util.spec_from_file_location("verify_files_integration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("file-operation integration recipe is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trash(client: httpx.Client, documents: Path, origin: str) -> None:
    # The earlier recipe leaves a session cookie. Drop it before proving that
    # trash is denied without authentication, then sign in again.
    client.cookies.clear()
    denied = client.get("/api/trash")
    assert denied.status_code == 401, denied.text
    previous = _previous()
    previous._DOCUMENTS[0] = str(documents)[1:]
    csrf = previous._login(client, origin)
    missing = client.post(
        "/api/trash",
        json={"action": "delete", "source": {"rootId": "fs", "path": previous._p(".integration-note.md")}},
        headers={"origin": origin},
    )
    assert missing.status_code == 403
    state = client.get("/api/state")
    assert state.status_code == 200
    revision = state.json()["stateRevision"]
    document = {key: value for key, value in state.json().items() if key != "stateRevision"}
    document["items"] = [
        {
            "path": "/" + previous._p(".integration-note.md"),
            "labelIds": ["azul"],
            "favorite": True,
            "emoji": None,
            "inode": None,
            "device": None,
        }
    ]
    headers = {"origin": origin, "x-csrf-token": csrf}
    marked = client.put("/api/state", json={"baseRevision": revision, **document}, headers=headers)
    assert marked.status_code == 200, marked.text
    stale = client.put("/api/state", json={"baseRevision": revision, **document}, headers=headers)
    assert stale.status_code == 409
    original = (documents / ".integration-note.md").read_bytes()
    deleted = client.post(
        "/api/trash",
        json={"action": "delete", "source": {"rootId": "fs", "path": previous._p(".integration-note.md")}},
        headers=headers,
    )
    assert deleted.status_code == 201, deleted.text
    identifier = deleted.json()["id"]
    assert deleted.json()["uiMetadata"][0]["favorite"] is True
    assert not (documents / ".integration-note.md").exists()
    assert client.get("/api/state").json()["items"] == []
    listed = client.get("/api/trash")
    assert listed.status_code == 200
    assert listed.json()["entries"][0]["id"] == identifier
    assert listed.json()["entries"][0]["quarantined"] is False
    (documents / ".integration-note.md").write_bytes(b"occupant")
    conflict = client.post("/api/trash", json={"action": "restore", "id": identifier}, headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert (documents / ".integration-note.md").read_bytes() == b"occupant"
    purge = client.post("/api/trash", json={"action": "purge", "id": identifier}, headers=headers)
    assert purge.status_code == 422
    restored = client.post(
        "/api/trash",
        json={"action": "restore", "id": identifier, "alternativePath": previous._p("integration-restored.md")},
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert (documents / "integration-restored.md").read_bytes() == original
    items = client.get("/api/state").json()["items"]
    assert items[0]["path"] == "/" + previous._p("integration-restored.md")
    assert items[0]["labelIds"] == ["azul"]
    assert client.get("/api/trash").json()["entries"] == []


def main() -> int:
    previous = _previous()
    started = time.monotonic()
    import tempfile

    with tempfile.TemporaryDirectory(prefix="hf-trash-loopback-") as temporary:
        root = Path(temporary)
        config, documents, origin, port = previous._write_instance(root)
        process, log = previous._start_runtime(config, port, root / "runtime.log")
        client = httpx.Client(base_url=origin, timeout=30, trust_env=False)
        try:
            health = client.get("/healthz")
            assert health.status_code == 200 and health.json() == {"service": "hopper-files", "status": "up"}
            previous._exercise(client, documents, origin)
            _trash(client, documents, origin)
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()
    print(f"Trash loopback integration passed in {time.monotonic() - started:.2f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
