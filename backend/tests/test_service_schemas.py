"""Audience-specific schemas of the service-ticket API (R12.1, R12.2, R12.4, R3.12, R15.2, R16.1).

The customer models are compared field for field with the lists in the requirements, so adding a field
to one of them, or removing one, fails here. Nothing is asserted through `hasattr` on an instance alone:
the field sets come from the model classes and from the JSON schema FastAPI would publish.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from app.service_tickets import schemas
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    TICKET_STATUSES,
    Category,
    Customer,
    HistoryEntry,
    Ticket,
)

# --- the models by role


def _models() -> dict[str, type[BaseModel]]:
    return {
        name: cls
        for name, cls in vars(schemas).items()
        if inspect.isclass(cls)
        and issubclass(cls, BaseModel)
        and cls.__module__ == schemas.__name__
        and not name.startswith("_")
    }


def _request_models() -> dict[str, type[BaseModel]]:
    return {n: c for n, c in _models().items() if issubclass(c, schemas._Request)}


def _response_models() -> dict[str, type[BaseModel]]:
    return {n: c for n, c in _models().items() if issubclass(c, schemas._Response)}


#: Models a customer can receive. Every other response model is for staff.
CUSTOMER_AUDIENCE = (
    "ServiceTokenResponse",
    "CustomerProfile",
    "FormConfig",
    "TicketCreated",
    "CustomerTicketRow",
    "CustomerTicketDetail",
    "CustomerTicketPage",
    "CategoryRef",
    "LabelTexts",
    "FormSettings",
    "LocalityRef",
    "BrandingInfo",
)

#: Fields that only staff may see, or that nobody may see (R12.4, R3.12).
STAFF_ONLY_FIELDS = {
    "history",
    "entry_text",
    "technician_name",
    "assigned_technician_name",
    "updated_at",
    "edited_at",
    "created_at",
    "customer_name",
    "customer_id",
    "photo_key",
    "city_key",
    "locality_id",  # R30: customers see the `locality` reference object, never the id column
    "token_version",
    "is_active",
    "password_hash",
}

CREDENTIAL_FIELDS = {"password_hash", "token_version", "totp_secret", "initial_password"}


def _fields(cls: type[BaseModel]) -> set[str]:
    return set(cls.model_fields)


def _schema_property_names(cls: type[BaseModel]) -> set[str]:
    """Every property name anywhere in the model's JSON schema, nested models included."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(cls.model_json_schema())
    return names


# --- sample stored data

NOW = datetime(2025, 3, 4, 10, 30, tzinfo=UTC)
LATER = datetime(2025, 3, 5, 8, 15, tzinfo=UTC)
CUSTOMER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
CATEGORY_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SECRET_TECHNICIAN = "Technician-Sentinel-Yael"
SECRET_PHOTO_KEY = "tickets/photo-key-sentinel.jpg"
SECRET_CITY_KEY = "city-key-sentinel"
SECRET_HASH = "$2b$12$sentinelhashsentinelhashsentinelhashsentinelhashsentinelha"


def _category() -> Category:
    return Category(id=CATEGORY_ID, seq=1, name_he="אינסטלציה", name_en="Plumbing", is_active=False)


def _ticket(**overrides: Any) -> Ticket:
    """A fully populated stored ticket: every staff-only column holds a recognisable value."""
    values: dict[str, Any] = {
        "id": uuid.UUID("33333333-3333-4333-8333-333333333333"),
        "ticket_number": 10042,
        "customer_id": CUSTOMER_ID,
        "category_id": CATEGORY_ID,
        "city": "Haifa",
        "city_key": SECRET_CITY_KEY,
        "apartment": "12",
        "phone": "0505556677",
        "description": "Leak under the sink",
        "photo_key": SECRET_PHOTO_KEY,
        "urgent": True,
        "status": "in_progress",
        "assigned_technician_name": SECRET_TECHNICIAN,
        "created_at": NOW,
        "created_date": date(2025, 3, 4),
        "updated_at": LATER,
    }
    values.update(overrides)
    return Ticket(**values)


def _customer() -> Customer:
    return Customer(
        id=CUSTOMER_ID,
        phone="0505556677",
        full_name="Dana Levi",
        full_name_key="dana levi",
        city="Haifa",
        apartment="12",
        password_hash=SECRET_HASH,
        must_change_password=False,
        is_active=True,
        token_version=7,
        created_at=NOW,
        updated_at=LATER,
    )


