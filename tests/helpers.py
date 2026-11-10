"""Test helpers shared by several test modules (fixtures are in conftest.py)."""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import fake_provider
import httpx2
from fastapi.testclient import TestClient
from PIL import Image

from pixdrop import logs
from pixdrop.storage import Storage
from pixdrop.store import JobStore, Status

TOKENS = {"alice": "alice-token-0123456789", "bob": "bob-token-9876543210"}
ALICE = {"Authorization": f"Bearer {TOKENS['alice']}"}
BOB = {"Authorization": f"Bearer {TOKENS['bob']}"}
API_KEY = "sk-or-test-key-0123456789"
BASE_URL = "https://openrouter.test/api/v1"

# The providers and models of the tests: one model per api_type, and one without options or references.
CONFIG: dict[str, Any] = {
    "providers": {
        "openrouter": {"base_url": BASE_URL, "api_key": API_KEY, "headers": {"X-OpenRouter-Title": "PixDrop"}},
    },
    "default_model": "gpt-image",
    "models": {
        "gpt-image": {
            "provider": "openrouter",
            "model": "openai/gpt-5-image",
            "api_type": "images",
            "description": "OpenAI's image model",
            "options": ["aspect_ratio", "size", "quality", "background", "output_format", "n"],
            "defaults": {"quality": "high"},
        },
        "nano-banana": {
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash-image",
            "api_type": "chat",
            "options": ["aspect_ratio", "resolution", "seed"],
            "max_reference_images": 3,
        },
        "plain": {"provider": "openrouter", "model": "example/plain", "api_type": "images", "max_reference_images": 0},
    },
}

# The models of CONFIG that the fake catalogue lists too: "plain" relies on it for its options (none).
CATALOGUE_EXTRA: list[dict[str, Any]] = [
    {"id": "example/plain", "name": "Plain", "supported_parameters": {}},
    {"id": "openai/gpt-5-image", "name": "GPT-5 Image", "supported_parameters": {"input_references": {"type": "range", "min": 0, "max": 16}}},
]

# The fields of a job record, in their order in the file (the specification's).
RECORD_FIELDS = [
    "id", "user", "status", "created_at", "started_at", "finished_at", "duration_seconds", "source",
    "client_ip", "user_agent", "prompt", "model", "provider", "upstream_model", "api_type", "options",
    "reference_count", "references", "images", "text", "usage", "upstream_id", "error",
]  # fmt: skip
JOB_ID = re.compile(r"\d{13}_[0-9a-f]{4}")

_mtimes = itertools.count(1)


# --- files the administrator edits ---


def write_json(path: Path, document: object) -> Path:
    """Write a JSON file as the administrator would, with a new modification time each time, so that
    the server notices the change even when the size stays the same and the clock is coarse."""
    path.write_text(json.dumps(document), encoding="utf-8")
    touch(path)
    return path


def touch(path: Path) -> None:
    stamp = next(_mtimes) * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def config_with(**models: dict[str, Any] | None) -> dict[str, Any]:
    """CONFIG with some models replaced, added, or (None) removed."""
    config = json.loads(json.dumps(CONFIG))
    for name, model in models.items():
        if model is None:
            config["models"].pop(name)
        else:
            config["models"][name] = model
    if config.get("default_model") not in config["models"]:
        config.pop("default_model", None)
    return config


# --- images ---


def png(width: int = 64, height: int = 64, colour: tuple[int, ...] = (200, 30, 30, 255), mode: str = "RGBA") -> bytes:
    return _encode(Image.new(mode, (width, height), colour[: len(mode)] if mode != "L" else colour[0]), "PNG")


def jpeg(width: int = 64, height: int = 64, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), (30, 120, 200))
    if orientation is None:
        return _encode(image, "JPEG")
    exif = Image.Exif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def webp(width: int = 64, height: int = 64) -> bytes:
    return _encode(Image.new("RGB", (width, height), (30, 200, 60)), "WEBP")


