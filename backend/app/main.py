"""Application factory.

The error envelope, structured request logging and rate limiting arrive with the API conventions
task; this module deliberately stays thin so those land in one place.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core import scheduler as scheduler_module
from app.core.config import Settings, get_settings
from app.core.security_headers import SecurityHeadersMiddleware
from app.models.user import set_2fa_enforcement
from app.repositories.health import get_object_storage_client

logger = logging.getLogger(__name__)


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Building the object storage client is local work but slow enough to distort the first
    # readiness probe, so it is paid here instead. No network call happens.
    get_object_storage_client()
    # The scheduled notification jobs (Requirement 14). Guarded by `scheduler_enabled` so the test
    # client and a multi-instance deployment do not both fire them; returns None when disabled.
    scheduler = scheduler_module.start_scheduler(settings)
    logger.info(
        "started service=%s version=%s environment=%s",
        settings.service_name,
        settings.version,
        settings.environment,
    )
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    logger.info("stopped service=%s", settings.service_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    # Honour the 2FA enrolment toggle (Requirement 1.6). Default-on; a deployment that sets
    # `require_2fa_enrolment=false` relaxes the obligation. Applied here so both the API gate and the
    # `/auth/me` flags the front end reads see the same value.
    set_2fa_enforcement(settings.require_2fa_enrolment)

    app = FastAPI(
        lifespan=lifespan,
        title="Hours API",
        version=settings.version,
        description="Employee management system: attendance, payroll, billing.",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # The hardening response headers (Requirement 20.9). Added before CORS so it is outermost and
    # stamps every response, including CORS preflight replies and error bodies.
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_enabled=settings.hsts_enabled,
        hsts_max_age_seconds=settings.hsts_max_age_seconds,
    )

    # Explicit allowlist rather than a wildcard; the frontend origins are known (Requirement 20.9).
    # Cross-origin access is restricted to the configured front-end origins, and credentials are
    # allowed only for them — a wildcard origin with credentials is disallowed by the browser anyway
    # and would be a mistake here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
