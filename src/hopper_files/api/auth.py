"""Login, logout, password change, and the authenticated shell. File operations are not served."""

from __future__ import annotations

import json
import logging
import secrets
from html import escape
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from hopper_files.config import InstanceConfig
from hopper_files.credentials import (
    PasswordTooLong,
    accept_login,
    change_password_with_current,
    password_byte_length,
)
from hopper_files.kdf import PASSWORD_MAX_BYTES
from hopper_files.limiter import consume_login_attempt
from hopper_files.api.request_body import CONTROL_BODY_MAX_BYTES, BodyTooLarge, read_limited_body
from hopper_files.responses import html_page, json_error, no_store
from hopper_files.sessions import (
    csrf_matches,
    issue_nonce,
    consume_nonce,
    revoke_session,
)
from hopper_files.state import KdfBusy

logger = logging.getLogger("hopper_files.auth")
MAX_BODY_BYTES = CONTROL_BODY_MAX_BYTES
PASSWORD_FIELDS = ("nonce", "current", "new", "confirmation")
PASSWORD_CHANGED = "Senha trocada. Entre com a nova senha."


async def get_login(request: Request) -> Response:
    runtime = request.app.state.runtime
    if not _accepts_json(request) and request.scope["state"].get("session") is not None:
        # With an active session, this route goes straight to the app instead of asking for the password again.
        response = RedirectResponse(runtime.config.base_url + "app", status_code=303)
        no_store(response)
        return response
    nonce = issue_nonce(runtime.config.state_directory, runtime.clock.now())
    if _accepts_json(request):
        return _json(200, {"nonce": nonce})
    # The password-change page redirects back here with the confirmation (HF-AUTH-004).
    changed = request.query_params.get("senha") == "trocada"
    return html_page(200, _login_markup(runtime.config, nonce, PASSWORD_CHANGED if changed else "", neutral=changed))


async def post_login(request: Request) -> Response:
    runtime = request.app.state.runtime
    config = runtime.config
    now = runtime.clock.now()
    try:
        nonce, password, wants_html = await _read_login(request)
    except BodyTooLarge:
        _log(config.instance_id, "login_rejected")
        return json_error(413, "limit_exceeded")
    except ValueError:
        _log(config.instance_id, "login_rejected")
        return _rejected(config, now, wants_html=False, status=400)
    decision = consume_login_attempt(
        config.state_directory,
        request.scope["state"]["network_origin"],
        now,
    )
    if not decision.allowed:
        _log(config.instance_id, "login_limited")
        return _limited(decision.retry_after, wants_html, config, now)
    # Every attempt spends its nonce (HF-AUTH-001), including one refused before the KDF.
    if not consume_nonce(config.state_directory, nonce, now):
        _log(config.instance_id, "login_rejected")
        return _rejected(config, now, wants_html, status=403)
    if password_byte_length(password) > PASSWORD_MAX_BYTES:
        _log(config.instance_id, "login_rejected")
        return _rejected(config, now, wants_html, status=400)
    try:
        established = accept_login(
            config.state_directory,
            instance_id=config.instance_id,
            cookie_lifetime=config.session_duration_seconds,
            password=password,
            now=now,
        )
    except KdfBusy:
        _log(config.instance_id, "login_limited")
        return _limited(1, wants_html, config, now)
    except PasswordTooLong:
        _log(config.instance_id, "login_rejected")
        return _rejected(config, now, wants_html, status=400)
    if established is None:
        _log(config.instance_id, "login_rejected")
        return _rejected(config, now, wants_html, status=401)
    cookie, csrf_token = established
    _log(config.instance_id, "login_accepted")
    if wants_html:
        response = RedirectResponse(config.base_url + "app", status_code=303)
        no_store(response)
    else:
        response = _json(200, {"authenticated": True, "csrfToken": csrf_token})
    _set_cookie(response, config, cookie)
    return response


async def get_password(request: Request) -> Response:
    """Password change page, reached from the login page. It needs no session."""
    runtime = request.app.state.runtime
    nonce = issue_nonce(runtime.config.state_directory, runtime.clock.now())
    if _accepts_json(request):
        return _json(200, {"nonce": nonce})
    return html_page(200, _password_markup(runtime.config, nonce, ""))


