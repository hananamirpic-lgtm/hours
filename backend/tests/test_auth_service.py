"""Authentication service.

Requirement 1 stated as behaviour. The tests that would catch a plausible regression are the ones
about what a refusal does *not* say (1.2), what the lock does after it expires (1.7), and what
happens to a refresh token that is presented twice (design: rotation with reuse detection) — each of
those is a place where an implementation can look correct and be wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import TokenType, decode_token, hash_password
from app.models.change_log import ChangeLog
from app.models.user import User, UserRole
from app.services import auth as auth_service
from app.services.audit import AuditContext
from auth_support import DEFAULT_PASSWORD

CONTEXT = AuditContext(request_id="req-login-1")

#: Anchored to the real clock rather than a literal date. The service takes an injectable `now` for
#: the lock window and the TOTP step, but token *validation* reads the real clock — a fixed date in
#: the past would mint tokens that are already expired.
#:
#: Refreshed before every test by `_anchor_now_to_the_present` below, not just at import. A token
#: minted at `NOW` lives 15 minutes; if `NOW` were frozen at collection time it would already be
#: expired by the time this module runs after a long earlier phase (the performance suite runs for
#: many minutes in a combined invocation), and the "valid token" assertions would fail against the
#: real validation clock. Re-anchoring per test keeps mint and decode within the same instant.
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _anchor_now_to_the_present() -> None:
    """Move `NOW` to the real present at the start of each test, so minted tokens are not stale."""
    global NOW
    NOW = datetime.now(UTC).replace(microsecond=0)


def _events(session: Session) -> list[str]:
    """Authentication events in the audit log, oldest first."""
    return [
        entry.new_value or ""
        for entry in session.scalars(
            select(ChangeLog)
            .where(ChangeLog.field == auth_service.AUDIT_FIELD)
            .order_by(ChangeLog.changed_at, ChangeLog.new_value)
        )
    ]


def _login(session: Session, user: User, password: str = DEFAULT_PASSWORD, **kwargs):
    return auth_service.login(session, username=user.username, password=password, context=CONTEXT, **kwargs)


# --------------------------------------------------------------------------- valid login


def test_a_valid_login_returns_an_access_and_a_refresh_token(session: Session, make_user):
    user = make_user()
    pair = _login(session, user, now=NOW)

    access = decode_token(pair.access_token, expected_type=TokenType.ACCESS)
    refresh = decode_token(pair.refresh_token, expected_type=TokenType.REFRESH)
    assert access.subject == user.id
    assert access.role == UserRole.SITE_MANAGER.value
    assert refresh.subject == user.id
    assert pair.token_type == "bearer"
    assert pair.expires_in == 15 * 60


def test_a_valid_login_records_the_sign_in_time(session: Session, make_user):
    user = make_user()
    _login(session, user, now=NOW)
    assert user.last_login_at is not None


def test_a_valid_login_is_audited(session: Session, make_user):
    """Requirement 13.5. The row carries the actor even though the request arrived anonymously —
    the service fills in the identity the moment it establishes one."""
    user = make_user()
    _login(session, user, now=NOW)

    entry = session.scalars(select(ChangeLog)).one()
    assert entry.new_value == auth_service.EVENT_LOGIN_SUCCEEDED
    assert entry.entity_type == "users"
    assert entry.entity_id == user.id
    assert entry.changed_by_user_id == user.id
    assert entry.request_id == "req-login-1"


def test_a_valid_login_clears_earlier_failures(session: Session, make_user):
    """Requirement 1.7's counter is about consecutive failures, so a success has to reset it or four
    failures spread over a month would eventually lock a working account."""
    user = make_user()
    for _ in range(3):
        with pytest.raises(auth_service.InvalidCredentials):
            _login(session, user, password="wrong", now=NOW)
    assert user.failed_login_count == 3

    _login(session, user, now=NOW)
    assert user.failed_login_count == 0
    assert user.locked_until is None


# --------------------------------------------------------------------------- rejected login


def test_an_invalid_password_is_rejected(session: Session, make_user):
    user = make_user()
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, password="wrong", now=NOW)


def test_an_unknown_username_is_rejected(session: Session):
    with pytest.raises(auth_service.InvalidCredentials):
        auth_service.login(session, username="nobody", password=DEFAULT_PASSWORD, context=CONTEXT, now=NOW)


def test_an_unknown_username_and_a_wrong_password_fail_identically(session: Session, make_user):
    """Requirement 1.2. Two refusals that differ in any observable way are an existence oracle, and
    the code is the only thing the API returns, so the codes have to match."""
    user = make_user()
    with pytest.raises(auth_service.InvalidCredentials) as wrong_password:
        _login(session, user, password="wrong", now=NOW)
    with pytest.raises(auth_service.InvalidCredentials) as unknown_user:
        auth_service.login(session, username="nobody", password="wrong", context=CONTEXT, now=NOW)

    assert wrong_password.value.code == unknown_user.value.code == "invalid_credentials"


def test_an_unknown_username_still_verifies_against_a_hash(session: Session, monkeypatch):
    """The other half of Requirement 1.2: skipping the comparison would answer the same question
    through response time. Asserted by observing that the comparison happens, because asserting on
    elapsed time would be a flaky test that proves less."""
    verifications: list[str] = []
    real_verify = auth_service.verify_password

    def _counting_verify(password: str, password_hash: str) -> bool:
        verifications.append(password_hash)
        return real_verify(password, password_hash)

    monkeypatch.setattr(auth_service, "verify_password", _counting_verify)
    with pytest.raises(auth_service.InvalidCredentials):
        auth_service.login(session, username="nobody", password="anything", context=CONTEXT, now=NOW)

    assert len(verifications) == 1
    assert verifications[0].startswith("$2b$")


def test_a_deactivated_user_is_rejected_even_with_the_right_password(session: Session, make_user):
    """Requirement 1.4."""
    user = make_user(is_active=False)
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, now=NOW)


def test_a_deactivated_user_is_rejected_with_the_same_code_as_a_bad_password(session: Session, make_user):
    """Otherwise "this account exists but is disabled" is readable from the response."""
    disabled = make_user(is_active=False)
    active = make_user()
    with pytest.raises(auth_service.InvalidCredentials) as deactivated:
        _login(session, disabled, now=NOW)
    with pytest.raises(auth_service.InvalidCredentials) as bad_password:
        _login(session, active, password="wrong", now=NOW)

    assert deactivated.value.code == bad_password.value.code


def test_a_failed_login_is_audited(session: Session, make_user):
    user = make_user()
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, password="wrong", now=NOW)

    entry = session.scalars(select(ChangeLog)).one()
    assert entry.new_value == auth_service.EVENT_LOGIN_FAILED
    assert entry.reason == auth_service.REASON_BAD_PASSWORD


def test_the_failed_attempt_counter_survives_the_rejection(session: Session, make_user):
    """The service commits before raising. If the caller owned the commit, a router that returned on
    the error path without committing would silently switch the lockout off."""
    user = make_user()
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, password="wrong", now=NOW)

    session.expire_all()
    assert session.get(User, user.id).failed_login_count == 1  # type: ignore[union-attr]


# --------------------------------------------------------------------------- lockout


def _fail(session: Session, user: User, now: datetime) -> None:
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, password="wrong", now=now)


def test_the_fifth_failure_locks_authentication(session: Session, make_user):
    """Requirement 1.7."""
    user = make_user()
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS):
        _fail(session, user, NOW + timedelta(seconds=attempt))

    assert auth_service.is_locked(user, NOW + timedelta(minutes=1)) is True


def test_four_failures_do_not_lock(session: Session, make_user):
    """The boundary in the direction that matters to a working user."""
    user = make_user()
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS - 1):
        _fail(session, user, NOW + timedelta(seconds=attempt))

    assert auth_service.is_locked(user, NOW + timedelta(minutes=1)) is False
    assert _login(session, user, now=NOW + timedelta(minutes=1))


def test_the_correct_password_is_refused_while_locked(session: Session, make_user):
    """The point of a lock. Without this the counter is decoration."""
    user = make_user()
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS):
        _fail(session, user, NOW + timedelta(seconds=attempt))

    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, now=NOW + timedelta(minutes=1))


def test_the_lock_expires(session: Session, make_user):
    """Temporary, per Requirement 1.7 — a permanent lock would be a denial-of-service anyone could
    trigger against any username they can guess."""
    user = make_user()
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS):
        _fail(session, user, NOW + timedelta(seconds=attempt))

    after = NOW + auth_service.LOCK_DURATION + timedelta(minutes=1)
    assert auth_service.is_locked(user, after) is False
    assert _login(session, user, now=after)


def test_failures_outside_the_window_do_not_accumulate(session: Session, make_user):
    """ "Within 15 minutes" is the requirement. Four failures today plus one next month is not a
    brute-force attempt, and locking on it would lock out the forgetful rather than the malicious."""
    user = make_user()
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS - 1):
        _fail(session, user, NOW + timedelta(seconds=attempt))

    beyond = NOW + auth_service.FAILED_ATTEMPT_WINDOW + timedelta(minutes=1)
    _fail(session, user, beyond)

    assert user.failed_login_count == 1
    assert auth_service.is_locked(user, beyond) is False


def test_attempting_a_login_while_locked_does_not_extend_the_lock(session: Session, make_user):
    """Otherwise anyone able to guess a username could keep its owner locked out for as long as they
    cared to keep sending requests."""
    user = make_user()
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS):
        _fail(session, user, NOW + timedelta(seconds=attempt))
    locked_until = user.locked_until

    _fail(session, user, NOW + timedelta(minutes=5))
    assert user.locked_until == locked_until
    assert user.failed_login_count == auth_service.MAX_FAILED_ATTEMPTS


def test_the_lockout_is_audited(session: Session, make_user):
    user = make_user()
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS):
        _fail(session, user, NOW + timedelta(seconds=attempt))

    assert auth_service.EVENT_ACCOUNT_LOCKED in _events(session)


def test_an_access_token_stops_working_once_the_account_is_locked(session: Session, make_user):
    """A lock that only guarded the login endpoint would leave a token holder unaffected by it."""
    user = make_user()
    pair = _login(session, user, now=NOW)
    for attempt in range(auth_service.MAX_FAILED_ATTEMPTS):
        _fail(session, user, NOW + timedelta(seconds=attempt))

    with pytest.raises(auth_service.InvalidToken):
        auth_service.resolve_access_token(session, pair.access_token, now=NOW + timedelta(minutes=1))


# --------------------------------------------------------------------------- two-factor


@pytest.fixture
def totp_user(make_user):
    secret = pyotp.random_base32()
    return make_user(is_2fa_enabled=True, totp_secret=secret, role=UserRole.ADMIN), secret


def test_the_right_password_alone_is_not_enough_when_2fa_is_enabled(session: Session, totp_user):
    """Requirement 1.5. The distinct code is safe here: it is only reachable by someone who has
    already supplied the correct password, so it reveals nothing they did not know."""
    user, _ = totp_user
    with pytest.raises(auth_service.TotpRequired):
        _login(session, user, now=NOW)


def test_a_correct_totp_code_completes_the_login(session: Session, totp_user):
    user, secret = totp_user
    code = pyotp.TOTP(secret).at(NOW)
    pair = _login(session, user, totp_code=code, now=NOW)
    assert decode_token(pair.access_token, expected_type=TokenType.ACCESS).subject == user.id


def test_an_incorrect_totp_code_is_rejected_generically(session: Session, totp_user):
    user, secret = totp_user
    wrong = "000000" if pyotp.TOTP(secret).at(NOW) != "000000" else "111111"
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, totp_code=wrong, now=NOW)


def test_an_incorrect_totp_code_counts_towards_the_lock(session: Session, totp_user):
    """A second factor with unlimited attempts is a six-digit password with unlimited attempts."""
    user, _ = totp_user
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, totp_code="000000", now=NOW)
    assert user.failed_login_count == 1
    assert _events(session) == [auth_service.EVENT_LOGIN_FAILED]


def test_a_code_from_the_previous_step_is_still_accepted(session: Session, totp_user):
    """Clock skew between a phone and the server, which is the ordinary case rather than an attack."""
    user, secret = totp_user
    code = pyotp.TOTP(secret).at(NOW - timedelta(seconds=30))
    assert _login(session, user, totp_code=code, now=NOW)


def test_a_stale_code_is_rejected(session: Session, totp_user):
    user, secret = totp_user
    code = pyotp.TOTP(secret).at(NOW - timedelta(minutes=5))
    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, totp_code=code, now=NOW)


def test_2fa_enabled_with_no_secret_is_reported_as_a_configuration_fault(session: Session, make_user):
    """The generic message would send the user round the loop of retyping a correct password."""
    user = make_user(is_2fa_enabled=True, totp_secret=None)
    with pytest.raises(auth_service.TotpNotEnrolled):
        _login(session, user, now=NOW)


def test_the_totp_secret_is_encrypted_at_rest(session: Session, totp_user):
    """Requirement 20.2. The attribute is plaintext to this module; the column must not be."""
    user, secret = totp_user
    stored = (
        session.connection()
        .exec_driver_sql("SELECT totp_secret_encrypted FROM users WHERE username = ?", (user.username,))
        .scalar_one()
    )
    assert stored != secret
    assert stored.startswith("gcm1:")


# --------------------------------------------------------------------------- two-factor enrolment
# The claim these tests protect is that enrolment cannot lock anyone out. Two ways it could: enabling
# 2FA on a secret nobody has proven works, and destroying a working secret while issuing its
# replacement. Both are checked directly, because both are the kind of fault that only shows up when a
# real user has already lost access.


def _enrol(session: Session, user: User) -> str:
    """Run a full enrolment for `user` and return the secret now in force."""
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)
    auth_service.complete_totp_enrolment(
        session,
        user=user,
        totp_code=pyotp.TOTP(enrolment.secret).at(NOW),
        context=CONTEXT,
        now=NOW,
    )
    return enrolment.secret


def test_beginning_enrolment_issues_a_secret_that_generates_valid_codes(session: Session, make_user):
    """Requirement 1.5. A secret that is not valid base32, or is not the one stored, produces an
    authenticator that agrees with nothing — and the user only finds out at their next login."""
    user = make_user(role=UserRole.ADMIN)
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    assert auth_service.verify_totp(enrolment.secret, pyotp.TOTP(enrolment.secret).at(NOW), now=NOW)
    assert user.totp_pending_secret == enrolment.secret


def test_the_provisioning_uri_names_the_application_and_the_account(session: Session, make_user):
    """What an authenticator app shows beside the code. An entry labelled with neither is one the
    user deletes a year later without knowing what it was for."""
    user = make_user(role=UserRole.ADMIN, username="rivka")
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    parsed = urlparse(enrolment.provisioning_uri)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "otpauth"
    assert parsed.netloc == "totp"
    assert query["secret"] == [enrolment.secret]
    assert query["issuer"] == [get_settings().totp_issuer]
    assert "rivka" in unquote(parsed.path)


def test_beginning_enrolment_does_not_enable_2fa(session: Session, make_user):
    """The reason enrolment is two calls. Enabling here would bind the account to a secret nobody has
    shown an authenticator can generate codes from, and the user would discover that at the login
    screen with no way back in."""
    user = make_user(role=UserRole.ADMIN)
    auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    assert user.is_2fa_enabled is False
    assert user.totp_secret is None
    assert _login(session, user, now=NOW)


def test_the_issued_secret_is_audited(session: Session, make_user):
    user = make_user(role=UserRole.ADMIN)
    auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    entry = session.scalars(select(ChangeLog)).one()
    assert entry.new_value == auth_service.EVENT_TOTP_SECRET_ISSUED
    assert entry.reason == auth_service.REASON_FIRST_ENROLMENT
    assert entry.changed_by_user_id == user.id


def test_the_audit_row_does_not_carry_the_secret(session: Session, make_user):
    """A secret in the audit table is a secret in a table nobody thinks of as sensitive, and one that
    the application role cannot delete rows from."""
    user = make_user(role=UserRole.ADMIN)
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    values = [
        f"{entry.old_value} {entry.new_value} {entry.reason}" for entry in session.scalars(select(ChangeLog))
    ]
    assert values
    assert all(enrolment.secret not in value for value in values)


def test_the_pending_secret_is_encrypted_at_rest(session: Session, make_user):
    """Requirement 20.2. Unproven is not unimportant: it is minutes away from being the credential."""
    user = make_user(role=UserRole.ADMIN)
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    stored = (
        session.connection()
        .exec_driver_sql(
            "SELECT totp_pending_secret_encrypted FROM users WHERE username = ?", (user.username,)
        )
        .scalar_one()
    )
    assert stored != enrolment.secret
    assert stored.startswith("gcm1:")


def test_a_correct_code_completes_enrolment(session: Session, make_user):
    user = make_user(role=UserRole.ADMIN)
    secret = _enrol(session, user)

    assert user.is_2fa_enabled is True
    assert user.totp_secret == secret
    assert user.totp_pending_secret is None


def test_completing_enrolment_is_audited(session: Session, make_user):
    user = make_user(role=UserRole.ADMIN)
    _enrol(session, user)

    assert _events(session) == [
        auth_service.EVENT_TOTP_SECRET_ISSUED,
        auth_service.EVENT_2FA_ENABLED,
    ]


def test_an_incorrect_code_does_not_complete_enrolment(session: Session, make_user):
    """The point of the second call. A code that does not verify is evidence the authenticator does
    not hold this secret, which is exactly the case where enabling 2FA locks the user out."""
    user = make_user(role=UserRole.ADMIN)
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)
    wrong = "000000" if pyotp.TOTP(enrolment.secret).at(NOW) != "000000" else "111111"

    with pytest.raises(auth_service.InvalidTotpCode):
        auth_service.complete_totp_enrolment(session, user=user, totp_code=wrong, context=CONTEXT, now=NOW)

    assert user.is_2fa_enabled is False
    assert user.totp_secret is None
    assert user.totp_pending_secret == enrolment.secret


def test_a_stale_code_does_not_complete_enrolment(session: Session, make_user):
    user = make_user(role=UserRole.ADMIN)
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)
    stale = pyotp.TOTP(enrolment.secret).at(NOW - timedelta(minutes=5))

    with pytest.raises(auth_service.InvalidTotpCode):
        auth_service.complete_totp_enrolment(session, user=user, totp_code=stale, context=CONTEXT, now=NOW)
    assert user.is_2fa_enabled is False


def test_a_wrong_enrolment_code_does_not_count_towards_the_login_lock(session: Session, make_user):
    """Deliberate, and the opposite of the login rule. The secret under test was handed to this
    authenticated caller seconds ago, so there is nothing to guess and the only person a counter could
    lock out is the legitimate user fumbling six digits."""
    user = make_user(role=UserRole.ADMIN)
    auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    for _ in range(auth_service.MAX_FAILED_ATTEMPTS + 1):
        with pytest.raises(auth_service.InvalidTotpCode):
            auth_service.complete_totp_enrolment(
                session, user=user, totp_code="000000", context=CONTEXT, now=NOW
            )

    assert user.failed_login_count == 0
    assert auth_service.is_locked(user, NOW) is False


def test_a_code_with_no_enrolment_in_progress_is_refused(session: Session, make_user):
    """Reported distinctly because the client's next move is to begin enrolment, not to retype."""
    user = make_user(role=UserRole.ADMIN)
    with pytest.raises(auth_service.TotpEnrolmentNotStarted):
        auth_service.complete_totp_enrolment(session, user=user, totp_code="000000", context=CONTEXT, now=NOW)


