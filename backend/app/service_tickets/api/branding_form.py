"""The form and the storage order of `PUT /staff/branding` (task 30.5; R28.1, R28.2, R28.6, R28.7).

The route itself is in `staff_router.py`. This module holds what is specific to the branding form, so the
router stays readable: reading the file part with a hard cap, checking every part of the form, decoding
the logo, and writing in the order that never leaves a database row pointing at a missing object.

The form (`multipart/form-data`)
--------------------------------
Every part is optional.

* `company_name`: text, trimmed, 1 to 100 characters, no NUL and no lone surrogate. An empty value is
  invalid (use `remove_name`). `invalid_company_name`.
* `logo`: a file part, JPEG, PNG or WebP of at most 1 MiB and 2,000 by 2,000 pixels (see `logo.py`).
* `remove_logo`, `remove_name`: the text `true` and nothing else.
* Any other part is `unknown_field`. A text part named `logo` is `unknown_field` too, and a repeated part is
  invalid (`invalid_logo_type` for `logo`, `invalid_company_name` for `company_name`, `invalid_request` for
  the flags). `logo` with `remove_logo`, and `company_name` with `remove_name`, contradict each other:
  `unknown_field` under the name of the part that was asked to be both set and removed. A request with no
  part at all (or a body that is not a form) is 422 `invalid_request` under `body`: there is nothing to do,
  which is a client mistake and not a silent success.

Check order
-----------
1. A file part named `logo` over 1 MiB is 413 `logo_too_large`, raised while reading it: at most 1 MiB
   plus one byte of it is ever read, so an oversized file is refused without holding it.
2. Every field problem is collected, in form order, into one 422 with `fields`, and the code of the first
   is the top-level code. The logo is decoded in this step (under the shared decode semaphore), so its
   problems are in the same answer.
3. Only when there is no problem is anything written. A refusal stores nothing.

Write order (R28.6)
-------------------
`save_branding` puts the new object, then updates `svc_branding` and commits, and only after the commit
deletes the old object, best effort. A failure after the put (the update, the commit, anything) deletes
the new object, best effort, and goes on. Removing the logo sets the key to NULL, commits, then deletes the
old object. A leftover object (a failed delete, a crash between put and commit) is found by the orphan
sweep, which never reports the object that `svc_branding.logo_key` points at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus

from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import CODE_INVALID_REQUEST
from app.service_tickets.error_codes import (
    CODE_INVALID_COMPANY_NAME,
    CODE_INVALID_LOGO_TYPE,
    CODE_LOGO_TOO_LARGE,
    CODE_PHOTO_STORAGE_UNAVAILABLE,
    CODE_UNKNOWN_FIELD,
)
from app.service_tickets.logo import MAX_LOGO_BYTES, LogoError
from app.service_tickets.photo_store import (
    PhotoStorageUnavailable,
    PhotoStore,
    new_logo_key,
    process_logo_limited,
)
from app.service_tickets.schemas import BrandingInfo
from app.service_tickets.services import config_admin

logger = logging.getLogger(__name__)

FIELD_COMPANY_NAME = "company_name"
FIELD_LOGO = "logo"
FIELD_REMOVE_LOGO = "remove_logo"
FIELD_REMOVE_NAME = "remove_name"

#: The only value of a remove flag.
TRUE = "true"


@dataclass(frozen=True, slots=True)
class Entries:
    """The parts of one form as the planner needs them, read off the async side.

    `parts` holds (name, text) in form order; `text` is `None` for a file part. `logos` holds the
    content of every file part named `logo`, already read with the 1 MiB cap, in form order.
    """

    parts: tuple[tuple[str, str | None], ...]
    logos: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class BrandingChange:
    """A valid request: what to change, with the processed logo (PNG bytes) when one is set."""

    set_name: bool = False
    company_name: str | None = None
    set_logo: bool = False
    logo: bytes | None = None


async def read_entries(form: FormData) -> Entries:
    """Read the form parts. A `logo` file part over 1 MiB is 413 `logo_too_large`, before anything else."""
    parts: list[tuple[str, str | None]] = []
    logos: list[bytes] = []
    for name, value in form.multi_items():
        if isinstance(value, UploadFile):
            parts.append((name, None))
            if name == FIELD_LOGO:
                content = await value.read(MAX_LOGO_BYTES + 1)
                if len(content) > MAX_LOGO_BYTES:
                    raise service_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, CODE_LOGO_TOO_LARGE)
                logos.append(content)
        else:
            parts.append((name, str(value)))
    return Entries(parts=tuple(parts), logos=tuple(logos))


def plan_branding(entries: Entries) -> BrandingChange:
    """Check every part and decode the logo. Raises the 422 with every problem; stores nothing.

    Synchronous and CPU bound (the decode), so the router runs it in a worker thread.
    """
    if not entries.parts:  # no part, or a body that is not a form: nothing to change is a client mistake
        raise service_error(HTTPStatus.UNPROCESSABLE_ENTITY, fields={"body": CODE_INVALID_REQUEST})
    problems: dict[str, str] = {}
    counts: dict[str, int] = {}
    for name, _ in entries.parts:
        counts[name] = counts.get(name, 0) + 1

    def refuse(name: str, code: str) -> None:
        problems.setdefault(name, code)

    set_name = False
    company_name: str | None = None
    remove_name = False
    remove_logo = False
    has_logo = False
    for name, text in entries.parts:
        if name == FIELD_COMPANY_NAME:
            cleaned = None if counts[name] > 1 or text is None else config_admin.clean_company_name(text)
            if cleaned is None:
                refuse(name, CODE_INVALID_COMPANY_NAME)
            else:
                set_name, company_name = True, cleaned
        elif name == FIELD_LOGO:
            if text is not None:  # a text part is not a file
                refuse(name, CODE_UNKNOWN_FIELD)
            elif counts[name] > 1:
                refuse(name, CODE_INVALID_LOGO_TYPE)
            else:
                has_logo = True
        elif name in (FIELD_REMOVE_LOGO, FIELD_REMOVE_NAME):
            if counts[name] > 1 or text != TRUE:
                refuse(name, CODE_INVALID_REQUEST)
            elif name == FIELD_REMOVE_LOGO:
                remove_logo = True
            else:
                remove_name = True
        else:
            refuse(name, CODE_UNKNOWN_FIELD)

    # A value and its removal contradict each other. The part that was asked to be both is reported.
    if has_logo and remove_logo:
        problems[FIELD_LOGO] = CODE_UNKNOWN_FIELD
    if set_name and remove_name:
        problems[FIELD_COMPANY_NAME] = CODE_UNKNOWN_FIELD

    logo: bytes | None = None
    if has_logo and not remove_logo and FIELD_LOGO not in problems:
        try:
            logo = process_logo_limited(entries.logos[0]).data
        except LogoError as error:
            if error.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
                raise service_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error.code) from error
            refuse(FIELD_LOGO, error.code)

    if problems:
        raise service_error(HTTPStatus.UNPROCESSABLE_ENTITY, fields=problems)
    return BrandingChange(
        set_name=set_name or remove_name,
        company_name=company_name,
        set_logo=logo is not None or remove_logo,
        logo=logo,
    )


def save_branding(session: Session, store: PhotoStore, change: BrandingChange) -> BrandingInfo:
    """Write the change in the order of the module docstring and return the branding as it now reads.

    Runs in a worker thread (blocking session and object store). A store that cannot take the new object
    is 503 `photo_storage_unavailable` with nothing changed.
    """
    new_key: str | None = None
    put_done = False
    try:
        if change.logo is not None:
            new_key = new_logo_key()
            store.put(new_key, change.logo)
            put_done = True
        replaced, branding = config_admin.update_branding(
            session,
            set_name=change.set_name,
            company_name=change.company_name,
            set_logo=change.set_logo,
            logo_key=new_key,
        )
        session.commit()
    except PhotoStorageUnavailable as error:
        session.rollback()
        raise service_error(HTTPStatus.SERVICE_UNAVAILABLE, CODE_PHOTO_STORAGE_UNAVAILABLE) from error
    except Exception:
        # The row was not committed: the new object is garbage. Best effort; the sweep finds it otherwise.
        if put_done and new_key is not None:
            _delete_quietly(store, new_key, "the new company logo object")
        raise
    if replaced is not None:
        _delete_quietly(store, replaced, "the previous company logo object")
    return BrandingInfo(company_name=branding.company_name, has_logo=branding.has_logo)


def _delete_quietly(store: PhotoStore, key: str, what: str) -> None:
    """Delete an object and never fail: the request is already decided. The key is not logged."""
    try:
        deleted = store.delete(key)
    except Exception as error:  # noqa: BLE001 - a bad stored key or any store failure must not fail the request
        logger.error("%s was not deleted: %s", what, type(error).__name__)
        return
    if not deleted:
        logger.error("%s was not deleted: the store refused", what)
