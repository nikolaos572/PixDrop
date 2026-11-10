"""The web UI: its pages (or the login page) plus the session endpoints that manage its cookie."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import COOKIE_MAX_AGE, COOKIE_NAME, current_user, registry, sessions

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

router = APIRouter()


class Login(BaseModel):
    token: str = Field(max_length=256)


class SessionOut(BaseModel):
    user: str


@router.get("/", include_in_schema=False)
def index(request: Request) -> FileResponse:
    return _page(request, "index.html")


@router.get("/stats", include_in_schema=False)
def stats(request: Request) -> FileResponse:
    return _page(request, "stats.html")


@router.get("/info", include_in_schema=False)
def info(request: Request) -> FileResponse:
    return _page(request, "info.html")


def _page(request: Request, name: str) -> FileResponse:
    """The named page, or the login page if the browser has no valid session."""
    session_id = request.cookies.get(COOKIE_NAME)
    page = name if session_id and sessions(request).user(session_id) else "login.html"
    return FileResponse(
        STATIC_DIR / page,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store", "Content-Security-Policy": CONTENT_SECURITY_POLICY},
    )


@router.post("/api/session", tags=["web"])
def login(body: Login, request: Request, response: Response) -> SessionOut:
    """Check a token and start a session of 30 days, kept in an HTTP-only cookie (which holds
    the session, not the token)."""
    user = registry(request).authenticate(body.token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    request.state.user = user  # for the access log
    response.set_cookie(
        COOKIE_NAME,
        sessions(request).start(user, body.token),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    return SessionOut(user=user)


@router.get("/api/session", tags=["web"])
def session(user: str = Depends(current_user)) -> SessionOut:
    return SessionOut(user=user)


@router.delete("/api/session", status_code=status.HTTP_204_NO_CONTENT, tags=["web"])
def logout(request: Request, response: Response) -> None:
    """Log out of the web page: end the session on the server, so that its cookie stops working
    also where it was copied, and delete the cookie (nothing else is deleted)."""
    if (session_id := request.cookies.get(COOKIE_NAME)) and (user := sessions(request).end(session_id)):
        request.state.user = user  # for the access log
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict", secure=request.url.scheme == "https")
