from __future__ import annotations

from hopper_files.clock import ManualClock

from conftest import CLOCK_START, boot


def test_health_is_data_free_and_login_page_is_portuguese(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START)) as world:
        health = world.client.get(world.config.base_path + "healthz")
        page = world.client.get(world.login_path())

    assert health.status_code == 200
    assert health.json() == {"service": "hopper-files", "status": "up"}
    assert page.status_code == 200
    assert "lang=\"pt-BR\"" in page.text
    assert "Informe a senha desta instância" in page.text
    assert "navegar pelos arquivos" in page.text
    assert "buscar conteúdo" in page.text
    assert 'id="login-form"' in page.text
    assert "name=\"nonce\"" in page.text


def test_unauthenticated_product_routes_return_no_documents(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START)) as world:
        denied = world.client.get("/api/roots")
        options = world.client.options("/api/file")

    assert denied.status_code == 401
    assert denied.json() == {"error": "authentication_required"}
    assert "root" not in denied.text
    assert options.status_code == 401
    assert options.headers.get("access-control-allow-origin") is None
    assert denied.headers.get("access-control-allow-origin") is None


def test_authenticated_editor_route_requires_a_version_precondition(tmp_path) -> None:
    from conftest import PASSWORD

    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        signed = world.post_login(PASSWORD)
        csrf = signed.json()["csrfToken"]
        without = world.client.put(
            "/api/file",
            headers={"origin": world.config.origin},
        )
        with_token = world.client.put(
            "/api/file",
            json={"content": "novo"},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )

    assert signed.status_code == 200
    assert without.status_code == 403
    assert with_token.status_code == 428
    assert with_token.json() == {"error": "precondition_required"}
