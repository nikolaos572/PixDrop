"""Changes a browser sends from a page of another site are refused: their Origin header must be
PUBLIC_BASE_URL's origin or, without it, name the host the request was sent to (the test client
sends its requests to http://testserver, and no Origin unless a test adds one, like curl)."""

import logging

import pytest
from fastapi.testclient import TestClient
from helpers import ALICE, TOKENS, access_lines, browser_of, png, reference

OTHER_SITES = [
    "https://evil.example",
    "http://blog.testserver",  # a sibling subdomain: the same site for SameSite cookies
    "http://testserver.evil.example",
    "null",  # sandboxed pages, some redirects
    "testserver",  # not an origin at all
]
CHANGES = [
    ("POST", "/api/session"),
    ("DELETE", "/api/session"),
    ("POST", "/api/jobs"),
    ("POST", "/api/jobs/1793456280412_a9f3/references"),
    ("POST", "/mcp"),
    ("POST", "/api/links/no-such-link"),
]


def login_from(client: TestClient, origin: str):
    return client.post("/api/session", json={"token": TOKENS["alice"]}, headers={"Origin": origin})


@pytest.mark.parametrize("origin", OTHER_SITES)
def test_a_login_from_another_site_is_refused(anonymous, origin):
    response = login_from(anonymous, origin)
    assert (response.status_code, response.json()) == (
        403, {"detail": f"the request came from a page at {origin}, not from this server (testserver)"},
    )  # fmt: skip
    assert "set-cookie" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"  # an answer of the server like any other


@pytest.mark.parametrize(("method", "path"), CHANGES)
def test_every_change_is_checked_before_anything_else(anonymous, method, path):
    # Before the token (here a valid one) and before the body (here none at all).
    response = anonymous.request(method, path, headers=ALICE | {"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_a_job_from_another_site_is_not_made(client):
    files = [reference("a.png", png())]
    response = client.post("/api/jobs", data={"prompt": "x"}, files=files, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.get("/api/jobs").json()["total"] == 0


def test_a_logout_from_another_site_keeps_the_session(client):
    browser = browser_of(client)
    assert browser.delete("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.get("/api/session").json() == {"user": "alice"}


@pytest.mark.parametrize("origin", ["http://testserver", "http://TestServer", "https://testserver", "http://testserver:8000"])
def test_without_public_base_url_the_host_decides(anonymous, origin):
    # Behind a reverse proxy, the scheme and port the server sees can be the proxy's.
    assert login_from(anonymous, origin).status_code == 200


@pytest.mark.parametrize("origin", ["https://pixdrop.example.com", "https://PixDrop.example.com:443"])
def test_with_public_base_url_its_origin_passes(make_app, origin):
    with TestClient(make_app(public_base_url="https://pixdrop.example.com")) as client:
        assert login_from(client, origin).status_code == 200


@pytest.mark.parametrize("origin", ["http://pixdrop.example.com", "https://pixdrop.example.com:8443", "http://testserver", "https://blog.example.com"])
def test_with_public_base_url_every_other_origin_is_refused(make_app, origin):
    with TestClient(make_app(public_base_url="https://pixdrop.example.com")) as client:
        response = login_from(client, origin)
    assert (response.status_code, response.json()["detail"]) == (
        403,
        f"this server's address is https://pixdrop.example.com (PUBLIC_BASE_URL), but the request came from a page at {origin}: "
        "open https://pixdrop.example.com, or set PUBLIC_BASE_URL to the address you use",
    )


def test_requests_without_an_origin_pass(anonymous, client):
    # Not from a browser, so no other site can make them; they still need a token.
    assert anonymous.post("/api/session", json={"token": TOKENS["alice"]}).status_code == 200
    assert client.post("/api/jobs", data={"prompt": "a red fox"}).status_code == 201


def test_reads_are_not_checked(client):
    assert client.get("/api/jobs", headers={"Origin": "https://evil.example"}).status_code == 200


def test_the_log_says_why(anonymous, caplog):
    caplog.set_level(logging.INFO)
    login_from(anonymous, "https://evil.example")
    assert access_lines(caplog) == [
        "testclient - POST /api/session 403 N ms: the request came from a page at https://evil.example, not from this server (testserver)"
    ]
