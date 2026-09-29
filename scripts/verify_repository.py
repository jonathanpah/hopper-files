#!/usr/bin/env python3
"""Verify the public source boundary without accessing product data."""

from __future__ import annotations

import hashlib
import json
import sys
from argparse import ArgumentParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FILES = frozenset(
    {
        ".gitignore",
        ".python-version",
        "AGENTS.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug-report.yml",
        ".github/ISSUE_TEMPLATE/change-proposal.yml",
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "requirements.in",
        "requirements.lock",
        "docs/index.md",
        "docs/repository-inventory.md",
        "docs/development.md",
        "docs/operations.md",
        "docs/security.md",
        "docs/validation.md",
        "docs/specification.md",
        "scripts/verify-clean-checkout.sh",
        "scripts/build_release_artifact.py",
        "scripts/verify_files_integration.py",
        "scripts/verify_trash_integration.py",
        "scripts/verify_navigation_integration.py",
        "scripts/verify_editor_integration.py",
        "scripts/verify_images_integration.py",
        "scripts/build_frontend.mjs",
        "scripts/verify_built_wheel.py",
        "scripts/verify_repository.py",
        "docs/runtime.md",
        "frontend/README.md",
        "src/hopper_files/__init__.py",
        "src/hopper_files/access.py",
        "src/hopper_files/admin.py",
        "src/hopper_files/lifecycle.py",
        "src/hopper_files/app.py",
        "src/hopper_files/files.py",
        "src/hopper_files/clock.py",
        "src/hopper_files/confinement.py",
        "src/hopper_files/config.py",
        "src/hopper_files/credentials.py",
        "src/hopper_files/kdf.py",
        "src/hopper_files/limiter.py",
        "src/hopper_files/responses.py",
        "src/hopper_files/roots.py",
        "src/hopper_files/filetimes.py",
        "src/hopper_files/corpus.py",
        "src/hopper_files/note_index.py",
        "src/hopper_files/migration.py",
        "src/hopper_files/runtime.py",
        "src/hopper_files/search.py",
        "src/hopper_files/sessions.py",
        "src/hopper_files/state.py",
        "src/hopper_files/systemd_unit.py",
        "src/hopper_files/trash.py",
        "src/hopper_files/editor.py",
        "src/hopper_files/buffers.py",
        "src/hopper_files/changes.py",
        "src/hopper_files/image_refs.py",
        "src/hopper_files/images.py",
        "src/hopper_files/api/__init__.py",
        "src/hopper_files/api/auth.py",
        "src/hopper_files/api/request_body.py",
        "src/hopper_files/api/files.py",
        "src/hopper_files/api/health.py",
        "src/hopper_files/api/trash.py",
        "src/hopper_files/api/search.py",
        "src/hopper_files/api/ui_state.py",
        "src/hopper_files/api/editor.py",
        "src/hopper_files/api/buffers.py",
        "src/hopper_files/api/changes.py",
        "src/hopper_files/api/images.py",
        "src/hopper_files/static/app.css",
        "src/hopper_files/static/app.js",
        "src/hopper_files/static/frontend-bundle-manifest.json",
        "src/hopper_files/static/app.bundle.js",
        "src/hopper_files/static/THIRD_PARTY_NOTICES.txt",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/src/entry.js",
        "frontend/src/editor-runtime.js",
        "frontend/src/pdf-viewer.js",
        "frontend/test/clean-copy.test.mjs",
        "frontend/test/editor-commands.test.mjs",
        "frontend/test/markdown-render.test.mjs",
        "tests/conftest.py",
        "tests/test_access.py",
        "tests/test_app.py",
        "tests/test_auth.py",
        "tests/test_confinement.py",
        "tests/test_credentials.py",
        "tests/test_lifecycle.py",
        "tests/test_isolation.py",
        "tests/test_limiter.py",
        "tests/test_public_boundary.py",
        "tests/test_navigation.py",
        "tests/test_migration.py",
        "tests/test_trash.py",
        "tests/test_search.py",
        "tests/test_ui_state.py",
        "tests/test_files.py",
        "tests/test_editor.py",
        "tests/test_images.py",
        "tests/test_image_collection_failures.py",
        "tests/test_http_regressions.py",
        "tests/test_changes.py",
    }
)
SPECIFICATION_SHA256 = "2993db6f2eae538b24a98b99401e1635804a9013f047a34c16375f1644606603"
def source_files() -> set[str]:
    generated_parts = {".git", "__pycache__", ".pytest_cache", ".venv", "build", "dist", "node_modules"}
    result: set[str] = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if not path.is_file() or any(part in generated_parts for part in relative.parts):
            continue
        if path.name == ".coverage" or path.suffix in {".pyc", ".pyo"} or any(
            part.endswith(".egg-info") for part in relative.parts
        ):
            continue
        result.add(relative.as_posix())
    return result


