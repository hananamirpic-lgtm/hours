"""The company logo pipeline, the key shapes and the decode slot of the logo (task 30.5; R28.2, R28.6).

Every image is generated with Pillow in memory. Nothing here touches the database or the object store,
except the fake client the key and put tests use.

Decisions under test:

* An animated WebP or APNG is not rejected: its first frame becomes the logo. An animated GIF is
  rejected, because GIF is not an accepted format (`invalid_logo_type`).
* "At most 2,000 by 2,000 pixels" means at most 2,000 wide and at most 2,000 high.
* A truncated or corrupt file is rejected whatever `ImageFile.LOAD_TRUNCATED_IMAGES` is (WeasyPrint sets it
  to true for the whole process), and the module never changes a Pillow global.
"""

from __future__ import annotations

import struct
import threading
import time
import zlib
from http import HTTPStatus
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image, ImageCms, ImageFile
from PIL import features as pil_features
from PIL.PngImagePlugin import PngInfo

from app.service_tickets import logo, photo_store
from app.service_tickets.error_codes import (
    CODE_INVALID_LOGO_TYPE,
    CODE_LOGO_DIMENSIONS_TOO_LARGE,
    CODE_LOGO_TOO_LARGE,
    CODE_SERVICE_UNAVAILABLE,
)
from app.service_tickets.logo import (
    MAX_LOGO_BYTES,
    InvalidLogoType,
    LogoDimensionsTooLarge,
    LogoError,
    LogoTooLarge,
    process_logo,
)
from app.service_tickets.photo_store import (
    LOGO_KEY_PREFIX,
    PhotoStore,
    check_key,
    new_key,
    new_logo_key,
    process_logo_limited,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RED = (255, 0, 0)
BLUE = (0, 0, 255)


@pytest.fixture(params=[False, True], ids=["pillow-strict", "pillow-lenient"])
def truncated_flag(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Run with `ImageFile.LOAD_TRUNCATED_IMAGES` false, then true (what WeasyPrint leaves behind)."""
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", request.param)
    return bool(request.param)


# --- helpers -------------------------------------------------------------------------------------


def encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def decoded(result: logo.ProcessedLogo) -> Image.Image:
    image = Image.open(BytesIO(result.data))
    image.load()
    return image


def png_chunks(data: bytes) -> list[bytes]:
    """The chunk types of a PNG, in order."""
    assert data.startswith(PNG_SIGNATURE)
    kinds: list[bytes] = []
    position = len(PNG_SIGNATURE)
    while position < len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        kinds.append(kind)
        position += 12 + length
    assert position == len(data)
    return kinds


def png_header(width: int, height: int, *, depth: int = 1, colour: int = 0) -> bytes:
    """A PNG that is only a header and an end chunk: enough for Pillow to read the declared size."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def secret_exif(orientation: int | None = None) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "SECRET-MAKE"
    exif[0x0110] = "SECRET-MODEL"
    if orientation is not None:
        exif[0x0112] = orientation
    return exif


def left_half_red(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size, BLUE)
    image.paste(Image.new("RGB", (size[0] // 2, size[1]), RED), (0, 0))
    return image


# --- the accepted formats ------------------------------------------------------------------------


def test_a_jpeg_becomes_a_png() -> None:
    result = process_logo(encode(Image.new("RGB", (120, 60), RED), "JPEG"))

    image = decoded(result)
    assert result.data.startswith(PNG_SIGNATURE)
    assert (image.format, image.size) == ("PNG", (120, 60))
    assert (result.width, result.height, result.content_type) == (120, 60, "image/png")
    assert all(abs(a - b) <= 12 for a, b in zip(image.convert("RGB").getpixel((5, 5)), RED, strict=True))


def test_a_png_stays_a_png_and_its_transparency_is_kept() -> None:
    source = Image.new("RGBA", (100, 50), (255, 0, 0, 255))
    source.paste((0, 0, 0, 0), (0, 0, 50, 50))  # the left half is fully transparent

    image = decoded(process_logo(encode(source, "PNG")))

    assert image.mode == "RGBA"
    assert image.getpixel((10, 10))[3] == 0
    assert image.getpixel((90, 10)) == (255, 0, 0, 255)


def test_a_palette_png_with_transparency_keeps_it() -> None:
    source = Image.new("P", (40, 40), 0)
    source.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
    source.paste(1, (20, 0, 40, 40))
    source.info["transparency"] = 0

    image = decoded(process_logo(encode(source, "PNG", transparency=0)))

    assert image.mode == "RGBA"
    assert image.getpixel((5, 5))[3] == 0
    assert image.getpixel((30, 5)) == (0, 0, 255, 255)


def test_a_grey_alpha_png_keeps_its_alpha() -> None:
    source = Image.new("LA", (30, 30), (200, 0))

    image = decoded(process_logo(encode(source, "PNG")))

    assert image.mode == "RGBA"
    assert image.getpixel((1, 1))[3] == 0


def test_an_opaque_image_is_stored_without_an_alpha_channel() -> None:
    image = decoded(process_logo(encode(Image.new("RGB", (30, 30), RED), "PNG")))

    assert image.mode == "RGB"


def test_a_webp_becomes_a_png() -> None:
    if not pil_features.check("webp"):
        pytest.skip("Pillow was built without WebP")

    image = decoded(
        process_logo(encode(Image.new("RGBA", (64, 32), (0, 255, 0, 128)), "WEBP", lossless=True))
    )

    assert (image.format, image.size, image.mode) == ("PNG", (64, 32), "RGBA")
    assert image.getpixel((3, 3))[3] == 128


def test_an_animated_webp_takes_its_first_frame() -> None:
    if not pil_features.check("webp"):
        pytest.skip("Pillow was built without WebP")
    first = Image.new("RGB", (80, 40), RED)
    second = Image.new("RGB", (80, 40), BLUE)
    data = encode(first, "WEBP", save_all=True, append_images=[second], lossless=True, duration=100)
    if getattr(Image.open(BytesIO(data)), "n_frames", 1) != 2:
        pytest.skip("this Pillow build writes no animated WebP")

    image = decoded(process_logo(data))

    assert image.size == (80, 40)
    assert image.getpixel((5, 5))[:3] == RED


def test_an_animated_png_takes_its_first_frame() -> None:
    first = Image.new("RGB", (80, 40), RED)
    second = Image.new("RGB", (80, 40), BLUE)
    data = encode(first, "PNG", save_all=True, append_images=[second], duration=100)
    assert getattr(Image.open(BytesIO(data)), "n_frames", 1) == 2

    image = decoded(process_logo(data))

    assert image.size == (80, 40)
    assert image.getpixel((5, 5))[:3] == RED


def test_a_gif_is_rejected_even_when_it_is_animated() -> None:
    first = Image.new("P", (20, 20), 0)
    data = encode(first, "GIF", save_all=True, append_images=[Image.new("P", (20, 20), 1)], duration=50)

    with pytest.raises(InvalidLogoType):
        process_logo(data)


# --- size ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ((800, 400), (400, 200)),
        ((1000, 200), (400, 80)),
        ((200, 1000), (40, 200)),
        ((2000, 2000), (200, 200)),
        ((2000, 1), (400, 1)),
        ((401, 200), (400, 200)),
    ],
)
def test_the_output_fits_in_400_by_200_and_keeps_the_aspect_ratio(
    source: tuple[int, int], expected: tuple[int, int]
) -> None:
    result = process_logo(encode(Image.new("RGB", source, RED), "PNG"))

    assert (result.width, result.height) == expected
    assert decoded(result).size == expected
    assert result.width <= 400 and result.height <= 200


@pytest.mark.parametrize("source", [(100, 50), (400, 200), (1, 1), (399, 199)])
def test_a_small_image_is_not_enlarged(source: tuple[int, int]) -> None:
    result = process_logo(encode(Image.new("RGB", source, RED), "PNG"))

    assert (result.width, result.height) == source


def test_the_exif_orientation_is_applied() -> None:
    source = left_half_red((60, 30))
    data = encode(source, "JPEG", quality=95, exif=secret_exif(orientation=6))  # rotate 90 degrees clockwise

    image = decoded(process_logo(data))

    assert image.size == (30, 60)
    assert all(abs(a - b) <= 20 for a, b in zip(image.convert("RGB").getpixel((15, 5)), RED, strict=True))


def test_a_cmyk_jpeg_is_converted_to_rgb() -> None:
    image = decoded(process_logo(encode(Image.new("CMYK", (40, 20), (0, 255, 255, 0)), "JPEG")))

    assert image.mode == "RGB"
    assert image.size == (40, 20)


def test_a_16_bit_grey_png_is_converted() -> None:
    source = Image.new("I;16", (30, 30), 40_000)

    image = decoded(process_logo(encode(source, "PNG")))

    assert image.mode == "RGB"
    assert 130 <= image.getpixel((1, 1))[0] <= 170


# --- metadata ------------------------------------------------------------------------------------


def test_every_metadata_item_of_a_png_is_dropped() -> None:
    info = PngInfo()
    info.add_text("Author", "SECRET-AUTHOR")
    info.add_itxt("Comment", "SECRET-COMMENT")
    source = Image.new("RGB", (50, 50), RED)
    data = encode(source, "PNG", pnginfo=info, exif=secret_exif(), dpi=(300, 300))
    assert b"SECRET-AUTHOR" in data

    result = process_logo(data)

    assert set(png_chunks(result.data)) <= {b"IHDR", b"IDAT", b"IEND"}
    assert b"SECRET" not in result.data
    assert decoded(result).info == {}


def test_every_metadata_item_of_a_jpeg_is_dropped() -> None:
    data = encode(Image.new("RGB", (50, 50), RED), "JPEG", exif=secret_exif(), comment=b"SECRET-COMMENT")
    assert b"SECRET-MAKE" in data

    result = process_logo(data)

    assert b"SECRET" not in result.data
    assert b"Exif" not in result.data
    assert set(png_chunks(result.data)) <= {b"IHDR", b"IDAT", b"IEND"}


def test_the_icc_profile_does_not_survive() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    data = encode(Image.new("RGB", (40, 40), RED), "PNG", icc_profile=profile)
    assert b"iCCP" in png_chunks(data)

    result = process_logo(data)

    assert b"iCCP" not in png_chunks(result.data)
    assert "icc_profile" not in decoded(result).info


def test_bytes_after_the_image_never_reach_the_output() -> None:
    marker = b"PAYLOAD-BEHIND-THE-IMAGE"
    data = encode(Image.new("RGB", (40, 40), RED), "JPEG") + marker  # a JPEG tolerates a trailer

    result = process_logo(data)

    assert marker not in result.data


# --- every refusal -------------------------------------------------------------------------------


def test_more_than_one_mebibyte_is_logo_too_large_whatever_the_bytes() -> None:
    for payload in (b"x" * (MAX_LOGO_BYTES + 1), PNG_SIGNATURE + b"\x00" * MAX_LOGO_BYTES):
        with pytest.raises(LogoTooLarge) as info:
            process_logo(payload)
        assert (info.value.code, info.value.status) == (CODE_LOGO_TOO_LARGE, 413)


def test_exactly_one_mebibyte_is_not_too_large() -> None:
    with pytest.raises(InvalidLogoType):  # garbage of exactly the limit is judged by its content
        process_logo(b"x" * MAX_LOGO_BYTES)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"hello world",
        b"\x00" * 100,
        bytes(range(256)) * 8,
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF",
        b"<html><body>logo</body></html>",
        b"BM" + b"\x00" * 60,  # a BMP header
        b"II*\x00" + b"\x00" * 60,  # a TIFF header
        b"\xff\xd8\xff",  # only the start of a JPEG
        b"\x89PNG\r\n\x1a\n",  # only the PNG signature
        b"RIFF\x04\x00\x00\x00WEBP",
    ],
    ids=lambda value: repr(value[:12]),
)
def test_a_file_that_is_not_a_jpeg_png_or_webp_is_invalid_logo_type(payload: bytes) -> None:
    with pytest.raises(InvalidLogoType) as info:
        process_logo(payload)
    assert (info.value.code, info.value.status) == (CODE_INVALID_LOGO_TYPE, 422)


def test_a_png_with_a_trailer_is_invalid() -> None:
    data = encode(Image.new("RGB", (30, 30), RED), "PNG") + b"<script>alert(1)</script>"

    with pytest.raises(InvalidLogoType):
        process_logo(data)


def test_a_polyglot_that_starts_as_an_image_and_ends_as_text_is_rejected_or_cleaned() -> None:
    png = encode(Image.new("RGB", (30, 30), RED), "PNG")
    with pytest.raises(InvalidLogoType):
        process_logo(png + b"\n<?php system($_GET['c']); ?>")
    # A text file that only starts with a JPEG marker is not an image.
    with pytest.raises(InvalidLogoType):
        process_logo(b"\xff\xd8\xff\xe0<?php system($_GET['c']); ?>")


def test_a_truncated_png_is_rejected_whatever_the_pillow_flag(truncated_flag: bool) -> None:
    data = encode(Image.frombytes("RGB", (200, 200), bytes(range(256)) * 468 + b"\x00" * 192), "PNG")

    for cut in (len(data) // 2, len(data) - 20, len(data) - 1):
        with pytest.raises(InvalidLogoType):
            process_logo(data[:cut])


def test_a_truncated_jpeg_is_rejected_whatever_the_pillow_flag(truncated_flag: bool) -> None:
    data = encode(Image.frombytes("RGB", (200, 200), bytes(range(256)) * 468 + b"\x00" * 192), "JPEG")

    for cut in (len(data) // 2, len(data) - 30, len(data) - 1):
        with pytest.raises(InvalidLogoType):
            process_logo(data[:cut])


def test_a_truncated_webp_is_rejected_whatever_the_pillow_flag(truncated_flag: bool) -> None:
    if not pil_features.check("webp"):
        pytest.skip("Pillow was built without WebP")
    data = encode(
        Image.frombytes("RGB", (200, 200), bytes(range(256)) * 468 + b"\x00" * 192), "WEBP", lossless=True
    )

    for cut in (len(data) // 2, len(data) - 8, len(data) - 1):
        with pytest.raises(InvalidLogoType):
            process_logo(data[:cut])


def test_a_png_with_a_damaged_data_chunk_is_rejected(truncated_flag: bool) -> None:
    data = bytearray(
        encode(Image.frombytes("RGB", (100, 100), bytes(range(256)) * 117 + b"\x00" * 48), "PNG")
    )
    position = data.index(b"IDAT") + 20
    data[position] ^= 0xFF

    with pytest.raises(InvalidLogoType):
        process_logo(bytes(data))


@pytest.mark.parametrize("size", [(2001, 10), (10, 2001), (2001, 2001), (3000, 100), (100, 5000)])
def test_an_image_over_2000_pixels_a_side_is_logo_dimensions_too_large(size: tuple[int, int]) -> None:
    data = encode(Image.new("1", size, 1), "PNG")  # one bit a pixel: a few KB

    with pytest.raises(LogoDimensionsTooLarge) as info:
        process_logo(data)
    assert (info.value.code, info.value.status) == (CODE_LOGO_DIMENSIONS_TOO_LARGE, 422)


@pytest.mark.parametrize("size", [(2000, 2000), (2000, 1), (1, 2000)])
def test_an_image_of_exactly_2000_pixels_a_side_is_accepted(size: tuple[int, int]) -> None:
    result = process_logo(encode(Image.new("1", size, 1), "PNG"))

    assert result.width <= 400 and result.height <= 200


def test_a_header_that_claims_billions_of_pixels_is_refused_before_any_decoding() -> None:
    for width, height in ((100_000, 100_000), (2_000_000_000, 2_000_000_000), (2001, 1)):
        with pytest.raises(LogoDimensionsTooLarge):
            process_logo(png_header(width, height))


def test_the_jpeg_header_size_is_checked_before_decoding() -> None:
    data = encode(Image.new("L", (2500, 20), 128), "JPEG")

    with pytest.raises(LogoDimensionsTooLarge):
        process_logo(data)


def test_the_error_classes_carry_a_code_and_a_status() -> None:
    assert issubclass(LogoTooLarge, LogoError) and issubclass(InvalidLogoType, LogoError)
    assert [e.status for e in (InvalidLogoType, LogoDimensionsTooLarge, LogoTooLarge)] == [422, 422, 413]


def test_non_bytes_input_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        process_logo("text")  # type: ignore[arg-type]


def test_no_pillow_global_is_read_or_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated-image flag and the pixel limit stay exactly as they were, after success and failure."""
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 123_456_789)

    process_logo(encode(Image.new("RGB", (30, 30), RED), "PNG"))
    for bad in (b"junk", encode(Image.new("1", (3000, 10), 1), "PNG"), b"x" * (MAX_LOGO_BYTES + 1)):
        with pytest.raises(LogoError):
            process_logo(bad)

    assert ImageFile.LOAD_TRUNCATED_IMAGES is True
    assert Image.MAX_IMAGE_PIXELS == 123_456_789


def test_the_module_text_never_assigns_a_pillow_global() -> None:
    source = Path(logo.__file__).read_text(encoding="utf-8")

    assert "LOAD_TRUNCATED_IMAGES =" not in source
    assert "MAX_IMAGE_PIXELS =" not in source


# --- key shapes ----------------------------------------------------------------------------------

LOGO_KEY = f"{LOGO_KEY_PREFIX}{'a1' * 16}.png"


def test_the_logo_prefix_is_under_the_ticket_prefix() -> None:
    assert LOGO_KEY_PREFIX == "service-tickets/branding/"


def test_new_logo_key_is_unique_valid_and_has_the_logo_shape() -> None:
    keys = [new_logo_key() for _ in range(300)]

    assert len(set(keys)) == 300
    assert all(check_key(key) == key for key in keys)
    assert all(
        key.startswith("service-tickets/branding/") and key.endswith(".png") and len(key) == len(LOGO_KEY)
        for key in keys
    )
    assert all(key[len("service-tickets/branding/") : -4].isalnum() for key in keys)


@pytest.mark.parametrize(
    "key",
    [
        "service-tickets/abc.jpg",
        "service-tickets/" + "a" * 32 + ".jpg",
        LOGO_KEY,
        "service-tickets/branding/" + "0" * 32 + ".png",
    ],
)
def test_both_key_shapes_are_accepted(key: str) -> None:
    assert check_key(key) == key
    assert check_key(new_key()) and check_key(new_logo_key())


@pytest.mark.parametrize(
    "key",
    [
        # traversal and path tricks
        "service-tickets/branding/../" + "a" * 32 + ".png",
        "service-tickets/branding/../../x.jpg",
        "service-tickets/../branding/" + "a" * 32 + ".png",
        "service-tickets/branding//" + "a" * 32 + ".png",
        "service-tickets/./branding/" + "a" * 32 + ".png",
        "service-tickets/branding/" + "a" * 32 + ".png/",
        "service-tickets/branding/",
        "service-tickets/branding",
        "service-tickets/branding/.png",
        "service-tickets/branding/%2e%2e/" + "a" * 32 + ".png",
        # case
        "service-tickets/branding/" + "A" * 32 + ".png",
        "service-tickets/Branding/" + "a" * 32 + ".png",
        "Service-Tickets/branding/" + "a" * 32 + ".png",
        "service-tickets/ABC.jpg",
        # extension
        "service-tickets/branding/" + "a" * 32 + ".jpg",
        "service-tickets/branding/" + "a" * 32 + ".PNG",
        "service-tickets/branding/" + "a" * 32 + ".png.exe",
        "service-tickets/branding/" + "a" * 32,
        "service-tickets/" + "a" * 32 + ".png",
        "service-tickets/" + "a" * 32 + ".jpeg",
        "service-tickets/abc",
        # length and alphabet of the name
        "service-tickets/branding/" + "a" * 31 + ".png",
        "service-tickets/branding/" + "a" * 33 + ".png",
        "service-tickets/branding/" + "g" * 32 + ".png",
        "service-tickets/branding/"
        + "a" * 8
        + "-"
        + "a" * 4
        + "-"
        + "a" * 4
        + "-"
        + "a" * 4
        + "-"
        + "a" * 12
        + ".png",
        "service-tickets/" + "a" * 65 + ".jpg",
        # other sub-folders and depth
        "service-tickets/other/" + "a" * 32 + ".png",
        "service-tickets/branding/other/" + "a" * 32 + ".png",
        "service-tickets/sub/abc.jpg",
        # characters
        "service-tickets/branding/" + "a" * 32 + ".png\n",
        "service-tickets/branding/" + "a" * 32 + "\x00.png",
        "service-tickets/branding\\" + "a" * 32 + ".png",
        "service-tickets/branding/" + "a" * 32 + ".png ",
        # outside the store
        "branding/" + "a" * 32 + ".png",
        "/service-tickets/branding/" + "a" * 32 + ".png",
        "employees/1/logo.png",
        "",
        None,
        b"service-tickets/branding/" + b"a" * 32 + b".png",
    ],
)
def test_every_other_key_is_rejected(key: object) -> None:
    with pytest.raises(ValueError):
        check_key(key)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_object(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return {}


def test_a_logo_is_put_as_image_png_and_a_photo_as_image_jpeg() -> None:
    client = _FakeClient()
    store = PhotoStore(client, bucket="bucket")

    store.put(LOGO_KEY, b"png")
    store.put(new_key(), b"jpeg")

    assert [call["ContentType"] for call in client.calls] == ["image/png", "image/jpeg"]
    assert all("ACL" not in call for call in client.calls)


# --- the decode slot -----------------------------------------------------------------------------


def _free_slots() -> int:
    taken = 0
    try:
        while photo_store._DECODE_SLOTS.acquire(blocking=False):  # noqa: SLF001
            taken += 1
    finally:
        for _ in range(taken):
            photo_store._DECODE_SLOTS.release()  # noqa: SLF001
    return taken


def test_the_logo_decode_runs_in_a_slot_and_gives_it_back() -> None:
    assert _free_slots() == 3

    result = process_logo_limited(encode(Image.new("RGB", (30, 30), RED), "PNG"))
    with pytest.raises(InvalidLogoType):
        process_logo_limited(b"junk")

    assert result.width == 30
    assert _free_slots() == 3


def test_a_full_decode_queue_is_503_for_a_logo_too() -> None:
    held = 0
    try:
        for _ in range(3):
            assert photo_store._DECODE_SLOTS.acquire(blocking=False)  # noqa: SLF001
            held += 1
        started = time.monotonic()
        with pytest.raises(HTTPException) as info:
            process_logo_limited(b"x", wait_seconds=0.05)
        assert time.monotonic() - started < 2
    finally:
        for _ in range(held):
            photo_store._DECODE_SLOTS.release()  # noqa: SLF001

    assert info.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert info.value.detail == {"error": {"code": CODE_SERVICE_UNAVAILABLE}}
    assert _free_slots() == 3


def test_logo_decodes_run_at_most_three_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The slots are the photo pipeline's own pool, so photos and logos together stay within three."""
    running = 0
    peak = 0
    lock = threading.Lock()

    def slow(_data: bytes) -> logo.ProcessedLogo:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return logo.ProcessedLogo(data=b"", width=1, height=1)

    monkeypatch.setattr(photo_store, "process_logo", slow)
    threads = [threading.Thread(target=process_logo_limited, args=(b"x",)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert 1 <= peak <= 3
    assert _free_slots() == 3
