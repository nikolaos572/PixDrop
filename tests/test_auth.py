"""Authentication: the four places a token can be and which one decides, the web page's cookie and its
CSRF header, the MCP endpoint (which ignores the cookie), and the users file reloaded while running."""

import pytest
from helpers import TOKENS, browser_of, login, png, reference, share, write_json

ALICE_TOKEN = TOKENS["alice"]
WRONG = "wrong-token-0123456789"
USERS = {token: name for name, token in TOKENS.items()}
MCP_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def credentials(client, bearer=None, api_key=None, query=None, cookie=None):
    """The headers and query parameters that carry these tokens. A cookie given a user's token
    holds a session of that user, as logging in on the web page makes."""
    headers, params = {}, {}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if api_key is not None:
        headers["X-API-Key"] = api_key
    if cookie is not None:
        headers["Cookie"] = f"pixdrop_session={login(client, USERS[cookie]) if cookie in USERS else cookie}"
    if query is not None:
        params["token"] = query
    return {"headers": headers, "params": params}


def session(client, **tokens):
    return client.get("/api/session", **credentials(client, **tokens))


@pytest.mark.parametrize("place", ["bearer", "api_key", "query", "cookie"])
def test_each_place_carries_the_token(anonymous, place):
    response = session(anonymous, **{place: ALICE_TOKEN})
    assert (response.status_code, response.json()) == (200, {"user": "alice"})


@pytest.mark.parametrize("place", ["bearer", "api_key", "query", "cookie"])
def test_a_wrong_token_is_refused_wherever_it_is(anonymous, place):
    response = session(anonymous, **{place: WRONG})
    detail = "the session ended or expired: log in again" if place == "cookie" else "missing or invalid token"
    assert (response.status_code, response.json()) == (401, {"detail": detail})
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": f"Basic {ALICE_TOKEN}"}, {"Authorization": ALICE_TOKEN}, {"Authorization": "Bearer"}, {"Cookie": "other=1"}],
)
def test_requests_without_a_token_are_refused(anonymous, headers):
    response = anonymous.get("/api/jobs", headers=headers)
    assert (response.status_code, response.headers["WWW-Authenticate"]) == (401, "Bearer")


def test_of_repeated_token_parameters_the_last_one_counts(anonymous):
    # Starlette's query parameters: the last value of a name wins.
    assert anonymous.get(f"/api/session?token={WRONG}&token={ALICE_TOKEN}").json() == {"user": "alice"}
    assert anonymous.get(f"/api/session?token={ALICE_TOKEN}&token={WRONG}").status_code == 401


def test_the_bearer_scheme_is_case_insensitive(anonymous):
    assert anonymous.get("/api/session", headers={"Authorization": f"bearer {ALICE_TOKEN}"}).json() == {"user": "alice"}


@pytest.mark.parametrize(
    ("tokens", "user"),
    [
        # The first place present decides: a wrong token there is not rescued by a right one further on.
        ({"bearer": WRONG, "api_key": ALICE_TOKEN}, None),
        ({"bearer": WRONG, "query": ALICE_TOKEN}, None),
        ({"bearer": WRONG, "cookie": ALICE_TOKEN}, None),
        ({"api_key": WRONG, "query": ALICE_TOKEN}, None),
        ({"api_key": WRONG, "cookie": ALICE_TOKEN}, None),
        ({"query": WRONG, "cookie": ALICE_TOKEN}, None),
        # And a right token there is not spoilt by a wrong one further on.
        ({"bearer": ALICE_TOKEN, "api_key": WRONG, "query": WRONG, "cookie": WRONG}, "alice"),
        ({"api_key": ALICE_TOKEN, "query": WRONG, "cookie": WRONG}, "alice"),
        ({"query": ALICE_TOKEN, "cookie": WRONG}, "alice"),
        ({"bearer": TOKENS["bob"], "query": ALICE_TOKEN}, "bob"),
    ],
)
def test_the_first_place_present_decides(anonymous, tokens, user):
    response = session(anonymous, **tokens)
    assert response.status_code == (200 if user else 401)
    if user:
        assert response.json() == {"user": user}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/info"),
        ("GET", "/api/models"),
        ("GET", "/api/stats"),
        ("GET", "/api/session"),
        ("GET", "/api/jobs"),
        ("POST", "/api/jobs"),
        ("GET", "/api/jobs/last"),
        ("GET", "/api/jobs/1793456280412_a9f3"),
        ("POST", "/api/jobs/1793456280412_a9f3/references"),
        ("GET", "/api/jobs/1793456280412_a9f3/images/1"),
        ("GET", "/api/jobs/1793456280412_a9f3/references/1?size=medium"),
    ],
)
def test_every_endpoint_but_health_needs_a_token(anonymous, method, path):
    response = anonymous.request(method, path)
    assert (response.status_code, response.json()) == (401, {"detail": "missing or invalid token"})


