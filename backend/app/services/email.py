"""Email delivery for notifications, with retry, backoff, and a failure that costs nothing (Req 14.7, 14.8).

Requirement 14.7 makes a notification deliverable in-app *and* by email; Requirement 14.8 adds that
when email delivery fails the system retries with backoff and records the final failure **without
losing the in-app notification**. Those two sentences shape everything here:

* **The in-app notification is the record of truth.** It is written first, by the notification
  service, under its `dedupe_key`, and it is never touched by a delivery failure. Email is a second
  channel layered on top: a best effort to push the same notification out, whose success or failure is
  recorded on the notification's `delivered_channels` list but can never unwrite the row. So a mail
  server that is down means the recipient still sees the notification the next time they open the app —
  the requirement's "without losing the in-app notification" made literal.

* **Retry with backoff.** A transient SMTP failure — a momentary refusal, a dropped connection — is
  retried a few times with a growing delay between attempts, because most such failures clear on their
  own within seconds. The backoff is exponential from a small base so the first retry is quick and a
  persistent outage is not hammered. `sleep` is injected so a test drives the backoff without waiting.

* **A recorded final failure.** When every attempt is exhausted the notification is marked
  `email_failed` rather than `email` on its `delivered_channels`, and the failure is logged. Nothing
  raises out of `deliver`: a job that sends a hundred notifications must not abort the ninety-nine
  that would have gone out because the first mail server hiccuped, and the requirement asks for the
  failure *recorded*, not *thrown*.

The SMTP transport is a thin, injectable seam (`SmtpTransport`), so the delivery logic — the retry,
the backoff, the channel recording — is unit-tested against a fake that fails on demand, and no test
opens a socket. The real transport uses the standard library's `smtplib` against the configured host,
which is MailHog locally (see `docker-compose.yml`) and a real relay in a deployment.

Rendering the body in the recipient's language (Requirement 21.6) is the front end's job for the
in-app channel and the email renderer's for this one: the notification carries a `title_key` and
`body_params`, and `render_email` turns them into a subject and body in the recipient's `language`.
The catalogue of templates is small and lives here beside the sender; a missing template falls back to
a legible key-and-parameters body rather than failing to send, because a notification the recipient
cannot read is still better than one they never receive.
"""

from __future__ import annotations

import logging
import smtplib
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.notification import Notification
from app.models.user import AppLanguage, User

logger = logging.getLogger(__name__)

#: Channel markers written onto `notifications.delivered_channels`. `CHANNEL_EMAIL` records a
#: successful send, `CHANNEL_EMAIL_FAILED` a final failure after every retry — the "record the final
#: failure" of Requirement 14.8. `CHANNEL_IN_APP` is written when the row is created; email adds to
#: the list rather than replacing it, so a notification can carry both.
CHANNEL_IN_APP = "in_app"
CHANNEL_EMAIL = "email"
CHANNEL_EMAIL_FAILED = "email_failed"

#: How many times a send is attempted in total before the failure is recorded, and the backoff base.
#: Three attempts with a 0.5s base means waits of 0.5s then 1.0s — quick enough that a job is not held
#: up for long, enough to ride out a momentary refusal. Tunable here rather than scattered.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5


# --------------------------------------------------------------------------- rendered content


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """A subject and a plain-text body, already in the recipient's language (Requirement 21.6)."""

    subject: str
    body: str


