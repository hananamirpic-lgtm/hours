"""Route classification of the Ticket API (Requirements 1.1, 1.14 and 21.9; design.md, "API surface").

Every route under `{api_prefix}/service` of an application built with the feature flag on is classified
by the principal dependency it declares, found by walking the route's dependant tree, and the result is
compared with `ENDPOINT_TABLE` below, which is the endpoint table of the design written out by hand:

* **customer**: `customer_principal` (the gated customer dependency).
* **customer-ungated**: `customer_principal_ungated` (sign-out, `me`, change-password).
* **ticket-staff**: `staff_principal` (administrator, operations administrator or technician).
* **config-staff**: `config_staff_principal` (which itself depends on `staff_principal`).
* **shared-photo**: `photo_principal` (the one route shared by the owning customer and ticket staff).
* **unauthenticated**: no principal dependency at all (customer sign-in).

Two failures are deliberate:

* A route that exists but is not in the table (a new route forces a decision), or whose declared
  dependencies give another class than the table says (a route that lost its guard).
* A table entry for which no route exists, unless the entry is marked pending for a later task.

Extending it for the photo routes (task 16.4): the shared photo route is already in the table as
`pending="16.4"`. When the route is added the test says so and asks for the marker to be removed. The
multipart submission is `POST /tickets`, already in the table. Any other route a later task adds goes in
the table with its class, and with `pending=<task number>` only while the route does not exist yet.

The classifier and the comparison are checked on synthetic applications too, so that a broken
classifier cannot pass the real check vacuously.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.main import create_app
from app.service_tickets.api import deps
from app.service_tickets.api.route import ServiceRoute

API_PREFIX = "/api"
SERVICE_PREFIX = f"{API_PREFIX}/service"
CONTACT = "access@example.test"

# --------------------------------------------------------------------------- the classes

CUSTOMER = "customer"
CUSTOMER_UNGATED = "customer-ungated"
TICKET_STAFF = "ticket-staff"
CONFIG_STAFF = "config-staff"
SHARED_PHOTO = "shared-photo"
UNAUTHENTICATED = "unauthenticated"

ALL_CLASSES = (CUSTOMER, CUSTOMER_UNGATED, TICKET_STAFF, CONFIG_STAFF, SHARED_PHOTO, UNAUTHENTICATED)

#: The principal dependencies, by the name of the class they give a route.
PRINCIPALS: dict[Callable, str] = {
    deps.customer_principal: CUSTOMER,
    deps.customer_principal_ungated: CUSTOMER_UNGATED,
    deps.staff_principal: TICKET_STAFF,
    deps.config_staff_principal: CONFIG_STAFF,
    deps.photo_principal: SHARED_PHOTO,
}

#: The sets of principal dependencies found in a dependant tree that make a valid class. Configuration
#: staff depends on ticket staff, so its tree holds both; nothing else may be combined.
_VALID_COMBINATIONS: dict[frozenset[str], str] = {
    frozenset(): UNAUTHENTICATED,
    frozenset({CUSTOMER}): CUSTOMER,
    frozenset({CUSTOMER_UNGATED}): CUSTOMER_UNGATED,
    frozenset({TICKET_STAFF}): TICKET_STAFF,
    frozenset({TICKET_STAFF, CONFIG_STAFF}): CONFIG_STAFF,
    frozenset({SHARED_PHOTO}): SHARED_PHOTO,
}


# --------------------------------------------------------------------------- the endpoint table


class Endpoint(NamedTuple):
    method: str
    path: str  # relative to `{api_prefix}/service`; path parameter names do not matter
    klass: str
    pending: str | None = None  # the number of the task that adds the route, while it does not exist


def E(method: str, path: str, klass: str, pending: str | None = None) -> Endpoint:
    return Endpoint(method, path, klass, pending)


#: design.md, "Customer and shared endpoints" and "Staff endpoints (`/staff`)". Written out by hand: it
#: is not generated from the application, so a route that appears or changes class is a decision.
ENDPOINT_TABLE: tuple[Endpoint, ...] = (
    # --- customer and shared
    E("POST", "/auth/sign-in", UNAUTHENTICATED),
    E("POST", "/auth/sign-out", CUSTOMER_UNGATED),
    E("GET", "/auth/me", CUSTOMER_UNGATED),
    E("POST", "/auth/change-password", CUSTOMER_UNGATED),
    E("GET", "/form-config", CUSTOMER),
    E("POST", "/tickets", CUSTOMER),
    E("GET", "/tickets", CUSTOMER),
    E("GET", "/tickets/{ticket_number}", CUSTOMER),
    E("POST", "/tickets/{ticket_number}/cancel", CUSTOMER),
    E("GET", "/tickets/{ticket_number}/photo", SHARED_PHOTO, pending="16.4"),
    # --- staff: dashboard, tickets, history
    E("GET", "/staff/dashboard", TICKET_STAFF),
    E("GET", "/staff/tickets", TICKET_STAFF),
    E("GET", "/staff/tickets/{ticket_number}", TICKET_STAFF),
    E("POST", "/staff/tickets/{ticket_number}/status", TICKET_STAFF),
    E("PUT", "/staff/tickets/{ticket_number}/assigned-technician", TICKET_STAFF),
    E("POST", "/staff/tickets/{ticket_number}/history", TICKET_STAFF),
    E("PUT", "/staff/tickets/{ticket_number}/history/{entry_id}", TICKET_STAFF),
    # --- staff: customers and reports (all ticket staff, R1.9)
    E("GET", "/staff/customers", TICKET_STAFF),
    E("POST", "/staff/customers", TICKET_STAFF),
    E("PUT", "/staff/customers/{customer_id}", TICKET_STAFF),
    E("POST", "/staff/customers/{customer_id}/deactivate", TICKET_STAFF),
    E("POST", "/staff/customers/{customer_id}/reactivate", TICKET_STAFF),
    E("POST", "/staff/customers/{customer_id}/reset-password", TICKET_STAFF),
    E("GET", "/staff/reports", TICKET_STAFF),
    # --- staff: configuration. The category list is open to all ticket staff (R19.9); everything else
    # here, reads included, is configuration staff only (R1.10, R19.16).
    E("GET", "/staff/categories", TICKET_STAFF),
    E("POST", "/staff/categories", CONFIG_STAFF),
    E("PUT", "/staff/categories/{category_id}", CONFIG_STAFF),
    E("POST", "/staff/categories/{category_id}/deactivate", CONFIG_STAFF),
    E("POST", "/staff/categories/{category_id}/reactivate", CONFIG_STAFF),
    E("DELETE", "/staff/categories/{category_id}", CONFIG_STAFF),
    E("GET", "/staff/settings", CONFIG_STAFF),
    E("PUT", "/staff/settings", CONFIG_STAFF),
    E("PUT", "/staff/labels", CONFIG_STAFF),
)

_PATH_PARAMETER = re.compile(r"\{[^}]*\}")


def _normalized(path: str) -> str:
    """The path with every parameter name erased, so renaming `{n}` to `{ticket_number}` is no change."""
    return _PATH_PARAMETER.sub("{}", path)


Key = tuple[str, str]


def _table_by_key(table: tuple[Endpoint, ...]) -> dict[Key, Endpoint]:
    by_key: dict[Key, Endpoint] = {}
    for entry in table:
        key = (entry.method, _normalized(entry.path))
        assert key not in by_key, f"duplicate endpoint table entry: {key}"
        by_key[key] = entry
    return by_key


# --------------------------------------------------------------------------- classifying a route


def principal_classes(dependant: Dependant) -> frozenset[str]:
    """The principal dependencies found anywhere in the dependant tree, as class names."""
    found: set[str] = set()

    def walk(node: Dependant) -> None:
        for sub in node.dependencies:
            if sub.call in PRINCIPALS:
                found.add(PRINCIPALS[sub.call])
            walk(sub)

    walk(dependant)
    return frozenset(found)


def classify(dependant: Dependant) -> str | None:
    """The class of a route, or `None` when its principal dependencies are not a valid combination."""
    return _VALID_COMBINATIONS.get(principal_classes(dependant))


def classify_app(app: FastAPI, prefix: str = SERVICE_PREFIX) -> tuple[dict[Key, str | None], list[str]]:
    """(method, relative path) -> class for every route under `prefix`, and the problems met on the way.

    A `None` class means the dependencies declared are not a valid combination. The problems are routes
    that cannot be classified at all (not an `APIRoute`).
    """
    found: dict[Key, str | None] = {}
    problems: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or not (path == prefix or path.startswith(prefix + "/")):
            continue
        if not isinstance(route, APIRoute):
            problems.append(f"{path} is a {type(route).__name__}, not an API route; classify it by hand")
            continue
        relative = _normalized(path[len(prefix) :])
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            key = (method, relative)
            if key in found:
                problems.append(f"{method} {path} is registered twice")
            found[key] = classify(route.dependant)
    return found, problems


def compare(found: dict[Key, str | None], table: tuple[Endpoint, ...]) -> list[str]:
    """Every difference between the routes found and the endpoint table, as readable lines."""
    expected = _table_by_key(table)
    differences: list[str] = []
    for key in sorted(found.keys() - expected.keys()):
        method, path = key
        differences.append(
            f"{method} {SERVICE_PREFIX}{path} is a route that is not in ENDPOINT_TABLE "
            f"(it declares: {found[key] or 'an invalid combination of principal dependencies'}). "
            "Decide who may call it and add it to the table."
        )
    for key in sorted(found.keys() & expected.keys()):
        entry = expected[key]
        if found[key] != entry.klass:
            differences.append(
                f"{entry.method} {SERVICE_PREFIX}{entry.path} should be {entry.klass} but declares "
                f"{found[key] or 'an invalid combination of principal dependencies'}"
            )
        elif entry.pending is not None:
            differences.append(
                f"{entry.method} {SERVICE_PREFIX}{entry.path} exists now: remove pending={entry.pending!r} "
                "from its ENDPOINT_TABLE entry"
            )
    for key in sorted(expected.keys() - found.keys()):
        entry = expected[key]
        if entry.pending is None:
            differences.append(
                f"{entry.method} {SERVICE_PREFIX}{entry.path} is in ENDPOINT_TABLE but no such route exists"
            )
    return differences


# --------------------------------------------------------------------------- the real application


def _build_app(settings, *, enabled: bool) -> FastAPI:
    changes = {
        "service_tickets_enabled": "true" if enabled else "",
        "service_accessibility_contact": CONTACT,
        "api_prefix": API_PREFIX,
    }
    return create_app(settings.model_copy(update=changes))


@pytest.fixture
def flag_on_app(settings) -> FastAPI:
    return _build_app(settings, enabled=True)


def _service_routes(app: FastAPI) -> list[APIRoute]:
    def under_prefix(path: str) -> bool:
        return path == SERVICE_PREFIX or path.startswith(SERVICE_PREFIX + "/")

    return [route for route in app.routes if under_prefix(getattr(route, "path", ""))]


def test_every_ticket_route_matches_the_endpoint_table(flag_on_app):
    """R1.1, R1.14, R21.9: no unlisted route, no wrong guard, no table entry without a route."""
    found, problems = classify_app(flag_on_app)

    assert not problems, "\n" + "\n".join(problems)
    differences = compare(found, ENDPOINT_TABLE)
    assert not differences, "\n" + "\n".join(differences)


def test_the_real_enumeration_is_not_vacuous(flag_on_app):
    """The check looked at the routers: many routes, and every class is seen at least once."""
    found, _ = classify_app(flag_on_app)

    assert len(found) >= 30
    # The photo route is pending (task 16.4), so every other class is the one that must be seen.
    assert set(found.values()) >= set(ALL_CLASSES) - {SHARED_PHOTO}
    # Every route under the prefix belongs to the package's own route class.
    routes = _service_routes(flag_on_app)
    assert all(isinstance(route, ServiceRoute) for route in routes)


def test_no_ticket_route_is_left_without_a_decision(flag_on_app):
    """A route with an invalid combination of principals is a failure in its own right."""
    found, _ = classify_app(flag_on_app)

    assert [key for key, klass in found.items() if klass is None] == []


def test_the_table_names_each_route_once_and_only_known_classes():
    keys = _table_by_key(ENDPOINT_TABLE)

    assert len(keys) == len(ENDPOINT_TABLE)
    assert {entry.klass for entry in ENDPOINT_TABLE} <= set(ALL_CLASSES)
    # The pending marker is a task number and only the photo route has one today.
    assert {(e.method, e.path): e.pending for e in ENDPOINT_TABLE if e.pending} == {
        ("GET", "/tickets/{ticket_number}/photo"): "16.4"
    }


# --------------------------------------------------------------------------- mount: order and flag


def test_flag_on_mounts_customer_then_staff_under_the_prefix(flag_on_app):
    """The mount order is fixed: every customer route is registered before every staff route."""
    paths = [route.path for route in _service_routes(flag_on_app)]
    staff_prefix = f"{SERVICE_PREFIX}/staff"
    first_staff = next(i for i, path in enumerate(paths) if path.startswith(staff_prefix + "/"))

    assert paths, "no ticket route was mounted"
    assert all(not path.startswith(staff_prefix + "/") for path in paths[:first_staff])
    assert all(path.startswith(staff_prefix + "/") for path in paths[first_staff:])
    assert first_staff > 0


def test_flag_off_mounts_no_ticket_route(settings):
    app = _build_app(settings, enabled=False)

    assert _service_routes(app) == []
    assert classify_app(app) == ({}, [])


def test_the_mount_order_is_the_same_on_every_build(settings):
    first = [(r.path, sorted(r.methods)) for r in _service_routes(_build_app(settings, enabled=True))]
    second = [(r.path, sorted(r.methods)) for r in _service_routes(_build_app(settings, enabled=True))]

    assert first == second


def test_the_flag_changes_no_hours_openapi_path_or_schema_name(settings):
    """R20.6, R21.8: mounting the ticket routers leaves every Hours path and schema untouched.

    A ticket response model that shares a class name with an Hours model makes FastAPI rename both
    schemas (`app__schemas__auth__TokenResponse`) and rewrites the `$ref` of the Hours paths, so the
    comparison covers the path items as well as the schema names. It holds in either environment flag
    state because both applications are built here.
    """
    off = _build_app(settings, enabled=False).openapi()
    on = _build_app(settings, enabled=True).openapi()

    def hours_paths(document: dict) -> dict:
        return {
            path: item
            for path, item in document["paths"].items()
            if not (path == SERVICE_PREFIX or path.startswith(SERVICE_PREFIX + "/"))
        }

    assert hours_paths(on) == hours_paths(off)
    for name, schema in off["components"]["schemas"].items():
        assert on["components"]["schemas"].get(name) == schema, name
    assert any(path.startswith(SERVICE_PREFIX + "/") for path in on["paths"])


# --------------------------------------------------------------------------- the classifier, on probes


def _probe_app(*routes: tuple[str, str, list]) -> FastAPI:
    """An application with one probe route per (method, path, dependencies) under the service prefix."""
    app = FastAPI()
    router = APIRouter()
    for method, path, dependencies in routes:

        def handler() -> dict:
            return {}

        router.add_api_route(path, handler, methods=[method], dependencies=dependencies)
    app.include_router(router, prefix=SERVICE_PREFIX)
    return app


def _declares(*calls: Callable) -> list:
    return [Depends(call) for call in calls]


PROBES = {
    CUSTOMER: (deps.customer_principal,),
    CUSTOMER_UNGATED: (deps.customer_principal_ungated,),
    TICKET_STAFF: (deps.staff_principal,),
    CONFIG_STAFF: (deps.config_staff_principal,),
    SHARED_PHOTO: (deps.photo_principal,),
    UNAUTHENTICATED: (),
}


@pytest.mark.parametrize("klass", ALL_CLASSES)
def test_the_classifier_recognises_each_class(klass):
    app = _probe_app(("GET", "/probe", _declares(*PROBES[klass])))

    assert classify_app(app) == ({("GET", "/probe"): klass}, [])


def test_the_classifier_sees_a_principal_inside_a_dependency_of_a_dependency():
    def wrapper(principal: deps.StaffPrincipalDep) -> None:
        return None

    app = _probe_app(("GET", "/probe", _declares(wrapper)))

    assert classify_app(app)[0] == {("GET", "/probe"): TICKET_STAFF}


def test_the_classifier_sees_a_router_level_dependency():
    app = FastAPI()
    router = APIRouter(dependencies=_declares(deps.staff_principal))

    @router.get("/probe", dependencies=_declares(deps.config_staff_principal))
    def probe() -> dict:
        return {}

    app.include_router(router, prefix=SERVICE_PREFIX)

    assert classify_app(app)[0] == {("GET", "/probe"): CONFIG_STAFF}


@pytest.mark.parametrize(
    "calls",
    [
        (deps.customer_principal, deps.staff_principal),
        (deps.customer_principal, deps.customer_principal_ungated),
        (deps.photo_principal, deps.staff_principal),
        (deps.config_staff_principal, deps.customer_principal),
    ],
    ids=["customer+staff", "gated+ungated", "photo+staff", "config+customer"],
)
def test_the_classifier_refuses_a_mix_of_principals(calls):
    app = _probe_app(("GET", "/probe", _declares(*calls)))

    assert classify_app(app)[0] == {("GET", "/probe"): None}
    assert compare(classify_app(app)[0], ()), "an invalid combination must be reported"


def test_the_classifier_ignores_routes_outside_the_prefix():
    app = FastAPI()

    @app.get("/api/features")
    def features() -> dict:
        return {}

    @app.get("/api/services")
    def services() -> dict:
        return {}

    assert classify_app(app) == ({}, [])


def test_the_classifier_reports_a_route_that_is_not_an_api_route():
    app = FastAPI()
    app.mount(f"{SERVICE_PREFIX}/static", FastAPI())

    found, problems = classify_app(app)

    assert found == {}
    assert len(problems) == 1


# --------------------------------------------------------------------------- the comparison, on probes

TABLE = (
    E("GET", "/a", CUSTOMER),
    E("POST", "/b/{x}", TICKET_STAFF),
    E("GET", "/later", SHARED_PHOTO, pending="99.9"),
)
MATCHING = {("GET", "/a"): CUSTOMER, ("POST", "/b/{}"): TICKET_STAFF}


def test_compare_accepts_the_table_with_its_pending_route_absent():
    assert compare(MATCHING, TABLE) == []


def test_compare_ignores_path_parameter_names():
    assert compare(MATCHING, (E("GET", "/a", CUSTOMER), E("POST", "/b/{anything}", TICKET_STAFF))) == []


def test_compare_fails_a_route_that_is_not_in_the_table():
    differences = compare({**MATCHING, ("DELETE", "/c"): CUSTOMER}, TABLE)

    assert len(differences) == 1
    assert "DELETE /api/service/c" in differences[0]
    assert "not in ENDPOINT_TABLE" in differences[0]


def test_compare_fails_the_same_path_with_another_method():
    differences = compare({**MATCHING, ("PUT", "/a"): CUSTOMER}, TABLE)

    assert len(differences) == 1
    assert "PUT /api/service/a" in differences[0]


def test_compare_fails_a_table_entry_with_no_route():
    differences = compare({("GET", "/a"): CUSTOMER}, TABLE)

    assert len(differences) == 1
    assert "POST /api/service/b/{x}" in differences[0]
    assert "no such route exists" in differences[0]


def test_compare_fails_a_route_with_the_wrong_class():
    differences = compare({**MATCHING, ("GET", "/a"): TICKET_STAFF}, TABLE)

    assert len(differences) == 1
    assert "should be customer but declares ticket-staff" in differences[0]


def test_compare_fails_a_route_without_a_guard_that_the_table_says_has_one():
    differences = compare({**MATCHING, ("POST", "/b/{}"): UNAUTHENTICATED}, TABLE)

    assert len(differences) == 1
    assert "should be ticket-staff but declares unauthenticated" in differences[0]


def test_compare_fails_a_route_with_an_invalid_combination():
    differences = compare({**MATCHING, ("GET", "/a"): None}, TABLE)

    assert len(differences) == 1
    assert "invalid combination" in differences[0]


def test_compare_checks_a_pending_route_that_exists_and_asks_for_the_marker_to_go():
    found = {**MATCHING, ("GET", "/later"): SHARED_PHOTO}

    differences = compare(found, TABLE)

    assert len(differences) == 1
    assert "remove pending='99.9'" in differences[0]
    wrong = compare({**MATCHING, ("GET", "/later"): CUSTOMER}, TABLE)
    assert len(wrong) == 1
    assert "should be shared-photo but declares customer" in wrong[0]


def test_compare_rejects_a_table_with_a_duplicate_entry():
    with pytest.raises(AssertionError, match="duplicate"):
        compare({}, (E("GET", "/a/{x}", CUSTOMER), E("GET", "/a/{y}", CUSTOMER)))

