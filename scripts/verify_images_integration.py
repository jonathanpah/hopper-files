#!/usr/bin/env python3
"""Exercise image uploads, collection, joint restore, and moves over loopback HTTP."""

from __future__ import annotations

import json
import errno
import os
import pwd
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from hopper_files.credentials import change_password
from hopper_files.state import initialize_state


PASSWORD = "synthetic-images-browser-password"
BASE = "/hf09/"



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


def _acl(entries: list[tuple[int, int, int]]) -> bytes:
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identifier)
        for tag, permissions, identifier in entries
    )


def _access_acl() -> bytes:
    return _acl([
        (1, 6, 0xFFFFFFFF),
        (2, 4, os.geteuid()),
        (4, 4, 0xFFFFFFFF),
        (16, 4, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    ])


def _default_acl() -> bytes:
    return _acl([
        (1, 7, 0xFFFFFFFF),
        (2, 7, os.geteuid()),
        (4, 5, 0xFFFFFFFF),
        (16, 7, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    ])


def _archive_root(directory: Path) -> tuple[Path, bool]:
    device = directory.stat().st_dev
    for parent in (Path("/var/tmp"), Path("/dev/shm")):
        try:
            if parent.is_dir() and os.access(parent, os.W_OK) and parent.stat().st_dev != device:
                return Path(tempfile.mkdtemp(prefix="hopper-files-images-archive-", dir=parent)), True
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="hopper-files-images-archive-", dir=directory)), False


