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
2b. Integrity check of the raw bytes (next section). It runs after the pixel check and before any
   decoding, and it does not depend on any Pillow setting.
3. `seek(0)` then `load()`: the first frame of an animated PNG or WebP, and nothing else is decoded. A
   decoder error still raises `InvalidPhotoType`. This is the second line of defence only; see below.
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

Why there is an integrity check (R11.3)
---------------------------------------
Pillow raises on a truncated or corrupt file only while `ImageFile.LOAD_TRUNCATED_IMAGES` is false. That
flag is process-wide, and WeasyPrint, which the Hours app uses for PDF export, sets it to true when it is
imported. From then on, `load()` swallows every decoder error and pads what is missing, so a damaged
photo would be accepted and stored. Pillow's PNG decoder also never checks the CRC of an IDAT chunk.

This module therefore does not rely on the flag, and it never reads or writes it, or any other Pillow
global: another thread may be inside WeasyPrint and need the flag to stay true, so even a short change
would affect the Hours app. Instead `_check_integrity` validates the container structure of the raw
bytes before they reach a decoder. Every failure raises `InvalidPhotoType`.

* PNG, strict. The 8-byte signature; then a walk over all chunks. Each chunk must fit in the data and its
  CRC-32 (over type and data) must match, ancillary chunks included. `IHDR` must be the first chunk, with
  13 bytes, a valid colour type and bit depth combination, compression 0, filter 0 and interlace 0 or 1.
  `IDAT` chunks must be consecutive and at least one must exist. `IEND` must be present, empty and the
  last chunk: any byte after it is rejected (a trailer could hide a second payload, and real encoders
  write none). The concatenated `IDAT` data must inflate with a zlib decompressor to exactly the size
  that `IHDR` implies (rows of `1 + ceil(width * channels * depth / 8)` bytes; the seven Adam7 passes
  summed for interlaced files), must reach the end of the zlib stream (so the Adler-32 is verified) and
  must leave no unused byte. Output is capped at that size plus one byte, so a zip bomb is stopped at the
  first excess byte. Not checked: the filter-type byte of each row (a crafted file with a valid zlib
  stream and CRC but an unknown filter type is still decoded by Pillow, which pads the rest of that
  image in the lenient state). Damage in transit breaks a CRC or the zlib stream first.
* JPEG, lenient about what follows the image. The data must start with SOI (FFD8). The marker structure
  is walked: every segment is skipped by its declared length (so an EOI inside an EXIF thumbnail is not
  mistaken for the end), entropy-coded data is scanned for the next real marker (RSTn, byte stuffing and
  fill bytes are not markers), and the first EOI (FFD9) ends the image. A segment that runs past the end
  of the data, or no EOI at all, is truncation. Bytes after that EOI are accepted: zero padding, the
  second image of an MPO file and the MP4 that Android "motion photos" append all occur in real uploads,
  and nothing after the EOI is decoded or stored, because the output is rebuilt from pixels. Corruption
  inside the entropy-coded data cannot be detected without decoding; libjpeg treats it as a warning in
  every Pillow state.
* WebP, strict. `RIFF`, a size field and `WEBP`; the size field plus 8 must equal the length of the data
  exactly (no pad byte, no trailer). The chunks must tile the area from byte 12 to the end exactly: each
  chunk is an 8-byte header and its payload, padded to an even length as RIFF requires, and the first
  chunk must be `VP8 `, `VP8L` or `VP8X`. libwebp itself rejects a truncated file in every Pillow state;
  this makes the rule explicit and also rejects trailing data.

Containers with more than 100,000 PNG or WebP chunks, or more than 10,000 JPEG segments, are rejected
as well; real files have a few dozen, and the cap keeps the Python-level walk short for a 10 MB part.

