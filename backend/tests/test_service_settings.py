"""Example tests for the service-ticket settings and parsing helpers.

Requirements 21.1, 21.2 (feature flag), 22.4 (numeric fallback), 25.8 (token TTL) and
23.10 (accessibility contact).
"""

from __future__ import annotations

import ipaddress
import logging

import pytest

from app.core.config import Settings
from app.service_tickets.config import (
    DEFAULT_TRUSTED_PROXY_CIDRS,
    ServiceConfig,
    flag_enabled,
    parse_cidrs,
    positive_int,
)


def _build(**overrides: str) -> ServiceConfig:
    """A ServiceConfig from a Settings built with only the given service values set."""
    env = Settings.model_validate(
        {
            "database_url": "sqlite://",
            "redis_url": "redis://localhost:6379/0",
            "jwt_secret_key": "x" * 32,
            "encryption_key": "y" * 32,
            "s3_endpoint_url": "http://localhost:9000",
            "s3_access_key_id": "a",
            "s3_secret_access_key": "b",
            "s3_bucket_documents": "docs",
            **overrides,
        }
    )
    return ServiceConfig.from_settings(env)


# ------------------------------------------------------------------ flag (R21.1, R21.2)


@pytest.mark.parametrize("raw", [None, "", "   ", "1", "yes", "on", "false", "truee"])
def test_flag_is_off_unless_the_word_true(raw):
    assert flag_enabled(raw) is False


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "TRUE ", " true", "  tRuE\t"])
def test_flag_is_on_for_true_ignoring_case_and_whitespace(raw):
    assert flag_enabled(raw) is True


def test_settings_do_not_parse_the_flag_as_a_bool():
    # Pydantic would read "1" or "yes" as True; the raw string must reach the helper unchanged.
    assert _build(service_tickets_enabled="1").enabled is False
    assert _build(service_tickets_enabled="yes").enabled is False
    assert _build(service_tickets_enabled="TRUE ").enabled is True


# ------------------------------------------------------------------ numbers (R22.4, R25.8)


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "abc", "12abc", "0", "-5", "5.5", "1e3", "+7", "٣", "²"]
)
def test_positive_int_falls_back_to_default(raw):
    assert positive_int(raw, 42) == 42


def test_positive_int_falls_back_for_an_absurdly_long_digit_string():
    assert positive_int("9" * 5000, 42) == 42


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("60", 60), (" 900 ", 900), ("007", 7)])
def test_positive_int_accepts_positive_integers(raw, expected):
    assert positive_int(raw, 42) == expected


def test_malformed_settings_values_do_not_fail_startup_and_use_defaults():
    config = _build(
        service_customer_token_ttl_seconds="eight hours",
        service_signin_rate_max="0",
        service_signin_rate_window_seconds="-60",
        service_submit_rate_max="2.5",
        service_signin_lock_attempts=" ",
    )
    assert config.token_ttl_seconds == 28800
    assert config.signin_rate_max == 10
    assert config.signin_rate_window_seconds == 60
    assert config.submit_rate_max == 10
    assert config.signin_lock_attempts == 5


def test_valid_settings_values_are_used():
    config = _build(
        service_customer_token_ttl_seconds="3600",
        service_request_rate_max="50",
        service_request_rate_window_seconds="30",
        service_signin_lock_seconds="600",
    )
    assert config.token_ttl_seconds == 3600
    assert config.request_rate_max == 50
    assert config.request_rate_window_seconds == 30
    assert config.signin_lock_seconds == 600


# ------------------------------------------------------------------ defaults


def test_defaults_when_nothing_is_set():
    config = _build()
    assert config.enabled is False
    assert config.token_ttl_seconds == 28800
    assert (config.signin_rate_max, config.signin_rate_window_seconds) == (10, 60)
    assert (config.submit_rate_max, config.submit_rate_window_seconds) == (10, 3600)
    assert (config.request_rate_max, config.request_rate_window_seconds) == (120, 60)
    assert (config.signin_lock_attempts, config.signin_lock_seconds) == (5, 900)
    assert config.client_ip_header == "CloudFront-Viewer-Address"
    assert config.trusted_proxy_cidrs == tuple(
        ipaddress.ip_network(c) for c in DEFAULT_TRUSTED_PROXY_CIDRS.split(",")
    )
    assert len(config.trusted_proxy_cidrs) == 6
    assert config.accessibility_contact == ""


def test_blank_client_ip_header_uses_the_default_and_a_set_one_is_stripped():
    assert _build(service_client_ip_header="  ").client_ip_header == "CloudFront-Viewer-Address"
    assert _build(service_client_ip_header=" X-Real-IP ").client_ip_header == "X-Real-IP"


def test_config_is_frozen_and_slotted():
    config = _build()
    with pytest.raises(AttributeError):
        config.enabled = True  # type: ignore[misc]
    assert not hasattr(config, "__dict__")


# ------------------------------------------------------------------ CIDRs (R22.1)


def test_invalid_cidr_is_dropped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.service_tickets.config"):
        config = _build(service_trusted_proxy_cidrs="10.0.0.0/8, not-a-cidr ,300.1.1.1/8,::1/128")
    assert config.trusted_proxy_cidrs == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "not-a-cidr" in warnings[0].getMessage()
    assert "300.1.1.1/8" in warnings[1].getMessage()


def test_cidr_with_host_bits_is_dropped(caplog):
    with caplog.at_level(logging.WARNING, logger="app.service_tickets.config"):
        assert parse_cidrs("10.0.0.1/8") == ()
    assert len(caplog.records) == 1


def test_stray_commas_are_skipped_without_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.service_tickets.config"):
        assert parse_cidrs(",10.0.0.0/8,,") == (ipaddress.ip_network("10.0.0.0/8"),)
    assert caplog.records == []


def test_all_invalid_cidrs_leave_no_trusted_proxy():
    assert _build(service_trusted_proxy_cidrs="garbage").trusted_proxy_cidrs == ()


# ------------------------------------------------------------------ accessibility contact (R23.10)


def test_accessibility_contact_is_stripped_and_otherwise_kept():
    config = _build(service_accessibility_contact="  access@example.com | 03-555-0100 \n")
    assert config.accessibility_contact == "access@example.com | 03-555-0100"