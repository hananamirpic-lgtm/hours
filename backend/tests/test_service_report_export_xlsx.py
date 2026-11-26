"""`GET /staff/reports/export`: the Excel workbook of the report (task 30.6; R31.1 to R31.5).

The real application, each request on its own session of the in-memory test database, tickets seeded
directly. Workbooks are read back with `openpyxl.load_workbook`, so what is checked is the file a
spreadsheet program would open: values, cell types, number formats, sheet flags, the raw XML for
formulas.

The seeded tickets, newest first, as the export must list them (created time descending, then ticket
number descending):

    20020  received  Electric  Tel Aviv (text city)   urgent, opened by staff `yossi`
    20011  closed    Plumbing  Haifa (a settlement)   same instant as 20010, the higher number first
    20010  closed    Plumbing  Haifa (a settlement)   parts Valve and Washer, written in the other order
    20030  cancelled Electric  Eilat (text city)      created 2025-03-09 22:30 UTC, which is already
                                                      2025-03-10 in Asia/Jerusalem (the opened date)
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from service_report_support import (
    BASE_TIME,
    EXPORT,
    REPORT,
    TICKET_STAFF_ROLES,
    Seeder,
    bearer,
    code,
    make_client,
    report_numbers,
)
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import Category, Locality, Part
from app.service_tickets.services import report_export
from app.service_tickets.services.report_export import (
    CELL_MAX_CHARS,
    EXPORT_MAX_ROWS,
    HEADERS,
    safe_text,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HE_HEADERS = (
    "מספר קריאה",
    "תאריך פתיחה",
    "שם הדייר",
    "טלפון",
    "ישוב",
    "דירה",
    "קטגוריית תקלה",
    "סטטוס",
    "טכנאי מטפל",
    "דחוף",
    "חלקים שהוחלפו",
    "נפתחה על ידי הצוות",
)
EN_HEADERS = (
    "Ticket number",
    "Opened",
    "Customer",
    "Phone",
    "Settlement",
    "Apartment",
    "Issue category",
    "Status",
    "Assigned technician",
    "Urgent",
    "Parts replaced",
    "Opened by staff",
)


# --------------------------------------------------------------------------- harness


@pytest.fixture
def client(settings, session: Session) -> TestClient:
    return make_client(settings, session)


@pytest.fixture
def headers(make_user) -> dict[str, str]:
    user = make_user(role=UserRole.TECHNICIAN, is_2fa_enabled=True, must_change_password=False)
    return bearer(user, UserRole.TECHNICIAN)


def export(client: TestClient, headers: dict[str, str] | None, **params: str) -> Any:
    return client.get(EXPORT, params=params, headers=headers or {})


def rows_of(response: Any) -> list[list[Any]]:
    """Every row of the one worksheet, as plain values (an empty text cell reads back as `None`)."""
    assert response.status_code == 200, response.text
    sheet = load_workbook(BytesIO(response.content)).active
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def sheet_of(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return load_workbook(BytesIO(response.content)).active


def numbers_of(response: Any) -> list[int]:
    return [row[0] for row in rows_of(response)[1:]]


def sheet_xml(response: Any) -> str:
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


class Shop:
    """The seeded rows of the main scenario."""

    seed: Seeder
    plumbing: Category
    electric: Category
    washer: Part
    valve: Part
    haifa: Locality


@pytest.fixture
def shop(session: Session) -> Shop:
    seed = Seeder(session)
    world = Shop()
    world.seed = seed
    world.plumbing = seed.category("Plumbing", "אינסטלציה")
    world.electric = seed.category("Electric", "חשמל")
    world.washer = seed.part("Washer", "דיסקית")  # created first, so it comes first by part sequence
    world.valve = seed.part("Valve", "שסתום")
    world.haifa = seed.locality("Haifa", "חיפה")
    seed.ticket(
        world.plumbing,
        number=20010,
        status="closed",
        city="Haifa-stored",
        locality=world.haifa,
        parts=(world.valve, world.washer),
        technician="Dana",
        created_at=BASE_TIME,
    )
    seed.ticket(
        world.plumbing,
        number=20011,
        status="closed",
        city="Haifa-stored",
        locality=world.haifa,
        apartment="7",
        created_at=BASE_TIME,
    )
    seed.ticket(
        world.electric,
        number=20020,
        status="received",
        city="Tel Aviv",
        urgent=True,
        technician="Moshe",
        opened_by="yossi",
        created_at=BASE_TIME.replace(hour=9),
    )
    seed.ticket(
        world.electric,
        number=20030,
        status="cancelled",
        city="Eilat",
        phone="0529876543",
        created_at=datetime(2025, 3, 9, 22, 30, tzinfo=UTC),
    )
    return world


# --------------------------------------------------------------------------- safe_text


@pytest.mark.parametrize("starter", ["=", "+", "-", "@", "\t", "\r"])
def test_safe_text_prefixes_one_quote_before_every_formula_starter(starter: str) -> None:
    assert safe_text(f"{starter}1+1") == f"'{starter}1+1"
    assert safe_text(starter) == f"'{starter}"
    assert safe_text(f"{starter}{starter}") == f"'{starter}{starter}"  # one quote, not one per character


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "a=b",
        "1+1",
        "x-y",
        "user@example.test",
        "  =x",  # a leading space is not a starter
        "\nline",
        "'already quoted",
        "שלום עולם",
        "דירה 12, קומה 3",
        "",
    ],
)
def test_safe_text_leaves_other_text_alone(value: str) -> None:
    assert safe_text(value) == value


def test_safe_text_handles_hebrew_that_starts_with_a_starter() -> None:
    assert safe_text("=שלום") == "'=שלום"
    assert safe_text("-שלום עולם") == "'-שלום עולם"


def test_safe_text_keeps_embedded_newlines_tabs_and_carriage_returns() -> None:
    assert safe_text("first\nsecond\r\nthird\tfourth") == "first\nsecond\r\nthird\tfourth"
    assert safe_text("=a\nb") == "'=a\nb"


def test_safe_text_cuts_text_longer_than_the_excel_cell_limit() -> None:
    assert CELL_MAX_CHARS == 32_767
    assert safe_text("x" * 40_000) == "x" * 32_767
    assert len(safe_text("=" + "x" * 40_000)) == 32_767  # the added quote counts
    assert safe_text("=" + "x" * 40_000).startswith("'=x")
    assert safe_text("ש" * 32_767) == "ש" * 32_767
    assert len(safe_text("ש" * 32_768)) == 32_767


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x00b", "ab"),
        ("a\x07b", "ab"),
        ("\x00\x07\x08\x0b\x0c\x0e\x1f", ""),
        ("x\x1fy", "xy"),
        ("a\ud800b", "ab"),  # a lone surrogate cannot be written either
        ("a\ufffeb\uffff", "ab"),
        ("\x00=1+1", "'=1+1"),  # dropping the character first cannot hide the starter
        ("\x07\x00@home", "'@home"),
    ],
)
def test_safe_text_drops_characters_xml_cannot_hold(raw: str, expected: str) -> None:
    assert safe_text(raw) == expected


def test_safe_text_of_none_and_other_types() -> None:
    assert safe_text(None) == ""
    assert safe_text(12) == "12"


# --------------------------------------------------------------------------- the workbook, Hebrew


def test_the_hebrew_workbook_has_the_specified_headers_rows_and_order(client, headers, shop: Shop) -> None:
    response = export(client, headers, lang="he")

    rows = rows_of(response)

    assert tuple(rows[0]) == HE_HEADERS
    assert rows[1:] == [
        [20020, datetime(2025, 3, 10), "Dana Levi", "0501234567", "Tel Aviv", "12", "חשמל",
         "נקלטה במערכת", "Moshe", "כן", None, "yossi"],
        [20011, datetime(2025, 3, 10), "Dana Levi", "0501234567", "חיפה", "7", "אינסטלציה",
         "טופלה", None, "לא", None, None],
        [20010, datetime(2025, 3, 10), "Dana Levi", "0501234567", "חיפה", "12", "אינסטלציה",
         "טופלה", "Dana", "לא", "דיסקית, שסתום", None],
        [20030, datetime(2025, 3, 10), "Dana Levi", "0529876543", "Eilat", "12", "חשמל",
         "בוטלה", None, "לא", None, None],
    ]  # fmt: skip


def test_the_workbook_cell_types_and_formats(client, headers, shop: Shop) -> None:
    sheet = sheet_of(export(client, headers, lang="he"))

    for row in range(2, 6):
        number, opened, *texts = [sheet.cell(row=row, column=column) for column in range(1, 13)]
        assert number.data_type == "n" and isinstance(number.value, int)
        assert opened.is_date and opened.number_format == "dd/mm/yyyy"
        assert all(cell.data_type == "s" for cell in texts if cell.value is not None)
    phone = sheet.cell(row=2, column=4)
    assert phone.value == "0501234567" and phone.data_type == "s"  # the leading zero stays


def test_the_opened_date_is_the_asia_jerusalem_calendar_date(client, headers, shop: Shop) -> None:
    rows = rows_of(export(client, headers))

    by_number = {row[0]: row[1].date() for row in rows[1:]}

    assert by_number[20030] == date(2025, 3, 10)  # created 2025-03-09 22:30 UTC
    assert by_number[20010] == date(2025, 3, 10)


def test_the_hebrew_sheet_is_right_to_left_with_a_frozen_bold_filtered_header(
    client, headers, shop: Shop
) -> None:
    sheet = sheet_of(export(client, headers, lang="he"))

    assert sheet.sheet_view.rightToLeft is True
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:L5"
    assert all(sheet.cell(row=1, column=column).font.bold for column in range(1, 13))
    assert not any(sheet.cell(row=2, column=column).font.bold for column in range(1, 13))
    assert sheet.max_row == 5 and sheet.max_column == 12


def test_the_column_widths_follow_the_longest_value_up_to_60(client, headers, shop: Shop) -> None:
    long_name = shop.seed.new_customer("ש" * 100)
    shop.seed.ticket(shop.plumbing, customer=long_name)
    shop.seed.ticket(shop.plumbing, customer=shop.seed.new_customer("שם\nשורה שנייה ארוכה יותר מהראשונה"))

    sheet = sheet_of(export(client, headers, lang="he"))

    widths = {letter: sheet.column_dimensions[letter].width for letter in "ABCDEFGHIJKL"}
    assert widths["C"] == 60  # a 100-character name is capped
    assert all(width <= 60 for width in widths.values())
    assert widths["D"] == len("0501234567") + 2
    assert widths["A"] >= len(HE_HEADERS[0])  # never narrower than its header
    assert all(widths[letter] >= len(HE_HEADERS[index]) for index, letter in enumerate("ABCDEFGHIJKL"))


def test_a_cell_with_line_breaks_is_as_wide_as_its_widest_line(client, headers, shop: Shop) -> None:
    shop.seed.ticket(shop.plumbing, customer=shop.seed.new_customer("ab\ncdefghijklmnop\nxyz"))

    sheet = sheet_of(export(client, headers, lang="he"))

    assert sheet.column_dimensions["C"].width == len("cdefghijklmnop") + 2


# --------------------------------------------------------------------------- the workbook, English


def test_the_english_workbook_has_the_specified_headers_rows_and_order(client, headers, shop: Shop) -> None:
    rows = rows_of(export(client, headers, lang="en"))

    assert tuple(rows[0]) == EN_HEADERS
    assert rows[1:] == [
        [20020, datetime(2025, 3, 10), "Dana Levi", "0501234567", "Tel Aviv", "12", "Electric",
         "Received", "Moshe", "Yes", None, "yossi"],
        [20011, datetime(2025, 3, 10), "Dana Levi", "0501234567", "Haifa", "7", "Plumbing",
         "Resolved", None, "No", None, None],
        [20010, datetime(2025, 3, 10), "Dana Levi", "0501234567", "Haifa", "12", "Plumbing",
         "Resolved", "Dana", "No", "Washer, Valve", None],
        [20030, datetime(2025, 3, 10), "Dana Levi", "0529876543", "Eilat", "12", "Electric",
         "Cancelled", None, "No", None, None],
    ]  # fmt: skip


def test_the_english_sheet_is_left_to_right(client, headers, shop: Shop) -> None:
    sheet = sheet_of(export(client, headers, lang="en"))

    assert not sheet.sheet_view.rightToLeft
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:L5"


def test_the_in_progress_status_is_named_in_both_languages(client, headers, shop: Shop) -> None:
    shop.seed.ticket(shop.plumbing, status="in_progress", number=20040)

    he = {row[0]: row[7] for row in rows_of(export(client, headers, lang="he"))[1:]}
    en = {row[0]: row[7] for row in rows_of(export(client, headers, lang="en"))[1:]}

    assert he[20040] == "בטיפול" and en[20040] == "In Progress"


def test_the_header_constants_are_the_specified_ones() -> None:
    assert HEADERS["he"] == HE_HEADERS and HEADERS["en"] == EN_HEADERS


@pytest.mark.parametrize("params", [{}, {"lang": "he"}])
def test_hebrew_is_the_default_language(client, headers, shop: Shop, params) -> None:
    assert tuple(rows_of(export(client, headers, **params))[0]) == HE_HEADERS


@pytest.mark.parametrize("lang", ["fr", "HE", "En", "", " he", "he ", "he,en", "english", "0"])
def test_any_other_language_is_invalid_filter_and_sends_no_file(client, headers, shop: Shop, lang) -> None:
    response = export(client, headers, lang=lang)

    assert response.status_code == 422 and code(response) == "invalid_filter"
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers


# --------------------------------------------------------------------------- filters equal the report


def _scenario(seed: Seeder) -> dict[str, Any]:
    """Forty tickets over two categories, three cities, a settlement, parts and several days."""
    plumbing, electric = seed.category("Plumbing"), seed.category("Electric")
    washer, valve, retired = seed.part("Washer"), seed.part("Valve"), seed.part("Retired", active=False)
    place = seed.locality("Haifa")
    cities = ("Haifa", "Akko", "Eilat")
    for index in range(40):
        seed.ticket(
            plumbing if index % 2 else electric,
            status=("received", "in_progress", "closed", "cancelled")[index % 4],
            city=cities[index % 3],
            locality=place if index % 5 == 0 else None,
            parts=((washer, valve), (valve,), (retired,), ())[index % 4],
            created_at=datetime(2025, 3, 1 + index // 2, 8, index, tzinfo=UTC),
            number=21000 + index,
        )
    return {"plumbing": plumbing, "electric": electric, "washer": washer, "retired": retired, "place": place}


@pytest.fixture
def forty(session: Session) -> dict[str, Any]:
    return _scenario(Seeder(session))


FILTERS: list[Callable[[dict[str, Any]], dict[str, str]]] = [
    lambda w: {},
    lambda w: {"category_id": str(w["plumbing"].id)},
    lambda w: {"city": "akko"},
    lambda w: {"city": " HAIFA "},
    lambda w: {"locality_id": str(w["place"].id)},
    lambda w: {"date_from": "2025-03-05", "date_to": "2025-03-12"},
    lambda w: {"date_from": "2025-03-15"},
    lambda w: {"date_to": "2025-03-03"},
    lambda w: {"part_filter": "yes"},
    lambda w: {"part_filter": "no"},
    lambda w: {"part_filter": "any"},
    lambda w: {"part_filter": str(w["washer"].id)},
    lambda w: {"part_filter": str(w["retired"].id)},
    lambda w: {
        "category_id": str(w["electric"].id),
        "city": "haifa",
        "part_filter": "yes",
        "date_from": "2025-03-02",
        "date_to": "2025-03-18",
    },
    lambda w: {"category_id": str(w["plumbing"].id), "locality_id": str(w["place"].id), "part_filter": "no"},
    lambda w: {"city": "nowhere"},
]


@pytest.mark.parametrize("make_params", FILTERS)
def test_the_export_holds_exactly_the_report_table_across_all_pages_in_its_order(
    client, headers, forty, make_params
) -> None:
    params = make_params(forty)

    table = report_numbers(client, headers, **params)
    exported = numbers_of(export(client, headers, **params))

    assert exported == table  # the same tickets, in the same order, not only the first 25
    report_total = client.get(REPORT, params=params, headers=headers).json()["total"]
    assert len(exported) == report_total


def test_the_unfiltered_export_is_longer_than_one_report_page(client, headers, forty) -> None:
    assert len(numbers_of(export(client, headers))) == 40


def test_the_export_ignores_page_and_unknown_parameters_like_the_report(client, headers, forty) -> None:
    plain = numbers_of(export(client, headers))

    assert numbers_of(export(client, headers, page="2")) == plain
    assert numbers_of(export(client, headers, surprise="1")) == plain
    assert client.get(REPORT, params={"surprise": "1"}, headers=headers).status_code == 200


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"part_filter": "maybe"}, "invalid_filter"),
        ({"part_filter": "YES"}, "invalid_filter"),
        ({"part_filter": "12345678-1234-1234-1234-123456789012"}, "invalid_filter"),
        ({"category_id": "not-a-uuid"}, "invalid_filter"),
        ({"category_id": "12345678-1234-1234-1234-123456789012"}, "invalid_filter"),
        ({"locality_id": "not-a-uuid"}, "invalid_filter"),
        ({"locality_id": "12345678-1234-1234-1234-123456789012"}, "invalid_filter"),
        ({"date_from": "2025-03-10", "date_to": "2025-03-09"}, "invalid_date_range"),
        ({"date_from": "2025-02-30"}, "invalid_date_range"),
    ],
)
def test_invalid_filters_are_answered_as_the_report_answers_them(
    client, headers, forty, params, error
) -> None:
    report = client.get(REPORT, params=params, headers=headers)
    response = export(client, headers, **params)

    assert (response.status_code, code(response)) == (report.status_code, code(report)) == (422, error)
    assert response.json() == report.json()
    assert "content-disposition" not in response.headers


# --------------------------------------------------------------------------- the 10,000 row cap


def test_the_default_cap_is_ten_thousand() -> None:
    assert EXPORT_MAX_ROWS == 10_000


def test_more_matching_tickets_than_the_cap_is_export_too_large_and_no_file(
    client, headers, shop: Shop, monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setattr(report_export, "EXPORT_MAX_ROWS", 3)
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = export(client, headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 422 and code(response) == "export_too_large"
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers
    # The count ran first and nothing was built: no statement read the customers (the row query joins them).
    assert not any("svc_customers" in statement for statement in seen)
    assert any("count(" in statement.lower() for statement in seen)

def test_exactly_the_cap_is_allowed(client, headers, shop: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_export, "EXPORT_MAX_ROWS", 4)

    assert len(numbers_of(export(client, headers))) == 4


def test_a_narrower_filter_gets_under_the_cap(client, headers, shop: Shop, monkeypatch) -> None:
    monkeypatch.setattr(report_export, "EXPORT_MAX_ROWS", 2)

    assert export(client, headers).status_code == 422
    assert numbers_of(export(client, headers, category_id=str(shop.electric.id))) == [20020, 20030]


def test_the_cap_counts_tickets_not_parts(client, headers, shop: Shop, monkeypatch) -> None:
    """One ticket with two parts is one row, so it cannot push a small export over the cap."""
    monkeypatch.setattr(report_export, "EXPORT_MAX_ROWS", 1)

    assert len(numbers_of(export(client, headers, part_filter="yes"))) == 1


# --------------------------------------------------------------------------- headers


def test_the_response_headers(client, headers, shop: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_export, "export_date", lambda: date(2025, 3, 9))

    response = export(client, headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX
    disposition = response.headers["content-disposition"]
    assert disposition == 'attachment; filename="service-tickets-report-20250309.xlsx"'
    assert response.headers.get_list("cache-control") == ["no-store"]
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]  # once, from the middleware
    assert response.content[:2] == b"PK"  # an xlsx file is a zip archive


def test_the_file_name_date_is_the_asia_jerusalem_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """22:30 UTC on 8 March 2025 is 00:30 on 9 March in Jerusalem (UTC+2 before the summer clock change)."""

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime(2025, 3, 8, 22, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(report_export, "datetime", Fixed)

    assert report_export.export_date() == date(2025, 3, 9)
    assert report_export.export_filename() == "service-tickets-report-20250309.xlsx"
    summer = datetime(2025, 7, 8, 21, 30, tzinfo=UTC)  # UTC+3: already 9 July in Jerusalem

    class Summer(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return summer.astimezone(tz)

    monkeypatch.setattr(report_export, "datetime", Summer)
    assert report_export.export_filename() == "service-tickets-report-20250709.xlsx"


def test_the_file_name_holds_only_a_date_whatever_the_filters_say(client, headers, shop: Shop) -> None:
    response = export(client, headers, city='../x"; filename="evil.xlsx', lang="en")

    assert response.status_code == 200
    assert re.fullmatch(
        r'attachment; filename="service-tickets-report-\d{8}\.xlsx"', response.headers["content-disposition"]
    )


def test_export_date_uses_the_clock_without_a_patch(client, headers, shop: Shop) -> None:
    from zoneinfo import ZoneInfo

    before = datetime.now(ZoneInfo("Asia/Jerusalem")).date()
    response = export(client, headers)
    after = datetime.now(ZoneInfo("Asia/Jerusalem")).date()

    stamp = re.search(r"report-(\d{8})\.xlsx", response.headers["content-disposition"]).group(1)  # type: ignore[union-attr]
    assert stamp in {before.strftime("%Y%m%d"), after.strftime("%Y%m%d")}


# --------------------------------------------------------------------------- the number of statements


def _statements_for(client: TestClient, headers: dict[str, str], session: Session, **params: str) -> int:
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert export(client, headers, **params).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(seen)


def test_the_number_of_statements_does_not_grow_with_the_number_of_rows(
    client, headers, session: Session
) -> None:
    seed = Seeder(session)
    category, other = seed.category("Cat"), seed.category("Cat2")
    part_a, part_b = seed.part("A"), seed.part("B")
    place = seed.locality("Place")
    seed.ticket(category, parts=(part_a,))
    seed.ticket(category, locality=place, parts=(part_a, part_b))
    seed.ticket(other)
    filters = {"category_id": str(category.id), "part_filter": str(part_a.id), "locality_id": str(place.id)}

    few = _statements_for(client, headers, session)
    few_filtered = _statements_for(client, headers, session, **filters)
    for index in range(27):
        seed.ticket(
            category if index % 2 else other,
            parts=(part_a, part_b) if index % 3 else (),
            locality=place if index % 4 else None,
            opened_by="yossi",
        )
    many = _statements_for(client, headers, session)
    many_filtered = _statements_for(client, headers, session, **filters)

    assert len(numbers_of(export(client, headers))) == 30
    assert many == few and many_filtered == few_filtered
    assert many <= 10


# --------------------------------------------------------------------------- formulas and illegal text


DANGEROUS = ["=1+1", "+1+1", "-1-1", "@SUM(1)", "\t=1", "\r=1", "=HYPERLINK(\"http://evil.test\",\"x\")",
             "=cmd|' /C calc'!A0", "#N/A", "#REF!", "TRUE", "123"]  # fmt: skip


def _quoted(value: str) -> str:
    return "'" + value if value[0] in "=+-@\t\r" else value


def test_text_cells_are_strings_and_never_formulas(client, headers, session: Session) -> None:
    seed = Seeder(session)
    category = seed.category("=Cat+1", "@קטגוריה")
    part = seed.part("-Part", "+חלק")
    for index, value in enumerate(DANGEROUS):
        customer = seed.new_customer(value)
        seed.ticket(
            category,
            customer=customer,
            number=22000 + index,
            apartment=value[:20],
            technician=value[:100],
            city=value[:100] or "x",
            opened_by=value,
            parts=(part,),
        )

    for lang in ("he", "en"):
        response = export(client, headers, lang=lang)
        sheet = sheet_of(response)

        assert "<f>" not in sheet_xml(response) and "<f " not in sheet_xml(response)
        for row in sheet.iter_rows(min_row=2):
            for column, cell in enumerate(row):
                if column == 0:
                    assert cell.data_type == "n"
                elif column == 1:
                    assert cell.is_date
                else:
                    assert cell.data_type == "s", (cell.coordinate, cell.value, cell.data_type)
        by_number = {row[0]: row for row in rows_of(response)[1:]}
        for index, value in enumerate(DANGEROUS):
            row = by_number[22000 + index]
            assert [cell.replace("\r", "\n") for cell in (row[2], row[4], row[5], row[8], row[11])] == [
                _quoted(value).replace("\r", "\n"),
                _quoted(value[:100]).replace("\r", "\n"),
                _quoted(value[:20]).replace("\r", "\n"),
                _quoted(value[:100]).replace("\r", "\n"),
                _quoted(value).replace("\r", "\n"),
            ]  # (a carriage return inside XML text reads back as a line feed)
            assert row[10] == ("'-Part" if lang == "en" else "'+חלק")
            assert row[6] == ("'=Cat+1" if lang == "en" else "'@קטגוריה")

def test_illegal_characters_are_dropped_and_do_not_raise(client, headers, session: Session) -> None:
    seed = Seeder(session)
    category = seed.category("Cat\x07egory")
    customer = seed.new_customer("Dana\x00 Levi\x07")
    seed.ticket(category, customer=customer, technician="\x00=Tech\x1f", opened_by="\x07", apartment="1\x002")

    response = export(client, headers, lang="en")

    assert response.status_code == 200
    row = rows_of(response)[1]
    assert row[2] == "Dana Levi"
    assert row[5] == "12"
    assert row[6] == "Category"
    assert row[8] == "'=Tech"  # the dropped character cannot hide the starter
    assert row[11] is None  # the whole value was illegal characters: an empty cell


def test_a_long_text_written_to_a_cell_is_cut_in_the_saved_file() -> None:
    """No stored column holds 32,768 characters, so the cut is shown on the writer's cell helper."""
    workbook = Workbook()
    sheet = workbook.active
    report_export._set_text(sheet["A1"], "=" + "x" * 40_000)
    report_export._set_text(sheet["A2"], "\x00" + "ש" * 40_000)
    buffer = BytesIO()
    workbook.save(buffer)

    loaded = load_workbook(BytesIO(buffer.getvalue())).active

    assert loaded["A1"].data_type == "s" and len(loaded["A1"].value) == CELL_MAX_CHARS
    assert loaded["A1"].value.startswith("'=xx")
    assert loaded["A2"].data_type == "s" and loaded["A2"].value == "ש" * CELL_MAX_CHARS