def test_a_pending_secret_cannot_be_verified_twice(session: Session, make_user):
    """Completion consumes the pending secret, so a replayed verify request finds nothing to prove
    rather than silently re-enabling 2FA on a secret that has already been promoted."""
    user = make_user(role=UserRole.ADMIN)
    secret = _enrol(session, user)

    with pytest.raises(auth_service.TotpEnrolmentNotStarted):
        auth_service.complete_totp_enrolment(
            session, user=user, totp_code=pyotp.TOTP(secret).at(NOW), context=CONTEXT, now=NOW
        )


def test_a_freshly_enrolled_user_must_supply_a_code_to_sign_in(session: Session, make_user):
    """Requirement 1.5 joined up: enrolment is only meaningful if it changes what login demands."""
    user = make_user(role=UserRole.ADMIN)
    secret = _enrol(session, user)

    with pytest.raises(auth_service.TotpRequired):
        _login(session, user, now=NOW)
    assert _login(session, user, totp_code=pyotp.TOTP(secret).at(NOW), now=NOW)


def test_beginning_a_second_enrolment_replaces_an_unproven_secret(session: Session, make_user):
    """Costs nothing: the client that asked is the only party that ever held the first one."""
    user = make_user(role=UserRole.ADMIN)
    first = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)
    second = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    assert second.secret != first.secret
    with pytest.raises(auth_service.InvalidTotpCode):
        auth_service.complete_totp_enrolment(
            session, user=user, totp_code=pyotp.TOTP(first.secret).at(NOW), context=CONTEXT, now=NOW
        )

    _prove(session, user, second.secret)
    assert user.totp_secret == second.secret


