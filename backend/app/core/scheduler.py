"""Wiring the notification jobs onto a clock (Requirement 14, design "Background work").

The jobs themselves are pure-ish functions in `app.services.jobs`: they take a session and an explicit
time and never decide *when* they run. This module is the part that decides when — an in-process
APScheduler that fires the daily jobs at the configured hour and the weekly summary on the configured
day (design: "APScheduler in milestone 1"). The separation is deliberate and is what keeps the jobs
testable to the minute: a test calls a job with a chosen `now`; only the scheduler ever reads the real
clock.

Each fire opens **its own** database session and commits once, so a job's notifications and the audit
line recording the run land together, and a failure in one job never rolls back another's work. After
the in-app notifications are written the email channel is drained (`app.services.email.deliver_unsent`),
so the second delivery channel of Requirement 14.7 goes out in the same run — and because a failed send
is recorded rather than raised (Requirement 14.8), a mail outage cannot abort the job.

Milestone 1 runs this inside the API process, guarded by `scheduler_enabled` so the test client and a
multi-instance deployment do not both fire the jobs. The cloud target moves the schedule to a dedicated
runner (the deployment task); it calls the same `run_all_daily` / `run_weekly` entry points, so moving
the schedule does not touch the jobs.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.services import email as email_service
from app.services import jobs as jobs_service
from app.services.audit import AuditContext

logger = logging.getLogger(__name__)


def run_all_daily(*, now: datetime | None = None) -> None:
    """Run the three daily jobs, then drain the email channel, in one transaction (Req 14.1, 14.2, 4.4-4.6).

    Opens a fresh session, runs the no-checkout reminder, the over-maximum open-shift alert and the
    document-expiry sweep, records each run in the audit log, delivers any notification not yet emailed,
    and commits once. `now` is injectable so a test drives the whole daily batch at a chosen instant;
    the scheduler passes the real time.
    """
    now = now or datetime.now(UTC)
    today = now.date()
    session = get_session_factory()()
    try:
        no_checkout = jobs_service.run_no_checkout_reminder(session, now=now)
        jobs_service._record_job_run(
            session,
            job="no_checkout_reminder",
            result=no_checkout,
            context=AuditContext(reason="no_checkout_reminder_job"),
        )

        over_maximum = jobs_service.run_over_maximum_open_shift(session, now=now)
        jobs_service._record_job_run(
            session,
            job="over_maximum_open_shift",
            result=over_maximum,
            context=AuditContext(reason="over_maximum_open_shift_job"),
        )

        jobs_service.run_document_expiry(session, today=today)

        email_service.deliver_unsent(session)
        session.commit()
        logger.info(
            "daily jobs complete: no_checkout raised=%d, over_maximum raised=%d",
            no_checkout.raised,
            over_maximum.raised,
        )
    except Exception:
        session.rollback()
        logger.exception("daily jobs failed; transaction rolled back")
        raise
    finally:
        session.close()


def run_weekly(*, now: datetime | None = None) -> None:
    """Run the weekly missing-report summary, then drain email, in one transaction (Requirement 14.5).

    The week summarised is the seven days ending on the run date. Opens a fresh session, runs the
    summary, records the run, delivers unsent notifications and commits once. `now` is injectable for a
    test.
    """
    now = now or datetime.now(UTC)
    week_end: date = now.date()
    week_start: date = week_end - timedelta(days=6)
    session = get_session_factory()()
    try:
        result = jobs_service.run_weekly_missing_report_summary(
            session, week_start=week_start, week_end=week_end
        )
        jobs_service._record_job_run(
            session,
            job="weekly_missing_report_summary",
            result=result,
            context=AuditContext(reason="weekly_missing_report_summary_job"),
        )
        email_service.deliver_unsent(session)
        session.commit()
        logger.info("weekly summary complete: raised=%d", result.raised)
    except Exception:
        session.rollback()
        logger.exception("weekly summary failed; transaction rolled back")
        raise
    finally:
        session.close()


def build_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    """A scheduler with the daily and weekly jobs registered, not yet started.

    Cron triggers in the business timezone, so "18:00" means 18:00 local wherever the server runs. The
    daily jobs fire together at the configured hour and minute; the weekly summary fires on the
    configured weekday and hour. Separated from `start_scheduler` so a test can build and inspect the
    schedule without a background thread.
    """
    settings = settings or get_settings()
    scheduler = BackgroundScheduler(timezone=settings.app_timezone)
    scheduler.add_job(
        run_all_daily,
        CronTrigger(
            hour=settings.daily_jobs_hour,
            minute=settings.daily_jobs_minute,
            timezone=settings.app_timezone,
        ),
        id="daily_jobs",
        replace_existing=True,
    )
    scheduler.add_job(
        run_weekly,
        CronTrigger(
            day_of_week=settings.weekly_summary_weekday,
            hour=settings.weekly_summary_hour,
            timezone=settings.app_timezone,
        ),
        id="weekly_summary",
        replace_existing=True,
    )
    return scheduler


def start_scheduler(settings: Settings | None = None) -> BackgroundScheduler | None:
    """Start the in-process scheduler if enabled; return it, or None when disabled.

    Guarded by `scheduler_enabled` so the test client and a multi-instance deployment do not both fire
    the jobs. The returned scheduler is held by the caller (the app lifespan) so it can be shut down
    cleanly on stop.
    """
    settings = settings or get_settings()
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED is false)")
        return None
    scheduler = build_scheduler(settings)
    scheduler.start()
    logger.info(
        "scheduler started: daily at %02d:%02d, weekly on weekday %d at %02d:00 (%s)",
        settings.daily_jobs_hour,
        settings.daily_jobs_minute,
        settings.weekly_summary_weekday,
        settings.weekly_summary_hour,
        settings.app_timezone,
    )
    return scheduler
