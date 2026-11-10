"""Users and their API tokens, read from a JSON file (USERS_FILE), which the administrator
changes with commands (accounts.py) or by hand.

The file maps user names to tokens, in the same format as TeXdrop's, so that one file can
serve both::

    {"users": {"alice": "4c1f...", "bob": "9e0d..."}}

The user name is also the name of the user's directory under outputs/. All users are
equal: each one sees only their own jobs, images and statistics. The file is re-read
when it changes, so users can be added or removed without a restart.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path

from .errors import ConfigError
from .watched import WatchedJsonFile

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{16,256}")  # safe in headers, cookies and URLs


def digest(secret: str) -> str:
    """The SHA-256 of a token or another secret, in hexadecimal: what is kept instead of it."""
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_users(raw: object) -> dict[str, str]:
    """Validate the decoded JSON document and return a token -> user name map."""
    users = raw.get("users") if isinstance(raw, dict) else None
    if not isinstance(users, dict):
        raise ConfigError('expected an object like {"users": {"name": "token"}}')
    tokens: dict[str, str] = {}
    for name, token in users.items():
        if not NAME_PATTERN.fullmatch(name):
            raise ConfigError(f"invalid user name {name!r}: use 1-32 of a-z, 0-9, '_' and '-'")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise ConfigError(f"invalid token for {name!r}: use 16-256 letters, digits or ._~+/=-")
        if token in tokens:
            raise ConfigError(f"users {tokens[token]!r} and {name!r} share a token")
        tokens[token] = name
    return tokens


class UserRegistry:
    """Maps tokens to user names, reloading the file when it changes. Without the file,
    every request is rejected."""

    def __init__(self, path: Path) -> None:
        self._file = WatchedJsonFile(path, parse_users, {}, "users")

    def authenticate(self, token: str) -> str | None:
        """Return the user owning `token`, or None."""
        candidate = token.encode()
        user = None
        for known, name in self._file.current().items():  # compare against all, in constant time
            if secrets.compare_digest(known.encode(), candidate):
                user = name
        return user

    def has_token(self, user: str, token_digest: str) -> bool:
        """Whether `user` still has the token with this digest (the token of a session)."""
        return any(
            name == user and secrets.compare_digest(digest(token), token_digest) for token, name in self._file.current().items()
        )

    def exists(self, user: str) -> bool:
        """Whether `user` is (still) in the file."""
        return user in self._file.current().values()

    def names(self) -> list[str]:
        return sorted(self._file.current().values())