def _prove(session: Session, user: User, secret: str) -> None:
    """Complete enrolment for a secret that was issued outside `_enrol`."""
    auth_service.complete_totp_enrolment(
        session, user=user, totp_code=pyotp.TOTP(secret).at(NOW), context=CONTEXT, now=NOW
    )


# --------------------------------------------------------------------------- re-enrolment
# The case that makes the second column worth its migration: a user who already signs in with a code
# and asks for a new secret. Nothing may stop working until the replacement is proven, because the
# window between the two calls is exactly where a user closes the tab.


def test_re_enrolment_leaves_the_existing_secret_working_until_the_new_one_is_proven(
    session: Session, totp_user
):
    user, existing = totp_user
    auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)

    assert user.totp_secret == existing
    assert _login(session, user, totp_code=pyotp.TOTP(existing).at(NOW), now=NOW)


def test_an_abandoned_re_enrolment_costs_the_user_nothing(session: Session, totp_user):
    """The whole point. With one column this is the sequence that locks a user out of their own
    account: the working secret is gone and the replacement was never scanned."""
    user, existing = totp_user
    auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT)
    session.expire_all()

    reloaded = session.get(User, user.id)
    assert reloaded is not None
    assert reloaded.is_2fa_enabled is True
    assert _login(session, reloaded, totp_code=pyotp.TOTP(existing).at(NOW), now=NOW)


