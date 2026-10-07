from __future__ import annotations

import os
import pwd

import pytest

import threading
from pathlib import Path

from hopper_files.clock import ManualClock
from hopper_files.config import ConfigError, require_service_account
from hopper_files.credentials import accept_login, change_password, read_verifier
from hopper_files.state import StateError, initialize_state

from conftest import CLOCK_START, PASSWORD, boot, write_config


def test_replay_across_instances_and_password_change(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path / "alpha", clock, password=PASSWORD, instance_id="alpha", port=8901) as alpha:
        with boot(tmp_path / "beta", clock, password=PASSWORD, instance_id="beta", port=8902) as beta:
            left = alpha.post_login(PASSWORD)
            right = beta.post_login(PASSWORD)
            from fastapi.testclient import TestClient

            stolen_cookie = (
                f"{beta.config.cookie_name}="
                f"{left.cookies[alpha.config.cookie_name]}"
            )
            intruder = TestClient(
                beta.client.app,
                base_url=f"http://{beta.config.host_header}",
                client=("127.0.0.1", 40000),
            )
            with intruder:
                stolen = intruder.get("/app", headers={"cookie": stolen_cookie})
                intruder.post(
                    "/logout",
                    headers={
                        "origin": beta.config.origin,
                        "x-csrf-token": left.json()["csrfToken"],
                        "cookie": stolen_cookie,
                    },
                )
            alpha_still = alpha.client.get("/app")
            from hopper_files.credentials import change_password

            before = read_verifier(alpha.config.state_directory / "credential")
            change_password(
                alpha.config.state_directory,
                "synthetic-rotated-horse",
                instance_id=alpha.config.instance_id,
            )
            after = read_verifier(alpha.config.state_directory / "credential")
            old_password = alpha.post_login(PASSWORD)
            new_password = alpha.post_login("synthetic-rotated-horse")
            beta_still = beta.client.get("/app")

    assert left.status_code == 200
    assert right.status_code == 200
    assert stolen.status_code == 401
    assert alpha_still.status_code == 200
    assert before.salt != after.salt
    assert before.digest != after.digest
    assert before.epoch + 1 == after.epoch
    assert old_password.status_code == 401
    assert new_password.status_code == 200
    assert beta_still.status_code == 200
    assert (alpha.config.state_directory / "signing-secret").read_bytes() != (
        beta.config.state_directory / "signing-secret"
    ).read_bytes()


def test_service_account_must_already_exist_and_match_the_process(tmp_path) -> None:
    missing = write_config(tmp_path / "missing", account="hf-missing-account", instance_id="missing")
    from hopper_files.config import load_config

    with pytest.raises(ConfigError, match="does not exist"):
        require_service_account(load_config(missing))
    other = pwd.getpwnam("nobody")
    assert other.pw_uid != os.geteuid()
    mismatch = write_config(tmp_path / "nobody", account="nobody", instance_id="nobody")
    with pytest.raises(ConfigError, match="does not match"):
        require_service_account(load_config(mismatch))


def test_second_instance_cannot_reuse_or_alias_bound_state(tmp_path, monkeypatch) -> None:
    shared = tmp_path / "state"
    initialize_state(shared, "alpha")
    change_password(shared, "synthetic-alpha-secret", instance_id="alpha")
    from hopper_files.limiter import consume_login_attempt

    consume_login_attempt(shared, "127.0.0.1", CLOCK_START)
    opened = accept_login(
        shared,
        instance_id="alpha",
        cookie_lifetime=60,
        password="synthetic-alpha-secret",
        now=CLOCK_START,
    )
    assert opened is not None
    before = _snapshot(shared)
    alias = tmp_path / "alias"
    alias.symlink_to(shared, target_is_directory=True)
    relative = tmp_path / "relative" / ".." / "state"

    def forbidden(*args, **kwargs):
        raise AssertionError("refused instance reached the KDF")

    monkeypatch.setattr("hopper_files.kdf.derive", forbidden)
    for path in (shared, alias, relative):
        with pytest.raises(StateError):
            initialize_state(path, "beta")
        with pytest.raises(StateError):
            change_password(path, "synthetic-beta-secret", instance_id="beta")
        with pytest.raises(StateError):
            accept_login(
                path,
                instance_id="beta",
                cookie_lifetime=60,
                password="synthetic-alpha-secret",
                now=CLOCK_START,
            )

    assert _snapshot(shared) == before
    assert json_instance_id(shared) == "alpha"
    monkeypatch.undo()
    again = accept_login(
        shared,
        instance_id="alpha",
        cookie_lifetime=60,
        password="synthetic-alpha-secret",
        now=CLOCK_START + 1,
    )
    assert again is not None


def test_simultaneous_initialization_binds_only_one_instance(tmp_path) -> None:
    shared = tmp_path / "race"
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def claim(instance_id: str) -> None:
        barrier.wait()
        try:
            initialize_state(shared, instance_id)
        except StateError:
            outcomes.append((instance_id, "refused"))
        else:
            outcomes.append((instance_id, "bound"))

    threads = [threading.Thread(target=claim, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome for _, outcome in outcomes) == ["bound", "refused"]
    winner = next(instance_id for instance_id, outcome in outcomes if outcome == "bound")
    assert json_instance_id(shared) == winner
    with pytest.raises(StateError):
        initialize_state(shared, "gamma")


def _snapshot(directory: Path) -> dict[str, bytes]:
    names = ("instance.json", "signing-secret", "credential", "limiter.json")
    captured = {name: (directory / name).read_bytes() for name in names if (directory / name).is_file()}
    sessions = directory / "sessions"
    captured["sessions"] = b"".join(
        sorted(path.read_bytes() for path in sessions.iterdir() if path.is_file())
    )
    return captured


def json_instance_id(directory: Path) -> str:
    import json

    return json.loads((directory / "instance.json").read_text(encoding="utf-8"))["instanceId"]
