"""Health router. HTTP only: call the service, shape the response, choose the status code."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus

from fastapi import APIRouter, Response

from app.api.deps import SettingsDep
from app.schemas.health import HealthResponse, ReadinessResponse
from app.services import health as health_service

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness",
    description="Confirms the process is serving. Touches no dependency.",
)
def get_health(settings: SettingsDep) -> HealthResponse:
    report = health_service.liveness(settings)
    return HealthResponse(**asdict(report))


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness",
    description=(
        "Reports per-dependency status for PostgreSQL, Redis and object storage. "
        "Returns 200 when every dependency is up and 503 when any is down."
    ),
    responses={
        HTTPStatus.SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": "At least one dependency is down",
        }
    },
)
def get_readiness(settings: SettingsDep, response: Response) -> ReadinessResponse:
    report = health_service.readiness(settings)
    if not report.is_ready:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status=report.status,
        checks=[asdict(check) for check in report.checks],  # type: ignore[arg-type]
    )
