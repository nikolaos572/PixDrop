"""The server's log: one access line per change, download or refusal (with the reason), tokens and link
secrets never written, control characters escaped, and the rotating file."""

import logging
import re

import pytest
from helpers import TOKENS, access_lines, browser_of, generate, png, reference, share, submit

from pixdrop import logs
from pixdrop.links import Target

NOT_AN_INTEGER = "Input should be a valid integer, unable to parse string as an integer"
TIME = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}"


def server_messages(caplog) -> list[str]:
    """Everything the server logged (the test's own HTTP client logs its requests too)."""
    return [record.getMessage() for record in caplog.records if not record.name.startswith("httpx")]


def test_one_line_per_change_or_download(client, anonymous, app, caplog):
    job = generate(client, references=[("knight.png", png())])
    secret, _ = app.state.links.create("alice", "testclient", Target(job_id=job["id"]))
    caplog.set_level(logging.INFO)
    # What the web page repeats every few seconds, and Docker's health check: not logged.
    for path in ["/", "/stats", "/info", "/static/style.css", "/api/health", "/api/session", "/api/info", "/api/models",
                 "/api/stats", "/api/jobs", f"/api/jobs/{job['id']}", "/api/jobs/last"]:  # fmt: skip
        assert client.get(path).status_code == 200, path
    for preview in ("images/1?size=medium", "references/1?size=medium"):  # the web page's thumbnails
        assert client.get(f"/api/jobs/{job['id']}/{preview}").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}/images/1").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}/references/1", params={"size": "full"}).status_code == 200
    assert anonymous.get(f"/api/links/{secret}").status_code == 200
    assert submit(client, reference_count=1).status_code == 201
    assert anonymous.post("/api/session", json={"token": TOKENS["bob"]}).status_code == 200
    assert anonymous.delete("/api/session").status_code == 204
    assert access_lines(caplog) == [
        f"testclient alice GET /api/jobs/{job['id']}/images/1 200 N ms",
        f"testclient alice GET /api/jobs/{job['id']}/references/1?size=full 200 N ms",
        "testclient alice GET /api/links/*** 200 N ms",
        "testclient alice POST /api/jobs 201 N ms",
        "testclient bob POST /api/session 200 N ms",
        "testclient bob DELETE /api/session 204 N ms",  # the session it ended
    ]


def test_failed_requests_say_why(client, anonymous, caplog):
    done = generate(client)
    for _ in range(4):
        submit(client, reference_count=1)
    browser = browser_of(client)
    caplog.set_level(logging.INFO)
    requests = [
        (anonymous.post("/api/session", json={"token": "wrong-token-0123456789"}), 401),
        (anonymous.get("/api/jobs"), 401),
        (browser.post("/api/jobs", data={"prompt": "x"}), 403),
        (client.get("/api/jobs/1793456280412_a9f3"), 404),
        (client.get("/api/jobs", params={"page": "x"}), 422),
        (client.post("/api/jobs", data={"prompt": "x", "quality": "ultra"}), 400),
        (client.post(f"/api/jobs/{done['id']}/references", files=[reference("a.png", png())]), 409),
        (client.post("/api/jobs", data={"prompt": "x"}), 429),
        (client.post("/api/jobs", content=iter([b"x"]), headers={"Content-Type": "text/plain"}), 411),
        (client.post("/api/jobs", content=b"x" * (17 * 1024**2 + 1), headers={"Content-Type": "text/plain"}), 413),
        (anonymous.get("/api/links/nope"), 404),
    ]
    assert [response.status_code for response, _ in requests] == [status for _, status in requests]
    assert access_lines(caplog) == [
        "testclient - POST /api/session 401 N ms: invalid token",
        "testclient - GET /api/jobs 401 N ms: missing or invalid token",
        "testclient - POST /api/jobs 403 N ms: cookie-authenticated requests need a X-Requested-With header",
        "testclient alice GET /api/jobs/1793456280412_a9f3 404 N ms: no such job",
        f"testclient alice GET /api/jobs?page=x 422 N ms: query.page: {NOT_AN_INTEGER}",
        "testclient alice POST /api/jobs 400 N ms: quality must be one of: auto, low, medium, high, xhigh, max",
        f"testclient alice POST /api/jobs/{done['id']}/references 409 N ms: the job is completed: reference images can be added only while it is pending",
        "testclient alice POST /api/jobs 429 N ms: you have 4 job(s) in progress, the most allowed (MAX_ACTIVE_JOBS); wait until one finishes",
        "testclient - POST /api/jobs 411 N ms: Content-Length required",
        "testclient - POST /api/jobs 413 N ms: the request exceeds the 17 MB limit",
        "testclient - GET /api/links/*** 404 N ms: unknown link",
    ]