async def post_password(request: Request) -> Response:
    """Change the password with the current one (HF-AUTH-004), under the login limits."""
    runtime = request.app.state.runtime
    config = runtime.config
    now = runtime.clock.now()
    try:
        nonce, current, new, confirmation, wants_html = await _read_password_change(request)
    except BodyTooLarge:
        _log(config.instance_id, "password_rejected")
        return json_error(413, "limit_exceeded")
    except ValueError:
        _log(config.instance_id, "password_rejected")
        return json_error(400, "invalid_request")
    # Checking the current password is a login attempt: same buckets, before any KDF.
    decision = consume_login_attempt(
        config.state_directory,
        request.scope["state"]["network_origin"],
        now,
    )
    if not decision.allowed:
        _log(config.instance_id, "password_limited")
        return _password_limited(decision.retry_after, wants_html, config, now)
    # Every attempt spends its nonce (HF-AUTH-001), including one refused before the KDF.
    if not consume_nonce(config.state_directory, nonce, now):
        _log(config.instance_id, "password_rejected")
        return _password_rejected(config, now, wants_html, 403, "forbidden", "Não foi possível trocar a senha. Tente de novo.")
    try:
        oversized = any(password_byte_length(value) > PASSWORD_MAX_BYTES for value in (current, new, confirmation))
    except PasswordTooLong:
        oversized = True
    if oversized or not new:
        _log(config.instance_id, "password_rejected")
        text = "A senha excede o limite." if oversized else "Digite a nova senha."
        return _password_rejected(config, now, wants_html, 400, "invalid_request", text)
    if new != confirmation:
        _log(config.instance_id, "password_rejected")
        return _password_rejected(
            config, now, wants_html, 400, "password_mismatch", "A nova senha e a confirmação não coincidem."
        )
    try:
        epoch = change_password_with_current(
            config.state_directory,
            instance_id=config.instance_id,
            current=current,
            new=new,
        )
    except KdfBusy:
        _log(config.instance_id, "password_limited")
        return _password_limited(1, wants_html, config, now)
    except PasswordTooLong:
        _log(config.instance_id, "password_rejected")
        return _password_rejected(config, now, wants_html, 400, "invalid_request", "A senha excede o limite.")
    if epoch is None:
        _log(config.instance_id, "password_rejected")
        return _password_rejected(config, now, wants_html, 401, "authentication_failed", "Senha atual incorreta.")
    _log(config.instance_id, "password_changed")
    if wants_html:
        # Redirect after the POST, as the login does: a reload does not resend the form.
        response = RedirectResponse(config.base_url + "login?senha=trocada", status_code=303)
        no_store(response)
    else:
        response = _json(200, {"changed": True})
    # Every session of this instance ended with the change, this browser's included.
    _clear_cookie(response, config)
    return response


async def post_logout(request: Request) -> Response:
    runtime = request.app.state.runtime
    config = runtime.config
    session = request.scope["state"].get("session")
    wants_html = _is_form(request)
    if session is None:
        _log(config.instance_id, "logout_rejected")
        return json_error(401, "authentication_required")
    try:
        presented = await _presented_csrf(request)
    except BodyTooLarge:
        _log(config.instance_id, "logout_rejected")
        return json_error(413, "limit_exceeded")
    if not csrf_matches(session, presented):
        _log(config.instance_id, "logout_rejected")
        return json_error(403, "forbidden")
    revoke_session(session)
    _log(config.instance_id, "logout")
    if wants_html:
        response = RedirectResponse(config.base_url + "login", status_code=303)
        no_store(response)
    else:
        response = _json(200, {"authenticated": False})
    _clear_cookie(response, config)
    return response


async def get_app(request: Request) -> Response:
    runtime = request.app.state.runtime
    session = request.scope["state"].get("session")
    if session is None:
        # A bookmarked address or an ended session: the page itself offers the login form (status 401).
        login_nonce = issue_nonce(runtime.config.state_directory, runtime.clock.now())
        return html_page(401, _login_markup(runtime.config, login_nonce, "Entre para continuar.", neutral=True))
    nonce = secrets.token_urlsafe(18)
    return html_page(
        200,
        _app_markup(runtime.config, session.csrf_token, nonce),
        **{"Content-Security-Policy": _app_csp(nonce)},
    )


