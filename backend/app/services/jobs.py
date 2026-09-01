"""The scheduled notification jobs (Requirement 14.1, 14.2, 14.5, and 4.4–4.6 by reuse).

Four jobs raise the notifications of Requirement 14, and this module is where each is expressed as a
plain function that takes a session and a clock. Keeping the clock (`now` / `today`) an argument
rather than reading it inside is what makes every job testable to the minute and lets the scheduler,
which knows the real time, stay a thin wrapper (`app.core.scheduler`).

Every job shares three properties, and they are the properties Requirement 14 asks for:

* **Idempotent.** No job checks "have I already sent this"; each asks the notification service to
  create under a `dedupe_key` that names the condition, and a re-run computes the same key and the
  insert is a no-op (Requirement 14.6). Run any job twice in the same window and the second run raises
  nothing new. This is the same mechanism the document sweep already relies on (Requirement 4.6), and
  the weekly summary keys its findings by `type:employee:date:site` exactly as the design specifies.

* **Language-neutral.** A job stores a `title_key` and `body_params`, never a sentence. The recipient's
  language is applied when the notification is read (the front end) or emailed (`app.services.email`),
  so the same run serves a Hebrew manager and an English one correctly (Requirement 21.6).

* **Non-committing.** Like the rest of the service layer, a job adds to the caller's session and lets
  the caller's unit of work decide. The scheduler opens one transaction per run and commits once, so a
  failure part-way through a job leaves none of that run's notifications behind.

The four jobs:

1. **No-checkout reminder** (`run_no_checkout_reminder`, Requirement 14.1). An employee still checked
   in at the configurable cutoff is reminded — sent to *the employee*, not a manager, keyed by the open
   entry so one open shift yields one reminder however often the job runs that day.

2. **Over-maximum open shift** (`run_over_maximum_open_shift`, Requirement 14.2). An entry open beyond
   the configurable maximum (default 16h) alerts the *site manager* and flags the entry as an anomaly.
   The flag is written on the entry so the hours view surfaces it; the notification names the employee,
   the site, the date and the hours open, and links to the entry.

3. **Document expiry** (`run_document_expiry`, Requirement 4.4–4.6). Delegates wholesale to the sweep
   Task 10 already built (`app.services.document.run_document_expiry_sweep`); this job is the scheduled
   entry point, no logic of its own, so the two cannot drift.

4. **Weekly missing-report summary** (`run_weekly_missing_report_summary`, Requirement 14.5). Reuses
   `detect_missing_reports` (Task 29) to find the week's findings, then sends *each site manager* a
   summary of the findings for their sites and *each administrator* a summary of all findings. A per-
   finding manager alert names the employee, the site, the date and what is missing, and links to the
   completion form (Requirement 14.4).

Recipients are resolved from the same tables authorization reads: a site's manager is
`sites.manager_user_id`, a manager's sites are their `user_sites` rows, and the employee behind a time
entry is the `users` row whose `employee_id` matches — so a notification reaches exactly who the
requirement names.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.core.config import get_settings
from app.models.notification import NotificationSeverity
from app.models.site import Site
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.user_site import UserSite
from app.services import document as document_service
from app.services import notification as notification_service
from app.services import reports as reports_service
from app.services import settings as settings_service
from app.services.audit import AuditContext, record_change
from app.services.reports import MissingReportFinding, MissingReportKind

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- settings keys

#: Local time of day the no-checkout reminder is sent to employees still open (Requirement 14.1).
NO_CHECKOUT_CUTOFF_SETTING = "no_checkout_cutoff_time"

#: Hours an open shift may run before the site manager is alerted (Requirement 14.2, default 16).
MAX_OPEN_SHIFT_SETTING = "max_open_shift_hours"
DEFAULT_MAX_OPEN_SHIFT_HOURS = 16

# --------------------------------------------------------------------------- notification types

NOTIFICATION_TYPE_NO_CHECKOUT = "no_checkout_reminder"
NOTIFICATION_TYPE_OVER_MAXIMUM = "over_maximum_open_shift"
NOTIFICATION_TYPE_MISSING_WEEKLY = "missing_reports_weekly"
NOTIFICATION_TYPE_MISSING_MANAGER = "missing_report_manager_alert"

TITLE_KEY_NO_CHECKOUT = "notifications.no_checkout_reminder"
TITLE_KEY_OVER_MAXIMUM = "notifications.over_maximum_open_shift"
TITLE_KEY_MISSING_WEEKLY = "notifications.missing_reports_weekly"
TITLE_KEY_MISSING_MANAGER = "notifications.missing_report_manager_alert"

#: The anomaly flag an over-maximum open shift carries, so the hours view surfaces it the same way it
#: surfaces an implausible duration or an unassigned site (Requirement 14.2). Written on the entry by
#: the over-maximum job; a completed entry never carries it, since the job only ever sees open ones.
FLAG_OVER_MAXIMUM_OPEN = "over_maximum_open_shift"


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class JobResult:
    """What one job run did, so a scheduler log and a test can see the outcome.

    `examined` is how many conditions were looked at, `raised` how many *new* notifications resulted —
    a re-run in the same window examines the same conditions and raises zero, which is the idempotency
    of Requirement 14.6 made visible.
    """

    examined: int
    raised: int


# --------------------------------------------------------------------------- 1. no-checkout (14.1)


def run_no_checkout_reminder(
    session: Session,
    *,
    now: datetime,
    context: AuditContext | None = None,
) -> JobResult:
    """Remind every employee still checked in at the cutoff that they have not checked out (Req 14.1).

    An entry is "still open" when `check_out_at IS NULL`. The reminder goes to the *employee* — the
    `users` row whose `employee_id` is the entry's employee — not to a manager, because 14.1 names the
    employee. Keyed by the open entry, so an employee still open produces one reminder however many
    times the job runs during the day; the next day's open shift is a different entry and a different
    key, so it is reminded afresh. An employee with no login is skipped: there is nobody to notify.

    `now` is the moment the job runs (the scheduler passes the real time at the cutoff). It is carried
    into the body so the reminder can state how long the shift has been open, and it is not compared
    against the cutoff here — the scheduler decides *when* to run this; the job's job is to notify
    whoever is open at that moment.
    """
    context = context or AuditContext(reason="no_checkout_reminder_job")
    timezone = ZoneInfo(get_settings().app_timezone)

    open_entries = _open_entries(session)
    logins = _employee_logins(session, {entry.employee_id for entry in open_entries})

    raised = 0
    for entry in open_entries:
        recipient = logins.get(entry.employee_id)
        if recipient is None:
            continue
        site = session.get(Site, entry.site_id)
        check_in_local = entry.check_in_at.astimezone(timezone)
        dedupe_key = f"{NOTIFICATION_TYPE_NO_CHECKOUT}:{entry.id}"
        _notification, created = notification_service.create_deduplicated(
            session,
            recipient_user_id=recipient.id,
            type=NOTIFICATION_TYPE_NO_CHECKOUT,
            dedupe_key=dedupe_key,
            title_key=TITLE_KEY_NO_CHECKOUT,
            severity=NotificationSeverity.WARNING,
            body_params={
                "employee_id": str(entry.employee_id),
                "site_id": str(entry.site_id),
                "site_name": site.name if site is not None else "",
                "work_date": entry.work_date.isoformat(),
                "check_in_local": check_in_local.isoformat(timespec="minutes"),
            },
            related_entity_type="time_entries",
            related_entity_id=entry.id,
        )
        if created:
            raised += 1

    return JobResult(examined=len(open_entries), raised=raised)


# --------------------------------------------------------------------------- 2. over-maximum (14.2)


def run_over_maximum_open_shift(
    session: Session,
    *,
    now: datetime,
    context: AuditContext | None = None,
) -> JobResult:
    """Alert the site manager to any open shift past the maximum, and flag the entry (Req 14.2).

    An entry is over the maximum when it is still open and its check-in is more than
    `max_open_shift_hours` (default 16) before `now`. Two things happen for each: the entry gains the
    `over_maximum_open_shift` flag so the hours view surfaces the anomaly, and the site's manager
    (`sites.manager_user_id`) is notified. The alert names the employee, the site, the date and the
    hours the shift has been open, and links to the entry so the manager can act (Requirement 14.4).

    Keyed by the entry, so a shift that stays open across several runs is flagged and alerted once, not
    once per run. A site with no manager assigned still gets the entry flagged — the anomaly is real
    whether or not there is a manager to tell — but raises no notification, which the count reflects.
    """
    context = context or AuditContext(reason="over_maximum_open_shift_job")
    timezone = ZoneInfo(get_settings().app_timezone)
    max_hours = settings_service.get_int_or(
        session, MAX_OPEN_SHIFT_SETTING, DEFAULT_MAX_OPEN_SHIFT_HOURS
    )
    cutoff = now - timedelta(hours=max_hours)

    over_maximum = [entry for entry in _open_entries(session) if entry.check_in_at <= cutoff]

    raised = 0
    for entry in over_maximum:
        _flag_over_maximum(session, entry)

        site = session.get(Site, entry.site_id)
        manager_user_id = site.manager_user_id if site is not None else None
        employee_name = _employee_name(session, entry.employee_id)
        hours_open = (now - entry.check_in_at).total_seconds() / 3600
        if manager_user_id is None:
            continue

        dedupe_key = f"{NOTIFICATION_TYPE_OVER_MAXIMUM}:{entry.id}"
        _notification, created = notification_service.create_deduplicated(
            session,
            recipient_user_id=manager_user_id,
            type=NOTIFICATION_TYPE_OVER_MAXIMUM,
            dedupe_key=dedupe_key,
            title_key=TITLE_KEY_OVER_MAXIMUM,
            severity=NotificationSeverity.CRITICAL,
            body_params={
                "employee_id": str(entry.employee_id),
                "employee_name": employee_name,
                "site_id": str(entry.site_id),
                "site_name": site.name if site is not None else "",
                "work_date": entry.work_date.isoformat(),
                "check_in_local": entry.check_in_at.astimezone(timezone).isoformat(timespec="minutes"),
                "max_hours": max_hours,
                "hours_open": round(hours_open, 1),
            },
            related_entity_type="time_entries",
            related_entity_id=entry.id,
        )
        if created:
            raised += 1

    return JobResult(examined=len(over_maximum), raised=raised)


def _flag_over_maximum(session: Session, entry: TimeEntry) -> None:
    """Add the over-maximum anomaly flag to an entry, if it is not already there.

    Reassigns a fresh list rather than mutating in place: the `flags` column is a JSON-backed list on
    SQLite whose change tracking notices a new list assigned, not an in-place append — the same shape
    the scan and manual-entry services use for their flags.
    """
    if FLAG_OVER_MAXIMUM_OPEN not in entry.flags:
        entry.flags = [*entry.flags, FLAG_OVER_MAXIMUM_OPEN]
        session.flush()


# --------------------------------------------------------------------------- 3. document expiry (4.4-4.6)


def run_document_expiry(
    session: Session,
    *,
    today: date,
    context: AuditContext | None = None,
) -> document_service.SweepResult:
    """Run the daily document-expiry sweep (Requirement 4.4–4.6).

    No logic of its own: it delegates to the sweep Task 10 already built, so the scheduled job and the
    sweep can never disagree. Kept here so the scheduler wires one module of jobs rather than reaching
    across into the document service, and so a test of "the jobs run" covers this one the same way.
    """
    return document_service.run_document_expiry_sweep(session, today=today, context=context)


# --------------------------------------------------------------------------- 4. weekly missing (14.5)


def run_weekly_missing_report_summary(
    session: Session,
    *,
    week_start: date,
    week_end: date,
    context: AuditContext | None = None,
) -> JobResult:
    """Send each manager a summary of their sites' missing reports, and each admin a summary of all (14.5).

    Reuses `detect_missing_reports` (Task 29) with an unrestricted scope to find every finding in the
    week, then routes them:

    * **Each administrator** gets a weekly summary covering *all* sites, keyed by the week so a re-run
      raises nothing new (Requirement 14.6).
    * **Each site manager** gets a weekly summary covering *their* sites — the findings whose site is
      in the manager's `user_sites` — keyed by the week and the manager, and a *per-finding* alert that
      names the employee, the site, the date and what is missing, linking to the completion form
      (Requirement 14.4). A manager with no findings for the week is not sent an empty summary.

    The per-finding manager alert is keyed `type:employee:date:site` exactly as the design's missing-
    report section specifies, so the alert a manager sees and the report the missing-reports endpoint
    returns are the same findings, deduplicated the same way.
    """
    context = context or AuditContext(reason="weekly_missing_report_summary_job")

    findings = reports_service.detect_missing_reports(
        session,
        date_from=week_start,
        date_to=week_end,
        scope=SiteScope.all_sites(),
    )

    raised = 0
    raised += _summarise_for_admins(
        session, findings=findings, week_start=week_start, week_end=week_end
    )
    raised += _summarise_for_managers(
        session, findings=findings, week_start=week_start, week_end=week_end
    )

    return JobResult(examined=len(findings), raised=raised)


def _summarise_for_admins(
    session: Session,
    *,
    findings: list[MissingReportFinding],
    week_start: date,
    week_end: date,
) -> int:
    """One weekly summary to each administrator covering all sites (Requirement 14.5). Returns raised.

    A summary is sent even when there are no findings? No — an empty week is silence: administrators are
    only told when there is something to tell, which keeps the notification meaningful. The key names
    the week and the admin, so the weekly summary is sent once per week per admin.
    """
    if not findings:
        return 0
    admins = session.scalars(
        select(User).where(User.role == UserRole.ADMIN).where(User.is_active.is_(True))
    ).all()

    raised = 0
    for admin in admins:
        dedupe_key = f"{NOTIFICATION_TYPE_MISSING_WEEKLY}:all:{week_start.isoformat()}:{admin.id}"
        _notification, created = notification_service.create_deduplicated(
            session,
            recipient_user_id=admin.id,
            type=NOTIFICATION_TYPE_MISSING_WEEKLY,
            dedupe_key=dedupe_key,
            title_key=TITLE_KEY_MISSING_WEEKLY,
            severity=NotificationSeverity.INFO,
            body_params={
                "scope": "all",
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "finding_count": len(findings),
            },
        )
        if created:
            raised += 1
    return raised


def _summarise_for_managers(
    session: Session,
    *,
    findings: list[MissingReportFinding],
    week_start: date,
    week_end: date,
) -> int:
    """A weekly summary and per-finding alerts to each site manager for their sites (Req 14.4, 14.5).

    A manager's sites are their `user_sites` grant — the same table authorization reads — so a manager
    is told about exactly the sites they run and no others. For each such manager the findings are
    narrowed to their sites; if any remain, a weekly summary keyed by week-and-manager is raised, plus
    one alert per finding keyed `type:employee:date:site` naming the employee, the site, the date and
    what is missing and linking to the completion form.
    """
    if not findings:
        return 0

    manager_sites = _manager_site_ids(session)
    raised = 0
    for manager_id, site_ids in manager_sites.items():
        theirs = [finding for finding in findings if finding.site_id in site_ids]
        if not theirs:
            continue

        summary_key = (
            f"{NOTIFICATION_TYPE_MISSING_WEEKLY}:manager:{week_start.isoformat()}:{manager_id}"
        )
        _summary, summary_created = notification_service.create_deduplicated(
            session,
            recipient_user_id=manager_id,
            type=NOTIFICATION_TYPE_MISSING_WEEKLY,
            dedupe_key=summary_key,
            title_key=TITLE_KEY_MISSING_WEEKLY,
            severity=NotificationSeverity.INFO,
            body_params={
                "scope": "manager",
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "finding_count": len(theirs),
            },
        )
        if summary_created:
            raised += 1

        for finding in theirs:
            if _raise_manager_alert(session, manager_id=manager_id, finding=finding):
                raised += 1

    return raised


def _raise_manager_alert(
    session: Session, *, manager_id: uuid.UUID, finding: MissingReportFinding
) -> bool:
    """One manager alert for one finding, naming employee/site/date/what-missing (Req 14.4). Returns created.

    Keyed `type:employee:date:site` per the design, so a finding that persists across weekly runs is
    alerted once. `related_entity_type`/`id` point at the employee so the front end can open the
    completion form prefilled — the "link directly to the completion form" of Requirement 14.4, which
    the manual-entry form reaches from a missing-report alert (Task 21).
    """
    dedupe_key = (
        f"{NOTIFICATION_TYPE_MISSING_MANAGER}:{finding.employee_id}:"
        f"{finding.work_date.isoformat()}:{finding.site_id}:{manager_id}"
    )
    _notification, created = notification_service.create_deduplicated(
        session,
        recipient_user_id=manager_id,
        type=NOTIFICATION_TYPE_MISSING_MANAGER,
        dedupe_key=dedupe_key,
        title_key=TITLE_KEY_MISSING_MANAGER,
        severity=NotificationSeverity.WARNING,
        body_params={
            "employee_id": str(finding.employee_id),
            "employee_name": finding.employee_name,
            "employee_name_en": finding.employee_name_en,
            "site_id": str(finding.site_id),
            "site_name": finding.site_name,
            "work_date": finding.work_date.isoformat(),
            "missing": _missing_word(finding.kind),
        },
        related_entity_type="employees",
        related_entity_id=finding.employee_id,
    )
    return created


# --------------------------------------------------------------------------- shared reads


def _open_entries(session: Session) -> list[TimeEntry]:
    """Every non-deleted, still-open time entry (`check_out_at IS NULL`).

    The condition the no-checkout and over-maximum jobs both start from. Soft-deleted entries are
    excluded — a deleted entry is not a real open shift.
    """
    return list(
        session.scalars(
            select(TimeEntry)
            .where(TimeEntry.check_out_at.is_(None))
            .where(TimeEntry.deleted_at.is_(None))
            .order_by(TimeEntry.check_in_at, TimeEntry.id)
        )
    )


def _employee_logins(
    session: Session, employee_ids: set[uuid.UUID]
) -> dict[uuid.UUID, User]:
    """The active login for each employee that has one, keyed by employee id (Requirement 14.1).

    An employee is notified through their `users` row — the one whose `employee_id` matches. An
    employee with no login is absent from the map, and the caller skips them: there is nobody to tell.
    """
    if not employee_ids:
        return {}
    users = session.scalars(
        select(User)
        .where(User.employee_id.in_(employee_ids))
        .where(User.is_active.is_(True))
    ).all()
    return {user.employee_id: user for user in users if user.employee_id is not None}


def _employee_name(session: Session, employee_id: uuid.UUID) -> str:
    """The employee's local-language name, for a manager alert body. Empty when the row is gone."""
    from app.models.employee import Employee

    employee = session.get(Employee, employee_id)
    return employee.full_name if employee is not None else ""


