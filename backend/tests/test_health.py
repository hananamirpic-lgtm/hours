"""Health endpoint and health service tests."""

from __future__ import annotations

import time

from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from app.repositories.health import DATABASE, OBJECT_STORAGE, REDIS, ProbeResult
from app.services import health as health_service


def _up(name: str):
    return lambda _settings: ProbeResult(name=name, status="up", latency_ms=1.0)


def _down(name: str, detail: str = "OperationalError"):
    return lambda _settings: ProbeResult(name=name, status="down", latency_ms=1.0, detail=detail)


ALL_UP = {DATABASE: _up(DATABASE), REDIS: _up(REDIS), OBJECT_STORAGE: _up(OBJECT_STORAGE)}


# --------------------------------------------------------------------------- liveness


def test_health_reports_healthy(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "hours-api"
    assert body["environment"] == "test"
    assert body["version"]


def test_health_does_not_depend_on_dependencies(client, monkeypatch):
    """Liveness must answer even when every dependency is unreachable."""

    def explode(_settings):
        raise AssertionError("liveness must not probe dependencies")

    monkeypatch.setattr(health_service, "DEFAULT_PROBES", {DATABASE: explode})

    assert client.get("/api/health").status_code == 200


# --------------------------------------------------------------------------- readiness


def test_readiness_healthy_when_all_dependencies_up(client, monkeypatch):
    monkeypatch.setattr(health_service, "DEFAULT_PROBES", ALL_UP)

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert {check["name"] for check in body["checks"]} == {DATABASE, REDIS, OBJECT_STORAGE}
    assert all(check["status"] == "up" for check in body["checks"])
    assert all(check["detail"] is None for check in body["checks"])


def test_readiness_returns_503_and_names_the_failed_dependency(client, monkeypatch):
    monkeypatch.setattr(
        health_service,
        "DEFAULT_PROBES",
        {**ALL_UP, REDIS: _down(REDIS, "ConnectionError")},
    )

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    by_name = {check["name"]: check for check in body["checks"]}
    assert by_name[REDIS]["status"] == "down"
    assert by_name[REDIS]["detail"] == "ConnectionError"
    assert by_name[DATABASE]["status"] == "up"


def test_readiness_reports_a_raising_probe_as_down_without_leaking_the_message(client, monkeypatch):
    """Driver errors embed connection strings, so only the exception type may be reported."""

    def leaky(_settings):
        raise ConnectionRefusedError("could not connect to postgres://user:sup3rsecret@host/db")

    monkeypatch.setattr(health_service, "DEFAULT_PROBES", {DATABASE: leaky})

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    detail = response.json()["checks"][0]["detail"]
    assert detail == "ConnectionRefusedError"
    assert "sup3rsecret" not in response.text


def test_readiness_bounds_a_hanging_probe(settings, monkeypatch):
    monkeypatch.setattr(settings, "probe_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "probe_deadline_grace_seconds", 0.1)

    def hangs(_settings):
        time.sleep(5)
        raise AssertionError("unreachable")

    started = time.perf_counter()
    report = health_service.readiness(settings, probes={DATABASE: hangs})
    elapsed = time.perf_counter() - started

    assert report.status == "degraded"
    assert report.checks[0].detail == "timeout"
    assert elapsed < 2.0


def test_readiness_deadline_leaves_room_for_a_probe_to_fail_on_its_own(settings):
    """
    The deadline must exceed the per-probe timeout. If it did not, every dependency failure would
    be reported as "timeout" and the real cause — refused, unauthorized, wrong bucket — would be
    lost exactly when it is needed.
    """
    assert settings.readiness_deadline_seconds > settings.probe_timeout_seconds


def test_real_probes_report_down_for_unreachable_dependencies(settings):
    """The probes themselves must degrade rather than raise when nothing is listening."""
    report = health_service.readiness(settings)

    assert report.status == "degraded"
    assert len(report.checks) == 3
    assert all(check.status == "down" for check in report.checks)
    assert all(check.detail for check in report.checks)


# --------------------------------------------------------------------------- property


@given(statuses=st.lists(st.booleans(), min_size=1, max_size=6))
@hypothesis_settings(deadline=None, max_examples=40)
def test_readiness_is_healthy_exactly_when_every_probe_is_up(statuses):
    """Aggregation property: one dependency down is enough to make the service not ready."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    app_settings = get_settings()

    probes = {
        f"dep{index}": (_up(f"dep{index}") if is_up else _down(f"dep{index}"))
        for index, is_up in enumerate(statuses)
    }

    report = health_service.readiness(app_settings, probes=probes)

    assert len(report.checks) == len(probes)
    assert report.is_ready is all(statuses)
    assert {check.name for check in report.checks} == set(probes)
