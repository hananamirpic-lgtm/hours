"""Company logo processing: bytes in, a clean PNG or a coded error out (R28.2).

This module is pure, like `photo.py`, and reuses its helpers (open by content, header check, raw-byte
integrity check, first-frame decode, orientation, ICC conversion). It touches no database, no object
store and no clock. `photo_store.py` holds the object store and the shared decode semaphore
(`process_logo_limited`).

What is trusted
---------------
Only the bytes. `process_logo(data)` takes nothing else, so a file name or a declared content type can
never decide anything. The format is found by decoding the header (`Image.open(..., formats=...)`), and
only JPEG, PNG and WebP are accepted. A GIF, BMP, TIFF, SVG, PDF or a text file renamed to `.png` is
`invalid_logo_type`.

Rules (design.md, "Branding (R28)")
-----------------------------------
1. More than 1 MiB of input is `LogoTooLarge` (HTTP 413 `logo_too_large`), whatever the bytes are. The
   router never reads more than that plus one byte, and this function repeats the check.
2. Zero bytes, garbage, another format, a truncated or corrupt file: `InvalidLogoType` (422
   `invalid_logo_type`).
3. A width or a height above 2,000 pixels is `LogoDimensionsTooLarge` (422 `logo_dimensions_too_large`).
   The size comes from the header and is checked before any pixel is decoded, so a decompression bomb is
   never decoded. Pillow's own bomb error maps to the same code.
4. The raw bytes are checked for integrity by `photo._check_integrity` (PNG chunks, CRCs and the zlib
   stream, JPEG markers up to the end-of-image, the WebP RIFF structure). That check does not depend on
   any Pillow setting. WeasyPrint sets `ImageFile.LOAD_TRUNCATED_IMAGES` to true for the whole process
   once it is imported, which makes Pillow pad a truncated image instead of raising, so this module never
   relies on the flag and never reads or writes it, or `Image.MAX_IMAGE_PIXELS`, or any other Pillow
   global.
5. Animated images: only the first frame is decoded and kept (an animated WebP or APNG becomes a still
   PNG). GIF is not an accepted format, so an animated GIF is rejected as `invalid_logo_type`.
6. EXIF orientation is applied. Transparency (alpha, palette transparency, colour key) is kept as RGBA;
   opaque images become RGB. A CMYK or a 16-bit grey image is converted, and a non-sRGB ICC profile of an
   RGB or CMYK image is converted to sRGB (best effort, as for photos).
7. `thumbnail((400, 200), LANCZOS)`: the image only shrinks, keeps its aspect ratio and is never
   enlarged. Resizing of RGBA works on premultiplied alpha, so no colour bleeds from transparent pixels.
8. The result is rebuilt from raw pixels, so the new image has an empty `info`: no EXIF, XMP, IPTC, ICC
   profile, PNG text chunk or comment survives. It is saved as PNG with no extra argument, so the file
   holds only the header, the image data and the end chunk. The logo is never stored as it was uploaded.

Resource use is bounded by the 2,000 by 2,000 header cap (at most 16 MB of RGBA) and the 1 MiB input
cap. The function is synchronous and CPU bound; the router runs it in a worker thread under the decode
semaphore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from app.service_tickets import photo
from app.service_tickets.error_codes import (
    CODE_INVALID_LOGO_TYPE,
    CODE_LOGO_DIMENSIONS_TOO_LARGE,
    CODE_LOGO_TOO_LARGE,
)

logger = logging.getLogger(__name__)

#: Input larger than this is rejected before anything is opened: 1 MiB (R28.2).
MAX_LOGO_BYTES = 1_048_576

#: The input may be at most this many pixels wide and at most this many high (R28.2).
MAX_INPUT_SIDE = 2_000

#: The stored logo fits in this box (R28.2).
MAX_OUTPUT_WIDTH = 400
MAX_OUTPUT_HEIGHT = 200

#: What the store keeps and the delivery route serves.
CONTENT_TYPE = "image/png"

_RESAMPLE = Image.Resampling.LANCZOS


class LogoError(ValueError):
    """A logo that cannot be stored. `code` is the API error code and `status` the HTTP status."""

    code: str = ""
    status: int = 422

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class InvalidLogoType(LogoError):
    """Not a decodable JPEG, PNG or WebP: wrong format, zero bytes, truncated or corrupt."""

    code = CODE_INVALID_LOGO_TYPE
    status = 422


class LogoDimensionsTooLarge(LogoError):
    """A decodable JPEG, PNG or WebP wider or higher than 2,000 pixels."""

    code = CODE_LOGO_DIMENSIONS_TOO_LARGE
    status = 422


class LogoTooLarge(LogoError):
    """More than 1 MiB of input."""

    code = CODE_LOGO_TOO_LARGE
    status = 413


@dataclass(frozen=True, slots=True)
class ProcessedLogo:
    """The stored form of a logo: a metadata-free PNG and its pixel size."""

    data: bytes
    width: int
    height: int
    content_type: str = CONTENT_TYPE


def process_logo(data: bytes) -> ProcessedLogo:
    """Validate `data` by content and return it as a clean PNG that fits in 400 by 200 pixels.

    Raises `InvalidLogoType`, `LogoDimensionsTooLarge` or `LogoTooLarge`, all `LogoError` with a `code`
    and a `status`. Any other exception is a bug or an out-of-memory condition and propagates.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("data must be bytes")
    if len(data) > MAX_LOGO_BYTES:
        raise LogoTooLarge(f"more than {MAX_LOGO_BYTES} bytes")
    if len(data) == 0:
        raise InvalidLogoType("empty file")
    raw = data if isinstance(data, bytes) else bytes(data)

    try:
        clean = _clean_pixels(raw)
    except photo.PhotoDimensionsTooLarge as exc:
        raise LogoDimensionsTooLarge(str(exc)) from exc
    except photo.PhotoError as exc:
        raise InvalidLogoType(str(exc)) from exc

    out = BytesIO()
    clean.save(out, format="PNG", optimize=True)
    width, height = clean.size
    return ProcessedLogo(data=out.getvalue(), width=width, height=height)


