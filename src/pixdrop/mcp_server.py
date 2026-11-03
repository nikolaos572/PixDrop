"""The MCP server at /mcp (ENABLE_MCP=1): the API's operations as tools for AI agents.

An agent (Claude Code, Codex, Antigravity, ...) connects with the user's token, in the
Authorization or X-API-Key header or as ?token= in the URL, and finds the tools itself.
Each tool runs the code of the API endpoint it stands for, so the checks, the limits and
the jobs are those of the API and the web page. Tool calls carry only JSON, and images
would fill the model's context as base64, so images travel through transfer links
instead (see links.py): a completed job comes with links and curl commands for its images
(full size and medium), and a job waiting for reference images with an upload command.

The endpoint is stateless and answers in plain JSON, without event streams: each call is
authenticated on its own, and any reverse proxy passes the answers through.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from . import __version__, api, images, logs
from .auth import agent_user
from .client import client_ip
from .config import MAX_REFERENCES, OPTIONS
from .errors import Conflict, InvalidInput, NotFound, TooManyJobs
from .jobs import MAX_PROMPT_CHARS
from .links import LinkService, Target
from .settings import Settings
from .stats import StatsOut
from .store import Job, Status

MAX_WAIT_SECONDS = 50  # per call: reverse proxies commonly give up on an answer after 60 seconds

INSTRUCTIONS = """\
PixDrop generates images with AI models. Images never travel through tool calls: you \
download them with curl, through short-lived links.

To generate an image:
1. Call list_models to pick a model and see the options it accepts (aspect_ratio, quality, \
background, ...); without a model, the server's default is used.
2. Call generate_image with the prompt. It waits up to wait_seconds for the job: call get_job \
with the job's id while its status is "generating".
3. A completed job lists downloads: for each image, a curl command for the full size and one \
for the medium size (scaled down to fit {medium} px), which is cheaper to look at. Run the one \
you need, changing the -o path to where the file should go.

To use reference images (image-to-image or text+image-to-image, such as the other views of a \
sprite from one view): call generate_image with reference_images set to their number. The job \
waits as "pending": run the curl command in its upload field, with your files in place of \
REFERENCE_1.png, REFERENCE_2.png, ... (PNG, JPEG, WebP, GIF or SVG, at most {max_mb} MB each). \
The upload starts the generation; then call get_job.

A link works for {minutes:g} minutes, only from this computer, for one complete transfer; a \
transfer that fails or is cut off leaves it valid, for another try. A link refused with 404 has \
expired or been used: call download_link or upload_link for a new one. Jobs are shared with the \
user's web page and API, and are never deleted.\
"""

# Hints for clients, which may run read-only tools without asking.
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_MAKES_LINK = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_GENERATES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

JobIdArgument = Annotated[str, Field(pattern=rf"^{api.JOB_ID}$", description="The job's id, e.g. 1793456280412_a9f3")]
JobRefArgument = Annotated[str, Field(pattern=rf"^(last|{api.JOB_ID})$", description="The job's id, or last")]
WaitArgument = Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS, description="How long to wait for the job to finish")]


def _choice(name: str) -> object:
    """The type of an enumerated option's argument. Each model accepts its own values (list_models
    gives them), so the argument is a string, checked against the model's values when the job is made."""
    examples = ", ".join(OPTIONS[name].values[:6])
    return Annotated[str | None, Field(description=f"{OPTIONS[name].description}; one of the model's values (list_models), such as {examples}")]


class LinkOut(BaseModel):
    url: str
    method: Literal["POST", "GET"]
    expires_at: str  # unless used before; in the server's time zone, like the dates of jobs
    command: str  # a curl command line that uses the link


class DownloadOut(LinkOut):
    image: int  # the image's number
    size: Literal["full", "medium"]


class McpJobOut(api.JobOut):
    """A job as the API gives it, without the API's URLs (which need the token), plus links."""

    upload: LinkOut | None  # a pending job: the command that uploads its reference images
    downloads: list[DownloadOut]  # a completed job: full and medium size of each image


class McpEndpoint:
    """The ASGI app of the route /mcp: authenticates each request, then lets the SDK answer it."""

    def __init__(self, settings: Settings) -> None:
        # The SDK logs each request at INFO; the access log has them already.
        logging.getLogger("mcp").setLevel(logging.WARNING)
        server = _server(settings)
        # This builds the SDK's session manager; the Starlette app it also returns is not used, since
        # the endpoint is a route of PixDrop's app, with its middleware and error handling.
        server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            # The SDK's protection against DNS rebinding would accept only localhost in the Host header.
            # A web page cannot call this endpoint anyway: it needs the token, which browsers do not
            # add by themselves (the web page's cookie is not accepted here).
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        self._sessions = server.session_manager
        self._app = StreamableHTTPASGIApp(self._sessions)

    def running(self) -> AbstractAsyncContextManager[None]:
        """The context in which the endpoint serves requests, entered by the app's lifespan."""
        return self._sessions.run()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await agent_user(Request(scope))  # 401 without a valid token; sets request.state.user for the tools
        await self._app(scope, receive, send)


