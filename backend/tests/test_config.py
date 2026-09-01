"""Settings tests. The point of interest is that secrets have no fallback."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import MIN_SECRET_LENGTH, Settings

SECRET_ENV_VARS = [
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_SECRET_KEY",
    "ENCRYPTION_KEY",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET_DOCUMENTS",
]


def test_settings_load_from_environment(settings):
    assert settings.environment == "test"
    assert settings.service_name == "hours-api"
    assert settings.api_prefix == "/api"
    assert settings.app_timezone == "Asia/Jerusalem"


def test_cors_origins_parsed_from_comma_separated_value(settings):
    assert settings.cors_origins == ["http://localhost:5173", "http://localhost:4173"]


@pytest.mark.parametrize("variable", SECRET_ENV_VARS)
def test_missing_secret_fails_startup(monkeypatch, variable):
    """A missing secret must be a hard failure, not a development default."""
    monkeypatch.delenv(variable, raising=False)
    # An .env on disk must not rescue it either.
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert variable.lower() in str(error.value).lower()


def test_short_secret_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert str(MIN_SECRET_LENGTH) in str(error.value)


def test_secrets_are_not_repr_readable(settings):
    """A settings dump reaching a log must not carry the key material."""
    dumped = repr(settings)
    assert settings.jwt_secret_key.get_secret_value() not in dumped
    assert settings.encryption_key.get_secret_value() not in dumped
