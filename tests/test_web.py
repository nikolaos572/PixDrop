"""The web page: its pages (or the login page), the session endpoints that manage its cookie, and the
static files. The pages are compared with the files they come from, so these tests do not depend on
what the pages say."""

import re

import pytest
from fastapi.testclient import TestClient
from helpers import ALICE, TOKENS, share

from pixdrop.web import CONTENT_SECURITY_POLICY, STATIC_DIR

PAGES = [("/", "index.html"), ("/stats", "stats.html"), ("/info", "info.html")]
LOGIN = STATIC_DIR / "login.html"


def page_of(response) -> str:
    """Which file of static/ the response is."""
    for path in STATIC_DIR.glob("*.html"):
        if response.content == path.read_bytes():
            return path.name
    raise AssertionError(f"not one of the pages: {response.text[:200]!r}")


@pytest.fixture
def browser(client):
    """A browser without a cookie yet, of the running app."""
    return share(client)


@pytest.mark.parametrize(("path", "page"), PAGES)
def test_pages_show_the_login_page_without_a_valid_cookie(browser, path, page):
    response = browser.get(path)
    assert response.status_code == 200
    assert page_of(response) == "login.html"
    stale = browser.get(path, headers={"Cookie": "pixdrop_token=revoked-token-0123456789"})
    assert page_of(stale) == "login.html"
    # A token elsewhere does not open a page: pages follow the cookie only.
    assert page_of(browser.get(path, headers=ALICE)) == "login.html"


@pytest.mark.parametrize(("path", "page"), PAGES)
def test_pages_with_the_cookie(browser, path, page):
    response = browser.get(path, headers={"Cookie": f"pixdrop_token={TOKENS['alice']}"})
    assert page_of(response) == page


@pytest.mark.parametrize("path", ["/", "/stats", "/info"])
def test_pages_forbid_what_they_do_not_need(browser, path):
    for cookie in ("", f"pixdrop_token={TOKENS['alice']}"):
        response = browser.get(path, headers={"Cookie": cookie} if cookie else {})
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Frame-Options"] == "DENY"
    for directive in ("default-src 'self'", "script-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert directive in CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("page", sorted(path.name for path in STATIC_DIR.glob("*.html")))
def test_pages_keep_to_their_content_security_policy(page):
    # script-src 'self' and style-src 'self': no inline scripts, event handlers or style attributes.
    html = (STATIC_DIR / page).read_text(encoding="utf-8")
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html) == []
    assert re.findall(r"\son[a-z]+\s*=", html) == []
    assert re.findall(r"\sstyle\s*=", html) == []
    assert "<style" not in html
    assert re.findall(r"(?:src|href)=\"(?:https?:)?//", html) == []  # nothing from elsewhere


@pytest.mark.parametrize("page", sorted(path.name for path in STATIC_DIR.glob("*.html")))
def test_what_pages_load_exists(browser, page):
    html = (STATIC_DIR / page).read_text(encoding="utf-8")
    assets = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html))
    assert assets
    for asset in assets:
        assert browser.get(asset).status_code == 200, asset


# --- logging in and out ---


def test_login_sets_a_30_day_http_only_cookie(browser):
    response = browser.post("/api/session", json={"token": TOKENS["bob"]})
    assert (response.status_code, response.json()) == (200, {"user": "bob"})
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"pixdrop_token={TOKENS['bob']};")
    for attribute in ("HttpOnly", "Max-Age=2592000", "Path=/", "SameSite=strict"):
        assert attribute in cookie
    assert "Secure" not in cookie  # plain HTTP; set over HTTPS
    assert page_of(browser.get("/")) == "index.html"
    assert browser.get("/api/session").json() == {"user": "bob"}
    assert browser.get("/api/info").json()["account"]["user"] == "bob"


def test_the_cookie_is_secure_over_https(app):
    response = TestClient(app, base_url="https://testserver").post("/api/session", json={"token": TOKENS["bob"]})
    assert "Secure" in response.headers["set-cookie"]


def test_a_wrong_token_is_refused(browser):
    response = browser.post("/api/session", json={"token": "not-a-valid-token-at-all"})
    assert (response.status_code, response.json()) == (401, {"detail": "invalid token"})
    assert "set-cookie" not in response.headers
    assert page_of(browser.get("/")) == "login.html"


@pytest.mark.parametrize(
    "request_",
    [{"json": {}}, {"json": {"token": "x" * 257}}, {"json": {"token": 12345}}, {"json": ["token"]}, {"content": b"token=abc", "headers": {"Content-Type": "text/plain"}}],
)
def test_invalid_login_bodies(browser, request_):
    assert browser.post("/api/session", **request_).status_code == 422


def test_the_session_needs_a_token(browser):
    assert browser.get("/api/session").status_code == 401


def test_logout_clears_the_cookie(browser, data_dir):
    browser.post("/api/session", json={"token": TOKENS["alice"]})
    response = browser.delete("/api/session")
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith('pixdrop_token="";') and "Max-Age=0" in cookie
    assert page_of(browser.get("/")) == "login.html"
    assert browser.get("/api/session").status_code == 401
    assert (data_dir / "users.json").exists()  # deletes nothing else


# --- static files ---


@pytest.mark.parametrize(
    ("asset", "content_type"),
    [
        ("style.css", "text/css"),
        ("common.js", "text/javascript"),
        ("theme.js", "text/javascript"),
        ("login.js", "text/javascript"),
        ("app.js", "text/javascript"),
        ("favicon.svg", "image/svg+xml"),
    ],
)
def test_static_files_are_served(anonymous, asset, content_type):
    response = anonymous.get(f"/static/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in (content_type, "application/javascript")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-cache"  # revalidated, so an upgrade never mixes old and new files
    assert response.content == (STATIC_DIR / asset).read_bytes()


@pytest.mark.parametrize("path", ["/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fsettings.py", "/static/missing.js", "/static/"])
def test_nothing_else_is_served_from_static(anonymous, path):
    response = anonymous.get(path)
    assert response.status_code == 404
    assert "Settings" not in response.text


def test_the_pages_have_their_policy_also_as_static_files(anonymous):
    response = anonymous.get("/static/index.html")
    assert (response.status_code, response.headers["Content-Security-Policy"]) == (200, CONTENT_SECURITY_POLICY)
    assert "Content-Security-Policy" not in anonymous.get("/static/style.css").headers