def _clean_pixels(raw: bytes) -> Image.Image:
    """The decoded, oriented, shrunk image rebuilt from raw pixels. Raises the `photo` errors."""
    image = photo._open(raw)  # noqa: SLF001 - the one pipeline of "open by content", shared on purpose
    try:
        _check_side(image)
        photo._check_integrity(image.format, raw)  # noqa: SLF001
        photo._decode_first_frame(image)  # noqa: SLF001
        return _to_clean_png_pixels(image)
    finally:
        image.close()


def _check_side(image: Image.Image) -> None:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise photo.InvalidPhotoType("empty image")
    if width > MAX_INPUT_SIDE or height > MAX_INPUT_SIDE:
        raise photo.PhotoDimensionsTooLarge(f"{width}x{height} is above {MAX_INPUT_SIDE} pixels a side")


def _to_clean_png_pixels(image: Image.Image) -> Image.Image:
    icc_profile = image.info.get("icc_profile")
    photo._apply_orientation(image)  # noqa: SLF001
    pixels = photo._normalise_mode(image)  # noqa: SLF001
    pixels.thumbnail((MAX_OUTPUT_WIDTH, MAX_OUTPUT_HEIGHT), _RESAMPLE)
    if isinstance(icc_profile, bytes) and pixels.mode in ("RGB", "CMYK"):
        converted = photo._to_srgb(pixels, icc_profile)  # noqa: SLF001
        if converted is not None:
            pixels = converted
    if pixels.mode not in ("RGB", "RGBA"):
        pixels = pixels.convert("RGB")  # L, CMYK
    # Rebuild from raw pixels: the new image carries no `info`, so no metadata can be written.
    return Image.frombytes(pixels.mode, pixels.size, pixels.tobytes())
