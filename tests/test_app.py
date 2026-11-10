"""The application as a whole: its startup (layout, recovery, files missing or invalid), its security
headers, the MCP switch, pending jobs expiring in the background, and the real server as
`python -m pixdrop` runs it."""

import dataclasses
import logging
import re
import signal
import subprocess
import sys
import time

import httpx2
import pytest
from fastapi.testclient import TestClient
from helpers import (
    ALICE,
    TOKENS,
    add_job,
    browser_of,
    free_port,
    generate,
    png,
    record_of,
    reference,
    server_environment,
    size_of,
    start_server,
    submit,
    wait_for,
    write_json,
)

from pixdrop import __version__
from pixdrop import app as app_module
from pixdrop.app import create_app
from pixdrop.errors import ConfigError
from pixdrop.jobs import INTERRUPTED_BY_RESTART
from pixdrop.store import Status
from pixdrop.worker import INTERRUPTED_BY_STOP

SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
TIME = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}"


def has_security_headers(response) -> bool:
    return all(response.headers.get(name) == value for name, value in SECURITY_HEADERS.items())


def test_every_response_has_the_security_headers(client, anonymous):
    job = generate(client)
    for response in (
        anonymous.get("/api/health"),
        anonymous.get("/api/jobs"),  # 401
        client.get("/api/jobs/1793456280412_a9f3"),  # 404
        client.get("/api/jobs/abc"),  # 422
        client.get("/nowhere"),
        client.get(job["images"][0]["url"]),
        client.get(job["images"][0]["medium_url"]),
        anonymous.get("/"),
        anonymous.get("/static/style.css"),
        client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"}),
    ):
        assert has_security_headers(response), response.request.url


def test_refused_bodies_have_the_security_headers_too(client):
    assert has_security_headers(client.post("/api/jobs", content=iter([b"x"])))
    assert has_security_headers(client.post("/api/jobs", content=b"x" * (17 * 1024**2 + 1)))


# --- startup ---


def test_startup_creates_the_layout_and_clears_leftovers(settings):
    leftover = settings.data_dir / "tmp" / "reference-abc"
    leftover.parent.mkdir()
    leftover.write_bytes(b"half an upload")
    create_app(settings)
    assert (settings.data_dir / "outputs").is_dir()
    assert list((settings.data_dir / "tmp").iterdir()) == []


