"""The web page's sessions: what its login cookie stands for.

Logging in with a token starts a session, and the cookie holds the session's id, a random
secret, never the token. Logging out ends the session on the server, so a copy of the
cookie taken before stops working too. A session also ends when it expires, and when its
user is removed or given a new token (python -m pixdrop reset-token): it is bound to the
token it was started with.

The sessions are kept in memory, and in a file of the data directory so that they survive
a restart. The file holds only SHA-256 digests, of the ids and of the tokens, so nothing in
it can be used to log in::

    {"sessions": {"<digest of the id>": {"user": "alice", "token": "<digest of the token>", "expires_at": 1795000000}}}
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .storage import write_secret_json
from .users import UserRegistry, digest

log = logging.getLogger(__name__)

MAX_PER_USER = 100  # sessions a user can have at the same time; logging in once more ends the oldest
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Session:
    user: str
    token: str  # the digest of the token it was started with
    expires_at: float  # Unix time


class Sessions:
    def __init__(self, path: Path, users: UserRegistry, lifetime_seconds: float, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._users = users
        self._lifetime = lifetime_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = _read(path)

    def start(self, user: str, token: str) -> str:
        """A new session of `user`, who logged in with `token`: its id, for the cookie."""
        session_id = secrets.token_urlsafe(32)  # 256 random bits
        with self._lock:
            self._sessions[digest(session_id)] = Session(user, digest(token), self._clock() + self._lifetime)
            self._save()
        return session_id

    def user(self, session_id: str) -> str | None:
        """The user of the session with this id, or None if it ended, expired or never was."""
        with self._lock:
            session = self._sessions.get(digest(session_id))
        return session.user if session is not None and self._valid(session) else None

    def end(self, session_id: str) -> str | None:
        """End the session with this id (logging out); returns its user, if it was valid."""
        with self._lock:
            session = self._sessions.pop(digest(session_id), None)
            if session is None:
                return None
            self._save()
        return session.user if self._valid(session) else None

    def _valid(self, session: Session) -> bool:
        return session.expires_at > self._clock() and self._users.has_token(session.user, session.token)

    def _save(self) -> None:
        """Write the sessions still valid, at most MAX_PER_USER per user (the newest)."""
        valid = sorted(((key, session) for key, session in self._sessions.items() if self._valid(session)),
                       key=lambda item: item[1].expires_at, reverse=True)  # fmt: skip
        kept: dict[str, Session] = {}
        count: dict[str, int] = {}
        for key, session in valid:
            count[session.user] = count.get(session.user, 0) + 1
            if count[session.user] <= MAX_PER_USER:
                kept[key] = session
        self._sessions = kept
        write_secret_json(self._path, {"sessions": {key: asdict(session) for key, session in kept.items()}})


def _read(path: Path) -> dict[str, Session]:
    """The sessions in the file. A broken file loses them, logging everyone out, rather than
    stopping the server: they are only a convenience."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return {key: _session(value) for key, value in document["sessions"].items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        log.error("ignoring the invalid sessions file %s, which logs every browser out: %s", path, exc)
        return {}


def _session(value: dict) -> Session:
    user, token, expires_at = value["user"], value["token"], value["expires_at"]
    if not isinstance(user, str) or not isinstance(token, str) or not _DIGEST.fullmatch(token) or not isinstance(expires_at, int | float):
        raise ValueError(f"invalid session {value!r}")
    return Session(user, token, float(expires_at))
