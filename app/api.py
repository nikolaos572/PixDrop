"""The JSON API under /api, used by scripts and by the web page alike, and the transfer
links under /api/links through which MCP clients move images (ENABLE_MCP=1).

Images are never sent inside JSON (no base64): a job lists the URLs of its images, in full
size and in medium size (scaled down when asked for), which a client downloads with its
token. Reference images are uploaded as multipart form data, as `curl -F` sends files.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Annotated, Any, BinaryIO
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.types import Message, Receive, Scope, Send

from . import __version__, images
from .auth import current_user, registry
from .client import client_ip
from .config import MAX_REFERENCES, Model
from .errors import InvalidInput, NotFound
from .jobs import MAX_PROMPT_CHARS, JobService, NewJob, Upload
from .links import Link, LinkRefused, LinkService
from .settings import Settings
from .stats import StatsOut, statistics
from .store import Job, Picture, Status
from .storage import Storage

router = APIRouter(prefix="/api")

MAX_WAIT_SECONDS = 600
PER_PAGE = 12  # jobs per page unless asked otherwise
MAX_PER_PAGE = 100
MAX_PAGE = 1_000_000
MCP_PATH = "/mcp"  # where the MCP server (mcp_server) answers when ENABLE_MCP=1
JOB_ID = r"\d{13}_[0-9a-f]{4}"
JobRef = Annotated[str, PathParameter(pattern=rf"^(last|{JOB_ID})$", description="A job id, or `last` for your newest job")]
JobId = Annotated[str, PathParameter(pattern=rf"^{JOB_ID}$", description="A job id")]
Index = Annotated[int, PathParameter(ge=1, le=MAX_REFERENCES, description="The image's number, from 1")]
Wait = Annotated[int, Query(ge=0, le=MAX_WAIT_SECONDS, description="Seconds to wait for the job to finish")]
# A form field rather than a file is accepted too, and ignored when empty: a browser's form sends an
# empty field (filename="") for a file input left empty.
References = Annotated[
    list[UploadFile | str] | None,
    File(alias="reference", description="Reference images (PNG, JPEG, WebP, GIF or SVG); repeat the field for several"),
]
_CHUNK = 1024 * 1024
_IMAGE_HEADERS = {
    "Cache-Control": "private, max-age=86400",  # a job's images never change
    # An SVG opened on its own cannot run scripts or load anything.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Size(StrEnum):
    FULL = "full"
    MEDIUM = "medium"  # scaled down to fit MEDIUM_IMAGE_PX


class Kind(StrEnum):
    IMAGES = "images"  # generated
    REFERENCES = "references"


def local_time(timestamp: float, timezone: ZoneInfo) -> str:
    """A Unix time as the API shows dates: ISO 8601, in the configured time zone."""
    return datetime.fromtimestamp(timestamp, timezone).isoformat(timespec="seconds")


def base_url(request: Request) -> str:
    """The URL the server is reached at: PUBLIC_BASE_URL, or the request's."""
    return _settings(request).public_base_url or str(request.base_url).rstrip("/")


# --- response models ---


class PictureOut(BaseModel):
    index: int
    filename: str
    content_type: str
    bytes: int
    width: int
    height: int
    original_name: str | None  # a reference image's name, as uploaded
    url: str | None  # the full-size image (send your token); None in MCP results
    medium_url: str | None  # the same image scaled down to fit MEDIUM_IMAGE_PX


class UsageOut(BaseModel):
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None  # as the provider reported it (OpenRouter's usage.cost)