def _server(settings: Settings) -> MCPServer:
    """The tools. Each gets the HTTP request of its call, authenticated by McpEndpoint, and passes
    it to the API's code, which reads the app's state from it."""
    server = MCPServer(
        "PixDrop",
        title="PixDrop",
        description="Generates images with AI models.",
        version=__version__,
        instructions=INSTRUCTIONS.format(
            medium=settings.medium_image_px,
            max_mb=settings.max_upload_bytes // 1024**2,
            minutes=settings.link_expiry_seconds / 60,
        ),
    )

    @server.tool(title="Server information", annotations=_READ_ONLY)
    def server_info(ctx: Context) -> api.InfoOut:
        """The server's settings and limits (timeout, largest upload, jobs in progress allowed,
        medium size, ...) and your account: your number of jobs and the storage they take."""
        request = _request(ctx)
        return api.server_info(request, request.state.user)

    @server.tool(title="List models", annotations=_READ_ONLY)
    def list_models(ctx: Context) -> api.ModelsOut:
        """The models you can generate with: their description, the default one, how many
        reference images each accepts (some need at least one), and the options each accepts
        with that model's allowed values and defaults. Pass a model's name and options to
        generate_image."""
        request = _request(ctx)
        return api.list_models(request, request.state.user)

    @server.tool(title="Generate an image", annotations=_GENERATES)
    async def generate_image(
        ctx: Context,
        prompt: Annotated[str, Field(min_length=1, max_length=MAX_PROMPT_CHARS, description="What to generate")],
        model: Annotated[str | None, Field(description="A model's name from list_models; the default model when omitted")] = None,
        aspect_ratio: _choice("aspect_ratio") = None,  # type: ignore[valid-type]
        resolution: _choice("resolution") = None,  # type: ignore[valid-type]
        size: Annotated[str | None, Field(description=OPTIONS["size"].description)] = None,
        quality: _choice("quality") = None,  # type: ignore[valid-type]
        background: _choice("background") = None,  # type: ignore[valid-type]
        output_format: _choice("output_format") = None,  # type: ignore[valid-type]
        output_compression: Annotated[
            int | None, Field(ge=0, le=100, description=OPTIONS["output_compression"].description)
        ] = None,
        n: Annotated[int | None, Field(ge=1, le=10, description=OPTIONS["n"].description)] = None,
        seed: Annotated[int | None, Field(ge=0, le=2**32 - 1, description=OPTIONS["seed"].description)] = None,
        reference_images: Annotated[
            int, Field(ge=0, le=MAX_REFERENCES, description="How many reference images you will upload for this job")
        ] = 0,
        wait_seconds: WaitArgument = MAX_WAIT_SECONDS,
    ) -> McpJobOut:
        """Generate images from a prompt, optionally guided by reference images. Pass only the
        options the model accepts, with the values list_models gives for it (they differ between
        models: transparent backgrounds, svg output, quality levels...). Without reference images, the generation
        starts at once and the call waits up to wait_seconds for it: while the status is
        "generating", call get_job. With reference_images, the job is "pending" until you run
        the curl command in its upload field. A completed job lists the downloads of its images;
        a failed one says why in error."""
        request = _request(ctx)
        options = {
            "aspect_ratio": aspect_ratio, "resolution": resolution, "size": size, "quality": quality,
            "background": background, "output_format": output_format, "output_compression": output_compression,
            "n": n, "seed": seed,
        }  # fmt: skip
        asked = api.new_job(request, prompt, model, options, reference_images, "mcp")
        with _tool_errors(request):
            job = await api.create_job(request, request.state.user, asked, None, wait_seconds)
        return _job_out(request, job)

    @server.tool(title="Upload link", annotations=_MAKES_LINK)
    def upload_link(ctx: Context, job_id: JobIdArgument) -> LinkOut:
        """A new link to upload the reference images of a pending job, and the curl command that
        does it: put your files in place of REFERENCE_1.png, ... The upload prints the job, which
        starts generating once all its reference images are in."""
        request = _request(ctx)
        with _tool_errors(request):
            job = request.app.state.service.get(request.state.user, job_id)
            if job.status is not Status.PENDING:
                raise Conflict(f"the job is {job.status}: only a pending job takes reference images")
        return _upload_link(request, job)

    @server.tool(title="Get a job", annotations=_MAKES_LINK)
    async def get_job(ctx: Context, job_id: JobRefArgument, wait_seconds: WaitArgument = MAX_WAIT_SECONDS) -> McpJobOut:
        """A job: its status (pending, generating, completed or failed), prompt, model, options,
        usage and cost, the downloads of its images once completed, and the reason it failed.
        Returns as soon as the job finishes, or after wait_seconds: call again while it is
        generating. "last" is your newest job."""
        request = _request(ctx)
        with _tool_errors(request):
            job = await request.app.state.service.wait(request.state.user, job_id, wait_seconds, stop=request.is_disconnected)
        return _job_out(request, job)

    @server.tool(title="Last job", annotations=_MAKES_LINK)
    async def last_job(ctx: Context, wait_seconds: WaitArgument = 0) -> McpJobOut:
        """Your newest job (also from the web page or the API), as get_job gives it."""
        return await get_job(ctx, "last", wait_seconds)

    @server.tool(title="List jobs", annotations=_READ_ONLY)
    def list_jobs(
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=api.MAX_PAGE, description="Page 1 has the newest jobs")] = 1,
        per_page: Annotated[int, Field(ge=1, le=api.MAX_PER_PAGE, description="Jobs per page")] = 10,
    ) -> api.JobListOut:
        """Your jobs (also those made through the web page or the API), newest first, one page
        at a time, and how many there are. Use get_job or download_link for a job's images."""
        request = _request(ctx)
        jobs, total = request.app.state.service.page(request.state.user, (page - 1) * per_page, per_page)
        return api.JobListOut(
            jobs=[api.JobOut.of(job, request, urls=False) for job in jobs], total=total, page=page, per_page=per_page
        )

    @server.tool(title="Download link", annotations=_MAKES_LINK)
    def download_link(
        ctx: Context,
        job_id: JobRefArgument,
        image: Annotated[int, Field(ge=1, le=MAX_REFERENCES, description="The image's number, from 1")] = 1,
        size: Annotated[Literal["full", "medium"], Field(description="full, or medium: scaled down, cheaper to look at")] = "full",
        reference: Annotated[bool, Field(description="True for one of the job's reference images instead")] = False,
    ) -> LinkOut:
        """A new link to download an image of a job (or a reference image), and the curl command
        that saves it in the current directory."""
        request = _request(ctx)
        kind = api.Kind.REFERENCES if reference else api.Kind.IMAGES
        with _tool_errors(request):  # the download's own checks, before making a link for it
            job, picture, _ = api.picture_file(request, request.state.user, job_id, kind, image)
        filename = picture.filename if kind is api.Kind.IMAGES else f"{job.id}_reference_{picture.filename}"
        return _download_link(request, job, kind, image, size, filename, picture.content_type)

    @server.tool(title="Statistics", annotations=_READ_ONLY)
    def get_stats(ctx: Context) -> StatsOut:
        """Your statistics over all your jobs: counts by status, images, cost in USD, tokens and
        durations, by model and by month."""
        request = _request(ctx)
        return api.get_stats(request, request.state.user)

    return server


