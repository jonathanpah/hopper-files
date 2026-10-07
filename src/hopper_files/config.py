"""Explicit per-instance configuration. Nothing here is site-specific."""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_SESSION_SECONDS = 366 * 24 * 60 * 60
TOP_LEVEL_FIELDS = {
    "version",
    "instanceId",
    "serviceAccount",
    "timeZone",
    "sessionDurationSeconds",
    "stateDirectory",
    "cookieName",
    "access",
}
CONFIG_VERSION = 2
# Version 1 fields of the root-based model. Their presence in a version 2
# document is an error; a version 1 document needs the HF-NAV-012 migration.
RETIRED_FIELDS = {"roots", "system", "protectedPaths", "creationPolicies"}
ACCESS_FIELDS = {"mode", "bindHost", "bindPort", "baseUrl", "trustedProxyPeer"}


class ConfigError(ValueError):
    """The instance configuration is absent or fails closed."""


@dataclass(frozen=True)
class InstanceConfig:
    version: int
    instance_id: str
    service_account: str
    time_zone: str
    session_duration_seconds: int
    state_directory: Path
    cookie_name: str
    access_mode: str
    bind_host: str
    bind_port: int
    base_url: str
    base_path: str
    origin: str
    host_header: str
    scheme: str
    hostname: str
    effective_port: int
    trusted_proxy_peer: str | None
    source_path: Path


