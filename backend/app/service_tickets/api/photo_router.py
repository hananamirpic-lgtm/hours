"""The shared photo delivery route (design.md, "Photo pipeline", "Delivery"; R11.9, R11.10, R11.12, R12.2).

`GET /tickets/{ticket_number}/photo` is the one route that serves both kinds of caller: the owning
customer and any of the three ticket staff roles. It sits behind `photo_principal`, which applies the
checks of the caller's own kind (a customer who owes a password change is 403, a staff role the ticket
system does not serve is 403).

Who sees what
-------------
* A customer sees the photo of their own ticket and nothing else. A ticket that belongs to someone
  else, a ticket number that does not exist, a malformed number and a ticket that has no photo are the
  same answer: 404 `ticket_not_found`, byte for byte (R11.10, R12.3). The ownership test is part of the
  lookup (`customer_id = :caller`), so a foreign ticket is never read.
* Staff see the photo of any ticket (R11.9).
* The setting `photo_upload_enabled` is not read. Switching uploads off stops new photos only; stored
  photos stay viewable (R11.12).

The response
------------
`Content-Type: image/jpeg` (the store holds only the JPEG the pipeline wrote) and
`Cache-Control: private, no-store`. `X-Content-Type-Options: nosniff` is **not** set here:
`SecurityHeadersMiddleware` adds it to every response, and it appends, so setting it again would send
the header twice (finding F18). A test asserts that it appears exactly once.

Failures of the store
---------------------
* The row holds a key but the object is gone: 404 `ticket_not_found` and an error log line with the
  ticket number. The key and the owner are not logged.
* The store cannot be reached: 503 `photo_storage_unavailable`.

The handler is synchronous, so FastAPI runs it (and the blocking store call) in a worker thread. It
writes nothing and commits nothing.

The company logo
----------------
`GET /branding/logo` (task 30.5, R28.5) is the second route behind `photo_principal`: the same two kinds of
caller, the same checks, the same cache policy, with `Content-Type: image/png`. There is no owner, so any
signed-in customer and any of the three staff roles may read it. With no logo stored it answers a plain
404 with an empty body and no code.
"""

from __future__ import annotations

import logging
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.service_tickets.api.deps import CustomerPrincipal, PhotoPrincipalDep, ServiceSession
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import ServiceRoute
from app.service_tickets.error_codes import CODE_PHOTO_STORAGE_UNAVAILABLE, CODE_TICKET_NOT_FOUND
from app.service_tickets.grammars import parse_ticket_number_path
from app.service_tickets.logo import CONTENT_TYPE as LOGO_CONTENT_TYPE
from app.service_tickets.models import Ticket
from app.service_tickets.photo import CONTENT_TYPE
from app.service_tickets.photo_store import (
    PhotoNotFound,
    PhotoStorageUnavailable,
    PhotoStore,
    get_photo_store,
)
from app.service_tickets.services import config_admin

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ServiceRoute, tags=["service-photo"])

PhotoStoreDep = Annotated[PhotoStore, Depends(get_photo_store)]

#: The one cache policy of a photo: the image is private to the caller and never kept (R11.9).
PHOTO_CACHE_CONTROL = "private, no-store"


def _not_found() -> HTTPException:
    return service_error(HTTPStatus.NOT_FOUND, CODE_TICKET_NOT_FOUND)


def _photo_key(session: Session, number: int, owner_id: uuid.UUID | None) -> str | None:
    """The object key of the ticket's photo, or `None` for a missing ticket, a foreign one or no photo.

    `owner_id` restricts the lookup to the caller's own tickets; staff pass `None`.
    """
    statement = select(Ticket.photo_key).where(Ticket.ticket_number == number)
    if owner_id is not None:
        statement = statement.where(Ticket.customer_id == owner_id)
    return session.execute(statement).scalar_one_or_none()


@router.get(
    "/tickets/{ticket_number}/photo",
    response_class=Response,
    responses={200: {"content": {CONTENT_TYPE: {}}, "description": "The ticket photo, a JPEG."}},
)
def get_ticket_photo(
    ticket_number: str,
    principal: PhotoPrincipalDep,
    session: ServiceSession,
    store: PhotoStoreDep,
) -> Response:
    """The photo of one ticket, to its owner or to ticket staff. Every miss is the same 404."""
    number = parse_ticket_number_path(ticket_number)
    if number is None:
        raise _not_found()
    owner_id = principal.customer_id if isinstance(principal, CustomerPrincipal) else None
    key = _photo_key(session, number, owner_id)
    if key is None:
        raise _not_found()
    try:
        data = store.get(key)
    except PhotoNotFound as error:
        logger.error("ticket photo object is missing for ticket %s", number)
        raise _not_found() from error
    except PhotoStorageUnavailable as error:
        raise service_error(HTTPStatus.SERVICE_UNAVAILABLE, CODE_PHOTO_STORAGE_UNAVAILABLE) from error
    return Response(content=data, media_type=CONTENT_TYPE, headers={"Cache-Control": PHOTO_CACHE_CONTROL})


@router.get(
    "/branding/logo",
    response_class=Response,
    responses={
        200: {"content": {LOGO_CONTENT_TYPE: {}}, "description": "The company logo, a PNG."},
        404: {"description": "No logo is stored. Empty body, no error code."},
    },
)
def get_branding_logo(
    principal: PhotoPrincipalDep,
    session: ServiceSession,
    store: PhotoStoreDep,
) -> Response:
    """The company logo, to any signed-in customer and to ticket staff (R28.5).

    Same guards as the ticket photo (`photo_principal`: the checks of the caller's own kind, a customer who
    owes a password change is 403). The logo belongs to the company, not to a customer, so there is no
    ownership test. `Content-Type: image/png` and `Cache-Control: private, no-store`; `nosniff` comes from
    `SecurityHeadersMiddleware`, as for the photo.

    When no logo is stored the answer is a plain 404 with an empty body and no error code: the portal asks
    only when the form configuration says `has_logo`, so this is a race (the logo was just removed) and not
    a state to explain. A row that holds a key whose object is gone, or a key of an unknown shape, is the
    same 404 and an error log line without the key.
    """
    key = config_admin.get_logo_key(session)
    if key is None:
        return Response(status_code=HTTPStatus.NOT_FOUND)
    try:
        data = store.get(key)
    except PhotoNotFound:
        logger.error("the stored company logo object is missing")
        return Response(status_code=HTTPStatus.NOT_FOUND)
    except ValueError:
        logger.error("the stored company logo key is not a valid object key")
        return Response(status_code=HTTPStatus.NOT_FOUND)
    except PhotoStorageUnavailable as error:
        raise service_error(HTTPStatus.SERVICE_UNAVAILABLE, CODE_PHOTO_STORAGE_UNAVAILABLE) from error
    headers = {"Cache-Control": PHOTO_CACHE_CONTROL}
    return Response(content=data, media_type=LOGO_CONTENT_TYPE, headers=headers)