class JobOut(BaseModel):
    id: str
    status: Status
    prompt: str
    model: str
    provider: str
    upstream_model: str
    api_type: str
    options: dict[str, str | int]
    source: str  # web, api or mcp
    client_ip: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None  # of the generation
    reference_count: int  # announced; references lists those uploaded
    references: list[PictureOut]
    images: list[PictureOut]
    text: str | None  # text the model returned along with the image
    usage: UsageOut | None
    error: str | None
    upload_url: str | None  # a pending job's: POST its reference images there (send your token)

    @classmethod
    def of(cls, job: Job, request: Request, *, urls: bool = True) -> JobOut:
        timezone = _settings(request).timezone
        job_url = f"{base_url(request)}/api/jobs/{job.id}"

        def local(timestamp: float | None) -> str | None:
            return None if timestamp is None else local_time(timestamp, timezone)

        def pictures(kind: Kind, items: tuple[Picture, ...]) -> list[PictureOut]:
            return [
                PictureOut(
                    index=item.index,
                    filename=item.filename,
                    content_type=item.content_type,
                    bytes=item.bytes,
                    width=item.width,
                    height=item.height,
                    original_name=item.original_name,
                    url=f"{job_url}/{kind}/{item.index}" if urls else None,
                    medium_url=f"{job_url}/{kind}/{item.index}?size=medium" if urls else None,
                )
                for item in items
            ]

        usage = job.usage
        return cls(
            id=job.id,
            status=job.status,
            prompt=job.prompt,
            model=job.model,
            provider=job.provider,
            upstream_model=job.upstream_model,
            api_type=job.api_type,
            options=dict(job.options),
            source=job.source,
            client_ip=job.client_ip,
            created_at=local_time(job.created_at, timezone),
            started_at=local(job.started_at),
            finished_at=local(job.finished_at),
            duration_seconds=job.duration_seconds,
            reference_count=job.reference_count,
            references=pictures(Kind.REFERENCES, job.references),
            images=pictures(Kind.IMAGES, job.images),
            text=job.text,
            usage=UsageOut(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cost_usd=usage.cost,
            )
            if usage
            else None,
            error=job.error,
            upload_url=f"{job_url}/references" if urls and job.status is Status.PENDING else None,
        )


class JobListOut(BaseModel):
    jobs: list[JobOut]
    total: int  # all of the user's jobs, on every page
    page: int
    per_page: int


class OptionOut(BaseModel):
    name: str
    description: str
    type: str  # enum, integer or string
    values: list[str] | None  # of an enum
    minimum: int | None  # of an integer
    maximum: int | None
    pattern: str | None  # of a string
    default: str | int | None  # the model's default; None: the provider's


class ModelOut(BaseModel):
    name: str  # what to ask for
    description: str
    provider: str
    upstream_model: str
    api_type: str
    default: bool  # used when no model is named
    min_reference_images: int  # a model that edits images needs at least one
    max_reference_images: int
    options: list[OptionOut]  # with this model's own values
    options_from: str  # "provider": its provider's model catalogue; "config": config.json; "pending": not read yet

    @classmethod
    def of(cls, model: Model, default: bool) -> ModelOut:
        options = []
        for name, spec in model.specs.items():
            options.append(
                OptionOut(
                    name=name,
                    description=spec.description,
                    type=spec.kind,
                    values=list(spec.values) or None,
                    minimum=spec.minimum,
                    maximum=spec.maximum,
                    pattern=spec.pattern,
                    default=model.defaults.get(name),
                )
            )
        return cls(
            name=model.name,
            description=model.description,
            provider=model.provider.name,
            upstream_model=model.upstream,
            api_type=model.api_type,
            default=default,
            min_reference_images=model.min_reference_images,
            max_reference_images=model.max_reference_images,
            options=options,
            options_from="config" if not model.auto_options else "provider" if model.discovered else "pending",
        )


class ModelsOut(BaseModel):
    default_model: str | None
    models: list[ModelOut]


