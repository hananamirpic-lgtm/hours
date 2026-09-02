"""Application settings.

Every value comes from the environment. Secrets carry no default: a missing secret is a startup
failure rather than a silent fallback, because a development default has a way of surviving into
production.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__

# Long enough that a hand-typed value cannot be brute-forced, short enough not to be annoying.
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration, validated once at startup."""

    model_config = SettingsConfigDict(
        # Both paths are tried so the app runs from the repository root or from backend/.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ application
    service_name: str = "hours-api"
    version: str = __version__
    environment: Literal["local", "test", "dev", "staging", "production"] = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api"

    # All business classification of time happens in this zone; storage stays UTC.
    app_timezone: str = "Asia/Jerusalem"

    # Comma-separated. Kept as a string because environment variables are strings; read it
    # through `cors_origins`.
    cors_allowed_origins: str = "http://localhost:5173"

    # Label an authenticator app shows beside the code, carried in the `otpauth://` provisioning URI
    # (Requirement 1.5). Configurable rather than a constant because it is how a user tells one
    # deployment's entry from another's: staging and production enrolled on the same phone under one
    # issuer are two entries with the same name and different secrets, which is a support call.
    totp_issuer: str = "Hours"

    # Whether the 2FA enrolment obligation is enforced (Requirement 1.6). On by default so the
    # mandatory-for-administrators rule holds everywhere unless a deployment deliberately relaxes it.
    # Turning it off (local development convenience) makes `is_2fa_enrolment_required` and
    # `is_2fa_enrolment_prompted` report false for every user, so the API gate lets an unenrolled
    # administrator through and the front end shows neither the blocking screen nor the prompt. Never
    # set this false in a real deployment.
    require_2fa_enrolment: bool = True

    # ------------------------------------------------------------------ secrets (no defaults)
    database_url: str = Field(description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pw@host/db")
    redis_url: str = Field(description="Redis URL used for caching and rate limiting")
    jwt_secret_key: SecretStr = Field(description="Signing key for access and refresh tokens")
    encryption_key: SecretStr = Field(description="AES-256 key material for encrypted columns")

    s3_endpoint_url: str = Field(description="S3-compatible endpoint; MinIO locally")
    #: Endpoint used only to build browser-facing presigned URLs. The two differ behind Docker: the
    #: API reaches MinIO at the internal `s3_endpoint_url` (e.g. http://minio:9000), but a presigned
    #: URL is followed by the *browser*, which can only reach the published port (http://localhost:9000).
    #: Defaults to `s3_endpoint_url` so a single-endpoint deployment (real S3, or same-host) is
    #: unaffected; set it to the public host locally so the direct upload/download from the browser works.
    s3_public_endpoint_url: str | None = Field(
        default=None, description="Public S3 endpoint for presigned URLs"
    )
    s3_access_key_id: SecretStr = Field(description="Object storage access key")
    s3_secret_access_key: SecretStr = Field(description="Object storage secret key")
    s3_bucket_documents: str = Field(description="Bucket holding employee documents")
    s3_region: str = "us-east-1"

    # ------------------------------------------------------------------ mail
    smtp_host: str = "mailhog"
    smtp_port: int = 1025
    smtp_from_address: str = "no-reply@hours.local"

    # ------------------------------------------------------------------ scheduled jobs (Requirement 14)
    # Whether the in-process APScheduler starts with the API. On locally and in a single-instance
    # deployment; off under the test client and where a dedicated runner owns the jobs instead, so two
    # instances do not both fire the daily jobs. The hour and minute the daily jobs run, and the day
    # and hour the weekly summary runs (Monday=0), are configurable so a deployment can place them off
    # peak. Times are in `app_timezone`.
    scheduler_enabled: bool = False
    daily_jobs_hour: int = 18
    daily_jobs_minute: int = 0
    weekly_summary_weekday: int = 0
    weekly_summary_hour: int = 7

    # ------------------------------------------------------------------ security headers (Requirement 20.9)
    # HTTP Strict-Transport-Security is only meaningful once TLS terminates in front of the app
    # (Requirement 20.1). Off by default so a local HTTP dev server does not pin a developer's browser
    # to HTTPS; a deployment behind TLS sets `hsts_enabled=true`. The other hardening headers are
    # unconditional and carry no such footgun, so they are always sent.
    hsts_enabled: bool = False
    hsts_max_age_seconds: int = 63072000  # two years, the value HSTS preload lists expect

    # ------------------------------------------------------------------ rate limiting (Requirement 20.4)
    # A fixed-window limiter backed by Redis guards the two endpoint groups an unauthenticated or
    # barely-authenticated caller can hammer: sign-in (credential guessing) and scans (a stolen QR
    # replayed in a loop). Both the auth lockout and the scan duplicate-window are per-identity rules
    # that still let a caller make many well-formed requests; this is the blunt per-client ceiling
    # underneath them. Limits are per client key (authenticated user id, else client IP) per window.
    # Configurable so a deployment behind a shared NAT can widen them without a code change; set the
    # limit to 0 to disable a group. The limiter fails open — if Redis is unreachable the request is
    # allowed — because a cache outage must not take sign-in down, and the auth lockout still applies.
    rate_limit_enabled: bool = True
    auth_rate_limit_max_requests: int = 10
    auth_rate_limit_window_seconds: int = 60
    scan_rate_limit_max_requests: int = 30
    scan_rate_limit_window_seconds: int = 60

    # ------------------------------------------------------------------ exports (Requirement 19)
    # The row count at or above which an export is rendered in the background rather than inline
    # (Requirement 19.6). Below it the render is cheap enough to do in the request and return the file
    # ready; at or above it the request returns a `pending` export and a worker renders it and notifies
    # the user when the file is ready. Configurable so a deployment can tune it to its data volume.
    export_async_row_threshold: int = 500

    # ------------------------------------------------------------------ bootstrap admin
    # The first administrator, created by the seed step so a fresh deployment has one login that
    # can create every other. Read here rather than baked into a migration because the password is
    # a secret: a migration is a frozen, committed artefact, and a credential must never live in
    # one. All three are optional, so the seed can run on a database that already has an admin
    # without demanding they be set again; the seed skips admin creation when the password is
    # absent and creates it exactly once when it is present (Requirement 24.6, and the runbook in
    # the deployment task).
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: SecretStr | None = None
    bootstrap_admin_language: Literal["he", "en"] = "he"

    # ------------------------------------------------------------------ operational
    # Per-dependency timeout handed to each client, so a probe fails with its own real error.
    probe_timeout_seconds: float = 2.0
    # Headroom on top of it. Probes run concurrently, so the readiness deadline only needs to
    # exceed the slowest single probe; it is a backstop for a client that ignores its timeout,
    # not the primary mechanism. Without the headroom every failure would read "timeout" and the
    # actual cause would be lost.
    probe_deadline_grace_seconds: float = 1.0

    @field_validator("jwt_secret_key", "encryption_key")
    @classmethod
    def _reject_short_secrets(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_LENGTH} characters")
        return value

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        """Rewrite a bare ``postgres://`` or ``postgresql://`` URL to ``postgresql+psycopg://``.

        A managed database (Render, Heroku and others) hands out a ``postgres://…`` URL, but the app
        talks to PostgreSQL through psycopg 3 and SQLAlchemy needs the driver named in the scheme.
        Normalising here means the URL the platform provides works as-is, with no manual editing and
        no scheme mismatch at connect time. A URL that already names a driver (``postgresql+psycopg``,
        ``postgresql+asyncpg``, …) or targets another database entirely (a SQLite test URL) is left
        untouched, so this only ever upgrades the two ambiguous PostgreSQL schemes.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def s3_presign_endpoint_url(self) -> str:
        """The endpoint presigned URLs are built against — the public one when set, else the internal.

        Presigned URLs are followed by the browser, so behind Docker they must point at the published
        host, not the internal service name. When `s3_public_endpoint_url` is unset the two are the
        same (real S3, or a same-host deployment), so nothing changes there.
        """
        return self.s3_public_endpoint_url or self.s3_endpoint_url

    @property
    def readiness_deadline_seconds(self) -> float:
        """Upper bound on how long the readiness endpoint may take."""
        return self.probe_timeout_seconds + self.probe_deadline_grace_seconds

    @property
    def cors_origins(self) -> list[str]:
        """Allowed browser origins, parsed from the comma-separated setting."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Settings singleton. Cached so validation and environment reads happen once."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
