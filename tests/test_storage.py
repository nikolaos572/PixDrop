"""The data directory: its layout, job ids, and atomic writes."""

import json
import os
import threading

import pytest
from helpers import JOB_ID, Clock

from pixdrop import storage as storage_module
from pixdrop.storage import Storage, read_json, write_file, write_json


@pytest.fixture
def storage(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.prepare()
    return storage


def leftovers(directory):
    """Temporary files of atomic writes left in `directory`."""
    return [path.name for path in directory.iterdir() if path.name.endswith(".tmp")]


def test_prepare_creates_the_layout_and_empties_the_temporary_directory(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.prepare()
    assert (tmp_path / "data" / "outputs").is_dir()
    assert list((tmp_path / "data" / "tmp").iterdir()) == []
    (storage.tmp_dir / "reference-abc").write_bytes(b"half an upload")
    (storage.tmp_dir / "nested").mkdir()
    (storage.outputs_dir / "alice").mkdir()
    storage.prepare()  # the next start
    assert list(storage.tmp_dir.iterdir()) == []
    assert (storage.outputs_dir / "alice").is_dir()  # never touched


def test_paths_of_a_job(storage):
    job_id = "1793456280412_a9f3"
    user_dir = storage.root / "outputs" / "alice"
    assert storage.record_path("alice", job_id) == user_dir / f"{job_id}.json"
    assert storage.image_path("alice", job_id, 1, ".png") == user_dir / f"{job_id}.png"
    assert storage.image_path("alice", job_id, 2, ".png") == user_dir / f"{job_id}_2.png"
    assert storage.image_path("alice", job_id, 10, ".jpg") == user_dir / f"{job_id}_10.jpg"
    assert storage.reference_dir("alice", job_id) == user_dir / f"{job_id}_reference"


@pytest.mark.parametrize("user", ["../alice", "Alice", "", "a/b", ".."])
def test_user_names_are_checked(storage, user):
    with pytest.raises(ValueError, match="invalid user name"):
        storage.user_dir(user)


@pytest.mark.parametrize("job_id", ["1", "../1793456280412_a9f3", "1793456280412_A9F3", "1793456280412_a9f3.json", "last", "179345628041_a9f3"])
def test_job_ids_are_checked(storage, job_id):
    for path in (
        lambda: storage.record_path("alice", job_id),
        lambda: storage.image_path("alice", job_id, 1, ".png"),
        lambda: storage.reference_dir("alice", job_id),
    ):
        with pytest.raises(ValueError, match="invalid job id"):
            path()


def test_a_new_record_gets_an_id_from_the_time(storage):
    record = storage.create_record("alice", lambda job_id: {"id": job_id}, clock=Clock(1_793_456_280.5))
    assert JOB_ID.fullmatch(record["id"])
    assert record["id"].startswith("1793456280500_")
    path = storage.record_path("alice", record["id"])
    assert json.loads(path.read_text()) == record
    assert path.read_text().endswith("\n")


def test_ids_sort_in_the_order_of_the_jobs(storage):
    times = [999_999_999.5, 1_000_000_000.0, 1_793_456_280.25, 1_793_456_280.5, 4_102_444_800.0]  # 2001 to 2100
    ids = [storage.create_record("alice", lambda job_id: {"id": job_id}, clock=Clock(now))["id"] for now in reversed(times)]
    assert ids[-1].startswith("0999999999500_")  # 13 digits, so they also sort as text
    assert storage.job_ids("alice") == ids[::-1]


def test_a_colliding_id_is_never_reused(storage, monkeypatch):
    hexes = iter(["abcd", "abcd", "abcd", "ef01"])
    monkeypatch.setattr(storage_module.secrets, "token_hex", lambda size: next(hexes))
    made = []

    def make(job_id):
        made.append(job_id)
        return {"id": job_id, "n": len(made)}

    clock = Clock(1_793_456_280.0)
    first = storage.create_record("alice", make, clock=clock)
    second = storage.create_record("alice", make, clock=clock)  # the same millisecond, the same random digits
    assert (first["id"], second["id"]) == ("1793456280000_abcd", "1793456280000_ef01")
    assert made == ["1793456280000_abcd", "1793456280000_abcd", "1793456280000_abcd", "1793456280000_ef01"]
    assert read_json(storage.record_path("alice", first["id"])) == {"id": "1793456280000_abcd", "n": 1}  # not overwritten
    assert read_json(storage.record_path("alice", second["id"]))["n"] == 4


def test_concurrent_records_in_the_same_millisecond_get_unique_ids(storage):
    clock = Clock(1_793_456_280.0)  # 65536 possible ids: collisions are likely among 400
    ids, lock = [], threading.Lock()

    def create_many():
        for _ in range(50):
            record = storage.create_record("alice", lambda job_id: {"id": job_id}, clock=clock)
            with lock:
                ids.append(record["id"])

    threads = [threading.Thread(target=create_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(ids)) == 400
    assert sorted(storage.job_ids("alice")) == sorted(ids)
    assert all(read_json(storage.record_path("alice", job_id)) == {"id": job_id} for job_id in ids)


def test_job_ids_lists_only_records(storage):
    user_dir = storage.user_dir("alice")
    user_dir.mkdir(parents=True)
    for name in (
        "1793456280412_a9f3.json", "1793456280413_0000.json", "1793456280412_a9f3.png", "1793456280412_a9f3_2.png",
        ".1793456280412_a9f3.json.1234abcd.tmp", "notes.json", "1793456280412_A9F3.json",
    ):  # fmt: skip
        (user_dir / name).write_text("{}")
    (user_dir / "1793456280412_a9f3_reference").mkdir()
    assert storage.job_ids("alice") == ["1793456280412_a9f3", "1793456280413_0000"]
    assert storage.job_ids("bob") == []  # no directory yet


def test_users_are_the_directories_with_valid_names(storage):
    for name in ("bob", "alice", "Not-A-User", ".hidden"):
        (storage.outputs_dir / name).mkdir()
    (storage.outputs_dir / "carol").write_text("a file")
    assert storage.users() == ["alice", "bob"]


def test_size_of_counts_records_images_and_references(storage):
    user_dir = storage.user_dir("alice")
    (user_dir / "x_reference").mkdir(parents=True)
    (user_dir / "a.json").write_bytes(b"x" * 10)
    (user_dir / "a.png").write_bytes(b"x" * 100)
    (user_dir / "x_reference" / "1_a.png").write_bytes(b"x" * 1000)
    (user_dir / "link").symlink_to("/etc/passwd")  # not followed
    assert storage.size_of("alice") == 1110
    assert storage.size_of("bob") == 0


def test_temporary_paths_are_new_and_inside_the_temporary_directory(storage):
    paths = {storage.temp_path("reference") for _ in range(100)}
    assert len(paths) == 100
    assert all(path.parent == storage.tmp_dir and path.name.startswith("reference-") and not path.exists() for path in paths)


# --- atomic writes ---


def test_write_json_replaces_the_file_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "job.json"
    write_json(path, {"status": "generating"})
    write_json(path, {"status": "completed", "prompt": "un renard roux, 狐"})
    assert read_json(path) == {"status": "completed", "prompt": "un renard roux, 狐"}
    assert "狐" in path.read_text(encoding="utf-8")  # readable by people: not escaped
    assert leftovers(tmp_path) == []


def test_a_failed_write_keeps_the_previous_content(tmp_path):
    path = tmp_path / "job.json"
    write_json(path, {"status": "generating"})
    with pytest.raises(TypeError):
        write_json(path, {"status": object()})  # cannot be written as JSON, halfway through
    assert read_json(path) == {"status": "generating"}
    assert leftovers(tmp_path) == []


def test_write_file_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "image.png"
    write_file(path, b"first")
    write_file(path, b"second")
    assert path.read_bytes() == b"second"

    def broken_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(storage_module.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        write_file(path, b"third")
    assert path.read_bytes() == b"second"
    assert leftovers(tmp_path) == []


def test_records_are_written_to_disk_before_they_replace_the_old_ones(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(storage_module.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    write_json(tmp_path / "job.json", {"a": 1})
    write_file(tmp_path / "image.png", b"png")
    assert len(synced) == 2


@pytest.mark.parametrize("content", [None, "", "{broken", "[1, 2]", '"text"', "\xff\xfe"])
def test_read_json_gives_none_for_what_is_not_a_record(tmp_path, content):
    path = tmp_path / "job.json"
    if content is not None:
        path.write_bytes(content.encode("latin-1"))
    assert read_json(path) is None