def test_completing_re_enrolment_switches_the_secret_over(session: Session, totp_user):
    user, _ = totp_user
    replacement = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT).secret
    _prove(session, user, replacement)

    assert user.totp_secret == replacement
    assert user.totp_pending_secret is None
    assert _login(session, user, totp_code=pyotp.TOTP(replacement).at(NOW), now=NOW)


def test_the_replaced_secret_stops_working(session: Session, totp_user):
    """A re-enrolment a user performs because their phone was lost has to actually cut the old phone
    off, or it has achieved nothing."""
    user, existing = totp_user
    replacement = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT).secret
    _prove(session, user, replacement)

    with pytest.raises(auth_service.InvalidCredentials):
        _login(session, user, totp_code=pyotp.TOTP(existing).at(NOW), now=NOW)


def test_re_enrolment_is_audited_as_its_own_event(session: Session, totp_user):
    """Distinguishable from a first enrolment, because the two mean different things to whoever reads
    the log: one is a user completing setup, the other is a credential being replaced."""
    user, _ = totp_user
    replacement = auth_service.begin_totp_enrolment(session, user=user, context=CONTEXT).secret
    _prove(session, user, replacement)

    assert _events(session) == [
        auth_service.EVENT_TOTP_SECRET_ISSUED,
        auth_service.EVENT_2FA_REENROLLED,
    ]
    issued = session.scalars(
        select(ChangeLog).where(ChangeLog.new_value == auth_service.EVENT_TOTP_SECRET_ISSUED)
    ).one()
    assert issued.reason == auth_service.REASON_RE_ENROLMENT