def _request(ctx: Context) -> Request:
    return ctx.request_context.request


@contextmanager
def _tool_errors(request: Request) -> Iterator[None]:
    """Turn the API's refusals (no such job, invalid option, ...) into tool errors, which the model reads."""
    try:
        yield
    except (InvalidInput, NotFound, Conflict, TooManyJobs) as exc:
        logs.record_error(request, str(exc))
        raise ToolError(str(exc)) from None
    except HTTPException as exc:
        logs.record_error(request, str(exc.detail))
        raise ToolError(str(exc.detail)) from None


def _job_out(request: Request, job: Job) -> McpJobOut:
    out = api.JobOut.of(job, request, urls=False)
    downloads = []
    if job.status is Status.COMPLETED:
        for picture in job.images:
            for size in ("full", "medium"):
                link = _download_link(request, job, api.Kind.IMAGES, picture.index, size, picture.filename, picture.content_type)
                downloads.append(DownloadOut(image=picture.index, size=size, **link.model_dump()))
    upload = _upload_link(request, job) if job.status is Status.PENDING else None
    return McpJobOut(**out.model_dump(), upload=upload, downloads=downloads)


def _upload_link(request: Request, job: Job) -> LinkOut:
    url, expires_at = _new_link(request, Target(job_id=job.id, upload=True))
    files = " ".join(f"-F reference=@REFERENCE_{index}.png" for index in range(len(job.references) + 1, job.reference_count + 1))
    return LinkOut(url=url, method="POST", expires_at=expires_at, command=f"curl -sS --fail-with-body {files} {shlex.quote(url)}")


def _download_link(
    request: Request, job: Job, kind: api.Kind, index: int, size: str, filename: str, content_type: str
) -> LinkOut:  # fmt: skip
    url, expires_at = _new_link(request, Target(job_id=job.id, kind=kind.value, index=index, size=size))
    if size == "medium":
        stem = filename.rsplit(".", 1)[0]
        filename = f"{stem}_medium{images.medium_extension(content_type)}"
    # --fail, not --fail-with-body: a refused link must not write its error over a file downloaded before.
    command = f"curl -sS --fail -o {shlex.quote(filename)} {shlex.quote(url)}"
    return LinkOut(url=url, method="GET", expires_at=expires_at, command=command)


def _new_link(request: Request, target: Target) -> tuple[str, str]:
    """A link for the client of `request` to transfer `target`: its URL and expiry time."""
    client = client_ip(request)
    if client == "-":
        raise ToolError("the server cannot tell your address, to which links are bound")
    links: LinkService = request.app.state.links
    secret, expires_at = links.create(request.state.user, client, target)
    return f"{api.base_url(request)}/api/links/{secret}", api.local_time(expires_at, request.app.state.settings.timezone)