class ServerOut(BaseModel):
    version: str
    timezone: str  # of the dates in jobs
    base_url: str  # of the URLs in jobs: PUBLIC_BASE_URL, or the address this request came to
    mcp_url: str | None  # the MCP endpoint (ENABLE_MCP=1); None when it is off
    link_expiry_minutes: float  # an MCP transfer link, and a pending job, expire this long after they are made
    generation_timeout_seconds: float
    max_upload_mb: int  # one reference image
    max_reference_images: int  # per job, whatever the model
    max_active_jobs: int  # per user, pending or generating
    max_prompt_chars: int
    medium_image_px: int  # longest side of the medium size
    models: int
    default_model: str | None


class AccountOut(BaseModel):
    user: str
    jobs: int
    in_progress: int  # pending or generating
    storage_bytes: int  # records, images and reference images


class InfoOut(BaseModel):
    server: ServerOut
    account: AccountOut


# --- helpers shared with the MCP server ---


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _service(request: Request) -> JobService:
    return request.app.state.service


def _storage(request: Request) -> Storage:
    return request.app.state.storage


def _links(request: Request) -> LinkService:
    return request.app.state.links


def _source(request: Request) -> str:
    return "web" if getattr(request.state, "via_cookie", False) else "api"


@contextmanager
def _uploads_cleaned(paths: list[Path]) -> Iterator[None]:
    """Remove the temporary files of uploads that did not end up in a job."""
    try:
        yield
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


async def received_references(request: Request, parts: list[UploadFile | str] | None, paths: list[Path]) -> list[Upload]:
    """The uploaded reference images, copied to temporary files (whose paths are added to
    `paths`) and checked: size, type and content."""
    if any(isinstance(part, str) and part for part in parts or []):
        raise InvalidInput("'reference' must be an image file (as curl -F reference=@image.png sends it), not text")
    # Starlette's UploadFile: the form parser makes those, of which FastAPI's is a subclass.
    files = [part for part in parts or [] if isinstance(part, StarletteUploadFile) and (part.filename or part.size)]
    if len(files) > MAX_REFERENCES:
        raise InvalidInput(f"at most {MAX_REFERENCES} reference images per job, got {len(files)}")
    limit = _settings(request).max_upload_bytes
    uploads = []
    for file in files:
        path = _storage(request).temp_path("reference")
        paths.append(path)
        size = await run_in_threadpool(_copy_upload, file.file, path, limit)
        try:
            info = await run_in_threadpool(lambda: images.inspect(path.read_bytes()))
        except InvalidInput as exc:
            raise InvalidInput(f"{_display_name(file.filename)}: {exc}") from None
        uploads.append(Upload(path=path, original_name=_original_name(file.filename), info=info, size=size))
    return uploads


def _copy_upload(source: BinaryIO, target: Path, limit: int) -> int:
    size = 0
    with target.open("wb") as output:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise InvalidInput(f"a reference image exceeds the {limit // 1024**2} MB limit (MAX_UPLOAD_MB)")
            output.write(chunk)
    return size


def _original_name(name: str | None) -> str | None:
    base = _CONTROL.sub("", re.split(r"[/\\]", name or "")[-1]).strip()[:200]
    return base or None


def _display_name(name: str | None) -> str:
    return _original_name(name) or "a reference image"


async def create_job(request: Request, user: str, new_job: NewJob, files: list[UploadFile | str] | None, wait: int) -> Job:
    """Create a job (and wait for it), as POST /api/jobs and the MCP server do."""
    paths: list[Path] = []
    with _uploads_cleaned(paths):
        uploads = await received_references(request, files, paths)
        job = await _service(request).create(user, new_job, uploads)
    if wait and job.status is Status.GENERATING:
        job = await _service(request).wait(user, job.id, wait, stop=request.is_disconnected)
    return job


async def add_references(request: Request, user: str, job_id: str, files: list[UploadFile | str] | None) -> Job:
    paths: list[Path] = []
    with _uploads_cleaned(paths):
        uploads = await received_references(request, files, paths)
        return await _service(request).add_references(user, job_id, uploads)


