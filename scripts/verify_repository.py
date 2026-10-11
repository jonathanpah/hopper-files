#!/usr/bin/env python3
"""Verify the public source boundary without accessing product data.

Without options it checks the source inventory, the generated frontend files, the
specification hash, and the public tree for personal data: e-mail addresses, IP
addresses outside the private and documentation ranges, home and site paths,
tailnet names, private keys, and access tokens. ``--markers-file`` adds a
private list of site markers, one per line (``#`` starts a comment), matched
without regard to case, also in UTF-16 and Latin-1 binary content.
``--git-commits`` and ``--git-tag`` run the same content checks on Git history
instead of the working tree: every file of every listed commit, the file names,
and the author, committer, and message of each commit or annotated tag. A
failure names the place and the kind of finding, never a private marker.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
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
        "scripts/browser_fixture.py",
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
        "frontend/browser/harness.mjs",
        "frontend/browser/copy-paths.test.mjs",
        "frontend/browser/drag.test.mjs",
        "frontend/browser/editor-bar.test.mjs",
        "frontend/browser/favorites.test.mjs",
        "frontend/browser/layout.test.mjs",
        "frontend/browser/live-refresh.test.mjs",
        "frontend/browser/login.test.mjs",
        "frontend/browser/menu.test.mjs",
        "frontend/browser/tags.test.mjs",
        "frontend/browser/viewers.test.mjs",
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
        "tests/test_repository_verifier.py",
    }
)
SPECIFICATION_SHA256 = "f4cbfb213ab68d3d39d84e18f4bdf82718c75fb372e6c6ac6f92e8a60502515b"
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


# Generated or third-party files: checked for private markers, not for the generic patterns, which
# minified code and integrity hashes would match by chance.
GENERATED_PREFIXES = (
    "src/hopper_files/static/assets/",
    "src/hopper_files/static/app.bundle.js",
    "src/hopper_files/static/THIRD_PARTY_NOTICES.txt",
    "frontend/package-lock.json",
    "requirements.lock",
)
PATTERNS = {
    "e-mail address": re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "IP address": re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "home or site path": re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+|/root/|/srv/[A-Za-z0-9._-]+"),
    "tailnet name": re.compile(rb"\btail[0-9a-f]{4,}\.ts\.net\b"),
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "access token": re.compile(
        rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
        rb"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,})"
    ),
}
# Example values the documentation and the tests use on purpose.
EXAMPLE_ADDRESS_DOMAINS = ("example.com", "example.org", "example.net", "example.test", "users.noreply.github.com")
# The co-author trailer of commits written with an assistant.
ALLOWED_ADDRESSES = ("noreply@anthropic.com",)
EXAMPLE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "0.0.0.0/32",
                    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
EXAMPLE_PATHS = ("/home/alice", "/Users/alice")
EXCLUDED_FROM_SELF_CHECK = "scripts/verify_repository.py"


def _allowed(kind: str, found: bytes) -> bool:
    text = found.decode("ascii", "replace")
    if kind == "e-mail address":
        domain = text.rsplit("@", 1)[1].lower()
        return text.lower() in ALLOWED_ADDRESSES or any(domain == allowed or domain.endswith("." + allowed) for allowed in EXAMPLE_ADDRESS_DOMAINS)
    if kind == "IP address":
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return True
        return any(address in network for network in EXAMPLE_NETWORKS)
    if kind == "home or site path":
        return text in EXAMPLE_PATHS
    return False


def load_markers(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _line(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def scan_content(place: str, data: bytes, markers: list[str], *, generic: bool) -> list[str]:
    """Return the findings in one file or text; markers are reported by number only."""
    messages: list[str] = []
    folded = data.lower()
    for index, marker in enumerate(markers, start=1):
        needle = marker.lower()
        hit = folded.find(needle.encode("utf-8"))
        if hit < 0:
            for encoding in ("utf-16-le", "latin-1"):
                try:
                    if folded.find(needle.encode(encoding)) >= 0 or data.find(marker.encode(encoding)) >= 0:
                        hit = 0
                        break
                except UnicodeEncodeError:
                    continue
        if hit >= 0:
            messages.append(f"private marker #{index} in {place}" + (f":{_line(data, hit)}" if hit else ""))
    if generic:
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(data):
                if not _allowed(kind, match.group(0)):
                    messages.append(f"{kind} in {place}:{_line(data, match.start())}")
                    break
    return messages


def verify_forbidden_markers(markers: list[str]) -> list[str]:
    messages: list[str] = []
    for relative_path in sorted(source_files() - {EXCLUDED_FROM_SELF_CHECK}):
        data = (ROOT / relative_path).read_bytes()
        messages += scan_content(relative_path, relative_path.encode("utf-8") + b"\n", markers, generic=True)
        messages += scan_content(relative_path, data, markers, generic=not relative_path.startswith(GENERATED_PREFIXES))
    return messages


def _git(*arguments: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *arguments], input=data, check=True, capture_output=True).stdout


def verify_git_history(markers: list[str], commits: list[str], tags: list[str]) -> list[str]:
    """Check every file, file name, and message of the given commits and annotated tags."""
    messages: list[str] = []
    revisions = _git("rev-list", *commits).decode().split() if commits else []
    seen: set[str] = set()
    for commit in revisions:
        short = commit[:12]
        metadata = _git("log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B", commit)
        messages += scan_content(f"commit {short} message", metadata, markers, generic=True)
        for record in _git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not record:
                continue
            header, path = record.split(b"\t", 1)
            _mode, kind, blob = header.decode().split()
            name = path.decode("utf-8", "replace")
            messages += scan_content(f"commit {short} name {name}", path + b"\n", markers, generic=True)
            if kind != "blob" or blob in seen:
                continue
            seen.add(blob)
            if name == EXCLUDED_FROM_SELF_CHECK:
                continue
            content = _git("cat-file", "blob", blob)
            messages += scan_content(f"commit {short} {name}", content, markers, generic=not name.startswith(GENERATED_PREFIXES))
    for tag in tags:
        if _git("cat-file", "-t", tag).strip() == b"tag":
            messages += scan_content(f"tag {tag}", _git("cat-file", "tag", tag), markers, generic=True)
    return sorted(set(messages), key=messages.index)


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        metavar="TEXT",
        help="fail when TEXT appears in a public source file",
    )
    parser.add_argument("--markers-file", type=Path, help="private list of site markers, one per line")
    parser.add_argument(
        "--git-commits",
        nargs="+",
        metavar="REVISION",
        help="check these commits instead of the working tree (git rev-list arguments, e.g. A..B)",
    )
    parser.add_argument("--git-tag", action="append", default=[], metavar="TAG", help="also check this annotated tag")
    arguments = parser.parse_args()
    markers = arguments.forbid + (load_markers(arguments.markers_file) if arguments.markers_file else [])
    if arguments.git_commits or arguments.git_tag:
        messages = verify_git_history(markers, arguments.git_commits or [], arguments.git_tag)
    else:
        messages = (
            verify_inventory()
            + verify_frontend_assets()
            + verify_specification()
            + verify_forbidden_markers(markers)
        )
    if messages:
        print("repository verification failed:", file=sys.stderr)
        print("\n".join(f"- {message}" for message in messages), file=sys.stderr)
        return 1
    print("repository verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
