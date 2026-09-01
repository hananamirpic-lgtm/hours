"""Database roles.

The application connects as a role that must not be able to rewrite history: `change_logs` is
append-only, and that is enforced by PostgreSQL privileges rather than by convention, because a
convention cannot survive a bug in a service method (Requirement 13.3).

The name is repeated as a literal inside the initial migration on purpose. A migration is a frozen
record of what was applied; if it read this constant, renaming the constant would silently change
the meaning of an already-applied migration. `tests/integration/test_change_logs_append_only.py`
asserts the two stay in step.
"""

from __future__ import annotations

APPLICATION_ROLE = "hours_app"
"""Role the API is expected to connect as, or to be a member of."""
