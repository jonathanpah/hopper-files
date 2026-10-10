#!/usr/bin/env python3
"""Serve a synthetic instance for the browser suite in frontend/browser.

Usage: browser_fixture.py DIRECTORY

DIRECTORY must be absent or empty, and writable. The script writes there the
instance configuration, its state, and a ``documents`` directory with the
fixture files; marks ``documents`` as the only monitored tag folder; saves one
folder tab on ``documents``, so the application opens there; and serves the
instance on a free loopback port. The first line on standard output is JSON with
``url``, ``username`` (the account the instance runs as), ``password``, and
``documents``. The process stops when standard input
reaches end of file or on SIGTERM.

Note content is read only below ``documents``, as in the integration scripts.
The product itself still uses ``/`` as its base and acts with the account's
permissions, so run it as an account whose other files a test may not touch, or
inside a sandbox that leaves only DIRECTORY writable.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import socket
import sys
import threading
from pathlib import Path

import hopper_files.corpus as corpus
from hopper_files.credentials import change_password
from hopper_files.state import default_ui_state, initialize_state, replace_ui_state, set_tag_folder

PASSWORD = "synthetic-browser-password"
INSTANCE_ID = "browser-synthetic"
FILES = {
    "notas/projeto.md": "# Projeto\n\n#alfa #beta\n\nTexto da nota com várias palavras para selecionar.\n",
    "notas/reuniao.md": "# Reunião\n\n#alfa #gama\n",
    "notas/ideias.md": "# Ideias\n\n#beta #delta-longo-para-testar\n",
    "app/nota.md": "# App\n\n#epsilon\n",
    "app/node_modules/pacote/leia.md": "# Leia\n\n#dependencia\n",
    "app/node_modules/pacote/node_modules/funda.md": "#funda\n",
    ".oculta/segredo.md": "#segredo\n",
    "arquivo/2024/janeiro/relatorio.md": "# Relatório\n",
    "texto.txt": "texto simples\n",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_fixture(directory: Path, port: int) -> tuple[Path, Path]:
    documents = directory / "documents"
    state = directory / "state"
    for relative, text in FILES.items():
        path = documents / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config_path = directory / "instance.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "instanceId": INSTANCE_ID,
                "serviceAccount": pwd.getpwuid(os.geteuid()).pw_name,
                "timeZone": "UTC",
                "sessionDurationSeconds": 86400,
                "stateDirectory": str(state),
                "cookieName": "hf_browser_synthetic",
                "access": {
                    "mode": "local",
                    "bindHost": "127.0.0.1",
                    "bindPort": port,
                    "baseUrl": f"http://127.0.0.1:{port}/",
                    "trustedProxyPeer": None,
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    initialize_state(state, INSTANCE_ID)
    change_password(state, PASSWORD, instance_id=INSTANCE_ID)
    set_tag_folder(state, str(documents), True)
    document = default_ui_state()
    document.pop("stateRevision")
    document["tabs"] = [{"path": str(documents), "mode": "navegar"}]
    replace_ui_state(state, base_revision=0, document=document)
    return config_path, documents


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(mode=0o700, exist_ok=True)
    if any(directory.iterdir()):
        print("the fixture directory must be empty", file=sys.stderr)
        return 2
    port = _free_port()
    config_path, documents = _write_fixture(directory, port)
    corpus.CORPUS_START = str(documents)[1:]

    import uvicorn

    from hopper_files.app import create_app
    from hopper_files.config import load_config
    from hopper_files.confinement import apply_service_umask

    apply_service_umask()
    server = uvicorn.Server(
        uvicorn.Config(create_app(load_config(config_path)), host="127.0.0.1", port=port, log_level="warning")
    )

    def stop(*_arguments: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=lambda: (sys.stdin.read(), stop()), daemon=True).start()
    ready = threading.Thread(
        target=lambda: (
            _wait_listening(port),
            print(json.dumps({"url": f"http://127.0.0.1:{port}/", "username": pwd.getpwuid(os.geteuid()).pw_name,
                              "password": PASSWORD, "documents": str(documents)}), flush=True),
        ),
        daemon=True,
    )
    ready.start()
    server.run()
    return 0


def _wait_listening(port: int) -> None:
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            threading.Event().wait(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
