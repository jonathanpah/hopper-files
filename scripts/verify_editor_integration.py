#!/usr/bin/env python3
"""Exercise the editor and viewer routes through a synthetic loopback process."""

from __future__ import annotations

import base64
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

import httpx

from hopper_files.credentials import change_password
from hopper_files.state import initialize_state

PASSWORD = "synthetic-editor-browser-password"
BASE = "/hf08/"



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


def _write_fixture(directory: Path) -> tuple[Path, Path, str, int]:
    documents = directory / "documents"
    _DOCUMENTS[0] = str(documents)[1:]
    state = directory / "state"
    documents.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (documents / "note.md").write_text("# Synthetic note\n\nHello **world**.\n", encoding="utf-8")
    (documents / "plain.txt").write_text("Synthetic plain text.\n", encoding="utf-8")
    (documents / "crlf.txt").write_bytes(b"first line\r\nsecond line\r\n")
    (documents / "invalid.txt").write_bytes(b"\xffnot UTF-8")
    (documents / "preview.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/pOYAAAAASUVORK5CYII="
        )
    )
    (documents / "preview.pdf").write_bytes(_minimal_pdf())
    large_png = documents / "large.png"
    large_png.write_bytes(b"\x89PNG\r\n\x1a\n")
    with large_png.open("r+b") as stream:
        stream.truncate(20 * 1024 * 1024 + 1)
    large_pdf = documents / "large.pdf"
    large_pdf.write_bytes(b"%PDF-1.7\n")
    with large_pdf.open("r+b") as stream:
        stream.truncate(100 * 1024 * 1024 + 1)
    (documents / "active.svg").write_text('<svg onload="alert(1)"><script>alert(2)</script></svg>', encoding="utf-8")
    (documents / "active.html").write_text('<img src=x onerror="alert(3)"><script>alert(4)</script>', encoding="utf-8")
    (documents / "hostile.md").write_text(
        '# Synthetic hostile Markdown\n\n<script>window.__hfXss = true</script>\n\n'
        '<img src=x onerror="window.__hfXss = true">\n\n'
        '[unsafe](javascript:window.__hfXss = true)\n',
        encoding="utf-8",
    )
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config_path = directory / "instance.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "instanceId": "editor-synthetic",
                "serviceAccount": pwd.getpwuid(os.geteuid()).pw_name,
                "timeZone": "UTC",
                "sessionDurationSeconds": 3600,
                "stateDirectory": str(state),
                "cookieName": "hf_editor_synthetic",
                "access": {
                    "mode": "local", "bindHost": "127.0.0.1", "bindPort": port,
                    "baseUrl": origin + BASE, "trustedProxyPeer": None,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    initialize_state(state, "editor-synthetic")
    change_password(state, PASSWORD, instance_id="editor-synthetic")
    return config_path, documents, origin, port


def _write_browser_documents(documents: Path) -> None:
    """Add deterministic editor stress/copy fixtures to a disposable browser instance."""
    (documents / "mixed.txt").write_bytes(b"linha um\r\nlinha dois\nlinha tres\r\n")
    (documents / "crlf.md").write_bytes("# Título\r\n\r\nLinha de origem.\r\n".encode("utf-8"))
    (documents / "literal.txt").write_text(
        "    **Texto** sintético demonstra quebra automática e preserva os sinais\n"
        "    e esta segunda linha continua a mesma frase com ==destaque==\n"
        "    antes de terminar com - marcador literal.\n",
        encoding="utf-8",
    )
    (documents / "code.py").write_text(
        "def uma_funcao_com_nome_bastante_longo_para_o_teste(argumento_a, argumento_b):\n"
        "    return argumento_a + argumento_b  # comentario longo para chegar ao limite\n"
        "valor = uma_funcao_com_nome_bastante_longo_para_o_teste(1, 2) # outro comentario\n"
        "print(valor)\n",
        encoding="utf-8",
    )
    long1 = "Esta linha sintética é longa o bastante para atingir o limiar de junção agora"
    long2 = "e esta segunda linha também é longa o bastante para seguir o mesmo parágrafo"
    (documents / "join.txt").write_text(f"{long1}\n{long2}\nRua A\nSala 2\n", encoding="utf-8")
    (documents / "fence.md").write_text("texto antes\n```js\nconst a = 1;\n```\ntexto depois\n", encoding="utf-8")
    (documents / "bullets.md").write_text("primeira\n\nsegunda\n", encoding="utf-8")
    (documents / "bullets2.md").write_text("primeira\n\nsegunda\n", encoding="utf-8")
    (documents / "readonly.md").write_text("read only\n", encoding="utf-8")
    (documents / "readonly.md").chmod(0o444)
    (documents / "stale.md").write_text("base text\n", encoding="utf-8")
    (documents / "links.md").write_text(
        "[ftp](ftp://example.test/x) [tel](tel:123) [rel](api/trash) "
        "[proto](//example.test/p) [https](https://example.test) "
        "[http](http://example.test) [mail](mailto:hello@example.test)\n",
        encoding="utf-8",
    )
    (documents / "bold.md").write_text("**forte**\n", encoding="utf-8")
    (documents / "preview-stale.md").write_text("# Antes\n", encoding="utf-8")
    prefix = "# Grande\n\n"
    line = "Linha sintética de parágrafo com **negrito** e ==marca== para medir o editor.\n"
    total = 5 * 1024 * 1024
    repetitions, remainder = divmod(total - len(prefix.encode()), len(line.encode()))
    body = line * repetitions + "x" * remainder
    large = prefix + body
    assert len(large.encode("utf-8")) == total
    (documents / "big.md").write_text(large, encoding="utf-8", newline="")
    inline_code_lines = 70_849
    code_lines = [f"line {index} `code-{index}` tail".ljust(73, "x") for index in range(inline_code_lines)]
    code_lines[-1] += "x" * 55
    large_code = "\n".join(code_lines)
    assert len(large_code.encode("ascii")) == total
    (documents / "big-code.md").write_text(large_code, encoding="ascii", newline="")


def _minimal_pdf() -> bytes:
    content = b"BT /F1 16 Tf 20 50 Td (Editor) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"endstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode("ascii"))
        data.extend(body)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(data)