# --------------------------------------------------------------------------- empty result


@pytest.mark.parametrize("lang", ["he", "en"])
def test_no_matching_ticket_is_a_header_only_workbook(client, headers, shop: Shop, lang: str) -> None:
    response = export(client, headers, city="nowhere", lang=lang)

    sheet = sheet_of(response)

    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [list(HEADERS[lang])]
    assert sheet.auto_filter.ref == "A1:L1"
    assert sheet.freeze_panes == "A2"
    assert bool(sheet.sheet_view.rightToLeft) is (lang == "he")


def test_a_database_without_tickets_is_a_header_only_workbook(client, headers) -> None:
    assert len(rows_of(export(client, headers))) == 1


# --------------------------------------------------------------------------- who may export


@pytest.mark.parametrize("role", TICKET_STAFF_ROLES)
def test_every_ticket_staff_role_may_export(client, shop: Shop, make_user, role: UserRole) -> None:
    user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)

    response = export(client, bearer(user, role))

    assert response.status_code == 200 and response.headers["content-type"] == XLSX
    assert len(numbers_of(response)) == 4


@pytest.mark.parametrize("role", [r for r in UserRole if r not in TICKET_STAFF_ROLES])
def test_other_staff_roles_are_refused(client, shop: Shop, make_user, role: UserRole) -> None:
    user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)

    response = export(client, bearer(user, role))

    assert response.status_code == 403 and code(response) == "insufficient_role"
    assert "content-disposition" not in response.headers