def new_job(request: Request, prompt: str, model: str | None, options: dict[str, object], reference_count: int, source: str) -> NewJob:
    return NewJob(
        prompt=prompt,
        model=model,
        options=options,
        reference_count=reference_count,
        source=source,
        client_ip=client_ip(request),
        user_agent=_CONTROL.sub("", request.headers.get("user-agent", "")),
    )


def picture_file(request: Request, user: str, job_ref: str, kind: Kind, index: int) -> tuple[Job, Picture, Path]:
    """One image of a job of the user, after the checks of every download: the API's, the
    links', and the MCP server's before it makes a link."""
    job = _service(request).get(user, job_ref)
    items = job.images if kind is Kind.IMAGES else job.references
    picture = next((item for item in items if item.index == index), None)
    if picture is None:
        what = "image" if kind is Kind.IMAGES else "reference image"
        raise NotFound(f"job {job.id} has no {what} {index}" + ("" if job.status.finished or kind is Kind.REFERENCES else f" yet: it is {job.status}"))
    storage = _storage(request)
    if kind is Kind.IMAGES:
        path = storage.user_dir(user) / picture.filename
    else:
        path = storage.reference_dir(user, job.id) / picture.filename
    if not path.is_file():
        raise NotFound("the image file is missing on the server")
    return job, picture, path


async def picture_response(
    request: Request, user: str, job_ref: str, kind: Kind, index: int, size: Size,
    on_sent: Callable[[], None] | None = None,
) -> Response:  # fmt: skip
    """An image of a job, full size or medium. `on_sent` runs once all of it has been sent."""
    job, picture, path = await run_in_threadpool(picture_file, request, user, job_ref, kind, index)
    name = picture.filename if kind is Kind.IMAGES else f"{job.id}_reference_{picture.filename}"
    headers = dict(_IMAGE_HEADERS)
    if job_ref == "last":  # which job that is changes: never kept
        headers["Cache-Control"] = "no-cache"
    if size is Size.MEDIUM:
        medium = await run_in_threadpool(images.medium, path, picture.content_type, _settings(request).medium_image_px)
        if medium is not None:
            content, content_type = medium
            stem = name.rsplit(".", 1)[0]
            response: Response = _BytesResponse(content, media_type=content_type, headers=headers)
            response.headers["Content-Disposition"] = f'inline; filename="{stem}_medium{images.TYPES[content_type]}"'
            response.on_sent = on_sent  # type: ignore[attr-defined]
            return response
    response = _FileResponse(path, media_type=picture.content_type, filename=name, content_disposition_type="inline", headers=headers)
    response.on_sent = on_sent
    return response


class _OnSent:
    """A response that can tell when all of it has been sent: it then runs `on_sent`,
    unless the client went away first or asked for part of the file (a Range request). Sent
    means handed to the network; behind a reverse proxy, received by the proxy. No server
    can know more without the client confirming."""

    on_sent: Callable[[], None] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.on_sent is None:
            await super().__call__(scope, receive, send)  # type: ignore[misc]
            return
        status_code = 0
        sent = disconnected = False

        async def watched_receive() -> Message:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not sent:  # after the end, the server says so too
                disconnected = True
            return message

        async def watched_send(message: Message) -> None:
            nonlocal status_code, sent
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                sent = True

        await super().__call__(scope, watched_receive, watched_send)  # type: ignore[misc]
        if sent and not disconnected and status_code == status.HTTP_200_OK:
            await run_in_threadpool(self.on_sent)


class _FileResponse(_OnSent, FileResponse):
    pass


class _BytesResponse(_OnSent, Response):
    pass


def _options(**values: object) -> dict[str, object]:
    return {name: value for name, value in values.items() if value is not None and value != ""}


# --- endpoints ---


@router.get("/health", tags=["status"])
async def health() -> dict[str, str]:  # async: answers even while the thread pool is busy
    return {"status": "ok"}


