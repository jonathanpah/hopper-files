from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("verify_repository", ROOT / "scripts" / "verify_repository.py")
verifier = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_repository"] = verifier
_SPEC.loader.exec_module(verifier)


@pytest.mark.parametrize(
    "text",
    [
        "Contact alice@example.com or the author at 1+bob@users.noreply.github.com.",
        "Co-Authored-By: An Assistant <noreply@anthropic.com>",
        "Listen on 127.0.0.1:8765, never 0.0.0.0; proxy peers 192.168.1.4, 10.0.0.2, 203.0.113.50, 198.51.100.7.",
        "The account home is /home/alice and /Users/alice on a Mac; TAILNET.ts.net is a placeholder.",
        "Versions 6.43.13 and 1.58.2 are not addresses.",
    ],
)
def test_example_values_pass(text: str) -> None:
    assert verifier.scan_content("sample", text.encode(), [], generic=True) == []


# The samples are split so that this file itself passes the repository check.
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Write to someone@" + "mail-provider.net", "e-mail address"),
        ("The Mac answers at 100.88" + ".1.2", "IP address"),
        ("Public host 8.8" + ".4.4", "IP address"),
        ("See /home" + "/bob/notes", "home or site path"),
        ("Copied from /srv" + "/system/index.md", "home or site path"),
        ("Open https://box.tail1234ab" + ".ts.net/", "tailnet name"),
        ("-----BEGIN OPENSSH PRIVATE" + " KEY-----", "private key"),
        ("token ghp_" + "a" * 36, "access token"),
    ],
)
def test_personal_data_fails(text: str, kind: str) -> None:
    findings = verifier.scan_content("sample", text.encode(), [], generic=True)
    assert findings and findings[0].startswith(kind)


def test_private_markers_are_found_in_any_case_and_encoding_but_never_printed() -> None:
    markers = ["Example-Site"]
    for data in (b"line one\nan example-site here\n", "EXAMPLE-SITE".encode("utf-16-le"), "Example-Site".encode("latin-1")):
        findings = verifier.scan_content("sample", data, markers, generic=False)
        assert len(findings) == 1 and findings[0].startswith("private marker #1 in sample")
        assert "example-site" not in findings[0].lower()
    assert verifier.scan_content("sample", b"nothing to see\n", markers, generic=False) == []


def test_generated_files_skip_the_generic_patterns_only() -> None:
    assert "frontend/package-lock.json".startswith(verifier.GENERATED_PREFIXES)
    assert "src/hopper_files/static/assets/app.js".startswith(verifier.GENERATED_PREFIXES)
    assert not "docs/development.md".startswith(verifier.GENERATED_PREFIXES)


def test_git_history_reports_a_marker_in_an_intermediate_commit(tmp_path: Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=Alice", "-c", "user.email=alice@example.com", *arguments],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "notes.md").write_text("clean\n", encoding="utf-8")
    git("add", "notes.md")
    git("commit", "-q", "-m", "first")
    (tmp_path / "notes.md").write_text("mentions Example-Site\n", encoding="utf-8")
    git("commit", "-q", "-am", "second")
    (tmp_path / "notes.md").write_text("clean again\n", encoding="utf-8")
    git("commit", "-q", "-am", "third")
    original = verifier.ROOT
    verifier.ROOT = tmp_path
    try:
        findings = verifier.verify_git_history(["Example-Site"], ["HEAD"], [])
    finally:
        verifier.ROOT = original
    assert len(findings) == 1 and findings[0].startswith("private marker #1 in commit ")