def _category_ref_dict() -> dict[str, Any]:
    return {"id": CATEGORY_ID, "name_he": "אינסטלציה", "name_en": "Plumbing"}


def _staff_row_values() -> dict[str, Any]:
    return {
        "ticket_number": 10042,
        "created_at": NOW,
        "customer_name": "Dana Levi",
        "city": "Haifa",
        "apartment": "12",
        "category": _category_ref_dict(),
        "status": "in_progress",
        "assigned_technician_name": SECRET_TECHNICIAN,
        "urgent": True,
    }


def _staff_detail_values() -> dict[str, Any]:
    entry = {
        "id": uuid.uuid4(),
        "entry_text": "Replaced the seal",
        "technician_name": SECRET_TECHNICIAN,
        "created_at": NOW,
        "edited_at": None,
    }
    return {
        **_staff_row_values(),
        "phone": "0505556677",
        "description": "Leak under the sink",
        "has_photo": True,
        "history": [entry],
        "updated_at": LATER,
    }


# --- the two customer serializers

ROW_FIELDS = {"ticket_number", "category", "created_date", "apartment", "status", "urgent"}
DETAIL_FIELDS = ROW_FIELDS | {"city", "apartment", "phone", "description", "has_photo", "locality"}


def test_customer_ticket_row_has_exactly_the_listed_fields() -> None:
    assert _fields(schemas.CustomerTicketRow) == ROW_FIELDS


def test_customer_ticket_detail_has_exactly_the_row_fields_plus_five() -> None:
    # The row already carries the apartment (R12.1 as amended), so the detail adds four fields to it,
    # and the settlement reference of R30.5. The list row has no settlement.
    assert _fields(schemas.CustomerTicketDetail) == DETAIL_FIELDS
    assert _fields(schemas.CustomerTicketDetail) - _fields(schemas.CustomerTicketRow) == {
        "city",
        "phone",
        "description",
        "has_photo",
        "locality",
    }


def test_category_in_a_customer_row_has_id_and_both_names_only() -> None:
    assert _fields(schemas.CategoryRef) == {"id", "name_he", "name_en"}


@pytest.mark.parametrize("name", ["CustomerTicketRow", "CustomerTicketDetail", "CustomerTicketPage"])
def test_customer_ticket_models_have_no_staff_only_field_anywhere_in_their_schema(name: str) -> None:
    present = _schema_property_names(_models()[name]) & STAFF_ONLY_FIELDS
    assert present == set()


@pytest.mark.parametrize("name", CUSTOMER_AUDIENCE)
def test_nothing_a_customer_receives_carries_a_staff_only_field(name: str) -> None:
    # CustomerProfile and ServiceTokenResponse share no name with the staff list, but the loop proves it.
    assert _schema_property_names(_models()[name]) & STAFF_ONLY_FIELDS == set()


def test_customer_audience_names_all_exist_and_staff_models_are_the_rest() -> None:
    assert set(CUSTOMER_AUDIENCE) <= set(_response_models())
    staff = set(_response_models()) - set(CUSTOMER_AUDIENCE)
    assert {"StaffTicketRow", "StaffTicketDetail", "HistoryEntryOut", "CustomerRecord"} <= staff


# --- built from a fully populated ticket


def test_customer_detail_from_a_fully_populated_ticket_holds_no_staff_data() -> None:
    ticket = _ticket()
    payload = schemas.customer_ticket_detail(ticket, _category()).model_dump(mode="json")

    assert set(payload) == DETAIL_FIELDS
    assert set(payload["category"]) == {"id", "name_he", "name_en"}
    text = json.dumps(payload, ensure_ascii=False)
    for secret in (
        SECRET_TECHNICIAN,
        SECRET_PHOTO_KEY,
        str(CUSTOMER_ID),
        str(ticket.id),
        LATER.isoformat().replace("+00:00", "Z"),
        SECRET_CITY_KEY,
    ):
        assert secret not in text
    # What is allowed is there, with the stored values.
    assert payload["ticket_number"] == 10042
    assert payload["city"] == "Haifa"
    assert payload["apartment"] == "12"
    assert payload["phone"] == "0505556677"
    assert payload["description"] == "Leak under the sink"
    assert payload["created_date"] == "2025-03-04"
    assert payload["status"] == "in_progress"
    assert payload["urgent"] is True
    assert payload["has_photo"] is True


