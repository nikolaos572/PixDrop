"""The server's log: on the console (docker logs) and in a size-limited file in the data directory.

Each line is one event: a fact at startup, a finished job, a request (access_log), or an
error, which comes with its traceback when it is unexpected. Tokens never appear in it:
a `token=` in a URL is logged as `token=***`, and the secrets of transfer links as `***`.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote, unquote_plus

from fastapi import Request, Response

from .client import client_ip

FILE_NAME = "server.log"
FILE_BYTES = 10 * 1024**2  # at this size the file becomes server.log.1, the previous .1 becomes .2, ...
BACKUPS = 4  # ... and the oldest beyond .4 is deleted: the log takes at most 50 MB
_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"
_DOWNLOAD = re.compile(r"/api/jobs/[^/]+/(images|references)/\d+|/api/links/[^/]+")
_LINK_SECRET = re.compile(r"(?<=^/api/links/)[^/]+")  # stands in for a token, so never logged
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85  ]")

log = logging.getLogger(__name__)


def log_to_console() -> None:
    """Send every record, the server's and uvicorn's, to standard error."""
    logging.basicConfig(level=logging.INFO, format=_FORMAT, datefmt=_DATE_FORMAT)
    # The HTTP client logs each request to a provider; the job's own line says what happened.
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def log_to_file(directory: Path, *, max_bytes: int = FILE_BYTES, backups: int = BACKUPS) -> None:
    """Also write every record to `directory`/server.log, rotating it as described above."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            directory / FILE_NAME, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
    except OSError as exc:
        log.warning("no log file: cannot write %s (%s), logging to the console only", directory, exc.strerror)
        return
    handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    logging.getLogger().addHandler(handler)


def one_line(text: str) -> str:
    """`text` with line breaks and other control characters escaped, so it cannot fake log lines."""
    return _CONTROL.sub(lambda match: repr(match.group())[1:-1], text)


def record_error(request: Request, detail: str) -> None:
    """Keep why `request` failed, for its line in the access log."""
    request.state.error = detail


def redacted_query(query: str) -> str:
    """A query string with the value of any token parameter hidden, however its name is
    written (the server decodes ?%74oken= as ?token=)."""
    parts = []
    for part in query.split("&"):
        name, equals, _ = part.partition("=")
        parts.append(f"{name}=***" if equals and unquote_plus(name).lower() == "token" else part)
    return "&".join(parts)


async def access_log(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Log each request on one line: client, user, request, status, duration and, if it
    failed, why. Successful reads are left out, except downloads of full-size images and
    through links: the web page repeats reads every few seconds (and loads a medium-size
    preview of every job it lists), as Docker repeats its health check."""
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        _log_request(request, 500, start)  # the traceback follows, from uvicorn
        raise
    _log_request(request, response.status_code, start)
    return response


def _log_request(request: Request, status: int, start: float) -> None:
    path = request.scope["path"]
    preview = path.startswith("/api/jobs/") and request.query_params.get("size") == "medium"  # the web page's thumbnails
    if status < 400 and request.method in ("GET", "HEAD") and (preview or not _DOWNLOAD.fullmatch(path)):
        return
    target = _LINK_SECRET.sub("***", quote(path))
    if query := request.scope["query_string"]:
        target += "?" + one_line(redacted_query(query.decode("latin-1")))
    user = getattr(request.state, "user", "-")
    line = f"{client_ip(request)} {user} {request.method} {target} {status} {(time.monotonic() - start) * 1000:.0f} ms"
    if error := getattr(request.state, "error", None):
        line += f": {one_line(error)}"
    log.log(logging.ERROR if status >= 500 else logging.INFO, "%s", line)