Resource use
------------
* The integrity check adds, for a part of at most 10 MB: one CRC-32 pass over the data, at most 100,000
  loop steps for PNG, WebP and JPEG segments, one regular-expression pass over a JPEG, and for PNG one
  inflate that stops at the expected raw size plus one byte. That inflate does about as much work as the
  decoder's own and is the largest cost: at most 50,000,000 pixels at 8 bytes per pixel is 400 MB of
  output, produced and discarded in pieces. The input is fed in 16 KiB slices and deflate cannot expand
  more than 1,032 to 1, so one call allocates 17 MB at most. Nothing is kept. The pixel limit is checked
  before the inflate, so an oversized header never costs any decompression.
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
import re
import struct
import zlib
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

# Integrity check limits (see "Why there is an integrity check" in the module docstring).
_MAX_CHUNKS = 100_000  # PNG chunks or WebP chunks
_MAX_JPEG_SEGMENTS = 10_000
_INFLATE_SLICE = 16_384  # compressed bytes per zlib call; the output of one call is at most 1032 times this

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_MAX_CHUNK = 0x7FFFFFFF
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
#: Adam7 passes as (x start, y start, x step, y step).
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
#: FF followed by anything but 00 (stuffed byte), D0 to D7 (restart markers) and FF (fill byte).
_JPEG_MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")
_WEBP_FIRST_CHUNKS = (b"VP8 ", b"VP8L", b"VP8X")


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
    raw = data if isinstance(data, bytes) else bytes(data)

    image = _open(raw)
    try:
        _check_pixels(image)
        _check_integrity(image.format, raw)
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


def _open(data: bytes) -> Image.Image:
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


def _check_integrity(image_format: str | None, data: bytes) -> None:
    """Reject truncated or corrupt data by its structure, whatever Pillow's global flags are.

    The rules are in the module docstring. Raises `InvalidPhotoType` (or `PhotoDimensionsTooLarge` for a
    PNG header above the pixel limit); returns nothing.
    """
    if image_format == "PNG":
        _check_png(data)
    elif image_format == "JPEG":
        _check_jpeg(data)
    elif image_format == "WEBP":
        _check_webp(data)
    else:
        raise InvalidPhotoType("not a JPEG, PNG or WebP")


def _check_png(data: bytes) -> None:
    if not data.startswith(_PNG_SIGNATURE):
        raise InvalidPhotoType("PNG signature missing")
    size = len(data)
    view = memoryview(data)
    pos = len(_PNG_SIGNATURE)
    expected = 0
    spans: list[tuple[int, int]] = []  # (start, length) of every IDAT payload
    previous = b""
    count = 0
    while True:
        if pos + 12 > size:
            raise InvalidPhotoType("PNG ends before IEND")
        length, kind = struct.unpack_from(">I4s", data, pos)
        body = pos + 8
        end = body + length
        if length > _PNG_MAX_CHUNK or end + 4 > size:
            raise InvalidPhotoType("PNG chunk runs past the end of the data")
        if zlib.crc32(view[pos + 4 : end]) != struct.unpack_from(">I", data, end)[0]:
            raise InvalidPhotoType("PNG chunk CRC mismatch")
        count += 1
        if count > _MAX_CHUNKS or not kind.isalpha():
            raise InvalidPhotoType("PNG chunk structure is not valid")
        if count == 1:
            if kind != b"IHDR" or length != 13:
                raise InvalidPhotoType("PNG does not start with IHDR")
            expected = _png_raw_size(data[body:end])
        elif kind == b"IHDR":
            raise InvalidPhotoType("PNG has a second IHDR")
        elif kind == b"IDAT":
            if spans and previous != b"IDAT":
                raise InvalidPhotoType("PNG IDAT chunks are not consecutive")
            spans.append((body, length))
        elif kind == b"IEND":
            if length != 0:
                raise InvalidPhotoType("PNG IEND is not empty")
            if end + 4 != size:
                raise InvalidPhotoType("PNG has data after IEND")
            break
        previous = kind
        pos = end + 4
    if not spans:
        raise InvalidPhotoType("PNG has no image data")
    _inflate_exactly(view, spans, expected)


