"""One access mode per instance, with exact host, origin, and proxy trust."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from hopper_files.config import InstanceConfig, host_matches, origin_matches

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    network_origin: str | None


def evaluate_access(scope: dict[str, object], config: InstanceConfig) -> AccessDecision:
    """Decide whether this request matches the single configured entry point.

    Forwarded headers are ignored unless the socket peer is the configured
    trusted proxy. A direct loopback request in remote mode therefore cannot
    become an authorized local browser session.
    """
    peer = _peer_ip(scope)
    if peer is None:
        return AccessDecision(False, None)
    headers, duplicated = _headers(scope)
    if duplicated & {"host", "origin", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-for"}:
        return AccessDecision(False, None)
    if config.access_mode == "remote":
        if peer != config.trusted_proxy_peer:
            return AccessDecision(False, None)
        if headers.get("x-forwarded-proto") != "https":
            return AccessDecision(False, None)
        if not host_matches(headers.get("x-forwarded-host"), config):
            return AccessDecision(False, None)
        if not host_matches(headers.get("host"), config):
            return AccessDecision(False, None)
    else:
        if scope.get("scheme") != "http":
            return AccessDecision(False, None)
        if not host_matches(headers.get("host"), config):
            return AccessDecision(False, None)
    origin = headers.get("origin")
    method = str(scope.get("method", ""))
    if method in UNSAFE_METHODS and not origin_matches(origin, config):
        return AccessDecision(False, None)
    if "origin" in headers and not origin_matches(origin, config):
        return AccessDecision(False, None)
    return AccessDecision(True, _network_origin(peer, headers, config))


def _network_origin(
    peer: str,
    headers: dict[str, str],
    config: InstanceConfig,
) -> str:
    if config.access_mode != "remote" or peer != config.trusted_proxy_peer:
        return peer
    forwarded = headers.get("x-forwarded-for")
    if forwarded is None:
        return peer
    candidate = forwarded.split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


def _peer_ip(scope: dict[str, object]) -> str | None:
    client = scope.get("client")
    if not isinstance(client, tuple) or not client:
        return None
    try:
        return str(ipaddress.ip_address(str(client[0])))
    except ValueError:
        return None


def _headers(scope: dict[str, object]) -> tuple[dict[str, str], set[str]]:
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, list):
        return {}, set()
    collected: dict[str, list[bytes]] = {}
    for item in raw_headers:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        name, value = item
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            continue
        collected.setdefault(name.lower().decode("latin-1"), []).append(value)
    parsed: dict[str, str] = {}
    duplicated: set[str] = set()
    for name, values in collected.items():
        if len(values) != 1:
            duplicated.add(name)
            continue
        parsed[name] = values[0].decode("latin-1")
    return parsed, duplicated
