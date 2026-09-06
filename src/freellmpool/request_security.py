"""Pure HTTP request-boundary checks for the local inference gateway."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from email.message import Message
from typing import Protocol, cast
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BoundaryRejection:
    status: int
    message: str


def _authority(value: str) -> tuple[str, int] | None:
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit("http://" + value)
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
            return None
        hostname = parsed.hostname.lower()
        if ":" in hostname:
            hostname = str(ipaddress.IPv6Address(hostname))
        elif not re.fullmatch(r"[a-z0-9.-]+", hostname) or hostname.endswith("."):
            return None
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            return None
        return hostname, port
    except ValueError:
        return None


@dataclass(frozen=True)
class RequestBoundaryPolicy:
    authorities: frozenset[tuple[str, int]]

    @classmethod
    def for_address(
        cls, host: str, port: int, extra_authorities: Iterable[str] = (),
    ) -> RequestBoundaryPolicy:
        hosts = {host.lower()}
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if loopback:
            hosts.update(("127.0.0.1", "::1", "localhost"))
        authorities = {(name, port) for name in hosts}
        for value in extra_authorities:
            parsed = _authority(value)
            if parsed is None:
                raise ValueError("invalid configured HTTP authority")
            authorities.add(parsed)
        return cls(frozenset(authorities))


class _RepeatedHeaders(Protocol):
    def get_all(self, name: str, failobj: list[str], /) -> list[str]: ...


def _values(headers: Message | Mapping[str, str], name: str) -> list[str]:
    if hasattr(headers, "get_all"):
        return cast(_RepeatedHeaders, headers).get_all(name, [])
    return [str(value) for key, value in headers.items() if key.lower() == name.lower()]


def validate_boundary(
    headers: Message | Mapping[str, str],
    policy: RequestBoundaryPolicy,
    *,
    json_body: bool = False,
    multipart_body: bool = False,
) -> BoundaryRejection | None:
    """Reject browser origin/rebinding and ambiguous unsupported HTTP framing.

    This supplements API-key authentication; no DNS or forwarded-header trust is
    involved. Error strings are constant so credentials/header values cannot leak.
    """
    for name in ("Host", "Origin", "Content-Length", "Content-Type", "Transfer-Encoding"):
        if len(_values(headers, name)) > 1:
            return BoundaryRejection(400, "ambiguous HTTP headers")
    host_values = _values(headers, "Host")
    authority = _authority(host_values[0]) if host_values else None
    if authority is None:
        return BoundaryRejection(400, "invalid or missing Host header")
    if authority not in policy.authorities:
        return BoundaryRejection(403, "HTTP authority is not allowed")
    origins = _values(headers, "Origin")
    if origins:
        try:
            origin = urlsplit(origins[0])
            origin_authority = _authority(origin.netloc)
            if (
                origin.scheme != "http"
                or origin.path
                or origin.query
                or origin.fragment
                or origin_authority not in policy.authorities
            ):
                return BoundaryRejection(403, "browser origin is not allowed")
        except ValueError:
            return BoundaryRejection(403, "browser origin is not allowed")
    if _values(headers, "Transfer-Encoding"):
        return BoundaryRejection(400, "transfer encoding is not supported")
    if json_body or multipart_body:
        values = _values(headers, "Content-Type")
        content_type = Message()
        content_type["Content-Type"] = values[0] if values else ""
        if json_body and content_type.get_content_type() != "application/json":
            return BoundaryRejection(415, "this endpoint requires a JSON media type")
        if multipart_body and (
            content_type.get_content_type() != "multipart/form-data"
            or not content_type.get_param("boundary")
        ):
            return BoundaryRejection(415, "this endpoint requires a multipart upload")
    return None
