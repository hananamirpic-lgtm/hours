"""Health service.

Liveness answers whether the process is running and must never depend on anything external — an
orchestrator restarting the API because PostgreSQL was briefly slow makes an incident worse.
Readiness answers whether the process can actually serve traffic, and so probes every dependency.

Probes run concurrently and under a deadline, so readiness costs the slowest dependency rather than
the sum of all of them, and a hung dependency cannot hang the endpoint.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings
from app.repositories.health import (
    DATABASE,
    OBJECT_STORAGE,
    REDIS,
    ProbeResult,
    check_database,
    check_object_storage,
    check_redis,
)

Probe = Callable[[Settings], ProbeResult]

DEFAULT_PROBES: Mapping[str, Probe] = {
    DATABASE: check_database,
    REDIS: check_redis,
    OBJECT_STORAGE: check_object_storage,
}

HEALTHY = "healthy"
DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class LivenessReport:
    status: Literal["healthy"]
    service: str
    version: str
    environment: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: Literal["healthy", "degraded"]
    checks: tuple[ProbeResult, ...]

    @property
    def is_ready(self) -> bool:
        return self.status == HEALTHY


def liveness(settings: Settings) -> LivenessReport:
    return LivenessReport(
        status=HEALTHY,
        service=settings.service_name,
        version=settings.version,
        environment=settings.environment,
    )


def readiness(settings: Settings, probes: Mapping[str, Probe] | None = None) -> ReadinessReport:
    """Probe every dependency and summarise. Injectable probes keep this unit-testable."""
    selected = DEFAULT_PROBES if probes is None else probes
    if not selected:
        return ReadinessReport(status=HEALTHY, checks=())

    deadline_seconds = settings.readiness_deadline_seconds
    pool = ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="readiness")
    try:
        futures = {name: pool.submit(probe, settings) for name, probe in selected.items()}
        deadline = time.monotonic() + deadline_seconds
        results: list[ProbeResult] = []
        for name, future in futures.items():
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                results.append(future.result(timeout=remaining))
            except FutureTimeoutError:
                results.append(
                    ProbeResult(
                        name=name,
                        status="down",
                        latency_ms=round(deadline_seconds * 1000, 2),
                        detail="timeout",
                    )
                )
            except Exception as error:  # noqa: BLE001 - a broken probe means the dependency is unusable
                results.append(
                    ProbeResult(name=name, status="down", latency_ms=0.0, detail=type(error).__name__)
                )
    finally:
        # Do not wait on a hung probe; its client-side socket timeout will end it.
        pool.shutdown(wait=False, cancel_futures=True)

    status = HEALTHY if all(result.is_up for result in results) else DEGRADED
    return ReadinessReport(status=status, checks=tuple(results))
