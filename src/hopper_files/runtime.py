"""Loopback service entry. The listener address comes only from the instance."""

from __future__ import annotations

import os
import json
import sys
from pathlib import Path

from hopper_files.app import create_app
from hopper_files.confinement import apply_service_umask
from hopper_files.migration import require_current_format
from hopper_files.config import (
    ConfigError,
    load_service_config,
    require_loopback,
    require_service_account,
    validate_service_identity,
)
from hopper_files.kdf import KdfUnavailable, prove_kdf_runtime
from hopper_files.state import StateError, ensure_initialized


def server_options(config) -> dict[str, object]:
    """Uvicorn settings that do not trust proxy headers on their own."""
    return {
        "host": config.bind_host,
        "port": config.bind_port,
        "proxy_headers": False,
        "forwarded_allow_ips": "",
        "server_header": False,
        "access_log": False,
        "log_level": "info",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("O serviço não aceita argumentos.", file=sys.stderr)
        return 2
    try:
        _report_active_release()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"A release ativa não passou na verificação: {exc}", file=sys.stderr)
        return 1
    configured = os.environ.get("HOPPER_FILES_INSTANCE", "")
    if configured == "":
        print("HOPPER_FILES_INSTANCE está ausente.", file=sys.stderr)
        return 1
    try:
        config = load_service_config(Path(configured))
        require_loopback(config.bind_host)
        require_service_account(config)
        ensure_initialized(config.state_directory, config.instance_id)
        require_current_format(config.state_directory)
        validate_service_identity(config)
        prove_kdf_runtime()
        apply_service_umask()
    except (ConfigError, StateError, KdfUnavailable) as exc:
        print(f"A instância não pode iniciar: {exc}", file=sys.stderr)
        return 1
    import uvicorn

    uvicorn.run(create_app(config), **server_options(config))
    return 0


def _report_active_release() -> None:
    """Bind startup evidence to the immutable release selected by the launcher."""
    active_text = os.environ.get("HOPPER_FILES_ACTIVE_ROOT")
    selector_text = os.environ.get("HOPPER_FILES_ACTIVE_SELECTOR")
    if active_text is None and selector_text is None:
        return
    if not active_text or not selector_text:
        raise ValueError("active release environment is incomplete")
    active = Path(active_text)
    selected = Path(selector_text).resolve(strict=True)
    active_real = active.resolve(strict=True)
    if active_real != active or selected != active_real:
        raise ValueError("process release does not match the atomic active selector")
    module_file = Path(__file__).resolve(strict=True)
    source_root = (active_real / "source/src/hopper_files").resolve(strict=True)
    if not module_file.is_relative_to(source_root):
        raise ValueError("runtime module was not imported from the selected release")
    metadata = json.loads((active_real / "release.json").read_text(encoding="utf-8"))
    release_id = metadata.get("releaseId") if isinstance(metadata, dict) else None
    source_tree = metadata.get("sourceTree") if isinstance(metadata, dict) else None
    if not isinstance(release_id, str) or not isinstance(source_tree, str):
        raise ValueError("selected release metadata is incomplete")
    print(
        f"Hopper Files active release={release_id} source_tree={source_tree} module={module_file}",
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