def test_customer_row_from_a_fully_populated_ticket_holds_only_six_fields() -> None:
    payload = schemas.customer_ticket_row(_ticket(), _category()).model_dump(mode="json")
    assert set(payload) == ROW_FIELDS
    assert payload["apartment"] == "12"
    assert payload["category"] == {
        "id": str(CATEGORY_ID),
        "name_he": "אינסטלציה",
        "name_en": "Plumbing",
    }
    assert SECRET_TECHNICIAN not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(("photo_key", "expected"), [(None, False), ("a/b.jpg", True)])
def test_has_photo_reports_presence_without_the_key(photo_key: str | None, expected: bool) -> None:
    detail = schemas.customer_ticket_detail(_ticket(photo_key=photo_key), _category())
    payload = detail.model_dump(mode="json")
    assert payload["has_photo"] is expected
    assert "photo_key" not in payload


def test_an_unset_urgent_flag_and_a_cancelled_status_pass_through() -> None:
    payload = schemas.customer_ticket_row(_ticket(urgent=False, status="cancelled"), _category()).model_dump()
    assert payload["urgent"] is False
    assert payload["status"] == "cancelled"


def test_customer_page_serialises_with_the_envelope_fields_only() -> None:
    page = schemas.CustomerTicketPage(
        items=[schemas.customer_ticket_row(_ticket(), _category())], page=1, page_size=50, total=1
    )
    payload = page.model_dump(mode="json")
    assert set(payload) == {"items", "page", "page_size", "total"}
    assert set(payload["items"][0]) == ROW_FIELDS


# --- a customer response is never built from a staff model


def test_customer_models_and_staff_models_share_no_base_class() -> None:
    customer = [schemas.CustomerTicketRow, schemas.CustomerTicketDetail]
    staff = [schemas.StaffTicketRow, schemas.StaffTicketDetail]
    for c in customer:
        for s in staff:
            assert not issubclass(c, s)
            assert not issubclass(s, c)


@pytest.mark.parametrize("name", ["CustomerTicketRow", "CustomerTicketDetail", "CustomerTicketPage"])
def test_customer_ticket_models_do_not_read_attributes_of_other_objects(name: str) -> None:
    # rom_attributes would let any object with matching attribute names fill the model, so a staff
    # object that happens to share the fields could be copied into a customer response.
    assert not _models()[name].model_config.get("from_attributes", False)


def test_an_object_that_merely_has_the_same_attribute_names_is_refused() -> None:
    # A staff object exposing every customer field by name must still not fill a customer model.
    lookalike = SimpleNamespace(
        ticket_number=10042,
        category=schemas.CategoryRef(**_category_ref_dict()),
        created_date=date(2025, 3, 4),
        status="received",
        urgent=False,
        assigned_technician_name=SECRET_TECHNICIAN,
    )
    with pytest.raises(ValidationError):
        schemas.CustomerTicketRow.model_validate(lookalike)


def test_a_staff_row_instance_is_refused_by_the_customer_row() -> None:
    staff = schemas.StaffTicketRow(**_staff_row_values())
    with pytest.raises(ValidationError):
        schemas.CustomerTicketRow.model_validate(staff)


def test_a_staff_detail_instance_is_refused_by_the_customer_detail() -> None:
    staff = schemas.StaffTicketDetail(**_staff_detail_values())
    with pytest.raises(ValidationError):
        schemas.CustomerTicketDetail.model_validate(staff)


def test_a_staff_payload_dict_is_refused_by_the_customer_detail() -> None:
    payload = schemas.StaffTicketDetail(**_staff_detail_values()).model_dump()
    with pytest.raises(ValidationError) as caught:
        schemas.CustomerTicketDetail.model_validate(payload)
    kinds = {error["type"] for error in caught.value.errors()}
    assert "extra_forbidden" in kinds


STAFF_EXTRAS = [
    "history",
    "assigned_technician_name",
    "updated_at",
    "password_hash",
    "customer_name",
    "photo_key",
    "opened_by_username",  # R26.6
    "parts_replaced",  # R29.9
]


