from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass


class PublicNetworkTargetError(RuntimeError):
    """Raised when an outbound target cannot be proven publicly routable."""


@dataclass(frozen=True)
class ResolvedPublicTarget:
    hostname: str
    port: int
    connect_ip: str
    addresses: tuple[str, ...]


def normalize_hostname(value: str) -> str:
    hostname = str(value or "").strip().rstrip(".")
    if not hostname:
        raise PublicNetworkTargetError("Outbound target hostname is required.")

    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PublicNetworkTargetError("Outbound target hostname is invalid.") from exc

    lowered = hostname.casefold()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise PublicNetworkTargetError(
            "Outbound target must resolve to a public internet address."
        )
    return hostname


def resolve_public_target(hostname: str, port: int) -> ResolvedPublicTarget:
    """Resolve once, fail closed on mixed/private answers, and return one pinned IP."""
    hostname = normalize_hostname(hostname)

    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise PublicNetworkTargetError("Outbound target port is invalid.") from exc
    if not 1 <= port <= 65535:
        raise PublicNetworkTargetError("Outbound target port is invalid.")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [literal]
    else:
        try:
            records = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise PublicNetworkTargetError(
                "Outbound target hostname could not be resolved."
            ) from exc

        addresses = []
        for record in records:
            try:
                address = ipaddress.ip_address(record[4][0])
            except (IndexError, ValueError):
                continue
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise PublicNetworkTargetError(
            "Outbound target hostname resolved to no usable address."
        )

    if any(not address.is_global for address in addresses):
        raise PublicNetworkTargetError(
            "Outbound target must resolve only to public internet addresses."
        )

    address_strings = tuple(str(address) for address in addresses)
    return ResolvedPublicTarget(
        hostname=hostname,
        port=port,
        connect_ip=address_strings[0],
        addresses=address_strings,
    )