def verify_inventory() -> list[str]:
    actual = source_files()
    try:
        generated = json.loads(
            (ROOT / "src/hopper_files/static/frontend-bundle-manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        generated = {}
    expected = set(EXPECTED_FILES) | {
        f"src/hopper_files/static/{relative}"
        for relative in generated
        if isinstance(relative, str)
    }
    messages: list[str] = []
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra:
        messages.append(f"unexpected source paths: {', '.join(extra)}")
    if missing:
        messages.append(f"missing source paths: {', '.join(missing)}")
    return messages


def verify_specification() -> list[str]:
    digest = hashlib.sha256(
        (ROOT / "docs/specification.md").read_bytes()
    ).hexdigest()
    if digest == SPECIFICATION_SHA256:
        return []
    return ["specification hash does not match the accepted contract"]


def verify_frontend_assets() -> list[str]:
    static_root = ROOT / "src/hopper_files/static"
    manifest_path = static_root / "frontend-bundle-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["frontend bundle manifest is missing or invalid"]
    if not isinstance(manifest, dict) or not manifest or any(
        not isinstance(path, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or path.startswith("/")
        or ".." in Path(path).parts
        or path not in {"app.bundle.js", "THIRD_PARTY_NOTICES.txt"} and not path.startswith("assets/")
        for path, digest in manifest.items()
    ):
        return ["frontend bundle manifest has invalid entries"]
    expected = set(manifest)
    actual = {
        path.relative_to(static_root).as_posix()
        for path in (static_root / "assets").rglob("*")
        if path.is_file()
    } | {"app.bundle.js", "THIRD_PARTY_NOTICES.txt"}
    messages = []
    if expected != actual:
        messages.append("frontend manifest paths do not match generated bundle files")
    for relative, digest in manifest.items():
        candidate = (static_root / relative).resolve()
        if not candidate.is_relative_to(static_root.resolve()) or not candidate.is_file():
            messages.append(f"frontend bundle file is missing or escapes static root: {relative}")
            continue
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
            messages.append(f"frontend bundle hash mismatch: {relative}")
    return messages


def verify_forbidden_markers(markers: list[str]) -> list[str]:
    messages: list[str] = []
    for relative_path in sorted(source_files() - {"scripts/verify_repository.py"}):
        try:
            contents = (ROOT / relative_path).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for marker in markers:
            if marker in contents:
                messages.append(
                    f"site-specific marker found in public source: {relative_path}"
                )
    return messages


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        metavar="TEXT",
        help="fail when TEXT appears in a public source file",
    )
    arguments = parser.parse_args()
    messages = (
        verify_inventory()
        + verify_frontend_assets()
        + verify_specification()
        + verify_forbidden_markers(arguments.forbid)
    )
    if messages:
        print("repository verification failed:", file=sys.stderr)
        print("\n".join(f"- {message}" for message in messages), file=sys.stderr)
        return 1
    print("repository verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