@pytest.mark.parametrize("extra", STAFF_EXTRAS)
def test_a_customer_detail_cannot_be_constructed_with_a_staff_field(extra: str) -> None:
    values = {
        "ticket_number": 10042,
        "category": _category_ref_dict(),
        "created_date": date(2025, 3, 4),
        "status": "received",
        "urgent": False,
        "city": "Haifa",
        "apartment": "12",
        "phone": "0505556677",
        "description": "",
        "has_photo": False,
    }
    schemas.CustomerTicketDetail(**values)  # the base values are valid
    with pytest.raises(ValidationError) as caught:
        schemas.CustomerTicketDetail(**values, **{extra: "x"})
    assert [error["type"] for error in caught.value.errors()] == ["extra_forbidden"]


# --- staff models


def test_staff_row_has_the_fields_of_r15_2() -> None:
    assert _fields(schemas.StaffTicketRow) == {
        "ticket_number",
        "created_at",
        "customer_name",
        "city",
        "apartment",
        "category",
        "status",
        "assigned_technician_name",
        "urgent",
        "locality",  # R30.5: the settlement with its current names, null for a text city
    }


def test_staff_detail_adds_contact_data_history_and_last_modified_time() -> None:
    assert _fields(schemas.StaffTicketDetail) - _fields(schemas.StaffTicketRow) == {
        "phone",
        "description",
        "has_photo",
        "history",
        "updated_at",
        "opened_by_username",  # R26.4: staff only; no customer model has it (R26.6)
        "parts_replaced",  # R29.7: staff only; no customer model has it (R29.9)
    }
    detail = schemas.StaffTicketDetail(**_staff_detail_values()).model_dump(mode="json")
    assert detail["opened_by_username"] is None  # a ticket the customer opened
    assert detail["parts_replaced"] == []  # no resolution recorded
    assert detail["history"][0]["entry_text"] == "Replaced the seal"
    assert detail["history"][0]["edited_at"] is None
    assert detail["assigned_technician_name"] == SECRET_TECHNICIAN


def test_history_entry_is_built_from_a_stored_entry() -> None:
    entry = HistoryEntry(
        id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        entry_text="Done",
        technician_name="Yael",
        created_at=NOW,
        edited_at=LATER,
    )
    out = schemas.HistoryEntryOut.model_validate(entry).model_dump()
    assert set(out) == {"id", "entry_text", "technician_name", "created_at", "edited_at"}
    assert out["edited_at"] == LATER


def test_customer_record_never_carries_the_hash_or_token_version() -> None:
    record = schemas.CustomerRecord.model_validate(_customer())
    payload = record.model_dump(mode="json")
    assert set(payload) == {"id", "full_name", "phone", "city", "apartment", "is_active", "locality"}
    assert payload["locality"] is None
    text = json.dumps(payload)
    assert SECRET_HASH not in text
    assert "token_version" not in payload


def test_customer_profile_hides_id_hash_and_counters() -> None:
    payload = schemas.CustomerProfile.model_validate(_customer()).model_dump(mode="json")
    assert set(payload) == {"full_name", "phone", "city", "apartment", "must_change_password", "locality"}
    assert payload["locality"] is None
    assert SECRET_HASH not in json.dumps(payload)


def test_dashboard_warning_defaults_to_null() -> None:
    payload = schemas.ServiceDashboardResponse(open=3, in_progress=1, urgent=0).model_dump()
    assert payload == {"open": 3, "in_progress": 1, "urgent": 0, "ticket_number_warning": None}
    warned = schemas.ServiceDashboardResponse(
        open=3, in_progress=1, urgent=0, ticket_number_warning=schemas.NumberWarning(remaining=999)
    )
    assert warned.model_dump()["ticket_number_warning"] == {"remaining": 999}


def test_report_has_all_four_statuses_and_the_staff_ticket_page() -> None:
    assert _fields(schemas.StatusCounts) == set(TICKET_STATUSES)
    assert _fields(schemas.ReportResponse) == {"total", "open", "by_status", "by_category", "tickets"}
    report = schemas.ReportResponse(
        total=1,
        open=1,
        by_status=schemas.StatusCounts(received=0, in_progress=1, closed=0, cancelled=0),
        by_category=[schemas.CategoryCount(category=schemas.CategoryRef(**_category_ref_dict()), count=1)],
        tickets=schemas.StaffTicketPage(
            items=[schemas.StaffTicketRow(**_staff_row_values())], page=1, page_size=25, total=1
        ),
    )
    assert report.model_dump(mode="json")["tickets"]["items"][0]["ticket_number"] == 10042


