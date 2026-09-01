"""Global-search request and response schemas (Requirement 22.1, 22.5).

Locale-neutral like every other schema: no message text, only the values the front end formats and
translates. The response groups the hits by kind — employees, sites and clients — because the search
box shows them under headings, and each group carries its own `total`, `limit` and `offset` so the
front end can page each independently (Requirement 22.5).

Employee hits carry both name forms so the row labels in either language (Requirement 21.5); no wage
or sensitive personal field appears, so a hit is safe for any personnel reader and needs no
redaction. Site hits carry the number the search matched on; client hits carry the company for
disambiguation. None of the three carries a money field, so the whole payload is servable to a site
manager unchanged.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from app.models.employee import EmployeeStatus
from app.models.site import SiteStatus


class EmployeeSearchHit(BaseModel):
    """One employee matched by the search."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    full_name_en: str
    status: EmployeeStatus


class SiteSearchHit(BaseModel):
    """One site matched by the search. `site_number` is carried because the term may have matched it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    site_number: str
    client_id: uuid.UUID
    status: SiteStatus


class ClientSearchHit(BaseModel):
    """One client matched by the search."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    company: str | None


class SearchGroup[Hit: BaseModel](BaseModel):
    """A page of hits of one kind, with the total that matched before paging (Requirement 22.5)."""

    items: list[Hit]
    total: int
    limit: int
    offset: int


class SearchResponse(BaseModel):
    """The three groups a global search returns, plus the term it ran for (Requirement 22.1).

    `query` echoes the term so the response is self-describing — a client rendering a stale result can
    tell which term produced it.
    """

    query: str
    employees: SearchGroup[EmployeeSearchHit]
    sites: SearchGroup[SiteSearchHit]
    clients: SearchGroup[ClientSearchHit]
