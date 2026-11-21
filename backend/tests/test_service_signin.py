"""Example tests for the sign-in and change-password services (Requirements 5 and 6).

Runs on the shared SQLite `session` fixture with an injected clock. A spy around `verify_password` counts
password-hash verifications: R5.5 asks for exactly one per attempt that reaches the credential check, and
none for an attempt that is refused before it. Customers are inserted with a hash computed once per test
session, so the only bcrypt work in a test is the service's own verification.

What only PostgreSQL can show (the advisory lock under parallel guesses) is not covered here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password, verify_password
from app.service_tickets import customer_token
from app.service_tickets.config import ServiceConfig
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import Customer, SigninFailure
from app.service_tickets.services import signin
from app.service_tickets.services.signin import (
    InvalidCredentials,
    InvalidPasswordFormat,
    InvalidPhoneInput,
    Locked,
    PasswordChanged,
    PasswordNotChanged,
    Success,
    change_password,
    sign_in,
)

PHONE = "0505556677"
OTHER_PHONE = "0521234567"
UNREGISTERED_PHONE = "0549998888"
PASSWORD = "4321"
WRONG_PASSWORD = "9999"
#: The injected clock starts at the real present, whole seconds, so a minted token is also unexpired for
#: `decode_customer_token`, which reads the real clock.
START = datetime.now(UTC).replace(microsecond=0)


def _config() -> ServiceConfig:
    """Every setting unset, so every default applies: 5 failures in 900 seconds, 8-hour tokens."""
    names = (
        "service_tickets_enabled service_customer_token_ttl_seconds service_signin_rate_max "
        "service_signin_rate_window_seconds service_submit_rate_max service_submit_rate_window_seconds "
        "service_request_rate_max service_request_rate_window_seconds service_signin_lock_attempts "
        "service_signin_lock_seconds service_client_ip_header service_trusted_proxy_cidrs "
        "service_accessibility_contact"
    ).split()
    return ServiceConfig.from_settings(SimpleNamespace(**dict.fromkeys(names, "")))  # type: ignore[arg-type]


CONFIG = _config()


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def verifications(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every `verify_password` call the service makes, as (password, hash) pairs."""
    calls: list[tuple[str, str]] = []

    def spy(password: str, stored_hash: str) -> bool:
        calls.append((password, stored_hash))
        return verify_password(password, stored_hash)

    monkeypatch.setattr(signin, "verify_password", spy)
    return calls


@pytest.fixture
def make_customer(session: Session, password_hash: str):
    def _make(phone: str = PHONE, **overrides: object) -> Customer:
        fields: dict[str, object] = {
            "phone": phone,
            "full_name": "Avi Cohen",
            "full_name_key": fold_key("Avi Cohen"),
            "password_hash": password_hash,
            "must_change_password": False,
        }
        fields.update(overrides)
        row = Customer(**fields)
        session.add(row)
        session.flush()
        return row

    return _make


def _failures(session: Session, phone: str | None = None) -> int:
    query = select(func.count()).select_from(SigninFailure)
    if phone is not None:
        query = query.where(SigninFailure.phone == phone)
    return session.execute(query).scalar_one()


def _attempt(session: Session, now: datetime, phone: str = PHONE, password: object = WRONG_PASSWORD):
    return sign_in(session, phone_raw=phone, password_raw=password, now=now, settings=CONFIG)


def _fail_times(session: Session, count: int, phone: str = PHONE) -> datetime:
    """Fail `count` sign-ins one second apart from START and return the time of the last one."""
    last = START
    for index in range(count):
        last = START + timedelta(seconds=index)
        assert isinstance(_attempt(session, last, phone), InvalidCredentials)
    return last


# --------------------------------------------------------------------------- sign in: success


