#!/usr/bin/env python3
"""Exercise authenticated navigation, search, tags, and UI-state CAS over loopback."""

from __future__ import annotations

import json
import os
import pwd
import re
import socket
import subprocess
import sys
import tempfile
import time
from argparse import ArgumentParser
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

import httpx

from hopper_files.credentials import change_password
from hopper_files.state import initialize_state

PASSWORD = "synthetic-navigation-browser-password"
_MODULE_IMPORT = re.compile(
    r"(?:\bfrom\s*|\bimport\s*\(\s*)[\"']([^\"']+\.m?js)(?:\?[^\"']*)?[\"']"
)
_JS_HEX_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")



# The synthetic documents directory of this run, relative to / (HF-NAV-005).
_DOCUMENTS: list[str] = [""]


def _p(relative: str = "") -> str:
    """Address path relative to / of an entry in the synthetic documents directory."""
    return _DOCUMENTS[0] if relative == "" else f"{_DOCUMENTS[0]}/{relative}"


# Harness isolation: the runtime process reads note content (image references)
# only below the synthetic documents directory; the product keeps /. The tag
# index reads only monitored folders, and this run marks the documents directory.
_ISOLATED_RUNTIME = (
    "import os; import hopper_files.corpus as corpus; "
    "corpus.CORPUS_START = os.environ.pop('HF_HARNESS_CORPUS_START'); "
    "from hopper_files.runtime import main; raise SystemExit(main())"
)

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_fixture(directory: Path) -> tuple[Path, Path, str, int]:
    documents = directory / "documents"
    _DOCUMENTS[0] = str(documents)[1:]
    state = directory / "state"
    (documents / "Projetos").mkdir(parents=True, mode=0o700)
    state.mkdir(mode=0o700)
    (documents / "Projetos" / "Plano Árvore.md").write_text(
        "# Projeto #AÇÃO\nprimeira árvore da prova\n#favorito\n", encoding="utf-8"
    )
    (documents / "Lista.txt").write_text("árvore na lista\nsegunda linha\n", encoding="utf-8")
    (documents / "Projeto.md").write_text("#Projeto #busca\n", encoding="utf-8")
    (documents / "invalid.txt").write_bytes(b"\xffconteudo UTF-8 invalido")
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config_path = directory / "instance.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "instanceId": "navigation-synthetic",
                "serviceAccount": pwd.getpwuid(os.geteuid()).pw_name,
                "timeZone": "UTC",
                "sessionDurationSeconds": 3600,
                "stateDirectory": str(state),
                "cookieName": "hf_navigation_synthetic",
                "access": {
                    "mode": "local",
                    "bindHost": "127.0.0.1",
                    "bindPort": port,
                    "baseUrl": origin + "/",
                    "trustedProxyPeer": None,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    initialize_state(state, "navigation-synthetic")
    change_password(state, PASSWORD, instance_id="navigation-synthetic")
    return config_path, documents, origin, port


def _start_runtime(config_path: Path, port: int, log_path: Path) -> tuple[subprocess.Popen[bytes], BinaryIO]:
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


def _app_module_graph(client: httpx.Client, script: str, source: str) -> list[str]:
    """Fetch same-origin JavaScript chunks reachable from the app entry point."""
    origin = "http://hopper-files.invalid"
    pending = [(script, source)]
    visited = {script}
    modules: list[str] = []
    while pending:
        path, body = pending.pop()
        modules.append(body)
        for specifier in _MODULE_IMPORT.findall(body):
            resolved = urlsplit(urljoin(origin + path, specifier))
            if resolved.netloc != "hopper-files.invalid" or not resolved.path.startswith("/assets/"):
                continue
            module_path = resolved.path
            if module_path in visited:
                continue
            assert len(visited) < 64, "app JavaScript module graph exceeded 64 assets"
            response = client.get(module_path)
            assert response.status_code == 200, f"JavaScript module unavailable: {module_path}"
            visited.add(module_path)
            pending.append((module_path, response.text))
    return modules


def _decode_js_hex_escapes(source: str) -> str:
    """Decode hex escapes emitted by the minifier before checking visible copy."""
    return _JS_HEX_ESCAPE.sub(
        lambda match: chr(int(match.group(1) or match.group(2), 16)), source
    )


def _exercise(client: httpx.Client, origin: str) -> None:
    denied = client.get("/api/search", params={"q": "árvore", "scope": "/" + _p()})
    assert denied.status_code == 401
    csrf = _login(client, origin)
    headers = {"origin": origin, "x-csrf-token": csrf}

    page = client.get("/app")
    assert page.status_code == 200
    assert 'id="hf-app"' in page.text and 'id="tabs"' in page.text
    script = client.get("/app.js")
    assert script.status_code == 200, script.text
    modules = _app_module_graph(client, "/app.js", script.text)
    assert any(
        "Juntar as mudanças" in _decode_js_hex_escapes(module)
        for module in modules
    ), "explicit UI-state merge action is missing from the served app module graph"
    # HF-NAV-001: there is no root discovery route.
    assert client.get("/api/roots").status_code == 404

    listing = client.get("/api/list", params={"rootId": "fs", "path": _p("Projetos")})
    assert listing.status_code == 200
    assert [entry["name"] for entry in listing.json()["entries"]] == ["Plano Árvore.md"]

    search = client.get(
        "/api/search",
        params=[("mode", "text"), ("q", "ÁRVORE"), ("scope", "/" + _p()), ("limit", "1")],
    )
    assert search.status_code == 200, search.text
    assert search.json()["items"][0]["path"] == _p("Lista.txt")
    assert search.json()["items"][0]["line"] == 1
    assert search.json()["nextCursor"]
    exclusions = {item["category"]: item["count"] for item in search.json()["exclusions"]}
    assert exclusions["invalid_utf8"] == 1
    next_page = client.get(
        "/api/search",
        params=[
            ("mode", "text"), ("q", "ÁRVORE"), ("scope", "/" + _p()),
            ("limit", "1"), ("cursor", search.json()["nextCursor"]),
        ],
    )
    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["path"] == _p("Projetos/Plano Árvore.md")

    unmonitored = client.get("/api/tags")
    assert unmonitored.status_code == 200 and unmonitored.json()["tags"] == []
    marked = client.post("/api/tag-folders", json={"path": "/" + _p(), "monitored": True}, headers=headers)
    assert marked.status_code == 200, marked.text
    assert marked.json() == {"folders": [{"path": "/" + _p(), "available": True}]}
    tags = client.get("/api/tags")
    assert tags.status_code == 200
    tag_names = {item["tag"] for item in tags.json()["tags"]}
    assert {"ação", "projeto", "busca", "favorito"} <= tag_names
    selected = client.get("/api/tags", params={"tag": "AÇÃO"})
    assert selected.status_code == 200
    assert [item["path"] for item in selected.json()["items"]] == [_p("Projetos/Plano Árvore.md")]

    current = client.get("/api/state")
    assert current.status_code == 200
    proposal = {key: value for key, value in current.json().items() if key != "stateRevision"}
    proposal["tabs"] = [{"path": "/" + _p("Projetos"), "mode": "navegar"}]
    proposal["items"] = [{"path": "/" + _p("Lista.txt"), "labelIds": ["azul"], "favorite": True, "emoji": None, "inode": None, "device": None}]
    proposal["labels"]["azul"]["name"] = "Pesquisa"
    saved = client.put("/api/state", json={"baseRevision": 0, **proposal}, headers=headers)
    stale = client.put("/api/state", json={"baseRevision": 0, **proposal}, headers=headers)
    missing_revision = client.put("/api/state", json=proposal, headers=headers)
    assert saved.status_code == 200 and saved.json()["stateRevision"] == 1
    assert stale.status_code == 409 and stale.json()["stateRevision"] == 1
    assert missing_revision.status_code == 428
    print("Navigation loopback integration passed: authenticated listing, normalized search, derived tags of a monitored folder, and UI-state CAS.")


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument(
        "--serve-for-browser",
        action="store_true",
        help="keep a synthetic loopback instance running for a manual browser session",
    )
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hf-navigation-loopback-") as temporary:
        directory = Path(temporary)
        config, _documents, origin, port = _write_fixture(directory)
        process, log = _start_runtime(config, port, directory / "runtime.log")
        try:
            if arguments.serve_for_browser:
                print(f"Synthetic browser fixture: {origin}/login", flush=True)
                print(f"Synthetic login password: {PASSWORD}", flush=True)
                print("Stop this process after the browser session to remove the fixture.", flush=True)
                try:
                    while process.poll() is None:
                        time.sleep(0.25)
                except KeyboardInterrupt:
                    pass
            else:
                with httpx.Client(base_url=origin, timeout=12, trust_env=False) as client:
                    health = client.get("/healthz")
                    assert health.status_code == 200 and health.json() == {"service": "hopper-files", "status": "up"}
                    _exercise(client, origin)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.flush()
            log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
