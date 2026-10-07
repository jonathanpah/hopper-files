"""Version 1 scrypt parameters. Callers cannot select a weaker KDF."""

from __future__ import annotations

import hashlib
import hmac

SCRYPT_VERSION = 1
SCRYPT_ALGORITHM = "scrypt"
SCRYPT_N = 131072
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SCRYPT_SALT_LENGTH = 16
SCRYPT_MAXMEM = 268435456
PASSWORD_MAX_BYTES = 1024

PROOF_PASSWORD = b"hopper-files-runtime-proof"
PROOF_SALT = bytes.fromhex("00112233445566778899aabbccddeeff")
PROOF_DIGEST = bytes.fromhex(
    "7af69960d6ffbfad6496d72743a02acd26d37fe8c313a47d1c593a22136c871d"
    "8d56a41a75c901b5fafb17b4d7f833ebeb53508d91bde8aff97f2cfdc1281f03"
)

_PROVED = False


class KdfUnavailable(RuntimeError):
    """The runtime cannot execute the required scrypt parameters."""


def derive(password: bytes, salt: bytes) -> bytes:
    """Hash one password with the fixed version 1 parameters."""
    if len(password) > PASSWORD_MAX_BYTES:
        raise ValueError("password exceeds the KDF input limit")
    if len(salt) != SCRYPT_SALT_LENGTH:
        raise ValueError("salt must be 16 bytes")
    try:
        digest = hashlib.scrypt(
            password,
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        )
    except (MemoryError, ValueError, OSError) as exc:
        raise KdfUnavailable("scrypt parameters are unavailable") from exc
    if len(digest) != SCRYPT_DKLEN:
        raise KdfUnavailable("scrypt returned an unexpected digest length")
    return digest


def prove_kdf_runtime(expected: bytes = PROOF_DIGEST) -> None:
    """Prove this process can run the required scrypt parameters.

    A mismatch or an unavailable KDF fails closed. Successful proof of the
    built-in vector is remembered for the rest of the process.
    """
    global _PROVED
    if _PROVED and expected == PROOF_DIGEST:
        return
    digest = derive(PROOF_PASSWORD, PROOF_SALT)
    if not hmac.compare_digest(digest, expected):
        raise KdfUnavailable("scrypt proof did not match the required parameters")
    if expected == PROOF_DIGEST:
        _PROVED = True
