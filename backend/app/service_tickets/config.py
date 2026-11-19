"""Parsing of the service-ticket settings.

`Settings` carries every service-ticket value as a raw string (see `app.core.config`). This module
turns those strings into typed values. The helpers are pure: they read no environment and no
database, and they never raise on bad input. A malformed number falls back to its documented default
rather than failing startup, and a flag is on only for the exact word `true`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_TTL_SECONDS = 28800
DEFAULT_SIGNIN_RATE_MAX = 10
DEFAULT_SIGNIN_RATE_WINDOW_SECONDS = 60
DEFAULT_SUBMIT_RATE_MAX = 10
DEFAULT_SUBMIT_RATE_WINDOW_SECONDS = 3600
DEFAULT_REQUEST_RATE_MAX = 120
DEFAULT_REQUEST_RATE_WINDOW_SECONDS = 60
DEFAULT_SIGNIN_LOCK_ATTEMPTS = 5
DEFAULT_SIGNIN_LOCK_SECONDS = 900
DEFAULT_CLIENT_IP_HEADER = "CloudFront-Viewer-Address"
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7"
)

# ASCII digits only: `str.isdigit` also accepts characters such as superscripts that `int` rejects.
_POSITIVE_INTEGER = re.compile(r"[0-9]+")

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def flag_enabled(raw: str | None) -> bool:
    """True only when the value, ignoring case and surrounding whitespace, is the word `true`.

    `1`, `yes`, `on`, an empty string and an unset value are all off.
    """
    if raw is None:
        return False
    return raw.strip().lower() == "true"


def positive_int(raw: str | None, default: int) -> int:
    """A positive integer parsed from `raw`, else `default`.

    Zero, negative numbers, decimals, signs, malformed text, an empty or whitespace-only value and
    `None` all give the default.
    """
    if raw is None:
        return default
    text = raw.strip()
    if not _POSITIVE_INTEGER.fullmatch(text):
        return default
    try:
        value = int(text)
    except ValueError:  # an absurdly long digit string exceeds the interpreter's int limit
        return default
    return value if value > 0 else default


def parse_cidrs(raw: str | None) -> tuple[IPNetwork, ...]:
    """Parse a comma-separated CIDR list. Invalid entries are dropped with a logged warning.

    An empty entry (a stray comma) is skipped silently. An entry with host bits set, such as
    `10.0.0.1/8`, is invalid and dropped, so a typo cannot widen the trusted range unnoticed.
    """
    networks: list[IPNetwork] = []
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=True))
        except ValueError:
            logger.warning(
                "Ignoring invalid CIDR %r in SERVICE_TRUSTED_PROXY_CIDRS", entry[:64]
            )
    return tuple(networks)


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Typed service-ticket configuration, built once from `Settings`."""

    enabled: bool
    token_ttl_seconds: int
    signin_rate_max: int
    signin_rate_window_seconds: int
    submit_rate_max: int
    submit_rate_window_seconds: int
    request_rate_max: int
    request_rate_window_seconds: int
    signin_lock_attempts: int
    signin_lock_seconds: int
    client_ip_header: str
    trusted_proxy_cidrs: tuple[IPNetwork, ...]
    accessibility_contact: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ServiceConfig:
        header = (settings.service_client_ip_header or "").strip()
        cidrs_raw = settings.service_trusted_proxy_cidrs
        return cls(
            enabled=flag_enabled(settings.service_tickets_enabled),
            token_ttl_seconds=positive_int(
                settings.service_customer_token_ttl_seconds, DEFAULT_TOKEN_TTL_SECONDS
            ),
            signin_rate_max=positive_int(
                settings.service_signin_rate_max, DEFAULT_SIGNIN_RATE_MAX
            ),
            signin_rate_window_seconds=positive_int(
                settings.service_signin_rate_window_seconds,
                DEFAULT_SIGNIN_RATE_WINDOW_SECONDS,
            ),
            submit_rate_max=positive_int(
                settings.service_submit_rate_max, DEFAULT_SUBMIT_RATE_MAX
            ),
            submit_rate_window_seconds=positive_int(
                settings.service_submit_rate_window_seconds,
                DEFAULT_SUBMIT_RATE_WINDOW_SECONDS,
            ),
            request_rate_max=positive_int(
                settings.service_request_rate_max, DEFAULT_REQUEST_RATE_MAX
            ),
            request_rate_window_seconds=positive_int(
                settings.service_request_rate_window_seconds,
                DEFAULT_REQUEST_RATE_WINDOW_SECONDS,
            ),
            signin_lock_attempts=positive_int(
                settings.service_signin_lock_attempts, DEFAULT_SIGNIN_LOCK_ATTEMPTS
            ),
            signin_lock_seconds=positive_int(
                settings.service_signin_lock_seconds, DEFAULT_SIGNIN_LOCK_SECONDS
            ),
            client_ip_header=header or DEFAULT_CLIENT_IP_HEADER,
            # Unset or blank gives the default list. A list whose entries are all invalid yields
            # no trusted proxy at all, which is the safe direction: the header is then never trusted.
            trusted_proxy_cidrs=parse_cidrs(
                cidrs_raw if (cidrs_raw or "").strip() else DEFAULT_TRUSTED_PROXY_CIDRS
            ),
            accessibility_contact=(settings.service_accessibility_contact or "").strip(),
        )