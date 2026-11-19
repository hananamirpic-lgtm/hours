"""OpenAPI isolation check for the Hours API (Requirements 20.6 and 21.8).

`data/openapi_hours_baseline.json` is a snapshot of the paths and component schemas that the Hours
API published before the service-ticket feature existed, captured with the feature flag off. This
test rebuilds the document and compares it with that snapshot. Regenerate the snapshot only on
purpose, with `tests/data/regenerate_openapi_baseline.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).parent / "data" / "openapi_hours_baseline.json"

# Ticket-system paths are not part of the Hours contract (Requirements 20.5 and 21.3).
EXCLUDED_PATH_PREFIXES = ("/api/service", "/api/features")

TECHNICIAN = "technician"


def _is_excluded(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in EXCLUDED_PATH_PREFIXES)


def build_openapi_snapshot(document: dict[str, Any]) -> dict[str, Any]:
    """Reduce an OpenAPI document to the Hours paths and the component schemas."""
    return {
        "paths": {path: item for path, item in document["paths"].items() if not _is_excluded(path)},
        "components": {"schemas": dict(document.get("components", {}).get("schemas", {}))},
    }


def _is_technician_added_to_role_enum(baseline: Any, current: Any) -> bool:
    """True only when `current` is the baseline role enum plus the single new value `technician`.

    This is the one difference Requirement 20.6 allows: endpoints that accept or return a user role
    also accept and return `technician`. A baseline list counts as a role enum when it holds the
    existing role value `admin`. Anything else, including a removed or reordered value, is not
    tolerated.
    """
    if not isinstance(baseline, list) or not isinstance(current, list):
        return False
    if "admin" not in baseline or TECHNICIAN in baseline:
        return False
    return current.count(TECHNICIAN) == 1 and [v for v in current if v != TECHNICIAN] == baseline


def find_differences(baseline: Any, current: Any, where: str = "$") -> list[str]:
    """Return a description of every difference; an empty list means the two are equal.

    Comparison is exact, except for the `technician` role enum addition described above.
    """
    if isinstance(baseline, dict) and isinstance(current, dict):
        found: list[str] = []
        for key in sorted(baseline.keys() | current.keys()):
            here = f"{where}.{key}"
            if key not in current:
                found.append(f"{here}: removed")
            elif key not in baseline:
                found.append(f"{here}: added")
            elif key == "enum" and _is_technician_added_to_role_enum(baseline[key], current[key]):
                continue
            else:
                found.extend(find_differences(baseline[key], current[key], here))
        return found
    if isinstance(baseline, list) and isinstance(current, list) and len(baseline) == len(current):
        found = []
        for index, (old, new) in enumerate(zip(baseline, current, strict=True)):
            found.extend(find_differences(old, new, f"{where}[{index}]"))
        return found
    return [] if baseline == current else [f"{where}: {baseline!r} -> {current!r}"]


def test_openapi_existing_paths_unchanged(settings):
    from app.main import create_app

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    current = build_openapi_snapshot(create_app(settings).openapi())

    # Every Hours path, outside the ticket prefixes, must be the same set.
    path_differences = find_differences(baseline["paths"], current["paths"], "paths")
    assert path_differences == [], "Hours paths changed:\n" + "\n".join(path_differences[:20])

    # Every baseline schema must still exist and be unchanged. Schemas added for the ticket routers
    # are not part of the Hours contract and are not compared.
    baseline_schemas = baseline["components"]["schemas"]
    current_schemas = current["components"]["schemas"]
    schema_differences = [
        difference
        for name, schema in baseline_schemas.items()
        for difference in (
            find_differences(schema, current_schemas[name], f"schemas.{name}")
            if name in current_schemas
            else [f"schemas.{name}: removed"]
        )
    ]
    assert schema_differences == [], "Hours schemas changed:\n" + "\n".join(schema_differences[:20])


def test_role_enum_tolerance_allows_only_the_technician_addition():
    roles = ["admin", "operations_admin", "site_manager", "accounting", "employee"]
    assert find_differences({"enum": roles}, {"enum": [*roles, TECHNICIAN]}) == []
    assert find_differences({"enum": roles}, {"enum": roles}) == []
    assert find_differences({"enum": roles}, {"enum": [*roles, "other"]}) != []
    assert find_differences({"enum": roles}, {"enum": roles[:-1]}) != []
    assert find_differences({"enum": ["a", "b"]}, {"enum": ["a", "b", TECHNICIAN]}) != []
    assert find_differences({"type": "string"}, {"type": "integer"}) != []