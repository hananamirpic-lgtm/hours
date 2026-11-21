"""Operator maintenance commands of the service ticket system (R11.13, R20.11; design.md, "Photo pipeline").

Run by hand, never by a scheduler (manual on purpose):

    python -m app.service_tickets.maintenance sweep-orphans --older-than 1h            # report only
    python -m app.service_tickets.maintenance sweep-orphans --older-than 1h --delete   # report and delete

``sweep-orphans``
-----------------
A crash between the photo write and the ticket commit leaves one object in the bucket that no ticket
references. This command finds such objects and, with ``--delete``, removes them.

* **Listing.** Only the ``service-tickets/`` prefix is listed (``list_objects_v2`` with ``Prefix`` and
  pagination, by ``PhotoLister``, a small class of its own that takes the client). ``PhotoStore`` has no
  list operation and must not get one; the lister is read-only and cannot write or delete.
* **Age.** An object is a candidate only when its ``LastModified`` is older than ``--older-than``
  (``30m``, ``1h``, ``2d``). The smallest accepted value is 5 minutes and the largest 365 days, so a
  ticket that is being created right now is never swept. An object without a usable ``LastModified`` is
  treated as recent.
* **References.** The keys of one listed page are looked up in ``svc_tickets.photo_key`` with an ``IN``
  query (in chunks of 500), never by loading the ticket table. A referenced key is never reported or
  deleted. The lookup of a page runs immediately before that page is deleted.
* **Prefix safety.** Every listed key goes through ``check_key``; a key outside the prefix, or one that
  is not a plain key under it, is ignored and counted, never reported or deleted. Deletion goes through
  ``PhotoStore.delete``, which checks the key again.
* **Output.** Object keys (``service-tickets/<32 hex>.jpg``), their last-modified time and counters
  only. No ticket data is read except the ``photo_key`` column, and none is printed. Failures print the
  exception class and, for the object store, its error code, never a message that could echo a value.

Exit codes
----------
==== ===========================================================================================
0    The sweep finished. Orphans may have been found (and, with ``--delete``, removed).
2    Bad command line: unknown command, missing or invalid ``--older-than`` (not ``<n>m|h|d``,
     below 5 minutes or above 365 days). Nothing was listed or deleted.
3    The object store could not be configured or listed. Objects reported or deleted before the
     failure stay reported or deleted; the summary says how many.
4    The database could not be read. Nothing is deleted for the page that failed.
5    The sweep finished listing, but one or more ``--delete`` calls failed. Failed keys are printed
     as ``FAILED <key>`` and stay in the bucket; run the command again.
==== ===========================================================================================
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, TextIO

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.service_tickets.models import Ticket
from app.service_tickets.photo_store import KEY_PREFIX, PhotoStore, build_client, check_key

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LISTING_FAILED = 3
EXIT_DATABASE_FAILED = 4
EXIT_DELETE_FAILED = 5

#: An object younger than this is never swept, so a ticket being created now keeps its photo.
MIN_AGE = timedelta(minutes=5)
MAX_AGE = timedelta(days=365)

#: Keys per `IN` query. Well below every driver's bind-parameter limit.
LOOKUP_CHUNK = 500

_DURATION = re.compile(r"[0-9]{1,6}[mhd]")
_UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


# --------------------------------------------------------------------------- durations


def parse_age(text: str) -> timedelta:
    """Parse ``30m``, ``1h`` or ``2d`` (digits and one unit, nothing else).

    Raises ``ValueError`` for anything else and for a value below ``MIN_AGE`` or above ``MAX_AGE``.
    """
    cleaned = text.strip() if isinstance(text, str) else ""
    if not _DURATION.fullmatch(cleaned):
        raise ValueError(f"invalid age {text!r}: use a number and a unit, such as 30m, 1h or 2d")
    age = int(cleaned[:-1]) * _UNITS[cleaned[-1]]
    if age < MIN_AGE:
        raise ValueError(f"age {text!r} is below the safe minimum of 5 minutes")
    if age > MAX_AGE:
        raise ValueError(f"age {text!r} is above the maximum of 365 days")
    return age


def _age_argument(text: str) -> timedelta:
    try:
        return parse_age(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


# --------------------------------------------------------------------------- listing


class ListingFailed(Exception):
    """The object store could not be listed. The message holds no store-supplied text."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    #: Timezone-aware, or `None` when the store gave no usable time (treated as recent).
    last_modified: datetime | None


class ObjectLister(Protocol):
    def pages(self) -> Iterator[list[StoredObject]]: ...


class PhotoLister:
    """Lists the objects under ``service-tickets/``, one page at a time. Read-only.

    Takes the boto3 client (tests pass a fake). It only ever calls ``list_objects_v2``, always with
    ``Prefix=KEY_PREFIX``.
    """

    def __init__(self, client, bucket: str) -> None:  # noqa: ANN001
        self._client = client
        self._bucket = bucket

    def pages(self) -> Iterator[list[StoredObject]]:
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": KEY_PREFIX}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                response = self._client.list_objects_v2(**kwargs)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc.response, dict) else ""
                raise ListingFailed(f"object store listing failed ({type(exc).__name__} {code})") from exc
            except BotoCoreError as exc:
                raise ListingFailed(f"object store listing failed ({type(exc).__name__})") from exc

            yield [
                StoredObject(item["Key"], _aware(item.get("LastModified")))
                for item in response.get("Contents") or []
                if isinstance(item.get("Key"), str)
            ]

            if not response.get("IsTruncated"):
                return
            next_token = response.get("NextContinuationToken")
            if not next_token or next_token == token:
                raise ListingFailed("object store listing did not advance; stopping")
            token = next_token


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- the sweep


