"""A fake OpenRouter for development and tests: python tests/fake_provider.py [--port 9000]

It answers POST /api/v1/images and POST /api/v1/chat/completions as OpenRouter does, with
a picture drawn from the prompt (a colour of its own and the prompt's first words; an SVG
for output_format svg), and lists its models with their parameters at
GET /api/v1/images/models (CATALOGUE), so PixDrop can be run and tried without a key and
without cost. GET /api/v1/key accepts any key but REJECTED_KEY, and says that MANAGEMENT_KEY
is a management key, which OpenRouter does not let generate. Point a provider at it:

    {"providers": {"fake": {"base_url": "http://127.0.0.1:9000/api/v1", "api_key": "anything"}}}

Words in the prompt change its behaviour: FAIL (an error status), FAIL200 (an error in an
answer with status 200), NOIMAGE (an answer without an image), SLOW (answers after 5 s),
HANG (never answers in time: 600 s), BADIMAGE (an image that is not one). The Docker
smoke tests also use it, through make_server().
"""

from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import io
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from PIL import Image, ImageDraw

COST = 0.04  # USD per image, as OpenRouter would report it
REJECTED_KEY = "rejected-key"
MANAGEMENT_KEY = "management-key"

# The models, with their parameters, as OpenRouter's GET /images/models lists them.
CATALOGUE: dict[str, Any] = {
    "data": [
        {
            "id": "fake/images",
            "name": "Fake Images",
            "description": "A fake model of the Images API.",
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]},
            "supported_parameters": {
                "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9", "9:16", "auto"]},
                "quality": {"type": "enum", "values": ["low", "medium", "high"]},
                "background": {"type": "enum", "values": ["auto", "transparent", "opaque"]},
                "output_format": {"type": "enum", "values": ["png", "jpeg", "webp", "svg"]},
                "output_compression": {"type": "range", "min": 0, "max": 100},
                "n": {"type": "range", "min": 1, "max": 4},
                "seed": {"type": "boolean"},
                "input_references": {"type": "range", "min": 0, "max": 4},
                "style": {"type": "enum", "values": ["photo", "sketch"]},  # a parameter PixDrop does not know
            },
        },
        {
            "id": "fake/chat",
            "name": "Fake Chat",
            "description": "A fake model that draws through chat completions.",
            "architecture": {"input_modalities": ["image", "text"], "output_modalities": ["image", "text"]},
            "supported_parameters": {
                "resolution": {"type": "enum", "values": ["1K", "2K"]},
                "aspect_ratio": {"type": "enum", "values": ["1:1", "4:3", "3:4"]},
                "n": {"type": "range", "min": 1, "max": 1},
                "input_references": {"type": "range", "min": 0, "max": 3},
            },
        },
        {
            "id": "fake/editor",
            "name": "Fake Editor",
            "description": "A fake model that only edits images: it needs a reference image.",
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]},
            "supported_parameters": {"input_references": {"type": "range", "min": 1, "max": 2}},
        },
    ]
}


