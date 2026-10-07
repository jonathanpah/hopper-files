from __future__ import annotations

import os
from pathlib import Path

import pytest

# Site-specific markers (host names, account names, cookie names, private paths)
# must never enter the public tree. The list itself is private: a maintainer
# keeps it outside the repository, one marker per line, and points this
# variable at it. Without the file, the check is skipped. The same list also
# feeds `scripts/verify_repository.py --forbid` before a push.
MARKERS_VARIABLE = "HOPPER_FILES_FORBIDDEN_MARKERS"
CHECKED_SUFFIXES = {".py", ".md", ".toml", ".in", ".lock", ".css", ".js", ".html", ".sh", ".json", ".txt"}
CHECKED_NAMES = {".gitignore", ".python-version", "LICENSE"}
SKIPPED_PARTS = {".git", "node_modules", "__pycache__", ".pytest_cache"}


def _markers() -> list[str]:
    location = os.environ.get(MARKERS_VARIABLE)
    if not location:
        pytest.skip(f"{MARKERS_VARIABLE} is not set")
    lines = Path(location).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_public_tree_has_no_site_profile_constants() -> None:
    markers = _markers()
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if SKIPPED_PARTS & set(relative.parts):
            continue
        if path.suffix not in CHECKED_SUFFIXES and path.name not in CHECKED_NAMES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for index, marker in enumerate(markers, start=1):
            if marker in text:
                # Name the file and the line number of the marker in the private
                # list, never the marker itself, so a failure log stays public-safe.
                offenders.append(f"{relative}: private marker #{index}")
    assert offenders == []
