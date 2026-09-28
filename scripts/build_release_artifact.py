#!/usr/bin/env python3
"""Build a source-only release archive from an identified Git tree object."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TREE_ID = re.compile(r"^[0-9a-f]{40,64}$")
SPECIFICATION_SHA256 = "047d85594de6a592bb176d29e6a1416f391d8b5c20adbf73887de3bf3bcb07df"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, help="full Git tree object ID, never a branch name")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if TREE_ID.fullmatch(args.tree) is None:
        parser.error("--tree must be a full lowercase Git tree object ID")
    if RELEASE_ID.fullmatch(args.release_id) is None or args.release_id in {".", ".."}:
        parser.error("--release-id is invalid")
    output = args.output.absolute()
    if output.is_relative_to(ROOT):
        parser.error("release artifacts must be written outside the public source checkout")
    sidecar = output.with_name(output.name + ".sha256")
    if output.exists() or output.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        parser.error("release artifact or digest sidecar already exists; choose an empty output path")
    try:
        kind = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-t", args.tree],
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.error(f"Git tree object is unavailable: {exc}")
    if kind != "tree":
        parser.error("--tree must identify a Git tree object")

    with tempfile.TemporaryDirectory(prefix="hopper-files-release-") as temporary:
        source_tar = Path(temporary) / "source.tar"
        with source_tar.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(ROOT), "archive", "--format=tar", "--prefix=source/", args.tree],
                check=True,
                stdout=stream,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        files, modes, directories = _read_git_archive(source_tar)
        _verify_export(files, modes, directories, Path(temporary) / "export")
        lock = files.get("source/requirements.lock")
        specification = files.get("source/docs/specification.md")
        if lock is None or b"--index-url https://pypi.org/simple" not in lock:
            parser.error("identified source tree lacks the official-index requirements lock")
        if specification is None or hashlib.sha256(specification).hexdigest() != SPECIFICATION_SHA256:
            parser.error("identified source tree does not contain the frozen public specification")
        manifest = {
            "schema": 1,
            "releaseId": args.release_id,
            "sourceTree": args.tree,
            "requirementsLockSha256": hashlib.sha256(lock).hexdigest(),
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())},
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_release(output, files, modes, directories, manifest)

    digest = _sha256(output)
    sidecar.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"release_id={args.release_id}")
    print(f"source_tree={args.tree}")
    print(f"requirements_lock_sha256={manifest['requirementsLockSha256']}")
    print(f"archive={output}")
    print(f"archive_sha256={digest}")
    print(f"source_files={len(files)} source_bytes={sum(map(len, files.values()))}")
    return 0


def _verify_export(
    files: dict[str, bytes],
    modes: dict[str, int],
    directories: set[str],
    export_root: Path,
) -> None:
    export_root.mkdir(mode=0o700)
    for name in sorted(directories, key=lambda item: (item.count("/"), item)):
        relative = Path(*Path(name).parts[1:])
        if relative.parts:
            (export_root / relative).mkdir(parents=True, exist_ok=True, mode=0o755)
    for name, content in files.items():
        relative = Path(*Path(name).parts[1:])
        target = export_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes(content)
        target.chmod(modes[name])
    verifier = export_root / "scripts/verify_repository.py"
    if not verifier.is_file():
        raise ValueError("identified source tree has no repository verifier")
    completed = subprocess.run(
        ["/usr/bin/python3", str(verifier)],
        check=False,
        capture_output=True,
        text=True,
        cwd=export_root,
        env={"HOME": "/tmp", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "repository verifier failed").strip()[-2000:]
        raise ValueError(f"identified source tree failed its public source/inventory checks: {detail}")


def _read_git_archive(path: Path) -> tuple[dict[str, bytes], dict[str, int], set[str]]:
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    directories: set[str] = {"source"}
    with tarfile.open(path, mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            if not name:
                continue
            if (name != "source" and not name.startswith("source/")) or "\\" in name:
                raise ValueError(f"git archive emitted an unsafe path: {member.name!r}")
            parts = Path(name).parts
            if Path(name).is_absolute() or any(part in {".", ".."} for part in parts):
                raise ValueError(f"git archive emitted an unsafe path: {member.name!r}")
            if member.isdir():
                directories.add(name)
                continue
            if not member.isfile():
                raise ValueError(f"source tree contains a link or non-regular object: {name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"git archive member cannot be read: {name}")
            with extracted:
                files[name] = extracted.read()
            modes[name] = member.mode & 0o777
            parent = Path(name).parent
            while parent.as_posix() not in {".", ""}:
                directories.add(parent.as_posix())
                parent = parent.parent
    return files, modes, directories


def _write_release(
    output: Path,
    files: dict[str, bytes],
    modes: dict[str, int],
    directories: set[str],
    manifest: dict[str, object],
) -> None:
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as raw:
            compressed = gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=9)
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name in sorted(directories, key=lambda item: (item.count("/"), item)):
                    entry = tarfile.TarInfo(name + "/")
                    entry.type = tarfile.DIRTYPE
                    entry.mode = 0o755
                    entry.mtime = 0
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    archive.addfile(entry)
                for name, content in sorted(files.items()):
                    entry = tarfile.TarInfo(name)
                    entry.mode = modes[name]
                    entry.size = len(content)
                    entry.mtime = 0
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    archive.addfile(entry, io.BytesIO(content))
                entry = tarfile.TarInfo("release.json")
                entry.mode = 0o644
                entry.size = len(manifest_bytes)
                entry.mtime = 0
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                archive.addfile(entry, io.BytesIO(manifest_bytes))
            compressed.close()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