@router.get("/info", tags=["status"])
def server_info(request: Request, user: str = Depends(current_user)) -> InfoOut:
    """The server's settings and limits, and your account."""
    settings = _settings(request)
    catalog = request.app.state.models.catalog()
    service = _service(request)
    return InfoOut(
        server=ServerOut(
            version=__version__,
            timezone=settings.timezone.key,
            base_url=base_url(request),
            mcp_url=f"{base_url(request)}{MCP_PATH}" if settings.enable_mcp else None,
            link_expiry_minutes=settings.link_expiry_seconds / 60,
            generation_timeout_seconds=settings.generation_timeout_seconds,
            max_upload_mb=settings.max_upload_bytes // 1024**2,
            max_reference_images=MAX_REFERENCES,
            max_active_jobs=settings.max_active_jobs,
            max_prompt_chars=MAX_PROMPT_CHARS,
            medium_image_px=settings.medium_image_px,
            models=len(catalog.models),
            default_model=catalog.default,
        ),
        account=AccountOut(
            user=user,
            jobs=service.count(user),
            in_progress=service.active_count(user),
            storage_bytes=_storage(request).size_of(user),
        ),
    )


@router.get("/models", tags=["status"])
def list_models(request: Request, user: str = Depends(current_user)) -> ModelsOut:
    """The models you can generate with, the options each accepts, and their allowed values."""
    catalog = request.app.state.models.catalog()
    return ModelsOut(
        default_model=catalog.default,
        models=[ModelOut.of(model, name == catalog.default) for name, model in catalog.models.items()],
    )


@router.get("/stats", tags=["status"])
def get_stats(request: Request, user: str = Depends(current_user)) -> StatsOut:
    """Your statistics, computed from all your jobs: counts, images, cost, tokens, durations,
    by model and by month."""
    return statistics(user, _service(request).all(user), _settings(request).timezone)


@router.post("/jobs", status_code=status.HTTP_201_CREATED, tags=["jobs"])
async def submit(
    request: Request,
    prompt: Annotated[str, Form(description="What to generate")],
    model: Annotated[str | None, Form(description="A model's name (GET /api/models); the default one when omitted")] = None,
    aspect_ratio: Annotated[str | None, Form()] = None,
    resolution: Annotated[str | None, Form()] = None,
    size: Annotated[str | None, Form()] = None,
    quality: Annotated[str | None, Form()] = None,
    background: Annotated[str | None, Form()] = None,
    output_format: Annotated[str | None, Form()] = None,
    output_compression: Annotated[str | None, Form()] = None,
    n: Annotated[str | None, Form(description="Number of images")] = None,
    seed: Annotated[str | None, Form()] = None,
    reference_count: Annotated[
        int, Form(ge=0, le=MAX_REFERENCES, description="Reference images to upload later (POST /api/jobs/{id}/references)")
    ] = 0,
    reference: References = None,
    wait: Wait = 0,
    user: str = Depends(current_user),
) -> JobOut:
    """Generate images: from a prompt, and reference images sent along (or announced with
    reference_count and uploaded later). The options must be among the model's."""
    options = _options(
        aspect_ratio=aspect_ratio, resolution=resolution, size=size, quality=quality, background=background,
        output_format=output_format, output_compression=output_compression, n=n, seed=seed,
    )  # fmt: skip
    asked = new_job(request, prompt, model, options, reference_count, _source(request))
    return JobOut.of(await create_job(request, user, asked, reference, wait), request)


@router.get("/jobs", tags=["jobs"])
def list_jobs(
    request: Request,
    page: int = Query(1, ge=1, le=MAX_PAGE, description="Page number; page 1 has the newest jobs"),
    per_page: int = Query(PER_PAGE, ge=1, le=MAX_PER_PAGE, description="Jobs per page"),
    user: str = Depends(current_user),
) -> JobListOut:
    """Your jobs, newest first, one page at a time. A page past the last one is empty."""
    jobs, total = _service(request).page(user, (page - 1) * per_page, per_page)
    return JobListOut(jobs=[JobOut.of(job, request) for job in jobs], total=total, page=page, per_page=per_page)