def gif(width: int = 64, height: int = 64, frames: int = 1, transparent: bool = False) -> bytes:
    pictures = [Image.new("P", (width, height), index + 1) for index in range(frames)]
    options: dict[str, Any] = {"save_all": True, "append_images": pictures[1:]} if frames > 1 else {}
    if transparent:
        options["transparency"] = 1
    buffer = io.BytesIO()
    pictures[0].save(buffer, "GIF", **options)
    return buffer.getvalue()


def svg(attributes: str = 'width="64" height="64"', body: str = '<rect width="100%" height="100%" fill="#0a0"/>') -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attributes}>{body}</svg>'.encode()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def format_of(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as image:
        return image.format or ""


def _encode(image: Image.Image, image_format: str) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, image_format)
    return buffer.getvalue()


def reference(name: str, data: bytes, content_type: str = "application/octet-stream") -> tuple[str, tuple[str, bytes, str]]:
    """A multipart field `reference` with a file, for the `files` argument of a request."""
    return ("reference", (name, data, content_type))


# --- the providers ---


def prompt_of(body: dict[str, Any]) -> str:
    """The prompt of a request to a provider, of either api_type."""
    if "prompt" in body:
        return str(body["prompt"])
    content = body["messages"][-1]["content"]
    return content if isinstance(content, str) else " ".join(p.get("text", "") for p in content if p.get("type") == "text")


class FakeUpstream:
    """The providers, behind the app's httpx2.MockTransport. It answers as tests/fake_provider.py does
    (the prompt's words FAIL, FAIL200, NOIMAGE and BADIMAGE pick a failure), records every request,
    and gives the answers queued in `answers` first. HANG never answers (without blocking the event
    loop, as the fake's own sleep would); GATE answers once `gate` is set by the test."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.answers: list[httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]] = []
        self.files: dict[str, httpx2.Response] = {}  # images that answers link to, by URL
        self.gate = _Gate()

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/images/models"):
            return httpx2.Response(200, json=fake_provider.CATALOGUE | {"data": fake_provider.CATALOGUE["data"] + CATALOGUE_EXTRA})
        if request.method == "GET":
            return self.files.get(str(request.url)) or httpx2.Response(404, text="not found")
        body = json.loads(request.content)
        prompt = prompt_of(body)
        if "HANG" in prompt:
            await asyncio.sleep(3600)
        if "GATE" in prompt:
            while not self.gate.is_set():
                await asyncio.sleep(0.005)
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer(request) if callable(answer) else answer
        status, document = fake_provider.answer(request.url.path, body)
        return httpx2.Response(status, json=document)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        """The JSON bodies of the generation requests, in order."""
        return [json.loads(request.content) for request in self.requests if request.method == "POST"]


class _Gate:
    """Set from the test's thread, read from the app's event loop."""

    def __init__(self) -> None:
        self._set = False

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


# --- clients ---


def share(running: TestClient, **options: Any) -> TestClient:
    """Another client of the app that `running` runs (a client entered with `with`). Its requests
    reach the same event loop, where the generations run: without that, a job made through it would
    be started in an event loop of its own, which ends with the request."""
    if "headers" in options:
        options["headers"] = dict(options["headers"])  # TestClient adds its User-Agent to the dict it is given
    other = TestClient(running.app, **options)
    other.portal = running.portal
    return other


def login(running: TestClient, user: str = "alice") -> str:
    """Log a browser in as `user`, as the web page does: the value of its session cookie."""
    response = share(running).post("/api/session", json={"token": TOKENS[user]})
    assert response.status_code == 200, response.text
    return response.cookies["pixdrop_session"]


def browser_of(running: TestClient, user: str = "alice", **options: Any) -> TestClient:
    """Another client of the app that `running` runs, logged in as `user` through the web page."""
    return share(running, cookies={"pixdrop_session": login(running, user)}, **options)


def routes_of(routes: Iterable[Any], prefix: str = "") -> Iterable[tuple[str, set[str]]]:
    """(path, methods) of every route, also those of the routers included in the app."""
    for route in routes:
        if hasattr(route, "original_router"):  # FastAPI's included router
            yield from routes_of(route.original_router.routes, prefix + route.include_context.prefix)
        else:
            yield prefix + getattr(route, "path", ""), set(getattr(route, "methods", None) or ())


def submit(
    client: TestClient, prompt: str = "a red fox", *, references: Iterable[tuple[str, bytes]] = (), wait: int | None = None,
    headers: dict[str, str] | None = None, **form: object,
) -> httpx2.Response:  # fmt: skip
    """POST /api/jobs with a prompt, form fields and reference images (name, content)."""
    files = [reference(name, data) for name, data in references]
    params = {"wait": wait} if wait is not None else None
    data = {"prompt": prompt, **{name: str(value) for name, value in form.items()}}
    return client.post("/api/jobs", data=data, files=files or None, params=params, headers=headers)


def generate(client: TestClient, prompt: str = "a red fox", **form: object) -> dict[str, Any]:
    """A job made through the API and waited for, as GET /api/jobs/{id} gives it once finished."""
    response = submit(client, prompt, wait=60, **form)
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] in ("completed", "failed"), job
    return job


def wait_for(client: TestClient, job_id: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = client.get(f"/api/jobs/{job_id}", params={"wait": 60}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def add_job(data_dir: Path, user: str, now: float, **fields: Any) -> Any:
    """Write a job's record directly (a job of the past), as the server would have."""
    defaults: dict[str, Any] = {
        "status": Status.COMPLETED, "prompt": "a red fox", "model": "gpt-image", "provider": "openrouter",
        "upstream_model": "openai/gpt-5-image", "api_type": "images",
    }  # fmt: skip
    return JobStore(Storage(data_dir)).create(user, now, **(defaults | fields))


