"""T14: the `service` locale files carry a text for every error code (R8.12).

The Hebrew and English files live in the front end (`frontend/src/i18n/locales/{he,en}/service.json`).
The `api` container does not mount `frontend/`, so the folder is found in this order:

1. the environment variable `SERVICE_LOCALES_DIR`, a folder that holds `he/service.json` and
   `en/service.json`;
2. `<repository root>/frontend/src/i18n/locales`, which exists when the tests run on the host
   (outside the container) or in a container that mounts the whole repository.

When neither exists the tests skip with an explicit message instead of passing silently. Run them for
real with the folder mounted, without touching docker-compose.yml:

    docker compose run --rm -v ${PWD}/frontend:/frontend -e SERVICE_LOCALES_DIR=/frontend/src/i18n/locales \
        api python -m pytest tests/test_service_locale_codes.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.service_tickets.error_codes import ERROR_CODES

LANGUAGES = ("he", "en")
ENV_VAR = "SERVICE_LOCALES_DIR"


def _locales_dir() -> Path | None:
    configured = os.environ.get(ENV_VAR, "").strip()
    candidates = [Path(configured)] if configured else []
    # tests/ -> backend/ -> repository root
    candidates.append(Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "locales")
    for candidate in candidates:
        if all((candidate / lang / "service.json").is_file() for lang in LANGUAGES):
            return candidate
    return None


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        flat: dict[str, Any] = {}
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
        return flat
    return {prefix: node}


@pytest.fixture(scope="module")
def catalogues() -> dict[str, dict[str, Any]]:
    directory = _locales_dir()
    if directory is None:
        pytest.skip(
            f"service locale files not found: set {ENV_VAR} to a folder holding he/service.json and "
            "en/service.json (the api container does not mount frontend/); see this module's docstring "
            "for the docker compose command that mounts it"
        )
    loaded: dict[str, dict[str, Any]] = {}
    for lang in LANGUAGES:
        raw = (directory / lang / "service.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{lang}/service.json must be UTF-8 without BOM"
        loaded[lang] = json.loads(raw.decode("utf-8"))
    return loaded


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_error_code_has_a_text(catalogues: dict[str, dict[str, Any]], lang: str) -> None:
    flat = _flatten(catalogues[lang])
    missing = [code for code in ERROR_CODES if not str(flat.get(f"errors.{code}", "")).strip()]
    assert missing == [], f"{lang}: no errors.<code> text for {missing}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_generic_error_text_exists(catalogues: dict[str, dict[str, Any]], lang: str) -> None:
    assert str(_flatten(catalogues[lang]).get("errors.generic", "")).strip()


def test_key_sets_are_identical(catalogues: dict[str, dict[str, Any]]) -> None:
    he_keys = set(_flatten(catalogues["he"]))
    en_keys = set(_flatten(catalogues["en"]))
    assert sorted(he_keys - en_keys) == [], "keys only in he"
    assert sorted(en_keys - he_keys) == [], "keys only in en"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_no_value_is_empty(catalogues: dict[str, dict[str, Any]], lang: str) -> None:
    bad = [
        key
        for key, value in _flatten(catalogues[lang]).items()
        if not isinstance(value, str) or not value.strip()
    ]
    assert bad == [], f"{lang}: empty or non-string values at {bad}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_error_texts_expose_no_internal_detail(catalogues: dict[str, dict[str, Any]], lang: str) -> None:
    # R8.12 and the design: texts never show a code, a status number or a stack trace to the user.
    for code in ERROR_CODES:
        text = str(_flatten(catalogues[lang])[f"errors.{code}"])
        assert code not in text, f"{lang}: errors.{code} shows its own code"
        assert "Traceback" not in text
