"""User accounts: create a login, edit it, deactivate it, and set a site manager's scope.

The administrative half of Requirement 1 and 2. `app.services.auth` owns the *authentication* side of
a user — logging in, refresh rotation, 2FA enrolment — and this module owns the *management* side:
the screens an administrator uses to bring a login into being and maintain it. The two share the
`users` table and the `token_version` mechanism, and nothing here duplicates what `auth` already does.

Like the rest of the service layer, nothing here raises an HTTP error and nothing commits: the router
decides the fate of a change and its audit rows together (Requirement 13.2). Three rules carry the
weight.

**A password never round-trips.** It arrives, is hashed by `app.core.security`, and the plaintext is
gone. Setting or resetting a password bumps `token_version`, so any session established under the old
password ends at once — the same immediacy Requirement 20.8 asks of deactivation, applied to a reset.

**Deactivation ends sessions immediately (Requirement 20.8).** Setting `is_active` false is not
enough on its own: a token already issued carries no liveness flag, so it would keep working until it
expired. Bumping `token_version` in the same act is what makes `resolve_access_token` reject every
outstanding token on its next use. The two always move together, which is why deactivation is a method
rather than a settable field.

**Scope is a site-manager concept (Requirement 2.3).** `user_sites` grants a site manager the sites
they may see and write. Assigning sites to any other role is meaningless — an administrator and
accounting are unrestricted, an employee has no site scope — so the service refuses it rather than
writing rows that nothing reads.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.user import User, UserRole
from app.models.user_site import UserSite
from app.schemas.user import UserCreate, UserUpdate
from app.services.audit import AuditContext, record_change, record_model_changes, snapshot

# --------------------------------------------------------------------------- errors
# Domain errors, not HTTP errors, each carrying a machine `code` the router lifts into the error
# envelope — the same shape the other services use.


class UserError(Exception):
    """Base for every user-service failure. `code` is what the front end translates."""

    code = "user_error"


class UserNotFound(UserError):
    code = "user_not_found"

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__(f"no user {user_id}")
        self.user_id = user_id


class DuplicateUsername(UserError):
    """Another login already holds this username.

    Carries the conflicting user so the router can name it, which a bare uniqueness violation could
    never supply.
    """

    code = "duplicate_username"

    def __init__(self, conflicting: User) -> None:
        super().__init__(f"username already held by user {conflicting.id}")
        self.conflicting = conflicting


class SiteScopeNotApplicable(UserError):
    """Sites were assigned to a role that has no site scope (Requirement 2.3).

    Only a site manager's access is defined by `user_sites`; an administrator and accounting are
    unrestricted and an employee acts on their own record, so a site assignment on any of them is a
    request that could only mislead.
    """

    code = "site_scope_not_applicable"


# --------------------------------------------------------------------------- reads


def _base_select() -> Select[tuple[User]]:
    return select(User)


def get_user(session: Session, user_id: uuid.UUID) -> User:
    """Load one user, or raise `UserNotFound`."""
    user = session.scalars(_base_select().where(User.id == user_id)).one_or_none()
    if user is None:
        raise UserNotFound(user_id)
    return user


@dataclass(frozen=True, slots=True)
class UserPage:
    """A page of users plus the unfiltered-by-paging total, for list rendering."""

    items: Sequence[User]
    total: int


def list_users(
    session: Session,
    *,
    role: UserRole | None = None,
    include_inactive: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> UserPage:
    """A stable-sorted page of users (Requirement 22.5).

    Sort is `(username, id)` so the order is total and does not shift between pages. Inactive users are
    listed by default — an administrator managing accounts needs to see the deactivated ones to
    reactivate or audit them — and can be filtered out with `include_inactive=False`.
    """
    statement = _base_select()
    if role is not None:
        statement = statement.where(User.role == role)
    if not include_inactive:
        statement = statement.where(User.is_active.is_(True))

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    ordered = statement.order_by(User.username, User.id).limit(limit).offset(offset)
    items = list(session.scalars(ordered))
    return UserPage(items=items, total=total)


def assigned_site_ids(session: Session, user_id: uuid.UUID) -> list[uuid.UUID]:
    """The site ids granted to `user_id` by `user_sites`, in a stable order.

    A site-manager concept: any other role returns an empty list because no rows are ever written for
    them (see `replace_user_sites`).
    """
    ids = session.scalars(select(UserSite.site_id).where(UserSite.user_id == user_id))
    return sorted(ids, key=str)


# --------------------------------------------------------------------------- username uniqueness


def _find_username_holder(
    session: Session, username: str, *, exclude_id: uuid.UUID | None = None
) -> User | None:
    """A login already holding this username, if any. Excludes `exclude_id` so an update that leaves
    the username unchanged does not collide with the row being updated."""
    statement = select(User).where(User.username == username)
    if exclude_id is not None:
        statement = statement.where(User.id != exclude_id)
    return session.scalars(statement).first()


# --------------------------------------------------------------------------- create


def create_user(session: Session, payload: UserCreate, *, context: AuditContext) -> User:
    """Create a login and, for a site manager, its site scope (Requirement 1, 2.1, 2.3).

    Username uniqueness is checked before the insert so the conflicting login can be named; the
    database's `uq_users_username` is the backstop for the race between the check and the flush. The
    password is hashed and its plaintext discarded here — it never reaches the model as anything but a
    hash.
    """
    if payload.site_ids and payload.role is not UserRole.SITE_MANAGER:
        raise SiteScopeNotApplicable

    existing = _find_username_holder(session, payload.username)
    if existing is not None:
        raise DuplicateUsername(existing)

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        employee_id=payload.employee_id,
        language=payload.language,
        is_active=True,
        # A login the administrator creates carries a password the administrator chose, so anyone but
        # an administrator must pick their own before using the system (enforced by the gate in
        # `app.api.deps`). An admin is exempt: their obligation is 2FA, not a first-use change, and an
        # admin who was made by another admin is trusted to keep the password they were given.
        must_change_password=payload.role is not UserRole.ADMIN,
    )
    session.add(user)
    # Flush so the row has an id the audit rows and any site rows can reference.
    session.flush()

    _audit_creation(session, user, context=context)

    if payload.role is UserRole.SITE_MANAGER and payload.site_ids:
        _write_site_scope(session, user.id, payload.site_ids, context=context)

    return user


def _audit_creation(session: Session, user: User, *, context: AuditContext) -> None:
    """One audit row per field set at creation, so a login's origin is as traceable as its edits.

    Diffing against an empty snapshot reuses the update path's machinery, and the sensitive columns
    (`totp_secret`, `totp_pending_secret`) are redacted by the same rule, so no secret lands in the
    audit table even on the create path. `password_hash` is not sensitive-typed but is not a secret
    worth reading in an audit trail either; it is left as the machinery emits it — a bcrypt hash that
    discloses nothing.
    """
    empty = dict.fromkeys(snapshot(user), None)
    record_model_changes(session, user, empty, context=context, reason="user_created")


# --------------------------------------------------------------------------- update


#: Attribute names the update path may write directly. `is_active` is absent — deactivation is its own
#: act (see `deactivate`) — and so are the authentication counters, the token version, and the TOTP
#: secrets, which `app.services.auth` owns. `password` is handled specially because it is hashed and
#: carries the session-ending token bump.
_UPDATABLE_FIELDS = ("role", "employee_id", "language")


def update_user(
    session: Session, user_id: uuid.UUID, payload: UserUpdate, *, context: AuditContext
) -> User:
    """Apply a partial update, checking username uniqueness if the username changes (Requirement 1, 2.1).

    Only the fields present in the request are touched (`exclude_unset`), so a patch that names one
    field leaves the rest alone, and only the fields that actually moved produce an audit row. A
    `password` in the request is a reset: it is hashed and the token version is bumped so sessions on
    the old password end at once.
    """
    user = get_user(session, user_id)
    changes = payload.model_dump(exclude_unset=True)

    before = snapshot(user)

    if "username" in changes and changes["username"] != user.username:
        conflict = _find_username_holder(session, changes["username"], exclude_id=user.id)
        if conflict is not None:
            raise DuplicateUsername(conflict)

    if "username" in changes:
        user.username = changes["username"]

    for field in _UPDATABLE_FIELDS:
        if field in changes:
            setattr(user, field, changes[field])

    password_reset = changes.get("password") is not None
    if password_reset:
        user.password_hash = hash_password(changes["password"])
        # A password reset must end sessions established under the old one, the same immediacy
        # deactivation gives (Requirement 20.8, applied to a reset).
        user.token_version += 1

    session.flush()
    record_model_changes(session, user, before, context=context)
    if password_reset:
        # The hash change is audited by the diff above; this row names *why* the token version moved,
        # so a reader of the audit trail sees a reset rather than an unexplained version bump.
        record_change(
            session,
            entity_type="users",
            entity_id=user.id,
            field="password",
            old_value=None,
            new_value="reset",
            context=context,
            reason="password_reset",
        )
    return user


# --------------------------------------------------------------------------- deactivate / reactivate


def deactivate(session: Session, user_id: uuid.UUID, *, context: AuditContext) -> User:
    """Deactivate a login and end its sessions immediately (Requirement 20.8).

    Setting `is_active` false and bumping `token_version` are one act: the flag stops future logins
    (`app.services.auth.login` refuses an inactive user), and the version bump makes every token
    already issued fail on its next use, so an open browser session dies at once rather than at the
    token's expiry. A no-op on an already-inactive user writes nothing and leaves the version alone, so
    resubmitting the action does not needlessly invalidate a fresh admin session for the same account.
    """
    user = get_user(session, user_id)
    if not user.is_active:
        return user

    before = snapshot(user, fields=["is_active"])
    user.is_active = False
    user.token_version += 1
    session.flush()
    record_model_changes(
        session, user, before, context=context, reason="user_deactivated", fields=["is_active"]
    )
    return user


def reactivate(session: Session, user_id: uuid.UUID, *, context: AuditContext) -> User:
    """Reactivate a previously deactivated login.

    The counterpart to `deactivate`. It does not touch `token_version`: no token exists to revive — a
    deactivated user could not have obtained one — so there is nothing to invalidate, and the user
    signs in fresh. A no-op on an already-active user writes nothing.
    """
    user = get_user(session, user_id)
    if user.is_active:
        return user

    before = snapshot(user, fields=["is_active"])
    user.is_active = True
    session.flush()
    record_model_changes(
        session, user, before, context=context, reason="user_reactivated", fields=["is_active"]
    )
    return user


# --------------------------------------------------------------------------- site scope


def replace_user_sites(
    session: Session,
    user_id: uuid.UUID,
    site_ids: Sequence[uuid.UUID],
    *,
    context: AuditContext,
) -> list[uuid.UUID]:
    """Replace the set of sites a site manager may see and write (Requirement 2.3).

    The scope is replaced wholesale rather than appended to, because the request states the intended
    scope. Refused for any role but site manager, since scope is not a concept that applies to them.
    The change takes effect on the manager's next request: `resolve_site_scope` reads `user_sites`
    live, so a site added here widens what they see and a site removed narrows it, with no token
    reissue needed.
    """
    user = get_user(session, user_id)
    if user.role is not UserRole.SITE_MANAGER:
        raise SiteScopeNotApplicable
    return _write_site_scope(session, user.id, site_ids, context=context)


def _write_site_scope(
    session: Session,
    user_id: uuid.UUID,
    site_ids: Sequence[uuid.UUID],
    *,
    context: AuditContext,
) -> list[uuid.UUID]:
    """Reconcile the `user_sites` rows for a user to exactly `site_ids`, and audit the change.

    A no-op when the scope is unchanged, so re-saving the same set writes nothing. The audit row
    records that scope changed and its new size rather than the ids themselves — an administrator
    reading the audit trail cares that a manager's reach moved and by whom, and the exact sites live in
    the `user_sites` table where the card reads them.
    """
    unique_ids = _deduplicate(site_ids)
    existing = set(assigned_site_ids(session, user_id))
    desired = set(unique_ids)
    if existing == desired:
        return sorted(desired, key=str)

    session.execute(delete(UserSite).where(UserSite.user_id == user_id))
    for site_id in unique_ids:
        session.add(UserSite(user_id=user_id, site_id=site_id))
    session.flush()

    record_change(
        session,
        entity_type="users",
        entity_id=user_id,
        field="assigned_sites",
        old_value=str(len(existing)),
        new_value=str(len(desired)),
        context=context,
        reason="user_sites_updated",
    )
    return sorted(desired, key=str)


def _deduplicate(ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Preserve order while dropping repeats, so a set with a duplicate id writes one row."""
    seen: set[uuid.UUID] = set()
    unique: list[uuid.UUID] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique
