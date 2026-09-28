from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from hopper_files.clock import ManualClock

from conftest import CLOCK_START, PASSWORD, boot


def _bad_attempt(world, forwarded_for: str | None = None, client=None):
    headers = {}
    if forwarded_for is not None:
        headers["x-forwarded-for"] = forwarded_for
    target = world
    response = target.post_login(PASSWORD, nonce="not-a-real-nonce", **headers)
    return response


def test_origin_bucket_retry_after_and_no_refill_on_success(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path, clock, password=PASSWORD) as world:
        for _ in range(4):
            assert _bad_attempt(world).status_code == 403
        success = world.post_login(PASSWORD)
        blocked = _bad_attempt(world)
        clock.advance(59)
        still = _bad_attempt(world)
        clock.advance(1)
        restored = _bad_attempt(world)

    assert success.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json() == {"error": "too_many_requests"}
    assert blocked.headers["retry-after"] == "60"
    assert "remaining" not in blocked.text
    assert still.status_code == 429
    assert still.headers["retry-after"] == "1"
    assert restored.status_code == 403


def test_spoofed_forwarded_for_stays_on_the_socket_origin(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START)) as world:
        for _ in range(5):
            assert _bad_attempt(world).status_code == 403
        spoofed = _bad_attempt(world, forwarded_for="203.0.113.50")

    assert spoofed.status_code == 429
    assert spoofed.json() == {"error": "too_many_requests"}


def test_global_bucket_limits_a_fresh_origin(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path, clock) as world:
        for index in range(4):
            client_host = f"198.51.100.{index + 1}"
            from fastapi.testclient import TestClient

            client = TestClient(
                world.client.app,
                base_url=f"http://{world.config.host_header}",
                client=(client_host, 40000),
            )
            with client:
                for _ in range(5):
                    response = client.post(
                        "/login",
                        json={"nonce": "not-a-real-nonce", "password": PASSWORD},
                        headers={"origin": world.config.origin},
                    )
                    assert response.status_code == 403
        fresh = TestClient(
            world.client.app,
            base_url=f"http://{world.config.host_header}",
            client=("198.51.100.20", 40000),
        )
        with fresh:
            blocked = fresh.post(
                "/login",
                json={"nonce": "not-a-real-nonce", "password": PASSWORD},
                headers={"origin": world.config.origin, "x-forwarded-for": "203.0.113.50"},
            )
    assert blocked.status_code == 429
    assert blocked.json() == {"error": "too_many_requests"}
    assert blocked.headers["retry-after"] == "15"


def test_buckets_persist_across_processes_and_do_not_lock_forever(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path, clock) as world:
        for _ in range(5):
            assert _bad_attempt(world).status_code == 403
        denied = _run_consume(world.config.state_directory, "127.0.0.1", clock.now())
        allowed = _run_consume(world.config.state_directory, "127.0.0.1", clock.now() + 60)

    assert denied == {"allowed": False, "retry_after": 60}
    assert allowed["allowed"] is True


def test_remote_untrusted_forwarded_for_does_not_spend_a_bucket(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path, clock, mode="remote", port=9010) as world:
        from fastapi.testclient import TestClient

        untrusted = TestClient(
            world.client.app,
            base_url=f"http://{world.config.host_header}",
            client=("203.0.113.8", 40000),
            headers={
                "origin": world.config.origin,
                "x-forwarded-proto": "https",
                "x-forwarded-host": world.config.host_header,
                "x-forwarded-for": "198.51.100.99",
            },
        )
        with untrusted:
            spoofed = untrusted.post(
                "/login",
                json={"nonce": "not-a-real-nonce", "password": PASSWORD},
            )
        for _ in range(5):
            response = world.client.post(
                "/login",
                json={"nonce": "not-a-real-nonce", "password": PASSWORD},
                headers={"origin": world.config.origin, "x-forwarded-for": "198.51.100.99"},
            )
            assert response.status_code == 403
        blocked = world.client.post(
            "/login",
            json={"nonce": "not-a-real-nonce", "password": PASSWORD},
            headers={"origin": world.config.origin, "x-forwarded-for": "198.51.100.99"},
        )

    assert spoofed.status_code == 403
    assert blocked.status_code == 429


def test_busy_verifier_returns_the_same_generic_limit(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path, clock, password=PASSWORD) as world:
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX); sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(30)",
                str(world.config.state_directory / "kdf.lock"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline() == "ready\n"
            outcome: dict[str, object] = {}

            def attempt() -> None:
                outcome["response"] = world.post_login(PASSWORD)

            worker = threading.Thread(target=attempt)
            started = time.monotonic()
            worker.start()
            worker.join(2)
            elapsed = time.monotonic() - started
            assert not worker.is_alive()
            response = outcome["response"]
        finally:
            holder.kill()
            holder.wait(timeout=5)

    assert response.status_code == 429
    assert response.json() == {"error": "too_many_requests"}
    assert response.headers["retry-after"] == "1"
    assert elapsed < 2


def _run_consume(state_directory, origin: str, now: float) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; from hopper_files.limiter import consume_login_attempt; decision=consume_login_attempt(Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])); print(json.dumps({'allowed': decision.allowed, 'retry_after': decision.retry_after}))",
            str(state_directory),
            origin,
            str(now),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)
