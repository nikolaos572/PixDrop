"""The administrator's commands on the users file: adding a user, and giving one a new token.

They run in a process of their own beside the server, which only reads the file and notices
the change (users.py). The rest of the document is kept, since another service may share it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .storage import dump_json
from .users import parse_users

NEW_FILE_MODE = 0o600  # tokens are secrets


def add_user(path: Path, name: str) -> str:
    """Add a user with a new token, creating the file if needed; returns the token."""
    return _new_token(path, name, exists=False)


def reset_token(path: Path, name: str) -> str:
    """Give a user a new token, so that the old one stops working; returns the new one."""
    return _new_token(path, name, exists=True)


def _new_token(path: Path, name: str, *, exists: bool) -> str:
    path = path.resolve()  # a link stays a link: what it points to is what changes
    with _locked(path.parent):
        document = _read(path)
        users = document["users"]
        if exists and name not in users:
            raise ConfigError(f"no user {name!r} in {path}")
        if not exists and name in users:
            raise ConfigError(f"user {name!r} already exists: reset-token gives them a new token")
        token = secrets.token_hex(32)
        users[name] = token
        parse_users(document)  # checks the new name
        _write(path, document)
    return token


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Commands running at the same time take turns, so that none undoes another's change.
    The lock is on the directory because the file itself gets replaced."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)  # releases the lock


def _read(path: Path) -> dict[str, Any]:
    """The document, checked: a broken file stays as it is, for the administrator to fix."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        parse_users(document)
    except FileNotFoundError:
        return {"users": {}}
    except ValueError as exc:  # ConfigError, and JSON and encoding errors
        raise ConfigError(f"{path}: {exc}") from exc
    return document


def _write(path: Path, document: dict[str, Any]) -> None:
    """Replace the file atomically, so that the server never reads a part of it. The new file
    keeps the owner and mode of the old one; without one, it is private to the directory's owner
    (so that the server can still read it when root runs the command)."""
    try:
        owner = path.stat()
        mode = stat.S_IMODE(owner.st_mode)
    except FileNotFoundError:
        owner, mode = path.parent.stat(), NEW_FILE_MODE
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # private
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            with suppress(OSError):  # only root can give a file to another user
                os.fchown(file.fileno(), owner.st_uid, owner.st_gid)
            os.fchmod(file.fileno(), mode)
            dump_json(document, file)
        try:
            os.replace(temporary, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise
            _rewrite(path, document)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _rewrite(path: Path, document: dict[str, Any]) -> None:
    """Write over a file Docker mounted by itself, which cannot be replaced."""
    with path.open("r+", encoding="utf-8") as file:
        dump_json(document, file)
        file.truncate()
