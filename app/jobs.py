"""The job lifecycle: create, receive reference images, start, finish, wait, recover.

    pending ──(all reference images uploaded)──▶ generating ──▶ completed
       │                                             │
       └──(not uploaded in time)──▶ failed ◀─────────┘ (provider error, timeout, restart)

A job's record is written as soon as its request is accepted: "generating", or "pending"
when its reference images are announced but not uploaded yet (the agents' two-step
upload, see mcp_server). Every change to a job happens under one lock, so two requests
for the same job, or a request and the end of the job's generation, cannot interleave.
The lock is held only for the bookkeeping, never while a provider is generating.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .config import ModelRegistry
from .errors import Conflict, InvalidInput, NotFound, TooManyJobs
from .images import ImageInfo
from .storage import Storage
from .store import Job, JobStore, Picture, Status, Usage

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 32_000
INTERRUPTED_BY_RESTART = "interrupted: the server restarted during the generation"


@dataclass(frozen=True)
class NewJob:
    """What a request asks for: the same for the web page, the API and the MCP server."""

    prompt: str
    model: str | None = None  # None: the default model
    options: Mapping[str, object] = field(default_factory=dict)  # option name -> raw value (None: not given)
    reference_count: int = 0  # reference images announced, to be uploaded later
    source: str = "api"
    client_ip: str = "-"
    user_agent: str = ""


@dataclass(frozen=True)
class Upload:
    """A reference image received and checked, waiting in the temporary directory."""

    path: Path
    original_name: str | None
    info: ImageInfo
    size: int


class JobService:
    def __init__(
        self,
        store: JobStore,
        storage: Storage,
        models: ModelRegistry,
        *,
        max_active_jobs: int,
        pending_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._storage = storage
        self._models = models
        self._max_active = max_active_jobs
        self._pending_seconds = pending_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active: dict[tuple[str, str], asyncio.Event] = {}  # (user, id) of pending and generating jobs
        self._deadlines: dict[tuple[str, str], float] = {}  # pending jobs: when they fail
        self._starter: Callable[[Job], None] = lambda job: None

    def set_starter(self, starter: Callable[[Job], None]) -> None:
        """What starts a job's generation (the worker), called once its references are in."""
        self._starter = starter

    # --- creating and starting jobs ---

    async def create(self, user: str, request: NewJob, uploads: list[Upload]) -> Job:
        """Accept a request: write its record, store its reference images, and start the
        generation unless reference images are still to come. The uploads are moved away."""
        prompt = request.prompt.strip() if isinstance(request.prompt, str) else ""
        if not prompt:
            raise InvalidInput("the prompt is empty")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise InvalidInput(f"the prompt is too long: {len(prompt)} characters (at most {MAX_PROMPT_CHARS})")
        model = self._models.catalog().model(request.model)
        options = model.resolve_options(request.options)
        if uploads and request.reference_count not in (0, len(uploads)):
            raise InvalidInput(f"reference_count is {request.reference_count} but {len(uploads)} reference image(s) were sent")
        count = len(uploads) or request.reference_count
        if count < 0:
            raise InvalidInput("reference_count cannot be negative")
        if count > model.max_reference_images:
            raise InvalidInput(f"model {model.name!r} accepts at most {model.max_reference_images} reference image(s), got {count}")
        if count < model.min_reference_images:  # a model that only edits images
            raise InvalidInput(f"model {model.name!r} needs at least {model.min_reference_images} reference image(s), got {count}")
        waiting = count > len(uploads)
        async with self._lock:
            active = sum(1 for owner, _ in self._active if owner == user)
            if active >= self._max_active:
                raise TooManyJobs(
                    f"you have {active} job(s) in progress, the most allowed (MAX_ACTIVE_JOBS); wait until one finishes"
                )
            now = self._clock()
            job = await asyncio.to_thread(
                self._store.create,
                user,
                now,
                status=Status.PENDING if waiting else Status.GENERATING,
                prompt=prompt,
                model=model.name,
                provider=model.provider.name,
                upstream_model=model.upstream,
                api_type=model.api_type,
                options=options,
                source=request.source,
                client_ip=request.client_ip,
                user_agent=request.user_agent[:300],
                reference_count=count,
                started_at=None if waiting else now,
            )
            if uploads:
                try:
                    references = await asyncio.to_thread(self._store_references, job, uploads)
                    job = job.updated(references=tuple(references))
                    await asyncio.to_thread(self._store.save, job)
                except Exception:
                    log.exception("job %s (%s): cannot store its reference images", job.id, user)
                    failed = job.updated(status=Status.FAILED, finished_at=self._clock(), error="internal error while storing the reference images; see the server log")
                    await asyncio.to_thread(self._store.save, failed)
                    raise
            self._active[(user, job.id)] = asyncio.Event()
            if waiting:
                self._deadlines[(user, job.id)] = now + self._pending_seconds
        if not waiting:
            self._starter(job)
        return job

    async def add_references(self, user: str, job_id: str, uploads: list[Upload]) -> Job:
        """Store reference images announced by a pending job; start it once all are in."""
        if not uploads:
            raise InvalidInput("no reference image was sent: add them as form fields named 'reference'")
        async with self._lock:
            job = await asyncio.to_thread(self._store.get, user, job_id)
            if job is None:
                raise NotFound("no such job")
            if job.status is Status.PENDING and self._expired(user, job_id):
                job = await self._finish_locked(job, Status.FAILED, error=self._expiry_error())
            if job.status is not Status.PENDING:
                raise Conflict(f"the job is {job.status}: reference images can be added only while it is pending")
            remaining = job.reference_count - len(job.references)
            if len(uploads) > remaining:
                raise InvalidInput(f"the job expects {remaining} more reference image(s), got {len(uploads)}")
            references = await asyncio.to_thread(self._store_references, job, uploads)
            job = job.updated(references=job.references + tuple(references))
            starting = len(job.references) == job.reference_count
            if starting:
                job = job.updated(status=Status.GENERATING, started_at=self._clock())
                self._deadlines.pop((user, job_id), None)
            await asyncio.to_thread(self._store.save, job)
        if starting:
            self._starter(job)
        return job

    # --- the end of a job (called by the worker) ---

    async def complete(
        self, job: Job, *, images: list[Picture], usage: Usage | None, text: str | None, upstream_id: str | None,
        duration: float,
    ) -> Job:  # fmt: skip
        async with self._lock:
            return await self._finish_locked(
                job, Status.COMPLETED, images=tuple(images), usage=usage, text=text, upstream_id=upstream_id,
                duration_seconds=round(duration, 3),
            )  # fmt: skip

    async def fail(self, job: Job, error: str, *, duration: float | None = None, usage: Usage | None = None) -> Job:
        async with self._lock:
            return await self._finish_locked(
                job, Status.FAILED, error=error, usage=usage,
                duration_seconds=None if duration is None else round(duration, 3),
            )  # fmt: skip

    async def _finish_locked(self, job: Job, status: Status, **changes: object) -> Job:
        finished = job.updated(status=status, finished_at=self._clock(), **changes)
        try:
            await asyncio.to_thread(self._store.save, finished)
        finally:  # even if the record cannot be written: the user's slot is freed (the next start fails the job)
            key = (job.user, job.id)
            self._deadlines.pop(key, None)
            if (event := self._active.pop(key, None)) is not None:
                event.set()
        return finished

    # --- reading ---

    def get(self, user: str, job_id: str) -> Job:
        """One of the user's jobs; `job_id` may be "last". NotFound if there is no such job."""
        job = self._store.last(user) if job_id == "last" else self._store.get(user, job_id)
        if job is None:
            raise NotFound("you have no jobs yet" if job_id == "last" else "no such job")
        return job

    def page(self, user: str, offset: int, limit: int) -> tuple[list[Job], int]:
        return self._store.page(user, offset, limit)

    def all(self, user: str) -> list[Job]:
        return self._store.all(user)

    def count(self, user: str) -> int:
        return len(self._store.ids(user))

    def active_count(self, user: str) -> int:
        return sum(1 for owner, _ in self._active if owner == user)

    async def wait(self, user: str, job_id: str, seconds: float, *, stop: Callable[[], object] | None = None) -> Job:
        """The job once it is finished, or as it is after `seconds`. `stop` (a coroutine
        function, such as Request.is_disconnected) is asked every second whether to give up.
        A pending job is returned at once: only the upload of its references moves it on."""
        job = await asyncio.to_thread(self.get, user, job_id)
        deadline = time.monotonic() + seconds
        while job.status is Status.GENERATING and (remaining := deadline - time.monotonic()) > 0:
            event = self._active.get((user, job.id))
            if event is None:  # finished meanwhile
                return await asyncio.to_thread(self.get, user, job.id)
            try:
                await asyncio.wait_for(event.wait(), min(1.0, remaining))
            except TimeoutError:
                if stop is not None and await stop():  # type: ignore[misc]
                    break
            job = await asyncio.to_thread(self.get, user, job.id)
        return job

    # --- startup and housekeeping ---

    def recover(self) -> tuple[int, int]:
        """At startup: fail the jobs a restart interrupted, and the pending jobs whose time is
        up; keep the other pending jobs waiting. Returns (failed, still pending)."""
        failed = pending = 0
        now = self._clock()
        for user in self._store.users():
            for job in self._store.all(user):
                deadline = job.created_at + self._pending_seconds
                if job.status is Status.GENERATING or (job.status is Status.PENDING and deadline <= now):
                    error = INTERRUPTED_BY_RESTART if job.status is Status.GENERATING else self._expiry_error()
                    self._store.save(job.updated(status=Status.FAILED, finished_at=now, error=error))
                    failed += 1
                elif job.status is Status.PENDING:
                    self._active[(user, job.id)] = asyncio.Event()
                    self._deadlines[(user, job.id)] = deadline
                    pending += 1
        return failed, pending

    async def expire_pending(self) -> int:
        """Fail the pending jobs whose reference images did not come in time."""
        expired = 0
        async with self._lock:
            for user, job_id in [key for key, deadline in self._deadlines.items() if deadline <= self._clock()]:
                job = await asyncio.to_thread(self._store.get, user, job_id)
                if job is not None and job.status is Status.PENDING:
                    await self._finish_locked(job, Status.FAILED, error=self._expiry_error())
                    expired += 1
                else:
                    self._deadlines.pop((user, job_id), None)
                    self._active.pop((user, job_id), None)
        return expired

    def _expired(self, user: str, job_id: str) -> bool:
        deadline = self._deadlines.get((user, job_id))
        return deadline is not None and deadline <= self._clock()

    def _expiry_error(self) -> str:
        return f"the reference images were not uploaded within {self._pending_seconds / 60:g} minute(s)"

    def _store_references(self, job: Job, uploads: list[Upload]) -> list[Picture]:
        """Move the uploads into the job's reference directory, numbered after those it has."""
        directory = self._storage.reference_dir(job.user, job.id)
        directory.mkdir(parents=True, exist_ok=True)
        pictures = []
        for index, upload in enumerate(uploads, start=len(job.references) + 1):
            filename = f"{index}_{_stem(upload.original_name)}{upload.info.extension}"
            os.replace(upload.path, directory / filename)
            pictures.append(
                Picture(
                    index=index,
                    filename=filename,
                    content_type=upload.info.content_type,
                    bytes=upload.size,
                    width=upload.info.width,
                    height=upload.info.height,
                    original_name=upload.original_name,
                )
            )
        return pictures


def _stem(name: str | None) -> str:
    """A safe file name stem from a client-supplied file name."""
    base = re.split(r"[/\\]", name or "")[-1]
    stem = base.rsplit(".", 1)[0] if "." in base.strip(".") else base
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")[:60] or "reference"