# --------------------------------------------------------------------------- mandatory enrolment


def test_2fa_is_outstanding_for_an_administrator_who_has_not_enrolled(make_user):
    """Requirement 1.6. The flag is what `app.api.deps.get_enrolled_user` gates on."""
    user = make_user(role=UserRole.ADMIN)
    assert user.is_2fa_enrolment_required is True
    assert user.is_2fa_enrolment_prompted is True


def test_an_enrolled_administrator_owes_nothing(session: Session, make_user):
    user = make_user(role=UserRole.ADMIN)
    _enrol(session, user)

    assert user.is_2fa_enrolment_required is False
    assert user.is_2fa_enrolment_prompted is False


@pytest.mark.parametrize("role", [UserRole.SITE_MANAGER, UserRole.ACCOUNTING])
def test_a_console_role_is_prompted_but_not_required(make_user, role: UserRole):
    """The other half of Requirement 1.6: prompted, not blocked. A site manager who declines keeps
    working, which is why the two flags are separate rather than one."""
    user = make_user(role=role)
    assert user.is_2fa_enrolment_required is False
    assert user.is_2fa_enrolment_prompted is True


def test_an_employee_is_neither_prompted_nor_required(make_user):
    """An employee scans a QR code from a phone. A second factor there buys nothing worth asking for."""
    user = make_user(role=UserRole.EMPLOYEE)
    assert user.is_2fa_enrolment_required is False
    assert user.is_2fa_enrolment_prompted is False


