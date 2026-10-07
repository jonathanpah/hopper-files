"""Strong file revisions and cooperative atomic replacement for the editor."""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from hopper_files.roots import (
    AddressRejected,
    RootCatalog,
    _open_directory_for_create,
    address_has_missing_component,
    open_existing_directory,
    open_regular,
    parse_relative_path,
)
from hopper_files.state import StateError, read_signing_secret

MAX_TEXT_BYTES = 5 * 1024 * 1024
_ACCESS_ACL = "system.posix_acl_access"


class EditorError(ValueError):
    def __init__(self, code: str, *, current: dict[str, object] | None = None, reason: str | None = None) -> None:
        self.code = code
        self.current = current
        self.reason = reason
        super().__init__(code)


def editability(parent_fd: int, name: str, fd: int, info: os.stat_result) -> str | None:
    """Return None when the file is editable now, else a machine-readable reason.

    This is the single rule of HF-NAV-007. ``GET api/file`` reports it and the
    save path enforces it before any staging.
    """
    if not stat.S_ISREG(info.st_mode):
        return "not_regular"
    if info.st_nlink != 1:
        return "hard_link"
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        return "set_id"
    try:
        attrs = os.listxattr(fd)
    except OSError:
        return "extended_attributes"
    if any(attribute != _ACCESS_ACL for attribute in attrs):
        return "extended_attributes"
    if info.st_uid != os.geteuid():
        return "not_owner"
    if info.st_gid != os.getegid() and info.st_gid not in os.getgroups():
        return "group_not_member"
    try:
        if not os.access(name, os.W_OK, dir_fd=parent_fd, follow_symlinks=False):
            return "file_not_writable"
        if not os.access(".", os.W_OK | os.X_OK, dir_fd=parent_fd):
            return "directory_not_writable"
    except OSError:
        return "file_not_writable"
    return None


@dataclass
class Target:
    root_id: str
    path: str
    parent_fd: int
    name: str
    fd: int
    info: os.stat_result
    data: bytes
    digest: str
    acl: bytes | None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