@router.get("/jobs/{job}", tags=["jobs"])
async def get_job(request: Request, job: JobRef, wait: Wait = 0, user: str = Depends(current_user)) -> JobOut:
    """A job; `last` is your newest. With `wait`, answers when the job finishes, or after
    `wait` seconds."""
    found = await _service(request).wait(user, job, wait, stop=request.is_disconnected)
    return JobOut.of(found, request)


@router.post("/jobs/{job}/references", tags=["jobs"])
async def upload_references(request: Request, job: JobId, reference: References = None, user: str = Depends(current_user)) -> JobOut:
    """Upload reference images announced by a pending job (reference_count). The job starts
    once all of them are in."""
    return JobOut.of(await add_references(request, user, job, reference), request)


@router.get("/jobs/{job}/images/{index}", tags=["images"], response_class=Response)
async def download_image(
    request: Request, job: JobRef, index: Index, size: Size = Size.FULL, user: str = Depends(current_user)
) -> Response:
    """A generated image: `size=full` (as generated) or `size=medium` (scaled down)."""
    return await picture_response(request, user, job, Kind.IMAGES, index, size)


@router.get("/jobs/{job}/references/{index}", tags=["images"], response_class=Response)
async def download_reference(
    request: Request, job: JobRef, index: Index, size: Size = Size.FULL, user: str = Depends(current_user)
) -> Response:
    """A reference image of a job, full size (as uploaded) or medium."""
    return await picture_response(request, user, job, Kind.REFERENCES, index, size)


# --- transfer links (see links.py), served when ENABLE_MCP=1 ---


class _LinkRoute(APIRoute):
    """A route under /api/links/{secret}. It checks the link as soon as the request arrives,
    before an upload is read: a refused upload is not received, and a transfer that starts
    while the link is valid may end after it expires."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def check_then_handle(request: Request) -> Response:
            request.state.link = await run_in_threadpool(_checked_link, request)
            return await handle(request)

        return check_then_handle


def _checked_link(request: Request) -> Link:
    """The link in the request's path, if it may serve this request; raises LinkRefused otherwise."""
    link = _links(request).check(request.path_params["secret"], client_ip(request), upload=request.method == "POST")
    if not registry(request).exists(link.user):  # removed from the users file since
        raise LinkRefused(f"link of {link.user!r}, who is no longer a user")
    request.state.user = link.user  # for the access log
    return link


link_router = APIRouter(prefix="/api/links", route_class=_LinkRoute, tags=["links"])
LinkSecret = Annotated[str, PathParameter(description="The link's secret, as the MCP server gave it")]


@link_router.post("/{secret}")
async def upload_through_link(request: Request, secret: LinkSecret, reference: References = None) -> JobOut:
    """Upload a pending job's reference images, as POST /api/jobs/{id}/references does,
    through an upload link of the MCP server instead of a token. A refused upload leaves
    the link valid."""
    link: Link = request.state.link
    with _links(request).using(link):
        job = await add_references(request, link.user, link.target.job_id, reference)
    return JobOut.of(job, request, urls=False)


@link_router.get("/{secret}", response_class=Response)
async def download_through_link(request: Request, secret: LinkSecret) -> Response:
    """An image, as the API's downloads give it, through a download link of the MCP server
    instead of a token. A download cut off before its end leaves the link valid."""
    link: Link = request.state.link
    target = link.target
    # A link serves one whole image: Range requests (parts) are answered with all of it, which uses it up.
    request.scope["headers"] = [(name, value) for name, value in request.scope["headers"] if name != b"range"]
    return await picture_response(
        request, link.user, target.job_id, Kind(target.kind), target.index, Size(target.size),
        on_sent=partial(_links(request).use_up, link),
    )  # fmt: skip