#: Per-language templates keyed by the notification `title_key`. Each maps to a `(subject, body)` pair
#: where the body is a `str.format` template filled from `body_params`. Kept deliberately small: the
#: keys the Task 30 jobs raise, in both languages. A key with no template falls back to a legible
#: default so a notification is never dropped for want of a translation.
_TEMPLATES: dict[AppLanguage, dict[str, tuple[str, str]]] = {
    AppLanguage.ENGLISH: {
        "notifications.no_checkout_reminder": (
            "You have not checked out",
            "You are still checked in at {site_name} since {check_in_local}. "
            "Please check out, or contact your site manager.",
        ),
        "notifications.over_maximum_open_shift": (
            "Open shift over the maximum",
            "{employee_name} has an open shift at {site_name} since {check_in_local}, "
            "beyond the {max_hours}h maximum. It has been flagged for review.",
        ),
        "notifications.missing_reports_weekly": (
            "Weekly missing-report summary",
            "{finding_count} missing report(s) for the week of {week_start} to {week_end} "
            "across your sites. Open the missing-reports view to complete them.",
        ),
        "notifications.document_expiry_warning": (
            "Document expiring soon",
            "A document for employee {employee_id} expires on {expiry_date}.",
        ),
        "notifications.document_expiry_escalation": (
            "Document expired",
            "A document for employee {employee_id} expired on {expiry_date}.",
        ),
    },
    AppLanguage.HEBREW: {
        "notifications.no_checkout_reminder": (
            "לא בוצע ניתוק",
            "אתה עדיין מחובר באתר {site_name} מאז {check_in_local}. "
            "אנא בצע ניתוק או פנה למנהל האתר.",
        ),
        "notifications.over_maximum_open_shift": (
            "משמרת פתוחה מעבר למקסימום",
            "ל-{employee_name} יש משמרת פתוחה באתר {site_name} מאז {check_in_local}, "
            "מעבר למקסימום של {max_hours} שעות. המשמרת סומנה לבדיקה.",
        ),
        "notifications.missing_reports_weekly": (
            "סיכום שבועי של דיווחים חסרים",
            "{finding_count} דיווחים חסרים לשבוע {week_start} עד {week_end} באתרים שלך. "
            "פתח את מסך הדיווחים החסרים כדי להשלים אותם.",
        ),
        "notifications.document_expiry_warning": (
            "מסמך עומד לפוג",
            "מסמך של עובד {employee_id} יפוג בתאריך {expiry_date}.",
        ),
        "notifications.document_expiry_escalation": (
            "תוקף מסמך פג",
            "מסמך של עובד {employee_id} פג בתאריך {expiry_date}.",
        ),
    },
}


def render_email(
    *, title_key: str, body_params: dict, language: AppLanguage
) -> RenderedEmail:
    """Render a notification into a subject and body in the recipient's language (Requirement 21.6).

    Looks the `title_key` up in the language's template table and fills the body from `body_params`. A
    key with no template, or a template missing a parameter, falls back to a legible subject and a body
    that spells out the key and its parameters — a notification the recipient can still act on is worth
    more than a send that failed because a translation was absent. The recipient's language is applied
    here, at render time, so the same stored notification reads in Hebrew or English by who receives it,
    not by when the job ran.
    """
    catalogue = _TEMPLATES.get(language, _TEMPLATES[AppLanguage.ENGLISH])
    template = catalogue.get(title_key)
    if template is None:
        return RenderedEmail(
            subject=title_key,
            body=f"{title_key}: {body_params}",
        )
    subject, body_template = template
    try:
        body = body_template.format(**body_params)
    except (KeyError, IndexError):
        # A parameter the template expected was not supplied. Fall back rather than fail to send.
        body = f"{subject}\n\n{body_params}"
    return RenderedEmail(subject=subject, body=body)


# --------------------------------------------------------------------------- transport seam


class SmtpTransport(Protocol):
    """The one operation the delivery logic needs: send a rendered message to an address.

    A protocol so the retry, backoff and channel-recording logic is tested against a fake that fails on
    demand, and no unit test opens a socket. Any failure is raised as an exception; the sender treats
    every exception as a transient failure worth retrying, which is the safe default — a permanent
    failure simply exhausts the retries and is recorded, at the cost of a few wasted attempts.
    """

    def send(self, *, to_address: str, subject: str, body: str) -> None:
        """Send one message, or raise on failure."""
        ...


class SmtplibTransport:
    """The real transport: the standard library's `smtplib` against the configured SMTP host.

    MailHog locally (`docker-compose.yml`), a real relay in a deployment. No TLS and no auth here: the
    local MailHog needs neither, and a deployment terminates both at the relay or the network. The
    connection is opened per send — the volume is a handful of notifications a day, so a pool would be
    complexity with no payoff, and a fresh connection cannot be a stale one.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def send(self, *, to_address: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._settings.smtp_from_address
        message["To"] = to_address
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=10) as smtp:
            smtp.send_message(message)


# --------------------------------------------------------------------------- delivery


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The outcome of one delivery attempt sequence, so a job and a test can see what happened.

    `delivered` is True when the email went out, False when every retry was exhausted and the failure
    was recorded. `attempts` is how many sends were tried. In neither case is the in-app notification
    affected — it was written before email was attempted and is the record of truth (Requirement 14.8).
    """

    delivered: bool
    attempts: int