def test_without_the_users_file_the_server_starts_and_refuses_everyone(settings, caplog):
    (settings.users_file).unlink()
    with TestClient(create_app(settings), headers=dict(ALICE)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/jobs").status_code == 401
    assert f"users file {settings.users_file} not found" in caplog.text
    assert "no users yet: add one with python -m pixdrop adduser <name>" in caplog.text


def test_the_log_asks_for_users_only_when_there_are_none(settings, caplog):
    create_app(settings)
    assert "no users yet" not in caplog.text


def test_without_the_config_file_the_server_starts_without_models(settings, caplog):
    settings.config_file.unlink()
    with TestClient(create_app(settings), headers=dict(ALICE)) as client:
        assert client.get("/api/models").json() == {"default_model": None, "models": []}
        assert (client.get("/api/info").json()["server"]["models"], client.get("/api/info").json()["server"]["default_model"]) == (0, None)
        refused = client.post("/api/jobs", data={"prompt": "a red fox"})
        assert (refused.status_code, refused.json()) == (400, {"detail": "no models are configured on this server"})
    assert f"models file {settings.config_file} not found" in caplog.text


@pytest.mark.parametrize(
    ("file", "content", "reason"),
    [
        ("config.json", '{"models": {"m": {"provider": "nope", "model": "x", "api_type": "images"}}}', "model 'm': unknown provider 'nope'"),
        ("config.json", "{not json", "Expecting property name"),
        ("users.json", '{"users": {"Bad Name": "0123456789abcdef"}}', "invalid user name 'Bad Name'"),
    ],
)
def test_an_invalid_file_stops_the_startup(settings, file, content, reason):
    (settings.data_dir / file).write_text(content)
    with pytest.raises(ConfigError, match=re.escape(reason)) as caught:
        create_app(settings)
    assert str(caught.value).startswith(f"{settings.data_dir / file}: ")


def test_startup_logs_what_it_found(make_app, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(make_app(public_base_url="https://pixdrop.example.com")):
        pass
    messages = [record.getMessage() for record in caplog.records if record.name == "pixdrop.app"]
    assert "3 model(s) configured, default gpt-image" in messages
    assert "URLs are given as https://pixdrop.example.com/..." in messages
    assert "MCP server at /mcp, transfer links valid for 60 minute(s)" in messages
    assert "clients' addresses come from X-Forwarded-For in requests from 127.0.0.1 (FORWARDED_ALLOW_IPS)" in messages


def test_startup_recovers_the_jobs_of_the_last_run(settings, upstream, caplog):
    now = time.time()
    interrupted = add_job(settings.data_dir, "alice", now - 30, status=Status.GENERATING, started_at=now - 30)
    expired = add_job(settings.data_dir, "alice", now - 7200, status=Status.PENDING, reference_count=1)
    waiting = add_job(settings.data_dir, "bob", now - 60, status=Status.PENDING, reference_count=1)
    done = add_job(settings.data_dir, "alice", now - 9000)
    caplog.set_level(logging.INFO)
    app = create_app(settings, transport=httpx2.MockTransport(upstream))
    with TestClient(app, headers=dict(ALICE)) as client:
        assert (client.get(f"/api/jobs/{interrupted.id}").json()["error"]) == INTERRUPTED_BY_RESTART
        assert client.get(f"/api/jobs/{expired.id}").json()["error"] == "the reference images were not uploaded within 60 minute(s)"
        assert client.get(f"/api/jobs/{done.id}").json()["status"] == "completed"
        bob = {"Authorization": f"Bearer {TOKENS['bob']}"}
        assert client.get(f"/api/jobs/{waiting.id}", headers=bob).json()["status"] == "pending"
        uploaded = client.post(f"/api/jobs/{waiting.id}/references", files=[reference("a.png", png())], headers=bob)
        assert uploaded.json()["status"] == "generating"
        assert wait_for(client, waiting.id, headers=bob)["status"] == "completed"
    messages = [record.getMessage() for record in caplog.records if record.name == "pixdrop.app"]
    assert "marked 2 job(s) interrupted by the last shutdown (or not given their references in time) as failed" in messages
    assert "1 job(s) still wait for their reference images" in messages


def test_pending_jobs_fail_in_the_background_when_their_references_do_not_come(make_app, monkeypatch):
    monkeypatch.setattr(app_module, "EXPIRY_CHECK_SECONDS", 0.01)
    with TestClient(make_app(link_expiry_seconds=0.05), headers=dict(ALICE)) as client:
        job = submit(client, reference_count=1).json()
        deadline = time.monotonic() + 10
        while (failed := client.get(f"/api/jobs/{job['id']}").json())["status"] == "pending":  # until the check fails it
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert failed["status"] == "failed"
        assert failed["error"].startswith("the reference images were not uploaded within ")
        assert client.get("/api/info").json()["account"]["in_progress"] == 0


# --- the MCP switch ---


def test_without_mcp_there_is_no_endpoint_and_no_links(make_app):
    with TestClient(make_app(enable_mcp=False), headers=dict(ALICE)) as client:
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 404
        assert client.get("/api/links/abc").json() == {"detail": "Not Found"}
        assert client.get("/api/info").json()["server"]["mcp_url"] is None
        assert not any(path.startswith("/api/links") for path in client.get("/openapi.json").json()["paths"])
        assert generate(client)["status"] == "completed"  # the rest works as before


def test_the_api_documentation_is_served(anonymous):
    assert anonymous.get("/docs").status_code == 200
    assert anonymous.get("/openapi.json").json()["info"] == {"title": "PixDrop", "version": __version__}


# --- the real server, as python -m pixdrop starts it ---


def test_the_real_server_generates_logs_and_fails_what_it_interrupts(real_data):
    port = free_port()
    server = start_server(real_data, port)
    url = f"http://127.0.0.1:{port}"
    try:
        with httpx2.Client(base_url=url, headers=dict(ALICE), timeout=30) as client:
            assert client.get("/api/health").json() == {"status": "ok"}
            job = client.post("/api/jobs", params={"wait": 20}, data={"prompt": "a red fox", "model": "fake-images", "n": "2"}).json()
            assert (job["status"], len(job["images"])) == ("completed", 2)
            assert job["images"][1]["url"] == f"{url}/api/jobs/{job['id']}/images/2"
            assert size_of(client.get(job["images"][1]["medium_url"]).content) == (512, 512)
            assert httpx2.post(f"{url}/api/session", json={"token": "wrong-token-0123456789"}).status_code == 401
            slow = client.post("/api/jobs", data={"prompt": "SLOW", "model": "fake-chat"}).json()  # the fake answers after 5 s
            assert slow["status"] == "generating"
    finally:
        server.send_signal(signal.SIGTERM)
        console = server.communicate(timeout=30)[0]
    assert server.returncode in (0, -signal.SIGTERM)
    assert record_of(real_data, "alice", slow["id"])["error"] == INTERRUPTED_BY_STOP  # stopped, not waited for

    log = (real_data / "logs" / "server.log").read_text()
    assert log in console  # the same lines in both
    lines = log.splitlines()
    assert re.fullmatch(rf"{TIME} INFO    PixDrop {re.escape(__version__)} starting, data in {re.escape(str(real_data))}", lines[0])
    assert any(re.fullmatch(rf"{TIME} INFO    127\.0\.0\.1 alice POST /api/jobs\?wait=20 201 \d+ ms", line) for line in lines)
    assert any(re.fullmatch(rf"{TIME} INFO    127\.0\.0\.1 - POST /api/session 401 \d+ ms: invalid token", line) for line in lines)
    assert "/api/health" not in log
    assert "fake-key" not in log  # the provider's key


def test_behind_a_named_proxy_the_client_comes_from_its_header_only(data_dir):
    port = free_port()
    server = start_server(data_dir, port, FORWARDED_ALLOW_IPS="127.0.0.1")  # this test is the trusted proxy
    try:
        headers = dict(ALICE) | {"X-Forwarded-For": "203.0.113.7", "CF-Connecting-IP": "198.51.100.4"}
        job = httpx2.post(f"http://127.0.0.1:{port}/api/jobs", headers=headers, data={"prompt": "x", "reference_count": "1"}).json()
        assert job["client_ip"] == "203.0.113.7"  # from X-Forwarded-For, through uvicorn; CF-Connecting-IP is not trusted then
    finally:
        server.send_signal(signal.SIGTERM)
        server.communicate(timeout=30)


def failed_start(data, **env) -> str:
    """Run the server, which must stop at once; its last line, the same on the console and in the file."""
    result = subprocess.run(
        [sys.executable, "-m", "pixdrop"], env=server_environment(data, free_port()) | env,
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    last = result.stderr.splitlines()[-1]
    assert (data / "logs" / "server.log").read_text().splitlines()[-1] == last
    return last


def test_the_real_server_does_not_start_with_invalid_files_or_settings(data_dir):
    write_json(data_dir / "config.json", {"models": {"m": {"provider": "nope", "model": "x", "api_type": "images"}}})
    assert failed_start(data_dir).endswith(f"ERROR   cannot start: {data_dir / 'config.json'}: model 'm': unknown provider 'nope'")
    assert failed_start(data_dir, MAX_ACTIVE_JOBS="many").endswith("ERROR   cannot start: MAX_ACTIVE_JOBS must be an integer, got 'many'")


def test_settings_are_frozen(settings):
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.max_active_jobs = 100


# --- bounded request bodies (no one makes the server hold a large body without a token) ---


def test_chunked_bodies_are_refused_even_with_a_length(anonymous, client):
    for sender in (anonymous, client):
        response = sender.post("/api/session", headers={"Content-Length": "5", "Transfer-Encoding": "chunked"}, content=b"5\r\nhello\r\n0\r\n\r\n")
        assert (response.status_code, response.json()) == (411, {"detail": "Content-Length required"})


def test_only_uploads_may_be_large(client, anonymous):
    big = b"x" * (64 * 1024 + 1)
    assert anonymous.post("/api/session", content=big, headers={"Content-Type": "application/json"}).json() == {
        "detail": "the request exceeds the 64 KB limit"
    }
    response = client.post("/mcp", content=b"x" * (1024**2 + 1), headers={"Content-Type": "application/json"})
    assert (response.status_code, response.json()) == (413, {"detail": "the request exceeds the 1 MB limit"})
    assert client.post("/api/jobs", data={"prompt": "a fox"}, files=[("reference", ("a.png", png(), "image/png"))]).status_code == 201


def test_uploads_are_refused_before_their_body_without_a_valid_token(anonymous, client):
    files = [("reference", ("a.png", b"x" * 300_000, "image/png"))]  # not even an image: the token is checked first
    response = anonymous.post("/api/jobs", data={"prompt": "a fox"}, files=files)
    assert (response.status_code, response.headers["WWW-Authenticate"]) == (401, "Bearer")
    cookie_without_header = browser_of(client)
    assert cookie_without_header.post("/api/jobs/1793456280412_a9f3/references", files=files).status_code == 403