def _start_runtime(config_path: Path, port: int, log_path: Path) -> tuple[subprocess.Popen[bytes], BinaryIO]:
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "PYTHONPATH") if key in os.environ}
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
    nonce = client.get(BASE + "login", headers={"accept": "application/json"})
    assert nonce.status_code == 200
    response = client.post(
        BASE + "login", json={"nonce": nonce.json()["nonce"], "password": PASSWORD}, headers={"origin": origin}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def _exercise(client: httpx.Client, origin: str) -> None:
    assert client.get(BASE + "api/file", params={"rootId": "fs", "path": _p("note.md")}).status_code == 401
    csrf = _login(client, origin)
    headers = {"origin": origin, "x-csrf-token": csrf}
    page = client.get(BASE + "app")
    assert page.status_code == 200
    assert 'id="hf-app"' in page.text and 'type="module"' in page.text
    csp = page.headers["content-security-policy"]
    assert "nonce-" in csp and "worker-src 'self'" in csp and "img-src 'self' data: blob:" in csp
    shell = client.get(BASE + "app.js")
    assert shell.status_code == 200 and "HFEditorRuntime" in shell.text
    chunk_queue = ["app.js"]
    checked_chunks: set[str] = set()
    while chunk_queue:
        current = chunk_queue.pop()
        if current in checked_chunks:
            continue
        checked_chunks.add(current)
        source = shell.text
        if current != "app.js":
            response = client.get(BASE + current)
            assert response.status_code == 200, current
            assert "javascript" in response.headers.get("content-type", "")
            source = response.text
        for reference in re.findall(r"(?:from|import)\s*['\"](\.?/assets/[^'\"]+\.m?js)['\"]", source):
            chunk_queue.append(reference.removeprefix("./"))
    assert len(checked_chunks) >= 2, checked_chunks
    worker = client.get(BASE + "assets/pdf.worker.min.mjs")
    assert worker.status_code == 200 and len(worker.content) > 100_000
    assert client.get(BASE + "third-party-notices.txt").status_code == 200

    address = {"rootId": "fs", "path": _p("note.md")}
    first = client.get(BASE + "api/file", params=address)
    assert first.status_code == 200 and first.json()["content"] == "# Synthetic note\n\nHello **world**.\n"
    assert first.json()["version"].startswith("v2.")
    assert first.json()["editable"] is True
    missing = client.put(BASE + "api/file", params=address, json={"content": "missing"}, headers=headers)
    assert missing.status_code == 428
    saved = client.put(
        BASE + "api/file", params=address, headers=headers,
        json={"baseVersion": first.json()["version"], "content": "# Saved through HTTP\n"},
    )
    assert saved.status_code == 200 and saved.json()["content"] == "# Saved through HTTP\n"
    stale = client.put(
        BASE + "api/file", params=address, headers=headers,
        json={"baseVersion": first.json()["version"], "content": "stale overwrite"},
    )
    assert stale.status_code == 409 and stale.json()["current"]["content"] == "# Saved through HTTP\n"

    invalid = client.get(BASE + "api/file", params={"rootId": "fs", "path": _p("invalid.txt")})
    assert invalid.status_code == 422 and invalid.json() == {"error": "invalid_encoding"}
    for filename, media in (("preview.png", "image/png"), ("preview.pdf", "application/pdf")):
        preview = client.get(BASE + "api/preview", params={"rootId": "fs", "path": _p(filename)})
        assert preview.status_code == 200 and preview.headers["content-type"].startswith(media)
        assert preview.headers["content-disposition"].startswith("inline;")
    assert client.get(BASE + "api/preview", params={"rootId": "fs", "path": _p("large.png")}).status_code == 413
    assert client.get(BASE + "api/preview", params={"rootId": "fs", "path": _p("large.pdf")}).status_code == 413
    svg_preview = client.get(BASE + "api/preview", params={"rootId": "fs", "path": _p("active.svg")})
    assert svg_preview.status_code == 415
    svg_source = client.get(BASE + "api/file", params={"rootId": "fs", "path": _p("active.svg")})
    assert svg_source.status_code == 200 and "<script>" in svg_source.json()["content"]
    assert client.get(BASE + "assets/%2e%2e/app.js").status_code == 404
    print("Editor loopback integration passed: ESM assets, authenticated revisions, 428/409, inert SVG, raster/PDF routes, and invalid UTF-8.")


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--serve-for-browser", action="store_true", help="keep this synthetic instance available to agent-browser")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hf-editor-loopback-") as temporary:
        directory = Path(temporary)
        config, documents, origin, port = _write_fixture(directory)
        if args.serve_for_browser:
            _write_browser_documents(documents)
        process, log = _start_runtime(config, port, directory / "runtime.log")
        try:
            if args.serve_for_browser:
                print(f"Synthetic editor browser fixture: {origin}{BASE}login", flush=True)
                print(f"Synthetic editor login password: {PASSWORD}", flush=True)
                print("Stop this process after desktop and coarse-pointer checks to remove its fixture.", flush=True)
                try:
                    while process.poll() is None:
                        time.sleep(0.25)
                except KeyboardInterrupt:
                    pass
            else:
                with httpx.Client(base_url=origin, timeout=20, trust_env=False) as client:
                    health = client.get(BASE + "healthz")
                    assert health.status_code == 200
                    _exercise(client, origin)
        finally:
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