def test_success_returns_an_eight_hour_token_and_the_flag(
    session: Session, make_customer, verifications: list
) -> None:
    customer = make_customer(must_change_password=True, token_version=3)

    outcome = _attempt(session, START, password=PASSWORD)

    assert isinstance(outcome, Success)
    assert outcome.customer_id == customer.id
    assert outcome.must_change_password is True
    assert outcome.expires_in == 28800
    claims = customer_token.decode_customer_token(outcome.access_token)
    assert claims.customer_id == customer.id
    assert claims.version == 3
    assert claims.expires_at == START + timedelta(hours=8)
    assert len(verifications) == 1
    assert verifications[0][1] == customer.password_hash


def test_phone_spellings_reach_the_same_customer(session: Session, make_customer) -> None:
    customer = make_customer()

    outcome = _attempt(session, START, phone="+972 50-555-6677", password=PASSWORD)

    assert isinstance(outcome, Success)
    assert outcome.customer_id == customer.id


def test_success_deletes_the_failure_rows_of_that_phone_only(session: Session, make_customer) -> None:
    make_customer()
    _fail_times(session, 3)
    _fail_times(session, 2, OTHER_PHONE)

    assert isinstance(_attempt(session, START + timedelta(minutes=1), password=PASSWORD), Success)

    assert _failures(session, PHONE) == 0
    assert _failures(session, OTHER_PHONE) == 2


# --------------------------------------------------------------------------- sign in: failures


def test_the_four_failure_classes_return_equal_values_and_cost_one_verification_each(
    session: Session, make_customer, verifications: list
) -> None:
    make_customer(PHONE)
    make_customer(OTHER_PHONE, is_active=False)

    wrong_password = _attempt(session, START, PHONE, WRONG_PASSWORD)
    unregistered = _attempt(session, START, UNREGISTERED_PHONE, PASSWORD)
    inactive_with_right_password = _attempt(session, START, OTHER_PHONE, PASSWORD)
    malformed_password = _attempt(session, START, PHONE, "12345")

    assert wrong_password == unregistered == inactive_with_right_password == malformed_password
    assert isinstance(wrong_password, InvalidCredentials)
    assert len(verifications) == 4


@pytest.mark.parametrize("password", [None, "", "123", "12345", " 123", "١٢٣٤", 1234, ["1234"], "1234\n"])
def test_a_password_that_is_not_four_ascii_digits_is_never_compared_with_the_stored_hash(
    session: Session, make_customer, verifications: list, password: object
) -> None:
    customer = make_customer()

    outcome = _attempt(session, START, password=password)

    assert isinstance(outcome, InvalidCredentials)
    assert len(verifications) == 1
    assert verifications[0][1] != customer.password_hash


def test_every_failure_class_records_one_row_for_the_normalized_phone(
    session: Session, make_customer
) -> None:
    make_customer(PHONE)
    make_customer(OTHER_PHONE, is_active=False)

    _attempt(session, START, "050-555-6677", WRONG_PASSWORD)
    _attempt(session, START, UNREGISTERED_PHONE, PASSWORD)
    _attempt(session, START, OTHER_PHONE, PASSWORD)

    assert _failures(session, PHONE) == 1
    assert _failures(session, UNREGISTERED_PHONE) == 1
    assert _failures(session, OTHER_PHONE) == 1


@pytest.mark.parametrize("phone", [None, "", "050-555-667", "05055566778", "abc", "٠٥٠٥٥٥٦٦٧٧"])
def test_an_invalid_phone_is_refused_without_verification_or_a_row(
    session: Session, make_customer, verifications: list, phone: str | None
) -> None:
    make_customer()

    outcome = sign_in(session, phone_raw=phone, password_raw=PASSWORD, now=START, settings=CONFIG)

    assert isinstance(outcome, InvalidPhoneInput)
    assert verifications == []
    assert _failures(session) == 0


# --------------------------------------------------------------------------- sign in: the lock


def test_five_failures_lock_the_phone_even_for_the_correct_password(
    session: Session, make_customer, verifications: list
) -> None:
    make_customer()
    last = _fail_times(session, 5)
    verified_before = len(verifications)

    outcome = _attempt(session, last + timedelta(seconds=10), password=PASSWORD)

    assert outcome == Locked(retry_after=890)
    assert len(verifications) == verified_before
    assert _failures(session, PHONE) == 5