def test_page_envelopes_share_the_same_four_fields() -> None:
    for name in ("CustomerTicketPage", "StaffTicketPage", "CustomerRecordPage"):
        assert _fields(_models()[name]) == {"items", "page", "page_size", "total"}


def test_category_models_and_delete_result() -> None:
    assert _fields(schemas.CategoryRecord) == {"id", "name_he", "name_en", "is_active"}
    record = schemas.CategoryRecord.model_validate(_category()).model_dump()
    assert record["is_active"] is False
    assert schemas.CategoryDeleteResult(result="removed").result == "removed"
    with pytest.raises(ValidationError):
        schemas.CategoryDeleteResult(result="gone")  # type: ignore[arg-type]


def test_settlement_and_part_models_and_delete_results() -> None:
    for record in (schemas.LocalityRecord, schemas.PartRecord):
        assert _fields(record) == {"id", "name_he", "name_en", "is_active"}
        assert record.model_validate(_category()).is_active is False
    for result in (schemas.LocalityDeleteResult, schemas.PartDeleteResult):
        assert result(result="deactivated").result == "deactivated"
        with pytest.raises(ValidationError):
            result(result="gone")  # type: ignore[arg-type]
    assert _fields(schemas.LocalityList) == _fields(schemas.PartList) == {"items"}


def test_form_config_has_its_six_fields_only() -> None:
    assert _fields(schemas.FormConfig) == {
        "categories",
        "labels",
        "settings",
        "localities",
        "settlements_required",
        "branding",
    }
    assert _fields(schemas.FormSettings) == {"description_required", "photo_upload_enabled"}
    assert _fields(schemas.LocalityRef) == {"id", "name_he", "name_en"}  # nothing about the state
    assert _fields(schemas.BrandingInfo) == {"company_name", "has_logo"}  # never the object-store key
    labels = {key: {"he": "ה", "en": "E"} for key in FIELD_LABEL_KEYS}
    config = schemas.FormConfig.model_validate(
        {
            "categories": [_category_ref_dict()],
            "labels": labels,
            "settings": {"description_required": True, "photo_upload_enabled": False},
            "localities": [{"id": CATEGORY_ID, "name_he": "חיפה", "name_en": "Haifa"}],
            "settlements_required": True,
            "branding": {"company_name": None, "has_logo": False},
        }
    )
    assert set(config.labels) == set(FIELD_LABEL_KEYS)
    with pytest.raises(ValidationError):
        schemas.FormConfig.model_validate(
            {
                "categories": [],
                "labels": {"nickname": {"he": "ה", "en": "E"}},
                "settings": {"description_required": True, "photo_upload_enabled": True},
                "localities": [],
                "settlements_required": False,
                "branding": {"company_name": None, "has_logo": False},
            }
        )


def test_token_response_fields_and_bearer_type() -> None:
    expected = {"access_token", "token_type", "expires_in", "must_change_password"}
    assert _fields(schemas.ServiceTokenResponse) == expected
    token = schemas.ServiceTokenResponse(access_token="t", expires_in=900, must_change_password=True)
    assert token.token_type == "bearer"
    with pytest.raises(ValidationError):
        schemas.ServiceTokenResponse(  # type: ignore[arg-type]
            access_token="t", token_type="mac", expires_in=1, must_change_password=False
        )
    with pytest.raises(ValidationError):  # there is no refresh token
        schemas.ServiceTokenResponse(  # type: ignore[call-arg]
            access_token="t", expires_in=1, must_change_password=False, refresh_token="r"
        )


# --- no schema carries a credential