def deliver(
    session: Session,
    notification: Notification,
    *,
    transport: SmtpTransport,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep: Callable[[float], None] = time_module.sleep,
) -> DeliveryResult:
    """Deliver one notification by email, retrying with backoff, recording the outcome (Req 14.7, 14.8).

    The recipient is resolved from the notification; a recipient with no address on record cannot be
    emailed, so the notification is recorded as an email failure and the in-app row stands — the
    requirement's "without losing the in-app notification" for the case there is nowhere to send.

    Each attempt calls the transport; a raised exception is treated as transient and, if attempts
    remain, is followed by an exponentially growing sleep before the next try. On the first success the
    notification's `delivered_channels` gains `email` and the function returns. When every attempt has
    failed the notification gains `email_failed` instead, the failure is logged, and the function
    returns `delivered=False` — it never raises, so one bad address in a batch does not abort the rest.

    The channel list is read-modify-write on the ORM attribute rather than an in-place append, because
    the column is a JSON-backed list on SQLite whose change tracking needs a new list assigned to
    notice the mutation. Nothing here commits: the caller's unit of work owns that, so the channel
    update lands with whatever else the job wrote.
    """
    recipient = session.get(User, notification.recipient_user_id)
    to_address = _recipient_address(recipient)
    if to_address is None:
        _record_channel(notification, CHANNEL_EMAIL_FAILED)
        logger.warning(
            "email delivery skipped: recipient %s has no address (notification=%s)",
            notification.recipient_user_id,
            notification.id,
        )
        return DeliveryResult(delivered=False, attempts=0)

    language = recipient.language if recipient is not None else AppLanguage.ENGLISH
    rendered = render_email(
        title_key=notification.title_key,
        body_params=notification.body_params,
        language=language,
    )

    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            transport.send(
                to_address=to_address, subject=rendered.subject, body=rendered.body
            )
        except Exception as error:  # noqa: BLE001 - every transport failure is retried then recorded
            logger.info(
                "email delivery attempt %d/%d failed for notification=%s: %s",
                attempt,
                max_attempts,
                notification.id,
                error,
            )
            if attempt < max_attempts:
                sleep(backoff_base_seconds * (2 ** (attempt - 1)))
            continue
        else:
            _record_channel(notification, CHANNEL_EMAIL)
            session.flush()
            return DeliveryResult(delivered=True, attempts=attempts)

    _record_channel(notification, CHANNEL_EMAIL_FAILED)
    session.flush()
    logger.warning(
        "email delivery failed after %d attempts for notification=%s (in-app notification retained)",
        attempts,
        notification.id,
    )
    return DeliveryResult(delivered=False, attempts=attempts)


def deliver_unsent(
    session: Session,
    *,
    transport: SmtpTransport | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep: Callable[[float], None] = time_module.sleep,
) -> int:
    """Attempt email delivery for every notification not yet emailed. Returns how many were delivered.

    The channel the jobs use to push out what they raised: a job creates the in-app notifications, then
    this walks the ones whose `delivered_channels` carry neither a success nor a recorded failure and
    tries each. A notification already marked `email` or `email_failed` is skipped, so a re-run does not
    re-send a delivered one or re-attempt a permanently failed one — the same idempotency the dedupe key
    gives the in-app row, applied to the email channel.
    """
    transport = transport or SmtplibTransport()
    pending = session.scalars(select(Notification)).all()
    delivered = 0
    for notification in pending:
        channels = notification.delivered_channels or []
        if CHANNEL_EMAIL in channels or CHANNEL_EMAIL_FAILED in channels:
            continue
        result = deliver(
            session,
            notification,
            transport=transport,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
            sleep=sleep,
        )
        if result.delivered:
            delivered += 1
    return delivered


def _recipient_address(recipient: User | None) -> str | None:
    """The email address to send a notification to, or None when there is nowhere to send.

    Users have no dedicated email column in the schema; the username is the login and is treated as the
    address when it looks like one. A recipient with no address is not an error — it is the case
    Requirement 14.8 covers: the in-app notification stands and the email is recorded as failed.
    """
    if recipient is None:
        return None
    username = recipient.username
    return username if "@" in username else None


def _record_channel(notification: Notification, channel: str) -> None:
    """Add a channel marker to the notification, assigning a new list so ORM change tracking fires.

    The `delivered_channels` column is a JSON-backed list on SQLite; mutating it in place does not mark
    the attribute dirty, so a fresh list is assigned. A channel already present is not added twice.
    """
    channels = list(notification.delivered_channels or [])
    if channel not in channels:
        channels.append(channel)
        notification.delivered_channels = channels
