"""Dedicated scheduled-job runner (Requirement 14, deployment task).

Milestone 1 fires the daily and weekly notification jobs from an in-process APScheduler inside the
API (`app.core.scheduler`), guarded by `SCHEDULER_ENABLED` so a multi-instance deployment does not
have every replica firing the same jobs. The cloud target does the opposite: the API replicas run
with `SCHEDULER_ENABLED=false`, and a *dedicated runner* — one scheduled task that the platform's
scheduler starts, runs to completion and stops — owns the jobs. This module is that runner's entry
point.

It is deliberately a run-once-and-exit command, not a long-lived daemon:

* A container scheduler (an ECS/Kubernetes scheduled task, a cron entry, an EventBridge rule) is the
  right place to decide *when* a job runs in a multi-instance world — one schedule, one firing, no
  matter how many API replicas are up. Baking a second scheduler into a long-running process here
  would just recreate the multiple-firing problem the deployment moved away from.
* Run-once makes the exit code the batch's result: the process exits `0` when the batch committed and
  non-zero when it raised, so the platform scheduler records the run as succeeded or failed and can
  alert on a failure. A daemon that swallows the outcome cannot.

Two batches, matching the two schedules the API scheduler registers:

* `daily`  — the no-checkout reminder, the over-maximum open-shift alert and the document-expiry
  sweep, then the email drain (`app.core.scheduler.run_all_daily`).
* `weekly` — the missing-report summary for the seven days ending today, then the email drain
  (`app.core.scheduler.run_weekly`).

Both delegate straight to `app.core.scheduler`, so the dedicated runner and the in-process scheduler
run byte-for-byte the same code; moving the schedule from one to the other never changes what a job
does. Usage:

    python -m app.jobs_runner daily
    python -m app.jobs_runner weekly

The scheduled task definitions that call these live in `infra/` (see `infra/cloud/` and the
deployment runbook).
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.core.config import get_settings
from app.core.scheduler import run_all_daily, run_weekly

logger = logging.getLogger("app.jobs_runner")

#: The batches the runner can fire, mapped to the scheduler entry point each delegates to. Kept a
#: table so adding a batch is one line and the argument parser and the dispatch never drift.
_BATCHES = {
    "daily": run_all_daily,
    "weekly": run_weekly,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs_runner",
        description="Run one scheduled notification-job batch to completion and exit.",
    )
    parser.add_argument(
        "batch",
        choices=sorted(_BATCHES),
        help="Which batch to run: 'daily' (no-checkout, over-maximum, document expiry) or "
        "'weekly' (missing-report summary).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the named batch once. Returns 0 on success, 1 if the batch raised.

    The batch functions open their own session and commit once (or roll back and re-raise on
    failure), so this wrapper only has to turn an exception into a non-zero exit code the platform
    scheduler can see. `argv` is injectable for tests.
    """
    logging.basicConfig(
        level=get_settings().log_level, format="%(levelname)s %(name)s: %(message)s"
    )
    args = _build_parser().parse_args(argv)

    logger.info("scheduled runner starting: batch=%s", args.batch)
    try:
        _BATCHES[args.batch]()
    except Exception:
        logger.exception("scheduled runner failed: batch=%s", args.batch)
        return 1
    logger.info("scheduled runner complete: batch=%s", args.batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
