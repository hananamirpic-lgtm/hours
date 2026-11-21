"""Ticket photo processing: bytes in, a clean JPEG or a coded error out (R11.2 to R11.7).

This module is pure. It touches no database, no object store, no clock and no environment, so it can be
tested with generated images. `photo_store.py` is the only code that talks to the object store.

What is trusted
---------------
Only the bytes. The file name and the declared content type never reach this module (`process_photo`
takes `data` and nothing else). `Image.open(..., formats=("JPEG", "PNG", "WEBP"))` decides by content;
a GIF, BMP, TIFF, SVG, PDF or a text file renamed to `.jpg` is rejected.

What the caller must do first
-----------------------------
The router rejects a part over 10,485,760 bytes with 413 `photo_too_large` before it calls this module
(R11.4). This module does not repeat that check, and it reads whatever it is given, so it must not be
called with an unbounded body.

Pipeline (design.md, "Photo pipeline")
--------------------------------------
1. Open by content. Zero bytes, garbage and every other format raise `InvalidPhotoType`.
2. Header check, before any pixel is decoded: `width * height > 50,000,000` raises
   `PhotoDimensionsTooLarge`. Pillow's own `DecompressionBombError` (above twice its limit of
   89,478,485 pixels, so about 179 million) and `DecompressionBombWarning` map to the same code, so a
   header that claims billions of pixels never reaches a decoder.
3. `seek(0)` then `load()`: the first frame of an animated PNG or WebP, and nothing else is decoded. A
   truncated or corrupt file raises `InvalidPhotoType` (Pillow's `LOAD_TRUNCATED_IMAGES` stays false;
   this module never changes any Pillow global).
4. Orientation: `ImageOps.exif_transpose(in_place=True)`, so no second full-size copy is made.
5. Transparency (alpha, palette transparency, colour-key transparency) is kept as RGBA until the image
   is small, then pasted on a white canvas. Resizing happens on premultiplied alpha, so no colour bleeds
   from transparent pixels into the edge.
6. `thumbnail((1600, 1600), LANCZOS)`. It only shrinks and keeps the aspect ratio, so an image whose
   longest side is 1600 pixels or less keeps its size.
7. A non-sRGB ICC profile (RGB or CMYK images) is converted to sRGB with `ImageCms`. Best effort: any
   failure leaves the pixels as they are. CMYK without a usable profile uses Pillow's plain conversion.
8. Rebuild from pixels: `Image.frombytes("RGB", size, rgb.tobytes())`. The new object has an empty
   `info`, so no EXIF, XMP, IPTC, ICC profile, PNG text chunk or JPEG comment can ride along.
9. Save as JPEG, quality 85, `optimize=True`, with no `exif`, `icc_profile`, `comment` or `xmp`
   argument. Pillow then writes the JFIF header and the image tables and nothing else.

Steps 5 and 6 run before the rebuild, in a different order from the design's table, so that the
expensive work happens on the reduced image. The result is the same: a transparent pixel ends up white,
and the orientation is applied before the size is limited.

Resource use
------------
* Memory is bounded by the 50 MP header limit. The worst case is a 50 MP RGBA PNG: about 200 MB for the
  decoded image, about 200 MB more for the short moment of an in-place rotation, and nothing large
  after the shrink. With the router's 3-slot semaphore that is the 600 MB the design plans for.
* JPEG input is decoded with DCT scaling (`Image.draft`) down to the smallest size that is still at
  least 1600 pixels on both axes, so a 50 MP phone photo is decoded at a quarter of its pixels or less.
* CPU is bounded by the same limits. A single frame is decoded; the other frames of an animation are
  never touched. The function is synchronous and CPU bound, so the router runs it in a worker thread.
* Pillow's own limit on PNG text and profile chunks (1 MiB decompressed) applies at open. A PNG that
  exceeds it is rejected as `InvalidPhotoType`; real photos stay far below it.
* Pillow releases the GIL inside its decoders and resamplers, so threads do run in parallel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageCms, ImageOps

from app.service_tickets.error_codes import CODE_INVALID_PHOTO_TYPE, CODE_PHOTO_DIMENSIONS_TOO_LARGE

logger = logging.getLogger(__name__)

#: The only formats that are decoded, in Pillow's spelling (R11.2).
ALLOWED_FORMATS: tuple[str, ...] = ("JPEG", "PNG", "WEBP")

#: A file part larger than this is rejected by the router, before this module sees it (R11.4). 10 MB as
#: the requirements count it: 10 x 1,048,576 bytes.
MAX_PHOTO_BYTES = 10_485_760

#: Width times height above this is rejected before decoding (R11.5).
MAX_PIXELS = 50_000_000

#: Longest side of the stored image (R11.6).
MAX_SIDE = 1600

JPEG_QUALITY = 85

#: What the store keeps and the delivery route serves.
CONTENT_TYPE = "image/jpeg"

#: An embedded ICC profile larger than this is ignored, not parsed. Real profiles are a few KB, a
#: display profile with a lookup table is a few hundred KB.
MAX_ICC_BYTES = 1_048_576

_WHITE = (255, 255, 255)
_RESAMPLE = Image.Resampling.LANCZOS
_BOMB_ERRORS = (Image.DecompressionBombError, Image.DecompressionBombWarning)


class PhotoError(ValueError):
    """A photo that cannot be stored. `code` is the API error code (HTTP 422 at the router)."""

    code: str = ""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class InvalidPhotoType(PhotoError):
    """Not a decodable JPEG, PNG or WebP: wrong format, zero bytes, truncated or corrupt (R11.3)."""

    code = CODE_INVALID_PHOTO_TYPE


class PhotoDimensionsTooLarge(PhotoError):
    """A decodable JPEG, PNG or WebP with more than 50,000,000 pixels (R11.5)."""

    code = CODE_PHOTO_DIMENSIONS_TOO_LARGE


@dataclass(frozen=True, slots=True)
class ProcessedPhoto:
    """The stored form of a photo: a metadata-free JPEG and its pixel size."""

    data: bytes
    width: int
    height: int
    content_type: str = CONTENT_TYPE


def process_photo(data: bytes) -> ProcessedPhoto:
    """Validate `data` by content and return it as a clean JPEG of at most 1600 pixels per side.

    Raises `InvalidPhotoType` or `PhotoDimensionsTooLarge`, both `PhotoError` with a `code`. Any other
    exception is a bug or an out-of-memory condition and propagates (the router answers 5xx).
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("data must be bytes")
    if len(data) == 0:
        raise InvalidPhotoType("empty file")

    image = _open(data)
    try:
        _check_pixels(image)
        _decode_first_frame(image)
        rgb = _to_clean_rgb(image)
        # Rebuild from raw pixels: the new image carries no `info`, so no metadata can be written.
        clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
    finally:
        image.close()

    out = BytesIO()
    clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    width, height = clean.size
    return ProcessedPhoto(data=out.getvalue(), width=width, height=height)


