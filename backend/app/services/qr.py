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

import io
import zlib
from dataclasses import dataclass

import qrcode
from qrcode.image.pil import PilImage

from app.core.qr_token import QrAction, mint
from app.models.site import QrMode, Site

# A comfortable print size. The QR itself is drawn at whatever the module's box size gives; these are
# the sheet's dimensions in PDF points (1 point = 1/72 inch), a portrait half of A4 so two fit a page.
_PAGE_WIDTH = 420
_PAGE_HEIGHT = 595
_QR_RENDER_PX = 360


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


def render_site_qr(site: Site) -> SiteQr:
    """Render every QR artefact a site offers, minting a current-version token for each.

    The token is minted at the site's *current* `qr_token_version`, so a sheet printed now encodes
    the live version and a sheet printed before the last regeneration does not — which is the print
    side of Requirement 8.6. The scan side (rejecting the stale token) is `qr_token.verify`.
    """
    pngs: list[QrArtifact] = []
    pdfs: list[QrArtifact] = []
    for action in _actions_for(site):
        token = mint(site.id, site.qr_token_version, action=action)
        label = _label_for(site, action)
        png_bytes = _render_png(token)
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


# --------------------------------------------------------------------------- PDF
# A minimal, dependency-free PDF: one page, the QR embedded as an image, the label drawn beneath it in
# Helvetica. Enough for a printable code sheet and nothing more; the export task owns rich PDFs.


def _pdf_escape(text: str) -> str:
    """Escape the characters a PDF text string may not carry literally."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk, so the image is placed at its real aspect ratio."""
    # Bytes 16:24 of a PNG are the IHDR width and height, big-endian.
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return width, height


def _render_pdf(png: bytes, label: str) -> bytes:
    """A one-page PDF placing the QR image centred with the label under it (Requirement 8.5).

    Written by hand as a small set of numbered objects with a cross-reference table, which is all a
    single-image single-line page needs. The label is Latin-and-digit identifier text (site name and
    number), drawn in a base-14 font that every PDF reader carries, so no font has to be embedded.
    """
    width_px, height_px = _png_dimensions(png)

    # Place the QR as a square sized to most of the page width, centred, with room for the label.
    draw_size = _PAGE_WIDTH - 120
    qr_x = (_PAGE_WIDTH - draw_size) / 2
    qr_y = _PAGE_HEIGHT - 90 - draw_size
    label_y = qr_y - 40

    # The QR is a raw-image XObject. It is embedded as a flate-compressed PNG stream via a DCT-free
    # image dictionary; to keep the writer trivial the PNG's own bytes are re-decoded into raw RGB.
    raw_rgb = _png_to_raw_rgb(png, width_px, height_px)
    image_stream = zlib.compress(raw_rgb)

    content = (
        f"q\n{draw_size} 0 0 {draw_size} {qr_x:.2f} {qr_y:.2f} cm\n/QR Do\nQ\n"
        f"BT\n/F1 16 Tf\n{_label_x(label):.2f} {label_y:.2f} Td\n({_pdf_escape(label)}) Tj\nET\n"
    ).encode("latin-1", errors="replace")

    objects: list[bytes] = []

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
        f"/Resources << /XObject << /QR 5 0 R >> /Font << /F1 6 0 R >> >> "
        f"/Contents 4 0 R >>".encode("latin-1")
    )
    objects.append(
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
    )
    objects.append(
        b"<< /Type /XObject /Subtype /Image /Width " + str(width_px).encode()
        + b" /Height " + str(height_px).encode()
        + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length "
        + str(len(image_stream)).encode() + b" >>\nstream\n" + image_stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    return _assemble_pdf(objects)


def _label_x(label: str) -> float:
    """A rough horizontal start so the label sits near the centre; exact metrics are not worth it."""
    approx_width = len(label) * 8
    return max(20.0, (_PAGE_WIDTH - approx_width) / 2)


def _png_to_raw_rgb(png: bytes, width: int, height: int) -> bytes:
    """Decode a PNG to raw interleaved RGB bytes for embedding as a PDF image.

    Uses Pillow, already a dependency through `qrcode[pil]`, so the writer above only has to deal in
    raw samples and never in PNG chunk structure.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != (width, height):
        image = image.resize((width, height))
    return image.tobytes()


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
