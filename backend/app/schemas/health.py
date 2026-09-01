"""Health response schemas. Locale-neutral, like every response the API produces."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness payload."""

    status: Literal["healthy"]
    service: str
    version: str
    environment: str


class DependencyStatus(BaseModel):
    """One dependency's probe outcome."""

    name: str = Field(description="database | redis | object_storage")
    status: Literal["up", "down"]
    latency_ms: float
    detail: str | None = Field(
        default=None,
        description="Failure kind when down. Never contains a message that could carry a credential.",
    )


class ReadinessResponse(BaseModel):
    """Readiness payload. `degraded` is returned with HTTP 503."""

    status: Literal["healthy", "degraded"]
    checks: list[DependencyStatus]
