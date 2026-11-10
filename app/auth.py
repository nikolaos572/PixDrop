"""Authentication: one dependency that finds the token of a request and checks it.

The token is taken from the first of these places that the request uses:

1. `Authorization: Bearer <token>` (API clients and MCP clients that send headers),
2. `X-API-Key: <token>`,
3. `?token=<token>` in the URL (an MCP client configured with `https://.../mcp?token=...`,
   or a link to an image),
4. the web page's cookie, set by logging in (web.py), which holds a session (sessions.py)
   rather than the token. Requests authenticated by the cookie must carry an
   `X-Requested-With` header when they change something: another site cannot add it
   without a CORS preflight, which this server never approves.

The MCP endpoint, which no web page calls, accepts the first three only.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, APIKeyQuery, HTTPAuthorizationCredentials, HTTPBearer

from .sessions import Sessions
from .users import UserRegistry

COOKIE_NAME = "pixdrop_session"
COOKIE_MAX_AGE = 30 * 86_400  # seconds, also the session's lifetime
CSRF_HEADER = "X-Requested-With"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Declared as security schemes, so that the OpenAPI page (/docs) shows the three ways.
_bearer = HTTPBearer(auto_error=False, description="Your token, sent as `Authorization: Bearer <token>`.")
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False, description="Your token, sent as `X-API-Key: <token>`.")
_query = APIKeyQuery(name="token", auto_error=False, description="Your token, as `?token=<token>` in the URL.")


def current_user(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer),
    api_key: str | None = Depends(_api_key),
    query: str | None = Depends(_query),
) -> str:
    """The user owning the request's token; rejects the request otherwise."""
    return _user(request, _explicit_token(bearer, api_key, query), cookie=True)


async def request_user(request: Request, *, cookie: bool) -> str:
    """current_user outside of FastAPI's dependencies: for the MCP endpoint (cookie=False) and
    for the uploads, which are authenticated before their body is read (see app.guard)."""
    explicit = _explicit_token(await _bearer(request), await _api_key(request), await _query(request))
    return _user(request, explicit, cookie=cookie)


async def agent_user(request: Request) -> str:
    """Like current_user, but never from the cookie: for the MCP endpoint."""
    return await request_user(request, cookie=False)


def _user(request: Request, explicit: str | None, *, cookie: bool) -> str:
    if explicit is None and cookie and (session_id := request.cookies.get(COOKIE_NAME, "")):
        if request.method not in _SAFE_METHODS and CSRF_HEADER not in request.headers:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"cookie-authenticated requests need a {CSRF_HEADER} header")
        user = sessions(request).user(session_id)
        if user is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "the session ended or expired: log in again", headers={"WWW-Authenticate": "Bearer"}
            )
        request.state.user = user  # for the access log
        request.state.via_cookie = True  # the web page: jobs it creates are marked "web"
        return user
    return _authenticated(request, explicit or "")


def _explicit_token(bearer: HTTPAuthorizationCredentials | None, api_key: str | None, query: str | None) -> str | None:
    """The token from the first of the header, X-API-Key and the URL that is present."""
    for token in (bearer.credentials if bearer else None, api_key, query):
        if token is not None:
            return token
    return None


def _authenticated(request: Request, token: str) -> str:
    user = registry(request).authenticate(token) if token else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
    request.state.user = user  # for the access log
    return user


def registry(request: Request) -> UserRegistry:
    return request.app.state.users


def sessions(request: Request) -> Sessions:
    return request.app.state.sessions
