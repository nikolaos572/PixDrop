"""The job lifecycle (jobs.JobService): accepting requests, reference images, pending jobs and their
expiry, the limits, waiting, and recovering after a restart. With a clock the tests move by hand."""

import asyncio

import pytest
from helpers import CONFIG, Clock, gif, jpeg, png, record_of, write_json

from pixdrop import images
from pixdrop.config import ModelRegistry
from pixdrop.errors import Conflict, InvalidInput, NotFound, TooManyJobs
from pixdrop.jobs import INTERRUPTED_BY_RESTART, MAX_PROMPT_CHARS, JobService, NewJob, Upload, _stem
from pixdrop.storage import Storage
from pixdrop.store import JobStore, Picture, Status, Usage

EXPIRED = "the reference images were not uploaded within 60 minute(s)"


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def storage(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.prepare()
    return storage


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def started():
    """The jobs handed to the worker."""
    return []


@pytest.fixture
def make_service(storage, tmp_path, clock, started):
    def make(max_active_jobs=2):
        models = ModelRegistry(write_json(tmp_path / "config.json", CONFIG), env={})
        service = JobService(JobStore(storage), storage, models, max_active_jobs=max_active_jobs, pending_seconds=3600, clock=clock)
        service.set_starter(started.append)
        return service

    return make


@pytest.fixture
def service(make_service):
    return make_service()


@pytest.fixture
def upload(storage):
    """upload(data, name) -> a checked reference image in the temporary directory, as the API receives it."""

    def make(data=None, name="knight.png"):
        data = png() if data is None else data
        path = storage.temp_path("reference")
        path.write_bytes(data)
        return Upload(path=path, original_name=name, info=images.inspect(data), size=len(data))

    return make


def new(prompt="a red fox", **fields):
    return NewJob(prompt=prompt, **fields)


# --- accepting a request ---


def test_a_job_without_references_starts_at_once(service, storage, clock, started):
    job = run(service.create("alice", new("  a red fox  ", source="web", client_ip="203.0.113.7", user_agent="curl/8.5.0"), []))
    assert job.status is Status.GENERATING
    assert (job.prompt, job.model, job.provider, job.upstream_model, job.api_type) == (
        "a red fox", "gpt-image", "openrouter", "openai/gpt-5-image", "images",
    )  # fmt: skip
    assert dict(job.options) == {"quality": "high"}  # the model's defaults
    assert (job.source, job.client_ip, job.user_agent, job.reference_count) == ("web", "203.0.113.7", "curl/8.5.0", 0)
    assert job.created_at == job.started_at == clock.now
    assert started == [job]
    assert service.get("alice", job.id) == job  # written at once
    assert record_of(storage.root, "alice", job.id)["status"] == "generating"
    assert service.active_count("alice") == 1


def test_options_and_model_are_resolved(service):
    job = run(service.create("alice", new(model="nano-banana", options={"aspect_ratio": "16:9", "seed": "42", "resolution": None}), []))
    assert (job.model, job.api_type, dict(job.options)) == ("nano-banana", "chat", {"aspect_ratio": "16:9", "seed": 42})


def test_reference_images_are_stored_with_the_job(service, storage, upload, started):
    uploads = [upload(png(64, 48), "knight.png"), upload(jpeg(), "C:\\fakepath\\photo.png"), upload(gif(), None)]
    job = run(service.create("alice", new(), uploads))
    assert job.status is Status.GENERATING and started == [job]
    assert job.reference_count == 3
    assert tuple(job.references) == (  # (create() gives a list, where the dataclass declares a tuple)
        Picture(1, "1_knight.png", "image/png", len(png(64, 48)), 64, 48, original_name="knight.png"),
        Picture(2, "2_photo.jpg", "image/jpeg", len(jpeg()), 64, 64, original_name="C:\\fakepath\\photo.png"),  # its real type
        Picture(3, "3_reference.gif", "image/gif", len(gif()), 64, 64, original_name=None),
    )
    directory = storage.reference_dir("alice", job.id)
    assert sorted(path.name for path in directory.iterdir()) == ["1_knight.png", "2_photo.jpg", "3_reference.gif"]
    assert (directory / "1_knight.png").read_bytes() == png(64, 48)
    assert not any(item.path.exists() for item in uploads)  # moved, not copied
    assert service.get("alice", job.id).references == tuple(job.references)


def test_announced_references_make_the_job_wait(service, upload, started, clock):
    job = run(service.create("alice", new(reference_count=2), []))
    assert (job.status, job.reference_count, job.references, job.started_at) == (Status.PENDING, 2, (), None)
    assert started == []
    clock.now += 60
    job = run(service.add_references("alice", job.id, [upload(png(), "front.png")]))
    assert (job.status, [picture.filename for picture in job.references]) == (Status.PENDING, ["1_front.png"])
    assert started == []
    clock.now += 60
    back = png(32, 32)
    job = run(service.add_references("alice", job.id, [upload(back, "back.png")]))
    assert job.status is Status.GENERATING
    assert [picture.filename for picture in job.references] == ["1_front.png", "2_back.png"]  # numbered on
    assert (job.references[1].bytes, job.references[1].width) == (len(back), 32)
    assert job.started_at == clock.now
    assert started == [job]
    assert service.get("alice", job.id) == job


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t"])
def test_an_empty_prompt_is_refused(service, storage, prompt):
    with pytest.raises(InvalidInput, match="^the prompt is empty$"):
        run(service.create("alice", new(prompt), []))
    assert storage.job_ids("alice") == []


def test_the_prompt_length_limit(service):
    assert len(run(service.create("alice", new(" " + "x" * MAX_PROMPT_CHARS + "\n"), [])).prompt) == 32_000
    with pytest.raises(InvalidInput, match="^the prompt is too long: 32001 characters \\(at most 32000\\)$"):
        run(service.create("alice", new("x" * 32_001), []))


@pytest.mark.parametrize(
    ("model", "count", "uploads", "message"),
    [
        ("gpt-image", 3, 2, "^reference_count is 3 but 2 reference image\\(s\\) were sent$"),
        ("gpt-image", 1, 2, "^reference_count is 1 but 2 reference image\\(s\\) were sent$"),
        ("nano-banana", 4, 0, "^model 'nano-banana' accepts at most 3 reference image\\(s\\), got 4$"),
        ("nano-banana", 0, 4, "^model 'nano-banana' accepts at most 3 reference image\\(s\\), got 4$"),
        ("plain", 0, 1, "^model 'plain' accepts at most 0 reference image\\(s\\), got 1$"),
        ("gpt-image", 17, 0, "accepts at most 16 reference image"),
        ("gpt-image", -1, 0, "^reference_count cannot be negative$"),
    ],
)
def test_reference_count_rules(service, storage, upload, model, count, uploads, message):
    with pytest.raises(InvalidInput, match=message):
        run(service.create("alice", new(model=model, reference_count=count), [upload() for _ in range(uploads)]))
    assert storage.job_ids("alice") == []


@pytest.mark.parametrize("count", [0, 2])
def test_reference_count_may_repeat_the_number_of_uploads(service, upload, count):
    job = run(service.create("alice", new(reference_count=count), [upload(), upload()]))
    assert (job.status, job.reference_count) == (Status.GENERATING, 2)


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        (new(model="dall-e"), "unknown model 'dall-e'"),
        (new(options={"quality": "ultra"}), "quality must be one of"),
        (new(model="nano-banana", options={"quality": "high"}), "does not support 'quality'"),
    ],
)
def test_invalid_requests_make_no_job(service, storage, started, request_, message):
    with pytest.raises(InvalidInput, match=message):
        run(service.create("alice", request_, []))
    assert (storage.job_ids("alice"), started) == ([], [])


