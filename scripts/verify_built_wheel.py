#!/usr/bin/env python3
"""Verify that the wheel contains only the intended public package files."""

from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile


EXPECTED_FILES = frozenset(
    {
        "hopper_files/__init__.py",
        "hopper_files/access.py",
        "hopper_files/admin.py",
        "hopper_files/lifecycle.py",
        "hopper_files/app.py",
        "hopper_files/files.py",
        "hopper_files/filetimes.py",
        "hopper_files/clock.py",
        "hopper_files/confinement.py",
        "hopper_files/config.py",
        "hopper_files/credentials.py",
        "hopper_files/kdf.py",
        "hopper_files/limiter.py",
        "hopper_files/responses.py",
        "hopper_files/roots.py",
        "hopper_files/corpus.py",
        "hopper_files/note_index.py",
        "hopper_files/migration.py",
        "hopper_files/runtime.py",
        "hopper_files/search.py",
        "hopper_files/sessions.py",
        "hopper_files/state.py",
        "hopper_files/systemd_unit.py",
        "hopper_files/trash.py",
        "hopper_files/editor.py",
        "hopper_files/buffers.py",
        "hopper_files/image_refs.py",
        "hopper_files/images.py",
        "hopper_files/api/__init__.py",
        "hopper_files/api/auth.py",
        "hopper_files/api/request_body.py",
        "hopper_files/api/files.py",
        "hopper_files/api/health.py",
        "hopper_files/api/trash.py",
        "hopper_files/api/search.py",
        "hopper_files/api/ui_state.py",
        "hopper_files/api/editor.py",
        "hopper_files/api/buffers.py",
        "hopper_files/api/images.py",
        "hopper_files/static/app.css",
        "hopper_files/static/app.js",
        "hopper_files/static/frontend-bundle-manifest.json",
        "hopper_files-0.1.0.dist-info/METADATA",
        "hopper_files-0.1.0.dist-info/WHEEL",
        "hopper_files-0.1.0.dist-info/RECORD",
        "hopper_files-0.1.0.dist-info/licenses/LICENSE",
    }
)
LICENSE_MEMBER = "hopper_files-0.1.0.dist-info/licenses/LICENSE"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_built_wheel.py WHEEL", file=sys.stderr)
        return 2

    wheel_path = Path(sys.argv[1])
    with ZipFile(wheel_path) as archive:
        actual = {
            entry.filename for entry in archive.infolist() if not entry.is_dir()
        }

        try:
            manifest = json.loads(archive.read("hopper_files/static/frontend-bundle-manifest.json"))
        except (KeyError, UnicodeError, json.JSONDecodeError):
            print("wheel frontend manifest is missing or invalid", file=sys.stderr)
            return 1
        if not isinstance(manifest, dict):
            print("wheel frontend manifest is not an object", file=sys.stderr)
            return 1
        if any(
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or relative not in {"app.bundle.js", "THIRD_PARTY_NOTICES.txt"}
            and not relative.startswith("assets/")
            or not isinstance(digest, str)
            or len(digest) != 64
            for relative, digest in manifest.items()
        ):
            print("wheel frontend manifest has invalid paths or hashes", file=sys.stderr)
            return 1
        generated_files = {
            f"hopper_files/static/{relative}" for relative in manifest
        }
        expected_files = set(EXPECTED_FILES) | generated_files
        extra = sorted(actual - expected_files)
        missing = sorted(expected_files - actual)
        bad_hashes = []
        for relative, digest in manifest.items():
            try:
                data = archive.read(f"hopper_files/static/{relative}")
            except KeyError:
                continue
            if not isinstance(digest, str) or hashlib.sha256(data).hexdigest() != digest:
                bad_hashes.append(relative)
        source_root = Path(__file__).resolve().parents[1] / "src/hopper_files"
        source_static = source_root / "static"
        source_manifest = source_static / "frontend-bundle-manifest.json"
        if not source_manifest.is_file() or archive.read("hopper_files/static/frontend-bundle-manifest.json") != source_manifest.read_bytes():
            bad_hashes.append("frontend-bundle-manifest.json (source mismatch)")
        for relative in manifest:
            source_file = source_static / relative
            wheel_file = f"hopper_files/static/{relative}"
            if source_file.is_file() and wheel_file in actual and archive.read(wheel_file) != source_file.read_bytes():
                bad_hashes.append(f"{relative} (source mismatch)")
        for wheel_file in expected_files:
            if not wheel_file.startswith("hopper_files/"):
                continue
            source_file = source_root / wheel_file.removeprefix("hopper_files/")
            if wheel_file in actual and source_file.is_file() and archive.read(wheel_file) != source_file.read_bytes():
                bad_hashes.append(f"{wheel_file} (source mismatch)")
        project_license = Path(__file__).resolve().parents[1] / "LICENSE"
        if LICENSE_MEMBER in actual and archive.read(LICENSE_MEMBER) != project_license.read_bytes():
            bad_hashes.append(f"{LICENSE_MEMBER} (source mismatch)")
    if extra or missing or bad_hashes:
        print("wheel inventory verification failed:", file=sys.stderr)
        if extra:
            print(f"- unexpected wheel paths: {', '.join(extra)}", file=sys.stderr)
        if missing:
            print(f"- missing wheel paths: {', '.join(missing)}", file=sys.stderr)
        if bad_hashes:
            print(f"- generated frontend hash mismatch: {', '.join(bad_hashes)}", file=sys.stderr)
        return 1

    print("wheel inventory verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
