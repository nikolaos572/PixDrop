"""With PUBLIC_BASE_URL set, changes a browser sends from a page at any other address are refused:
their Origin header must be exactly PUBLIC_BASE_URL's. Without it, the Origin is not checked (the
server cannot tell its own address behind a proxy), and the other protections stay. The test
client sends no Origin unless a test adds one, like curl."""

import logging

import pytest
from fastapi.testclient import TestClient
from helpers import ALICE, TOKENS, access_lines, browser_of, png, reference, share

PUBLIC = "https://pixdrop.example.com"
OTHER_ORIGINS = [
    "https://evil.example",
    "https://blog.example.com",  # a sibling subdomain: the same site for SameSite cookies
    "http://pixdrop.example.com",  # the same host over plain HTTP
    "https://pixdrop.example.com:8443",  # the same host, another port
    "https://pixdrop.example.com.evil.example",
    "http://testserver",  # the address the request was sent to, but not PUBLIC_BASE_URL
    "null",  # sandboxed pages, some redirects
]
CHANGES = [
    ("POST", "/api/session"),
    ("DELETE", "/api/session"),
    ("POST", "/api/jobs"),
    ("POST", "/api/jobs/1793456280412_a9f3/references"),
    ("POST", "/mcp"),
    ("POST", "/api/links/no-such-link"),
]


@pytest.fixture
def public(make_app):
    """Alice's client of a running app whose PUBLIC_BASE_URL is set."""
    with TestClient(make_app(public_base_url=PUBLIC), headers=dict(ALICE)) as client:
        yield client


def login_from(client: TestClient, origin: str):
    return client.post("/api/session", json={"token": TOKENS["alice"]}, headers={"Origin": origin})


@pytest.mark.parametrize("origin", OTHER_ORIGINS)
def test_a_login_from_another_address_is_refused(public, origin):
    response = login_from(share(public), origin)
    assert (response.status_code, response.json()["detail"]) == (
        403,
        f"this server's address is {PUBLIC} (PUBLIC_BASE_URL), but the request came from a page at {origin}: "
        f"open {PUBLIC}, or set PUBLIC_BASE_URL to the address you use",
    )
    assert "set-cookie" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"  # an answer of the server like any other


@pytest.mark.parametrize("origin", [PUBLIC, "https://PixDrop.example.com:443"])
def test_its_own_address_passes_however_written(public, origin):
    assert login_from(share(public), origin).status_code == 200


@pytest.mark.parametrize(("method", "path"), CHANGES)
def test_every_change_is_checked_before_anything_else(public, method, path):
    # Before the token (here a valid one) and before the body (here none at all).
    response = share(public).request(method, path, headers=ALICE | {"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_a_job_from_another_site_is_not_made(public):
    files = [reference("a.png", png())]
    response = public.post("/api/jobs", data={"prompt": "x"}, files=files, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert public.get("/api/jobs").json()["total"] == 0


def test_a_logout_from_another_site_keeps_the_session(public):
    browser = browser_of(public)
    assert browser.delete("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.get("/api/session").json() == {"user": "alice"}


def test_requests_without_an_origin_pass(public):
    # Not from a browser, so no other site can make them; they still need a token.
    assert share(public).post("/api/session", json={"token": TOKENS["alice"]}).status_code == 200
    assert public.post("/api/jobs", data={"prompt": "a red fox"}).status_code == 201


def test_reads_are_not_checked(public):
    assert public.get("/api/jobs", headers={"Origin": "https://evil.example"}).status_code == 200


def test_the_log_says_why(public, caplog):
    caplog.set_level(logging.INFO)
    login_from(share(public), "https://evil.example")
    assert access_lines(caplog) == [
        f"testclient - POST /api/session 403 N ms: this server's address is {PUBLIC} (PUBLIC_BASE_URL), but the request "
        f"came from a page at https://evil.example: open {PUBLIC}, or set PUBLIC_BASE_URL to the address you use"
    ]


def test_without_public_base_url_the_origin_is_not_checked(anonymous, client):
    # Behind a reverse proxy, the server cannot tell which address is its own...
    assert login_from(anonymous, "https://evil.example").status_code == 200
    # ...and the cookie still needs the header that other sites cannot send.
    refused = browser_of(client).post("/api/jobs", data={"prompt": "x"}, headers={"Origin": "https://evil.example"})
    assert (refused.status_code, refused.json()) == (403, {"detail": "cookie-authenticated requests need a X-Requested-With header"})
