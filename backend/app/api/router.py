"""Aggregates every router mounted under the API prefix."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import (
    audit,
    auth,
    billing,
    clients,
    documents,
    employees,
    exports,
    health,
    notifications,
    payroll,
    periods,
    reports,
    scans,
    search,
    settings,
    sites,
    time_entries,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(employees.router)
api_router.include_router(documents.router)
api_router.include_router(clients.router)
api_router.include_router(sites.router)
api_router.include_router(scans.router)
api_router.include_router(time_entries.router)
api_router.include_router(periods.router)
api_router.include_router(payroll.router)
api_router.include_router(billing.router)
api_router.include_router(reports.router)
api_router.include_router(exports.router)
api_router.include_router(notifications.router)
api_router.include_router(audit.router)
api_router.include_router(search.router)
api_router.include_router(settings.router)
