"""QR image and printable-sheet rendering for a site (Requirement 8.1, 8.5).

The signing and verification of the token live in `app.core.qr_token`; this module is only the
*rendering* half — turning a minted token into a PNG a phone can scan and a PDF an administrator can
print. Every rendered artefact carries the site name and site number as human-readable text
(Requirement 8.5), so a printed sheet on a wall says which site it is without anyone decoding the
code.

**PNG** is produced with the `qrcode` library over Pillow, the generator named in the design.

**PDF** is written directly rather than through a full HTML-to-PDF engine. The design names WeasyPrint
for exports, and it remains the right tool for the reports and payment requests of milestone 9, where
Hebrew right-to-left paragraphs have to shape correctly. A QR sheet is not that: it is one image and
two short lines of Latin-and-digits identifier text, and it must render on every host the API runs on,
including ones without the native GTK/Pango/Cairo stack WeasyPrint links against. So the sheet is a
small, self-contained PDF built here, with the QR embedded as an image and the site name and number
drawn in a standard PDF font. When export rendering lands, its shared WeasyPrint helper can take over
this sheet too if a single path is wanted; until then this keeps the feature working everywhere.

A **separate**-mode site (Requirement 8.3) yields two of each artefact — one per action — each with
its own token and a label saying which is which. A **unified** site yields one.
"""

from __future__ import annotations

import functools
import io
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.image.pil import PilImage

from app.core.qr_token import QrAction, mint
from app.models.site import QrMode, Site

# A comfortable print size. The QR itself is drawn at whatever the module's box size gives; these are
# the sheet's dimensions in PDF points (1 point = 1/72 inch), a portrait half of A4 so two fit a page.
_PAGE_WIDTH = 420
_PAGE_HEIGHT = 595
_QR_RENDER_PX = 360

# The label under the QR is drawn as image pixels, not as PDF text, because the site name may be
# Hebrew and the PDF base-14 fonts (Helvetica et al.) carry no Hebrew glyphs — a PDF-text label would
# come out as "?????". So a bundled TrueType font with Hebrew and Latin coverage is rendered to an
# image strip with Pillow, and that strip is composed onto the QR sheet the PDF embeds. `bidi`
# reorders the mixed Hebrew/Latin label into visual order first, since Pillow (without libraqm) draws
# glyphs in logical order and would otherwise reverse the Hebrew.
_FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansHebrew-Regular.ttf"
_LABEL_FONT_PX = 28
_LABEL_STRIP_PX = 72  # height of the text band drawn beneath the QR, in image pixels


@dataclass(frozen=True, slots=True)
class QrArtifact:
    """One rendered QR: its bytes, media type, the token it encodes, and a human label.

    `action` is `None` for a unified site and set for each of a separate site's two codes, so a
    caller can name the file `site-42-check-in.png` without re-deriving which is which.
    """

    content: bytes
    media_type: str
    token: str
    action: QrAction | None
    label: str


@dataclass(frozen=True, slots=True)
class SiteQr:
    """Everything a site's QR download offers: one artifact when unified, two when separate."""

    site_id: str
    site_number: str
    site_name: str
    mode: QrMode
    png: list[QrArtifact]
    pdf: list[QrArtifact]


def _actions_for(site: Site) -> list[QrAction | None]:
    """The tokens a site needs: one unified, or one per action when separate (Requirement 8.2, 8.3)."""
    if site.qr_mode is QrMode.SEPARATE:
        return [QrAction.CHECK_IN, QrAction.CHECK_OUT]
    return [None]


_ACTION_LABEL = {
    QrAction.CHECK_IN: "Check-in",
    QrAction.CHECK_OUT: "Check-out",
}


def _label_for(site: Site, action: QrAction | None) -> str:
    """The human label printed under the code, naming the site and, for separate mode, the action."""
    base = f"{site.name} · {site.site_number}"
    if action is None:
        return base
    return f"{base} · {_ACTION_LABEL[action]}"


def _scan_url(public_app_url: str, token: str) -> str:
    """Join the public app URL, the `/m/scan` path, and the `token` query parameter.

    All trailing slashes on the base collapse to exactly one separator before `m/scan`, so a base with
    or without a trailing slash produces the same URL. The token is percent-encoded with no safe
    characters, so its `site:` prefix, its `.` separator and any base64url `-`/`_` survive a round-trip
    through a standards-compliant URL parser byte-for-byte.
    """
    base = public_app_url.rstrip("/")
    return f"{base}/m/scan?token={urllib.parse.quote(token, safe='')}"


def render_site_qr(site: Site, *, public_app_url: str) -> SiteQr:
    """Render every QR artefact a site offers, encoding a scan URL rather than the bare token.

    The token is minted at the site's *current* `qr_token_version`, so a sheet printed now encodes the
    live version and a sheet printed before the last regeneration does not — the print side of
    Requirement 8.6; the scan side (rejecting the stale token) is `qr_token.verify`. What the QR encodes
    is now `<public_app_url>/m/scan?token=<token>`, so scanning it with any phone camera opens the
    employee portal. `public_app_url` is passed in so the renderer stays pure — the router resolves the
    setting and refuses a blank value before calling here; the assert is the backstop against a
    programming error silently encoding a base-less URL.
    """
    assert public_app_url.strip(), "public_app_url must be configured before rendering"
    pngs: list[QrArtifact] = []
    pdfs: list[QrArtifact] = []
    for action in _actions_for(site):
        token = mint(site.id, site.qr_token_version, action=action)
        label = _label_for(site, action)
        png_bytes = _render_png(_scan_url(public_app_url, token))
        pngs.append(
            QrArtifact(
                content=png_bytes,
                media_type="image/png",
                token=token,
                action=action,
                label=label,
            )
        )
        pdfs.append(
            QrArtifact(
                content=_render_pdf(png_bytes, label),
                media_type="application/pdf",
                token=token,
                action=action,
                label=label,
            )
        )
    return SiteQr(
        site_id=str(site.id),
        site_number=site.site_number,
        site_name=site.name,
        mode=site.qr_mode,
        png=pngs,
        pdf=pdfs,
    )


