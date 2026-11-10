"""The administrator's commands on the users file: adding a user, and giving one a new token.

They run in a process of their own beside the server, which only reads the file and notices
the change (users.py). The rest of the document is kept, since another service may share it.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .storage import locked_file, write_secret_json
from .users import parse_users


def add_user(path: Path, name: str) -> str:
    """Add a user with a new token, creating the file if needed; returns the token."""
    return _new_token(path, name, exists=False)


def reset_token(path: Path, name: str) -> str:
    """Give a user a new token, so that the old one stops working; returns the new one."""
    return _new_token(path, name, exists=True)


def _new_token(path: Path, name: str, *, exists: bool) -> str:
    with locked_file(path) as real_path:
        document = _read(real_path)
        users = document["users"]
        if exists and name not in users:
            raise ConfigError(f"no user {name!r} in {real_path}")
        if not exists and name in users:
            raise ConfigError(f"user {name!r} already exists: reset-token gives them a new token")
        token = secrets.token_hex(32)
        users[name] = token
        parse_users(document)  # checks the new name
        write_secret_json(real_path, document)
    return token


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
