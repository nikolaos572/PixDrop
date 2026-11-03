"""Image files: recognised from their content, checked, scaled down, and prepared for the providers."""

import base64
import io
import struct
import zlib
from pathlib import Path

import pytest
from helpers import format_of, gif, jpeg, png, size_of, svg, webp
from PIL import Image

from pixdrop import images
from pixdrop.errors import InvalidInput
from pixdrop.images import ImageInfo, data_url, inspect, medium, medium_extension

DAMAGED = "the image is damaged or cannot be read"
UNSUPPORTED = "unsupported image type: use PNG, JPEG, WebP, GIF or SVG"


def declared_png(width: int, height: int) -> bytes:
    """A PNG whose header declares width × height pixels, with hardly any data behind it."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b"")


def encoded(image_format: str, size=(16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(buffer, image_format)
    return buffer.getvalue()


def write(tmp_path: Path, data: bytes, name: str = "image") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def decoded(url: str) -> tuple[str, bytes]:
    header, _, payload = url.partition(",")
    assert header.startswith("data:") and header.endswith(";base64")
    return header[5:-7], base64.b64decode(payload)


# --- recognising images ---


@pytest.mark.parametrize(
    ("data", "content_type", "size", "extension"),
    [
        (png(40, 30), "image/png", (40, 30), ".png"),
        (jpeg(40, 30), "image/jpeg", (40, 30), ".jpg"),
        (webp(40, 30), "image/webp", (40, 30), ".webp"),
        (gif(40, 30), "image/gif", (40, 30), ".gif"),
        (gif(40, 30, frames=3), "image/gif", (40, 30), ".gif"),
        (svg('width="40" height="30"'), "image/svg+xml", (40, 30), ".svg"),
    ],
)
def test_the_accepted_types(data, content_type, size, extension):
    info = inspect(data)
    assert info == ImageInfo(content_type, *size)
    assert info.extension == extension


def test_the_type_comes_from_the_content():
    assert inspect(jpeg()).content_type == "image/jpeg"  # whatever the file was called: inspect never sees the name
    assert images.TYPES == {
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg",
    }  # fmt: skip


@pytest.mark.parametrize("data", [b"hello, world", b"%PDF-1.7\n", b"\x00" * 100, b"<html><body>hi</body></html>", b"<?xml version='1.0'?><note/>"])
def test_other_files_are_refused(data):
    with pytest.raises(InvalidInput, match=f"^{UNSUPPORTED}$"):
        inspect(data)


def test_an_empty_file_is_refused():
    with pytest.raises(InvalidInput, match="^the file is empty$"):
        inspect(b"")


@pytest.mark.parametrize("image_format", ["BMP", "TIFF"])
def test_other_image_types_are_called_unsupported(image_format):
    with pytest.raises(InvalidInput, match=f"^{UNSUPPORTED}$"):
        inspect(encoded(image_format))


@pytest.mark.parametrize("image_format", ["BMP", "TIFF", "ICO", "PPM"])
def test_other_image_types_are_refused(image_format):
    with pytest.raises(InvalidInput):
        inspect(encoded(image_format))


@pytest.mark.parametrize(
    "data",
    [
        png(200, 200)[:-200],  # cut off
        jpeg(200, 200)[:300],
        gif(200, 200)[:-60],
        webp(200, 200)[:60],
    ],
    ids=["png", "jpeg", "gif", "webp"],
)
def test_damaged_images_are_refused(data):
    with pytest.raises(InvalidInput, match=f"^{DAMAGED}$"):
        inspect(data)


def test_a_png_with_a_broken_header_is_refused():
    data = png()[:29] + b"\0\0\0\0" + png()[33:]  # the header's checksum
    with pytest.raises(InvalidInput):
        inspect(data)


def test_pictures_of_up_to_50_megapixels_are_accepted():
    buffer = io.BytesIO()
    Image.new("1", (10_000, 5_000)).save(buffer, "PNG")  # one bit per pixel: quick to make and to decode
    assert inspect(buffer.getvalue()) == ImageInfo("image/png", 10_000, 5_000)


def test_every_picture_is_decoded_in_full():
    with pytest.raises(InvalidInput, match=f"^{DAMAGED}$"):
        inspect(declared_png(1000, 1000))  # a valid header, but not the pixels it announces


def test_pictures_above_50_megapixels_are_refused_as_too_large():
    with pytest.raises(InvalidInput, match="too large: 8000 × 8000 pixels \\(at most 50 megapixels\\)"):
        inspect(declared_png(8000, 8000))


@pytest.mark.parametrize("size", [(8000, 8000), (50_001, 1000), (20_000, 20_000), (65_535, 65_535)])
def test_pictures_above_50_megapixels_are_refused(size):
    with pytest.raises(InvalidInput):  # without decoding them
        inspect(declared_png(*size))


# --- SVG ---


@pytest.mark.parametrize(
    ("attributes", "size"),
    [
        ('width="64" height="32"', (64, 32)),
        ('width="64px" height="32px"', (64, 32)),
        ('width="1in" height="0.5in"', (96, 48)),
        ('width="12pt" height="3pc"', (16, 48)),
        ('width="10mm" height="1cm"', (38, 38)),
        ('viewBox="0 0 400 200"', (400, 200)),
        ('viewBox="0,0,400,200"', (400, 200)),
        ('width="100" viewBox="0 0 400 200"', (100, 50)),
        ('height="100" viewBox="0 0 400 200"', (200, 100)),
        ('width="100%" height="100%" viewBox="0 0 30 20"', (30, 20)),
        ('width="2em" height="1em"', (300, 150)),
        ("", (300, 150)),  # the browsers' default
        ('viewBox="0 0 -5 10"', (300, 150)),
    ],
)
def test_the_size_of_an_svg(attributes, size):
    info = inspect(svg(attributes))
    assert (info.content_type, info.width, info.height) == ("image/svg+xml", *size)


def test_an_svg_may_start_with_a_byte_order_mark_an_xml_declaration_and_comments():
    data = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\n<!-- drawn by hand -->\n' + svg()
    assert inspect(data).content_type == "image/svg+xml"
    assert inspect(b"\n  \t" + svg()).content_type == "image/svg+xml"  # or with white space


@pytest.mark.parametrize(
    "data",
    [
        # Entities, which can expand to gigabytes ("billion laughs") or read files (XXE), are refused.
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>&b;</text></svg>',
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>&x;</text></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect></svg>',  # not XML
    ],
    ids=["entities", "external-entity", "broken"],
)
def test_svgs_that_are_not_safe_xml_are_refused(data):
    with pytest.raises(InvalidInput, match="^the SVG file is not valid XML \\(or declares entities, which are refused\\)$"):
        inspect(data)


def test_an_svg_must_be_an_svg_document():
    with pytest.raises(InvalidInput, match="^the SVG file has no <svg> root element$"):
        inspect(b'<html><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/></html>')


def test_an_svg_declaring_more_than_50_megapixels_is_refused():
    with pytest.raises(InvalidInput, match="^the image is too large: 100000 × 100000 pixels \\(at most 50 megapixels\\)$"):
        inspect(svg('width="100000" height="100000"'))


def test_an_svg_cannot_smuggle_in_a_larger_picture(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 100 * 100)  # the same rule, at a size quick to test
    inner = base64.b64encode(png(200, 200)).decode()  # refused on its own: 40,000 pixels
    data = svg('width="10" height="10"', f'<image width="10" height="10" href="data:image/png;base64,{inner}"/>')
    with pytest.raises(InvalidInput):
        inspect(data)


def test_an_svg_may_embed_pictures_within_the_limit():
    inner = base64.b64encode(png(20, 20)).decode()
    data = svg('width="10" height="10"', f'<image width="10" height="10" href="data:image/png;base64,{inner}"/>')
    assert inspect(data) == ImageInfo("image/svg+xml", 10, 10)


def test_a_large_svg_is_drawn_within_bounds(tmp_path):
    data = svg('width="6000" height="4000"')  # 24 megapixels: accepted, but never drawn at that size
    assert (inspect(data).width, inspect(data).height) == (6000, 4000)
    content_type, picture = decoded(data_url(write(tmp_path, data), "image/svg+xml"))
    assert (content_type, format_of(picture), size_of(picture)) == ("image/png", "PNG", (2048, 1365))
    scaled, scaled_type = medium(write(tmp_path, data, "big.svg"), "image/svg+xml", 512)
    assert (scaled_type, size_of(scaled)) == ("image/png", (512, 341))


def test_an_svg_cannot_read_local_files(tmp_path):
    secret = write(tmp_path, png(64, 64, (255, 0, 0, 255)), "secret.png")
    data = svg(
        'width="64" height="64" xmlns:xlink="http://www.w3.org/1999/xlink"',
        f'<image href="file://{secret}" xlink:href="file://{secret}" width="64" height="64"/>',
    )
    try:
        inspect(data)
    except InvalidInput:
        return  # refused: as good
    _, picture = decoded(data_url(write(tmp_path, data, "drawing.svg"), "image/svg+xml"))
    with Image.open(io.BytesIO(picture)) as image:
        colours = {colour for _, colour in image.convert("RGBA").getcolors(1 << 24)}
    assert (255, 0, 0, 255) not in colours


# --- the medium size ---


@pytest.mark.parametrize(
    ("data", "content_type", "image_format"),
    [(png(1024, 768), "image/png", "PNG"), (jpeg(1024, 768), "image/jpeg", "JPEG"), (webp(1024, 768), "image/webp", "WEBP")],
)
def test_medium_keeps_the_format_and_the_proportions(tmp_path, data, content_type, image_format):
    scaled, scaled_type = medium(write(tmp_path, data), content_type, 512)
    assert (scaled_type, format_of(scaled), size_of(scaled)) == (content_type, image_format, (512, 384))


@pytest.mark.parametrize(("size", "content_type"), [((512, 300), "image/png"), ((100, 100), "image/png"), ((300, 512), "image/jpeg")])
def test_medium_never_scales_up(tmp_path, size, content_type):
    data = png(*size) if content_type == "image/png" else jpeg(*size)
    assert medium(write(tmp_path, data), content_type, 512) is None  # the full size serves


def test_medium_of_a_gif_is_a_png_of_its_first_frame(tmp_path):
    small, small_type = medium(write(tmp_path, gif(100, 50)), "image/gif", 512)  # even when it fits
    assert (small_type, format_of(small), size_of(small)) == ("image/png", "PNG", (100, 50))
    animated, _ = medium(write(tmp_path, gif(2000, 1000, frames=3), "animated.gif"), "image/gif", 512)
    assert (format_of(animated), size_of(animated)) == ("PNG", (512, 256))


def test_medium_of_an_svg_is_a_png_of_the_medium_size(tmp_path):
    scaled, scaled_type = medium(write(tmp_path, svg('width="100" height="50"')), "image/svg+xml", 512)
    assert (scaled_type, size_of(scaled)) == ("image/png", (512, 256))  # vector images draw well at any size


def test_medium_keeps_transparency(tmp_path):
    scaled, _ = medium(write(tmp_path, png(1000, 1000, (0, 0, 0, 0))), "image/png", 100)
    with Image.open(io.BytesIO(scaled)) as image:
        assert image.mode == "RGBA" and image.getpixel((50, 50))[3] == 0
    transparent_gif, _ = medium(write(tmp_path, gif(50, 50, transparent=True), "t.gif"), "image/gif", 100)
    with Image.open(io.BytesIO(transparent_gif)) as image:
        assert image.mode == "RGBA" and image.getpixel((25, 25))[3] == 0


def test_medium_follows_the_photos_orientation(tmp_path):
    scaled, _ = medium(write(tmp_path, jpeg(1000, 500, orientation=6)), "image/jpeg", 512)  # taken sideways
    assert size_of(scaled) == (256, 512)


def test_medium_sizes_are_cached_until_the_file_changes(tmp_path):
    path = write(tmp_path, png(1024, 1024))
    first = medium(path, "image/png", 512)
    assert medium(path, "image/png", 512) is first  # the very same bytes: not made again
    path.write_bytes(png(2048, 1024))
    assert size_of(medium(path, "image/png", 512)[0]) == (512, 256)
    assert size_of(medium(path, "image/png", 256)[0]) == (256, 128)  # one entry per size, too


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [("image/png", ".png"), ("image/jpeg", ".jpg"), ("image/webp", ".webp"), ("image/gif", ".png"), ("image/svg+xml", ".png")],
)
def test_medium_extension(content_type, extension):
    assert medium_extension(content_type) == extension


# --- data: URLs for the providers ---


@pytest.mark.parametrize(("data", "content_type"), [(png(), "image/png"), (jpeg(), "image/jpeg"), (webp(), "image/webp")])
def test_data_urls_carry_the_file_as_it_is(tmp_path, data, content_type):
    assert decoded(data_url(write(tmp_path, data), content_type)) == (content_type, data)


def test_gifs_and_svgs_are_sent_as_png(tmp_path):
    content_type, picture = decoded(data_url(write(tmp_path, gif(30, 20, transparent=True)), "image/gif"))
    assert (content_type, format_of(picture), size_of(picture)) == ("image/png", "PNG", (30, 20))
    content_type, picture = decoded(data_url(write(tmp_path, svg('width="64" height="32"'), "s.svg"), "image/svg+xml"))
    assert (content_type, format_of(picture), size_of(picture)) == ("image/png", "PNG", (2048, 1024))


def test_an_svg_that_takes_too_long_to_draw_is_refused(monkeypatch):
    # Each level multiplies the shapes by 16: CairoSVG would work on it for many seconds.
    levels = ['<g id="a0"><rect width="1" height="1"/></g>']
    for level in range(1, 5):
        levels.append(f'<g id="a{level}">' + "".join(f'<use href="#a{level - 1}"/>' for _ in range(16)) + "</g>")
    bomb = svg('width="64" height="64"', "<defs>" + "".join(levels) + '</defs><use href="#a4"/>')
    monkeypatch.setattr(images.svgdraw, "DRAW_SECONDS", 1)
    with pytest.raises(InvalidInput, match="^the SVG file takes too long to draw"):
        inspect(bomb)


def test_the_medium_cache_is_bounded_in_bytes(tmp_path, monkeypatch):
    first, second = write(tmp_path, png(1024, 1024), "a.png"), write(tmp_path, png(1024, 1024, (0, 0, 255, 255)), "b.png")
    kept = medium(first, "image/png", 512)
    monkeypatch.setattr(images, "MEDIUM_CACHE_BYTES", len(kept[0]) + 10)  # room for one
    images.clear_cache()
    assert medium(first, "image/png", 512) is not kept  # made again after clearing
    again = medium(first, "image/png", 512)
    medium(second, "image/png", 512)  # pushes the first out
    assert medium(first, "image/png", 512) is not again
