"""The scheduled notification jobs (Requirement 14.1, 14.2, 14.5, 14.6, 4.4–4.6, 21.6).

Task 30's jobs raise the notifications of Requirement 14. The properties the task calls out — that
every job is idempotent across repeated runs (14.6), that the recipients and their scope are correct
(14.1, 14.2, 14.5), and that the content is language-neutral so it renders in the recipient's language
(21.6) — are exactly what these tests pin.

Every job is a plain function taking a session and an explicit clock, so the tests drive them at a
chosen instant against the in-memory SQLite session the suite provides. The `dedupe_key` unique
constraint that makes idempotency work is enforced on SQLite as well as PostgreSQL, so the "run twice,
raise once" claim is tested on the same mechanism production relies on. The make-* helpers mirror
`test_missing_reports_api.py`.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee
from app.models.notification import Notification
from app.models.site import EmployeeSite, Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import AppLanguage, User, UserRole
from app.models.user_site import UserSite
from app.services import jobs as jobs_service

_TZ = ZoneInfo("Asia/Jerusalem")
_PASSPORT = itertools.count(1)


# --------------------------------------------------------------------------- builders


def _make_employee(session: Session, *, name: str = "Worker") -> Employee:
    passport = f"J{next(_PASSPORT):07d}"
    employee = Employee(
        full_name=name,
        full_name_en=f"{name} EN",
        passport_number=passport,
        passport_number_hash=passport,
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.commit()
    return employee


def _make_client(session: Session) -> Client:
    client = Client(name="Acme")
    session.add(client)
    session.commit()
    return client


def _make_site(session: Session, *, number: str, manager: User | None = None) -> Site:
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=_make_client(session).id,
        manager_user_id=manager.id if manager is not None else None,
        qr_token=f"tok-{number}-{next(_PASSPORT)}",
        qr_token_version=1,
    )
    session.add(site)
    session.commit()
    return site


def _make_user(
    session: Session,
    *,
    role: UserRole,
    username: str,
    employee: Employee | None = None,
    language: AppLanguage = AppLanguage.ENGLISH,
) -> User:
    user = User(
        username=username,
        password_hash="x",
        role=role,
        is_active=True,
        language=language,
        employee_id=employee.id if employee is not None else None,
    )
    session.add(user)
    session.commit()
    return user


def _open_entry(
    session: Session, *, employee: Employee, site: Site, check_in: datetime
) -> TimeEntry:
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=check_in.astimezone(_TZ).date(),
        check_in_at=check_in.astimezone(UTC),
        check_out_at=None,
        total_minutes=None,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=TimeEntryStatus.DRAFT,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


def _assign_day(session: Session, *, employee: Employee, site: Site, day: date) -> None:
    session.add(
        EmployeeSite(
            employee_id=employee.id, site_id=site.id, assigned_from=day, assigned_to=day
        )
    )
    session.commit()


def _notifications_of(session: Session, type_: str) -> list[Notification]:
    return list(
        session.scalars(select(Notification).where(Notification.type == type_))
    )


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Notification)) or 0


# ===================================================================== no-checkout reminder (14.1)


def test_no_checkout_reminder_goes_to_the_employee(session: Session):
    """Requirement 14.1: an employee still open at the cutoff is reminded — the employee, not a manager."""
    employee = _make_employee(session)
    login = _make_user(session, role=UserRole.EMPLOYEE, username="emp1", employee=employee)
    site = _make_site(session, number="A")
    _open_entry(
        session, employee=employee, site=site, check_in=datetime(2025, 8, 4, 8, 0, tzinfo=_TZ)
    )

    result = jobs_service.run_no_checkout_reminder(
        session, now=datetime(2025, 8, 4, 18, 0, tzinfo=_TZ)
    )

    reminders = _notifications_of(session, jobs_service.NOTIFICATION_TYPE_NO_CHECKOUT)
    assert result.raised == 1
    assert len(reminders) == 1
    assert reminders[0].recipient_user_id == login.id
    assert reminders[0].title_key == jobs_service.TITLE_KEY_NO_CHECKOUT
    # Language-neutral: a key and parameters, never a rendered sentence (Requirement 21.6).
    assert reminders[0].body_params["site_name"] == site.name


def test_no_checkout_reminder_is_idempotent(session: Session):
    """Requirement 14.6: running the reminder twice raises one notification, not two."""
    employee = _make_employee(session)
    _make_user(session, role=UserRole.EMPLOYEE, username="emp1", employee=employee)
    site = _make_site(session, number="A")
    _open_entry(
        session, employee=employee, site=site, check_in=datetime(2025, 8, 4, 8, 0, tzinfo=_TZ)
    )

    now = datetime(2025, 8, 4, 18, 0, tzinfo=_TZ)
    first = jobs_service.run_no_checkout_reminder(session, now=now)
    second = jobs_service.run_no_checkout_reminder(session, now=now)

    assert first.raised == 1
    assert second.raised == 0
    assert len(_notifications_of(session, jobs_service.NOTIFICATION_TYPE_NO_CHECKOUT)) == 1


def test_no_checkout_reminder_skips_employee_without_login(session: Session):
    """An employee with no login has nobody to notify; the job raises nothing for them."""
    employee = _make_employee(session)  # no User row linked
    site = _make_site(session, number="A")
    _open_entry(
        session, employee=employee, site=site, check_in=datetime(2025, 8, 4, 8, 0, tzinfo=_TZ)
    )

    result = jobs_service.run_no_checkout_reminder(
        session, now=datetime(2025, 8, 4, 18, 0, tzinfo=_TZ)
    )
    assert result.raised == 0
    assert _count(session) == 0


# ===================================================================== over-maximum open shift (14.2)


def test_over_maximum_alerts_manager_and_flags_entry(session: Session):
    """Requirement 14.2: an open shift past the maximum alerts the manager and flags the entry."""
    manager = _make_user(session, role=UserRole.SITE_MANAGER, username="mgr1")
    site = _make_site(session, number="A", manager=manager)
    employee = _make_employee(session, name="Dana")
    # Open 17 hours before now, past the 16h default maximum.
    check_in = datetime(2025, 8, 4, 1, 0, tzinfo=_TZ)
    entry = _open_entry(session, employee=employee, site=site, check_in=check_in)

    result = jobs_service.run_over_maximum_open_shift(
        session, now=datetime(2025, 8, 4, 18, 0, tzinfo=_TZ)
    )

    alerts = _notifications_of(session, jobs_service.NOTIFICATION_TYPE_OVER_MAXIMUM)
    assert result.raised == 1
    assert len(alerts) == 1
    assert alerts[0].recipient_user_id == manager.id
    # The alert names employee, site, date (Requirement 14.4) and links to the entry.
    assert alerts[0].body_params["employee_name"] == "Dana"
    assert alerts[0].body_params["site_name"] == site.name
    assert alerts[0].related_entity_type == "time_entries"
    assert alerts[0].related_entity_id == entry.id
    # The entry is flagged as an anomaly (Requirement 14.2).
    session.refresh(entry)
    assert jobs_service.FLAG_OVER_MAXIMUM_OPEN in entry.flags


def test_over_maximum_ignores_a_shift_under_the_maximum(session: Session):
    """A shift open less than the maximum is neither alerted nor flagged."""
    manager = _make_user(session, role=UserRole.SITE_MANAGER, username="mgr1")
    site = _make_site(session, number="A", manager=manager)
    employee = _make_employee(session)
    # Open only 4 hours before now.
    entry = _open_entry(
        session, employee=employee, site=site, check_in=datetime(2025, 8, 4, 14, 0, tzinfo=_TZ)
    )

    result = jobs_service.run_over_maximum_open_shift(
        session, now=datetime(2025, 8, 4, 18, 0, tzinfo=_TZ)
    )
    assert result.raised == 0
    session.refresh(entry)
    assert jobs_service.FLAG_OVER_MAXIMUM_OPEN not in entry.flags


def test_over_maximum_is_idempotent(session: Session):
    """Requirement 14.6: a shift open across two runs is alerted once and flagged once."""
    manager = _make_user(session, role=UserRole.SITE_MANAGER, username="mgr1")
    site = _make_site(session, number="A", manager=manager)
    employee = _make_employee(session)
    entry = _open_entry(
        session, employee=employee, site=site, check_in=datetime(2025, 8, 4, 1, 0, tzinfo=_TZ)
    )

    now = datetime(2025, 8, 4, 18, 0, tzinfo=_TZ)
    first = jobs_service.run_over_maximum_open_shift(session, now=now)
    second = jobs_service.run_over_maximum_open_shift(session, now=now)

    assert first.raised == 1
    assert second.raised == 0
    assert len(_notifications_of(session, jobs_service.NOTIFICATION_TYPE_OVER_MAXIMUM)) == 1
    session.refresh(entry)
    assert entry.flags.count(jobs_service.FLAG_OVER_MAXIMUM_OPEN) == 1


def test_over_maximum_flags_even_when_site_has_no_manager(session: Session):
    """A site with no manager still gets the anomaly flag; there is just nobody to notify."""
    site = _make_site(session, number="A", manager=None)
    employee = _make_employee(session)
    entry = _open_entry(
        session, employee=employee, site=site, check_in=datetime(2025, 8, 4, 1, 0, tzinfo=_TZ)
    )

    result = jobs_service.run_over_maximum_open_shift(
        session, now=datetime(2025, 8, 4, 18, 0, tzinfo=_TZ)
    )
    assert result.raised == 0
    session.refresh(entry)
    assert jobs_service.FLAG_OVER_MAXIMUM_OPEN in entry.flags


# ===================================================================== weekly missing summary (14.5)


def test_weekly_summary_recipients_and_scope(session: Session):
    """Requirement 14.5: each manager is summarised their sites; each admin, all sites.

    An employee is expected at two sites and records nothing at either (both-missing). The manager runs
    Site A only. The admin gets a summary covering everything; the manager gets a summary and per-
    finding alerts for Site A only, never Site B.
    """
    admin = _make_user(session, role=UserRole.ADMIN, username="admin1")
    manager = _make_user(session, role=UserRole.SITE_MANAGER, username="mgr1")
    employee = _make_employee(session, name="Dana")
    site_a = _make_site(session, number="A")
    site_b = _make_site(session, number="B")
    session.add(UserSite(user_id=manager.id, site_id=site_a.id))
    session.commit()
    _assign_day(session, employee=employee, site=site_a, day=date(2025, 8, 4))
    _assign_day(session, employee=employee, site=site_b, day=date(2025, 8, 4))

    result = jobs_service.run_weekly_missing_report_summary(
        session, week_start=date(2025, 8, 4), week_end=date(2025, 8, 10)
    )
    assert result.raised > 0

    # Admin: one weekly summary covering all.
    admin_summaries = [
        n
        for n in _notifications_of(session, jobs_service.NOTIFICATION_TYPE_MISSING_WEEKLY)
        if n.recipient_user_id == admin.id
    ]
    assert len(admin_summaries) == 1
    assert admin_summaries[0].body_params["scope"] == "all"
    assert admin_summaries[0].body_params["finding_count"] == 2

    # Manager: one weekly summary, scoped to their site.
    manager_summaries = [
        n
        for n in _notifications_of(session, jobs_service.NOTIFICATION_TYPE_MISSING_WEEKLY)
        if n.recipient_user_id == manager.id
    ]
    assert len(manager_summaries) == 1
    assert manager_summaries[0].body_params["finding_count"] == 1

    # Manager: per-finding alerts, and only for Site A (Requirement 14.4).
    manager_alerts = [
        n
        for n in _notifications_of(session, jobs_service.NOTIFICATION_TYPE_MISSING_MANAGER)
        if n.recipient_user_id == manager.id
    ]
    assert len(manager_alerts) == 1
    alert = manager_alerts[0]
    assert alert.body_params["site_id"] == str(site_a.id)
    assert alert.body_params["employee_name"] == "Dana"
    assert alert.body_params["missing"] == "both"
    # Links to the completion form via the employee (Requirement 14.4).
    assert alert.related_entity_type == "employees"
    assert alert.related_entity_id == employee.id


def test_weekly_summary_is_idempotent(session: Session):
    """Requirement 14.6: a second weekly run over the same week raises nothing new."""
    _make_user(session, role=UserRole.ADMIN, username="admin1")
    manager = _make_user(session, role=UserRole.SITE_MANAGER, username="mgr1")
    employee = _make_employee(session)
    site = _make_site(session, number="A")
    session.add(UserSite(user_id=manager.id, site_id=site.id))
    session.commit()
    _assign_day(session, employee=employee, site=site, day=date(2025, 8, 4))

    first = jobs_service.run_weekly_missing_report_summary(
        session, week_start=date(2025, 8, 4), week_end=date(2025, 8, 10)
    )
    before = _count(session)
    second = jobs_service.run_weekly_missing_report_summary(
        session, week_start=date(2025, 8, 4), week_end=date(2025, 8, 10)
    )
    after = _count(session)

    assert first.raised > 0
    assert second.raised == 0
    assert before == after


def test_weekly_summary_silent_when_no_findings(session: Session):
    """An empty week tells nobody: no notifications when there are no findings."""
    _make_user(session, role=UserRole.ADMIN, username="admin1")
    manager = _make_user(session, role=UserRole.SITE_MANAGER, username="mgr1")
    site = _make_site(session, number="A")
    session.add(UserSite(user_id=manager.id, site_id=site.id))
    session.commit()

    result = jobs_service.run_weekly_missing_report_summary(
        session, week_start=date(2025, 8, 4), week_end=date(2025, 8, 10)
    )
    assert result.raised == 0
    assert _count(session) == 0
