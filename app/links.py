"""Transfer links: short-lived URLs through which AI agents upload and download images.

MCP tool calls carry only JSON that the model writes and reads, so images never travel
through them (base64 would fill the model's context). The MCP server (mcp_server) hands
out links instead, which the agent uses with curl. A link holds a random secret that
stands in for the user's token, for one transfer:

- it works only from the address of the client that asked for it;
- it stops working LINK_EXPIRY_MINUTES after it was made;
- one complete transfer uses it up: the reference images of a job uploaded, or one whole
  download. A transfer that fails or is cut off leaves it valid, for another try.

The API checks a link when its request arrives, before an upload is received, so a
transfer that starts in time may end after the link expires. Links are kept in memory, by
the SHA-256 of their secret (the secrets themselves are only in the URLs): a restart
forgets them, and the agent asks for new ones.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

REFUSAL = "no such link: links expire, work once and only from the address they were made for; ask for a new one"


@dataclass(frozen=True)
class Target:
    """What a link transfers: a job's reference images (an upload), or one of its images."""

    job_id: str
    upload: bool = False
    kind: str = "images"  # for a download: "images" or "references"
    index: int = 1
    size: str = "full"  # or "medium"


@dataclass(frozen=True)
class Link:
    hash: str
    user: str
    target: Target
    client: str
    expires_at: float


class LinkRefused(Exception):
    """The link cannot serve this request. The message says why, for the server's log;
    the client only ever gets REFUSAL, whatever the reason."""


class LinkService:
    def __init__(self, lifetime_seconds: float, clock: Callable[[], float] = time.time) -> None:
        self._lifetime = lifetime_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._links: dict[str, Link] = {}

    def create(self, user: str, client: str, target: Target) -> tuple[str, float]:
        """A new link for `client` to transfer `target`: its secret and the time it expires.
        Expired links are forgotten on the way."""
        secret = secrets.token_urlsafe(32)  # 256 random bits
        now = self._clock()
        link = Link(hash=_hash(secret), user=user, target=target, client=client, expires_at=now + self._lifetime)
        with self._lock:
            self._links = {key: value for key, value in self._links.items() if value.expires_at > now}
            self._links[link.hash] = link
        return secret, link.expires_at

    def check(self, secret: str, client: str, *, upload: bool) -> Link:
        """The link with this secret, if `client` may use it now for an upload (or a download)."""
        with self._lock:
            link = self._links.get(_hash(secret))
        if link is None:
            raise LinkRefused("unknown link")  # never made, used up, or expired and forgotten since
        if link.expires_at <= self._clock():
            raise LinkRefused("expired link")
        if link.client != client:
            raise LinkRefused(f"link for {link.client} used from {client}")
        if link.target.upload != upload:
            raise LinkRefused(f"{'upload' if link.target.upload else 'download'} link used for {'an upload' if upload else 'a download'}")
        return link

    @contextmanager
    def using(self, link: Link) -> Iterator[None]:
        """Use a checked link for the enclosed transfer: it is used up when the block completes,
        and valid again when it raises. It is taken out meanwhile, so that of two transfers
        through the same link at the same time, one at most succeeds."""
        with self._lock:
            if self._links.pop(link.hash, None) is None:
                raise LinkRefused("link used up meanwhile")
        try:
            yield
        except BaseException:
            with self._lock:
                self._links.setdefault(link.hash, link)
            raise

    def use_up(self, link: Link) -> None:
        """Forget a link whose transfer has completed (a download, which ends after the
        request's handler returned, so outside of using())."""
        with self._lock:
            self._links.pop(link.hash, None)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
