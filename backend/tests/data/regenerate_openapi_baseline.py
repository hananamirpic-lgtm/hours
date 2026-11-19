"""Regenerate `openapi_hours_baseline.json`. Run this deliberately, never from a test.

Run inside the api container, with the service-ticket feature flag off:

    python tests/data/regenerate_openapi_baseline.py

It overwrites the baseline that `tests/test_service_openapi_baseline.py` compares against, so
review the resulting diff before committing it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "openapi_hours_baseline.json"


def main() -> None:
    # The baseline describes Hours with the feature off, whatever the shell has exported.
    os.environ.pop("SERVICE_TICKETS_ENABLED", None)
    sys.path.insert(0, str(TESTS_DIR))

    from test_service_openapi_baseline import build_openapi_snapshot

    from app.main import create_app

    snapshot = build_openapi_snapshot(create_app().openapi())
    text = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    # Write bytes so the file has LF line endings and no byte order mark on every platform.
    BASELINE_PATH.write_bytes(text.encode("utf-8"))
    print(
        f"wrote {BASELINE_PATH.name}: {len(snapshot['paths'])} paths, "
        f"{len(snapshot['components']['schemas'])} schemas"
    )


if __name__ == "__main__":
    main()