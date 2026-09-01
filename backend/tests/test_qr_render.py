"""Rendering a site's QR to PNG and PDF (Requirement 8.1, 8.2, 8.3, 8.5).

The claims here are about the artefacts an administrator downloads: a unified site produces one code
and a separate site two, each is a real PNG and a real PDF, each encodes a token that verifies at the
site's current version, and the token is minted per version so a fresh render follows a regeneration.
Font metrics and exact layout are not tested — they are not a requirement — only that the bytes are a
well-formed image and document.
"""

from __future__ import annotations

import uuid

from app.core import qr_token
from app.core.qr_token import QrAction
from app.models.site import QrMode, Site
from app.services import qr as qr_service

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _site(*, mode: QrMode = QrMode.UNIFIED, version: int = 1) -> Site:
    return Site(
        id=uuid.uuid4(),
        name="North Gate",
        site_number="S-42",
        client_id=uuid.uuid4(),
        qr_mode=mode,
        qr_token="placeholder",
        qr_token_version=version,
    )


def test_a_unified_site_renders_one_png_and_one_pdf():
    """Requirement 8.2: a unified site has a single code."""
    rendered = qr_service.render_site_qr(_site())

    assert len(rendered.png) == 1
    assert len(rendered.pdf) == 1
    assert rendered.png[0].action is None


def test_a_separate_site_renders_a_code_per_action():
    """Requirement 8.3: a separate site has a check-in code and a check-out code."""
    rendered = qr_service.render_site_qr(_site(mode=QrMode.SEPARATE))

    actions = {art.action for art in rendered.png}
    assert actions == {QrAction.CHECK_IN, QrAction.CHECK_OUT}
    assert len(rendered.pdf) == 2


def test_the_png_is_a_real_png():
    rendered = qr_service.render_site_qr(_site())
    assert rendered.png[0].content.startswith(_PNG_MAGIC)
    assert rendered.png[0].media_type == "image/png"


def test_the_pdf_is_a_real_pdf_and_carries_the_site_name_and_number():
    """Requirement 8.5: the PDF is well-formed and includes the readable identifier text."""
    rendered = qr_service.render_site_qr(_site())
    pdf = rendered.pdf[0].content

    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert rendered.pdf[0].media_type == "application/pdf"
    # The site name and number are drawn as a visible text label on the sheet.
    assert b"North Gate" in pdf
    assert b"S-42" in pdf


def test_the_rendered_token_verifies_at_the_current_version():
    """The encoded token is a live one for the site (Requirement 8.4)."""
    site = _site(version=3)
    rendered = qr_service.render_site_qr(site)

    parsed = qr_token.verify(rendered.png[0].token, expected_version=3)
    assert parsed.site_id == site.id
    assert parsed.version == 3


def test_a_render_after_a_regeneration_encodes_the_new_version():
    """Requirement 8.6: a sheet rendered at version 2 does not verify against the old version 1."""
    site = _site(version=2)
    rendered = qr_service.render_site_qr(site)

    # The freshly rendered token is version 2; the version-1 check the old print would pass fails.
    import pytest

    with pytest.raises(qr_token.InvalidToken):
        qr_token.verify(rendered.png[0].token, expected_version=1)
    assert qr_token.verify(rendered.png[0].token, expected_version=2).version == 2
