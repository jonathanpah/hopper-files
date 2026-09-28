from __future__ import annotations

import asyncio
import hashlib
import logging
import re

import pytest
from starlette.requests import Request

import hopper_files.api.auth as auth_api
from hopper_files.api.request_body import BodyTooLarge

from hopper_files.clock import ManualClock
from hopper_files.kdf import SCRYPT_DKLEN, SCRYPT_MAXMEM, SCRYPT_N, SCRYPT_P, SCRYPT_R

from conftest import CLOCK_START, OTHER_PASSWORD, PASSWORD, boot, cookie_flags


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_login_body_limit_stops_streaming_without_trusting_content_length(declared_length: str | None) -> None:
    maximum = auth_api.MAX_BODY_BYTES
    chunk_size = 4096
    total_size = maximum * 4
    consumed = 0

    async def receive() -> dict[str, object]:
        nonlocal consumed
        if consumed >= total_size:
            return {"type": "http.request", "body": b"", "more_body": False}
        size = min(chunk_size, total_size - consumed)
        consumed += size
        return {"type": "http.request", "body": b"x" * size, "more_body": consumed < total_size}

    headers = [(b"content-type", b"application/json")]
    if declared_length is not None:
        headers.append((b"content-length", declared_length.encode("ascii")))
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "headers": headers,
        "server": ("example.test", 80),
        "client": ("127.0.0.1", 40000),
        "root_path": "",
    }, receive)

    with pytest.raises(BodyTooLarge):
        asyncio.run(auth_api._read_login(request))

    assert consumed <= maximum + chunk_size
    assert consumed < total_size


def test_login_body_limit_accepts_exact_boundary() -> None:
    maximum = auth_api.MAX_BODY_BYTES
    prefix = b'{"nonce":"n","password":"'
    suffix = b'"}'
    body = prefix + b"x" * (maximum - len(prefix) - len(suffix)) + suffix

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(maximum).encode("ascii"))],
        "server": ("example.test", 80),
        "client": ("127.0.0.1", 40000),
        "root_path": "",
    }, receive)

    nonce, password, wants_html = asyncio.run(auth_api._read_login(request))

    assert len(body) == maximum
    assert nonce == "n"
    assert len(password) == maximum - len(prefix) - len(suffix)
    assert not wants_html


