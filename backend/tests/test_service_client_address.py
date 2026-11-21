"""Example tests for the client address extraction (R22.1; design.md, "Client address")."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from app.service_tickets import client_address as module
from app.service_tickets.client_address import client_address, parse_header_address
from app.service_tickets.config import (
    DEFAULT_CLIENT_IP_HEADER,
    DEFAULT_TRUSTED_PROXY_CIDRS,
    ServiceConfig,
    parse_cidrs,
)

HEADER = DEFAULT_CLIENT_IP_HEADER
LOGGER_NAME = "app.service_tickets.client_address"


def make_cfg(cidrs: str = DEFAULT_TRUSTED_PROXY_CIDRS, header: str = HEADER) -> ServiceConfig:
    return ServiceConfig(
        enabled=True,
        token_ttl_seconds=28800,
        signin_rate_max=10,
        signin_rate_window_seconds=60,
        submit_rate_max=10,
        submit_rate_window_seconds=3600,
        request_rate_max=120,
        request_rate_window_seconds=60,
        signin_lock_attempts=5,
        signin_lock_seconds=900,
        client_ip_header=header,
        trusted_proxy_cidrs=parse_cidrs(cidrs),
        accessibility_contact="",
    )


@pytest.fixture(autouse=True)
def _reset_warning_gate() -> Iterator[None]:
    module._missing_header_gate.reset()
    yield
    module._missing_header_gate.reset()


@pytest.fixture(autouse=True)
def _usable_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Alembic run earlier in the process (`fileConfig`) can leave this logger disabled."""
    monkeypatch.setattr(module.logger, "disabled", False)
    monkeypatch.setattr(module.logger, "propagate", True)


TRUSTED_PEER = "172.18.0.5"


def test_trusted_peer_header_with_ipv4_and_port() -> None:
    assert client_address(TRUSTED_PEER, {HEADER: "203.0.113.7:51234"}, make_cfg()) == "203.0.113.7"


def test_trusted_peer_header_with_bare_ipv4() -> None:
    assert client_address(TRUSTED_PEER, {HEADER: "203.0.113.7"}, make_cfg()) == "203.0.113.7"


def test_forged_header_from_untrusted_peer_is_ignored() -> None:
    result = client_address("198.51.100.9", {HEADER: "203.0.113.7:1"}, make_cfg())
    assert result == "198.51.100.9"


def test_forged_header_from_untrusted_ipv6_peer_is_ignored() -> None:
    result = client_address("2001:db8:aa:bb:1:2:3:4", {HEADER: "203.0.113.7:1"}, make_cfg())
    assert result == "2001:db8:aa:bb::/64"


def test_untrusted_peer_with_no_header_uses_its_own_address_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert client_address("198.51.100.9", {}, make_cfg()) == "198.51.100.9"
    assert caplog.records == []


def test_ipv6_in_header_is_reduced_to_its_64() -> None:
    headers = {HEADER: "[2001:db8:1:2:aaaa:bbbb:cccc:dddd]:443"}
    assert client_address(TRUSTED_PEER, headers, make_cfg()) == "2001:db8:1:2::/64"


def test_two_addresses_in_one_64_share_a_key_and_other_64s_differ() -> None:
    cfg = make_cfg()
    a = client_address(TRUSTED_PEER, {HEADER: "[2001:db8:1:2::1]:1"}, cfg)
    b = client_address(TRUSTED_PEER, {HEADER: "[2001:db8:1:2:ffff:ffff:ffff:ffff]:2"}, cfg)
    c = client_address(TRUSTED_PEER, {HEADER: "[2001:db8:1:3::1]:1"}, cfg)
    assert a == b == "2001:db8:1:2::/64"
    assert c == "2001:db8:1:3::/64"


def test_bare_ipv6_in_header_is_reduced_to_its_64() -> None:
    assert client_address(TRUSTED_PEER, {HEADER: "2001:DB8:0:0:1::1"}, make_cfg()) == "2001:db8::/64"


def test_untrusted_ipv6_peer_is_reduced_to_its_64() -> None:
    assert client_address("2001:db8:1:2:3:4:5:6", {}, make_cfg()) == "2001:db8:1:2::/64"


def test_trusted_ipv6_peer_reads_the_header() -> None:
    assert client_address("::1", {HEADER: "203.0.113.7:9"}, make_cfg()) == "203.0.113.7"


MALFORMED = [
    "",
    "   ",
    "garbage",
    "203.0.113.7:",
    "203.0.113.7:abc",
    "203.0.113.7:0",
    "203.0.113.7:65536",
    "203.0.113.7:-1",
    "203.0.113.7:１２３",  # full-width digits
    "203.0.113.7:80:80",
    "256.1.1.1:80",
    "203.0.113",
    "203.0.113.07",
    "[203.0.113.7]:80",
    "[2001:db8::1]",
    "[2001:db8::1]:",
    "[2001:db8::1]:x",
    "[2001:db8::1]:80:80",
    "2001:db8::1:80:99999",
    "[[2001:db8::1]]:80",
    "[2001:db8::1:80",
    "2001:db8::1]:80",
    "fe80::1%eth0",
    "[fe80::1%eth0]:80",
    "203.0.113.7, 198.51.100.1",
    "203.0.113.7:80, 198.51.100.1:81",
    ",203.0.113.7",
    "203.0.113.7,",
    "203.0.113.7 :80",
    "http://203.0.113.7",
]


