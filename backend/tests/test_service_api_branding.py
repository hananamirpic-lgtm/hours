"""The company name and logo: `GET/PUT /staff/branding` and `GET /branding/logo` (task 30.5; R28.1 to R28.7).

The application is the real one (`create_app` with the flag on), so the response headers of the real
middleware stack are under test (`X-Content-Type-Options` appears once). Each request gets its own session
on the in-memory test database, tokens are real, and the object store is `FakePhotoStore`, which records
every call. Nothing reaches a real object store.

Decisions under test:

* A request with no form part is 422 `invalid_request` under `body`; nothing to do is a client mistake.
* A text part named `logo` is `unknown_field`; a repeated `logo` is `invalid_logo_type`.
* Removing the logo when none is stored, or the name when none is set, succeeds and changes nothing.
* With no logo stored, `GET /branding/logo` is a plain 404: empty body, no error code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from test_service_api_photo import FakePhotoStore, jpeg_bytes, png_bytes, too_many_pixels_png

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import UserRole
from app.service_tickets.api import branding_form
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.logo import MAX_LOGO_BYTES
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    Branding,
    Category,
    Customer,
    FieldLabel,
    ServiceSettings,
)
from app.service_tickets.photo_store import LOGO_KEY_PREFIX, check_key, get_photo_store, new_logo_key
from app.service_tickets.services import config_admin

API = "/api/service"
BRANDING = f"{API}/staff/branding"
LOGO = f"{API}/branding/logo"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: The shape of what the API answers for the branding.
EMPTY = {"company_name": None, "has_logo": False}


# --------------------------------------------------------------------------- harness


class LoggingStore(FakePhotoStore):
    """A fake store that can look at the database when an object is deleted, and can fail a delete."""

    def __init__(self) -> None:
        super().__init__()
        self.on_delete: Callable[[str], None] | None = None
        self.delete_raises: Exception | None = None
        self.delete_result = True

    def delete(self, key: str) -> bool:
        if self.on_delete is not None:
            self.on_delete(key)
        if self.delete_raises is not None:
            self.ops.append(("delete", key))
            raise self.delete_raises
        if not self.delete_result:
            self.ops.append(("delete", key))
            return False
        return super().delete(key)


@dataclass
class Faults:
    commit_fails: bool = False


@pytest.fixture(autouse=True)
def _usable_loggers(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Alembic run earlier in the process (`fileConfig`) can leave a logger disabled."""
    from app.service_tickets.api import photo_router

    for owner in (branding_form, photo_router):
        monkeypatch.setattr(owner.logger, "disabled", False)
        monkeypatch.setattr(owner.logger, "propagate", True)


@pytest.fixture
def photos() -> LoggingStore:
    return LoggingStore()


@pytest.fixture
def faults() -> Faults:
    return Faults()


@pytest.fixture
def factory(session: Session) -> sessionmaker[Session]:
    return sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def app(settings, factory: sessionmaker[Session], photos: LoggingStore, faults: Faults) -> FastAPI:
    changes = {"service_tickets_enabled": "true", "service_accessibility_contact": "a@example.test"}
    application = create_app(settings.model_copy(update={"api_prefix": "/api", **changes}))

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            if faults.commit_fails:

                def failing_commit() -> None:
                    raise DBAPIError("COMMIT", {}, Exception("connection lost"))

                per_request.commit = failing_commit  # type: ignore[method-assign]
            yield per_request

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[get_photo_store] = lambda: photos
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("4321")


@pytest.fixture
def customer(session: Session, password_hash: str) -> Customer:
    created = Customer(
        phone="0505550001",
        full_name="Avi Cohen",
        full_name_key="avi cohen",
        city="Haifa",
        apartment="12",
        password_hash=password_hash,
        must_change_password=False,
        token_version=1,
    )
    session.add(created)
    session.commit()
    return created


@pytest.fixture
def staff(make_user):
    def _headers(role: UserRole, **extra: Any) -> dict[str, str]:
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False, **extra)
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture
def admin(staff) -> dict[str, str]:
    return staff(UserRole.ADMIN)


def customer_headers(owner: Customer) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_customer_token(owner.id, owner.token_version)}"}


# --------------------------------------------------------------------------- helpers


def text(**fields: str) -> list[tuple[str, tuple[None, str]]]:
    return [(name, (None, value)) for name, value in fields.items()]


