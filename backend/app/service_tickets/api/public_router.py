"""The two anonymous branding reads of the service ticket API (task 30.16; R28.8, R22.11; design.md,
"Public branding").

`GET /public/branding` and `GET /public/branding/logo` need no token. They exist so that the sign-in screen
and the forced change-password screen can show the company name and logo before anyone is signed in. They
are the first anonymous routes of the package other than customer sign-in, so they are kept as small as
they can be.

What they expose
----------------
* `/public/branding`: `{company_name, has_logo}` and nothing else (the `BrandingInfo` model, the same two
  values the form configuration already carries to a signed-in customer).
* `/public/branding/logo`: the logo PNG that `PUT /staff/branding` stored, byte for byte, or an empty 404.

No customer, ticket, category, settlement, label or setting is read or returned, and no key or object path
is ever put in a response or in a log line.

Route isolation (R1.1, R20)
---------------------------
* No `Caller`, no `require_roles` and no principal dependency of `deps.py`: the route classification test
  lists both routes as `unauthenticated`, the same class as customer sign-in, and the access matrix test
  allows anonymous access to these two paths only.
* They write nothing: no insert, update, delete or commit, and no Redis write other than the limiter's own.
* They read one row, `svc_branding` (by its primary key, through `config_admin`), and one object, the one
  that row names. The object is read only when its key has the exact shape of a logo key
  (`photo_store.is_logo_key`); a row that names a ticket photo, or any other key, answers the empty 404
  and the store is not called. The SQL-isolation audit sees no `users` read and no Hours table.
* The feature flag decides whether they exist: with the flag off the router is not mounted, so both paths
  fall through to the framework's unknown-path 404 like every other service route (R21.3).

Limit
-----
Both routes carry `deps.public_address_limit`: one per-address window (`svc:rl:public:<address key>`, the
deployment's request limit, 120 per 60 seconds by default), kept apart from the sign-in window. It runs
before the handler, the address is taken as for sign-in, an address that cannot be determined skips the limit
and a Redis failure allows the request (R22.5, R22.11). A request over the limit is 429 `rate_limited` with
`Retry-After`, and is not counted.

Headers
-------
* JSON: `Cache-Control: no-store`.
* Logo: `Content-Type: image/png`, `Cache-Control: public, max-age=300` (the logo is public and rarely
  changes, so a browser may reuse it for five minutes; a replaced logo shows up within that time). Errors
  (404, 429, 503) are `no-store`, set by `ServiceRoute`.
* `X-Content-Type-Options: nosniff` and the rest come from `SecurityHeadersMiddleware`, which appends them
  to every response, so they are not set here (finding F18).

Failures
--------
No row, no logo key, a key of another shape, a missing object: 404 with an empty body and no code, as for
the token route. The store unreachable: 503 `photo_storage_unavailable`. Logging names the failure class
only, never the key.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.service_tickets.api.deps import ServiceSession, public_address_limit
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import ServiceRoute
from app.service_tickets.error_codes import CODE_PHOTO_STORAGE_UNAVAILABLE
from app.service_tickets.logo import CONTENT_TYPE as LOGO_CONTENT_TYPE
from app.service_tickets.photo_store import (
    PhotoNotFound,
    PhotoStorageUnavailable,
    PhotoStore,
    get_photo_store,
    is_logo_key,
)
from app.service_tickets.schemas import BrandingInfo
from app.service_tickets.services import config_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/public",
    route_class=ServiceRoute,
    tags=["service-public"],
    dependencies=[Depends(public_address_limit)],
)

PhotoStoreDep = Annotated[PhotoStore, Depends(get_photo_store)]

#: The logo is public and changes rarely: a browser may keep it for five minutes (R28.8).
LOGO_CACHE_CONTROL = "public, max-age=300"

#: The JSON answer is read fresh every time, so a change of name shows at once.
JSON_CACHE_CONTROL = "no-store"


@router.get("/branding", response_model=BrandingInfo)
def get_public_branding(response: Response, session: ServiceSession) -> BrandingInfo:
    """The company name and whether a logo exists. Nothing else is read or returned (R28.8)."""
    branding = config_admin.get_form_branding(session)
    response.headers["Cache-Control"] = JSON_CACHE_CONTROL
    return BrandingInfo(company_name=branding.company_name, has_logo=branding.has_logo)


@router.get(
    "/branding/logo",
    response_class=Response,
    responses={
        200: {"content": {LOGO_CONTENT_TYPE: {}}, "description": "The company logo, a PNG."},
        404: {"description": "No logo is stored. Empty body, no error code."},
    },
)
def get_public_branding_logo(session: ServiceSession, store: PhotoStoreDep) -> Response:
    """The company logo to anyone, or an empty 404 when there is none (R28.8)."""
    key = config_admin.get_logo_key(session)
    if key is None:
        return Response(status_code=HTTPStatus.NOT_FOUND)
    if not is_logo_key(key):
        # A row that names anything but a logo object (a ticket photo, a path outside the logo prefix) is
        # never read for an anonymous caller. The key is not logged.
        logger.error("the stored company logo key is not a logo key")
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
    return Response(content=data, media_type=LOGO_CONTENT_TYPE, headers={"Cache-Control": LOGO_CACHE_CONTROL})
