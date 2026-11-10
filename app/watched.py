"""JSON files the administrator edits while the server runs (users, models), re-read when they change.

A broken edit keeps the previous content (with an error in the log), except at startup,
where it stops the server. A missing file means empty content, with a warning.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

from .errors import ConfigError

log = logging.getLogger(__name__)

T = TypeVar("T")


class WatchedJsonFile(Generic[T]):
    def __init__(self, path: Path, parse: Callable[[object], T], empty: T, what: str) -> None:
        """`parse` validates the decoded document (raising ConfigError or ValueError) and
        returns its content; `empty` stands for a missing file; `what` names the file in the log."""
        self._path = path
        self._parse = parse
        self._empty = empty
        self._what = what
        self._lock = threading.Lock()
        self._signature: tuple[int, int] | None = None
        self._content = empty
        with self._lock:
            self._reload(strict=True)

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> T:
        """The content, re-read first if the file changed."""
        with self._lock:
            self._reload(strict=False)
            return self._content

    def _reload(self, *, strict: bool) -> None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            if self._signature != (0, 0):
                log.warning("%s file %s not found", self._what, self._path)
            self._signature, self._content = (0, 0), self._empty
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._signature:
            return
        self._signature = signature
        try:
            self._content = self._parse(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:  # ConfigError and JSONDecodeError are ValueErrors
            if strict:
                raise ConfigError(f"{self._path}: {exc}") from exc
            log.error("ignoring invalid %s file %s (keeping the previous one): %s", self._what, self._path, exc)
            return
        log.info("loaded the %s file %s", self._what, self._path)
