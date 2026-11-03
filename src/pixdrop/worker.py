"""The generations: one asyncio task per job, all running at the same time.

A generation is mostly waiting for the provider's answer, which the asynchronous HTTP
client does without holding up anything else: the server keeps answering requests (and
other generations go on) meanwhile. Each task loads the job's reference images, asks the
provider, checks and saves the images it returns, and records the result. Whatever goes
wrong, the job ends as failed with the reason: a provider's error, GENERATION_TIMEOUT_SECONDS
passing, or the server stopping (whose interrupted jobs are failed at the next start, too).
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import images, logs
from .config import ModelRegistry
from .errors import InvalidInput
from .jobs import JobService
from .providers import GenerationError, ProviderClient
from .storage import Storage, write_file
from .store import Job, Picture, Usage

log = logging.getLogger(__name__)

INTERRUPTED_BY_STOP = "interrupted: the server stopped during the generation"


class Worker:
    def __init__(
        self, service: JobService, storage: Storage, models: ModelRegistry, client: ProviderClient, timeout_seconds: float
    ) -> None:
        self._service = service
        self._storage = storage
        self._models = models
        self._client = client
        self._timeout = timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, job: Job) -> None:
        """Generate the job's images in a new task (called from the event loop)."""
        task = asyncio.get_running_loop().create_task(self.run(job), name=f"generate {job.id}")
        self._tasks.add(task)  # keeps the task alive until it is done
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        """Stop the running generations, whose jobs fail as interrupted."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, job: Job) -> None:
        start = time.monotonic()
        generation = None
        completing = False
        try:
            async with asyncio.timeout(self._timeout):
                model = self._models.catalog().models.get(job.model)
                if model is None:
                    raise GenerationError(f"the model {job.model!r} is no longer configured on this server")
                if (model.provider.name, model.upstream, model.api_type) != (job.provider, job.upstream_model, job.api_type):
                    raise GenerationError(f"the configuration of the model {job.model!r} changed since the job was made; submit it again")
                references = await asyncio.to_thread(self._references, job)
                generation = await self._client.generate(model, job.prompt, job.options, references)
            took = time.monotonic() - start
            pictures = await asyncio.to_thread(self._save_images, job, generation.images)
            completing = True  # from here on the job ends completed, even if the server stops meanwhile
            finished = await asyncio.shield(
                self._service.complete(
                    job, images=pictures, usage=generation.usage, text=generation.text,
                    upstream_id=generation.upstream_id, duration=took,
                )
            )  # fmt: skip
            cost = finished.usage.cost if finished.usage else None
            spent = f", ${cost:.4f}" if cost is not None else ""
            log.info("job %s (%s): %s, completed in %.1f s, %d image(s)%s", job.id, job.user, job.model, took, len(pictures), spent)
        except asyncio.CancelledError:
            if not completing:
                await asyncio.shield(self._fail(job, INTERRUPTED_BY_STOP, start, generation.usage if generation else None))
            raise
        except GenerationError as exc:
            await self._fail(job, str(exc), start, exc.usage)
        except InvalidInput as exc:  # an image the provider returned
            usage = generation.usage if generation else None
            await self._fail(job, f"the provider returned an unusable image: {exc}", start, usage)
        except TimeoutError:
            await self._fail(job, f"no image after {self._timeout:g} seconds (GENERATION_TIMEOUT_SECONDS)", start)
        except Exception:
            log.exception("job %s (%s): unexpected error", job.id, job.user)
            await self._fail(job, "internal error while generating; see the server log", start)

    async def _fail(self, job: Job, error: str, start: float, usage: Usage | None = None) -> None:
        """Record the failure, with the usage the provider reported if it answered (it may charge)."""
        took = time.monotonic() - start
        await self._service.fail(job, error, duration=took, usage=usage)
        log.info("job %s (%s): %s, failed after %.1f s: %s", job.id, job.user, job.model, took, logs.one_line(error))

    def _references(self, job: Job) -> list[str]:
        directory = self._storage.reference_dir(job.user, job.id)
        return [images.data_url(directory / picture.filename, picture.content_type) for picture in job.references]

    def _save_images(self, job: Job, contents: list[bytes]) -> list[Picture]:
        """Check every image first (InvalidInput if one is unusable), then save them all."""
        checked = [(images.inspect(content), content) for content in contents]
        pictures = []
        for index, (info, content) in enumerate(checked, start=1):
            path = self._storage.image_path(job.user, job.id, index, info.extension)
            write_file(path, content)
            pictures.append(
                Picture(
                    index=index, filename=path.name, content_type=info.content_type, bytes=len(content),
                    width=info.width, height=info.height,
                )  # fmt: skip
            )
        return pictures