def require_loopback(host: str) -> None:
    """Accept only a numeric loopback address, never a public or wildcard bind."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError("listener must be an explicit loopback address") from exc
    if not address.is_loopback:
        raise ConfigError("listener must be an explicit loopback address")


def load_config(path: Path) -> InstanceConfig:
    if not path.is_file() or path.is_symlink():
        raise ConfigError("instance config must be a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("instance config is unreadable") from exc
    if isinstance(payload, dict) and payload.get("version") == 1 and not isinstance(payload.get("version"), bool):
        raise ConfigError(
            "instance config is version 1 (root-based); run the HF-NAV-012 state migration "
            "with the administrative installer before serving it with this release"
        )
    if isinstance(payload, dict) and set(payload) & RETIRED_FIELDS:
        retired = ", ".join(sorted(set(payload) & RETIRED_FIELDS))
        raise ConfigError(f"instance config field is retired by the single filesystem base: {retired}")
    if not isinstance(payload, dict) or set(payload) != TOP_LEVEL_FIELDS:
        raise ConfigError("instance config fields are invalid")
    if payload["version"] != CONFIG_VERSION or isinstance(payload["version"], bool):
        raise ConfigError("unsupported instance config version")
    instance_id = _text(payload["instanceId"], "instance id")
    if INSTANCE_ID.fullmatch(instance_id) is None:
        raise ConfigError("instance id is invalid")
    service_account = _text(payload["serviceAccount"], "service account")
    if service_account == "" or "/" in service_account or "\x00" in service_account:
        raise ConfigError("service account is invalid")
    time_zone = _text(payload["timeZone"], "time zone")
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("time zone is not an IANA identifier") from exc
    duration = payload["sessionDurationSeconds"]
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ConfigError("session duration is invalid")
    if duration < 1 or duration > MAX_SESSION_SECONDS:
        raise ConfigError("session duration is invalid")
    state_text = _text(payload["stateDirectory"], "state directory")
    state_directory = Path(state_text)
    if not state_directory.is_absolute():
        raise ConfigError("state directory must be absolute")
    cookie_name = _text(payload["cookieName"], "cookie name")
    if COOKIE_NAME.fullmatch(cookie_name) is None:
        raise ConfigError("cookie name is invalid")
    access = payload["access"]
    if not isinstance(access, dict) or set(access) != ACCESS_FIELDS:
        raise ConfigError("access configuration is invalid")
    mode = access["mode"]
    if mode not in {"local", "remote"}:
        raise ConfigError("access mode must be local or remote")
    bind_host = _text(access["bindHost"], "bind host")
    require_loopback(bind_host)
    bind_port = access["bindPort"]
    if isinstance(bind_port, bool) or not isinstance(bind_port, int):
        raise ConfigError("bind port is invalid")
    if bind_port < 1 or bind_port > 65535:
        raise ConfigError("bind port is invalid")
    base_url = _text(access["baseUrl"], "base URL")
    canonical_url, origin, host_header, base_path, scheme, hostname, effective_port = _parse_base_url(base_url)
    proxy = access["trustedProxyPeer"]
    if mode == "local":
        if proxy is not None:
            raise ConfigError("local mode cannot trust a proxy")
        if scheme != "http":
            raise ConfigError("local mode requires an HTTP base URL")
        if not _same_listener(hostname, effective_port, bind_host, bind_port):
            raise ConfigError("local base URL must match the loopback listener")
        trusted = None
    else:
        if not isinstance(proxy, str):
            raise ConfigError("remote mode requires one trusted proxy peer")
        try:
            trusted = str(ipaddress.ip_address(proxy))
        except ValueError as exc:
            raise ConfigError("trusted proxy peer must be an IP address") from exc
        if scheme != "https":
            raise ConfigError("remote mode requires an HTTPS base URL")
    return InstanceConfig(
        version=CONFIG_VERSION,
        instance_id=instance_id,
        service_account=service_account,
        time_zone=time_zone,
        session_duration_seconds=duration,
        state_directory=state_directory,
        cookie_name=cookie_name,
        access_mode=mode,
        bind_host=bind_host,
        bind_port=bind_port,
        base_url=canonical_url,
        base_path=base_path,
        origin=origin,
        host_header=host_header,
        scheme=scheme,
        hostname=hostname,
        effective_port=effective_port,
        trusted_proxy_peer=trusted,
        source_path=path,
    )


def same_service_identity(left: InstanceConfig, right: InstanceConfig) -> bool:
    """True when both configs are the same instance, state, access, and account.

    A different instance, state directory, access
    mode, or service account must not be treated as a reload of this process.
    """
    return (
        left.version == right.version
        and left.instance_id == right.instance_id
        and left.service_account == right.service_account
        and os.path.abspath(left.state_directory) == os.path.abspath(right.state_directory)
        and left.cookie_name == right.cookie_name
        and left.access_mode == right.access_mode
        and left.bind_host == right.bind_host
        and left.bind_port == right.bind_port
        and left.base_url == right.base_url
        and left.trusted_proxy_peer == right.trusted_proxy_peer
    )


def load_service_config(path: Path) -> InstanceConfig:
    """Load the configuration a running process is allowed to serve."""
    return load_config(path)


def require_service_account(config: InstanceConfig) -> None:
    """Require an existing account and a process already running as that UID."""
    try:
        account = pwd.getpwnam(config.service_account)
    except KeyError as exc:
        raise ConfigError("service account does not exist") from exc
    if account.pw_uid != os.geteuid():
        raise ConfigError("process identity does not match the service account")


def validate_service_identity(config: InstanceConfig) -> None:
    """Prove the service UID can use its state directory.

    This never creates an account and never changes document permissions.
    """
    require_service_account(config)
    state_directory = config.state_directory
    if not state_directory.is_dir():
        raise ConfigError("state directory is not accessible to the service account")
    probe_name = f".access.{secrets.token_hex(4)}"
    probe = state_directory / probe_name
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except OSError as exc:
        raise ConfigError("state directory is not writable by the service account") from exc
    finally:
        try:
            os.unlink(probe)
        except FileNotFoundError:
            pass


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} is invalid")
    return value


def host_matches(value: str | None, config: InstanceConfig) -> bool:
    """True when a Host header is the configured authority, default port omitted."""
    parsed = _split_host(value, config.scheme)
    if parsed is None:
        return False
    hostname, port = parsed
    return _hosts_equal(hostname, config.hostname) and port == config.effective_port


def origin_matches(value: str | None, config: InstanceConfig) -> bool:
    """True when Origin is the configured scheme, host, and effective port.

    A malformed authority is not a match. It must not escape as an exception.
    """
    if value is None:
        return False
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != config.scheme
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            return False
        hostname, port = _read_authority(parts)
    except ValueError:
        return False
    return _hosts_equal(hostname, config.hostname) and port == config.effective_port


def _parse_base_url(value: str) -> tuple[str, str, str, str, str, str, int]:
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ConfigError("base URL authority is invalid") from exc
    if parts.scheme not in {"http", "https"}:
        raise ConfigError("base URL scheme is invalid")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("base URL must contain only scheme, host, port, and path")
    path = parts.path
    if not path.startswith("/") or not path.endswith("/"):
        raise ConfigError("base path must start and end with /")
    components = path.split("/")
    if any(part in {"", ".", ".."} for part in components[1:-1]):
        raise ConfigError("base path is not canonical")
    try:
        hostname, port = _read_authority(parts)
    except ValueError as exc:
        raise ConfigError("base URL authority is invalid") from exc
    host_header = _format_host(hostname, port, parts.scheme)
    origin = f"{parts.scheme}://{host_header}"
    return origin + path, origin, host_header, path, parts.scheme, hostname, port


def _read_authority(parts: object) -> tuple[str, int]:
    """Return a canonical hostname and effective port, or raise ValueError."""
    scheme = str(getattr(parts, "scheme", ""))
    hostname = parts.hostname
    if hostname is None or scheme not in {"http", "https"}:
        raise ValueError("host")
    port = parts.port if parts.port is not None else _default_port(scheme)
    if port < 1 or port > 65535:
        raise ValueError("port")
    return _canonical_hostname(hostname), port


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _canonical_hostname(hostname: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname.lower()
    if address.version == 6:
        return address.compressed
    return str(address)


def _format_host(hostname: str, port: int, scheme: str) -> str:
    shown = f"[{hostname}]" if ":" in hostname else hostname
    if port == _default_port(scheme):
        return shown
    return f"{shown}:{port}"


def _split_host(value: str | None, scheme: str) -> tuple[str, int] | None:
    if value is None or value == "" or any(character in value for character in " \t\r\n/\\?#@"):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            return None
        hostname = value[1:end]
        rest = value[end + 1 :]
        if rest == "":
            port = _default_port(scheme)
        elif rest.startswith(":") and rest[1:].isdigit() and 1 <= int(rest[1:]) <= 65535:
            port = int(rest[1:])
        else:
            return None
    else:
        host, separator, port_text = value.rpartition(":")
        if separator == "":
            hostname = value
            port = _default_port(scheme)
        elif port_text.isdigit() and 1 <= int(port_text) <= 65535 and host != "":
            hostname = host
            port = int(port_text)
        else:
            return None
    return _canonical_hostname(hostname), port


def _hosts_equal(left: str, right: str) -> bool:
    try:
        return ipaddress.ip_address(left) == ipaddress.ip_address(right)
    except ValueError:
        return left.lower() == right.lower()


def _same_listener(hostname: str, port: int, bind_host: str, bind_port: int) -> bool:
    try:
        return ipaddress.ip_address(hostname) == ipaddress.ip_address(bind_host) and port == bind_port
    except ValueError:
        return False
