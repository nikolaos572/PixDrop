"""Image files: what they are, their medium-size version, and their form for the provider.

Uploaded reference images and generated images are recognised from their content, never
from their name: PNG, JPEG, WebP and GIF (decoded in full by Pillow, which refuses damaged
files) and SVG (read without entity declarations or external references, and drawn in a
helper process with limits: see svgdraw). Pictures above MAX_PIXELS are refused, as are
SVGs whose drawing fails.

The medium size is made when it is asked for and kept in a small memory cache: only the
full-size files are stored. Providers accept PNG, JPEG, WebP and GIF, so GIF and SVG
references are sent to them as PNG.
"""

from __future__ import annotations

import base64
import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree
from PIL import Image, ImageOps, UnidentifiedImageError

from . import svgdraw
from .errors import InvalidInput

MAX_PIXELS = 50_000_000  # width × height of a picture
SVG_RENDER_PX = 2048  # longest side of an SVG drawn for a provider
MEDIUM_CACHE_BYTES = 64 * 1024**2  # the medium sizes kept in memory
# Decoding a large picture takes up to 4 bytes per pixel (200 MB at 50 megapixels): at most this many at once.
_DECODING = threading.BoundedSemaphore(2)

# The image types accepted, with the extension of their files.
TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}
_PILLOW_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
_UNSUPPORTED = "unsupported image type: use PNG, JPEG, WebP, GIF or SVG"
_SVG_UNITS = {"": 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0}


@dataclass(frozen=True)
class ImageInfo:
    content_type: str
    width: int
    height: int

    @property
    def extension(self) -> str:
        return TYPES[self.content_type]


def inspect(data: bytes) -> ImageInfo:
    """What the image in `data` is; InvalidInput if it is not one of TYPES or is unusable."""
    if not data:
        raise InvalidInput("the file is empty")
    info = _raster_info(data)
    if info is None:
        info = _svg_info(data)
    if info is None:
        raise InvalidInput(_UNSUPPORTED)
    return info