# --------------------------------------------------------------------------- refresh and rotation


def test_a_refresh_token_can_be_exchanged_for_a_new_pair(session: Session, make_user):
    """Requirement 1.8."""
    user = make_user()
    first = _login(session, user, now=NOW)
    second = auth_service.refresh(session, refresh_token=first.refresh_token, context=CONTEXT, now=NOW)

    assert second.refresh_token != first.refresh_token
    assert decode_token(second.access_token, expected_type=TokenType.ACCESS).subject == user.id
    assert auth_service.resolve_access_token(session, second.access_token, now=NOW) is user


def test_rotation_is_audited_and_names_the_token_it_replaced(session: Session, make_user):
    user = make_user()
    pair = _login(session, user, now=NOW)
    rotated = decode_token(pair.refresh_token, expected_type=TokenType.REFRESH).rotation_id

    auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)
    entry = session.scalars(
        select(ChangeLog).where(ChangeLog.new_value == auth_service.EVENT_REFRESH_ROTATED)
    ).one()
    assert str(rotated) in (entry.reason or "")


def test_a_replayed_refresh_token_is_rejected(session: Session, make_user):
    """Rotation makes a refresh token single-use, so a second presentation means the token was either
    kept or stolen. Neither is a reason to issue more tokens."""
    user = make_user()
    pair = _login(session, user, now=NOW)
    auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)

    with pytest.raises(auth_service.InvalidToken):
        auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)


def test_a_replay_revokes_the_outstanding_refresh_token_too(session: Session, make_user):
    """The reason detection is worth having: on the assumption that a replay means the token leaked,
    the chain it belongs to is cut, so whoever holds the current token has to sign in again."""
    user = make_user()
    pair = _login(session, user, now=NOW)
    rotated = auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)

    with pytest.raises(auth_service.InvalidToken):
        auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)
    with pytest.raises(auth_service.InvalidToken):
        auth_service.refresh(session, refresh_token=rotated.refresh_token, context=CONTEXT, now=NOW)


def test_a_replay_is_audited_as_a_reuse_event(session: Session, make_user):
    """Distinguishable from an ordinary failure in the audit log, because it is the one event here
    that suggests a token has leaked."""
    user = make_user()
    pair = _login(session, user, now=NOW)
    auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)
    with pytest.raises(auth_service.InvalidToken):
        auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)

    assert auth_service.EVENT_REFRESH_REUSE_DETECTED in _events(session)