def test_four_failures_do_not_lock(session: Session, make_customer) -> None:
    make_customer()
    last = _fail_times(session, 4)

    assert isinstance(_attempt(session, last + timedelta(seconds=1), password=PASSWORD), Success)


def test_the_lock_ends_exactly_fifteen_minutes_after_the_fifth_failure(
    session: Session, make_customer
) -> None:
    make_customer()
    fifth = _fail_times(session, 5)

    assert _attempt(session, fifth + timedelta(seconds=1), password=PASSWORD) == Locked(899)
    assert _attempt(session, fifth + timedelta(seconds=899), password=PASSWORD) == Locked(1)
    assert isinstance(_attempt(session, fifth + timedelta(seconds=900), password=PASSWORD), Success)


def test_five_failures_spread_over_fourteen_minutes_lock_and_the_lock_runs_from_the_fifth(
    session: Session, make_customer
) -> None:
    make_customer()
    _fail_times(session, 4)
    fifth = START + timedelta(minutes=14)
    assert isinstance(_attempt(session, fifth), InvalidCredentials)

    assert _attempt(session, fifth + timedelta(seconds=60), password=PASSWORD) == Locked(840)
    assert isinstance(_attempt(session, fifth + timedelta(seconds=900), password=PASSWORD), Success)


def test_a_fifth_failure_after_the_first_has_left_the_fifteen_minute_window_does_not_lock(
    session: Session, make_customer
) -> None:
    make_customer()
    _fail_times(session, 4)
    fifth = START + timedelta(seconds=901)
    assert isinstance(_attempt(session, fifth), InvalidCredentials)

    assert isinstance(_attempt(session, fifth + timedelta(seconds=1), password=PASSWORD), Success)


def test_retry_after_rounds_a_partial_second_up(session: Session, make_customer) -> None:
    make_customer()
    fifth = _fail_times(session, 5)

    outcome = _attempt(session, fifth + timedelta(seconds=10, milliseconds=1), password=PASSWORD)

    assert outcome == Locked(retry_after=890)


def test_locked_requests_record_nothing_and_do_not_extend_the_lock(session: Session, make_customer) -> None:
    make_customer()
    fifth = _fail_times(session, 5)

    for offset in (1, 100, 500):
        assert isinstance(_attempt(session, fifth + timedelta(seconds=offset)), Locked)

    assert _failures(session, PHONE) == 5
    assert isinstance(_attempt(session, fifth + timedelta(seconds=900), password=PASSWORD), Success)


def test_counting_restarts_from_zero_when_a_lock_ends(session: Session, make_customer) -> None:
    make_customer()
    fifth = _fail_times(session, 5)
    after = fifth + timedelta(seconds=900)

    for index in range(4):
        assert isinstance(_attempt(session, after + timedelta(seconds=index)), InvalidCredentials)
    assert isinstance(_attempt(session, after + timedelta(seconds=10), password=PASSWORD), Success)


def test_an_unregistered_phone_locks_the_same_way(session: Session) -> None:
    fifth = _fail_times(session, 5, UNREGISTERED_PHONE)

    outcome = _attempt(session, fifth + timedelta(seconds=1), UNREGISTERED_PHONE, PASSWORD)

    assert outcome == Locked(retry_after=899)


def test_failures_older_than_the_window_do_not_count(session: Session, make_customer) -> None:
    make_customer()
    _fail_times(session, 4)

    later = START + timedelta(seconds=1000)

    assert isinstance(_attempt(session, later), InvalidCredentials)
    assert isinstance(_attempt(session, later + timedelta(seconds=1), password=PASSWORD), Success)


def test_a_failure_purges_rows_older_than_one_hour(session: Session) -> None:
    _fail_times(session, 2, OTHER_PHONE)

    _attempt(session, START + timedelta(hours=1, seconds=5), UNREGISTERED_PHONE)

    assert _failures(session, OTHER_PHONE) == 0
    assert _failures(session, UNREGISTERED_PHONE) == 1


