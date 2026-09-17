"""Settings router — read and tune the calculation engine's knobs (the settings screen).

The administrative surface for the tuning settings: the working-day length (the daily overtime
threshold), the Shabbat window, the anomaly thresholds. Two endpoints, both administrator-only —
tuning the engine is the administrator's job (Requirement 2.2), and no other role has any business
changing what every future calculation is measured against.

HTTP only, like the other routers: guard the caller, validate the body, call the service, map a
refusal onto a status code and the error envelope. The service owns every rule and never commits, so
this module commits after a successful write and the audit rows the service added ride along with it,
keeping a change and its audit record in one transaction (Requirement 13.2).

A change here alters *future* calculations only. The engine reads a setting at calculation time, so
editing the working-day length changes the next calculation and never rewrites an already-computed
payroll or billing figure — there is no recalculation triggered here, by design.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, HTTPException

from app.api.deps import AuthenticatedContext, DbSession, OperationsCaller
from app.models.setting import Setting
from app.schemas.settings import SettingItem, SettingsListResponse, SettingsUpdate
from app.services import settings as settings_service

router = APIRouter(prefix="/settings", tags=["settings"])


def _list_response(rows: list[Setting]) -> SettingsListResponse:
    """The whole settings table, ordered by key, as the screen reads it."""
    return SettingsListResponse(items=[SettingItem.model_validate(row) for row in rows])


@router.get(
    "",
    response_model=SettingsListResponse,
    summary="Read the tuning settings",
    description=(
        "Every tuning knob the calculation engine reads — the working-day length (the daily overtime "
        "threshold), the Shabbat window, the anomaly thresholds — with its stored value and declared "
        "type, ordered by key. Administrator only."
    ),
    responses={HTTPStatus.FORBIDDEN: {"description": "The caller is not an administrator"}},
)
def read_settings(caller: OperationsCaller, session: DbSession) -> SettingsListResponse:
    # The administrator guard is the whole access decision; `caller` is bound so the guard runs.
    _ = caller
    return _list_response(settings_service.list_settings(session))


@router.patch(
    "",
    response_model=SettingsListResponse,
    summary="Update the tuning settings",
    description=(
        "Applies a set of changes to the tuning knobs, keyed by name, and returns the whole list "
        "afterwards. Each value is validated against its setting's declared type and range — the "
        "working-day length must be a whole number of minutes between 1 and 1440, for instance — and "
        "a rejected value leaves every setting unchanged. A key that is not already a setting is "
        "refused: this tunes the seeded knobs, it does not create new ones. Changing a setting alters "
        "future calculations only; it does not rewrite an already-computed payroll or billing figure. "
        "Administrator only."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: {"description": "A named setting does not exist"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not an administrator"},
        HTTPStatus.UNPROCESSABLE_ENTITY: {"description": "A value does not fit its type or range"},
    },
)
def update_settings(
    payload: SettingsUpdate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> SettingsListResponse:
    # The administrator guard is the whole access decision; `caller` is bound so the guard runs.
    _ = caller
    try:
        rows = settings_service.update_settings(session, changes=payload.updates, context=context)
        session.commit()
    except settings_service.SettingNotFound as error:
        session.rollback()
        # An unknown key is a bad request — the body named a knob that is not there, and a different
        # body would work. 400 rather than 404 because the endpoint addresses the collection, not a
        # single row; the offending key travels in the envelope so the screen can point at it.
        raise _envelope(HTTPStatus.BAD_REQUEST, error.code, error.key) from error
    except settings_service.InvalidSettingValue as error:
        session.rollback()
        # A value that will not fit its type or range is unprocessable content — the request is
        # well-formed but semantically wrong for the named knob (422).
        raise _envelope(HTTPStatus.UNPROCESSABLE_ENTITY, error.code, error.key) from error
    return _list_response(rows)


def _envelope(status: HTTPStatus, code: str, key: str) -> HTTPException:
    """A settings failure carrying the machine code and the offending key.

    Names the `key` in the envelope's `params` so the settings screen can mark the field that was
    rejected rather than showing a form-wide error, the same shape the user router uses to name a
    conflicting username.
    """
    return HTTPException(status_code=status, detail={"error": {"code": code, "params": {"key": key}}})