def test_the_user_agent_is_kept_short(service):
    assert len(run(service.create("alice", new(user_agent="x" * 1000), [])).user_agent) == 300


# --- the limit of jobs in progress ---


def test_at_most_max_active_jobs_per_user(service, storage):
    first = run(service.create("alice", new(), []))
    run(service.create("alice", new(reference_count=1), []))  # pending jobs count too
    with pytest.raises(TooManyJobs, match="^you have 2 job\\(s\\) in progress, the most allowed \\(MAX_ACTIVE_JOBS\\); wait until one finishes$"):
        run(service.create("alice", new(), []))
    assert len(storage.job_ids("alice")) == 2
    run(service.create("bob", new(), []))  # each user has their own
    run(service.fail(first, "boom"))
    assert service.active_count("alice") == 1
    run(service.create("alice", new(), []))


# --- adding reference images to a pending job ---


def test_reference_images_for_a_job_that_cannot_take_them(service, upload):
    pending = run(service.create("alice", new(reference_count=2), []))
    with pytest.raises(InvalidInput, match="^no reference image was sent: add them as form fields named 'reference'$"):
        run(service.add_references("alice", pending.id, []))
    with pytest.raises(InvalidInput, match="^the job expects 2 more reference image\\(s\\), got 3$"):
        run(service.add_references("alice", pending.id, [upload(), upload(), upload()]))
    with pytest.raises(NotFound, match="^no such job$"):
        run(service.add_references("bob", pending.id, [upload()]))  # alice's
    with pytest.raises(NotFound, match="^no such job$"):
        run(service.add_references("alice", "1793456280000_0000", [upload()]))
    generating = run(service.create("alice", new(), []))
    with pytest.raises(Conflict, match="^the job is generating: reference images can be added only while it is pending$"):
        run(service.add_references("alice", generating.id, [upload()]))
    assert service.get("alice", pending.id).references == ()


