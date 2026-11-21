"""Client network address for the per-address sign-in limit (R22.1).

`client_address(peer, headers, cfg)` returns the "address key" the limiter counts against, or `None`
when no trustworthy address is available. The key is an IPv4 address in dotted form, or an IPv6 /64
network in canonical CIDR form (one subscriber controls a whole /64).

Trust model (design.md, "Client address"):

- The header named by `cfg.client_ip_header` (default `CloudFront-Viewer-Address`, value `ip:port`) is
  read only when `peer`, the TCP address of the caller, lies inside `cfg.trusted_proxy_cidrs`. From any
  other peer the header is ignored, so a client that reaches the app directly cannot forge its key.
- A trusted peer without a usable header gives `None`. Falling back to the proxy's own address would put
  every customer in one bucket. The caller skips the per-address limit and the failure is logged, at most
  once a minute.
- An untrusted peer uses its own address. A peer that is missing or not an IP literal gives `None`.

Decisions on edge cases:

- Header value: one address only, `a.b.c.d`, `a.b.c.d:port`, `v6`, or `[v6]:port`. Surrounding whitespace
  is ignored. A list with several comma-separated hops, a bracketed address without a port, an IPv4
  address in brackets, an unbracketed IPv6 with a port, a zone id (`%eth0`), a port outside 1 to 65535 or
  one that is not plain ASCII digits, and an empty value are all unusable and give `None`. The design
  names no multi-hop form, and the chosen header is set by CloudFront, never appended to.
- IPv4-mapped IPv6 (`::ffff:a.b.c.d`) is converted to the IPv4 address, for the peer (before the trust
  check, so `::ffff:10.0.0.5` matches `10.0.0.0/8`) and for the header value. Without this, every
  IPv4 client seen through a dual-stack socket would share the single `::ffff:0:0/64` bucket. Other
  IPv4-in-IPv6 forms (6to4, NAT64) are treated as ordinary IPv6.
- Peer zone ids (`fe80::1%eth0`) are dropped before the trust check, since they are not part of the
  address. In the header they make the value unusable.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Protocol

logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

IPV6_PREFIX_LENGTH = 64
MISSING_HEADER_WARNING_INTERVAL_SECONDS = 60.0

_PORT = re.compile(r"[0-9]{1,5}")
_BRACKETED_V6_WITH_PORT = re.compile(r"\[([^\[\]]+)\]:([^\[\]:]*)")


class ClientIpConfig(Protocol):
    """The two `ServiceConfig` fields this module reads."""

    @property
    def client_ip_header(self) -> str: ...

    @property
    def trusted_proxy_cidrs(self) -> tuple[IPNetwork, ...]: ...


class _IntervalGate:
    """Lets one event through per interval. The clock is passed in, so tests need no sleeping."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last: float | None = None
        self._lock = threading.Lock()

    def allow(self, now: float) -> bool:
        with self._lock:
            if self._last is not None and 0 <= now - self._last < self._interval:
                return False
            self._last = now
            return True

    def reset(self) -> None:
        with self._lock:
            self._last = None


_missing_header_gate = _IntervalGate(MISSING_HEADER_WARNING_INTERVAL_SECONDS)


def _normalize(address: IPAddress) -> IPAddress:
    """An IPv4-mapped IPv6 address becomes the IPv4 address it carries; anything else is unchanged."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _parse_ip(text: str) -> IPAddress | None:
    if not text or "%" in text:
        return None
    try:
        return _normalize(ipaddress.ip_address(text))
    except ValueError:
        return None


def _valid_port(text: str) -> bool:
    return _PORT.fullmatch(text) is not None and 1 <= int(text) <= 65535


def parse_header_address(value: str | None) -> IPAddress | None:
    """The single client address in a `CloudFront-Viewer-Address` style value, or `None`.

    Accepts `a.b.c.d`, `a.b.c.d:port`, a bare IPv6 address and `[v6]:port`. IPv4-mapped IPv6 is
    returned as IPv4. Everything else, including lists of addresses, returns `None`.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or "," in text:
        return None
    bracketed = _BRACKETED_V6_WITH_PORT.fullmatch(text)
    if bracketed is not None:
        host, port = bracketed.groups()
        if ":" not in host or not _valid_port(port):
            return None  # `[1.2.3.4]:80` is not a form CloudFront sends
        return _parse_ip(host)
    if "[" in text or "]" in text:
        return None
    colons = text.count(":")
    if colons == 0:
        return _parse_ip(text)
    if colons == 1:
        host, _, port = text.partition(":")
        if not _valid_port(port):
            return None
        address = _parse_ip(host)
        return address if isinstance(address, ipaddress.IPv4Address) else None
    return _parse_ip(text)  # bare IPv6; an unbracketed `v6:port` cannot be told apart and fails here


def address_key(address: IPAddress) -> str:
    """IPv4 as is; IPv6 reduced to its /64 network in canonical form (`2001:db8:1:2::/64`)."""
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return str(ipaddress.ip_network((address, IPV6_PREFIX_LENGTH), strict=False))


def _parse_peer(peer: str | None) -> IPAddress | None:
    if peer is None:
        return None
    return _parse_ip(peer.strip().partition("%")[0])


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Value of header `name`. Starlette headers are case-insensitive; a plain dict gets a scan."""
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, item in headers.items():
        if key.lower() == lowered:
            return item
    return None


def client_address(
    peer: str | None,
    headers: Mapping[str, str],
    cfg: ClientIpConfig,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
    """The address key of the caller for the per-address limit, or `None` when it is unavailable.

    `peer` is the TCP address of the caller (`request.client.host`). `monotonic` is the clock for the
    once-a-minute warning and exists for tests.
    """
    peer_address = _parse_peer(peer)
    if peer_address is None:
        return None
    if not any(peer_address in network for network in cfg.trusted_proxy_cidrs):
        return address_key(peer_address)  # never read the header from an untrusted peer
    parsed = parse_header_address(_header_value(headers, cfg.client_ip_header))
    if parsed is None:
        if _missing_header_gate.allow(monotonic()):
            # The header value is client-controlled, so it is left out of the log line.
            logger.warning(
                "Trusted proxy %s sent no usable %s header; the per-address limit is skipped",
                peer_address,
                cfg.client_ip_header[:64],
            )
        return None
    return address_key(parsed)