def prepare_document_rewrite(
    catalog: RootCatalog,
    root_id: str,
    path: str,
    destination_root_id: str,
    destination_path: str,
    base_version: object,
    content: object,
    instance_id: str,
    state_directory: Path,
    temporary_name: str,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
) -> dict[str, object]:
    """Create a metadata-preserving, destination-filesystem rewrite stage.

    The source stays locked only while its authenticated revision and metadata
    are checked. The caller journals ``temporary_name`` before invoking this
    function, so recovery can identify a completed stage by its kernel handle.
    """
    if not isinstance(content, str):
        raise EditorError("invalid_request")
    try:
        encoded = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise EditorError("invalid_request") from exc
    if len(encoded) > max_bytes:
        raise EditorError("limit")
    canonical = "/".join(parse_relative_path(path))
    destination_parts = parse_relative_path(destination_path)
    if not destination_parts:
        raise AddressRejected("invalid")
    if not (temporary_name.startswith(".hopper-stage-") and temporary_name.endswith(".tmp")):
        raise AddressRejected("invalid")
    destination = "/".join(destination_parts)
    after_digest = hashlib.sha256(encoded).hexdigest()
    with _file_lock(state_directory, instance_id, root_id, canonical):
        try:
            source_context = _open_target(catalog, root_id, canonical, writable=True, max_bytes=max_bytes)
            source = source_context.__enter__()
        except EditorError:
            raise
        except AddressRejected as exc:
            raise _address_error(exc) from exc
        except OSError as exc:
            raise _filesystem_error(exc) from exc
        parent = -1
        descriptor = -1
        stage_identity: tuple[int, int] | None = None
        try:
            current_version = _version(
                state_directory, instance_id, root_id, canonical, source.digest, source.info
            )
            if not isinstance(base_version, str) or not hmac.compare_digest(base_version, current_version):
                raise EditorError("conflict", current=_revision_payload(source.data, current_version))
            _check_supported_metadata(source)
            parent_path, parent = _open_stage_parent(
                catalog, destination_root_id, destination_parts[:-1]
            )
            try:
                # A moved note's destination is required to remain absent until
                # its source object has been published by the trash transaction.
                if (destination_root_id, destination) != (root_id, canonical):
                    try:
                        with open_regular(catalog, destination_root_id, destination):
                            pass
                    except FileNotFoundError:
                        pass
                    except AddressRejected as exc:
                        if not address_has_missing_component(catalog, destination_root_id, destination):
                            raise
                    else:
                        raise AddressRejected("conflict")
                descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent,
                )
                details = os.fstat(descriptor)
                stage_identity = (details.st_dev, details.st_ino)
                os.fchown(descriptor, source.info.st_uid, source.info.st_gid)
                os.fchmod(descriptor, stat.S_IMODE(source.info.st_mode))
                if source.acl is not None:
                    os.setxattr(descriptor, _ACCESS_ACL, source.acl)
                elif _ACCESS_ACL in os.listxattr(descriptor):
                    os.removexattr(descriptor, _ACCESS_ACL)
                    os.fchmod(descriptor, stat.S_IMODE(source.info.st_mode))
                attrs = os.listxattr(descriptor)
                copied_acl = os.getxattr(descriptor, _ACCESS_ACL) if _ACCESS_ACL in attrs else None
                staged_info = os.fstat(descriptor)
                if (
                    any(name != _ACCESS_ACL for name in attrs)
                    or staged_info.st_uid != source.info.st_uid
                    or staged_info.st_gid != source.info.st_gid
                    or stat.S_IMODE(staged_info.st_mode) != stat.S_IMODE(source.info.st_mode)
                    or copied_acl != source.acl
                ):
                    raise EditorError("metadata_unsupported")
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
                source_handle = _kernel_file_handle(source.parent_fd, source.name)
                stage_handle = _kernel_file_handle(parent, temporary_name)
                os.fsync(parent)
                return {
                    "rootId": destination_root_id,
                    "path": destination,
                    "stageParent": parent_path,
                    "sourceRootId": root_id,
                    "sourcePath": canonical,
                    "temporaryName": temporary_name,
                    "targetHandle": source_handle,
                    "stageHandle": stage_handle,
                    "targetVersion": current_version,
                    "beforeDigest": source.digest,
                    "afterDigest": after_digest,
                    "size": len(encoded),
                    "uid": source.info.st_uid,
                    "gid": source.info.st_gid,
                    "mode": stat.S_IMODE(source.info.st_mode),
                    "acl": base64.b64encode(source.acl).decode("ascii") if source.acl is not None else None,
                }
            except BaseException:
                if descriptor >= 0 and stage_identity is not None:
                    try:
                        current = os.stat(temporary_name, dir_fd=parent, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == stage_identity:
                            os.unlink(temporary_name, dir_fd=parent)
                    except OSError:
                        pass
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                    descriptor = -1
                if parent >= 0:
                    os.close(parent)
                    parent = -1
        except OSError as exc:
            raise _filesystem_error(exc) from exc
        finally:
            source_context.__exit__(None, None, None)


def publish_document_rewrite(
    catalog: RootCatalog,
    staged: dict[str, object],
    expected_target_handle: object,
    instance_id: str,
    state_directory: Path,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
) -> dict[str, object]:
    """CAS-publish a pre-staged rewrite by replacing only its recorded object."""
    root_id = staged.get("rootId")
    path = staged.get("path")
    temporary_name = staged.get("temporaryName")
    if not all(isinstance(value, str) for value in (root_id, path, temporary_name)):
        raise EditorError("conflict")
    canonical = "/".join(parse_relative_path(path))
    target_context = None
    published = False
    try:
        with _file_lock(state_directory, instance_id, root_id, canonical):
            try:
                target_context = _open_target(catalog, root_id, canonical, writable=False, max_bytes=max_bytes)
                target = target_context.__enter__()
            except AddressRejected as exc:
                raise _address_error(exc) from exc
            except OSError as exc:
                raise _filesystem_error(exc) from exc
            current_handle = _kernel_file_handle(target.parent_fd, target.name)
            stage_handle = staged.get("stageHandle")
            current_version = _version(
                state_directory, instance_id, root_id, canonical, target.digest, target.info
            )
            if target.digest == staged.get("afterDigest") and current_handle == stage_handle:
                saved_version = staged.get("savedVersion")
                if isinstance(saved_version, str) and not hmac.compare_digest(saved_version, current_version):
                    raise EditorError("conflict", current=_revision_payload(target.data, current_version))
                return {
                    "savedVersion": current_version,
                    "content": target.data.decode("utf-8"),
                    "size": len(target.data),
                }
            expected_version = staged.get("restoredTargetVersion", staged.get("targetVersion"))
            if not isinstance(expected_version, str) or not hmac.compare_digest(
                expected_version, current_version
            ):
                raise EditorError("conflict", current=_revision_payload(target.data, current_version))
            if target.digest != staged.get("beforeDigest") or current_handle != expected_target_handle:
                raise EditorError("conflict", current=_revision_payload(target.data, current_version))
            acl = base64.b64decode(staged["acl"], validate=True) if isinstance(staged.get("acl"), str) else None
            if _access_policy(target) != (staged.get("uid"), staged.get("gid"), staged.get("mode"), acl):
                raise EditorError("conflict")
            stage_parent_path = staged.get("stageParent")
            if not isinstance(stage_parent_path, str):
                raise EditorError("conflict")
            stage_parent_fd = open_existing_directory(catalog, root_id, stage_parent_path)
            try:
                try:
                    stage_fd = os.open(
                        temporary_name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                        dir_fd=stage_parent_fd,
                    )
                except OSError as exc:
                    raise EditorError("conflict") from exc
                try:
                    stage_info = os.fstat(stage_fd)
                    stage_chunks: list[bytes] = []
                    while True:
                        block = os.read(stage_fd, 64 * 1024)
                        if not block:
                            break
                        stage_chunks.append(block)
                    stage_bytes = b"".join(stage_chunks)
                    stage_attrs = os.listxattr(stage_fd)
                    stage_acl = os.getxattr(stage_fd, _ACCESS_ACL) if _ACCESS_ACL in stage_attrs else None
                    if (
                        not stat.S_ISREG(stage_info.st_mode)
                        or _kernel_file_handle(stage_parent_fd, temporary_name) != stage_handle
                        or os.fstat(stage_parent_fd).st_dev != os.fstat(target.parent_fd).st_dev
                        or hashlib.sha256(stage_bytes).hexdigest() != staged.get("afterDigest")
                        or len(stage_bytes) != staged.get("size")
                        or stage_info.st_uid != staged.get("uid")
                        or stage_info.st_gid != staged.get("gid")
                        or stat.S_IMODE(stage_info.st_mode) != staged.get("mode")
                        or stage_acl != acl
                        or any(name != _ACCESS_ACL for name in stage_attrs)
                    ):
                        raise EditorError("conflict")
                finally:
                    os.close(stage_fd)
                os.replace(
                    temporary_name,
                    target.name,
                    src_dir_fd=stage_parent_fd,
                    dst_dir_fd=target.parent_fd,
                )
                published = True
                try:
                    os.fsync(stage_parent_fd)
                    os.fsync(target.parent_fd)
                except OSError as exc:
                    raise EditorError("indeterminate") from exc
            finally:
                os.close(stage_parent_fd)
            target_context.__exit__(None, None, None)
            target_context = None
            with _open_target(catalog, root_id, canonical, writable=False, max_bytes=max_bytes) as saved:
                if saved.digest != staged.get("afterDigest") or _kernel_file_handle(saved.parent_fd, saved.name) != stage_handle:
                    raise EditorError("indeterminate")
                version = _version(state_directory, instance_id, root_id, canonical, saved.digest, saved.info)
                return {"savedVersion": version, "content": saved.data.decode("utf-8"), "size": len(saved.data)}
    except AddressRejected as exc:
        if published:
            raise EditorError("indeterminate") from exc
        raise _address_error(exc) from exc
    except OSError as exc:
        if published:
            raise EditorError("indeterminate") from exc
        raise _filesystem_error(exc) from exc
    finally:
        if target_context is not None:
            target_context.__exit__(None, None, None)


def discard_document_rewrite(catalog: RootCatalog, staged: dict[str, object]) -> None:
    """Remove only a still-unpublished stage whose kernel handle matches its journal."""
    root_id, path, temporary_name = staged.get("rootId"), staged.get("path"), staged.get("temporaryName")
    if not all(isinstance(value, str) for value in (root_id, path, temporary_name)):
        return
    try:
        parent_path = staged.get("stageParent")
        if not isinstance(parent_path, str):
            return
        parent_fd = open_existing_directory(catalog, root_id, parent_path)
    except (AddressRejected, OSError):
        return
    try:
        if _kernel_file_handle(parent_fd, temporary_name) == staged.get("stageHandle"):
            os.unlink(temporary_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except (OSError, EditorError):
        return
    finally:
        os.close(parent_fd)


def bind_document_version(
    catalog: RootCatalog,
    root_id: str,
    path: str,
    expected_handle: object,
    expected_digest: str,
    instance_id: str,
    state_directory: Path,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
) -> str:
    """Capture a strong revision only after confirming a moved target handle."""
    try:
        canonical = "/".join(parse_relative_path(path))
        with _open_target(catalog, root_id, canonical, writable=False, max_bytes=max_bytes) as target:
            current_handle = _kernel_file_handle(target.parent_fd, target.name)
            current_version = _version(
                state_directory, instance_id, root_id, canonical, target.digest, target.info
            )
            if target.digest != expected_digest or current_handle != expected_handle:
                raise EditorError("conflict", current=_revision_payload(target.data, current_version))
            return current_version
    except AddressRejected as exc:
        raise _address_error(exc) from exc
    except OSError as exc:
        raise _filesystem_error(exc) from exc


def _kernel_file_handle(parent_fd: int, name: str) -> dict[str, object]:
    try:
        from hopper_files.trash import _file_handle_at

        return _file_handle_at(parent_fd, name)
    except Exception as exc:
        raise EditorError("metadata_unsupported") from exc


def _open_stage_parent(
    catalog: RootCatalog,
    root_id: str,
    destination_parent: tuple[str, ...],
) -> tuple[str, int]:
    """Pin the nearest existing writable ancestor on the destination root.

    Directory moves can carry a rewritten note into a child directory that does
    not exist until after the directory itself is published. A sibling stage in
    its nearest existing ancestor is still on the same destination filesystem.
    """
    for count in range(len(destination_parent), -1, -1):
        parent_path = "/".join(destination_parent[:count])
        try:
            return parent_path, open_existing_directory(catalog, root_id, parent_path)
        except AddressRejected as exc:
            if not parent_path or not address_has_missing_component(catalog, root_id, parent_path):
                raise
    raise AddressRejected("not_found")


def read_document(
    catalog: RootCatalog,
    root_id: str,
    path: str,
    instance_id: str,
    state_directory: Path,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
) -> dict[str, object]:
    parts = parse_relative_path(path)
    if not parts:
        raise AddressRejected("invalid")
    canonical = "/".join(parts)
    parent_fd = open_existing_directory(catalog, root_id, "/".join(parts[:-1]))
    try:
        return _read_document_at(
            catalog, root_id, canonical, parts[-1], parent_fd, instance_id, state_directory, max_bytes
        )
    finally:
        os.close(parent_fd)


def _read_document_at(
    catalog: RootCatalog,
    root_id: str,
    canonical: str,
    name: str,
    parent_fd: int,
    instance_id: str,
    state_directory: Path,
    max_bytes: int,
) -> dict[str, object]:
    with open_regular(catalog, root_id, canonical) as (fd, info):
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise AddressRejected("conflict") from exc
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            raise AddressRejected("conflict")
        if info.st_size > max_bytes:
            raise EditorError("limit")
        before = os.fstat(fd)
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(fd, 64 * 1024)
            if not block:
                break
            size += len(block)
            if size > max_bytes:
                raise EditorError("limit")
            chunks.append(block)
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            raise EditorError("conflict")
        data = b"".join(chunks)
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EditorError("invalid_encoding") from exc
        reason = editability(parent_fd, name, fd, after)
        return {
            "rootId": root_id,
            "path": canonical,
            "address": "/" + canonical,
            "editable": reason is None,
            "readOnlyReason": reason,
            "content": text,
            "version": _version(
                state_directory,
                instance_id,
                root_id,
                canonical,
                hashlib.sha256(data).hexdigest(),
                after,
            ),
            "size": len(data),
        }


@contextmanager
def _open_target(
    catalog: RootCatalog,
    root_id: str,
    raw_path: str,
    *,
    writable: bool,
    max_bytes: int = MAX_TEXT_BYTES,
) -> Iterator[Target]:
    parts = parse_relative_path(raw_path)
    if not parts:
        raise AddressRejected("invalid")
    path = "/".join(parts)
    _record, parent = _open_directory_for_create(catalog, root_id, "/".join(parts[:-1]), parts[-1])
    fd = -1
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        fd = os.open(parts[-1], flags, dir_fd=parent.fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AddressRejected("forbidden")
        if writable:
            reason = editability(parent.fd, parts[-1], fd, info)
            if reason is not None:
                raise EditorError("not_editable", reason=reason)
        # The descriptor walk from / confirms that the name still resolves to
        # this inode without meeting a symbolic link.
        with open_regular(catalog, root_id, path) as (checked_fd, checked_info):
            if (checked_info.st_dev, checked_info.st_ino) != (info.st_dev, info.st_ino):
                raise AddressRejected("conflict")
            before = os.fstat(fd)
            chunks: list[bytes] = []
            size = 0
            while True:
                block = os.read(fd, 64 * 1024)
                if not block:
                    break
                size += len(block)
                if size > max_bytes:
                    raise EditorError("limit")
                chunks.append(block)
            after = os.fstat(fd)
            if _identity(before) != _identity(after):
                raise AddressRejected("conflict")
            if writable:
                write_fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=parent.fd,
                )
                try:
                    write_info = os.fstat(write_fd)
                    if (write_info.st_dev, write_info.st_ino) != (info.st_dev, info.st_ino):
                        raise AddressRejected("conflict")
                finally:
                    os.close(write_fd)
        try:
            attrs = os.listxattr(fd)
        except OSError as exc:
            raise EditorError("metadata_unsupported") from exc
        unsupported = [name for name in attrs if name != _ACCESS_ACL]
        if unsupported or info.st_mode & (stat.S_ISUID | stat.S_ISGID):
            raise EditorError("metadata_unsupported")
        try:
            acl = os.getxattr(fd, _ACCESS_ACL) if _ACCESS_ACL in attrs else None
        except OSError as exc:
            raise EditorError("metadata_unsupported") from exc
        if _identity(os.fstat(fd)) != _identity(info):
            raise AddressRejected("conflict")
        data = b"".join(chunks)
        yield Target(root_id, path, parent.fd, parts[-1], fd, info, data, hashlib.sha256(data).hexdigest(), acl)
    finally:
        if fd >= 0:
            os.close(fd)
        if parent.fd >= 0:
            os.close(parent.fd)


@contextmanager
def _file_lock(state_directory: Path, instance_id: str, root_id: str, path: str) -> Iterator[None]:
    lock_directory = state_directory / "file-locks"
    try:
        lock_directory.mkdir(mode=0o700, exist_ok=True)
        if lock_directory.is_symlink() or not lock_directory.is_dir():
            raise OSError(errno.EPERM, "invalid editor lock directory")
        os.chmod(lock_directory, 0o700)
        key = hashlib.sha256(
            json.dumps([instance_id, root_id, path], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        fd = os.open(lock_directory / f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise EditorError("storage_unavailable") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise EditorError("storage_unavailable")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def save_document(
    catalog: RootCatalog,
    root_id: str,
    path: str,
    base_version: object,
    content: object,
    instance_id: str,
    state_directory: Path,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
) -> dict[str, object]:
    if not isinstance(base_version, str) or len(base_version) > 128:
        raise EditorError("conflict", current=_current(catalog, root_id, path, instance_id, state_directory))
    if not isinstance(content, str):
        raise EditorError("invalid_request")
    try:
        encoded = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise EditorError("invalid_request") from exc
    if len(encoded) > max_bytes:
        raise EditorError("limit")
    canonical = "/".join(parse_relative_path(path))
    with _file_lock(state_directory, instance_id, root_id, canonical):
        try:
            target_context = _open_target(catalog, root_id, canonical, writable=True, max_bytes=max_bytes)
            target = target_context.__enter__()
        except EditorError:
            raise
        except AddressRejected as exc:
            raise _address_error(exc) from exc
        except OSError as exc:
            raise _filesystem_error(exc) from exc
        published = False
        temporary = f".hopper-stage-{secrets.token_hex(16)}.tmp"
        try:
            current_version = _version(state_directory, instance_id, root_id, canonical, target.digest, target.info)
            if not hmac.compare_digest(base_version, current_version):
                raise EditorError("conflict", current=_revision_payload(target.data, current_version))
            _check_supported_metadata(target)
            mode = stat.S_IMODE(target.info.st_mode)
            temp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=target.parent_fd,
            )
            try:
                try:
                    os.fchown(temp_fd, target.info.st_uid, target.info.st_gid)
                    os.fchmod(temp_fd, mode)
                    if target.acl is not None:
                        os.setxattr(temp_fd, _ACCESS_ACL, target.acl)
                    elif _ACCESS_ACL in os.listxattr(temp_fd):
                        # A directory default ACL is inherited by newly-created
                        # files. It was not part of the original file's policy.
                        os.removexattr(temp_fd, _ACCESS_ACL)
                        os.fchmod(temp_fd, mode)
                    details = os.fstat(temp_fd)
                    attrs_after = os.listxattr(temp_fd)
                    acl_after = os.getxattr(temp_fd, _ACCESS_ACL) if _ACCESS_ACL in attrs_after else None
                    if (
                        details.st_uid != target.info.st_uid
                        or details.st_gid != target.info.st_gid
                        or stat.S_IMODE(details.st_mode) != mode
                        or acl_after != target.acl
                        or any(name != _ACCESS_ACL for name in attrs_after)
                    ):
                        raise EditorError("metadata_unsupported")
                except OSError as exc:
                    raise EditorError("metadata_unsupported") from exc
                _write_all(temp_fd, encoded)
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
            # Re-read the current directory entry immediately before publication.
            with _open_target(catalog, root_id, canonical, writable=True, max_bytes=max_bytes) as latest:
                latest_version = _version(
                    state_directory, instance_id, root_id, canonical, latest.digest, latest.info
                )
                if not hmac.compare_digest(base_version, latest_version):
                    raise EditorError("conflict", current=_revision_payload(latest.data, latest_version))
                if (latest.info.st_dev, latest.info.st_ino) != (target.info.st_dev, target.info.st_ino):
                    raise EditorError("conflict")
                if _access_policy(latest) != _access_policy(target):
                    raise EditorError("conflict", current=_revision_payload(latest.data, latest_version))
            # This is an atomic same-filesystem replace. An external writer can
            # still race after the final check; cooperative sessions share flock.
            os.replace(temporary, target.name, src_dir_fd=target.parent_fd, dst_dir_fd=target.parent_fd)
            published = True
            try:
                os.fsync(target.parent_fd)
            except OSError as exc:
                raise EditorError("indeterminate", current=_current(catalog, root_id, canonical, instance_id, state_directory)) from exc
            with _open_target(catalog, root_id, canonical, writable=False, max_bytes=max_bytes) as saved:
                version = _version(state_directory, instance_id, root_id, canonical, saved.digest, saved.info)
                if saved.data != encoded:
                    raise EditorError(
                        "indeterminate",
                        current=_revision_payload(saved.data, version),
                    )
                persisted = encoded.decode("utf-8", errors="strict")
                return {"savedVersion": version, "content": persisted, "size": len(saved.data)}
        except EditorError as exc:
            if published and exc.code != "indeterminate":
                raise EditorError(
                    "indeterminate",
                    current=_current(catalog, root_id, canonical, instance_id, state_directory),
                ) from exc
            raise
        except AddressRejected as exc:
            if published:
                raise EditorError(
                    "indeterminate",
                    current=_current(catalog, root_id, canonical, instance_id, state_directory),
                ) from exc
            raise EditorError("conflict") from exc
        except StateError as exc:
            if published:
                raise EditorError(
                    "indeterminate",
                    current=_current(catalog, root_id, canonical, instance_id, state_directory),
                ) from exc
            raise EditorError("storage_unavailable") from exc
        except OSError as exc:
            if published:
                raise EditorError("indeterminate", current=_current(catalog, root_id, canonical, instance_id, state_directory)) from exc
            raise _filesystem_error(exc) from exc
        finally:
            if not published:
                try:
                    os.unlink(temporary, dir_fd=target.parent_fd)
                except OSError:
                    pass
            target_context.__exit__(None, None, None)


def _current(
    catalog: RootCatalog, root_id: str, path: str, instance_id: str, state_directory: Path
) -> dict[str, object] | None:
    try:
        with _open_target(catalog, root_id, path, writable=False) as target:
            version = _version(state_directory, instance_id, root_id, target.path, target.digest, target.info)
            return _revision_payload(target.data, version)
    except (EditorError, AddressRejected, OSError, StateError):
        return None


def _revision_payload(data: bytes, version: str) -> dict[str, object]:
    try:
        return {"content": data.decode("utf-8", errors="strict"), "version": version}
    except UnicodeDecodeError:
        return {"version": version, "invalidEncoding": True}


def _address_error(error: AddressRejected) -> EditorError:
    if error.code == "forbidden" and isinstance(error.__cause__, FileNotFoundError):
        return EditorError("not_found")
    code = {
        "invalid": "invalid_request",
        "not_found": "not_found",
        "unavailable": "not_found",
        "forbidden": "forbidden",
        "conflict": "conflict",
    }.get(error.code, "forbidden")
    return EditorError(code)


def _filesystem_error(error: OSError) -> EditorError:
    if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
        return EditorError("storage_unavailable")
    if error.errno in {errno.ENOENT, errno.ENOTDIR}:
        return EditorError("not_found")
    if error.errno in {errno.EACCES, errno.EPERM, errno.ELOOP, errno.EMLINK}:
        return EditorError("forbidden")
    return EditorError("io_error")


def _version(
    state_directory: Path,
    instance_id: str,
    root_id: str,
    path: str,
    digest: str,
    info: os.stat_result,
) -> str:
    payload = json.dumps(
        {
            "instance": instance_id,
            "path": "/" + path,
            "sha256": digest,
            "identity": list(_identity(info)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    del root_id
    # Bound to the absolute canonical path (HF-SAVE-001, HF-NAV-005).
    signature = hmac.new(read_signing_secret(state_directory), b"editor-v2\0" + payload, hashlib.sha256).digest()
    return "v2." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size


def _check_supported_metadata(target: Target) -> None:
    try:
        attrs = os.listxattr(target.fd)
    except OSError as exc:
        raise EditorError("metadata_unsupported") from exc
    if any(name != _ACCESS_ACL for name in attrs) or target.info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise EditorError("metadata_unsupported")


def _access_policy(target: Target) -> tuple[int, int, int, bytes | None]:
    return target.info.st_uid, target.info.st_gid, stat.S_IMODE(target.info.st_mode), target.acl


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short editor write")
        view = view[written:]