def test_the_module_defines_exactly_these_models() -> None:
    # A new schema must be added here, and so be assigned a place in the audience tables above.
    assert set(_models()) == set(VALID_REQUESTS) | {
        "CategoryCount",
        "CategoryDeleteResult",
        "CategoryList",
        "CategoryRecord",
        "CategoryRef",
        "CustomerProfile",
        "CustomerRecord",
        "CustomerRecordPage",
        "CustomerTicketDetail",
        "CustomerTicketPage",
        "CustomerTicketRow",
        "ServiceDashboardResponse",
        "BrandingInfo",
        "FormConfig",
        "FormSettings",
        "HistoryEntryOut",
        "LabelTexts",
        "LabelsResponse",
        "LocalityDeleteResult",
        "LocalityList",
        "LocalityRecord",
        "LocalityRef",
        "NumberWarning",
        "PartDeleteResult",
        "PartList",
        "PartRecord",
        "PartRef",
        "ReportResponse",
        "SettingsResponse",
        "StaffTicketDetail",
        "StaffTicketPage",
        "StaffTicketRow",
        "StatusCounts",
        "TicketCreated",
        "ServiceTokenResponse",
    }
    assert set(_request_models()) | set(_response_models()) == set(_models())
    assert set(_request_models()).isdisjoint(_response_models())

@pytest.mark.parametrize("name", sorted(_models()))
def test_no_schema_has_a_password_hash_or_other_credential_field(name: str) -> None:
    properties = _schema_property_names(_models()[name])
    assert properties & CREDENTIAL_FIELDS == set()
    assert not any("hash" in prop for prop in properties)


@pytest.mark.parametrize("name", sorted(_response_models()))
def test_no_response_exposes_a_password_field_besides_the_change_flag(name: str) -> None:
    properties = _schema_property_names(_response_models()[name])
    assert {prop for prop in properties if "password" in prop} <= {"must_change_password"}


# --- requests forbid unknown fields

#: A minimal valid body for each request model.
VALID_REQUESTS: dict[str, dict[str, Any]] = {
    "SignInRequest": {"phone": "0505556677", "password": "1234"},
    "ChangePasswordRequest": {"current_password": "1234", "new_password": "5678"},
    "CancelTicketRequest": {"expected_status": "received"},
    "StatusChangeRequest": {"expected_status": "received", "target_status": "in_progress"},
    "ResolutionRequest": {
        "entry_text": "Replaced the seal",
        "technician_name": "Yael",
        "parts_replaced": False,
        "part_ids": [],
    },
    "AssignedTechnicianRequest": {"assigned_technician_name": "Yael"},
    "HistoryEntryRequest": {"entry_text": "Done", "technician_name": "Yael"},
    "CustomerCreateRequest": {"phone": "0505556677", "full_name": "Dana Levi"},
    "CustomerUpdateRequest": {"phone": "0505556677", "full_name": "Dana Levi"},
    "CategoryCreateRequest": {"name_he": "שרברבות", "name_en": "Plumbing"},
    "CategoryUpdateRequest": {"name_he": "שרברבות", "name_en": "Plumbing"},
    "LocalityCreateRequest": {"name_he": "חיפה", "name_en": "Haifa"},
    "LocalityUpdateRequest": {"name_he": "חיפה", "name_en": "Haifa"},
    "PartCreateRequest": {"name_he": "מסנן", "name_en": "Filter"},
    "PartUpdateRequest": {"name_he": "מסנן", "name_en": "Filter"},
    "LabelTextsRequest": {"he": "עיר", "en": "City"},
    "LabelsUpdateRequest": {"labels": {"city": {"he": "עיר", "en": "City"}}},
    "SettingsUpdateRequest": {"description_required": True, "photo_upload_enabled": False},
}


def test_every_request_model_has_a_valid_body_in_the_table() -> None:
    assert set(VALID_REQUESTS) == set(_request_models())


@pytest.mark.parametrize("name", sorted(VALID_REQUESTS))
def test_request_model_accepts_its_valid_body(name: str) -> None:
    _request_models()[name].model_validate(VALID_REQUESTS[name])


@pytest.mark.parametrize("name", sorted(VALID_REQUESTS))
def test_request_model_rejects_an_unknown_field_as_extra_forbidden(name: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _request_models()[name].model_validate({**VALID_REQUESTS[name], "owner_id": "x"})
    assert [(error["type"], error["loc"]) for error in caught.value.errors()] == [
        ("extra_forbidden", ("owner_id",))
    ]


@pytest.mark.parametrize("name", sorted(VALID_REQUESTS))
def test_request_model_is_configured_to_forbid_extras(name: str) -> None:
    assert _request_models()[name].model_config.get("extra") == "forbid"


def test_a_nested_label_body_also_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError) as caught:
        schemas.LabelsUpdateRequest.model_validate({"labels": {"city": {"he": "ע", "en": "C", "fr": "V"}}})
    assert {error["type"] for error in caught.value.errors()} == {"extra_forbidden"}