def test_an_access_token_is_not_accepted_as_a_refresh_token(session: Session, make_user):
    user = make_user()
    pair = _login(session, user, now=NOW)
    with pytest.raises(auth_service.InvalidToken):
        auth_service.refresh(session, refresh_token=pair.access_token, context=CONTEXT, now=NOW)


def test_a_deactivated_user_cannot_refresh(session: Session, make_user):
    user = make_user()
    pair = _login(session, user, now=NOW)
    user.is_active = False
    session.commit()

    with pytest.raises(auth_service.InvalidToken):
        auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)


# --------------------------------------------------------------------------- token version


def test_bumping_the_token_version_invalidates_an_issued_access_token(session: Session, make_user):
    """Requirement 20.8, and the mechanism behind 1.4: no server-side session store, so the only way
    to revoke early is a version the token carries and the row can move past."""
    user = make_user()
    pair = _login(session, user, now=NOW)
    assert auth_service.resolve_access_token(session, pair.access_token, now=NOW) is user

    user.token_version += 1
    session.commit()

    with pytest.raises(auth_service.InvalidToken):
        auth_service.resolve_access_token(session, pair.access_token, now=NOW)


def test_deactivation_invalidates_an_issued_access_token(session: Session, make_user):
    user = make_user()
    pair = _login(session, user, now=NOW)
    user.is_active = False
    session.commit()

    with pytest.raises(auth_service.InvalidToken):
        auth_service.resolve_access_token(session, pair.access_token, now=NOW)


def test_a_token_for_a_user_that_no_longer_exists_is_refused(session: Session, make_user):
    user = make_user()
    pair = _login(session, user, now=NOW)
    session.delete(user)
    session.commit()

    with pytest.raises(auth_service.InvalidToken):
        auth_service.resolve_access_token(session, pair.access_token, now=NOW)


def test_a_forged_access_token_is_refused(session: Session):
    with pytest.raises(auth_service.InvalidToken):
        auth_service.resolve_access_token(session, "not.a.token", now=NOW)


# --------------------------------------------------------------------------- logout


def test_logout_invalidates_the_access_token(session: Session, make_user):
    user = make_user()
    pair = _login(session, user, now=NOW)
    auth_service.logout(session, user=user, context=CONTEXT)

    with pytest.raises(auth_service.InvalidToken):
        auth_service.resolve_access_token(session, pair.access_token, now=NOW)


def test_logout_invalidates_the_refresh_token(session: Session, make_user):
    """A logout that left a 7-day refresh token working would not be a logout."""
    user = make_user()
    pair = _login(session, user, now=NOW)
    auth_service.logout(session, user=user, context=CONTEXT)

    with pytest.raises(auth_service.InvalidToken):
        auth_service.refresh(session, refresh_token=pair.refresh_token, context=CONTEXT, now=NOW)


def test_logout_is_audited(session: Session, make_user):
    user = make_user()
    _login(session, user, now=NOW)
    auth_service.logout(session, user=user, context=CONTEXT)

    assert _events(session) == [auth_service.EVENT_LOGIN_SUCCEEDED, auth_service.EVENT_LOGOUT]


def test_signing_in_again_after_a_logout_works(session: Session, make_user):
    user = make_user()
    _login(session, user, now=NOW)
    auth_service.logout(session, user=user, context=CONTEXT)

    pair = _login(session, user, now=NOW)
    assert auth_service.resolve_access_token(session, pair.access_token, now=NOW) is user


# --------------------------------------------------------------------------- password storage


def test_no_password_reaches_the_database_in_plaintext(session: Session, make_user):
    """Requirement 1.3, checked against the column rather than the code path that wrote it."""
    make_user(password_hash=hash_password("s3cret-passphrase"))
    stored = list(session.connection().exec_driver_sql("SELECT password_hash FROM users").scalars())
    assert stored
    assert all(value.startswith("$2b$") for value in stored)
    assert all("s3cret-passphrase" not in value for value in stored)


# --------------------------------------------------------------------------- self-service password change
# The first-use obligation of Requirement 1, from the service's side. The claims worth pinning: a
# change clears the flag and ends other sessions, an unchanged or wrong-current password is refused
# without touching anything, and the change is audited.

NEW_PASSWORD = "a-brand-new-passphrase"


def test_changing_the_password_clears_the_obligation_and_replaces_the_hash(session: Session, make_user):
    user = make_user(role=UserRole.SITE_MANAGER, must_change_password=True)
    original_hash = user.password_hash

    auth_service.change_password(
        session,
        user=user,
        current_password=DEFAULT_PASSWORD,
        new_password=NEW_PASSWORD,
        context=CONTEXT,
    )

    assert user.must_change_password is False
    assert user.password_hash != original_hash
    # The new password is the one that now verifies; the old one no longer does.
    from app.core.security import verify_password

    assert verify_password(NEW_PASSWORD, user.password_hash)
    assert not verify_password(DEFAULT_PASSWORD, user.password_hash)


