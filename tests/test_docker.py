"""Smoke tests of the Docker image: python -m pytest tests/test_docker.py -m docker

The image is built once per session; set TEST_IMAGE to test an existing image instead.
The containers generate with the fake provider (fake_provider.py), which runs in this
process and which they reach through the host's gateway.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx2
import pytest
from fake_provider import make_server
from PIL import Image

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "docker-test-token-0123456789"
HARDENING = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges")
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 10"><rect width="20" height="10" fill="teal"/></svg>'


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def png(width: int = 64, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 200, 30, 255)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture(scope="session")
def image() -> str:
    if image := os.environ.get("TEST_IMAGE"):
        return image
    subprocess.run(["docker", "build", "-t", "pixdrop:test", str(ROOT)], check=True)
    return "pixdrop:test"


@pytest.fixture(scope="session")
def provider_port() -> int:
    """The fake provider, on every interface of the host, so that containers reach it."""
    server = make_server("0.0.0.0", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def data(tmp_path: Path, image: str, provider_port: int):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o777)  # the container runs as uid 1001
    (data / "users.json").write_text(json.dumps({"users": {"tester": TOKEN}}))
    (data / "config.json").write_text(
        json.dumps(
            {
                "providers": {"fake": {"base_url": f"http://host.docker.internal:{provider_port}/api/v1", "api_key": "k"}},
                "models": {
                    "fake-images": {"provider": "fake", "model": "fake/images", "api_type": "images", "options": ["quality"]},
                    "fake-chat": {"provider": "fake", "model": "fake/chat", "api_type": "chat", "options": ["aspect_ratio"]},
                    "fake-auto": {"provider": "fake", "model": "fake/images", "api_type": "images"},  # the catalogue's options
                },
            }
        )
    )
    yield data
    # Let the host user remove what the container created.
    subprocess.run(["docker", "run", "--rm", "--volume", f"{data}:/data", "--entrypoint", "chmod", image, "-R", "a+rwX", "/data"])


class Server:
    """A container of the image, serving a data directory from the host."""

    def __init__(self, image: str, data: Path, *options: str, hardened: bool = True) -> None:
        self.container = docker(
            "run", "--detach", "--publish", "127.0.0.1::8000", "--volume", f"{data}:/data",
            "--add-host", "host.docker.internal:host-gateway", "--env", "TZ=Europe/Athens",
            *(HARDENING if hardened else ()), *options, image,
        )  # fmt: skip
        port = docker("port", self.container, "8000/tcp").splitlines()[0].rsplit(":", 1)[1]
        self.url = f"http://127.0.0.1:{port}"
        self.client = httpx2.Client(base_url=self.url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120)
        deadline = time.monotonic() + 60
        while True:
            try:
                self.client.get("/api/health").raise_for_status()
                return
            except httpx2.HTTPError:
                running = docker("inspect", "--format", "{{.State.Running}}", self.container) == "true"
                if not running or time.monotonic() > deadline:
                    logs = subprocess.run(["docker", "logs", self.container], capture_output=True, text=True)
                    self.stop()
                    raise AssertionError(f"the server did not start:\n{logs.stdout}{logs.stderr}") from None
                time.sleep(0.5)

    def generate(self, prompt: str, files: list | None = None, wait: int = 60, **form: str) -> dict:
        response = self.client.post("/api/jobs", params={"wait": wait}, data={"prompt": prompt, **form}, files=files)
        assert response.status_code == 201, response.text
        return response.json()

    def stop(self) -> None:
        self.client.close()
        subprocess.run(["docker", "rm", "--force", self.container], check=False, capture_output=True)


@pytest.fixture
def server(image: str, data: Path):
    server = Server(image, data)
    yield server
    server.stop()


def test_runs_as_an_unprivileged_user(server):
    assert docker("exec", server.container, "id", "-u") == "1001"
    assert docker("inspect", "--format", "{{.Config.User}}", server.container) == "app"


def test_web_ui_and_auth(server):
    assert 'id="login-form"' in httpx2.get(f"{server.url}/").text
    assert httpx2.get(f"{server.url}/api/jobs").status_code == 401
    assert httpx2.get(f"{server.url}/api/jobs", params={"token": TOKEN}).status_code == 200


def test_generates_from_references_and_serves_both_sizes(server, data):
    files = [("reference", ("front.png", png(), "image/png")), ("reference", ("logo.svg", SVG, "image/svg+xml"))]
    response = server.client.post(
        "/api/jobs", params={"wait": 60}, headers={"X-Forwarded-For": "198.51.100.7"},
        data={"prompt": "the back of this sprite", "model": "fake-chat", "aspect_ratio": "1:1"}, files=files,
    )  # fmt: skip
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "completed", job
    assert job["client_ip"] == "198.51.100.7"
    assert job["created_at"][-6:] in ("+02:00", "+03:00")  # TZ=Europe/Athens
    assert [reference["filename"] for reference in job["references"]] == ["1_front.png", "2_logo.svg"]
    full = server.client.get(f"/api/jobs/{job['id']}/images/1")
    assert full.headers["content-type"] == "image/png" and Image.open(io.BytesIO(full.content)).size == (1024, 1024)
    medium = server.client.get(f"/api/jobs/{job['id']}/images/1", params={"size": "medium"})
    assert Image.open(io.BytesIO(medium.content)).size == (512, 512)
    svg_medium = server.client.get(f"/api/jobs/{job['id']}/references/2", params={"size": "medium"})  # drawn by cairo
    assert svg_medium.headers["content-type"] == "image/png"
    user_dir = data / "outputs" / "tester"
    record = json.loads((user_dir / f"{job['id']}.json").read_text())
    assert record["status"] == "completed" and record["images"][0]["filename"] == f"{job['id']}.png"
    assert (user_dir / f"{job['id']}.png").stat().st_uid == 1001
    assert sorted(path.name for path in (user_dir / f"{job['id']}_reference").iterdir()) == ["1_front.png", "2_logo.svg"]


def test_jobs_survive_a_new_container(image, data):
    first = Server(image, data)
    try:
        job = first.generate("a red apple")
    finally:
        first.stop()
    second = Server(image, data)
    try:
        assert second.client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"
        assert second.client.get("/api/jobs/last/images/1").status_code == 200
    finally:
        second.stop()


def test_docker_stop_fails_the_running_generation(image, data):
    server = Server(image, data)
    try:
        job = server.generate("HANG forever", wait=0)
        assert job["status"] == "generating"
        docker("stop", server.container)  # through tini, the server stops gracefully
        record = json.loads((data / "outputs" / "tester" / f"{job['id']}.json").read_text())
        assert (record["status"], record["error"]) == ("failed", "interrupted: the server stopped during the generation")
    finally:
        server.stop()


def test_a_killed_generation_fails_at_the_next_start(image, data):
    server = Server(image, data)
    try:
        job = server.generate("HANG forever", wait=0)
        docker("kill", server.container)  # no chance to record anything
        docker("start", server.container)  # the same container, published on a new port
        port = docker("port", server.container, "8000/tcp").splitlines()[0].rsplit(":", 1)[1]
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    found = client.get(f"/api/jobs/{job['id']}").json()
                    break
                except httpx2.HTTPError:
                    assert time.monotonic() < deadline
                    time.sleep(0.5)
        assert (found["status"], found["error"]) == ("failed", "interrupted: the server restarted during the generation")
    finally:
        server.stop()


def test_the_mcp_server_is_off_unless_enabled(server):
    assert server.client.post("/mcp", json={}).status_code == 404
    assert server.client.get("/api/info").json()["server"]["mcp_url"] is None


def test_the_mcp_server_generates_through_links(image, data):
    server = Server(image, data, "--env", "ENABLE_MCP=1")
    try:
        _generate_through_mcp(server)
    finally:
        server.stop()


def _generate_through_mcp(server: Server) -> None:
    def call(tool: str, **arguments) -> dict:
        response = httpx2.post(
            f"{server.url}/mcp",
            params={"token": TOKEN},  # as an MCP client configured with the token in the URL
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            timeout=120,
        )
        result = response.json()["result"]
        assert not result["isError"], result
        return result["structuredContent"]

    job = call("generate_image", prompt="a blue gem", quality="high")
    assert job["status"] == "completed"
    downloads = {download["size"]: download for download in job["downloads"]}
    assert Image.open(io.BytesIO(httpx2.get(downloads["medium"]["url"]).content)).size == (512, 512)
    pending = call("generate_image", prompt="its other side", reference_images=1)
    uploaded = httpx2.post(pending["upload"]["url"], files=[("reference", ("side.png", png(), "image/png"))])
    assert uploaded.json()["status"] == "generating"
    assert call("get_job", job_id=pending["id"])["status"] == "completed"


def test_the_health_check_follows_port(image, data):
    container = docker(
        "run", "--detach", "--volume", f"{data}:/data", "--env", "PORT=9000", "--health-interval", "1s", image
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while (health := docker("inspect", "--format", "{{.State.Health.Status}}", container)) != "healthy":
            assert time.monotonic() < deadline, health
            time.sleep(0.5)
    finally:
        subprocess.run(["docker", "rm", "--force", container], check=False, capture_output=True)


def test_settings_come_from_the_environment(image, data):
    server = Server(image, data, "--env", "MAX_UPLOAD_MB=5", "--env", "PUBLIC_BASE_URL=https://pix.example.com", "--env", "ENABLE_MCP=1")
    try:
        info = server.client.get("/api/info").json()["server"]
        assert (info["timezone"], info["max_upload_mb"], info["mcp_url"]) == ("Europe/Athens", 5, "https://pix.example.com/mcp")
        job = server.generate("a lamp")
        assert job["images"][0]["url"] == f"https://pix.example.com/api/jobs/{job['id']}/images/1"
    finally:
        server.stop()


def test_the_stats_command_shows_every_user(server):
    server.generate("a tree")
    table = docker("exec", server.container, "python", "-m", "pixdrop", "stats")
    assert "tester" in table and "ALL USERS" in table
    report = json.loads(docker("exec", server.container, "python", "-m", "pixdrop", "stats", "--json"))
    assert report[0]["user"] == "tester" and report[0]["totals"]["completed"] == 1


def test_the_log_file_outlives_the_container_and_hides_tokens(image, data):
    server = Server(image, data)
    try:
        server.generate("a cloud")
        httpx2.get(f"{server.url}/api/jobs/last/images/1", params={"token": TOKEN})
    finally:
        server.stop()
    log = (data / "logs" / "server.log").read_text()
    assert "PixDrop" in log and "completed in" in log
    assert "token=***" in log and TOKEN not in log


def test_a_plain_docker_run_needs_no_options(image, data):
    server = Server(image, data, hardened=False)
    try:
        assert server.generate("a cat")["status"] == "completed"
    finally:
        server.stop()


def test_the_models_options_come_from_the_providers_catalogue(server):
    deadline = time.monotonic() + 30
    while (auto := server.client.get("/api/models").json()["models"][2])["options_from"] != "provider":
        assert time.monotonic() < deadline
        time.sleep(0.5)
    quality = next(option for option in auto["options"] if option["name"] == "quality")
    assert (quality["values"], auto["max_reference_images"]) == (["low", "medium", "high"], 4)
    job = server.generate("a round logo", model="fake-auto", output_format="svg")
    assert (job["status"], job["images"][0]["content_type"]) == ("completed", "image/svg+xml")
    listing = docker("exec", server.container, "python", "-m", "pixdrop", "models", "fake/images")
    assert "[configured as 'fake-images', 'fake-auto']" in listing and "quality: low medium high" in listing
