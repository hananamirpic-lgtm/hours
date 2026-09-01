"""Shared constants for the authentication tests.

A module rather than a fixture, and not `conftest`: pytest puts both `tests/` and `tests/integration/`
on `sys.path`, so `from conftest import ...` resolves to whichever conftest was imported first. A
named module removes the ambiguity, and the value is a constant that no test needs to vary.
"""

from __future__ import annotations

#: The password every test user is created with, unless a test says otherwise.
DEFAULT_PASSWORD = "correct-horse-battery-staple"
