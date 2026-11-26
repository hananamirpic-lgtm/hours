"""Shared set-up for the service report and export tests (task 30.6).

The application is the real one (`create_app` with the flag on), so the routes are reached through the
whole middleware stack. Each request gets its own session on the in-memory test database, and the test
seeds rows through its own session. Rows are seeded directly, not through the API, so that every test
controls the creation times, the parts and the settlements of its tickets.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app
from app.models.user import UserRole
from app.service_tickets.models import Category, Customer, Locality, Part, Ticket, TicketPart

API = "/api/service"
STAFF = f"{API}/staff"
REPORT = f"{STAFF}/reports"
EXPORT = f"{STAFF}/reports/export"

#: The three roles of Ticket_Staff (R1).
TICKET_STAFF_ROLES = (UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN)

#: Creation time of the first seeded ticket; each later one is an hour newer unless the test says so.
BASE_TIME = datetime(2025, 3, 10, 6, 0, tzinfo=UTC)

_JERUSALEM = ZoneInfo("Asia/Jerusalem")

_counter = itertools.count(1)
_numbers = itertools.count(20000)


def build_app(settings: Any, factory: sessionmaker[Session], *, enabled: bool = True) -> FastAPI:
    changes = {
        "service_tickets_enabled": "true" if enabled else "",
        "service_accessibility_contact": "a@b.test",
        "api_prefix": "/api",
    }
    application = create_app(settings.model_copy(update=changes))

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    application.dependency_overrides[get_db] = request_session
    return application


def make_client(settings: Any, session: Session, *, enabled: bool = True) -> TestClient:
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)
    return TestClient(build_app(settings, factory, enabled=enabled), raise_server_exceptions=False)


def bearer(user: Any, role: UserRole) -> dict[str, str]:
    token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
    return {"Authorization": f"Bearer {token}"}


def code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


class Seeder:
    """Rows for one test, committed as they are made."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.customer = self._customer()
        self.tickets: list[Ticket] = []

    def _customer(self) -> Customer:
        row = Customer(
            phone=f"05{next(_counter):08d}",
            full_name="Dana Levi",
            full_name_key="dana levi",
            password_hash="x",
        )
        self.session.add(row)
        self.session.commit()
        return row

    def new_customer(self, full_name: str) -> Customer:
        row = Customer(
            phone=f"05{next(_counter):08d}",
            full_name=full_name,
            full_name_key=full_name.casefold(),
            password_hash="x",
        )
        self.session.add(row)
        self.session.commit()
        return row

    def category(self, name_en: str, name_he: str | None = None) -> Category:
        row = Category(
            seq=next(_counter),
            name_he=name_he or f"{name_en}-he",
            name_en=name_en,
            name_he_key=(name_he or f"{name_en}-he").casefold(),
            name_en_key=name_en.casefold(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def part(self, name_en: str, name_he: str | None = None, *, active: bool = True) -> Part:
        row = Part(
            seq=next(_counter),
            name_he=name_he or f"{name_en}-he",
            name_en=name_en,
            name_he_key=(name_he or f"{name_en}-he").casefold(),
            name_en_key=name_en.casefold(),
            is_active=active,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def locality(self, name_en: str, name_he: str | None = None) -> Locality:
        row = Locality(
            seq=next(_counter),
            name_he=name_he or f"{name_en}-he",
            name_en=name_en,
            name_he_key=(name_he or f"{name_en}-he").casefold(),
            name_en_key=name_en.casefold(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def ticket(
        self,
        category: Category,
        *,
        parts: tuple[Part, ...] = (),
        status: str = "received",
        city: str = "Haifa",
        apartment: str = "12",
        phone: str = "0501234567",
        technician: str = "",
        urgent: bool = False,
        locality: Locality | None = None,
        opened_by: str | None = None,
        customer: Customer | None = None,
        created_at: datetime | None = None,
        number: int | None = None,
    ) -> Ticket:
        """A ticket created an hour after the previous seeded one (or at `created_at`).

        `created_date` is the Asia/Jerusalem calendar date of `created_at`, as the application stores it.
        """
        when = created_at or BASE_TIME + timedelta(hours=len(self.tickets))
        local_date = when.astimezone(_JERUSALEM).date()
        owner = customer or self.customer
        row = Ticket(
            ticket_number=number if number is not None else next(_numbers),
            customer_id=owner.id,
            category_id=category.id,
            city=city,
            city_key=city.casefold(),
            apartment=apartment,
            phone=phone,
            description="leaking tap",
            status=status,
            assigned_technician_name=technician,
            urgent=urgent,
            created_at=when,
            created_date=local_date,
            updated_at=when,
            locality_id=None if locality is None else locality.id,
            opened_by_username=opened_by,
        )
        self.session.add(row)
        self.session.flush()
        for part in parts:  # the order the rows are written in is not the order they are reported in
            self.session.add(TicketPart(ticket_id=row.id, part_id=part.id))
        self.session.commit()
        self.tickets.append(row)
        return row


def new_uuid() -> str:
    return str(uuid.uuid4())


def report_numbers(client: TestClient, headers: dict[str, str], **params: str) -> list[int]:
    """Every ticket number of the report table for the filters, page after page, in table order."""
    numbers: list[int] = []
    page = 1
    while True:
        response = client.get(REPORT, params={**params, "page": str(page)}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        numbers.extend(item["ticket_number"] for item in body["tickets"]["items"])
        if len(numbers) >= body["total"] or not body["tickets"]["items"]:
            return numbers
        page += 1