def logo_part(data: bytes, name: str = "logo.png") -> tuple[str, tuple[str, bytes, str]]:
    return ("logo", (name, data, "image/png"))


def put(client: TestClient, headers: dict[str, str] | None, *parts: Any) -> Any:
    return client.put(BRANDING, headers=headers or {}, files=list(parts))


def code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def fields(response: Any) -> dict[str, str]:
    return response.json()["detail"]["error"]["fields"]


def row(session: Session) -> Branding | None:
    session.rollback()
    return session.get(Branding, 1, populate_existing=True)


def stored(session: Session) -> tuple[str | None, str | None]:
    current = row(session)
    return (None, None) if current is None else (current.company_name, current.logo_key)


def big_png(size: tuple[int, int] = (800, 400)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


def noise_png() -> bytes:
    """A PNG of more than 1 MiB that really is an image (random pixels do not compress)."""
    import random

    rng = random.Random(7)
    out = BytesIO()
    Image.frombytes("RGB", (600, 600), rng.randbytes(600 * 600 * 3)).save(out, "PNG", compress_level=0)
    assert len(out.getvalue()) > MAX_LOGO_BYTES
    return out.getvalue()


def seed_logo(session: Session, photos: FakePhotoStore, *, name: str | None = None) -> str:
    key = new_logo_key()
    photos.seed(key, png_bytes((40, 20)))
    session.add(Branding(id=1, company_name=name, logo_key=key))
    session.commit()
    return key


# --------------------------------------------------------------------------- GET /staff/branding


def test_the_empty_branding_reads_as_no_name_and_no_logo(client: TestClient, admin) -> None:
    response = client.get(BRANDING, headers=admin)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_the_branding_read_shows_the_name_and_whether_a_logo_exists_and_never_the_key(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    key = seed_logo(session, photos, name="Acme Service")

    response = client.get(BRANDING, headers=admin)

    assert response.json() == {"company_name": "Acme Service", "has_logo": True}
    assert key not in response.text and "service-tickets" not in response.text
    assert photos.ops == []


# --------------------------------------------------------------------------- PUT: success paths


def test_a_valid_upload_stores_a_clean_png_and_the_name(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    response = put(client, admin, *text(company_name="  Acme Service  "), logo_part(big_png()))

    assert response.status_code == 200
    assert response.json() == {"company_name": "Acme Service", "has_logo": True}
    name, key = stored(session)
    assert name == "Acme Service" and key is not None
    assert check_key(key) == key and key.startswith(LOGO_KEY_PREFIX) and key.endswith(".png")
    assert [op for op, _ in photos.ops] == ["put"]
    assert list(photos.objects) == [key]
    image = Image.open(BytesIO(photos.objects[key]))
    assert (image.format, image.size) == ("PNG", (400, 200))  # re-encoded and shrunk, never kept as sent
    assert key not in response.text and "service-tickets" not in response.text


@pytest.mark.parametrize("source", ["jpeg", "png", "webp"])
def test_jpeg_png_and_webp_are_accepted(client: TestClient, photos: LoggingStore, admin, source: str) -> None:
    if source == "jpeg":
        data = jpeg_bytes((300, 100))
    elif source == "png":
        data = png_bytes((300, 100))
    else:
        out = BytesIO()
        try:
            Image.new("RGB", (300, 100), (1, 2, 3)).save(out, "WEBP")
        except Exception:  # pragma: no cover - a Pillow build without WebP
            pytest.skip("Pillow was built without WebP")
        data = out.getvalue()

    response = put(client, admin, logo_part(data, f"logo.{source}"))

    assert response.status_code == 200 and response.json()["has_logo"] is True
    (key,) = photos.objects
    assert Image.open(BytesIO(photos.objects[key])).format == "PNG"


def test_the_name_alone_changes_only_the_name_and_touches_no_object(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    key = seed_logo(session, photos, name="Old")

    response = put(client, admin, *text(company_name="New name"))

    assert response.json() == {"company_name": "New name", "has_logo": True}
    assert stored(session) == ("New name", key)
    assert photos.ops == [] and key in photos.objects


def test_the_logo_alone_keeps_the_name(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    seed_logo(session, photos, name="Keep me")
    photos.ops.clear()

    response = put(client, admin, logo_part(big_png((100, 50))))

    assert response.json() == {"company_name": "Keep me", "has_logo": True}
    assert stored(session)[0] == "Keep me"


def test_the_name_is_plain_text_stored_and_returned_literally(
    client: TestClient, session: Session, admin
) -> None:
    markup = '<script>alert("x")</script> & <b>Acme</b>'

    response = put(client, admin, *text(company_name=markup))

    assert response.json()["company_name"] == markup
    assert stored(session)[0] == markup
    assert client.get(BRANDING, headers=admin).json()["company_name"] == markup


def test_a_hebrew_name_round_trips(client: TestClient, admin) -> None:
    assert (
        put(client, admin, *text(company_name="שירות מהיר בע״מ")).json()["company_name"] == "שירות מהיר בע״מ"
    )


@pytest.mark.parametrize("length", [1, 100])
def test_the_name_may_be_1_to_100_characters(client: TestClient, admin, length: int) -> None:
    response = put(client, admin, *text(company_name="x" * length))

    assert response.status_code == 200 and response.json()["company_name"] == "x" * length


def test_the_name_is_trimmed_before_its_length_is_checked(client: TestClient, admin) -> None:
    padded = "  " + "y" * 100 + "  "

    assert put(client, admin, *text(company_name=padded)).json()["company_name"] == "y" * 100


def test_the_second_save_wins_and_a_second_logo_replaces_the_first(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    put(client, admin, logo_part(big_png()))
    (first,) = photos.objects

    response = put(client, admin, logo_part(big_png((600, 100))))

    assert response.status_code == 200
    (second,) = photos.objects
    assert second != first
    assert stored(session)[1] == second


# --------------------------------------------------------------------------- PUT: the write order


def test_a_replacement_deletes_the_old_object_only_after_the_commit(
    client: TestClient, session: Session, factory: sessionmaker[Session], photos: LoggingStore, admin
) -> None:
    old = seed_logo(session, photos, name="Acme")
    photos.ops.clear()
    seen: list[tuple[str, str | None]] = []

    def at_delete(key: str) -> None:
        with factory() as other:  # what the database holds at the moment of the delete
            seen.append((key, other.execute(select(Branding.logo_key)).scalar_one()))

    photos.on_delete = at_delete

    response = put(client, admin, logo_part(big_png()))

    assert response.status_code == 200
    (new,) = [key for op, key in photos.ops if op == "put"]
    assert [op for op, _ in photos.ops] == ["put", "delete"]  # the new object first, the old one last
    assert seen == [(old, new)]  # the commit was already visible when the old object went
    assert old not in photos.objects and new in photos.objects


def test_a_failed_commit_deletes_the_new_object_and_keeps_the_old_state(
    client: TestClient, session: Session, photos: LoggingStore, faults: Faults, admin
) -> None:
    old = seed_logo(session, photos, name="Acme")
    photos.ops.clear()
    faults.commit_fails = True

    response = put(client, admin, *text(company_name="Changed"), logo_part(big_png()))

    assert response.status_code == 503 and code(response) == "service_unavailable"
    (new,) = photos.keys("put")
    assert photos.keys("delete") == [new]  # only the new object is deleted
    assert set(photos.objects) == {old}
    assert stored(session) == ("Acme", old)


def test_a_failure_after_the_put_deletes_the_new_object_and_goes_on(
    client: TestClient, session: Session, photos: LoggingStore, monkeypatch: pytest.MonkeyPatch, admin
) -> None:
    old = seed_logo(session, photos)
    photos.ops.clear()

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(config_admin, "update_branding", broken)

    response = put(client, admin, logo_part(big_png()))

    assert response.status_code == 500
    assert [op for op, _ in photos.ops] == ["put", "delete"]
    assert set(photos.objects) == {old}


def test_a_store_that_cannot_take_the_object_is_503_and_nothing_changes(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    old = seed_logo(session, photos, name="Acme")
    photos.ops.clear()
    photos.fail_put = True

    response = put(client, admin, *text(company_name="Changed"), logo_part(big_png()))

    assert response.status_code == 503 and code(response) == "photo_storage_unavailable"
    assert stored(session) == ("Acme", old)
    assert set(photos.objects) == {old}
    assert [op for op, _ in photos.ops] == ["put"]  # no delete: the put stored nothing


@pytest.mark.parametrize("failure", ["false", "raises", "bad-key"])
def test_a_failed_delete_of_the_old_object_never_fails_the_request(
    client: TestClient,
    session: Session,
    photos: LoggingStore,
    caplog: pytest.LogCaptureFixture,
    admin,
    failure: str,
) -> None:
    old = seed_logo(session, photos)
    if failure == "false":
        photos.delete_result = False
    elif failure == "raises":
        photos.delete_raises = ClientError(
            {"Error": {"Code": "InternalError", "Message": "secret"}}, "DeleteObject"
        )
    else:
        # A row that holds a key of an unknown shape: the store refuses it with ValueError.
        row_ = session.get(Branding, 1)
        assert row_ is not None
        row_.logo_key = "service-tickets/branding/legacy-key.png"
        session.commit()
        old = "service-tickets/branding/legacy-key.png"
    logger_name = branding_form.logger.name
    caplog.set_level(logging.ERROR, logger=logger_name)

    response = put(client, admin, logo_part(big_png()))

    assert response.status_code == 200 and response.json()["has_logo"] is True
    new = stored(session)[1]
    assert new is not None and new != old
    assert any("was not deleted" in record.getMessage() for record in caplog.records)
    assert all(old not in record.getMessage() and new not in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- PUT: removal


def test_removing_the_logo_sets_the_key_to_null_then_deletes_the_object(
    client: TestClient, session: Session, factory: sessionmaker[Session], photos: LoggingStore, admin
) -> None:
    old = seed_logo(session, photos, name="Acme")
    photos.ops.clear()
    seen: list[str | None] = []
    photos.on_delete = lambda _key: seen.append(factory().execute(select(Branding.logo_key)).scalar_one())

    response = put(client, admin, *text(remove_logo="true"))

    assert response.json() == {"company_name": "Acme", "has_logo": False}
    assert stored(session) == ("Acme", None)
    assert photos.ops == [("delete", old)] and seen == [None]  # the row was already NULL at the delete
    assert photos.objects == {}


def test_removing_the_name_keeps_the_logo(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    key = seed_logo(session, photos, name="Acme")
    photos.ops.clear()

    response = put(client, admin, *text(remove_name="true"))

    assert response.json() == {"company_name": None, "has_logo": True}
    assert stored(session) == (None, key)
    assert photos.ops == []


def test_both_can_be_removed_at_once(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    seed_logo(session, photos, name="Acme")

    response = put(client, admin, *text(remove_name="true", remove_logo="true"))

    assert response.json() == EMPTY
    assert stored(session) == (None, None)
    assert photos.objects == {}


def test_removing_what_is_not_there_succeeds_and_changes_nothing(
    client: TestClient, photos: LoggingStore, admin
) -> None:
    response = put(client, admin, *text(remove_name="true", remove_logo="true"))

    assert response.status_code == 200 and response.json() == EMPTY
    assert photos.ops == []


def test_a_new_name_with_the_logo_removed_and_a_new_logo_with_the_name_removed(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    seed_logo(session, photos, name="Acme")

    first = put(client, admin, *text(company_name="Beta", remove_logo="true"))
    second = put(client, admin, *text(remove_name="true"), logo_part(big_png()))

    assert first.json() == {"company_name": "Beta", "has_logo": False}
    assert second.json() == {"company_name": None, "has_logo": True}
    assert len(photos.objects) == 1


# --------------------------------------------------------------------------- PUT: refusals store nothing


def untouched(session: Session, photos: LoggingStore, before: tuple, objects: set[str]) -> None:
    assert stored(session) == before
    assert set(photos.objects) == objects
    assert photos.keys("put") == [] and photos.keys("delete") == []


@pytest.mark.parametrize(
    ("parts", "status", "expected_code", "expected_fields"),
    [
        # the company name
        ([text(company_name="")[0]], 422, "invalid_company_name", {"company_name": "invalid_company_name"}),
        (
            [text(company_name="   ")[0]],
            422,
            "invalid_company_name",
            {"company_name": "invalid_company_name"},
        ),
        (
            [text(company_name="x" * 101)[0]],
            422,
            "invalid_company_name",
            {"company_name": "invalid_company_name"},
        ),
        (
            [text(company_name="a\x00b")[0]],
            422,
            "invalid_company_name",
            {"company_name": "invalid_company_name"},
        ),
        (
            [text(company_name="a")[0], text(company_name="b")[0]],
            422,
            "invalid_company_name",
            {"company_name": "invalid_company_name"},
        ),
        (
            [("company_name", ("n.txt", b"Acme", "text/plain"))],
            422,
            "invalid_company_name",
            {"company_name": "invalid_company_name"},
        ),
        # the logo
        ([logo_part(b"not an image")], 422, "invalid_logo_type", {"logo": "invalid_logo_type"}),
        ([logo_part(b"")], 422, "invalid_logo_type", {"logo": "invalid_logo_type"}),
        (
            [logo_part(b"GIF89a\x01\x00\x01\x00\x00\x00\x00;")],
            422,
            "invalid_logo_type",
            {"logo": "invalid_logo_type"},
        ),
        ([logo_part(png_bytes((50, 50))[:-20])], 422, "invalid_logo_type", {"logo": "invalid_logo_type"}),
        (
            [logo_part(png_bytes((50, 50)) + b"<script>")],
            422,
            "invalid_logo_type",
            {"logo": "invalid_logo_type"},
        ),
        (
            [logo_part(png_bytes()), logo_part(png_bytes())],
            422,
            "invalid_logo_type",
            {"logo": "invalid_logo_type"},
        ),
        (
            [logo_part(too_many_pixels_png())],
            422,
            "logo_dimensions_too_large",
            {"logo": "logo_dimensions_too_large"},
        ),
        (
            [logo_part(png_bytes((2001, 10)))],
            422,
            "logo_dimensions_too_large",
            {"logo": "logo_dimensions_too_large"},
        ),
        ([text(logo="a text, not a file")[0]], 422, "unknown_field", {"logo": "unknown_field"}),
        # flags
        ([text(remove_logo="false")[0]], 422, "invalid_request", {"remove_logo": "invalid_request"}),
        ([text(remove_name="True")[0]], 422, "invalid_request", {"remove_name": "invalid_request"}),
        ([text(remove_name="")[0]], 422, "invalid_request", {"remove_name": "invalid_request"}),
        ([text(remove_logo=" true")[0]], 422, "invalid_request", {"remove_logo": "invalid_request"}),
        (
            [text(remove_logo="true")[0], text(remove_logo="true")[0]],
            422,
            "invalid_request",
            {"remove_logo": "invalid_request"},
        ),
        # contradictions
        (
            [logo_part(png_bytes()), text(remove_logo="true")[0]],
            422,
            "unknown_field",
            {"logo": "unknown_field"},
        ),
        (
            [text(company_name="Acme")[0], text(remove_name="true")[0]],
            422,
            "unknown_field",
            {"company_name": "unknown_field"},
        ),
        # unknown parts
        ([text(surprise="1")[0]], 422, "unknown_field", {"surprise": "unknown_field"}),
        ([("photo", ("a.png", png_bytes(), "image/png"))], 422, "unknown_field", {"photo": "unknown_field"}),
        (
            [text(company_name="Acme")[0], text(Company_Name="x")[0]],
            422,
            "unknown_field",
            {"Company_Name": "unknown_field"},
        ),
    ],
)
def test_every_refusal_stores_nothing(
    client: TestClient,
    session: Session,
    photos: LoggingStore,
    admin,
    parts: list,
    status: int,
    expected_code: str,
    expected_fields: dict[str, str],
) -> None:
    old = seed_logo(session, photos, name="Acme")
    photos.ops.clear()

    response = put(client, admin, *parts)

    assert response.status_code == status
    assert code(response) == expected_code
    assert fields(response) == expected_fields
    untouched(session, photos, ("Acme", old), {old})


def test_a_logo_over_one_mebibyte_is_413_logo_too_large_and_stores_nothing(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    old = seed_logo(session, photos, name="Acme")
    photos.ops.clear()
    data = noise_png()

    response = put(client, admin, *text(company_name="Changed"), logo_part(data))

    assert response.status_code == 413 and code(response) == "logo_too_large"
    untouched(session, photos, ("Acme", old), {old})


def test_exactly_one_mebibyte_is_judged_by_its_content(
    client: TestClient, photos: LoggingStore, admin
) -> None:
    response = put(client, admin, logo_part(b"x" * MAX_LOGO_BYTES))

    assert response.status_code == 422 and code(response) == "invalid_logo_type"
    assert photos.ops == []


def test_a_bad_name_with_a_good_logo_stores_neither(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    response = put(client, admin, *text(company_name="x" * 101), logo_part(big_png()))

    assert response.status_code == 422 and code(response) == "invalid_company_name"
    assert stored(session) == (None, None)
    assert photos.ops == [] and photos.objects == {}


def test_a_good_name_with_a_bad_logo_stores_neither(
    client: TestClient, session: Session, photos: LoggingStore, admin
) -> None:
    response = put(client, admin, *text(company_name="Acme"), logo_part(b"not an image"))

    assert response.status_code == 422 and fields(response) == {"logo": "invalid_logo_type"}
    assert stored(session) == (None, None)
    assert photos.ops == []


def test_every_failing_part_is_reported_in_form_order(client: TestClient, admin) -> None:
    response = put(
        client,
        admin,
        *text(company_name="x" * 101),
        logo_part(b"nope"),
        *text(surprise="1", remove_name="no"),
    )

    assert response.status_code == 422
    assert fields(response) == {
        "company_name": "invalid_company_name",
        "logo": "invalid_logo_type",
        "surprise": "unknown_field",
        "remove_name": "invalid_request",
    }
    assert code(response) == "invalid_company_name"


def test_a_request_with_no_part_is_refused(client: TestClient, photos: LoggingStore, admin) -> None:
    for response in (
        client.put(BRANDING, headers=admin, files=[]),
        client.put(BRANDING, headers=admin, json={"company_name": "Acme"}),
        client.put(BRANDING, headers=admin),
    ):
        assert response.status_code == 422
        assert fields(response) == {"body": "invalid_request"}
    assert photos.ops == []


def test_a_malformed_multipart_body_is_422(client: TestClient, admin) -> None:
    response = client.put(
        BRANDING, headers={**admin, "Content-Type": "multipart/form-data; boundary=zzz"}, content=b"garbage"
    )

    assert response.status_code == 422
    assert fields(response) == {"body": "invalid_request"}


def test_the_form_values_never_decide_anything_but_their_content(client: TestClient, admin) -> None:
    """The file name and the declared type of the logo are ignored: the bytes decide."""
    response = put(client, admin, ("logo", ("logo.png", jpeg_bytes((60, 30)), "text/html")))

    assert response.status_code == 200
    bad = put(client, admin, ("logo", ("logo.png", b"<svg/>", "image/png")))
    assert bad.status_code == 422 and code(bad) == "invalid_logo_type"


def test_the_company_name_validator_refuses_a_lone_surrogate() -> None:
    assert config_admin.clean_company_name("\ud800") is None
    assert config_admin.clean_company_name("ok\udfffok") is None
    assert config_admin.clean_company_name("  ok  ") == "ok"
    assert config_admin.clean_company_name(None) is None
    assert config_admin.clean_company_name(5) is None


# --------------------------------------------------------------------------- permissions


ROUTES = [("GET", BRANDING), ("PUT", BRANDING), ("GET", LOGO)]


def send(client: TestClient, method: str, path: str, headers: dict[str, str] | None) -> Any:
    if method == "PUT":
        return client.put(path, headers=headers or {}, files=text(company_name="Acme"))
    return client.get(path, headers=headers or {})


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_a_technician_gets_403_on_the_branding_routes(
    client: TestClient, session: Session, staff, method: str
) -> None:
    response = send(client, method, BRANDING, staff(UserRole.TECHNICIAN))

    assert response.status_code == 403 and code(response) == "insufficient_role"
    assert stored(session) == (None, None)


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_a_customer_token_gets_403_on_the_branding_routes(
    client: TestClient, session: Session, customer: Customer, method: str
) -> None:
    response = send(client, method, BRANDING, customer_headers(customer))

    assert response.status_code == 403 and code(response) == "customer_token_not_accepted"
    assert stored(session) == (None, None)


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_no_token_is_401_on_the_branding_routes(client: TestClient, method: str) -> None:
    for headers in (None, {"Authorization": "Bearer not.a.token"}):
        response = send(client, method, BRANDING, headers)
        assert response.status_code == 401


@pytest.mark.parametrize("role", [UserRole.ACCOUNTING, UserRole.EMPLOYEE, UserRole.SITE_MANAGER])
def test_other_hours_roles_get_403_on_every_branding_route(client: TestClient, staff, role: UserRole) -> None:
    for method, path in ROUTES:
        response = send(client, method, path, staff(role))
        assert response.status_code == 403 and code(response) == "insufficient_role", (method, path)


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN])
def test_configuration_staff_may_read_and_write(client: TestClient, staff, role: UserRole) -> None:
    headers = staff(role)

    assert put(client, headers, *text(company_name="Acme")).status_code == 200
    assert client.get(BRANDING, headers=headers).json() == {"company_name": "Acme", "has_logo": False}


def test_a_deactivated_staff_user_is_401(client: TestClient, staff) -> None:
    headers = staff(UserRole.ADMIN, is_active=False)

    assert send(client, "PUT", BRANDING, headers).status_code == 401
    assert send(client, "GET", LOGO, headers).status_code == 401


def test_a_refused_write_changes_nothing_for_every_unauthorised_caller(
    client: TestClient, session: Session, photos: LoggingStore, staff, customer: Customer
) -> None:
    for headers in (None, staff(UserRole.TECHNICIAN), staff(UserRole.ACCOUNTING), customer_headers(customer)):
        response = client.put(
            BRANDING, headers=headers or {}, files=[*text(company_name="Evil"), logo_part(big_png())]
        )
        assert response.status_code in (401, 403)
    assert stored(session) == (None, None)
    assert photos.ops == []


def test_the_flag_off_makes_all_three_routes_unknown_paths(settings) -> None:
    changes = {"service_tickets_enabled": "", "api_prefix": "/api"}
    off = TestClient(create_app(settings.model_copy(update=changes)), raise_server_exceptions=False)

    for method, path in ROUTES:
        response = off.request(method, path)
        assert response.status_code == 404, (method, path)
        assert response.json() == {"detail": "Not Found"}


# --------------------------------------------------------------------------- GET /branding/logo


def uploaded(client: TestClient, admin, photos: LoggingStore) -> bytes:
    assert put(client, admin, logo_part(big_png())).status_code == 200
    (key,) = photos.objects
    return photos.objects[key]


def test_a_signed_in_customer_gets_the_logo_with_the_required_headers(
    client: TestClient, customer: Customer, photos: LoggingStore, admin
) -> None:
    expected = uploaded(client, admin, photos)

    response = client.get(LOGO, headers=customer_headers(customer))

    assert response.status_code == 200
    assert response.content == expected and response.content.startswith(PNG_SIGNATURE)
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]  # once, from the middleware


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN])
def test_each_ticket_staff_role_gets_the_logo(
    client: TestClient, photos: LoggingStore, staff, admin, role: UserRole
) -> None:
    expected = uploaded(client, admin, photos)

    response = client.get(LOGO, headers=staff(role))

    assert response.status_code == 200 and response.content == expected
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


def test_no_token_or_a_bad_token_gets_401(client: TestClient, photos: LoggingStore, admin) -> None:
    uploaded(client, admin, photos)

    for headers in ({}, {"Authorization": "Bearer not.a.token"}):
        response = client.get(LOGO, headers=headers)
        assert response.status_code == 401
        assert b"\x89PNG" not in response.content


def test_a_customer_who_owes_a_password_change_is_403(
    client: TestClient, session: Session, customer: Customer, photos: LoggingStore, admin
) -> None:
    uploaded(client, admin, photos)
    customer.must_change_password = True
    session.commit()

    response = client.get(LOGO, headers=customer_headers(customer))

    assert response.status_code == 403 and code(response) == "password_change_required"


def test_with_no_logo_the_answer_is_a_plain_404_with_no_body_and_no_code(
    client: TestClient, customer: Customer, photos: LoggingStore, staff
) -> None:
    for headers in (customer_headers(customer), staff(UserRole.TECHNICIAN)):
        response = client.get(LOGO, headers=headers)

        assert response.status_code == 404
        assert response.content == b""
        assert "content-type" not in response.headers
        assert response.headers["cache-control"] == "no-store"
    assert photos.ops == []


def test_a_logo_removed_after_the_form_config_was_read_is_that_same_404(
    client: TestClient, customer: Customer, admin
) -> None:
    put(client, admin, logo_part(big_png()))
    put(client, admin, *text(remove_logo="true"))

    response = client.get(LOGO, headers=customer_headers(customer))

    assert (response.status_code, response.content) == (404, b"")


def test_a_row_whose_object_is_gone_is_the_same_404_and_logs_an_error(
    client: TestClient,
    session: Session,
    customer: Customer,
    photos: LoggingStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = seed_logo(session, photos)
    photos.objects.clear()
    from app.service_tickets.api import photo_router

    caplog.set_level(logging.ERROR, logger=photo_router.logger.name)

    response = client.get(LOGO, headers=customer_headers(customer))

    assert (response.status_code, response.content) == (404, b"")
    assert any("logo" in record.getMessage() for record in caplog.records)
    assert all(key not in record.getMessage() for record in caplog.records)


def test_a_row_with_a_key_of_an_unknown_shape_is_the_same_404(
    client: TestClient, session: Session, customer: Customer
) -> None:
    session.add(Branding(id=1, logo_key="service-tickets/branding/secret.png"))
    session.commit()

    response = client.get(LOGO, headers=customer_headers(customer))

    assert (response.status_code, response.content) == (404, b"")


def test_a_store_that_cannot_be_read_is_503(
    client: TestClient, session: Session, customer: Customer, photos: LoggingStore
) -> None:
    seed_logo(session, photos)
    photos.fail_get = True

    response = client.get(LOGO, headers=customer_headers(customer))

    assert response.status_code == 503 and code(response) == "photo_storage_unavailable"


def test_delivery_writes_nothing(
    client: TestClient, session: Session, customer: Customer, photos: LoggingStore
) -> None:
    key = seed_logo(session, photos, name="Acme")
    photos.ops.clear()

    client.get(LOGO, headers=customer_headers(customer))

    assert photos.ops == [("get", key)]
    assert stored(session) == ("Acme", key)


# --------------------------------------------------------------------------- the form configuration


@pytest.fixture
def category(session: Session) -> Category:
    created = Category(
        name_he="Plumbing-he", name_en="Plumbing", name_he_key="plumbing-he", name_en_key="plumbing", seq=1
    )
    session.add(created)
    session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=True))
    session.add_all(
        FieldLabel(field_key=key, label_he=f"{key}-he", label_en=f"{key}-en") for key in FIELD_LABEL_KEYS
    )
    session.commit()
    return created


def test_the_customer_and_staff_form_configs_reflect_the_stored_name_and_logo(
    client: TestClient, customer: Customer, category: Category, photos: LoggingStore, admin, staff
) -> None:
    technician = staff(UserRole.TECHNICIAN)

    def configs() -> list[dict]:
        return [
            client.get(f"{API}/form-config", headers=customer_headers(customer)).json()["branding"],
            client.get(f"{API}/staff/form-config", headers=technician).json()["branding"],
        ]

    assert configs() == [EMPTY, EMPTY]

    put(client, admin, *text(company_name="Acme"))
    assert configs() == [{"company_name": "Acme", "has_logo": False}] * 2

    put(client, admin, logo_part(big_png()))
    assert configs() == [{"company_name": "Acme", "has_logo": True}] * 2

    put(client, admin, *text(remove_name="true"))
    assert configs() == [{"company_name": None, "has_logo": True}] * 2

    put(client, admin, *text(remove_logo="true"))
    assert configs() == [EMPTY, EMPTY]


def test_the_logo_key_never_leaves_the_service(
    client: TestClient, customer: Customer, category: Category, photos: LoggingStore, admin, staff
) -> None:
    put(client, admin, *text(company_name="Acme"), logo_part(big_png()))
    (key,) = photos.objects
    responses = [
        client.get(f"{API}/form-config", headers=customer_headers(customer)),
        client.get(f"{API}/staff/form-config", headers=staff(UserRole.TECHNICIAN)),
        client.get(BRANDING, headers=admin),
        put(client, admin, *text(company_name="Acme 2")),
        client.get(LOGO, headers=customer_headers(customer)),
    ]

    for response in responses:
        assert key not in response.text and "service-tickets" not in response.text
        assert key.rsplit("/", 1)[-1][:-4] not in str(response.headers)


def test_unique_keys_per_upload(client: TestClient, admin, photos: LoggingStore) -> None:
    seen: set[str] = set()
    for _ in range(3):
        put(client, admin, logo_part(big_png()))
        seen |= set(photos.keys("put"))
    assert len(seen) == 3
    assert len(photos.objects) == 1  # every replacement removed its predecessor
