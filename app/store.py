"""Job records: one JSON file per job, the only metadata PixDrop keeps (there is no database).

A record is written when the request arrives, and replaced atomically at each change of
status, so that a reader (the API, the statistics, or the administrator with `cat`) always
sees a whole record. Times are stored in UTC, as ISO 8601 with milliseconds.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .storage import Storage, read_json, write_json

log = logging.getLogger(__name__)


class Status(StrEnum):
    PENDING = "pending"  # waiting for its reference images
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (Status.COMPLETED, Status.FAILED)


@dataclass(frozen=True)
class Picture:
    """An image file of a job: a generated image or a reference image."""

    index: int  # from 1
    filename: str  # in the user's directory (images) or the job's reference directory
    content_type: str
    bytes: int
    width: int
    height: int
    original_name: str | None = None  # of a reference image, as uploaded


@dataclass(frozen=True)
class Usage:
    """What the provider reported; cost in USD (OpenRouter's usage.cost)."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None


@dataclass(frozen=True)
class Job:
    id: str
    user: str
    status: Status
    created_at: float  # Unix time
    prompt: str
    model: str  # the configured model's name
    provider: str
    upstream_model: str  # the provider's model id
    api_type: str
    options: Mapping[str, str | int] = field(default_factory=dict)
    source: str = "api"  # web, api or mcp
    client_ip: str = "-"
    user_agent: str = ""
    reference_count: int = 0  # announced or uploaded
    references: tuple[Picture, ...] = ()
    images: tuple[Picture, ...] = ()
    started_at: float | None = None
    finished_at: float | None = None
    duration_seconds: float | None = None  # of the provider's answer
    text: str | None = None  # text the model returned along with the image
    usage: Usage | None = None
    upstream_id: str | None = None
    error: str | None = None

    def updated(self, **changes: Any) -> Job:
        return replace(self, **changes)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = self.status.value
        for name in ("created_at", "started_at", "finished_at"):
            record[name] = _iso(record[name])
        record["options"] = dict(self.options)
        for pictures in ("references", "images"):
            for picture in record[pictures]:
                if picture["original_name"] is None:
                    del picture["original_name"]
        return _ordered(record)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Job:
        """A job from its record; ValueError, KeyError or TypeError if it is malformed."""
        usage = record.get("usage")
        return cls(
            id=record["id"],
            user=record["user"],
            status=Status(record["status"]),
            created_at=_time(record["created_at"]),
            prompt=record["prompt"],
            model=record["model"],
            provider=record.get("provider", ""),
            upstream_model=record.get("upstream_model", ""),
            api_type=record.get("api_type", ""),
            options=dict(record.get("options") or {}),
            source=record.get("source", "api"),
            client_ip=record.get("client_ip", "-"),
            user_agent=record.get("user_agent", ""),
            reference_count=int(record.get("reference_count", 0)),
            references=tuple(Picture(**picture) for picture in record.get("references", ())),
            images=tuple(Picture(**picture) for picture in record.get("images", ())),
            started_at=_optional_time(record.get("started_at")),
            finished_at=_optional_time(record.get("finished_at")),
            duration_seconds=record.get("duration_seconds"),
            text=record.get("text"),
            usage=Usage(**usage) if isinstance(usage, dict) else None,
            upstream_id=record.get("upstream_id"),
            error=record.get("error"),
        )


# The order of the fields in a record file, for people reading it.
_FIELD_ORDER = (
    "id", "user", "status", "created_at", "started_at", "finished_at", "duration_seconds", "source",
    "client_ip", "user_agent", "prompt", "model", "provider", "upstream_model", "api_type", "options",
    "reference_count", "references", "images", "text", "usage", "upstream_id", "error",
)  # fmt: skip


def _ordered(record: dict[str, Any]) -> dict[str, Any]:
    return {name: record[name] for name in _FIELD_ORDER} | record


class JobStore:
    """Reads and writes job records. Writes are not serialized here: jobs.JobService makes
    every change to a job under its lock."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def create(self, user: str, now: float, **fields: Any) -> Job:
        """A new job, written as its record; its id is made from `now`."""
        record = self._storage.create_record(
            user,
            lambda job_id: Job(id=job_id, user=user, created_at=now, **fields).to_record(),
            clock=lambda: now,
        )
        return Job.from_record(record)

    def save(self, job: Job) -> None:
        write_json(self._storage.record_path(job.user, job.id), job.to_record())

    def get(self, user: str, job_id: str) -> Job | None:
        record = read_json(self._storage.record_path(user, job_id))
        return self._parse(record, user, job_id) if record is not None else None

    def ids(self, user: str) -> list[str]:
        """The user's job ids, newest first."""
        return self._storage.job_ids(user)[::-1]

    def page(self, user: str, offset: int, limit: int) -> tuple[list[Job], int]:
        """Some of the user's jobs, newest first, and the number of jobs the user has in all.
        Only the records of the page are read."""
        ids = self.ids(user)
        jobs = (self.get(user, job_id) for job_id in ids[offset : offset + limit])
        return [job for job in jobs if job is not None], len(ids)

    def last(self, user: str) -> Job | None:
        for job_id in self.ids(user):
            if (job := self.get(user, job_id)) is not None:
                return job
        return None

    def all(self, user: str) -> list[Job]:
        """Every job of the user, newest first (statistics read them all: a few hundred)."""
        return self.page(user, 0, len(self.ids(user)))[0]

    def users(self) -> list[str]:
        return self._storage.users()

    def _parse(self, record: dict[str, Any], user: str, job_id: str) -> Job | None:
        try:
            job = Job.from_record(record)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("ignoring the malformed record of job %s of %s: %s", job_id, user, exc)
            return None
        if job.id != job_id or job.user != user:
            log.warning("ignoring the record of job %s of %s, which names job %s of %s", job_id, user, job.id, job.user)
            return None
        return job


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _optional_time(text: str | None) -> float | None:
    return None if text is None else _time(text)
