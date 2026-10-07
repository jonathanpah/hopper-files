"""Version 1 password verifier. The record is fixed-width and fails closed."""

from __future__ import annotations

import hmac
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path

from hopper_files import kdf
from hopper_files.kdf import (
    PASSWORD_MAX_BYTES,
    SCRYPT_DKLEN,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SCRYPT_SALT_LENGTH,
)
from hopper_files.sessions import create_session, revoke_all_sessions
from hopper_files.state import KdfBusy, atomic_write, kdf_lock, require_instance_binding

MAGIC = b"HFCR"
ALGORITHM = b"scrypt"
RECORD_LENGTH = 4 + 1 + 1 + len(ALGORITHM) + (4 * 4) + 4 + SCRYPT_SALT_LENGTH + SCRYPT_DKLEN


class PasswordTooLong(ValueError):
    """The UTF-8 password exceeds 1024 bytes and must not reach the KDF."""


class MissingVerifier:
    pass


class MalformedVerifier:
    pass


@dataclass(frozen=True)
class Verifier:
    epoch: int
    salt: bytes
    digest: bytes


def password_byte_length(password: str) -> int:
    try:
        return len(password.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PasswordTooLong("password is not exact UTF-8") from exc


def publish_verifier(state_directory: Path, password: str, epoch: int) -> None:
    raw = _password_bytes(password)
    salt = secrets.token_bytes(SCRYPT_SALT_LENGTH)
    digest = kdf.derive(raw, salt)
    atomic_write(state_directory / "credential", encode_verifier(epoch, salt, digest))


def change_password(state_directory: Path, password: str, *, instance_id: str) -> int:
    """Publish a fresh verifier and invalidate every session of this instance."""
    require_instance_binding(state_directory, instance_id)
    if password_byte_length(password) > PASSWORD_MAX_BYTES:
        raise PasswordTooLong("password exceeds 1024 bytes")
    with kdf_lock(state_directory, blocking=True):
        require_instance_binding(state_directory, instance_id)
        epoch = _next_epoch(state_directory)
        publish_verifier(state_directory, password, epoch)
        revoke_all_sessions(state_directory)
        return epoch


def change_password_with_current(
    state_directory: Path,
    *,
    instance_id: str,
    current: str,
    new: str,
) -> int | None:
    """Change the password from the login page: the current one must verify first.

    One KDF verifies the current password; only then the new verifier is published
    with a fresh salt and epoch, and every session of this instance is invalidated,
    as in the local command. None means the current password did not verify.
    """
    require_instance_binding(state_directory, instance_id)
    if password_byte_length(new) > PASSWORD_MAX_BYTES:
        raise PasswordTooLong("password exceeds 1024 bytes")
    with kdf_lock(state_directory, blocking=False):
        require_instance_binding(state_directory, instance_id)
        if verify_password(state_directory, current) is None:
            return None
        epoch = _next_epoch(state_directory)
        publish_verifier(state_directory, new, epoch)
        revoke_all_sessions(state_directory)
        return epoch


def accept_login(
    state_directory: Path,
    *,
    instance_id: str,
    cookie_lifetime: int,
    password: str,
    now: float,
) -> tuple[str, str] | None:
    """Run one KDF and, on success, create a session before releasing the lock."""
    require_instance_binding(state_directory, instance_id)
    with kdf_lock(state_directory, blocking=False):
        require_instance_binding(state_directory, instance_id)
        epoch = verify_password(state_directory, password)
        if epoch is None:
            return None
        return create_session(
            state_directory,
            instance_id=instance_id,
            epoch=epoch,
            lifetime=cookie_lifetime,
            now=now,
        )


def verify_password(state_directory: Path, password: str) -> int | None:
    """Return the credential epoch, or None after exactly one KDF."""
    raw = _password_bytes(password)
    record = read_verifier(state_directory / "credential")
    if isinstance(record, Verifier):
        digest = kdf.derive(raw, record.salt)
        if hmac.compare_digest(digest, record.digest):
            return record.epoch
        return None
    kdf.derive(raw, _dummy_salt(state_directory))
    return None


def current_epoch(state_directory: Path) -> int | None:
    record = read_verifier(state_directory / "credential")
    if isinstance(record, Verifier):
        return record.epoch
    return None


def encode_verifier(epoch: int, salt: bytes, digest: bytes) -> bytes:
    if not 1 <= epoch <= 0xFFFFFFFF:
        raise ValueError("credential epoch is exhausted")
    if len(salt) != SCRYPT_SALT_LENGTH or len(digest) != SCRYPT_DKLEN:
        raise ValueError("credential material has the wrong length")
    return b"".join(
        (
            MAGIC,
            bytes((1, len(ALGORITHM))),
            ALGORITHM,
            struct.pack(">IIII", SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN),
            struct.pack(">I", epoch),
            salt,
            digest,
        )
    )


def read_verifier(path: Path) -> Verifier | MissingVerifier | MalformedVerifier:
    if not path.is_file() or path.is_symlink():
        return MissingVerifier()
    try:
        blob = path.read_bytes()
    except OSError:
        return MalformedVerifier()
    return decode_verifier(blob)


def decode_verifier(blob: bytes) -> Verifier | MalformedVerifier:
    if len(blob) != RECORD_LENGTH or not blob.startswith(MAGIC):
        return MalformedVerifier()
    version = blob[4]
    algorithm_length = blob[5]
    algorithm = blob[6 : 6 + algorithm_length]
    offset = 6 + algorithm_length
    if version != 1 or algorithm != ALGORITHM or offset + 20 + SCRYPT_SALT_LENGTH + SCRYPT_DKLEN != len(blob):
        return MalformedVerifier()
    n, r, p, dklen = struct.unpack(">IIII", blob[offset : offset + 16])
    if (n, r, p, dklen) != (SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN):
        return MalformedVerifier()
    epoch = struct.unpack(">I", blob[offset + 16 : offset + 20])[0]
    salt_start = offset + 20
    digest_start = salt_start + SCRYPT_SALT_LENGTH
    if epoch < 1 or digest_start + SCRYPT_DKLEN != len(blob):
        return MalformedVerifier()
    return Verifier(
        epoch=epoch,
        salt=blob[salt_start:digest_start],
        digest=blob[digest_start:],
    )


def _next_epoch(state_directory: Path) -> int:
    record = read_verifier(state_directory / "credential")
    if isinstance(record, Verifier):
        if record.epoch >= 0xFFFFFFFF:
            raise ValueError("credential epoch is exhausted")
        return record.epoch + 1
    if isinstance(record, MalformedVerifier):
        return secrets.randbelow(0x7FFFFFFF - 2) + 2
    return 1


def _dummy_salt(state_directory: Path) -> bytes:
    path = state_directory / "dummy-salt"
    try:
        salt = path.read_bytes()
    except OSError:
        salt = b""
    if len(salt) != SCRYPT_SALT_LENGTH:
        return secrets.token_bytes(SCRYPT_SALT_LENGTH)
    return salt


def _password_bytes(password: str) -> bytes:
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PasswordTooLong("password is not exact UTF-8") from exc