@dataclass
class SweepResult:
    scanned: int = 0
    ignored: int = 0  # not a plain key under the prefix
    recent: int = 0  # too young, or no usable time
    referenced: int = 0
    orphans: int = 0
    deleted: int = 0
    failed: int = 0
    exit_code: int = EXIT_OK
    errors: list[str] = field(default_factory=list)


def referenced_keys(session: Session, keys: Sequence[str]) -> set[str]:
    """The subset of `keys` that some ticket references, by ``IN`` queries over chunks of the keys."""
    found: set[str] = set()
    for start in range(0, len(keys), LOOKUP_CHUNK):
        chunk = list(keys[start : start + LOOKUP_CHUNK])
        found.update(session.execute(select(Ticket.photo_key).where(Ticket.photo_key.in_(chunk))).scalars())
    found.discard(None)  # type: ignore[arg-type]
    return found


def sweep_orphans(
    lister: ObjectLister,
    store: PhotoStore,
    session_factory: Callable[[], Session],
    *,
    older_than: timedelta,
    delete: bool,
    now: datetime,
    out: TextIO,
) -> SweepResult:
    """Report, and with `delete` remove, the old unreferenced objects under the prefix."""
    result = SweepResult()
    cutoff = now - older_than
    try:
        for page in lister.pages():
            candidates: dict[str, datetime] = {}
            for item in page:
                result.scanned += 1
                try:
                    check_key(item.key)
                except ValueError:
                    result.ignored += 1
                    continue
                if item.last_modified is None or item.last_modified >= cutoff:
                    result.recent += 1
                    continue
                candidates[item.key] = item.last_modified
            if not candidates:
                continue

            try:
                with session_factory() as session:
                    referenced = referenced_keys(session, list(candidates))
            except SQLAlchemyError as exc:
                result.exit_code = EXIT_DATABASE_FAILED
                result.errors.append(f"database read failed ({type(exc).__name__})")
                return result

            for key, modified in candidates.items():
                if key in referenced:
                    result.referenced += 1
                    continue
                result.orphans += 1
                print(f"ORPHAN {key} {modified.isoformat()}", file=out)
                if not delete:
                    continue
                if _delete(store, key):
                    result.deleted += 1
                    print(f"DELETED {key}", file=out)
                else:
                    result.failed += 1
                    print(f"FAILED {key}", file=out)
    except ListingFailed as exc:
        result.exit_code = EXIT_LISTING_FAILED
        result.errors.append(str(exc))
        return result

    if result.failed:
        result.exit_code = EXIT_DELETE_FAILED
        result.errors.append(f"{result.failed} delete call(s) failed; run the command again")
    return result


def _delete(store: PhotoStore, key: str) -> bool:
    try:
        check_key(key)
        return bool(store.delete(key))
    except ValueError:
        return False


# --------------------------------------------------------------------------- command line


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.service_tickets.maintenance",
        description="Maintenance commands of the service ticket system. Run by hand.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sweep = commands.add_parser(
        "sweep-orphans",
        help=f"list (and with --delete remove) old, unreferenced photo objects under {KEY_PREFIX}",
        description=(
            "Lists objects under the service-tickets/ prefix older than --older-than that no ticket "
            "references. Deletes them only with --delete. Exit codes: 0 done, 2 bad arguments, "
            "3 listing failed, 4 database failed, 5 some deletes failed."
        ),
    )
    sweep.add_argument(
        "--older-than",
        required=True,
        type=_age_argument,
        metavar="AGE",
        help="minimum age, such as 30m, 1h or 2d (at least 5m, at most 365d)",
    )
    sweep.add_argument("--delete", action="store_true", help="delete the orphans (default: report only)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    lister: ObjectLister | None = None,
    store: PhotoStore | None = None,
    session_factory: Callable[[], Session] | None = None,
    now: datetime | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the command line and return the exit code. The keyword arguments let tests inject fakes."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits on a usage error (2) and on --help (0)
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    if lister is None or store is None:
        try:
            client = build_client()
            store = store or PhotoStore(client)
            lister = lister or PhotoLister(client, store.bucket)
        except Exception as exc:  # settings or client construction; never echo the message
            print(f"error: object store could not be configured ({type(exc).__name__})", file=err)
            return EXIT_LISTING_FAILED

    mode = "DELETE" if args.delete else "DRY RUN (nothing is deleted; use --delete)"
    print(f"sweep-orphans: prefix {KEY_PREFIX} older than {args.older_than}, mode {mode}", file=out)

    result = sweep_orphans(
        lister,
        store,
        session_factory or get_session_factory(),
        older_than=args.older_than,
        delete=args.delete,
        now=now or datetime.now(UTC),
        out=out,
    )

    print(
        f"summary: scanned={result.scanned} ignored={result.ignored} recent={result.recent} "
        f"referenced={result.referenced} orphans={result.orphans} deleted={result.deleted} "
        f"failed={result.failed}",
        file=out,
    )
    for message in result.errors:
        print(f"error: {message}", file=err)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