@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_header_gives_none(value: str) -> None:
    assert parse_header_address(value) is None
    assert client_address(TRUSTED_PEER, {HEADER: value}, make_cfg()) is None


def test_surrounding_whitespace_is_ignored() -> None:
    assert client_address(TRUSTED_PEER, {HEADER: "  203.0.113.7:80 "}, make_cfg()) == "203.0.113.7"


def test_missing_header_from_trusted_peer_gives_none_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert client_address(TRUSTED_PEER, {}, make_cfg(), monotonic=lambda: 100.0) is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert HEADER in caplog.records[0].getMessage()


def test_missing_header_warning_is_logged_at_most_once_a_minute(caplog: pytest.LogCaptureFixture) -> None:
    cfg = make_cfg()
    now = [1000.0]

    def clock() -> float:
        return now[0]

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        client_address(TRUSTED_PEER, {}, cfg, monotonic=clock)  # warns
        now[0] += 59.9
        client_address(TRUSTED_PEER, {HEADER: "bad"}, cfg, monotonic=clock)  # suppressed
        assert len(caplog.records) == 1
        now[0] += 0.1  # 60 s after the first warning
        client_address(TRUSTED_PEER, {}, cfg, monotonic=clock)  # warns again
        assert len(caplog.records) == 2
        now[0] += 30
        client_address(TRUSTED_PEER, {}, cfg, monotonic=clock)  # suppressed
        assert len(caplog.records) == 2


def test_warning_does_not_contain_the_header_value(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        client_address(TRUSTED_PEER, {HEADER: "evil\nINJECTED"}, make_cfg(), monotonic=lambda: 1.0)
    assert len(caplog.records) == 1
    assert "INJECTED" not in caplog.records[0].getMessage()


def test_a_good_header_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        client_address(TRUSTED_PEER, {HEADER: "203.0.113.7:1"}, make_cfg())
    assert caplog.records == []


def test_header_name_lookup_is_case_insensitive() -> None:
    headers = {"cloudfront-viewer-address": "203.0.113.7:1"}
    assert client_address(TRUSTED_PEER, headers, make_cfg()) == "203.0.113.7"


def test_a_different_configured_header_name_is_used() -> None:
    cfg = make_cfg(header="X-Real-Client")
    assert client_address(TRUSTED_PEER, {"X-Real-Client": "203.0.113.7:1"}, cfg) == "203.0.113.7"
    assert client_address(TRUSTED_PEER, {HEADER: "203.0.113.7:1"}, cfg) is None


def test_peer_outside_a_custom_trusted_list_is_untrusted() -> None:
    cfg = make_cfg(cidrs="192.0.2.0/24")
    assert client_address(TRUSTED_PEER, {HEADER: "203.0.113.7:1"}, cfg) == TRUSTED_PEER
    assert client_address("192.0.2.77", {HEADER: "203.0.113.7:1"}, cfg) == "203.0.113.7"


def test_empty_trusted_list_never_trusts_the_header() -> None:
    cfg = make_cfg(cidrs="not-a-cidr")
    assert cfg.trusted_proxy_cidrs == ()
    assert client_address("127.0.0.1", {HEADER: "203.0.113.7:1"}, cfg) == "127.0.0.1"


@pytest.mark.parametrize("peer", [None, "", "testclient", "not an ip", "1.2.3"])
def test_missing_or_unparseable_peer_gives_none(peer: str | None) -> None:
    assert client_address(peer, {HEADER: "203.0.113.7:1"}, make_cfg()) is None


def test_ipv4_mapped_untrusted_peer_uses_its_ipv4_address() -> None:
    assert client_address("::ffff:198.51.100.9", {}, make_cfg()) == "198.51.100.9"


def test_ipv4_mapped_peer_is_checked_against_ipv4_cidrs() -> None:
    cfg = make_cfg()
    assert client_address("::ffff:172.18.0.5", {HEADER: "203.0.113.7:1"}, cfg) == "203.0.113.7"
    assert client_address("::ffff:198.51.100.9", {HEADER: "203.0.113.7:1"}, cfg) == "198.51.100.9"


@pytest.mark.parametrize("value", ["::ffff:203.0.113.7", "[::ffff:203.0.113.7]:443"])
def test_ipv4_mapped_header_address_becomes_ipv4(value: str) -> None:
    assert client_address(TRUSTED_PEER, {HEADER: value}, make_cfg()) == "203.0.113.7"


def test_peer_zone_id_is_dropped() -> None:
    assert client_address("2001:db8:1:2::9%eth0", {}, make_cfg()) == "2001:db8:1:2::/64"
    assert client_address("fe80::1%eth0", {}, make_cfg()) == "fe80::/64"


def test_real_starlette_headers_object_works() -> None:
    from starlette.datastructures import Headers

    headers = Headers({HEADER.upper(): "203.0.113.7:55"})
    assert client_address(TRUSTED_PEER, headers, make_cfg()) == "203.0.113.7"