def test_changing_the_password_bumps_the_token_version(session: Session, make_user):
    """Ends every other session on the old password, the same immediacy the admin reset gives."""
    user = make_user(role=UserRole.SITE_MANAGER)
    before = user.token_version

    auth_service.change_password(
        session,
        user=user,
        current_password=DEFAULT_PASSWORD,
        new_password=NEW_PASSWORD,
        context=CONTEXT,
    )
    assert user.token_version == before + 1


def test_a_wrong_current_password_is_refused_distinctly(session: Session, make_user):
    user = make_user(role=UserRole.SITE_MANAGER, must_change_password=True)
    with pytest.raises(auth_service.CurrentPasswordIncorrect) as error:
        auth_service.change_password(
            session,
            user=user,
            current_password="not-my-password",
            new_password=NEW_PASSWORD,
            context=CONTEXT,
        )
    assert error.value.code == "current_password_incorrect"
    # Nothing changed: the obligation still stands.
    assert user.must_change_password is True


def test_a_new_password_equal_to_the_current_one_is_refused(session: Session, make_user):
    user = make_user(role=UserRole.SITE_MANAGER, must_change_password=True)
    with pytest.raises(auth_service.NewPasswordMustDiffer) as error:
        auth_service.change_password(
            session,
            user=user,
            current_password=DEFAULT_PASSWORD,
            new_password=DEFAULT_PASSWORD,
            context=CONTEXT,
        )
    assert error.value.code == "new_password_must_differ"
    assert user.must_change_password is True


def test_a_too_short_new_password_is_refused(session: Session, make_user):
    user = make_user(role=UserRole.SITE_MANAGER)
    with pytest.raises(auth_service.NewPasswordTooShort):
        auth_service.change_password(
            session,
            user=user,
            current_password=DEFAULT_PASSWORD,
            new_password="short",
            context=CONTEXT,
        )


def test_an_employee_may_set_a_four_to_eight_digit_pin(session: Session, make_user):
    """An employee's password is a 4-8 digit numeric PIN, matching its numeric username."""
    from app.core.security import verify_password

    for pin in ("1357", "12345678"):
        user = make_user(role=UserRole.EMPLOYEE, must_change_password=True)
        auth_service.change_password(
            session,
            user=user,
            current_password=DEFAULT_PASSWORD,
            new_password=pin,
            context=CONTEXT,
        )
        assert user.must_change_password is False
        assert verify_password(pin, user.password_hash)


def test_an_employee_pin_shorter_than_four_digits_is_refused(session: Session, make_user):
    user = make_user(role=UserRole.EMPLOYEE)
    with pytest.raises(auth_service.NewPasswordInvalid):
        auth_service.change_password(
            session,
            user=user,
            current_password=DEFAULT_PASSWORD,
            new_password="123",
            context=CONTEXT,
        )


def test_an_employee_pin_longer_than_eight_digits_is_refused(session: Session, make_user):
    user = make_user(role=UserRole.EMPLOYEE)
    with pytest.raises(auth_service.NewPasswordInvalid):
        auth_service.change_password(
            session,
            user=user,
            current_password=DEFAULT_PASSWORD,
            new_password="123456789",
            context=CONTEXT,
        )


def test_an_employee_pin_with_non_digits_is_refused(session: Session, make_user):
    """"Digits" means 0-9 only, so a 4-8 character password with a letter is refused."""
    user = make_user(role=UserRole.EMPLOYEE)
    with pytest.raises(auth_service.NewPasswordInvalid):
        auth_service.change_password(
            session,
            user=user,
            current_password=DEFAULT_PASSWORD,
            new_password="12ab",
            context=CONTEXT,
        )


def test_a_console_role_still_requires_eight_characters(session: Session, make_user):
    """The employee PIN rule does not weaken the console roles: a 4-digit password is still refused."""
    user = make_user(role=UserRole.ACCOUNTING)
    with pytest.raises(auth_service.NewPasswordTooShort):
        auth_service.change_password(
            session,
            user=user,
            current_password=DEFAULT_PASSWORD,
            new_password="1234",
            context=CONTEXT,
        )


def test_changing_the_password_is_audited(session: Session, make_user):
    user = make_user(role=UserRole.SITE_MANAGER)
    auth_service.change_password(
        session,
        user=user,
        current_password=DEFAULT_PASSWORD,
        new_password=NEW_PASSWORD,
        context=CONTEXT,
    )

    row = session.scalars(
        select(ChangeLog).where(ChangeLog.field == "password", ChangeLog.entity_id == user.id)
    ).one()
    assert row.new_value == "changed"
    assert row.reason == auth_service.REASON_SELF_SERVICE_CHANGE
    assert row.changed_by_user_id == user.id
