"""Layout of the data directory (the volume mounted into the container).

    <data>/config.json                                 providers and models (CONFIG_FILE)
    <data>/users.json                                  users and tokens (USERS_FILE)
    <data>/outputs/<user>/<id>.json                    a job: its request, status and results
    <data>/outputs/<user>/<id>.png                     its image (the extension follows the format)
    <data>/outputs/<user>/<id>_2.png                   further images, when a model returns several
    <data>/outputs/<user>/<id>_reference/1_<name>.png  its reference images, numbered from 1
    <data>/logs/server.log                             the server's log (written by the logs module)
    <data>/tmp/                                        uploads in progress, emptied at startup

A job id is the UTC time of the request in milliseconds and 4 random hex digits, such as
1793456280412_a9f3: ids sort in the order the jobs were made. Nothing is ever deleted here
(except the temporary directory's leftovers): the administrator removes old jobs by hand.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from .users import NAME_PATTERN

ID_PATTERN = re.compile(r"\d{13}_[0-9a-f]{4}")
REFERENCE_SUFFIX = "_reference"


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.outputs_dir = root / "outputs"
        self.tmp_dir = root / "tmp"

    def prepare(self) -> None:
        """Create the layout and discard the uploads an interrupted run left behind."""
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir.mkdir()

    def user_dir(self, user: str) -> Path:
        if not NAME_PATTERN.fullmatch(user):  # names come from the validated users file
            raise ValueError(f"invalid user name {user!r}")
        return self.outputs_dir / user

    def users(self) -> list[str]:
        """Users with a directory (the users file may have dropped some since)."""
        with suppress(FileNotFoundError):
            return sorted(entry.name for entry in os.scandir(self.outputs_dir) if NAME_PATTERN.fullmatch(entry.name) and entry.is_dir())
        return []

    def record_path(self, user: str, job_id: str) -> Path:
        return self.user_dir(user) / f"{_checked(job_id)}.json"

    def image_path(self, user: str, job_id: str, index: int, extension: str) -> Path:
        suffix = "" if index == 1 else f"_{index}"
        return self.user_dir(user) / f"{_checked(job_id)}{suffix}{extension}"

    def reference_dir(self, user: str, job_id: str) -> Path:
        return self.user_dir(user) / f"{_checked(job_id)}{REFERENCE_SUFFIX}"

    def job_ids(self, user: str) -> list[str]:
        """The ids of the user's jobs, oldest first."""
        with suppress(FileNotFoundError):
            names = (entry.name for entry in os.scandir(self.user_dir(user)))
            return sorted(name[:-5] for name in names if name.endswith(".json") and ID_PATTERN.fullmatch(name[:-5]))
        return []

    def create_record(self, user: str, make: Callable[[str], dict[str, Any]], clock: Callable[[], float] = time.time) -> dict[str, Any]:
        """Write a new job's record under a new id; `make(id)` gives its content."""
        directory = self.user_dir(user)
        directory.mkdir(parents=True, exist_ok=True)
        while True:
            job_id = f"{int(clock() * 1000):013d}_{secrets.token_hex(2)}"
            record = make(job_id)
            try:
                with (directory / f"{job_id}.json").open("x", encoding="utf-8") as file:  # O_EXCL: never reuses an id
                    dump_json(record, file)
                return record
            except FileExistsError:
                continue

    def temp_path(self, prefix: str) -> Path:
        """A fresh, not yet existing path inside the temporary directory."""
        return self.tmp_dir / f"{prefix}-{uuid.uuid4().hex}"

    def size_of(self, user: str) -> int:
        """Bytes taken by the user's records, images and reference images."""
        total = 0
        for path in _files(self.user_dir(user)):
            with suppress(FileNotFoundError):
                total += path.stat().st_size
        return total


def read_json(path: Path) -> dict[str, Any] | None:
    """A record, or None if it does not exist or cannot be read."""
    try:
        with path.open(encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Replace the file atomically: readers see the old content or the new, never a part."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            dump_json(value, file)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_file(path: Path, content: bytes) -> None:
    """Write `content` to `path` atomically (a new image)."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def dump_json(value: dict[str, Any], file: Any) -> None:
    """Write `value` to an open file, and make sure that it reaches the disk."""
    json.dump(value, file, indent=2, ensure_ascii=False)
    file.write("\n")
    file.flush()
    os.fsync(file.fileno())


def _checked(job_id: str) -> str:
    if not ID_PATTERN.fullmatch(job_id):
        raise ValueError(f"invalid job id {job_id!r}")
    return job_id


def _files(root: Path) -> Iterator[Path]:
    with suppress(FileNotFoundError):
        for entry in os.scandir(root):
            if entry.is_dir(follow_symlinks=False):
                yield from _files(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
