"""No part of the schema can hold location data.

Requirements 6.8, 9.6 and 20.10 put location capture out of scope. A note in a design document does
not enforce that; a query against the live catalogue does. The check runs against whatever the
migrations have actually produced, so a coordinate column added in six months' time fails here
rather than being discovered in a privacy review.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

# Word-boundary matching (`\y`) rather than a substring search, because 'payroll_site_allocations'
# contains the letters of 'location' and is entirely legitimate. Abbreviations are included because
# `lat`/`lng` is how a coordinate pair usually arrives in a schema.
FORBIDDEN_NAME_PATTERN = (
    r"\y(latitude|longitude|lat|lon|lng|radius|location|geolocation|geofence|geo|"
    r"coord|coords|coordinate|coordinates|gps)\y"
)


def test_no_column_holds_location_data(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        offenders = connection.execute(
            sa.text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name ~* :pattern
                ORDER BY table_name, column_name
                """
            ),
            {"pattern": FORBIDDEN_NAME_PATTERN},
        ).all()

    assert offenders == [], f"location-like columns present: {offenders}"


def test_no_table_is_named_for_location_data(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        offenders = connection.execute(
            sa.text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name ~* :pattern
                ORDER BY table_name
                """
            ),
            {"pattern": FORBIDDEN_NAME_PATTERN},
        ).all()

    assert offenders == [], f"location-like tables present: {offenders}"


def test_no_geospatial_type_is_installed(migrated_engine: Engine) -> None:
    """PostGIS would let a coordinate hide inside a single opaque column."""
    with migrated_engine.connect() as connection:
        extensions = set(connection.execute(sa.text("SELECT extname FROM pg_extension")).scalars().all())
        spatial_columns = connection.execute(
            sa.text(
                "SELECT table_name, column_name, udt_name FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND udt_name IN ('geometry', 'geography', 'point', 'box', 'circle', 'path', 'polygon')"
            )
        ).all()

    assert not {"postgis", "postgis_topology"} & extensions
    assert spatial_columns == [], f"geometric columns present: {spatial_columns}"