def test_the_lock_is_per_phone(session: Session, make_customer) -> None:
    make_customer(PHONE)
    make_customer(OTHER_PHONE)
    fifth = _fail_times(session, 5, PHONE)

    assert isinstance(_attempt(session, fifth, OTHER_PHONE, PASSWORD), Success)


def test_lock_state_survives_a_new_session(sqlite_engine: Engine, password_hash: str) -> None:
    factory = sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as first:
        first.add(
            Customer(
                phone=PHONE,
                full_name="Avi Cohen",
                full_name_key=fold_key("Avi Cohen"),
                password_hash=password_hash,
                must_change_password=False,
            )
        )
        fifth = _fail_times(first, 5)
        first.commit()

    with factory() as second:
        assert _attempt(second, fifth + timedelta(seconds=1), password=PASSWORD) == Locked(899)


# --------------------------------------------------------------------------- change password


def _change(session: Session, customer: Customer, current: object, new: object, now: datetime = START):
    return change_password(
        session,
        customer_id=customer.id,
        current_password_raw=current,
        new_password_raw=new,
        now=now,
        settings=CONFIG,
    )


def test_change_password_stores_the_hash_clears_the_flag_and_revokes_old_tokens(
    session: Session, make_customer, password_hash: str
) -> None:
    customer = make_customer(must_change_password=True, token_version=4)
    old_token = customer_token.mint_customer_token(customer.id, 4, START, 28800)

    outcome = _change(session, customer, PASSWORD, "2468")

    assert isinstance(outcome, PasswordChanged)
    row = session.get(Customer, customer.id, populate_existing=True)
    assert row is not None
    assert row.password_hash != password_hash
    assert verify_password("2468", row.password_hash)
    assert row.must_change_password is False
    assert row.token_version == 5
    claims = customer_token.decode_customer_token(outcome.access_token)
    assert claims.version == 5
    assert claims.expires_at == START + timedelta(hours=8)
    assert customer_token.decode_customer_token(old_token).version != row.token_version
    assert outcome.must_change_password is False
    assert outcome.expires_in == 28800


def test_the_new_password_can_sign_in_and_the_old_one_cannot(session: Session, make_customer) -> None:
    customer = make_customer()
    assert isinstance(_change(session, customer, PASSWORD, "2468"), PasswordChanged)

    assert isinstance(_attempt(session, START, password=PASSWORD), InvalidCredentials)
    assert isinstance(_attempt(session, START + timedelta(seconds=1), password="2468"), Success)


def test_a_new_password_of_the_wrong_format_is_refused_without_verification_or_a_count(
    session: Session, make_customer, verifications: list
) -> None:
    customer = make_customer()

    for bad in (None, "", "123", "12345", " 123", "١٢٣٤", "1234\n", 1234):
        assert isinstance(_change(session, customer, WRONG_PASSWORD, bad), InvalidPasswordFormat)

    assert verifications == []
    assert _failures(session) == 0


def test_a_wrong_current_password_is_counted_and_changes_nothing(
    session: Session, make_customer, verifications: list, password_hash: str
) -> None:
    customer = make_customer(must_change_password=True)

    outcome = _change(session, customer, WRONG_PASSWORD, "2468")

    assert isinstance(outcome, InvalidCredentials)
    assert len(verifications) == 1
    assert _failures(session, PHONE) == 1
    row = session.get(Customer, customer.id, populate_existing=True)
    assert row is not None
    assert (row.password_hash, row.must_change_password, row.token_version) == (password_hash, True, 1)


@pytest.mark.parametrize("current", [None, "", "12345", 4321])
def test_a_missing_or_malformed_current_password_is_a_counted_401_with_one_verification(
    session: Session, make_customer, verifications: list, current: object
) -> None:
    customer = make_customer()

    assert isinstance(_change(session, customer, current, "2468"), InvalidCredentials)

    assert len(verifications) == 1
    assert _failures(session, PHONE) == 1


