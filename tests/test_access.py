from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hopper_files.clock import ManualClock
from hopper_files.config import ConfigError, host_matches, load_config, origin_matches

from conftest import CLOCK_START, PASSWORD, boot, cookie_flags, write_config


def test_wrong_host_origin_and_port_are_rejected(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        signed = world.post_login(PASSWORD)
        cookie = f"{world.config.cookie_name}={signed.cookies[world.config.cookie_name]}"
        wrong_origin = world.client.post(
            "/login",
            json={"nonce": world.issue_nonce(), "password": PASSWORD},
            headers={"origin": "http://127.0.0.1:9999"},
        )
        other = TestClient(
            world.client.app,
            base_url="http://127.0.0.1:9999",
            client=("127.0.0.1", 40000),
        )
        with other:
            wrong_port = other.get("/app", headers={"cookie": cookie})

    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"error": "forbidden"}
    assert "set-cookie" not in wrong_origin.headers
    assert wrong_port.status_code == 403


def test_local_mode_ignores_forwarded_headers_and_does_not_set_secure(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        response = world.post_login(
            PASSWORD,
            **{
                "x-forwarded-proto": "https",
                "x-forwarded-host": "files.example.test:443",
            },
        )

    assert response.status_code == 200
    assert "secure" not in cookie_flags(response.headers["set-cookie"])
    assert response.json()["authenticated"] is True


def test_remote_mode_requires_the_trusted_proxy_and_secure_cookies(tmp_path) -> None:
    with boot(
        tmp_path,
        ManualClock(CLOCK_START),
        password=PASSWORD,
        mode="remote",
        port=9000,
        external_port=8443,
    ) as world:
        direct = TestClient(
            world.client.app,
            base_url="http://127.0.0.1:9000",
            client=("127.0.0.1", 40000),
        )
        with direct:
            loopback = direct.post(
                "/login",
                json={"nonce": "ignored", "password": PASSWORD},
                headers={"origin": "http://127.0.0.1:9000"},
            )
        downgrade = world.client.post(
            "/login",
            json={"nonce": "ignored", "password": PASSWORD},
            headers={
                "origin": world.config.origin,
                "x-forwarded-proto": "http",
                "x-forwarded-host": world.config.host_header,
            },
        )
        untrusted = TestClient(
            world.client.app,
            base_url=f"http://{world.config.host_header}",
            client=("203.0.113.10", 40000),
            headers={
                "origin": world.config.origin,
                "x-forwarded-proto": "https",
                "x-forwarded-host": world.config.host_header,
            },
        )
        with untrusted:
            spoofed = untrusted.post(
                "/login",
                json={"nonce": "ignored", "password": PASSWORD},
            )
        accepted = world.post_login(PASSWORD)

    assert loopback.status_code == 403
    assert downgrade.status_code == 403
    assert "set-cookie" not in downgrade.headers
    assert spoofed.status_code == 403
    assert accepted.status_code == 200
    flags = cookie_flags(accepted.headers["set-cookie"])
    assert "secure" in flags
    assert not any(flag.startswith("domain=") for flag in flags)


def test_cross_origin_on_another_port_is_rejected(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD, port=8765) as world:
        response = world.client.post(
            "/login",
            json={"nonce": world.issue_nonce(), "password": PASSWORD},
            headers={"origin": "http://127.0.0.1:8766"},
        )
    assert response.status_code == 403


def test_default_ports_are_canonical_and_nondefault_ports_remain(tmp_path) -> None:
    clock = ManualClock(CLOCK_START)
    with boot(
        tmp_path / "https-explicit",
        clock,
        password=PASSWORD,
        mode="remote",
        port=9443,
        base_path="/files/",
        base_url="https://files.example.test:443/files/",
        instance_id="https-explicit",
    ) as explicit:
        assert explicit.config.origin == "https://files.example.test"
        assert explicit.config.host_header == "files.example.test"
        assert explicit.config.base_url == "https://files.example.test/files/"
        assert host_matches("files.example.test", explicit.config)
        assert host_matches("files.example.test:443", explicit.config)
        assert origin_matches("https://files.example.test", explicit.config)
        assert origin_matches("https://files.example.test:443", explicit.config)
        assert not host_matches("files.example.test:8443", explicit.config)
        assert not origin_matches("https://files.example.test:8443", explicit.config)
        accepted = explicit.post_login(
            PASSWORD,
            **{"x-forwarded-host": "files.example.test", "host": "files.example.test"},
        )
        also_explicit = explicit.client.post(
            "/files/login",
            json={"nonce": explicit.issue_nonce(), "password": PASSWORD},
            headers={
                "origin": "https://files.example.test:443",
                "x-forwarded-host": "files.example.test:443",
                "host": "files.example.test:443",
            },
        )
        mismatched_port = explicit.client.post(
            "/files/login",
            json={"nonce": "ignored", "password": PASSWORD},
            headers={
                "origin": "https://files.example.test:8443",
                "x-forwarded-host": "files.example.test:8443",
                "host": "files.example.test:8443",
            },
        )
        page = explicit.client.get("/files/login")
        nonce = page.text.split('name="nonce" value="', 1)[1].split('"', 1)[0]
        form = explicit.client.post(
            "/files/login",
            data={"nonce": nonce, "password": PASSWORD},
            headers={"origin": "https://files.example.test"},
            follow_redirects=False,
        )
        untrusted = TestClient(
            explicit.client.app,
            base_url="http://files.example.test",
            client=("203.0.113.10", 40000),
            headers={
                "origin": "https://files.example.test",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "files.example.test",
            },
        )
        with untrusted:
            spoofed = untrusted.post(
                "/files/login",
                json={"nonce": "ignored", "password": PASSWORD},
            )
    with boot(
        tmp_path / "https-omitted",
        clock,
        mode="remote",
        port=9444,
        base_path="/files/",
        base_url="https://files.example.test/files/",
        instance_id="https-omitted",
    ) as omitted:
        assert omitted.config.host_header == "files.example.test"
    with boot(
        tmp_path / "http80",
        clock,
        password=PASSWORD,
        port=80,
        base_url="http://127.0.0.1/",
        instance_id="http80",
    ) as local:
        assert local.config.origin == "http://127.0.0.1"
        local_login = local.post_login(PASSWORD, **{"host": "127.0.0.1:80"})
        wrong_port = local.client.post(
            "/login",
            json={"nonce": "ignored", "password": PASSWORD},
            headers={"origin": "http://127.0.0.1:81", "host": "127.0.0.1:81"},
        )
    with boot(
        tmp_path / "custom",
        clock,
        mode="remote",
        port=9445,
        base_path="/files/",
        base_url="https://files.example.test:8443/files/",
        instance_id="custom",
    ) as custom:
        missing_port = custom.client.get(
            "/files/login",
            headers={"x-forwarded-host": "files.example.test", "host": "files.example.test"},
        )

    assert accepted.status_code == 200
    assert "path=/files/" in accepted.headers["set-cookie"].lower()
    assert also_explicit.status_code == 200
    assert mismatched_port.status_code == 403
    assert form.status_code == 303
    assert form.headers["location"] == "https://files.example.test/files/app"
    assert ":443" not in form.headers["location"]
    assert spoofed.status_code == 403
    assert omitted.config.effective_port == 443
    assert local_login.status_code == 200
    assert wrong_port.status_code == 403
    assert missing_port.status_code == 403


def test_malformed_origin_is_forbidden_without_an_exception(tmp_path) -> None:
    hostile = (
        "https://files.example.test:not-a-port",
        "https://files.example.test:99999",
        "https://[::gggg]",
    )
    with boot(
        tmp_path / "app",
        ManualClock(CLOCK_START),
        mode="remote",
        port=9555,
        base_path="/files/",
        base_url="https://files.example.test/files/",
        instance_id="origin-guard",
    ) as world:
        for origin in hostile:
            response = world.client.post(
                "/files/login",
                json={"nonce": "ignored", "password": PASSWORD},
                headers={"origin": origin},
            )
            assert response.status_code == 403
            assert response.json() == {"error": "forbidden"}
    for origin in hostile:
        path = write_config(
            tmp_path / origin.encode().hex(),
            mode="remote",
            port=9556,
            base_path="/files/",
            base_url=origin if origin.endswith("/") else origin + "/files/",
            instance_id="bad-base",
        )
        with pytest.raises(ConfigError, match="authority"):
            load_config(path)


def test_non_loopback_listener_is_rejected_before_bind(tmp_path) -> None:
    path = write_config(tmp_path, bind="0.0.0.0")
    with pytest.raises(ConfigError, match="loopback"):
        load_config(path)


def test_base_path_scopes_routes_and_cookies(tmp_path) -> None:
    with boot(
        tmp_path,
        ManualClock(CLOCK_START),
        password=PASSWORD,
        base_path="/files/",
        port=8877,
    ) as world:
        outside = world.client.get("/api/roots")
        inside = world.client.get("/files/api/roots")
        signed = world.post_login(PASSWORD)

    assert outside.status_code == 404
    assert inside.status_code == 401
    assert "path=/files/" in signed.headers["set-cookie"].lower()