def _write_fixture(directory: Path) -> tuple[Path, Path, Path, str, int, bool]:
    documents = directory / "documents"
    _DOCUMENTS[0] = str(documents)[1:]
    archive, cross_filesystem = _archive_root(directory)
    state = directory / "state"
    for path in (documents, state):
        path.mkdir(mode=0o700)
    os.chmod(archive, 0o700)
    (documents / "note.md").write_text("# Synthetic note\n", encoding="utf-8")
    (archive / "photos").mkdir()
    (archive / "pictures").mkdir()
    image_bytes = b"\x89PNG\r\n\x1a\nmove-fixture"
    (archive / "photos" / "photo.png").write_bytes(image_bytes)
    os.chmod(archive / "photos" / "photo.png", 0o640)
    initial_reference = os.path.relpath(archive / "photos" / "photo.png", documents)
    (documents / "reference.md").write_text(
        f"![photo]({initial_reference})\n", encoding="utf-8"
    )
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config_path = directory / "instance.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "instanceId": "images-synthetic",
                "serviceAccount": pwd.getpwuid(os.geteuid()).pw_name,
                "timeZone": "UTC",
                "sessionDurationSeconds": 3600,
                "stateDirectory": str(state),
                "cookieName": "hf_images_synthetic",
                "access": {
                    "mode": "local",
                    "bindHost": "127.0.0.1",
                    "bindPort": port,
                    "baseUrl": origin + BASE,
                    "trustedProxyPeer": None,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    initialize_state(state, "images-synthetic")
    change_password(state, PASSWORD, instance_id="images-synthetic")
    return config_path, documents, archive, origin, port, cross_filesystem


def _start_runtime(config_path: Path, port: int, log_path: Path) -> tuple[subprocess.Popen[bytes], object]:
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
            detail = log_path.read_text(errors="replace")
            log.close()
            raise RuntimeError(f"synthetic runtime exited with {process.returncode}: {detail}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process, log
        except OSError:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    log.flush()
    detail = log_path.read_text(errors="replace")
    log.close()
    raise RuntimeError(f"synthetic runtime did not listen: {detail}")


def _login(client: httpx.Client, origin: str) -> str:
    nonce = client.get(BASE + "login", headers={"accept": "application/json"})
    assert nonce.status_code == 200, nonce.text
    response = client.post(
        BASE + "login",
        json={"nonce": nonce.json()["nonce"], "password": PASSWORD},
        headers={"origin": origin},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def _delete(client: httpx.Client, headers: dict[str, str], path: str) -> str:
    response = client.post(
        BASE + "api/trash",
        headers=headers,
        json={"action": "delete", "source": {"rootId": "fs", "path": _p(path)}},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _exercise(
    client: httpx.Client,
    documents: Path,
    archive: Path,
    origin: str,
    *,
    cross_filesystem: bool,
    hold_browser: bool,
) -> None:
    assert client.get(BASE + "api/images", params={"rootId": "fs", "path": _p("note.md")}).status_code == 401
    csrf = _login(client, origin)
    headers = {"origin": origin, "x-csrf-token": csrf}
    page = client.get(BASE + "app")
    assert page.status_code == 200, page.text
    assert 'id="btn-lixeira"' in page.text
    assert "img-src 'self' data: blob:" in page.headers["content-security-policy"]
    assert client.get(BASE + "app.js").status_code == 200

    address = {"rootId": "fs", "path": _p("note.md")}
    loaded = client.get(BASE + "api/file", params=address)
    assert loaded.status_code == 200, loaded.text
    token = client.post(BASE + "api/files/token", headers=headers)
    assert token.status_code == 201, token.text
    image_bytes = b"\x89PNG\r\n\x1a\nmanaged-upload"
    upload = client.post(
        BASE + "api/images/upload",
        params=address,
        content=image_bytes,
        headers={**headers, "content-type": "application/octet-stream", "x-hopper-operation-token": token.json()["operationToken"]},
    )
    assert upload.status_code == 201, upload.text
    image = upload.json()["image"]
    assert Path("/" + image["path"]).read_bytes() == image_bytes
    gallery = client.get(BASE + "api/images", params={"rootId": "fs", "path": _p("note.md")})
    assert gallery.status_code == 200 and gallery.json()["entries"][0]["pending"] is True

    saved = client.put(
        BASE + "api/file",
        params=address,
        headers=headers,
        json={"baseVersion": loaded.json()["version"], "content": f"# Synthetic note\n\n![upload]({image['reference']})\n"},
    )
    assert saved.status_code == 200, saved.text
    assert client.get(BASE + "api/images", params={"rootId": "fs", "path": _p("note.md")}).json()["entries"][0]["pending"] is False
    removed = client.put(
        BASE + "api/file",
        params=address,
        headers=headers,
        json={"baseVersion": saved.json()["savedVersion"], "content": "# Reference removed\n"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["imageCollection"] == {"removed": 1, "inconclusive": False}
    assert not Path("/" + image["path"]).exists()

    restore_note = documents / "restore.md"
    restore_note.write_text(f"![restored]({image['reference']})\n", encoding="utf-8")
    note_id = _delete(client, headers, "restore.md")
    trash = client.get(BASE + "api/trash", headers=headers)
    image_row = next(item for item in trash.json()["entries"] if item["sourcePath"] == image["path"])
    related = client.get(BASE + "api/trash/related-images", params={"id": note_id})
    assert related.status_code == 200 and related.json()["complete"] is True, related.text
    related_ids = [candidate["id"] for group in related.json()["images"] for candidate in group["candidates"]]
    assert related_ids == [image_row["id"]]
    joint = client.post(
        BASE + "api/trash",
        headers=headers,
        json={"action": "restore", "id": note_id, "relatedImageIds": related_ids},
    )
    assert joint.status_code == 200, joint.text
    assert len(joint.json()["restored"]) == 2
    assert (documents / "restore.md").read_text(encoding="utf-8") == f"![restored]({image['reference']})\n"
    assert Path("/" + image["path"]).read_bytes() == image_bytes

    before_move = (archive / "photos" / "photo.png").stat()
    move_token = client.post(BASE + "api/files/token", headers=headers)
    assert move_token.status_code == 201, move_token.text
    move = client.post(
        BASE + "api/files",
        headers=headers,
        json={"operationToken": move_token.json()["operationToken"], "actions": [{
            "action": "move",
            "source": {"rootId": "fs", "path": str(archive / "photos/photo.png")[1:]},
            "destination": {"rootId": "fs", "path": str(archive / "pictures/photo.png")[1:]},
        }]},
    )
    assert move.status_code == 200, move.text
    assert move.json()["status"] == "completed", move.text
    assert not (archive / "photos" / "photo.png").exists()
    assert (archive / "pictures" / "photo.png").read_bytes().startswith(b"\x89PNG")
    assert (documents / "reference.md").read_text(encoding="utf-8") == (
        f"![photo]({os.path.relpath(archive / 'pictures' / 'photo.png', documents)})\n"
    )
    cross_token = client.post(BASE + "api/files/token", headers=headers)
    assert cross_token.status_code == 201, cross_token.text
    cross_move = client.post(
        BASE + "api/files",
        headers=headers,
        json={"operationToken": cross_token.json()["operationToken"], "actions": [{
            "action": "move",
            "source": {"rootId": "fs", "path": str(archive / "pictures/photo.png")[1:]},
            "destination": {"rootId": "fs", "path": _p("attachments/moved-photo.png")},
        }]},
    )
    assert cross_move.status_code == 200, cross_move.text
    assert cross_move.json()["status"] == "completed", cross_move.text
    moved_photo = documents / "attachments" / "moved-photo.png"
    assert not (archive / "pictures" / "photo.png").exists()
    assert moved_photo.read_bytes().startswith(b"\x89PNG")
    moved_info = moved_photo.stat()
    if cross_filesystem:
        assert moved_info.st_dev != before_move.st_dev
    else:
        assert moved_info.st_dev == before_move.st_dev
    assert moved_info.st_uid == before_move.st_uid and moved_info.st_gid == before_move.st_gid
    assert os.stat(moved_photo).st_mode & 0o7777 == before_move.st_mode & 0o7777
    assert (documents / "reference.md").read_text(encoding="utf-8") == (
        "![photo](attachments/moved-photo.png)\n"
    )

    bundle = archive / "bundle"
    (bundle / "nested").mkdir(parents=True)
    os.chmod(bundle, 0o750)
    default_acl = _default_acl()
    try:
        os.setxattr(bundle, "system.posix_acl_default", default_acl)
        default_acl_supported = True
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
        default_acl_supported = False
    bundle_info = bundle.stat()
    bundle_photo = bundle / "nested" / "photo.png"
    bundle_photo.write_bytes(b"\x89PNG\r\n\x1a\nbundle-move")
    os.chmod(bundle_photo, 0o640)
    access_acl = _access_acl()
    try:
        os.setxattr(bundle_photo, "system.posix_acl_access", access_acl)
        access_acl_supported = True
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
        access_acl_supported = False
    bundle_photo_info = bundle_photo.stat()
    (bundle / "nested" / "inside.md").write_text(
        "![inside](photo.png)\n", encoding="utf-8"
    )
    directory_reference = os.path.relpath(bundle_photo, documents)
    (documents / "reference-dir.md").write_text(
        f"![directory]({directory_reference})\n", encoding="utf-8"
    )
    directory_token = client.post(BASE + "api/files/token", headers=headers)
    assert directory_token.status_code == 201, directory_token.text
    directory_move = client.post(
        BASE + "api/files",
        headers=headers,
        json={"operationToken": directory_token.json()["operationToken"], "actions": [{
            "action": "move",
            "source": {"rootId": "fs", "path": str(archive / "bundle")[1:]},
            "destination": {"rootId": "fs", "path": _p("attachments/moved-bundle")},
        }]},
    )
    assert directory_move.status_code == 200, directory_move.text
    assert directory_move.json()["status"] == "completed", directory_move.text
    moved_bundle = documents / "attachments" / "moved-bundle"
    moved_bundle_info = moved_bundle.stat()
    moved_bundle_photo = moved_bundle / "nested" / "photo.png"
    moved_bundle_photo_info = moved_bundle_photo.stat()
    assert not bundle.exists()
    assert moved_bundle_info.st_dev != bundle_info.st_dev if cross_filesystem else moved_bundle_info.st_dev == bundle_info.st_dev
    assert moved_bundle_info.st_uid == bundle_info.st_uid and moved_bundle_info.st_gid == bundle_info.st_gid
    assert moved_bundle_info.st_mode & 0o7777 == bundle_info.st_mode & 0o7777
    assert moved_bundle_photo.read_bytes().startswith(b"\x89PNG")
    assert moved_bundle_photo_info.st_uid == bundle_photo_info.st_uid and moved_bundle_photo_info.st_gid == bundle_photo_info.st_gid
    assert moved_bundle_photo_info.st_mode & 0o7777 == bundle_photo_info.st_mode & 0o7777
    if access_acl_supported:
        assert os.getxattr(moved_bundle_photo, "system.posix_acl_access") == access_acl
    if default_acl_supported:
        assert os.getxattr(moved_bundle, "system.posix_acl_default") == default_acl
    if not access_acl_supported or not default_acl_supported:
        print("POSIX ACL move check unavailable on this host's scratch filesystem.", flush=True)
    assert (moved_bundle / "nested" / "inside.md").read_text(encoding="utf-8") == "![inside](photo.png)\n"
    assert (documents / "reference-dir.md").read_text(encoding="utf-8") == (
        "![directory](attachments/moved-bundle/nested/photo.png)\n"
    )

    # The note starts in the corpus (the harness confines it to documents) and
    # moves with its directory to the other filesystem.
    (documents / "shared-for-directory.png").write_bytes(b"\x89PNG\r\n\x1a\nexternal-to-moved-directory")
    staged_bundle = documents / "bundle-with-reference"
    staged_bundle.mkdir()
    (staged_bundle / "rewrite.md").write_text(
        "![shared](../shared-for-directory.png)\n", encoding="utf-8"
    )
    (archive / "target-parent").mkdir()
    staged_directory_token = client.post(BASE + "api/files/token", headers=headers)
    assert staged_directory_token.status_code == 201, staged_directory_token.text
    staged_directory_move = client.post(
        BASE + "api/files",
        headers=headers,
        json={"operationToken": staged_directory_token.json()["operationToken"], "actions": [{
            "action": "move",
            "source": {"rootId": "fs", "path": _p("bundle-with-reference")},
            "destination": {"rootId": "fs", "path": str(archive / "target-parent/rebased")[1:]},
        }]},
    )
    assert staged_directory_move.status_code == 200, staged_directory_move.text
    assert staged_directory_move.json()["status"] == "completed", staged_directory_move.text
    rebased_reference = os.path.relpath(documents / "shared-for-directory.png", archive / "target-parent" / "rebased")
    assert (archive / "target-parent" / "rebased" / "rewrite.md").read_text(encoding="utf-8") == (
        f"![shared]({rebased_reference})\n"
    )

    if hold_browser:
        browser_image = documents / "browser.png"
        browser_image.write_bytes(b"\x89PNG\r\n\x1a\nbrowser-fixture")
        (documents / "browser-note.md").write_text("![browser](browser.png)\n", encoding="utf-8")
        _delete(client, headers, "browser.png")
        _delete(client, headers, "browser-note.md")


def main() -> int:
    hold_browser = "--hold-browser" in sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="hopper-files-images-") as temporary:
        directory = Path(temporary)
        config_path, documents, archive, origin, port, cross_filesystem = _write_fixture(directory)
        process = None
        log = None
        try:
            process, log = _start_runtime(config_path, port, directory / "runtime.log")
            with httpx.Client(base_url=origin, timeout=20, trust_env=False) as client:
                _exercise(
                    client,
                    documents,
                    archive,
                    origin,
                    cross_filesystem=cross_filesystem,
                    hold_browser=hold_browser,
                )
            print("Image loopback integration passed.", flush=True)
            if not cross_filesystem:
                print("Cross-filesystem move leg not exercised: no accessible second filesystem.", flush=True)
            if hold_browser:
                print(json.dumps({"url": origin + BASE + "login", "password": PASSWORD}, separators=(",", ":")), flush=True)
                while True:
                    time.sleep(1)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if log is not None:
                log.close()
            if process is not None and process.returncode not in {0, -15, 130}:
                print((directory / "runtime.log").read_text(errors="replace"), file=sys.stderr)
                return 1
            shutil.rmtree(archive, ignore_errors=True)
    print("Image synthetic runtime stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