# --- pending jobs expire ---


def test_a_pending_job_fails_when_its_references_do_not_come_in_time(service, clock):
    job = run(service.create("alice", new(reference_count=1), []))
    clock.now += 3599
    assert run(service.expire_pending()) == 0
    clock.now += 1
    assert run(service.expire_pending()) == 1
    failed = service.get("alice", job.id)
    assert (failed.status, failed.error, failed.finished_at) == (Status.FAILED, EXPIRED, clock.now)
    assert service.active_count("alice") == 0
    assert run(service.expire_pending()) == 0


def test_references_after_the_deadline_are_refused_even_before_the_check(service, clock, upload, started):
    job = run(service.create("alice", new(reference_count=1), []))
    clock.now += 3600
    with pytest.raises(Conflict, match="^the job is failed: reference images can be added only while it is pending$"):
        run(service.add_references("alice", job.id, [upload()]))
    assert (service.get("alice", job.id).status, service.get("alice", job.id).error) == (Status.FAILED, EXPIRED)
    assert started == []


def test_waiting_on_a_pending_job_returns_at_once(service, clock):
    # Only the upload of its references moves a pending job on: waiting for it would only block the caller.
    job = run(service.create("alice", new(reference_count=1), []))
    assert run(asyncio.wait_for(service.wait("alice", job.id, 30), 1)).status is Status.PENDING
    clock.now += 3600
    run(service.expire_pending())
    assert run(service.wait("alice", job.id, 30)).status is Status.FAILED


# --- the end of a job ---


def test_complete_records_the_results(service, storage, clock):
    job = run(service.create("alice", new(), []))
    clock.now += 12
    picture = Picture(1, f"{job.id}.png", "image/png", 10, 1024, 1024)
    done = run(service.complete(job, images=[picture], usage=Usage(1, 2, 3, 0.04), text="Here.", upstream_id="gen-1", duration=11.98751))
    assert (done.status, done.images, done.usage, done.text, done.upstream_id) == (Status.COMPLETED, (picture,), Usage(1, 2, 3, 0.04), "Here.", "gen-1")
    assert (done.duration_seconds, done.finished_at, done.error) == (11.988, clock.now, None)
    assert service.get("alice", job.id) == done
    assert service.active_count("alice") == 0


def test_fail_records_the_reason(service):
    job = run(service.create("alice", new(), []))
    failed = run(service.fail(job, "openrouter did not answer in time", duration=180.00049))
    assert (failed.status, failed.error, failed.duration_seconds) == (Status.FAILED, "openrouter did not answer in time", 180.0)
    assert run(service.fail(run(service.create("alice", new(), [])), "x")).duration_seconds is None


# --- reading and waiting ---


def test_get_and_last(service, clock):
    with pytest.raises(NotFound, match="^you have no jobs yet$"):
        service.get("alice", "last")
    first = run(service.create("alice", new("first"), []))
    clock.now += 1  # a later millisecond
    second = run(service.create("alice", new("second"), []))
    assert service.get("alice", "last") == second
    assert service.get("alice", first.id) == first
    with pytest.raises(NotFound, match="^no such job$"):
        service.get("bob", first.id)
    assert service.count("alice") == 2
    assert [job.id for job in service.page("alice", 0, 1)[0]] == [second.id]


def test_wait_returns_a_finished_job_at_once(service):
    job = run(service.fail(run(service.create("alice", new(), [])), "boom"))
    assert run(asyncio.wait_for(service.wait("alice", job.id, 600), 1)) == job


def test_wait_returns_the_job_as_it_is_when_the_time_is_up(service):
    job = run(service.create("alice", new(), []))
    assert run(service.wait("alice", job.id, 0.05)).status is Status.GENERATING
    assert run(service.wait("alice", "last", 0)).status is Status.GENERATING