def record_of(data_dir: Path, user: str, job_id: str) -> dict[str, Any]:
    return json.loads((data_dir / "outputs" / user / f"{job_id}.json").read_text(encoding="utf-8"))


class Clock:
    """A clock that the test moves by hand."""

    def __init__(self, now: float = 1_793_456_280.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def access_lines(caplog: Any) -> list[str]:
    """The access log's lines, with the duration replaced by N."""
    return [re.sub(r" \d+ ms", " N ms", record.getMessage()) for record in caplog.records if record.name == logs.__name__]


# --- the real server, as python -m pixdrop starts it ---


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def server_environment(data: Path, port: int) -> dict[str, str]:
    # Without the variables of PixDrop the test process may have, nor TZ, which a test sets when it needs it.
    names = ("TZ", "CONFIG_FILE", "USERS_FILE", "PUBLIC_BASE_URL", "ENABLE_MCP", "LINK_EXPIRY_MINUTES")
    environment = {name: value for name, value in os.environ.items() if name not in names}
    return environment | {"DATA_DIR": str(data), "HOST": "127.0.0.1", "PORT": str(port)}


def start_server(data: Path, port: int, **env: str) -> subprocess.Popen[str]:
    """Run the real server and wait until it answers."""
    server = subprocess.Popen(
        [sys.executable, "-m", "pixdrop"],
        env=server_environment(data, port) | env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            httpx2.get(f"http://127.0.0.1:{port}/api/health").raise_for_status()
            return server
        except httpx2.HTTPError:
            if time.monotonic() > deadline or server.poll() is not None:
                server.kill()
                raise AssertionError(f"the server did not start:\n{server.communicate()[0]}") from None
            time.sleep(0.1)


def run_cli(data: Path, *arguments: str, **env: str) -> subprocess.CompletedProcess[str]:
    """python -m pixdrop with `arguments`, as the administrator runs it."""
    return subprocess.run(
        [sys.executable, "-m", "pixdrop", *arguments],
        env=server_environment(data, free_port()) | env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