def test_a_staff_status_body_cannot_set_the_status_directly() -> None:
    with pytest.raises(ValidationError):
        schemas.StatusChangeRequest.model_validate({"expected_status": "received", "status": "closed"})


@pytest.mark.parametrize("name", sorted(_response_models()))
def test_every_response_model_forbids_extras(name: str) -> None:
    assert _response_models()[name].model_config.get("extra") == "forbid"


# --- decisions about what requests accept


@pytest.mark.parametrize("value", [None, "bogus", "closed", 5])
def test_status_values_reach_the_lifecycle_service_unchecked(value: Any) -> None:
    # The services answer invalid_expected_status and invalid_target_status (R13.10, R14.11).
    cancel = schemas.CancelTicketRequest.model_validate({"expected_status": value})
    assert cancel.expected_status == value
    change = schemas.StatusChangeRequest.model_validate({"expected_status": value, "target_status": value})
    assert (change.expected_status, change.target_status) == (value, value)


def test_a_resolution_reaches_the_service_with_every_value_unchecked() -> None:
    # Bad values must get as far as the resolution service, which names every failing field (R29.1).
    body = {
        "expected_status": "in_progress",
        "target_status": "closed",
        "resolution": {"entry_text": 5, "technician_name": None, "parts_replaced": "yes", "part_ids": "x"},
    }
    resolution = schemas.StatusChangeRequest.model_validate(body).resolution
    assert resolution is not None
    assert (resolution.entry_text, resolution.technician_name) == (5, None)
    assert (resolution.parts_replaced, resolution.part_ids) == ("yes", "x")
    assert schemas.StatusChangeRequest.model_validate({}).resolution is None
    assert schemas.StatusChangeRequest.model_validate({"resolution": None}).resolution is None


def test_status_fields_may_be_absent() -> None:
    assert schemas.CancelTicketRequest.model_validate({}).expected_status is None
    change = schemas.StatusChangeRequest.model_validate({})
    absent = (change.expected_status, change.target_status, change.assigned_technician_name)
    assert absent == (None, None, None)


@pytest.mark.parametrize("value", ["yes", 1, "true", None])
def test_settings_booleans_are_strict(value: Any) -> None:
    with pytest.raises(ValidationError):
        body = {"description_required": value, "photo_upload_enabled": True}
        schemas.SettingsUpdateRequest.model_validate(body)


def test_text_limits_are_not_enforced_by_the_request_models() -> None:
    # One code per failing field comes from the services, so an over-long value must get that far.
    long = "x" * 5000
    schemas.CustomerCreateRequest(phone=long, full_name=long, city=long, apartment=long)
    schemas.HistoryEntryRequest(entry_text=long, technician_name=long)
    schemas.CategoryCreateRequest(name_he="", name_en=long)
    schemas.LocalityCreateRequest(name_he="", name_en=long)
    schemas.PartCreateRequest(name_he="", name_en=long)
    schemas.LabelTextsRequest(he="", en=long)


def test_labels_update_rejects_a_field_that_is_not_a_form_field() -> None:
    with pytest.raises(ValidationError):
        schemas.LabelsUpdateRequest.model_validate({"labels": {"nickname": {"he": "ה", "en": "E"}}})


# --- literals follow the stored vocabulary


def test_status_literal_equals_the_stored_statuses_in_order() -> None:
    assert get_args(schemas.TicketStatus) == TICKET_STATUSES


def test_field_label_literal_equals_the_stored_label_keys() -> None:
    assert get_args(schemas.FieldLabelKey) == FIELD_LABEL_KEYS


def test_an_unknown_status_is_refused_by_a_response() -> None:
    with pytest.raises(ValidationError):
        schemas.CustomerTicketRow(
            ticket_number=10042,
            category=schemas.CategoryRef(**_category_ref_dict()),
            created_date=date(2025, 3, 4),
            apartment="12",
            status="deleted",  # type: ignore[arg-type]
            urgent=False,
        )