def _png_raw_size(header: bytes) -> int:
    """Size of the decompressed image data that an IHDR payload implies. Checks the pixel limit first."""
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    if not (0 < width <= _PNG_MAX_CHUNK and 0 < height <= _PNG_MAX_CHUNK):
        raise InvalidPhotoType("PNG has an empty or impossible size")
    if width * height > MAX_PIXELS:
        raise PhotoDimensionsTooLarge(f"{width}x{height} is above {MAX_PIXELS} pixels")
    if compression or filtering or interlace > 1 or depth not in _PNG_DEPTHS.get(colour, ()):
        raise InvalidPhotoType("PNG header is not valid")
    bits = depth * _PNG_CHANNELS[colour]
    if not interlace:
        return height * (1 + (width * bits + 7) // 8)
    total = 0
    for x_start, y_start, x_step, y_step in _ADAM7:
        pass_width = (width - x_start + x_step - 1) // x_step
        pass_height = (height - y_start + y_step - 1) // y_step
        if pass_width > 0 and pass_height > 0:
            total += pass_height * (1 + (pass_width * bits + 7) // 8)
    return total


def _inflate_exactly(view: memoryview, spans: list[tuple[int, int]], expected: int) -> None:
    """The zlib stream in `spans` must end, with no byte left over, after exactly `expected` bytes."""
    inflater = zlib.decompressobj()
    produced = 0
    try:
        for start, length in spans:
            stop = start + length
            for offset in range(start, stop, _INFLATE_SLICE):
                if inflater.eof:
                    raise InvalidPhotoType("PNG image data continues after the zlib stream ended")
                compressed = view[offset : min(offset + _INFLATE_SLICE, stop)]
                # max_length is at least 1, so 0 (meaning "no limit") never happens.
                produced += len(inflater.decompress(compressed, expected - produced + 1))
                if produced > expected:
                    raise InvalidPhotoType("PNG image data is longer than its header allows")
                if inflater.eof and inflater.unused_data:
                    raise InvalidPhotoType("PNG image data continues after the zlib stream ended")
    except zlib.error as exc:
        raise InvalidPhotoType("PNG image data is corrupt") from exc
    if not inflater.eof:
        raise InvalidPhotoType("PNG image data is truncated")
    if produced != expected:
        raise InvalidPhotoType("PNG image data is shorter than its header requires")


def _check_jpeg(data: bytes) -> None:
    if not data.startswith(b"\xff\xd8"):
        raise InvalidPhotoType("JPEG start marker missing")
    size = len(data)
    pos = 2
    segments = 0
    while True:
        found = _JPEG_MARKER.search(data, pos)
        if found is None:
            raise InvalidPhotoType("JPEG ends before its end-of-image marker")
        marker = data[found.end() - 1]
        pos = found.end()
        if marker == 0xD9:
            return
        if marker in (0xD8, 0x01):  # markers without a length
            continue
        segments += 1
        if segments > _MAX_JPEG_SEGMENTS or pos + 2 > size:
            raise InvalidPhotoType("JPEG segment structure is not valid")
        length = (data[pos] << 8) | data[pos + 1]
        if length < 2 or pos + length > size:
            raise InvalidPhotoType("JPEG segment runs past the end of the data")
        pos += length


def _check_webp(data: bytes) -> None:
    size = len(data)
    if size < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise InvalidPhotoType("WebP header is not valid")
    if int.from_bytes(data[4:8], "little") + 8 != size:
        raise InvalidPhotoType("WebP RIFF size does not match the data length")
    pos = 12
    count = 0
    while pos < size:
        if pos + 8 > size:
            raise InvalidPhotoType("WebP chunk header runs past the end of the data")
        kind = data[pos : pos + 4]
        length = int.from_bytes(data[pos + 4 : pos + 8], "little")
        count += 1
        if count > _MAX_CHUNKS or (count == 1 and kind not in _WEBP_FIRST_CHUNKS):
            raise InvalidPhotoType("WebP chunk structure is not valid")
        pos += 8 + length + (length & 1)  # chunks are padded to an even length
        if pos > size:
            raise InvalidPhotoType("WebP chunk runs past the end of the data")


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
