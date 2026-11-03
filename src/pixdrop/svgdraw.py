"""Drawing SVG files, in a helper process with limits on its time and memory.

An SVG is a program of sorts: a file of a kilobyte can ask for millions of shapes (nested
<use> elements) or wrap a picture of hundreds of megapixels in a data: URL. So it is drawn
by `python -m pixdrop.svgdraw` in a process of its own, as TeXdrop unpacks archives: it
gets at most DRAW_SECONDS of CPU time and DRAW_MEMORY_BYTES of memory, and is killed after
DRAW_SECONDS of waiting, while the server goes on undisturbed. Two helpers run at most at
a time; further drawings wait for their turn.

In the helper, CairoSVG reads no file and fetches nothing but data: URLs, and refuses
embedded pictures above the pixel limit before decoding them.
"""

from __future__ import annotations

import io
import resource
import subprocess
import sys
import threading

from .errors import InvalidInput

DRAW_SECONDS = 10  # real SVGs take well under a second
DRAW_MEMORY_BYTES = 2 * 1024**3  # address space, more than the Python interpreter and cairo need
_REFUSED = 3  # the helper's exit status when it refuses the SVG, with the reason on stderr
_DRAWING = threading.BoundedSemaphore(2)  # helpers at once: many uploads at a time cannot take every core


def draw(data: bytes, width: int, height: int, max_pixels: int) -> bytes:
    """The SVG drawn as a PNG of `width` × `height`; InvalidInput if it cannot be drawn
    within the limits, or embeds a picture above `max_pixels`."""
    command = [sys.executable, "-m", "pixdrop.svgdraw", str(width), str(height), str(max_pixels)]
    try:
        with _DRAWING:
            done = subprocess.run(command, input=data, capture_output=True, timeout=DRAW_SECONDS)
    except subprocess.TimeoutExpired:
        raise InvalidInput(f"the SVG file takes too long to draw (more than {DRAW_SECONDS} seconds)") from None
    if done.returncode == 0 and done.stdout.startswith(b"\x89PNG"):
        return done.stdout
    if done.returncode == _REFUSED:
        raise InvalidInput(done.stderr.decode("utf-8", "replace").strip())
    if done.returncode < 0 or b"MemoryError" in done.stderr:  # killed: CPU time (SIGXCPU/SIGKILL) or memory
        raise InvalidInput("the SVG file cannot be drawn within the limits of time and memory")
    raise InvalidInput("the SVG file cannot be drawn")


def _limit_self() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (DRAW_SECONDS, DRAW_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_AS, (DRAW_MEMORY_BYTES, DRAW_MEMORY_BYTES))


def _main(width: int, height: int, max_pixels: int) -> int:
    _limit_self()  # before anything of the SVG is read
    from cairosvg.surface import PNGSurface  # after the limits: importing takes memory too
    from cairosvg.url import fetch
    from PIL import Image

    def fetch_data_only(url: str, resource_type: str) -> bytes:
        if not url.startswith("data:"):
            return b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'  # files and the network: nothing
        content = fetch(url, resource_type)
        if not content.lstrip().startswith(b"<"):  # a picture rather than an SVG: check its size first
            try:
                with Image.open(io.BytesIO(content)) as picture:  # reads the header only
                    pixels = picture.width * picture.height
            except Exception:
                return content  # not something Pillow knows; cairo decides
            if pixels > max_pixels:
                raise InvalidInput(
                    f"the SVG file embeds a picture that is too large: {picture.width} × {picture.height} pixels "
                    f"(at most {max_pixels // 1_000_000} megapixels)"
                )
        return content

    data = sys.stdin.buffer.read()
    try:
        # PNGSurface.convert is svg2png, with the url_fetcher it passes on to the parser.
        png = PNGSurface.convert(
            bytestring=data, output_width=width, output_height=height, unsafe=False, url_fetcher=fetch_data_only
        )
    except InvalidInput as exc:
        print(exc, file=sys.stderr)
        return _REFUSED
    sys.stdout.buffer.write(png)
    return 0


if __name__ == "__main__":
    sys.exit(_main(*(int(argument) for argument in sys.argv[1:4])))