def _open(data: bytes | bytearray | memoryview) -> Image.Image:
    """Open by content. Reads the header only."""
    try:
        return Image.open(BytesIO(data), formats=ALLOWED_FORMATS)
    except _BOMB_ERRORS as exc:
        raise PhotoDimensionsTooLarge("declared size is far above the limit") from exc
    except MemoryError:
        raise
    except Exception as exc:  # Pillow raises OSError, ValueError, SyntaxError, struct.error, EOFError ...
        raise InvalidPhotoType("not a decodable JPEG, PNG or WebP") from exc


def _check_pixels(image: Image.Image) -> None:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise InvalidPhotoType("empty image")
    if width * height > MAX_PIXELS:
        raise PhotoDimensionsTooLarge(f"{width}x{height} is above {MAX_PIXELS} pixels")


def _decode_first_frame(image: Image.Image) -> None:
    try:
        image.seek(0)
        if image.format == "JPEG":
            # DCT scaling: decode at 1/2, 1/4 or 1/8 size while both sides stay at least MAX_SIDE.
            image.draft(image.mode, (MAX_SIDE, MAX_SIDE))
        image.load()
    except _BOMB_ERRORS as exc:
        raise PhotoDimensionsTooLarge("decoded size is above the limit") from exc
    except MemoryError:
        raise
    except Exception as exc:  # truncated and corrupt data surface here, as OSError and others
        raise InvalidPhotoType("truncated or corrupt image") from exc


def _to_clean_rgb(image: Image.Image) -> Image.Image:
    """Orientation, transparency, size and colour space. Returns an RGB image; metadata is dropped later."""
    icc_profile = image.info.get("icc_profile")
    _apply_orientation(image)
    pixels = _normalise_mode(image)
    pixels.thumbnail((MAX_SIDE, MAX_SIDE), _RESAMPLE)
    if pixels.mode == "RGBA":
        canvas = Image.new("RGB", pixels.size, _WHITE)
        canvas.paste(pixels, mask=pixels.getchannel("A"))
        pixels = canvas
    if isinstance(icc_profile, bytes) and pixels.mode in ("RGB", "CMYK"):
        converted = _to_srgb(pixels, icc_profile)
        if converted is not None:
            pixels = converted
    if pixels.mode != "RGB":
        pixels = pixels.convert("RGB")
    return pixels


def _apply_orientation(image: Image.Image) -> None:
    """Rotate or flip in place by the EXIF orientation. A broken EXIF block means no rotation."""
    try:
        ImageOps.exif_transpose(image, in_place=True)
    except Exception:
        logger.debug("EXIF orientation ignored: unreadable EXIF block", exc_info=True)


def _normalise_mode(image: Image.Image) -> Image.Image:
    """A mode that LANCZOS resamples well: L, RGB, CMYK, or RGBA when there is transparency."""
    mode = image.mode
    if image.has_transparency_data:
        return image.convert("RGBA")
    if mode.startswith("I;16") or mode == "I":
        try:
            return image.point(lambda value: value * (1 / 256)).convert("L")
        except Exception:
            logger.debug("16-bit grey scaling failed, using the plain conversion", exc_info=True)
            return image.convert("RGB")
    if mode == "1":
        return image.convert("L")
    if mode in ("L", "RGB", "CMYK"):
        return image
    return image.convert("RGB")


def _to_srgb(image: Image.Image, icc_profile: bytes) -> Image.Image | None:
    """Convert to sRGB through the embedded profile. None means: leave the pixels as they are."""
    if not icc_profile or len(icc_profile) > MAX_ICC_BYTES:
        return None
    try:
        source = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
        if "srgb" in _description(source).lower():
            return None
        return ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"), outputMode="RGB")
    except Exception:
        logger.debug("ICC conversion skipped", exc_info=True)
        return None


def _description(profile: ImageCms.ImageCmsProfile) -> str:
    try:
        return ImageCms.getProfileDescription(profile) or ""
    except Exception:
        return ""