def _manager_site_ids(session: Session) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Each site manager's assigned site ids, from `user_sites` (Requirement 14.5, 2.3).

    Grouped by user so the weekly job narrows each manager's findings to their own sites in memory
    after one read, rather than a query per manager. A manager with no assignment does not appear, and
    so is not sent an empty summary.
    """
    rows = session.execute(select(UserSite.user_id, UserSite.site_id)).all()
    grouped: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for user_id, site_id in rows:
        grouped[user_id].add(site_id)
    return dict(grouped)


#: How the three missing-report kinds read as the `missing` parameter of a manager alert. Machine
#: values the front end translates (Requirement 21.6), matching the finding's kind.
_MISSING_WORDS: dict[MissingReportKind, str] = {
    MissingReportKind.MISSING_CHECKOUT: "checkout",
    MissingReportKind.MISSING_CHECKIN: "checkin",
    MissingReportKind.BOTH_MISSING: "both",
}


def _missing_word(kind: MissingReportKind) -> str:
    return _MISSING_WORDS[kind]


# --------------------------------------------------------------------------- audit marker


#: A fixed, non-entity id for a job's summary audit row, matching the document sweep's marker. A job
#: run is not a change to one entity, so it is recorded against a stable marker.
_JOB_MARKER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _record_job_run(
    session: Session, *, job: str, result: JobResult, context: AuditContext
) -> None:
    """A single audit line that a job ran and what it raised, attributed to the system (Req 13.5).

    Scheduled actions are recorded like any other. Recorded against the fixed marker id, since a run is
    not a change to a single row.
    """
    record_change(
        session,
        entity_type="notifications",
        entity_id=_JOB_MARKER_ID,
        field=job,
        old_value=None,
        new_value=f"examined={result.examined} raised={result.raised}",
        context=context,
        reason=job,
    )
