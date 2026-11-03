"""Job records: one JSON file per job, its fields, and records that cannot be read."""

import json
import logging
from datetime import UTC, datetime

import pytest
from helpers import RECORD_FIELDS, record_of

from pixdrop.storage import Storage
from pixdrop.store import Job, JobStore, Picture, Status, Usage

T0 = 1_793_456_280.5  # exact in binary: times survive the millisecond precision of the records

FIELDS = {
    "status": Status.GENERATING, "prompt": "a red fox", "model": "gpt-image", "provider": "openrouter",
    "upstream_model": "openai/gpt-5-image", "api_type": "images",
}  # fmt: skip


@pytest.fixture
def storage(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.prepare()
    return storage


@pytest.fixture
def store(storage):
    return JobStore(storage)


def full_job(job_id="1793456280500_a9f3"):
    return Job(
        id=job_id,
        user="alice",
        status=Status.COMPLETED,
        created_at=T0,
        prompt="the knight, seen from the back",
        model="nano-banana",
        provider="openrouter",
        upstream_model="google/gemini-2.5-flash-image",
        api_type="chat",
        options={"aspect_ratio": "16:9", "seed": 42},
        source="mcp",
        client_ip="203.0.113.7",
        user_agent="claude-code/2.1",
        reference_count=1,
        references=(Picture(1, "1_knight.png", "image/png", 1234, 64, 64, original_name="knight.png"),),
        images=(Picture(1, f"{job_id}.png", "image/png", 456789, 1024, 1024), Picture(2, f"{job_id}_2.jpg", "image/jpeg", 9, 8, 8)),
        started_at=T0 + 0.25,
        finished_at=T0 + 12.75,
        duration_seconds=12.5,
        text="Here is your image.",
        usage=Usage(prompt_tokens=312, completion_tokens=1290, total_tokens=1602, cost=0.04),
        upstream_id="gen-123",
        error=None,
    )


def test_a_new_job_is_written_with_the_specified_fields(store, storage):
    created = datetime(2026, 11, 2, 14, 58, 0, 412000, tzinfo=UTC).timestamp()
    job = store.create("alice", created, **FIELDS, options={"quality": "high"}, client_ip="203.0.113.7", user_agent="curl/8.5.0", started_at=created)
    record = record_of(storage.root, "alice", job.id)
    assert list(record) == RECORD_FIELDS
    assert record == {
        "id": job.id, "user": "alice", "status": "generating",
        "created_at": "2026-11-02T14:58:00.412Z", "started_at": "2026-11-02T14:58:00.412Z", "finished_at": None,
        "duration_seconds": None, "source": "api", "client_ip": "203.0.113.7", "user_agent": "curl/8.5.0",
        "prompt": "a red fox", "model": "gpt-image", "provider": "openrouter", "upstream_model": "openai/gpt-5-image",
        "api_type": "images", "options": {"quality": "high"}, "reference_count": 0, "references": [], "images": [],
        "text": None, "usage": None, "upstream_id": None, "error": None,
    }  # fmt: skip
    assert job == Job.from_record(record)


def test_a_record_keeps_everything_about_a_job(store, storage):
    job = full_job()
    storage.user_dir("alice").mkdir()
    store.save(job)
    record = record_of(storage.root, "alice", job.id)
    assert list(record) == RECORD_FIELDS
    assert record["references"] == [
        {"index": 1, "filename": "1_knight.png", "content_type": "image/png", "bytes": 1234, "width": 64, "height": 64, "original_name": "knight.png"}
    ]
    assert record["images"][0] == {
        "index": 1, "filename": f"{job.id}.png", "content_type": "image/png", "bytes": 456789, "width": 1024, "height": 1024,
    }  # fmt: skip  (no original_name: only reference images have one)
    assert record["usage"] == {"prompt_tokens": 312, "completion_tokens": 1290, "total_tokens": 1602, "cost": 0.04}
    assert (record["created_at"], record["finished_at"]) == ("2026-10-31T14:18:00.500Z", "2026-10-31T14:18:13.250Z")
    assert store.get("alice", job.id) == job


def test_jobs_belong_to_their_user(store):
    job = store.create("alice", T0, **FIELDS)
    assert store.get("alice", job.id) == job
    assert store.get("bob", job.id) is None
    assert store.get("alice", "1793456280500_0000") is None
    assert store.users() == ["alice"]


def test_jobs_are_listed_newest_first_a_page_at_a_time(store):
    ids = [store.create("alice", T0 + minute * 60, **FIELDS).id for minute in range(5)]
    store.create("bob", T0, **FIELDS)
    assert store.ids("alice") == ids[::-1]
    jobs, total = store.page("alice", 1, 2)
    assert ([job.id for job in jobs], total) == ([ids[3], ids[2]], 5)
    assert store.page("alice", 5, 2) == ([], 5)
    assert [job.id for job in store.all("alice")] == ids[::-1]
    assert store.last("alice").id == ids[-1]
    assert store.last("carol") is None


def test_save_replaces_the_record(store, storage):
    job = store.create("alice", T0, **FIELDS)
    store.save(job.updated(status=Status.FAILED, finished_at=T0 + 1, error="boom"))
    stored = store.get("alice", job.id)
    assert (stored.status, stored.error, stored.finished_at) == (Status.FAILED, "boom", T0 + 1)
    assert [path.name for path in storage.user_dir("alice").iterdir()] == [f"{job.id}.json"]  # no temporary file left


def test_status():
    assert [status.value for status in Status] == ["pending", "generating", "completed", "failed"]
    assert [status.finished for status in Status] == [False, False, True, True]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda record: "{not json", None),
        (lambda record: json.dumps([record]), None),
        (lambda record: json.dumps({k: v for k, v in record.items() if k != "prompt"}), "malformed record"),
        (lambda record: json.dumps(record | {"status": "queued"}), "malformed record"),
        (lambda record: json.dumps(record | {"created_at": "yesterday"}), "malformed record"),
        (lambda record: json.dumps(record | {"images": [{"index": 1, "name": "x.png"}]}), "malformed record"),
        (lambda record: json.dumps(record | {"usage": {"tokens": 5}}), "malformed record"),
        (lambda record: json.dumps(record | {"reference_count": "many"}), "malformed record"),
        (lambda record: json.dumps(record | {"id": "1793456280500_ffff"}), "which names job 1793456280500_ffff of alice"),
        (lambda record: json.dumps(record | {"user": "bob"}), "which names job"),
    ],
)
def test_malformed_records_are_ignored(store, storage, caplog, change, reason):
    older = store.create("alice", T0, **FIELDS)
    newer = store.create("alice", T0 + 60, **FIELDS)
    path = storage.record_path("alice", newer.id)
    path.write_text(change(json.loads(path.read_text())))
    with caplog.at_level(logging.WARNING):
        assert store.get("alice", newer.id) is None
        assert store.last("alice") == older  # the newest that can be read
        jobs, total = store.page("alice", 0, 10)
    assert (jobs, total) == ([older], 2)
    if reason:
        assert f"of job {newer.id} of alice" in caplog.text and reason in caplog.text


def test_records_of_older_versions_get_defaults(store, storage):
    job = store.create("alice", T0, **FIELDS)
    path = storage.record_path("alice", job.id)
    minimal = {key: json.loads(path.read_text())[key] for key in ("id", "user", "status", "created_at", "prompt", "model")}
    path.write_text(json.dumps(minimal))
    old = store.get("alice", job.id)
    assert (old.provider, old.source, old.client_ip, old.reference_count, old.images, old.usage) == ("", "api", "-", 0, (), None)
