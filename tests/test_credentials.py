from __future__ import annotations

import hashlib
import hmac
import os
import pty
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import hopper_files.admin as admin_module
from hopper_files import roots
from hopper_files.admin import main
from hopper_files.credentials import (
    MalformedVerifier,
    decode_verifier,
    publish_verifier,
    read_verifier,
    verify_password,
)
from hopper_files.kdf import (
    PASSWORD_MAX_BYTES,
    SCRYPT_DKLEN,
    SCRYPT_MAXMEM,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SCRYPT_SALT_LENGTH,
    KdfUnavailable,
    prove_kdf_runtime,
)
from hopper_files.state import initialize_state

from conftest import PASSWORD, service_account, write_config
from hopper_files.config import load_config


def test_equal_passwords_get_distinct_verifiers(tmp_path) -> None:
    path = write_config(tmp_path, account=service_account())
    config = load_config(path)
    initialize_state(config.state_directory, config.instance_id)
    publish_verifier(config.state_directory, PASSWORD, 1)
    first = read_verifier(config.state_directory / "credential")
    publish_verifier(config.state_directory, PASSWORD, 2)
    second = read_verifier(config.state_directory / "credential")
    credential = config.state_directory / "credential"

    assert first.salt != second.salt
    assert first.digest != second.digest
    assert hmac.compare_digest(
        hashlib.scrypt(
            PASSWORD.encode(),
            salt=second.salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        ),
        second.digest,
    )
    assert PASSWORD.encode() not in credential.read_bytes()
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.state_directory.stat().st_mode) == 0o700
    assert verify_password(config.state_directory, PASSWORD) == 2


def test_malformed_parameter_does_not_select_another_kdf(tmp_path) -> None:
    path = write_config(tmp_path, account=service_account(), instance_id="params")
    config = load_config(path)
    initialize_state(config.state_directory, config.instance_id)
    publish_verifier(config.state_directory, PASSWORD, 1)
    blob = bytearray((config.state_directory / "credential").read_bytes())
    blob[12] ^= 0x01
    (config.state_directory / "credential").write_bytes(bytes(blob))
    assert isinstance(decode_verifier(bytes(blob)), MalformedVerifier)
    assert verify_password(config.state_directory, PASSWORD) is None


def test_runtime_proof_rejects_a_different_result() -> None:
    prove_kdf_runtime()
    with pytest.raises(KdfUnavailable):
        prove_kdf_runtime(b"\x00" * SCRYPT_DKLEN)


def test_password_command_rejects_arguments_and_uses_the_tty(tmp_path) -> None:
    path = write_config(tmp_path, account=service_account(), instance_id="command")
    secret = "synthetic-tty-secret"
    rejected = subprocess.run(
        [sys.executable, "-m", "hopper_files.admin", "set-password", "--config", str(path), "--password", secret],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert secret not in rejected.stderr
    assert secret not in rejected.stdout

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "hopper_files.admin", "set-password", "--config", str(path)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = _drive_prompts(master, secret, process)
    assert process.returncode == 0
    assert secret.encode() not in output
    credential = load_config(path).state_directory / "credential"
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert secret.encode() not in credential.read_bytes()
    assert main(["check", "--config", str(path)]) == 0
    assert len(secret.encode()) < PASSWORD_MAX_BYTES


def test_set_password_succeeds_when_admin_cwd_is_not_searchable(tmp_path, monkeypatch, capsys) -> None:
    path = write_config(tmp_path / "config", instance_id="private-cwd-password")
    private_cwd = tmp_path / "private-admin-home"
    private_cwd.mkdir(mode=0o700)
    private_cwd.chmod(0o700)
    monkeypatch.chdir(private_cwd)
    original_is_file = Path.is_file

    def is_file(candidate: Path) -> bool:
        if candidate == private_cwd / "pyproject.toml":
            raise PermissionError("synthetic administrator-private cwd")
        return original_is_file(candidate)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(admin_module, "getpass", lambda _prompt: PASSWORD)

    assert main(["set-password", "--config", str(path)]) == 0
    assert "Senha atualizada." in capsys.readouterr().out
    verifier = read_verifier(path.parent / "state" / "credential")
    assert len(verifier.salt) == SCRYPT_SALT_LENGTH
    assert verify_password(path.parent / "state", PASSWORD) == 1
    assert stat.S_IMODE((path.parent / "state" / "credential").stat().st_mode) == 0o600


def test_set_password_refuses_an_empty_password(tmp_path, monkeypatch, capsys) -> None:
    path = write_config(tmp_path / "config", instance_id="empty-password")
    monkeypatch.setattr(admin_module, "getpass", lambda _prompt: "")

    assert main(["set-password", "--config", str(path)]) == 2
    assert "A senha não pode ficar vazia." in capsys.readouterr().err
    assert not (path.parent / "state" / "credential").exists()


def test_set_password_names_a_state_directory_the_account_cannot_create(tmp_path, monkeypatch, capsys) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can create the directory, so the refusal cannot be observed")
    locked = tmp_path / "locked-parent"
    locked.mkdir(mode=0o700)
    state = locked / "state"
    path = write_config(tmp_path / "config", instance_id="uncreatable-state", state=state)
    # The helper creates the state directory; remove it so the account has to create it.
    state.rmdir()
    locked.chmod(0o500)
    monkeypatch.setattr(admin_module, "getpass", lambda _prompt: PASSWORD)
    try:
        assert main(["set-password", "--config", str(path)]) == 1
    finally:
        locked.chmod(0o700)
    error = capsys.readouterr().err
    assert error.startswith("A configuração da instância foi recusada: ")
    assert str(state) in error
    assert "Traceback" not in error


def _drive_prompts(fd: int, secret: str, process: subprocess.Popen[bytes]) -> bytes:
    import select
    import time

    collected = b""
    answers = [secret + "\n", secret + "\n"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and process.poll() is None:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        collected += chunk
        if answers and b"Nova senha:" in collected and len(answers) == 2:
            os.write(fd, answers.pop(0).encode())
        elif answers and b"Confirme a nova senha:" in collected:
            os.write(fd, answers.pop(0).encode())
    process.wait(timeout=30)
    os.close(fd)
    return collected
