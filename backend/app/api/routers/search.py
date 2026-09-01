"""Global-search router (Requirement 22.1, 22.2, 22.4, 22.5).

HTTP only: read the term and paging from the query, hand the caller's site scope to the service, and
shape the grouped response. A search is a pure read, so there is nothing to commit.

The endpoint takes any authenticated, enrolled caller rather than naming a role set. What each role
may find is decided by scope, not by a guard: an administrator and accounting search the whole
business, a site manager only the employees, sites and clients that fall within their assigned sites
(Requirement 22.2), and an employee — whose scope is their own record, not a site — finds nothing
here, which is the correct reading of `SiteScope.nothing()`. Every hit is name-and-identity only,
with no wage or billing field, so no redaction is needed; the scope is the whole of the access
control on this endpoint.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentCaller, DbSession
from app.schemas.search import (
    ClientSearchHit,
    EmployeeSearchHit,
    SearchGroup,
    SearchResponse,
    SiteSearchHit,
)
from app.services import search as search_service

router = APIRouter(prefix="/search", tags=["search"])


@router.get(
    "",
    response_model=SearchResponse,
    summary="Search employees, sites and clients",
    description=(
        "A global search across employees, sites and clients (Requirement 22.1). It matches a partial "
        "name in either language (Requirement 21.5, 22.4), a site number partially, a client phone "
        "partially, and a passport number exactly — case- and accent-insensitive throughout. Results "
        "are limited to what the caller may see (Requirement 22.2): administrators and accounting "
        "search the whole business, a site manager only their assigned sites and the people and "
        "clients attached to them. Each group is paged independently with a stable order "
        "(Requirement 22.5). A blank term returns empty groups."
    ),
)
def global_search(
    caller: CurrentCaller,
    session: DbSession,
    q: Annotated[str, Query(max_length=200, description="The search term.")] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchResponse:
    results = search_service.search(session, term=q, scope=caller.scope, limit=limit, offset=offset)

    return SearchResponse(
        query=q,
        employees=SearchGroup[EmployeeSearchHit](
            items=[EmployeeSearchHit.model_validate(hit) for hit in results.employees.items],
            total=results.employees.total,
            limit=limit,
            offset=offset,
        ),
        sites=SearchGroup[SiteSearchHit](
            items=[SiteSearchHit.model_validate(hit) for hit in results.sites.items],
            total=results.sites.total,
            limit=limit,
            offset=offset,
        ),
        clients=SearchGroup[ClientSearchHit](
            items=[ClientSearchHit.model_validate(hit) for hit in results.clients.items],
            total=results.clients.total,
            limit=limit,
            offset=offset,
        ),
    )