def test_the_format_check_comes_before_the_current_password_check(
    session: Session, make_customer
) -> None:
    customer = make_customer()

    outcome = _change(session, customer, WRONG_PASSWORD, "12a4")

    assert isinstance(outcome, InvalidPasswordFormat)
    assert _failures(session) == 0


@pytest.mark.parametrize("new", ["1234", PASSWORD])
def test_a_new_password_equal_to_the_initial_or_the_current_one_is_refused(
    session: Session, make_customer, password_hash: str, new: str
) -> None:
    customer = make_customer(must_change_password=True)

    outcome = _change(session, customer, PASSWORD, new)

    assert isinstance(outcome, PasswordNotChanged)
    row = session.get(Customer, customer.id, populate_existing=True)
    assert row is not None
    assert (row.password_hash, row.must_change_password, row.token_version) == (password_hash, True, 1)


def test_the_not_changed_check_only_runs_after_the_current_password_matched(
    session: Session, make_customer
) -> None:
    customer = make_customer()

    outcome = _change(session, customer, WRONG_PASSWORD, "1234")

    assert isinstance(outcome, InvalidCredentials)


def test_a_matching_current_password_resets_the_failure_count_even_when_the_change_is_refused(
    session: Session, make_customer
) -> None:
    customer = make_customer()
    _fail_times(session, 3)
    _fail_times(session, 1, OTHER_PHONE)

    outcome = _change(session, customer, PASSWORD, "1234", START + timedelta(seconds=10))

    assert isinstance(outcome, PasswordNotChanged)

    assert _failures(session, PHONE) == 0
    assert _failures(session, OTHER_PHONE) == 1


def test_a_locked_phone_gets_429_before_any_password_is_inspected(
    session: Session, make_customer, verifications: list, password_hash: str
) -> None:
    customer = make_customer(must_change_password=True)
    fifth = _fail_times(session, 5)
    verified_before = len(verifications)

    outcome = _change(session, customer, PASSWORD, "bad", fifth + timedelta(seconds=30))

    assert outcome == Locked(retry_after=870)
    assert len(verifications) == verified_before
    assert _failures(session, PHONE) == 5
    row = session.get(Customer, customer.id, populate_existing=True)
    assert row is not None
    assert (row.password_hash, row.must_change_password, row.token_version) == (password_hash, True, 1)


def test_failed_changes_and_failed_sign_ins_share_one_count(session: Session, make_customer) -> None:
    customer = make_customer()
    _fail_times(session, 3)
    for second in (5, 6):
        outcome = _change(session, customer, WRONG_PASSWORD, "2468", START + timedelta(seconds=second))
        assert isinstance(outcome, InvalidCredentials)

    assert isinstance(_attempt(session, START + timedelta(seconds=7), password=PASSWORD), Locked)


def test_an_inactive_or_missing_customer_gets_the_credential_failure(
    session: Session, make_customer, verifications: list
) -> None:
    inactive = make_customer(is_active=False)

    assert isinstance(_change(session, inactive, PASSWORD, "2468"), InvalidCredentials)
    assert len(verifications) == 1
    assert verifications[0][1] != inactive.password_hash

    ghost = Customer(id=uuid.uuid4(), phone=OTHER_PHONE, full_name="x", full_name_key="x", password_hash="x")
    assert isinstance(_change(session, ghost, PASSWORD, "2468"), InvalidCredentials)
    assert len(verifications) == 2


# --------------------------------------------------------------------------- logging


def test_neither_function_logs_a_phone_or_a_password(
    session: Session, make_customer, caplog: pytest.LogCaptureFixture
) -> None:
    customer = make_customer()
    with caplog.at_level(logging.DEBUG):
        _attempt(session, START, PHONE, WRONG_PASSWORD)
        _attempt(session, START, PHONE, PASSWORD)
        _change(session, customer, PASSWORD, "2468")

    text = caplog.text
    for secret in (PHONE, PASSWORD, WRONG_PASSWORD, "2468"):
        assert secret not in text