def _limited(retry_after: int, wants_html: bool, config: InstanceConfig, now: float) -> Response:
    if wants_html:
        nonce = issue_nonce(config.state_directory, now)
        response = html_page(
            429,
            _login_markup(config, nonce, f"Muitas tentativas. Aguarde {_wait_text(retry_after)} antes de tentar de novo."),
            **{"Retry-After": str(retry_after)},
        )
        return response
    return json_error(429, "too_many_requests", **{"Retry-After": str(retry_after)})


def _password_limited(retry_after: int, wants_html: bool, config: InstanceConfig, now: float) -> Response:
    if wants_html:
        nonce = issue_nonce(config.state_directory, now)
        text = f"Muitas tentativas. Aguarde {_wait_text(retry_after)} antes de tentar de novo."
        return html_page(429, _password_markup(config, nonce, text), **{"Retry-After": str(retry_after)})
    return json_error(429, "too_many_requests", **{"Retry-After": str(retry_after)})


def _password_rejected(config: InstanceConfig, now: float, wants_html: bool, status: int, code: str, text: str) -> Response:
    if wants_html:
        nonce = issue_nonce(config.state_directory, now)
        return html_page(status, _password_markup(config, nonce, text))
    return json_error(status, code)


def _wait_text(seconds: int) -> str:
    if seconds <= 60:
        return "1 segundo" if seconds <= 1 else f"{seconds} segundos"
    minutes = -(-seconds // 60)
    return "1 minuto" if minutes == 1 else f"{minutes} minutos"


def _rejected(config: InstanceConfig, now: float, wants_html: bool, status: int) -> Response:
    if wants_html:
        nonce = issue_nonce(config.state_directory, now)
        text = "A senha excede o limite." if status == 400 else "Não foi possível entrar."
        return html_page(status, _login_markup(config, nonce, text))
    code = {400: "invalid_request", 401: "authentication_failed", 403: "forbidden"}[status]
    return json_error(status, code)


def _json(status: int, payload: dict[str, object]) -> Response:
    from hopper_files.responses import SECURITY_HEADERS

    return Response(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        status_code=status,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )


async def _read_login(request: Request) -> tuple[str, str, bool]:
    body = await read_limited_body(request, MAX_BODY_BYTES)
    content_type = _content_type(request)
    if content_type == "application/json":
        nonce, password = _json_fields(body, required={"nonce", "password"})
        return nonce, password, False
    if content_type == "application/x-www-form-urlencoded":
        fields = _form_fields(body)
        if "nonce" not in fields or "password" not in fields:
            raise ValueError("login fields are missing")
        return fields["nonce"], fields["password"], True
    raise ValueError("unsupported login content type")


async def _read_password_change(request: Request) -> tuple[str, str, str, str, bool]:
    body = await read_limited_body(request, MAX_BODY_BYTES)
    content_type = _content_type(request)
    if content_type == "application/json":
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("password JSON is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != set(PASSWORD_FIELDS):
            raise ValueError("password JSON fields are invalid")
        values = tuple(payload[key] for key in PASSWORD_FIELDS)
        if not all(isinstance(value, str) for value in values):
            raise ValueError("password JSON fields are invalid")
        return (*values, False)
    if content_type == "application/x-www-form-urlencoded":
        fields = _form_fields(body)
        if not set(PASSWORD_FIELDS) <= set(fields):
            raise ValueError("password form fields are missing")
        return (*(fields[key] for key in PASSWORD_FIELDS), True)
    raise ValueError("unsupported password content type")


async def _presented_csrf(request: Request) -> str:
    header = request.headers.get("x-csrf-token", "")
    content_type = _content_type(request)
    body_token = ""
    if content_type == "application/json":
        body = await read_limited_body(request, MAX_BODY_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict) or set(payload) - {"csrfToken"}:
            return ""
        value = payload.get("csrfToken", "")
        if not isinstance(value, str):
            return ""
        body_token = value
    elif content_type == "application/x-www-form-urlencoded":
        body = await read_limited_body(request, MAX_BODY_BYTES)
        try:
            fields = _form_fields(body)
        except ValueError:
            return ""
        body_token = fields.get("csrfToken", "")
    if header and body_token and header != body_token:
        return ""
    return header or body_token


def _json_fields(body: bytes, *, required: set[str]) -> tuple[str, str]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("login JSON is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("login JSON fields are invalid")
    nonce = payload["nonce"]
    password = payload["password"]
    if not isinstance(nonce, str) or not isinstance(password, str):
        raise ValueError("login JSON fields are invalid")
    return nonce, password


def _form_fields(body: bytes) -> dict[str, str]:
    try:
        text = body.decode("utf-8")
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True, separator="&")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("login form is invalid") from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("duplicate login field")
        fields[key] = value
    return fields


def _set_cookie(response: Response, config: InstanceConfig, value: str) -> None:
    response.set_cookie(
        key=config.cookie_name,
        value=value,
        max_age=config.session_duration_seconds,
        path=config.base_path,
        secure=config.access_mode == "remote",
        httponly=True,
        samesite="strict",
    )


def _clear_cookie(response: Response, config: InstanceConfig) -> None:
    response.delete_cookie(
        key=config.cookie_name,
        path=config.base_path,
        secure=config.access_mode == "remote",
        httponly=True,
        samesite="strict",
    )


def _login_markup(config: InstanceConfig, nonce: str, message: str, *, neutral: bool = False) -> str:
    # An invitation to log in is not an error: it renders in the neutral color, without an alert style.
    kind = 'class="login-aviso" role="status"' if neutral else 'class="login-erro" role="alert"'
    notice = f"<p {kind}>{escape(message)}</p>" if message else ""
    return _page(
        "Entrar — Hopper Files",
        "anonymous",
        f"""
        <section class="login-cartao" aria-labelledby="login-titulo">
          <h1 id="login-titulo">Hopper Files</h1>
          <p class="login-sub">Informe a senha desta instância para navegar pelos arquivos, buscar conteúdo e editar suas notas.</p>
          <form id="login-form" method="post" action="{escape(config.base_path)}login" data-base-path="{escape(config.base_path)}">
            <input type="hidden" name="nonce" value="{escape(nonce)}">
            <label class="login-rotulo" for="password">Senha</label>
            <input id="password" name="password" type="password" autocomplete="current-password" placeholder="Senha" required autofocus>
            <button type="submit">Entrar</button>
          </form>
          {notice}
          <p id="login-status" class="login-status" role="status" aria-live="polite"></p>
          <p class="login-trocar"><a href="{escape(config.base_path)}password">Trocar senha</a></p>
        </section>
        """,
        config,
        include_script=True,
    )


def _password_markup(config: InstanceConfig, nonce: str, message: str) -> str:
    notice = f'<p class="login-erro" role="alert">{escape(message)}</p>' if message else ""
    return _page(
        "Trocar senha — Hopper Files",
        "anonymous",
        f"""
        <section class="login-cartao" aria-labelledby="senha-titulo">
          <h1 id="senha-titulo">Trocar senha</h1>
          <p class="login-sub">Digite a senha atual e a nova duas vezes. Depois da troca, as sessões abertas desta instância se encerram, e você entra de novo com a senha nova.</p>
          <form id="password-form" method="post" action="{escape(config.base_path)}password" data-base-path="{escape(config.base_path)}">
            <input type="hidden" name="nonce" value="{escape(nonce)}">
            <label class="login-rotulo" for="current">Senha atual</label>
            <input id="current" name="current" type="password" autocomplete="current-password" placeholder="Senha atual" required autofocus>
            <label class="login-rotulo" for="new">Nova senha</label>
            <input id="new" name="new" type="password" autocomplete="new-password" placeholder="Nova senha" required>
            <label class="login-rotulo" for="confirmation">Confirme a nova senha</label>
            <input id="confirmation" name="confirmation" type="password" autocomplete="new-password" placeholder="Confirme a nova senha" required>
            <button type="submit">Trocar senha</button>
          </form>
          {notice}
          <p id="login-status" class="login-status" role="status" aria-live="polite"></p>
          <p class="login-trocar"><a href="{escape(config.base_path)}login">Voltar para entrar</a></p>
        </section>
        """,
        config,
        include_script=True,
    )


def _app_markup(config: InstanceConfig, csrf_token: str, style_nonce: str) -> str:
    return _page(
        "Hopper Files",
        "authenticated",
        f'''<div id="hf-app" data-csrf="{escape(csrf_token)}" data-base-path="{escape(config.base_path)}" data-style-nonce="{escape(style_nonce)}" data-time-zone="{escape(config.time_zone)}">
          <div class="layout" id="app-layout">
            <aside id="sidebar" aria-label="Navegação">
              <div class="side-fixo">
              <div class="side-topo">
                <h1 class="side-title" id="side-title">HOPPER FILES</h1>
                <button class="acao so-icone side-tema tip-dir" id="btn-config" type="button" data-tip="Configurações" aria-label="Configurações">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33 2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83A1.65 1.65 0 0 0 4.6 15 1.65 1.65 0 0 0 3.09 14H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82 2 2 0 0 1 2.83-2.83A1.65 1.65 0 0 0 9 4.6 1.65 1.65 0 0 0 10 3.09V3a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33 2 2 0 0 1 2.83 2.83A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09A1.65 1.65 0 0 0 19.4 15z"/></svg>
                </button>
                <button class="acao so-icone side-tema tip-dir" id="theme-choice" type="button" data-tip="Tema claro/escuro" aria-label="Tema claro/escuro">
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M13.2 9.4A5.2 5.2 0 0 1 6.6 2.8 5.4 5.4 0 1 0 13.2 9.4z"/></svg>
                </button>
                <form id="logout-form" method="post" action="{escape(config.base_path)}logout">
                  <input type="hidden" name="csrfToken" value="{escape(csrf_token)}">
                  <button type="submit" hidden>Sair</button>
                </form>
              </div>
              <form id="sidebar-search" class="busca-wrap" aria-label="Busca"></form>
              </div>
              <div id="favoritos"></div>
              <div class="sec-titulo sec-recolhe" data-sec="tree" id="tree-heading"><span class="sec-chev"></span><span class="sec-rotulo" title="Arquivos &amp; Pastas">Arquivos &amp; Pastas</span><span class="sec-ferramentas">
                <span class="sf-sel tip-cima" data-tip="Agrupar"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><rect x="2.5" y="3" width="11" height="4" rx="1.2"/><rect x="2.5" y="9" width="11" height="4" rx="1.2"/></svg><select id="arv-agrupar" aria-label="Agrupar árvore"><option value="none">Nenhum</option><option value="type">Tipo</option></select></span>
                <span class="sf-sel tip-cima" data-tip="Ordenar"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M3 4.5h10M3 8h7M3 11.5h4"/></svg><select id="arv-ordenar" aria-label="Ordenar árvore"><option value="name">Nome</option><option value="type">Tipo</option></select></span>
                <button class="lb-dir tip-cima" id="arv-dir" type="button" aria-label="Inverter direção da árvore" data-tip="Inverter direção">↑</button>
              </span></div>
              <div id="tree"></div>
              <button class="node-row side-lixeira" id="btn-lixeira" type="button" aria-label="Lixeira"><span class="chev vazio"></span><svg class="icone" width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M3 4.5h10M6.2 4.5V3.2A1 1 0 0 1 7.2 2.2h1.6a1 1 0 0 1 1 1v1.3M4.3 4.5l.6 8a1.2 1.2 0 0 0 1.2 1.1h3.8a1.2 1.2 0 0 0 1.2-1.1l.6-8"/></svg><span class="nome">Lixeira</span></button>
              <div id="favoritos-soltar" aria-hidden="true"></div>
              <div id="tags-nav"></div><div id="etiquetas"></div>
            </aside>
            <div id="sidebar-splitter" class="side-resizer" role="separator" aria-label="Ajustar largura da navegação" aria-orientation="vertical" aria-valuemin="200" aria-valuemax="420" aria-valuenow="250" tabindex="0"></div>
            <main aria-label="Área de arquivos">
              <div class="abas-barra" id="tabs" role="group" aria-label="Abas abertas"></div>
              <div class="topo">
                <button class="acao so-icone" id="sidebar-toggle" type="button" aria-expanded="false" aria-controls="sidebar" aria-label="Mostrar/ocultar barra lateral" data-tip="Mostrar/ocultar barra lateral"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/></svg></button>
                <button class="acao so-icone" id="btn-inicio" type="button" aria-label="Início" data-tip="Início"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M2.5 7.5L8 2.8l5.5 4.7M3.8 6.6V13a.6.6 0 0 0 .6.6h7.2a.6.6 0 0 0 .6-.6V6.6"/></svg></button>
                <div class="acoes" id="toolbar">
                  <div id="toolbar-fixed" class="toolbar-fixed"></div>
                  <div id="document-mode-slot" class="document-mode-slot" aria-label="Modos do documento" hidden></div>
                  <div id="toolbar-actions" class="toolbar-actions"></div>
                </div>
              </div>
              <div class="trilha-barra"><nav class="migalhas" id="breadcrumbs" aria-label="Local atual"></nav></div>
              <div class="lista-barra" id="lista-barra" hidden>
                <span class="sf-sel tip-cima" data-tip="Agrupar"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><rect x="2.5" y="3" width="11" height="4" rx="1.2"/><rect x="2.5" y="9" width="11" height="4" rx="1.2"/></svg><select id="grouping-choice" aria-label="Agrupar"><option value="none">Nenhum</option><option value="type">Tipo</option></select></span>
                <span class="sf-sel tip-cima" data-tip="Ordenar"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M3 4.5h10M3 8h7M3 11.5h4"/></svg><select id="ordering-choice" aria-label="Ordenar"><option value="name">Nome</option><option value="type">Tipo</option><option value="size">Tamanho</option><option value="created">Criado</option><option value="modified">Modificado</option></select></span>
                <button class="lb-dir tip-cima tip-dir" id="sort-direction" type="button" aria-label="Inverter direção" data-tip="Inverter direção">↑</button>
              </div>
              <div class="conteudo" id="results"></div>
              <footer class="status-bar" id="status-bar"><span class="sb-info" id="app-status" role="status" aria-live="polite">Sessão iniciada; carregando sua instância.</span><button class="sb-icone tip-cima tip-dir" id="density-choice" type="button" aria-label="Alternar densidade" data-tip="Densidade compacta"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M2.5 4h11M2.5 8h11M2.5 12h11"/></svg></button></footer>
              <div class="drop-alvo" id="drop-alvo"><div class="drop-caixa">Soltar para adicionar</div></div>
            </main>
            <div class="split-pane" id="split-pane" hidden><div class="split-divisor" id="workspace-splitter" role="separator" aria-label="Ajustar painéis" aria-orientation="vertical" aria-valuemin="30" aria-valuemax="70" aria-valuenow="46" tabindex="0"></div><div id="split-content"></div></div>
            <aside class="info" id="painel-info"><div class="info-inner" id="info-inner"></div></aside>
          </div>
          <div id="menu-ctx" class="menu-ctx" hidden></div><div id="modal-host"></div><div id="toast" role="status" aria-live="polite"></div>
          <dialog id="operation-dialog" aria-labelledby="operation-dialog-title"></dialog>
        </div>''',
        config,
        include_script=True,
    )


# Tab icon: a light folder over a rounded dark square, inlined as a data: URI since the content
# policy already allows data: images.
_FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%231c1c1e'/%3E"
    "%3Cpath d='M7 11a2.5 2.5 0 0 1 2.5-2.5h4.2a2 2 0 0 1 1.6.8l1 1.2h6.2A2.5 2.5 0 0 1 25 13v8.5A2.5 2.5 0 0 1 22.5 24h-13A2.5 2.5 0 0 1 7 21.5z' fill='%23e8f1fb'/%3E"
    "%3C/svg%3E"
)


def _page(title: str, session: str, body: str, config: InstanceConfig, *, include_script: bool) -> str:
    script = f'<script type="module" src="{escape(config.base_path)}app.js"></script>' if include_script else ""
    page_body = body if session == "authenticated" else f'<main class="login">{body}</main>'
    return f"""<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>{escape(title)}</title>
    <link rel="icon" href="{_FAVICON}">
    <link rel="stylesheet" href="{escape(config.base_path)}app.css">
  </head>
  <body data-session="{session}">
    {page_body}
    {script}
  </body>
</html>
"""


def _app_csp(nonce: str) -> str:
    return (
        "default-src 'none'; style-src 'self' 'nonce-" + nonce + "'; script-src 'self'; connect-src 'self'; "
        "img-src 'self' data: blob:; worker-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _accepts_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _is_form(request: Request) -> bool:
    return _content_type(request) == "application/x-www-form-urlencoded"


def _log(instance_id: str, outcome: str) -> None:
    logger.info("auth_outcome instance=%s outcome=%s", instance_id, outcome)