# --- the web page's cookie ---


def test_the_cookie_reads_without_the_csrf_header(client):
    browser = browser_of(client)
    assert browser.get("/api/jobs").status_code == 200
    assert browser.get("/api/info").json()["account"]["user"] == "alice"


def test_changes_through_the_cookie_need_the_csrf_header(client, data_dir):
    browser = browser_of(client)
    refused = browser.post("/api/jobs", data={"prompt": "a red fox"})
    assert (refused.status_code, refused.json()) == (403, {"detail": "cookie-authenticated requests need a X-Requested-With header"})
    pending = client.post("/api/jobs", data={"prompt": "x", "reference_count": "1"}).json()
    upload = browser.post(f"/api/jobs/{pending['id']}/references", files=[reference("a.png", png())])
    assert upload.status_code == 403
    assert client.get("/api/jobs").json()["total"] == 1  # only the job made with the header token

    accepted = browser.post("/api/jobs", data={"prompt": "a red fox"}, headers={"X-Requested-With": "fetch"})
    assert (accepted.status_code, accepted.json()["source"]) == (201, "web")


def test_explicit_tokens_need_no_csrf_header(client):
    # Another site cannot make a browser send them; the cookie alongside is not even looked at.
    response = client.post("/api/jobs", data={"prompt": "a red fox"}, headers={"Cookie": f"pixdrop_session={login(client, 'bob')}"})
    assert (response.status_code, response.json()["source"]) == (201, "api")
    assert client.get(f"/api/jobs/{response.json()['id']}").status_code == 200  # alice's, not bob's


# --- the MCP endpoint ---


@pytest.mark.parametrize("tokens", [{"bearer": ALICE_TOKEN}, {"api_key": ALICE_TOKEN}, {"query": ALICE_TOKEN}])
def test_the_mcp_endpoint_takes_the_token_in_a_header_or_the_url(anonymous, tokens):
    request = credentials(anonymous, **tokens)
    response = anonymous.post("/mcp", json=MCP_LIST, params=request["params"], headers=MCP_HEADERS | request["headers"])
    assert response.status_code == 200
    assert "tools" in response.json()["result"]


@pytest.mark.parametrize(
    "tokens",
    [
        {},
        {"bearer": WRONG},
        {"bearer": WRONG, "query": ALICE_TOKEN},
        {"cookie": ALICE_TOKEN},  # no web page calls it: the cookie, with a session of alice's, is not accepted
    ],
)
def test_the_mcp_endpoint_refuses_other_requests(anonymous, tokens):
    request = credentials(anonymous, **tokens)
    headers = MCP_HEADERS | request["headers"] | {"X-Requested-With": "fetch"}
    response = anonymous.post("/mcp", json=MCP_LIST, params=request["params"], headers=headers)
    assert (response.status_code, response.headers["WWW-Authenticate"]) == (401, "Bearer")


# --- the users file ---


def test_users_can_be_added_and_removed_without_a_restart(anonymous, data_dir):
    write_json(data_dir / "users.json", {"users": {"bob": TOKENS["bob"], "carol": "carol-token-0123456789"}})
    assert session(anonymous, bearer=ALICE_TOKEN).status_code == 401
    assert session(anonymous, bearer="carol-token-0123456789").json() == {"user": "carol"}
    assert session(anonymous, query="carol-token-0123456789").json() == {"user": "carol"}
    write_json(data_dir / "users.json", {"users": TOKENS})
    assert session(anonymous, bearer=ALICE_TOKEN).json() == {"user": "alice"}


def test_a_broken_users_file_keeps_the_previous_users(anonymous, data_dir):
    write_json(data_dir / "users.json", {"users": {"alice": "short"}})
    assert session(anonymous, bearer=ALICE_TOKEN).json() == {"user": "alice"}


def test_without_the_users_file_every_request_is_refused(anonymous, data_dir):
    (data_dir / "users.json").unlink()
    assert session(anonymous, bearer=ALICE_TOKEN).status_code == 401
    assert anonymous.get("/api/health").status_code == 200