def test_tokens_in_the_url_are_hidden(client, anonymous, caplog):
    job = generate(client)
    caplog.set_level(logging.INFO)
    image = f"/api/jobs/{job['id']}/images/1"
    assert anonymous.get(f"{image}?token={TOKENS['alice']}&size=full").status_code == 200
    assert anonymous.get(f"/api/jobs?size=1&token={TOKENS['bob']}x&token={TOKENS['alice']}x").status_code == 401
    assert anonymous.get(f"/api/jobs?TOKEN={TOKENS['alice']}").status_code == 401  # not a token, but hidden all the same
    assert access_lines(caplog) == [
        f"testclient alice GET {image}?token=***&size=full 200 N ms",
        "testclient - GET /api/jobs?size=1&token=***&token=*** 401 N ms: missing or invalid token",
        "testclient - GET /api/jobs?TOKEN=*** 401 N ms: missing or invalid token",
    ]
    assert not any(TOKENS["alice"] in message or TOKENS["bob"] in message for message in server_messages(caplog))


def test_tokens_in_the_url_are_hidden_however_they_are_encoded(client, anonymous, caplog):
    job = generate(client)
    caplog.set_level(logging.INFO)
    assert anonymous.get(f"/api/jobs/{job['id']}/images/1?%74oken={TOKENS['alice']}").status_code == 200  # accepted as ?token=
    assert not any(TOKENS["alice"] in message for message in server_messages(caplog))


def test_link_secrets_never_reach_the_log(client, anonymous, app, caplog):
    job = generate(client)
    secret, _ = app.state.links.create("alice", "testclient", Target(job_id=job["id"]))
    caplog.set_level(logging.INFO)
    anonymous.get(f"/api/links/{secret}")
    anonymous.get(f"/api/links/{secret}")
    anonymous.post(f"/api/links/{secret}", files=[reference("a.png", png())])
    assert not any(secret in message for message in server_messages(caplog))
    assert access_lines(caplog) == [
        "testclient alice GET /api/links/*** 200 N ms",
        "testclient - GET /api/links/*** 404 N ms: unknown link",
        "testclient - POST /api/links/*** 404 N ms: unknown link",
    ]


def test_requests_cannot_forge_log_lines(client, caplog):
    caplog.set_level(logging.INFO)
    forged = "/api/jobs/1%0A2026-11-02 12:00:00 +0000 INFO    127.0.0.1 admin POST jobs 201 1 ms"
    assert client.get(forged).status_code in (404, 422)
    [line] = access_lines(caplog)
    assert "\n" not in line
    assert line.startswith("testclient alice GET /api/jobs/1%0A2026-11-02%2012%3A00%3A00%20%2B0000%20INFO")


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        ("a\nb\r\tc", "a\\nb\\r\\tc"),
        ("bell\x07 escape\x1b[31m del\x7f", "bell\\x07 escape\\x1b[31m del\\x7f"),
        ("next line\x85 separators\u2028\u2029", "next line\\x85 separators\\u2028\\u2029"),
        ("plain é 狐 text", "plain é 狐 text"),
    ],
)
def test_one_line_escapes_control_characters(text, escaped):
    assert logs.one_line(text) == escaped


@pytest.mark.parametrize(
    ("query", "redacted"),
    [
        ("token=abc", "token=***"),
        ("size=full&token=abc&x=1", "size=full&token=***&x=1"),
        ("Token=abc&token=", "Token=***&token=***"),
        ("mytoken=abc&tokens=1", "mytoken=abc&tokens=1"),
    ],
)
def test_redacted_query(query, redacted):
    assert logs.redacted_query(query) == redacted


def test_unexpected_errors_are_logged_as_errors(client, app, monkeypatch, caplog):
    monkeypatch.setattr(app.state.service, "page", lambda *args, **kwargs: 1 / 0)
    failing = share(client, headers=dict(client.headers), raise_server_exceptions=False)
    assert failing.get("/api/jobs").status_code == 500
    [record] = [record for record in caplog.records if record.name == logs.__name__]
    assert record.levelno == logging.ERROR
    assert re.fullmatch(r"testclient alice GET /api/jobs 500 \d+ ms", record.getMessage())


# --- the file ---


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in root.handlers[:]:
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def test_the_file_is_rotated(tmp_path, root_logger):
    logs.log_to_file(tmp_path / "logs", max_bytes=2000, backups=2)
    for number in range(100):
        logging.getLogger("pixdrop.test").warning("line %d %s", number, "x" * 50)
    files = sorted((tmp_path / "logs").iterdir())
    assert [file.name for file in files] == ["server.log", "server.log.1", "server.log.2"]
    assert all(file.stat().st_size <= 2000 for file in files)
    assert re.fullmatch(rf"{TIME} WARNING line 99 x{{50}}", files[0].read_text().splitlines()[-1])


def test_the_default_rotation_keeps_at_most_50_mb():
    assert (logs.FILE_BYTES, logs.BACKUPS) == (10 * 1024**2, 4)


def test_without_a_writable_directory_the_console_still_logs(tmp_path, root_logger, caplog):
    (tmp_path / "logs").write_text("a file where the directory should be")
    handlers = list(root_logger.handlers)
    logs.log_to_file(tmp_path / "logs")
    assert root_logger.handlers == handlers
    assert "no log file: cannot write" in caplog.text