def medium(path: Path, content_type: str, max_px: int) -> tuple[bytes, str] | None:
    """The image at `path` scaled down to fit `max_px` × `max_px`, and its type; None when
    the original fits already (raster images are never scaled up). GIF and SVG give a PNG.
    The result is remembered (MEDIUM_CACHE_BYTES), as the web page shows it again and again."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, content_type, max_px)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    with _DECODING:
        result = _medium(path, content_type, max_px)
    _remember(key, result)
    return result


def clear_cache() -> None:
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0


def medium_extension(content_type: str) -> str:
    """The extension of the medium size of an image of this type."""
    return ".png" if content_type in ("image/gif", "image/svg+xml") else TYPES[content_type]


def data_url(path: Path, content_type: str) -> str:
    """The image as a data: URL in a format providers accept (GIF and SVG become PNG)."""
    data = path.read_bytes()
    if content_type == "image/svg+xml":
        data, content_type = _render_svg(data, SVG_RENDER_PX), "image/png"
    elif content_type == "image/gif":
        with _DECODING, Image.open(io.BytesIO(data)) as image:
            data, content_type = _encode(_rgb_or_rgba(image), "PNG"), "image/png"
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


_cache: OrderedDict[tuple[object, ...], tuple[bytes, str] | None] = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def _remember(key: tuple[object, ...], result: tuple[bytes, str] | None) -> None:
    """Keep a medium size, forgetting the least recently used ones beyond MEDIUM_CACHE_BYTES."""
    global _cache_bytes
    size = len(result[0]) if result else 0
    with _cache_lock:
        if key in _cache or size > MEDIUM_CACHE_BYTES:
            return
        _cache[key] = result
        _cache_bytes += size
        while _cache_bytes > MEDIUM_CACHE_BYTES:
            _, dropped = _cache.popitem(last=False)
            _cache_bytes -= len(dropped[0]) if dropped else 0


def _medium(path: Path, content_type: str, max_px: int) -> tuple[bytes, str] | None:
    data = path.read_bytes()
    if content_type == "image/svg+xml":
        return _render_svg(data, max_px), "image/png"
    with Image.open(io.BytesIO(data)) as image:
        if max(image.size) <= max_px and content_type != "image/gif":
            return None
        # Scaled down first (which decodes JPEGs at a reduced size), then turned as the camera
        # says: no second copy of an RGB(A) picture at full size. Other modes (palettes, which
        # would be scaled without smoothing) are converted first.
        picture = _rgb_or_rgba(image)
        picture.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        picture = ImageOps.exif_transpose(picture)
        if content_type == "image/jpeg":
            return _encode(picture.convert("RGB"), "JPEG", quality=85), "image/jpeg"
        if content_type == "image/webp":
            return _encode(picture, "WEBP", quality=85), "image/webp"
        return _encode(picture, "PNG"), "image/png"  # PNG, and the first frame of a GIF


def _raster_info(data: bytes) -> ImageInfo | None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            content_type = _PILLOW_FORMATS.get(image.format or "")
            if content_type is None:
                raise InvalidInput(_UNSUPPORTED)
            width, height = image.size
            _check_pixels(width, height)
            with _DECODING:
                image.load()  # decodes the whole picture (the first frame of an animation)
    except UnidentifiedImageError:
        return None
    except InvalidInput:  # a ValueError too: the refusals above, which must keep their reason
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise InvalidInput("the image is damaged or cannot be read") from None
    return ImageInfo(content_type, width, height)


def _svg_info(data: bytes) -> ImageInfo | None:
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"<") or b"<svg" not in data[:65536].lower():
        return None
    width, height = _svg_size(data)
    _check_pixels(width, height)
    _render_svg(data, 64)  # draws it: refuses SVGs that CairoSVG cannot draw
    return ImageInfo("image/svg+xml", width, height)


def _svg_size(data: bytes) -> tuple[int, int]:
    """The SVG's own size in pixels: its width and height, else its viewBox, else 300 × 150
    (the browsers' default)."""
    try:
        root = ElementTree.fromstring(data)  # defusedxml: refuses entity declarations and external references
    except Exception:  # defusedxml refusals and XML syntax errors
        raise InvalidInput("the SVG file is not valid XML (or declares entities, which are refused)") from None
    if not root.tag.endswith("svg"):
        raise InvalidInput("the SVG file has no <svg> root element")
    box = [float(n) for n in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", root.get("viewBox", ""))]
    box_size = (box[2], box[3]) if len(box) == 4 and box[2] > 0 and box[3] > 0 else None
    width, height = _svg_length(root.get("width")), _svg_length(root.get("height"))
    if width and height:
        pass
    elif box_size and width:
        height = width * box_size[1] / box_size[0]
    elif box_size and height:
        width = height * box_size[0] / box_size[1]
    elif box_size:
        width, height = box_size
    else:
        width, height = 300.0, 150.0
    return max(1, round(width)), max(1, round(height))


def _svg_length(text: str | None) -> float | None:
    match = re.fullmatch(r"\s*(\d+\.?\d*|\.\d+)\s*(px|pt|pc|mm|cm|in)?\s*", text or "")
    if not match or float(match.group(1)) <= 0:
        return None  # missing, relative (%, em) or invalid: the viewBox decides
    return float(match.group(1)) * _SVG_UNITS[match.group(2) or ""]


def _render_svg(data: bytes, max_px: int) -> bytes:
    """The SVG drawn as a PNG that fits `max_px` × `max_px`, whatever size it declares, in a
    helper process with limits (svgdraw)."""
    width, height = _svg_size(data)
    scale = max_px / max(width, height)
    return svgdraw.draw(data, max(1, round(width * scale)), max(1, round(height * scale)), MAX_PIXELS)


def _check_pixels(width: int, height: int) -> None:
    if width * height > MAX_PIXELS:
        raise InvalidInput(f"the image is too large: {width} × {height} pixels (at most {MAX_PIXELS // 1_000_000} megapixels)")


def _rgb_or_rgba(image: Image.Image) -> Image.Image:
    if image.mode in ("RGB", "RGBA"):
        return image
    has_alpha = image.mode in ("LA", "PA", "La") or "transparency" in image.info
    return image.convert("RGBA" if has_alpha else "RGB")


def _encode(image: Image.Image, image_format: str, **options: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, image_format, **options)
    return buffer.getvalue()