@functools.lru_cache(maxsize=256)
def picture(prompt: str, size: tuple[int, int] = (1024, 1024), output_format: str = "png", transparent: bool = False) -> bytes:
    """An image drawn from `prompt`: the same prompt always gives the same picture (remembered)."""
    digest = hashlib.sha256(prompt.encode()).digest()
    colour = (digest[0], digest[1], digest[2], 255)
    image = Image.new("RGBA", size, (0, 0, 0, 0) if transparent else colour)
    draw = ImageDraw.Draw(image)
    margin = size[0] // 8
    draw.ellipse((margin, margin, size[0] - margin, size[1] - margin), fill=colour, outline=(255, 255, 255, 255), width=8)
    draw.text((margin, size[1] // 2), " ".join(prompt.split()[:6])[:60], fill=(255, 255, 255, 255))
    buffer = io.BytesIO()
    if output_format == "jpeg":
        image.convert("RGB").save(buffer, "JPEG", quality=90)
    elif output_format == "webp":
        image.save(buffer, "WEBP")
    else:
        image.save(buffer, "PNG")
    return buffer.getvalue()


def vector(prompt: str) -> bytes:
    """An SVG drawn from `prompt` (output_format svg)."""
    colour = hashlib.sha256(prompt.encode()).hexdigest()[:6]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 64 64">'
            f'<circle cx="32" cy="32" r="28" fill="#{colour}"/></svg>').encode()  # fmt: skip


def _size(body: dict[str, Any]) -> tuple[int, int]:
    size = str(body.get("size") or "")
    if "x" in size:
        width, height = (int(part) for part in size.split("x"))
        return min(width, 2048), min(height, 2048)
    ratio = str(body.get("aspect_ratio") or (body.get("image_config") or {}).get("aspect_ratio") or "1:1")
    if ":" in ratio and ratio != "auto":
        width, height = (float(part) for part in ratio.split(":"))
        return (1024, round(1024 * height / width)) if width >= height else (round(1024 * width / height), 1024)
    return 1024, 1024


def answer(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """The status and JSON answer to a request of PixDrop."""
    if path.endswith("/images"):
        prompt = str(body.get("prompt", ""))
    else:
        content = body["messages"][-1]["content"]
        prompt = content if isinstance(content, str) else " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    if "HANG" in prompt:
        time.sleep(600)
    if "SLOW" in prompt:
        time.sleep(5)
    if "FAIL200" in prompt:
        return 200, {"error": {"code": 502, "message": "Provider returned error", "metadata": {"provider_name": "Fake", "raw": "upstream exploded"}}}
    if "FAIL" in prompt:
        return 402, {"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"}}
    references = body.get("input_references") or []
    if not path.endswith("/images") and isinstance(body["messages"][-1]["content"], list):
        references = [part for part in body["messages"][-1]["content"] if part.get("type") == "image_url"]
    count = int(body.get("n") or 1) if path.endswith("/images") else 1
    output_format = str(body.get("output_format") or "png")
    transparent = body.get("background") == "transparent"
    images = [] if "NOIMAGE" in prompt else [
        b"not an image" if "BADIMAGE" in prompt
        else vector(f"{prompt} #{index}") if output_format == "svg"
        else picture(f"{prompt} #{index} refs={len(references)}", _size(body), output_format, transparent)
        for index in range(count)
    ]  # fmt: skip
    media_type = {"jpeg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}.get(output_format, "image/png")
    usage = {"prompt_tokens": 12 + 300 * len(references), "completion_tokens": 1290 * len(images), "cost": round(COST * len(images), 6)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    if path.endswith("/images"):
        return 200, {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(i).decode(), "media_type": media_type} for i in images], "usage": usage}
    message: dict[str, Any] = {"role": "assistant", "content": "Here is your image." if images else "I cannot draw that."}
    message["images"] = [{"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{base64.b64encode(i).decode()}"}} for i in images]
    return 200, {"id": f"gen-fake-{time.time_ns()}", "choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (the standard library's name)
        if self.path == "/api/v1/key":
            if self.headers.get("Authorization", "") in ("", f"Bearer {REJECTED_KEY}"):
                self._send(401, {"error": {"code": 401, "message": "User not found."}})
            else:
                management = self.headers["Authorization"] == f"Bearer {MANAGEMENT_KEY}"
                self._send(200, {"data": {"label": "fake", "is_management_key": management, "is_provisioning_key": False}})
            return
        if self.path != "/api/v1/images/models":
            self._send(404, {"error": {"code": 404, "message": f"no such endpoint {self.path}"}})
            return
        self._send(200, CATALOGUE)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/api/v1/images", "/api/v1/chat/completions"):
            self._send(404, {"error": {"code": 404, "message": f"no such endpoint {self.path}"}})
            return
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send(401, {"error": {"code": 401, "message": "No auth credentials found"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self._send(*answer(self.path, body))

    def _send(self, status: int, document: dict[str, Any]) -> None:
        payload = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def make_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """A fake provider listening on (host, port); port 0 picks a free one. Run serve_forever()."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    arguments = parser.parse_args()
    print(f"fake provider at http://{arguments.host}:{arguments.port}/api/v1")
    make_server(arguments.host, arguments.port).serve_forever()
