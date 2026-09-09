"""User management router (Requirement 1, 2.1, 2.3, 20.8).

The administrative surface for logins: create, edit, deactivate, and set a site manager's scope.
Every endpoint is administrator-only — managing accounts, roles and scope is the administrator's job
(Requirement 2.2), and no other role has any business reading another user's record.

HTTP only, like the other routers: guard, validate, call the service, map a refusal onto a status
code and the error envelope. The service owns every rule and never commits, so this module commits
after a successful write and the audit rows the service added ride along with it, keeping a change and
its audit record in one transaction (Requirement 13.2).

A response never carries a credential. `UserResponse` has no password or TOTP field, and the site
scope is attached from `user_sites` where the role is a site manager — the one place a login's card
differs by role.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import AuthenticatedContext, DbSession, OperationsCaller, api_error
from app.models.user import User, UserRole
from app.schemas.user import (
    UserCreate,
    UserListItem,
    UserListResponse,
    UserResponse,
    UserSitesResponse,
    UserSitesUpdate,
    UserUpdate,
)
from app.services import user as user_service

router = APIRouter(prefix="/users", tags=["users"])


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each service error maps onto. A duplicate username is a conflict — the request is
#: well-formed but collides with existing state. A site assignment on a role that has no scope is a
#: bad request — the body is wrong for the role named and a different body would work.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    user_service.UserNotFound.code: HTTPStatus.NOT_FOUND,
    user_service.DuplicateUsername.code: HTTPStatus.CONFLICT,
    user_service.SiteScopeNotApplicable.code: HTTPStatus.BAD_REQUEST,
    user_service.RoleAssignmentForbidden.code: HTTPStatus.FORBIDDEN,
}


def _raise_for(error: user_service.UserError) -> HTTPException:
    """Turn a service error into the matching HTTP failure, naming the conflict where it helps."""
    if isinstance(error, user_service.DuplicateUsername):
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={
                "error": {
                    "code": error.code,
                    "params": {
                        "user_id": str(error.conflicting.id),
                        "username": error.conflicting.username,
                    },
                }
            },
        )
    status = _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
    return api_error(status, error.code)


# --------------------------------------------------------------------------- serialisation


def _card(user: User, session: DbSession) -> UserResponse:
    """The user card, with a site manager's assigned scope attached (Requirement 2.3).

    `site_ids` is not a mapped column, so it is set after validation. It is empty for every role but
    site manager, which is exactly what `assigned_site_ids` returns for them.
    """
    response = UserResponse.model_validate(user)
    if user.role is UserRole.SITE_MANAGER:
        response.site_ids = user_service.assigned_site_ids(session, user.id)
    return response


# --------------------------------------------------------------------------- reads


@router.get(
    "",
    response_model=UserListResponse,
    summary="List users",
    description=(
        "A stable-sorted page of logins, optionally filtered by role. Deactivated users are included "
        "by default so an administrator can see and reactivate them; pass `include_inactive=false` to "
        "hide them. Administrator only."
    ),
)
def list_users(
    caller: OperationsCaller,
    session: DbSession,
    role: UserRole | None = None,
    include_inactive: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserListResponse:
    page = user_service.list_users(
        session, role=role, include_inactive=include_inactive, limit=limit, offset=offset
    )
    items = [UserListItem.model_validate(item) for item in page.items]
    return UserListResponse(items=items, total=page.total, limit=limit, offset=offset)


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Read one user",
    responses={HTTPStatus.NOT_FOUND: {"description": "No user with that id"}},
)
def read_user(user_id: uuid.UUID, caller: OperationsCaller, session: DbSession) -> UserResponse:
    try:
        user = user_service.get_user(session, user_id)
    except user_service.UserError as error:
        raise _raise_for(error) from error
    return _card(user, session)


# --------------------------------------------------------------------------- writes


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    response_model=UserResponse,
    summary="Create a user",
    description=(
        "Creates a login with a role and password. A site manager may be given an initial site scope "
        "in the same call. 2FA is not enrolled here — the user enrols themselves via `/auth/2fa`, "
        "mandatory before an administrator may use the API (Requirement 1.6). Administrator only."
    ),
    responses={HTTPStatus.CONFLICT: {"description": "The username is already in use"}},
)
def create_user(
    payload: UserCreate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> UserResponse:
    try:
        user = user_service.create_user(session, payload, acting_role=caller.role, context=context)
        session.commit()
    except user_service.UserError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(user)
    return _card(user, session)


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    summary="Update a user",
    description=(
        "Edits a login's username, role, linked employee, language, or password. A password sent here "
        "is a reset and ends the user's existing sessions immediately (Requirement 20.8, applied to a "
        "reset). Deactivation is a separate endpoint. Administrator only."
    ),
    responses={
        HTTPStatus.NOT_FOUND: {"description": "No user with that id"},
        HTTPStatus.CONFLICT: {"description": "The username is already in use"},
    },
)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> UserResponse:
    try:
        user = user_service.update_user(session, user_id, payload, acting_role=caller.role, context=context)
        session.commit()
    except user_service.UserError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(user)
    return _card(user, session)


@router.post(
    "/{user_id}/deactivate",
    response_model=UserResponse,
    summary="Deactivate a user",
    description=(
        "Marks a login inactive and ends its sessions immediately: future logins are refused and every "
        "token already issued stops working on its next use (Requirement 20.8). Reversible via "
        "`/users/{id}/reactivate`. Administrator only."
    ),
    responses={HTTPStatus.NOT_FOUND: {"description": "No user with that id"}},
)
def deactivate_user(
    user_id: uuid.UUID,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> UserResponse:
    try:
        user = user_service.deactivate(session, user_id, context=context)
        session.commit()
    except user_service.UserError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(user)
    return _card(user, session)


@router.post(
    "/{user_id}/reactivate",
    response_model=UserResponse,
    summary="Reactivate a user",
    description="Restores a deactivated login; the user signs in fresh. Administrator only.",
    responses={HTTPStatus.NOT_FOUND: {"description": "No user with that id"}},
)
def reactivate_user(
    user_id: uuid.UUID,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> UserResponse:
    try:
        user = user_service.reactivate(session, user_id, context=context)
        session.commit()
    except user_service.UserError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(user)
    return _card(user, session)


# --------------------------------------------------------------------------- site scope


@router.put(
    "/{user_id}/sites",
    response_model=UserSitesResponse,
    summary="Set a site manager's assigned sites",
    description=(
        "Replaces the set of sites a site manager may see and write (Requirement 2.3). The change "
        "takes effect on their next request — scope is read live from `user_sites`, so no token "
        "reissue is needed. Refused for any role but site manager. Administrator only."
    ),
    responses={
        HTTPStatus.NOT_FOUND: {"description": "No user with that id"},
        HTTPStatus.BAD_REQUEST: {"description": "The role has no site scope"},
    },
)
def set_user_sites(
    user_id: uuid.UUID,
    payload: UserSitesUpdate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> UserSitesResponse:
    try:
        site_ids = user_service.replace_user_sites(
            session, user_id, payload.site_ids, context=context
        )
        session.commit()
    except user_service.UserError as error:
        session.rollback()
        raise _raise_for(error) from error
    return UserSitesResponse(user_id=user_id, site_ids=site_ids)