# --------------------------------------------------------------------------- PNG


def _render_png(token: str) -> bytes:
    """The token as a PNG, with medium error correction so a scuffed print still scans."""
    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    code.add_data(token)
    code.make(fit=True)
    image = code.make_image(image_factory=PilImage).get_image()
    image = image.resize((_QR_RENDER_PX, _QR_RENDER_PX))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- label rendering


@functools.lru_cache(maxsize=1)
def _label_font() -> ImageFont.FreeTypeFont:
    """The bundled TrueType font used for the sheet label, loaded once and cached.

    A file font rather than Pillow's bitmap default because the label may be Hebrew, which the default
    font cannot draw. Cached because loading a TrueType face is not free and every rendered sheet uses
    the same one.
    """
    return ImageFont.truetype(str(_FONT_PATH), _LABEL_FONT_PX)


def _render_label_strip(label: str, width_px: int) -> Image.Image:
    """Draw the label centred on a white strip `width_px` wide, for composing beneath the QR.

    The text is drawn with Pillow's native right-to-left layout (`direction="rtl"`), which requires
    libraqm (bundled in the image). Raqm runs the Unicode bidirectional algorithm and shapes the run
    itself, so a Hebrew site name — one word or several — reads correctly, and a Latin site number
    embedded in it stays readable, in a single deterministic step that does not vary by library
    version the way a manual reorder did. The strip is a fixed-height band; the text is horizontally
    centred from its measured extent so a short or long name both sit in the middle.
    """
    strip = Image.new("RGB", (width_px, _LABEL_STRIP_PX), "white")
    draw = ImageDraw.Draw(strip)
    font = _label_font()
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font, direction="rtl")
    text_w, text_h = right - left, bottom - top
    x = max(0, (width_px - text_w) // 2 - left)
    y = max(0, (_LABEL_STRIP_PX - text_h) // 2 - top)
    draw.text((x, y), label, font=font, fill="black", direction="rtl")
    return strip


def _compose_sheet_image(png: bytes, label: str) -> Image.Image:
    """The printable sheet as one image: the QR above, the label strip below, on a white canvas.

    Composing the label into the image (rather than drawing it as PDF text) is what lets a Hebrew site
    name appear at all — the PDF's built-in fonts have no Hebrew glyphs. The QR keeps its own pixels
    untouched, so the code still scans; only the human caption is added beneath it.
    """
    qr_image = Image.open(io.BytesIO(png)).convert("RGB")
    width = qr_image.width
    sheet = Image.new("RGB", (width, qr_image.height + _LABEL_STRIP_PX), "white")
    sheet.paste(qr_image, (0, 0))
    sheet.paste(_render_label_strip(label, width_px=width), (0, qr_image.height))
    return sheet


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk, so the image is placed at its real aspect ratio."""
    # Bytes 16:24 of a PNG are the IHDR width and height, big-endian.
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return width, height


# --------------------------------------------------------------------------- PDF
# A minimal, dependency-free PDF: one page embedding a single composed image (the QR with its label
# rendered beneath it as pixels). The label is drawn into the image rather than as PDF text so a
# Hebrew site name renders — the base-14 PDF fonts carry no Hebrew glyphs. The export task owns rich
# multi-page PDFs; this is only a printable code sheet.


def _render_pdf(png: bytes, label: str) -> bytes:
    """A one-page PDF embedding the composed sheet image — the QR with its label beneath it.

    The QR and the label are composed into a single image first (`_compose_sheet_image`), so the label
    is pixels, not PDF text. That is deliberate: the site name may be Hebrew, and the base-14 PDF fonts
    carry no Hebrew glyphs, so a PDF-text label would render as "?????". Drawing the label into the
    image with a bundled Hebrew-capable TrueType font sidesteps font embedding entirely — the PDF only
    ever has to embed one RGB image. The page is sized to the image's aspect ratio, scaled to fit most
    of the page width and centred, with the same numbered-object writer as before (Requirement 8.5).
    """
    sheet = _compose_sheet_image(png, label)
    image_width, image_height = sheet.size

    # Scale the sheet to most of the page width, preserving aspect ratio, and centre it on the page.
    draw_width = _PAGE_WIDTH - 80
    draw_height = draw_width * image_height / image_width
    draw_x = (_PAGE_WIDTH - draw_width) / 2
    draw_y = (_PAGE_HEIGHT - draw_height) / 2

    # The sheet is a raw-image XObject: a flate-compressed stream of interleaved RGB samples.
    image_stream = zlib.compress(sheet.tobytes())

    content = (
        f"q\n{draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm\n/QR Do\nQ\n"
    ).encode("latin-1")

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
        f"/Resources << /XObject << /QR 5 0 R >> >> "
        f"/Contents 4 0 R >>".encode("latin-1")
    )
    objects.append(
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
    )
    objects.append(
        b"<< /Type /XObject /Subtype /Image /Width " + str(image_width).encode()
        + b" /Height " + str(image_height).encode()
        + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length "
        + str(len(image_stream)).encode() + b" >>\nstream\n" + image_stream + b"\nendstream"
    )

    return _assemble_pdf(objects)


def _assemble_pdf(objects: list[bytes]) -> bytes:
    """Serialise numbered objects with a header, body, cross-reference table and trailer."""
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_start = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF".encode()
    )
    return bytes(out)