def test_wait_returns_as_soon_as_the_job_finishes(service):
    async def scenario():
        job = await service.create("alice", new(), [])
        waiting = asyncio.create_task(service.wait("alice", "last", 600))
        await asyncio.sleep(0.01)
        await service.complete(job, images=[], usage=None, text=None, upstream_id=None, duration=1)
        return await asyncio.wait_for(waiting, 2)

    assert run(scenario()).status is Status.COMPLETED


def test_wait_for_a_missing_job(service):
    with pytest.raises(NotFound):
        run(service.wait("alice", "1793456280000_0000", 10))


# --- recovering after a restart ---


def test_recovery_fails_interrupted_and_expired_jobs_and_keeps_the_others(storage, make_service, clock, upload, started):
    store = JobStore(storage)
    fields = {"prompt": "x", "model": "gpt-image", "provider": "openrouter", "upstream_model": "openai/gpt-5-image", "api_type": "images"}
    generating = store.create("alice", clock.now - 30, status=Status.GENERATING, **fields)
    expired = store.create("alice", clock.now - 3600, status=Status.PENDING, reference_count=1, **fields)
    waiting = store.create("bob", clock.now - 600, status=Status.PENDING, reference_count=1, **fields)
    completed = store.create("alice", clock.now - 7200, status=Status.COMPLETED, **fields)
    failed = store.create("alice", clock.now - 7300, status=Status.FAILED, error="boom", **fields)

    service = make_service(max_active_jobs=1)
    assert service.recover() == (2, 1)
    assert (store.get("alice", generating.id).status, store.get("alice", generating.id).error) == (Status.FAILED, INTERRUPTED_BY_RESTART)
    assert INTERRUPTED_BY_RESTART.startswith("interrupted: the server restarted")
    assert store.get("alice", generating.id).finished_at == clock.now
    assert (store.get("alice", expired.id).status, store.get("alice", expired.id).error) == (Status.FAILED, EXPIRED)
    assert store.get("alice", completed.id) == completed
    assert store.get("alice", failed.id) == failed

    assert store.get("bob", waiting.id).status is Status.PENDING  # still waiting for its references
    assert service.active_count("bob") == 1
    with pytest.raises(TooManyJobs):  # it counts as in progress
        run(service.create("bob", new(), []))
    resumed = run(service.add_references("bob", waiting.id, [upload()]))
    assert resumed.status is Status.GENERATING and started == [resumed]


def test_a_kept_pending_job_expires_at_its_original_deadline(storage, make_service, clock):
    fields = {"prompt": "x", "model": "gpt-image", "provider": "openrouter", "upstream_model": "openai/gpt-5-image", "api_type": "images"}
    waiting = JobStore(storage).create("alice", clock.now - 600, status=Status.PENDING, reference_count=1, **fields)
    service = make_service()
    service.recover()
    clock.now += 3000 - 1
    assert run(service.expire_pending()) == 0
    clock.now += 1
    assert run(service.expire_pending()) == 1
    assert service.get("alice", waiting.id).error == EXPIRED


# --- names of reference files ---


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("knight.png", "knight"),
        ("C:\\Users\\me\\photo.final.jpg", "photo.final"),
        ("../../etc/passwd", "passwd"),
        ("my sprite (v2).PNG", "my-sprite-v2"),
        ("εικόνα.png", "reference"),
        (".hidden", "hidden"),
        ("...", "reference"),
        ("-x-.png", "x"),
        ("a" * 100 + ".png", "a" * 60),
        ("", "reference"),
        (None, "reference"),
    ],
)
def test_reference_file_names_are_safe(name, stem):
    assert _stem(name) == stem


def test_a_record_that_cannot_be_written_still_frees_the_users_slot(service, monkeypatch):
    job = run(service.create("alice", new(), []))
    assert service.active_count("alice") == 1

    def broken(job):
        raise OSError("disk full")

    monkeypatch.setattr(service._store, "save", broken)
    with pytest.raises(OSError):
        run(service.complete(job, images=[], usage=None, text=None, upstream_id=None, duration=1))
    assert service.active_count("alice") == 0  # the next start fails the job left as generating


def test_references_that_cannot_be_stored_fail_the_new_job(service, upload, storage, monkeypatch):
    def broken(job, uploads):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_store_references", broken)
    with pytest.raises(OSError):
        run(service.create("alice", new(), [upload(png(), "a.png")]))
    (job,) = service.all("alice")
    assert (job.status, job.error) == (Status.FAILED, "internal error while storing the reference images; see the server log")
    assert service.active_count("alice") == 0