def test_login_sets_isolated_cookie_and_hides_secret_from_logs(tmp_path, caplog, monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    real = hashlib.scrypt

    def recorded(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    caplog.set_level(logging.INFO)
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        monkeypatch.setattr(hashlib, "scrypt", recorded)
        response = world.post_login(PASSWORD)
        cookie = response.headers["set-cookie"]
        csrf = response.json()["csrfToken"]
        page = world.client.get(world.config.base_path + "app")
        credential = (world.config.state_directory / "credential").read_bytes()

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    flags = cookie_flags(cookie)
    assert "httponly" in flags
    assert "samesite=strict" in flags
    assert f"path={world.config.base_path.lower()}" in flags
    assert not any(flag.startswith("domain=") for flag in flags)
    assert "secure" not in flags
    assert PASSWORD.encode() not in credential
    assert PASSWORD not in caplog.text
    assert csrf not in caplog.text
    assert page.status_code == 200
    assert "Sessão iniciada" in page.text
    assert len(calls) == 1
    assert calls[0]["n"] == SCRYPT_N
    assert calls[0]["r"] == SCRYPT_R
    assert calls[0]["p"] == SCRYPT_P
    assert calls[0]["dklen"] == SCRYPT_DKLEN
    assert calls[0]["maxmem"] == SCRYPT_MAXMEM


def test_wrong_missing_and_malformed_credentials_match(tmp_path, monkeypatch) -> None:
    calls: list[int] = []
    real = hashlib.scrypt

    def recorded(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    clock = ManualClock(CLOCK_START)
    with boot(tmp_path / "wrong", clock, password=PASSWORD, instance_id="wrong", port=8801) as wrong:
        with boot(tmp_path / "missing", clock, instance_id="missing", port=8802) as missing:
            with boot(tmp_path / "bad", clock, password=PASSWORD, instance_id="bad", port=8803) as malformed:
                monkeypatch.setattr(hashlib, "scrypt", recorded)
                rejected = wrong.post_login(OTHER_PASSWORD)
                absent = missing.post_login(PASSWORD)
                (malformed.config.state_directory / "credential").write_bytes(b"not-a-verifier")
                broken = malformed.post_login(PASSWORD)

    assert rejected.status_code == absent.status_code == broken.status_code == 401
    assert rejected.json() == absent.json() == broken.json() == {"error": "authentication_failed"}
    assert calls == [1, 1, 1]
    assert "set-cookie" not in rejected.headers
    assert "set-cookie" not in absent.headers


def test_oversize_password_is_rejected_before_kdf(tmp_path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("KDF ran for an oversize password")

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        monkeypatch.setattr("hopper_files.kdf.derive", forbidden)
        response = world.post_login("é" * 513)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


def test_unicode_password_is_exact(tmp_path) -> None:
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    with boot(tmp_path, ManualClock(CLOCK_START), password=decomposed) as world:
        wrong = world.post_login(composed)
        right = world.post_login(decomposed)

    assert wrong.status_code == 401
    assert right.status_code == 200


def test_nonce_is_required_once_and_csrf_is_bound_to_the_session(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        missing = world.post_login(PASSWORD, nonce="")
        nonce = world.issue_nonce()
        first = world.post_login(PASSWORD, nonce=nonce)
        replay = world.post_login(PASSWORD, nonce=nonce)
        stale = world.client.post(
            world.config.base_path + "logout",
            headers={"origin": world.config.origin, "x-csrf-token": "not-the-token"},
        )
        logout = world.client.post(
            world.config.base_path + "logout",
            headers={"origin": world.config.origin, "x-csrf-token": first.json()["csrfToken"]},
        )
        after = world.client.get(world.config.base_path + "app")

    assert missing.status_code == 403
    assert first.status_code == 200
    assert replay.status_code == 403
    assert stale.status_code == 403
    assert logout.status_code == 200
    assert logout.json() == {"authenticated": False}
    assert after.status_code == 401


def test_authenticated_shell_logout_form_rejects_replayed_session(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        login = world.post_login(PASSWORD)
        cookie = world.client.cookies.get(world.config.cookie_name)
        page = world.client.get(world.config.base_path + "app")
        match = re.search(
            r'<form id="logout-form" method="post" action="([^"]+)">.*?'
            r'name="csrfToken" value="([^"]+)"',
            page.text,
            re.DOTALL,
        )
        assert match is not None
        action, csrf = match.groups()
        csrf_matches_session = csrf == login.json()["csrfToken"]
        logout = world.client.post(
            action,
            data={"csrfToken": csrf},
            headers={"origin": world.config.origin},
            follow_redirects=False,
        )
        replay = world.client.get(
            world.config.base_path + "api/roots",
            headers={"cookie": f"{world.config.cookie_name}={cookie}"},
        )

    assert login.status_code == 200
    assert page.status_code == 200
    assert csrf_matches_session, "logout form did not carry the current session CSRF token"
    assert logout.status_code == 303
    assert logout.headers["location"] == world.config.base_url + "login"
    assert replay.status_code == 401


def test_oversized_logout_body_does_not_revoke_session(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        login = world.post_login(PASSWORD)
        response = world.client.post(
            world.config.base_path + "logout",
            content=b"x" * (auth_api.MAX_BODY_BYTES + 1),
            headers={
                "origin": world.config.origin,
                "x-csrf-token": login.json()["csrfToken"],
                "content-type": "application/json",
            },
        )
        still_authenticated = world.client.get(world.config.base_path + "app")

    assert response.status_code == 413
    assert response.json() == {"error": "limit_exceeded"}
    assert still_authenticated.status_code == 200


def test_oversized_login_returns_limit_response_before_parsing(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        response = world.client.post(
            world.config.base_path + "login",
            content=b"x" * (auth_api.MAX_BODY_BYTES + 1),
            headers={"origin": world.config.origin, "content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"error": "limit_exceeded"}


def test_logout_and_expiry_invalidate_only_that_session(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path / "short", clock, password=PASSWORD, instance_id="short", duration=10, port=8811) as short:
        with boot(tmp_path / "long", clock, password=OTHER_PASSWORD, instance_id="long", duration=100, port=8812) as longer:
            left = short.post_login(PASSWORD)
            right = longer.post_login(OTHER_PASSWORD)
            clock.advance(10)
            expired = short.client.get("/app")
            still = longer.client.get("/app")

    assert left.status_code == 200
    assert right.status_code == 200
    assert expired.status_code == 401
    assert still.status_code == 200


def test_form_login_redirects_only_inside_the_base_url(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        page = world.client.get("/login")
        nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
        response = world.client.post(
            "/login",
            data={"nonce": nonce, "password": PASSWORD, "next": "https://evil.example/phish"},
            headers={"origin": world.config.origin},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == world.config.base_url + "app"
    assert "evil.example" not in response.headers["location"]


def test_entry_points_follow_the_session_state(tmp_path) -> None:
    """A saved `/app` address without a session shows the login page (still 401), and the
    login entry with a live session goes straight to the app instead of asking again."""
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        base = world.config.base_path
        anonymous_app = world.client.get(base + "app")
        anonymous_login = world.client.get(base + "login")
        world.post_login(PASSWORD)
        signed_login = world.client.get(base + "login", follow_redirects=False)
        signed_nonce = world.client.get(base + "login", headers={"accept": "application/json"})

    assert anonymous_app.status_code == 401
    assert anonymous_app.headers["content-type"].startswith("text/html")
    assert 'id="login-form"' in anonymous_app.text and 'name="nonce"' in anonymous_app.text
    assert anonymous_login.status_code == 200 and 'id="login-form"' in anonymous_login.text
    assert signed_login.status_code == 303
    assert signed_login.headers["location"].endswith(base + "app")
    assert signed_nonce.status_code == 200 and "nonce" in signed_nonce.json()


NEW_PASSWORD = "synthetic-new-battery-staple"


def _password_nonce(world) -> str:
    response = world.client.get(world.config.base_path + "password", headers={"accept": "application/json"})
    assert response.status_code == 200, response.text
    return response.json()["nonce"]


def _post_password(world, current: str, new: str, confirmation: str | None = None, nonce: str | None = None):
    return world.client.post(
        world.config.base_path + "password",
        json={
            "nonce": nonce if nonce is not None else _password_nonce(world),
            "current": current,
            "new": new,
            "confirmation": new if confirmation is None else confirmation,
        },
        headers={"origin": world.config.origin},
        follow_redirects=False,
    )


def test_password_change_needs_the_current_password_and_ends_every_session(tmp_path, caplog, monkeypatch) -> None:
    calls: list[int] = []
    real = hashlib.scrypt

    def recorded(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    caplog.set_level(logging.INFO)
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        base = world.config.base_path
        signed = world.post_login(PASSWORD)
        old_cookie = world.client.cookies.get(world.config.cookie_name)
        wrong = _post_password(world, OTHER_PASSWORD, NEW_PASSWORD)
        still_signed = world.client.get(base + "app")
        monkeypatch.setattr(hashlib, "scrypt", recorded)
        changed = _post_password(world, PASSWORD, NEW_PASSWORD)
        monkeypatch.setattr(hashlib, "scrypt", real)
        replayed = world.client.get(base + "app", headers={"cookie": f"{world.config.cookie_name}={old_cookie}"})
        old_login = world.post_login(PASSWORD)
        new_login = world.post_login(NEW_PASSWORD)
        credential = (world.config.state_directory / "credential").read_bytes()

    assert signed.status_code == 200
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "authentication_failed"}
    assert still_signed.status_code == 200, "a refused change must not end the session"
    assert changed.status_code == 200
    assert changed.json() == {"changed": True}
    assert world.config.cookie_name in changed.headers["set-cookie"]
    assert calls == [1, 1], "one KDF checks the current password and one derives the new verifier"
    assert replayed.status_code == 401, "the change ends every session of the instance"
    assert old_login.status_code == 401
    assert new_login.status_code == 200
    for secret in (PASSWORD, NEW_PASSWORD, OTHER_PASSWORD):
        assert secret not in caplog.text
        assert secret.encode() not in credential
    assert "outcome=password_changed" in caplog.text


def test_password_change_rejects_mismatch_empty_and_oversize_before_the_kdf(tmp_path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("KDF ran for a request that must be refused first")

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        monkeypatch.setattr("hopper_files.kdf.derive", forbidden)
        mismatch = _post_password(world, PASSWORD, NEW_PASSWORD, confirmation=NEW_PASSWORD + "x")
        empty = _post_password(world, PASSWORD, "")
        oversize = _post_password(world, PASSWORD, "é" * 513)
        oversize_current = _post_password(world, "é" * 513, NEW_PASSWORD)
        monkeypatch.undo()
        unchanged = world.post_login(PASSWORD)

    assert mismatch.status_code == 400 and mismatch.json() == {"error": "password_mismatch"}
    assert empty.status_code == 400 and empty.json() == {"error": "invalid_request"}
    assert oversize.status_code == 400 and oversize.json() == {"error": "invalid_request"}
    assert oversize_current.status_code == 400 and oversize_current.json() == {"error": "invalid_request"}
    assert unchanged.status_code == 200


def test_password_change_uses_its_nonce_once(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        missing = _post_password(world, PASSWORD, NEW_PASSWORD, nonce="")
        nonce = _password_nonce(world)
        first = _post_password(world, OTHER_PASSWORD, NEW_PASSWORD, nonce=nonce)
        replay = _post_password(world, PASSWORD, NEW_PASSWORD, nonce=nonce)
        unchanged = world.post_login(PASSWORD)

    assert missing.status_code == 403 and missing.json() == {"error": "forbidden"}
    assert first.status_code == 401
    assert replay.status_code == 403, "a used nonce cannot change the password"
    assert unchanged.status_code == 200


def test_password_change_shares_the_login_limits(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        attempts = [_post_password(world, OTHER_PASSWORD, NEW_PASSWORD) for _ in range(5)]
        limited = _post_password(world, PASSWORD, NEW_PASSWORD)
        login = world.post_login(PASSWORD)

    assert [response.status_code for response in attempts] == [401] * 5
    assert limited.status_code == 429
    assert limited.json() == {"error": "too_many_requests"}
    assert int(limited.headers["retry-after"]) > 0
    assert login.status_code == 429, "the change spends the same bucket as the login"


def test_password_page_is_linked_from_login_and_works_without_script(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        base = world.config.base_path
        login_page = world.client.get(base + "login")
        page = world.client.get(base + "password")
        nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
        mismatch = world.client.post(
            base + "password",
            data={"nonce": nonce, "current": PASSWORD, "new": NEW_PASSWORD, "confirmation": "other"},
            headers={"origin": world.config.origin},
        )
        nonce = re.search(r'name="nonce" value="([^"]+)"', mismatch.text).group(1)
        changed = world.client.post(
            base + "password",
            data={"nonce": nonce, "current": PASSWORD, "new": NEW_PASSWORD, "confirmation": NEW_PASSWORD},
            headers={"origin": world.config.origin},
            follow_redirects=False,
        )
        confirmation = world.client.get(changed.headers["location"])
        new_login = world.post_login(NEW_PASSWORD)

    assert login_page.status_code == 200
    assert f'href="{base}password"' in login_page.text
    assert page.status_code == 200 and 'id="password-form"' in page.text
    assert f'href="{base}login"' in page.text
    assert mismatch.status_code == 400 and "não coincidem" in mismatch.text
    assert 'id="password-form"' in mismatch.text
    assert changed.status_code == 303, "the page redirects after the POST, so a reload does not resend it"
    assert changed.headers["location"] == world.config.base_url + "login?senha=trocada"
    assert world.config.cookie_name in changed.headers["set-cookie"]
    assert 'id="login-form"' in confirmation.text and "Senha trocada" in confirmation.text
    assert new_login.status_code == 200


def test_refusals_before_the_kdf_still_spend_the_nonce(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        nonce = _password_nonce(world)
        mismatch = _post_password(world, PASSWORD, NEW_PASSWORD, confirmation="other", nonce=nonce)
        reused = _post_password(world, PASSWORD, NEW_PASSWORD, nonce=nonce)
        login_nonce = world.issue_nonce()
        oversize = world.post_login("é" * 513, nonce=login_nonce)
        login_reused = world.post_login(PASSWORD, nonce=login_nonce)

    assert mismatch.status_code == 400
    assert reused.status_code == 403, "the refused attempt spent the password nonce"
    assert oversize.status_code == 400
    assert login_reused.status_code == 403, "the refused attempt spent the login nonce"


def test_non_ascii_nonce_is_refused_instead_of_failing(tmp_path) -> None:
    # A lone surrogate, escaped in the JSON text as a forged request would send it.
    headers = {"origin": None, "content-type": "application/json"}
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers["origin"] = world.config.origin
        base = world.config.base_path
        login = world.client.post(
            base + "login", headers=headers,
            content=('{"nonce":"\\ud800","password":"' + PASSWORD + '"}').encode("ascii"),
        )
        change = world.client.post(
            base + "password", headers=headers,
            content=('{"nonce":"\\ud800","current":"' + PASSWORD + '","new":"' + NEW_PASSWORD
                     + '","confirmation":"' + NEW_PASSWORD + '"}').encode("ascii"),
        )
        unchanged = world.post_login(PASSWORD)

    assert login.status_code == 403 and login.json() == {"error": "forbidden"}
    assert change.status_code == 403 and change.json() == {"error": "forbidden"}
    assert unchanged.status_code == 200


def test_pages_with_forms_keep_the_origin_on_same_site_posts(tmp_path) -> None:
    """With no-referrer a browser form POST carries Origin: null and the guard refuses it."""
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        base = world.config.base_path
        pages = [world.client.get(base + "login"), world.client.get(base + "password"), world.client.get(base + "app")]

    for page in pages:
        assert page.headers["referrer-policy"] == "same-origin"