def test_a_customer_token_is_refused(client, shop: Shop) -> None:
    token = mint_customer_token(shop.seed.customer.id, 1)

    response = export(client, {"Authorization": f"Bearer {token}"})

    assert response.status_code == 403 and code(response) == "customer_token_not_accepted"


@pytest.mark.parametrize(
    "auth", [None, {"Authorization": "Bearer not.a.token"}, {"Authorization": "Basic eDp5"}]
)
def test_no_valid_token_is_401(client, shop: Shop, auth) -> None:
    response = export(client, auth)

    assert response.status_code == 401
    assert "content-disposition" not in response.headers


def test_with_the_flag_off_the_route_is_an_unknown_path(settings, session: Session, make_user) -> None:
    user = make_user(role=UserRole.ADMIN, is_2fa_enabled=True, must_change_password=False)
    off = make_client(settings, session, enabled=False)

    response = export(off, bearer(user, UserRole.ADMIN), lang="en")
    unknown = off.get("/api/service/staff/no-such-route", headers=bearer(user, UserRole.ADMIN))

    assert response.status_code == 404
    assert response.content == unknown.content


def test_the_export_leaves_the_stored_rows_unchanged(client, headers, shop: Shop, session: Session) -> None:
    from sqlalchemy import func, select

    from app.service_tickets.models import Ticket, TicketPart

    def snapshot() -> tuple[Any, ...]:
        session.rollback()
        return (
            session.scalar(select(func.count()).select_from(Ticket)),
            session.scalar(select(func.count()).select_from(TicketPart)),
            session.scalar(select(func.max(Ticket.updated_at))),
        )

    before = snapshot()
    assert export(client, headers).status_code == 200
    assert snapshot() == before
