"""Example tests of the pure photo processing (R11.2, R11.3, R11.5, R11.6, R11.7).

Every image is generated with Pillow in memory. Nothing here touches the database or the object store.
"""

from __future__ import annotations

import inspect
import random
import struct
import threading
import warnings
import zlib
from io import BytesIO

import pytest
from PIL import ExifTags, Image, ImageCms, ImageFile
from PIL import features as pil_features
from PIL.PngImagePlugin import PngInfo

from app.service_tickets import photo
from app.service_tickets.error_codes import CODE_INVALID_PHOTO_TYPE, CODE_PHOTO_DIMENSIONS_TOO_LARGE
from app.service_tickets.photo import (
    InvalidPhotoType,
    PhotoDimensionsTooLarge,
    PhotoError,
    ProcessedPhoto,
    process_photo,
)

RED = (255, 0, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)


# --- helpers -------------------------------------------------------------------------------------


def encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def decode(result: ProcessedPhoto) -> Image.Image:
    image = Image.open(BytesIO(result.data))
    image.load()
    return image


def solid(size: tuple[int, int], color: object = (90, 140, 200), mode: str = "RGB") -> Image.Image:
    return Image.new(mode, size, color)  # type: ignore[arg-type]


def noisy(size: tuple[int, int], seed: int = 1) -> Image.Image:
    rng = random.Random(seed)
    return Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))


def near(pixel: tuple[int, ...], expected: tuple[int, int, int], tolerance: int = 12) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], expected, strict=True))


def jpeg_markers(data: bytes) -> list[int]:
    """Marker codes of a JPEG up to and including the start of scan."""
    assert data[:2] == b"\xff\xd8"
    markers: list[int] = []
    position = 2
    while position < len(data):
        assert data[position] == 0xFF, f"no marker at {position}"
        marker = data[position + 1]
        markers.append(marker)
        if marker == 0xDA:  # SOS: entropy coded data follows
            break
        position += 2 + int.from_bytes(data[position + 2 : position + 4], "big")
    return markers


def insert_jpeg_segments(jpeg: bytes, *segments: tuple[int, bytes]) -> bytes:
    body = b"".join(
        b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload
        for marker, payload in segments
    )
    return jpeg[:2] + body + jpeg[2:]


def exif_with_secrets(orientation: int | None = None) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "SECRET-MAKE"
    exif[0x0110] = "SECRET-MODEL"
    exif[0x0132] = "2024:01:02 03:04:05"
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (32.0, 4.0, 5.0)
    gps[ExifTags.GPS.GPSLongitudeRef] = "E"
    gps[ExifTags.GPS.GPSLongitude] = (34.0, 47.0, 6.0)
    if orientation is not None:
        exif[0x0112] = orientation
    return exif


SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def display_p3_like_icc() -> bytes:
    """A minimal ICC v2 matrix profile with Display P3 primaries (D50 adapted) and a gamma 2.2 curve."""

    def s15(value: float) -> bytes:
        return struct.pack(">i", round(value * 65536))

    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ \0\0\0\0" + s15(x) + s15(y) + s15(z)

    name = b"Display P3\0"
    desc = (
        b"desc\0\0\0\0"
        + struct.pack(">I", len(name))
        + name
        + struct.pack(">II", 0, 0)
        + b"\0\0\0"
        + bytes(67)
    )
    curve = b"curv\0\0\0\0" + struct.pack(">IH", 1, round(2.2 * 256)) + b"\0\0"
    entries: list[tuple[bytes, bytes]] = [
        (b"desc", desc),
        (b"cprt", b"text\0\0\0\0test\0"),
        (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
        (b"rXYZ", xyz(0.5151, 0.2412, -0.0011)),
        (b"gXYZ", xyz(0.2920, 0.6922, 0.0419)),
        (b"bXYZ", xyz(0.1571, 0.0666, 0.7841)),
        (b"rTRC", curve),
        (b"gTRC", curve),
        (b"bTRC", curve),
    ]
    table = struct.pack(">I", len(entries))
    body = b""
    offset = 128 + 4 + 12 * len(entries)
    for signature, data in entries:
        padded = data + bytes(-len(data) % 4)
        table += signature + struct.pack(">II", offset + len(body), len(data))
        body += padded
    total = 128 + len(table) + len(body)
    header = (
        struct.pack(">I", total)
        + bytes(4)
        + bytes([2, 0x40, 0, 0])
        + b"mntr"
        + b"RGB "
        + b"XYZ "
        + struct.pack(">6H", 2024, 1, 1, 0, 0, 0)
        + b"acsp"
        + bytes(4 + 4 + 4 + 4 + 8)
        + struct.pack(">I", 0)
        + s15(0.9642)
        + s15(1.0)
        + s15(0.8249)
        + bytes(4 + 16 + 28)
    )
    assert len(header) == 128
    return header + table + body


def png_header_only(width: int, height: int) -> bytes:
    """A PNG that declares `width` x `height` and holds no pixel data at all."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def patch_jpeg_size(jpeg: bytes, width: int, height: int) -> bytes:
    """Rewrite the frame header of a JPEG so that it declares another size."""
    position = jpeg.index(b"\xff\xc0")
    return jpeg[: position + 5] + struct.pack(">HH", height, width) + jpeg[position + 9 :]


# --- module facts --------------------------------------------------------------------------------


def test_webp_support_is_available() -> None:
    assert pil_features.check("webp")


def test_process_photo_reads_only_the_bytes() -> None:
    # File name and declared content type cannot be consulted: they are not parameters.
    assert list(inspect.signature(process_photo).parameters) == ["data"]


def test_error_classes_carry_the_api_codes() -> None:
    assert InvalidPhotoType.code == CODE_INVALID_PHOTO_TYPE == "invalid_photo_type"
    assert PhotoDimensionsTooLarge.code == CODE_PHOTO_DIMENSIONS_TOO_LARGE == "photo_dimensions_too_large"
    for error_type in (InvalidPhotoType, PhotoDimensionsTooLarge):
        assert issubclass(error_type, PhotoError)
        assert issubclass(error_type, ValueError)
        assert str(error_type()) == error_type.code


def test_limits_match_the_requirements() -> None:
    assert photo.MAX_PIXELS == 50_000_000
    assert photo.MAX_SIDE == 1600
    assert photo.JPEG_QUALITY == 85
    assert photo.ALLOWED_FORMATS == ("JPEG", "PNG", "WEBP")


def test_pillow_globals_are_untouched() -> None:
    process_photo(encode(solid((8, 8)), "PNG"))
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False
    assert Image.MAX_IMAGE_PIXELS == 89_478_485


def test_str_is_a_programming_error_not_a_photo_error() -> None:
    with pytest.raises(TypeError):
        process_photo("not bytes")  # type: ignore[arg-type]


def test_bytearray_is_accepted() -> None:
    result = process_photo(bytearray(encode(solid((8, 8)), "PNG")))
    assert (result.width, result.height) == (8, 8)


# --- accepted formats and the output form --------------------------------------------------------


@pytest.mark.parametrize(
    "fmt, options",
    [("JPEG", {"quality": 90}), ("PNG", {}), ("WEBP", {"lossless": True})],
)
def test_accepts_each_format_and_returns_a_jpeg(fmt: str, options: dict[str, object]) -> None:
    result = process_photo(encode(solid((64, 48), RED), fmt, **options))
    assert isinstance(result, ProcessedPhoto)
    assert result.content_type == "image/jpeg"
    assert result.data.startswith(b"\xff\xd8\xff") and result.data.endswith(b"\xff\xd9")
    image = decode(result)
    assert image.format == "JPEG"
    assert image.mode == "RGB"
    assert image.size == (result.width, result.height) == (64, 48)
    assert near(image.getpixel((32, 24)), RED)


def test_output_is_quality_85_and_deterministic() -> None:
    source = encode(noisy((120, 90)), "PNG")
    first = process_photo(source)
    assert first.data == process_photo(source).data
    # libjpeg's luminance table at quality 85 starts with 5 (16 scaled by 30 percent).
    assert decode(first).quantization[0][0] == 5


def test_one_pixel_image_is_accepted() -> None:
    result = process_photo(encode(solid((1, 1), RED), "PNG"))
    assert (result.width, result.height) == (1, 1)


@pytest.mark.parametrize("seed", range(3))
def test_threads_give_the_same_result_as_one_thread(seed: int) -> None:
    sources = [encode(noisy((90 + i, 60), seed * 10 + i), "PNG") for i in range(4)]
    expected = [process_photo(source).data for source in sources]
    results: list[bytes | None] = [None] * len(sources)

    def work(index: int) -> None:
        results[index] = process_photo(sources[index]).data

    threads = [threading.Thread(target=work, args=(index,)) for index in range(len(sources))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == expected


# --- pixel limit (R11.5) -------------------------------------------------------------------------


def test_exactly_50_million_pixels_passes() -> None:
    source = encode(Image.new("1", (10_000, 5_000), 1), "PNG")
    assert len(source) < 100_000  # a small file that declares a large size
    result = process_photo(source)
    assert (result.width, result.height) == (1600, 800)


def test_50_million_and_one_pixels_fails() -> None:
    width, height = 16_666_667, 3
    assert width * height == 50_000_001
    source = encode(Image.new("1", (width, height), 1), "PNG")
    assert len(source) < 100_000
    with pytest.raises(PhotoDimensionsTooLarge) as caught:
        process_photo(source)
    assert caught.value.code == "photo_dimensions_too_large"


def test_one_more_row_than_the_limit_fails() -> None:
    with pytest.raises(PhotoDimensionsTooLarge):
        process_photo(encode(Image.new("1", (10_000, 5_001), 1), "PNG"))


def test_header_that_claims_billions_of_pixels_fails_without_pixel_data() -> None:
    # Above twice Pillow's own limit, so Image.open raises DecompressionBombError; it maps to the same code.
    with pytest.raises(PhotoDimensionsTooLarge):
        process_photo(png_header_only(100_000, 100_000))


def test_between_pillow_limit_and_double_it_fails_with_a_warning_filter_off() -> None:
    source = encode(Image.new("1", (12_000, 8_000), 1), "PNG")  # 96 million pixels
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(PhotoDimensionsTooLarge):
            process_photo(source)


def test_between_pillow_limit_and_double_it_fails_when_warnings_are_errors() -> None:
    source = encode(Image.new("1", (12_000, 8_000), 1), "PNG")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(PhotoDimensionsTooLarge):
            process_photo(source)


@pytest.mark.parametrize("claimed", [(10_000, 10_000), (30_000, 30_000)])
def test_jpeg_that_declares_a_huge_size_fails_before_decoding(claimed: tuple[int, int]) -> None:
    patched = patch_jpeg_size(encode(solid((16, 16)), "JPEG"), *claimed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(PhotoDimensionsTooLarge):
            process_photo(patched)


def test_truncated_file_with_oversized_header_reports_the_size() -> None:
    # The header check comes before decoding, so truncation is never reached.
    source = encode(Image.new("1", (10_000, 5_001), 1), "PNG")
    with pytest.raises(PhotoDimensionsTooLarge):
        process_photo(source[: len(source) // 2])


# --- not an image (R11.2, R11.3) -----------------------------------------------------------------


def test_zero_bytes_fail() -> None:
    with pytest.raises(InvalidPhotoType) as caught:
        process_photo(b"")
    assert caught.value.code == "invalid_photo_type"


@pytest.mark.parametrize(
    "data",
    [
        b"\x00",
        b"hello, this is a text file renamed to photo.jpg",
        random.Random(7).randbytes(4096),
        b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='1' height='1'/></svg>",
        b"<html><body><script>alert(1)</script></body></html>",
        b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n",
        b"\xff\xd8\xff",
        b"\x89PNG\r\n\x1a\n",
        b"RIFF\x00\x00\x00\x00WEBP",
        b"MZ" + bytes(200),
    ],
    ids=["one-byte", "text", "random", "svg", "html", "pdf", "jpeg-magic", "png-magic", "webp-magic", "exe"],
)
def test_non_images_fail(data: bytes) -> None:
    with pytest.raises(InvalidPhotoType):
        process_photo(data)


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF", "ICO", "PPM"])
def test_decodable_but_disallowed_formats_fail(fmt: str) -> None:
    # Pillow can read these, so only the format whitelist rejects them.
    with pytest.raises(InvalidPhotoType):
        process_photo(encode(solid((16, 16), RED), fmt))


@pytest.mark.parametrize(
    "fmt, options",
    [("JPEG", {"quality": 95}), ("PNG", {}), ("WEBP", {"lossless": True})],
)
@pytest.mark.parametrize("keep", [0.5, 0.9])
def test_truncated_files_fail(fmt: str, options: dict[str, object], keep: float) -> None:
    source = encode(noisy((160, 120)), fmt, **options)
    with pytest.raises(InvalidPhotoType):
        process_photo(source[: int(len(source) * keep)])


@pytest.mark.parametrize("fmt, options", [("JPEG", {}), ("PNG", {}), ("WEBP", {"lossless": True})])
def test_header_only_files_fail(fmt: str, options: dict[str, object]) -> None:
    with pytest.raises(InvalidPhotoType):
        process_photo(encode(noisy((160, 120)), fmt, **options)[:24])


def test_a_corrupt_png_body_fails() -> None:
    source = bytearray(encode(noisy((120, 90)), "PNG"))
    start = source.index(b"IDAT") + 4
    for offset in range(start + 8, start + 200):
        source[offset] ^= 0xFF
    with pytest.raises(InvalidPhotoType):
        process_photo(bytes(source))


# --- metadata (R11.7) ----------------------------------------------------------------------------

XMP = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b"<rdf:RDF><rdf:Description>SECRET-XMP</rdf:Description></rdf:RDF></x:xmpmeta>"
)
IPTC = (
    b"Photoshop 3.0\0"
    + b"8BIM"
    + b"\x04\x04"
    + b"\0\0"
    + struct.pack(">I", 16)
    + b"\x1c\x02\x5a\x00\x0bSECRET-CITY"
)
ALLOWED_JPEG_MARKERS = {0xE0, 0xDB, 0xC0, 0xC4, 0xDA}  # JFIF, quantization, frame, Huffman, scan


def assert_clean(result: ProcessedPhoto) -> None:
    assert b"SECRET" not in result.data
    assert b"Exif" not in result.data
    assert b"xmp" not in result.data.lower()
    assert b"Photoshop" not in result.data
    assert set(jpeg_markers(result.data)) <= ALLOWED_JPEG_MARKERS
    image = Image.open(BytesIO(result.data))
    assert not image.getexif()
    assert set(image.info) <= {"jfif", "jfif_version", "jfif_unit", "jfif_density", "dpi"}
    assert "icc_profile" not in image.info


def test_jpeg_metadata_is_removed() -> None:
    exif = exif_with_secrets()
    source = encode(solid((64, 48), RED), "JPEG", exif=exif, icc_profile=SRGB_ICC)
    source = insert_jpeg_segments(
        source,
        (0xE1, b"http://ns.adobe.com/xap/1.0/\0" + XMP),
        (0xED, IPTC),
        (0xFE, b"SECRET-COMMENT"),
    )
    # Guard against a vacuous test: the input really carries each kind of metadata.
    assert {0xE1, 0xE2, 0xED, 0xFE} <= set(jpeg_markers(source))
    reopened = Image.open(BytesIO(source))
    assert reopened.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    assert reopened.info["comment"] == b"SECRET-COMMENT"
    assert_clean(process_photo(source))


def test_png_metadata_is_removed() -> None:
    info = PngInfo()
    info.add_text("Comment", "SECRET-COMMENT")
    info.add_text("Author", "SECRET-AUTHOR")
    info.add_itxt("XML:com.adobe.xmp", XMP.decode())
    source = encode(
        solid((64, 48), RED), "PNG", exif=exif_with_secrets().tobytes(), pnginfo=info, icc_profile=SRGB_ICC
    )
    assert b"SECRET-COMMENT" in source and b"SECRET-MAKE" in source and b"SECRET-XMP" in source
    assert_clean(process_photo(source))


def test_webp_metadata_is_removed() -> None:
    source = encode(
        solid((64, 48), RED),
        "WEBP",
        lossless=True,
        exif=exif_with_secrets().tobytes(),
        xmp=XMP,
        icc_profile=SRGB_ICC,
    )
    assert b"SECRET-MAKE" in source and b"SECRET-XMP" in source
    assert_clean(process_photo(source))


def test_an_unreadable_exif_block_does_not_reject_the_photo() -> None:
    source = insert_jpeg_segments(encode(solid((64, 48), RED), "JPEG"), (0xE1, b"Exif\0\0" + b"\xff" * 40))
    result = process_photo(source)
    assert (result.width, result.height) == (64, 48)
    assert_clean(result)


# --- orientation (R11.6) -------------------------------------------------------------------------


def orientation_source(orientation: int, size: tuple[int, int] = (40, 20)) -> bytes:
    """White image with a red block in the top-left corner and an EXIF orientation tag."""
    image = solid(size, WHITE)
    image.paste(Image.new("RGB", (12, 12), RED), (0, 0))
    exif = Image.Exif()
    exif[0x0112] = orientation
    return encode(image, "JPEG", quality=95, exif=exif)


def moved_point(orientation: int, point: tuple[int, int], size: tuple[int, int]) -> tuple[int, int]:
    """Where a stored pixel shows up once the EXIF orientation is applied (EXIF specification, tag 0x0112)."""
    (x, y), (w, h) = point, size
    return {
        1: (x, y),
        2: (w - 1 - x, y),
        3: (w - 1 - x, h - 1 - y),
        4: (x, h - 1 - y),
        5: (y, x),
        6: (h - 1 - y, x),
        7: (h - 1 - y, w - 1 - x),
        8: (y, w - 1 - x),
    }[orientation]


@pytest.mark.parametrize("orientation", range(1, 9))
def test_orientation_is_applied(orientation: int) -> None:
    result = process_photo(orientation_source(orientation))
    swapped = orientation >= 5
    assert (result.width, result.height) == ((20, 40) if swapped else (40, 20))
    image = decode(result)
    assert near(image.getpixel(moved_point(orientation, (5, 5), (40, 20))), RED, 40)
    assert near(image.getpixel(moved_point(orientation, (30, 15), (40, 20))), WHITE, 20)
    assert not image.getexif().get(0x0112)  # no orientation tag is left to be applied twice


def test_orientation_of_a_large_jpeg_is_applied_after_scaling() -> None:
    source = orientation_source(6, (6000, 4000))
    result = process_photo(source)
    assert result.height == 1600 and result.width in (1066, 1067)


# --- transparency (R11.6) ------------------------------------------------------------------------


def test_transparent_png_becomes_white() -> None:
    image = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
    image.paste(Image.new("RGBA", (32, 64), (0, 0, 255, 255)), (0, 0))
    out = decode(process_photo(encode(image, "PNG")))
    assert near(out.getpixel((10, 32)), BLUE)
    assert near(out.getpixel((54, 32)), WHITE, 4)


def test_half_transparent_pixels_blend_with_white() -> None:
    out = decode(process_photo(encode(Image.new("RGBA", (32, 32), (0, 0, 0, 128)), "PNG")))
    assert all(120 <= channel <= 136 for channel in out.getpixel((16, 16)))


def test_transparent_color_does_not_bleed_into_the_edge_when_scaled() -> None:
    # Red pixels with alpha 0 next to opaque blue: the shrink must not pull the red into the edge.
    image = Image.new("RGBA", (3200, 1600), (255, 0, 0, 0))
    image.paste(Image.new("RGBA", (1600, 1600), (0, 0, 255, 255)), (0, 0))
    out = decode(process_photo(encode(image, "PNG")))
    assert out.size == (1600, 800)
    for x in range(780, 820):
        r, g, b = out.getpixel((x, 400))
        assert abs(r - g) <= 3  # white and blue mixes only; red from the transparent side would raise r


def test_palette_png_with_transparency_becomes_white() -> None:
    image = Image.new("P", (32, 32), 0)
    image.putpalette([255, 0, 0, 0, 0, 255] + [0] * 250)
    image.paste(1, (0, 0, 16, 32))
    out = decode(process_photo(encode(image, "PNG", transparency=0)))
    assert near(out.getpixel((24, 16)), WHITE, 4)  # index 0 is transparent
    assert near(out.getpixel((8, 16)), BLUE)


def test_grey_alpha_png_becomes_white() -> None:
    out = decode(process_photo(encode(Image.new("LA", (16, 16), (0, 0)), "PNG")))
    assert near(out.getpixel((8, 8)), WHITE, 4)


def test_color_key_transparency_becomes_white() -> None:
    image = Image.new("RGB", (32, 32), RED)
    image.paste(Image.new("RGB", (16, 32), BLUE), (0, 0))
    out = decode(process_photo(encode(image, "PNG", transparency=RED)))
    assert near(out.getpixel((24, 16)), WHITE, 4)
    assert near(out.getpixel((8, 16)), BLUE)


def test_webp_with_alpha_becomes_white() -> None:
    out = decode(process_photo(encode(Image.new("RGBA", (32, 32), (255, 0, 0, 0)), "WEBP", lossless=True)))
    assert near(out.getpixel((16, 16)), WHITE, 4)


# --- animation (R11.6) ---------------------------------------------------------------------------


def test_animated_webp_gives_the_first_frame() -> None:
    frames = [solid((48, 32), RED), solid((48, 32), BLUE), solid((48, 32), (0, 255, 0))]
    source = encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=100, lossless=True)
    assert getattr(Image.open(BytesIO(source)), "n_frames", 1) == 3  # the input is really animated
    out = decode(process_photo(source))
    assert out.size == (48, 32)
    assert near(out.getpixel((24, 16)), RED)


def test_animated_png_gives_the_first_frame() -> None:
    frames = [solid((48, 32), RED), solid((48, 32), BLUE)]
    source = encode(frames[0], "PNG", save_all=True, append_images=frames[1:], duration=100)
    assert getattr(Image.open(BytesIO(source)), "n_frames", 1) == 2
    out = decode(process_photo(source))
    assert near(out.getpixel((24, 16)), RED)


# --- size (R11.6) --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size, expected",
    [
        ((1600, 1000), (1600, 1000)),
        ((1000, 1600), (1000, 1600)),
        ((1600, 1600), (1600, 1600)),
        ((1000, 500), (1000, 500)),
        ((200, 100), (200, 100)),
        ((3200, 1600), (1600, 800)),
        ((1000, 4000), (400, 1600)),
        ((1601, 1601), (1600, 1600)),
        ((4000, 3000), (1600, 1200)),
    ],
)
def test_longest_side_is_limited_and_nothing_is_enlarged(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    result = process_photo(encode(solid(size), "PNG"))
    assert (result.width, result.height) == expected
    assert decode(result).size == expected


def test_a_large_jpeg_is_reduced_to_1600() -> None:
    result = process_photo(encode(solid((6000, 4000)), "JPEG"))
    assert result.width == 1600 and result.height in (1066, 1067)
    assert decode(result).size == (result.width, result.height)


def test_a_jpeg_between_1600_and_3200_is_not_decoded_below_1600() -> None:
    result = process_photo(encode(solid((2400, 1800)), "JPEG"))
    assert (result.width, result.height) == (1600, 1200)


# --- colour modes and profiles -------------------------------------------------------------------


def test_cmyk_jpeg_is_converted_to_rgb() -> None:
    red = Image.new("CMYK", (32, 32), (0, 255, 255, 0))
    out = decode(process_photo(encode(red, "JPEG", quality=95)))
    assert out.mode == "RGB"
    assert near(out.getpixel((16, 16)), RED, 30)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_grayscale_is_accepted(fmt: str) -> None:
    out = decode(process_photo(encode(Image.new("L", (32, 32), 100), fmt)))
    assert near(out.getpixel((16, 16)), (100, 100, 100), 4)


def test_sixteen_bit_grayscale_png_keeps_its_brightness() -> None:
    image = Image.new("I;16", (32, 32), 32768)
    out = decode(process_photo(encode(image, "PNG")))
    assert near(out.getpixel((16, 16)), (128, 128, 128), 4)


def test_one_bit_and_palette_png_are_accepted() -> None:
    white = decode(process_photo(encode(Image.new("1", (16, 16), 1), "PNG")))
    assert near(white.getpixel((8, 8)), WHITE, 2)
    palette = Image.new("P", (16, 16), 1)
    palette.putpalette([0, 0, 0, 0, 0, 255] + [0] * 250)
    blue = decode(process_photo(encode(palette, "PNG")))
    assert near(blue.getpixel((8, 8)), BLUE)


def test_srgb_profile_leaves_the_colors_alone() -> None:
    color = (200, 80, 80)
    out = decode(process_photo(encode(solid((64, 64), color), "JPEG", quality=98, icc_profile=SRGB_ICC)))
    assert near(out.getpixel((32, 32)), color, 4)
    assert "icc_profile" not in out.info


def test_display_p3_profile_is_converted_to_srgb() -> None:
    profile = display_p3_like_icc()
    color = (200, 80, 80)
    source = encode(solid((64, 64), color), "PNG", icc_profile=profile)
    assert "Display P3" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(BytesIO(profile)))
    out = decode(process_photo(source))
    red, green, _ = out.getpixel((32, 32))
    # P3 red is more saturated than sRGB red, so the same numbers become redder and less green.
    assert red > color[0] + 5
    assert green < color[1] - 5
    assert "icc_profile" not in out.info


@pytest.mark.parametrize(
    "profile",
    [b"not a profile", bytes(300), SRGB_ICC[:200]],
    ids=["text", "zeros", "truncated-profile"],
)
def test_a_broken_profile_never_rejects_the_photo(profile: bytes) -> None:
    source = encode(solid((32, 32), (10, 120, 200)), "PNG", icc_profile=profile)
    out = decode(process_photo(source))
    assert near(out.getpixel((16, 16)), (10, 120, 200), 3)
    assert "icc_profile" not in out.info


def test_an_oversized_profile_is_ignored_not_parsed() -> None:
    profile = b"\xff" * (photo.MAX_ICC_BYTES + 1)
    source = encode(solid((32, 32), (10, 120, 200)), "JPEG", quality=98, icc_profile=profile)
    assert Image.open(BytesIO(source)).info["icc_profile"] == profile  # the input carries it
    out = decode(process_photo(source))
    assert near(out.getpixel((16, 16)), (10, 120, 200), 4)
    assert "icc_profile" not in out.info
    assert len(out.info) <= 4
