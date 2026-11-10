"""Application factory: wires the components together and runs the background work."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext

import httpx2
from fastapi import FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import __version__, api, auth, logs, web
from .config import MAX_REFERENCES, ModelRegistry
from .discovery import Catalogs
from .errors import Conflict, InvalidInput, NotFound, TooManyJobs
from .jobs import JobService
from .links import REFUSAL, LinkRefused, LinkService
from .mcp_server import McpEndpoint
from .providers import ProviderClient
from .sessions import Sessions
from .settings import Settings, url_origin
from .storage import Storage
from .store import JobStore
from .users import UserRegistry
from .worker import Worker

log = logging.getLogger(__name__)

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MULTIPART_OVERHEAD = 1024**2  # room for the prompt and the other form fields around the files
_SMALL_BODY = 64 * 1024  # a login, and anything else that is not an upload
_MCP_BODY = 1024**2  # a tool call: JSON with a prompt of at most 32,000 characters
_UPLOAD_PATH = re.compile(r"/api/jobs(/\d{13}_[0-9a-f]{4}/references)?")  # requests that carry images
EXPIRY_CHECK_SECONDS = 30  # how often pending jobs are checked for expiry


def create_app(settings: Settings | None = None, transport: httpx2.AsyncBaseTransport | None = None) -> FastAPI:
    """The app. `transport` replaces the network for the requests to the providers (tests)."""
    settings = settings or Settings.from_env()
    storage = Storage(settings.data_dir)
    storage.prepare()
    store = JobStore(storage)
    users = UserRegistry(settings.users_file)
    if not users.names():
        log.warning("no users yet: add one with python -m pixdrop adduser <name>")
    sessions = Sessions(storage.sessions_file, users, auth.COOKIE_MAX_AGE)
    models = ModelRegistry(settings.config_file)
    service = JobService(
        store, storage, models, max_active_jobs=settings.max_active_jobs, pending_seconds=settings.link_expiry_seconds
    )
    mcp = McpEndpoint(settings) if settings.enable_mcp else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        failed, pending = await asyncio.to_thread(service.recover)
        if failed:
            log.info("marked %d job(s) interrupted by the last shutdown (or not given their references in time) as failed", failed)
        if pending:
            log.info("%d job(s) still wait for their reference images", pending)
        catalog = models.catalog()
        log.info("%d model(s) configured%s", len(catalog.models), f", default {catalog.default}" if catalog.default else "")
        if settings.public_base_url:
            log.info("URLs are given as %s/...", settings.public_base_url)
        proxies = "any peer" if settings.forwarded_allow_ips == "*" else settings.forwarded_allow_ips
        log.info("clients' addresses come from X-Forwarded-For in requests from %s (FORWARDED_ALLOW_IPS)", proxies)
        if mcp is not None:
            minutes = settings.link_expiry_seconds / 60
            log.info("MCP server at %s, transfer links valid for %g minute(s)", api.MCP_PATH, minutes)
        async with httpx2.AsyncClient(timeout=settings.generation_timeout_seconds, transport=transport) as http:
            worker = Worker(service, storage, models, ProviderClient(http), settings.generation_timeout_seconds)
            service.set_starter(worker.start)
            background = [
                asyncio.create_task(_expire_pending(service)),
                asyncio.create_task(Catalogs(models, http).keep_up_to_date()),  # the models' options (discovery)
            ]
            try:
                async with mcp.running() if mcp is not None else nullcontext():
                    yield
            finally:
                for task in background:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await worker.stop()

    app = FastAPI(title="PixDrop", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage
    app.state.users = users
    app.state.sessions = sessions
    app.state.models = models
    app.state.service = service
    if mcp is not None:
        app.state.links = LinkService(settings.link_expiry_seconds)

    # Every refusal also gives its reason to the access log.
    def refuse(request: Request, status_code: int, detail: str) -> JSONResponse:
        logs.record_error(request, detail)
        return JSONResponse({"detail": detail}, status_code=status_code)

    for exception, status_code in (
        (InvalidInput, status.HTTP_400_BAD_REQUEST),
        (NotFound, status.HTTP_404_NOT_FOUND),
        (Conflict, status.HTTP_409_CONFLICT),
        (TooManyJobs, status.HTTP_429_TOO_MANY_REQUESTS),
    ):

        async def handler(request: Request, exc: Exception, status_code: int = status_code) -> JSONResponse:
            return refuse(request, status_code, str(exc))

        app.add_exception_handler(exception, handler)

    @app.exception_handler(LinkRefused)
    async def link_refused(request: Request, exc: LinkRefused) -> JSONResponse:
        # The same answer whatever the reason, which goes only to the access log.
        logs.record_error(request, str(exc))
        return JSONResponse({"detail": REFUSAL}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        logs.record_error(request, str(exc.detail))
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        reasons = (f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors())
        logs.record_error(request, "; ".join(reasons))
        return await request_validation_exception_handler(request, exc)

    upload_limit = MAX_REFERENCES * settings.max_upload_bytes + _MULTIPART_OVERHEAD

    def body_limit(path: str) -> int:
        """The largest body a request to `path` may have: only uploads of images are large."""
        if _UPLOAD_PATH.fullmatch(path) or path.startswith("/api/links/"):
            return upload_limit
        return _MCP_BODY if path == api.MCP_PATH else _SMALL_BODY

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Refuse oversized bodies before reading them. The server never reads past Content-Length,
        # so requiring the header (and refusing chunked bodies, which do without it) bounds every
        # request. Uploads are authenticated first, so that no one receives a large body without a
        # token (links check theirs before reading, and /mcp authenticates first too). Before all
        # that, changes that a browser sends from a page at another address than PUBLIC_BASE_URL
        # are refused.
        length = request.headers.get("content-length", "")
        path = request.url.path
        response: Response | None = None
        if refusal := _foreign_origin(request, settings.public_base_url):
            response = refuse(request, status.HTTP_403_FORBIDDEN, refusal)
        elif request.method in _BODY_METHODS or "transfer-encoding" in request.headers:
            if "transfer-encoding" in request.headers or not length.isdigit():
                response = refuse(request, status.HTTP_411_LENGTH_REQUIRED, "Content-Length required")
            elif int(length) > (limit := body_limit(path)):
                size = f"{limit // 1024**2} MB" if limit >= 1024**2 else f"{limit // 1024} KB"
                response = refuse(request, status.HTTP_413_CONTENT_TOO_LARGE, f"the request exceeds the {size} limit")
            elif _UPLOAD_PATH.fullmatch(path):
                try:
                    await auth.request_user(request, cookie=True)
                except HTTPException as exc:
                    response = refuse(request, exc.status_code, str(exc.detail))
                    response.headers.update(exc.headers or {})
        if response is None:
            response = await call_next(request)
        if path.startswith("/static/"):
            # Revalidated (cheaply: ETag) on every use, so that an upgrade never pairs new pages with old scripts.
            response.headers.setdefault("Cache-Control", "no-cache")
            if path.endswith(".html"):
                response.headers.setdefault("Content-Security-Policy", web.CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    app.middleware("http")(logs.access_log)  # added last, so it runs first and sees every response
    app.include_router(api.router)
    if mcp is not None:
        app.include_router(api.link_router)
        # POST only: the server sends nothing on its own, so it offers no event stream (GET), and
        # being stateless it has no sessions to end (DELETE). Both get 405, as the protocol allows.
        app.add_route(api.MCP_PATH, mcp, methods=["POST"], include_in_schema=False)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static")
    return app


def _foreign_origin(request: Request, public_base_url: str) -> str | None:
    """Why the request is refused, if a browser sent it from a page at another address than
    PUBLIC_BASE_URL (only checked when it is set: without it, the server cannot tell which
    address is its own behind a reverse proxy).

    Browsers send the page's origin in an Origin header with every POST, PUT, PATCH and DELETE.
    It must be exactly PUBLIC_BASE_URL's scheme, host and port, which also refuses sibling
    subdomains (SameSite cookies take those for the same site). Requests without the header do
    not come from a browser (curl, agents), so no other site can make them; they pass, and need
    a token."""
    origin = request.headers.get("origin")
    if not public_base_url or request.method in _SAFE_METHODS or origin is None:
        return None
    expected = url_origin(public_base_url)
    if url_origin(origin) == expected:
        return None
    return (
        f"this server's address is {expected} (PUBLIC_BASE_URL), but the request came from a page at {origin}: "
        f"open {expected}, or set PUBLIC_BASE_URL to the address you use"
    )


async def _expire_pending(service: JobService) -> None:
    """Fail the pending jobs whose reference images did not come in time, every half minute."""
    while True:
        await asyncio.sleep(EXPIRY_CHECK_SECONDS)
        try:
            if expired := await service.expire_pending():
                log.info("%d pending job(s) failed: their reference images were not uploaded in time", expired)
        except Exception:
            log.exception("checking pending jobs failed")
