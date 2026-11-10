"""The JSON API under /api: every endpoint, as scripts and the web page use it."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from helpers import (
    ALICE,
    API_KEY,
    BASE_URL,
    RECORD_FIELDS,
    TOKENS,
    add_job,
    browser_of,
    config_with,
    generate,
    gif,
    jpeg,
    png,
    record_of,
    reference,
    routes_of,
    share,
    size_of,
    submit,
    svg,
    wait_for,
    write_json,
)

from pixdrop import __version__
from pixdrop.store import Status

DATE = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+0[23]:00"  # in the configured time zone, Europe/Athens
T0 = 1_793_456_280.5


def leftovers(data_dir):
    return list((data_dir / "tmp").iterdir())


def jobs_of(client, **params):
    listed = client.get("/api/jobs", params=params).json()
    return [job["id"] for job in listed["jobs"]], listed["total"]


# --- status ---


def test_health_needs_no_token(anonymous):
    assert anonymous.get("/api/health").json() == {"status": "ok"}


def test_info_gives_the_servers_settings_and_your_account(client, data_dir):
    generate(client)
    submit(client, reference_count=1)
    info = client.get("/api/info").json()
    assert info["server"] == {
        "version": __version__, "timezone": "Europe/Athens", "base_url": "http://testserver",
        "mcp_url": "http://testserver/mcp", "link_expiry_minutes": 60, "generation_timeout_seconds": 180,
        "max_upload_mb": 1, "max_reference_images": 16, "max_active_jobs": 4, "max_prompt_chars": 32000,
        "medium_image_px": 512, "models": 3, "default_model": "gpt-image",
    }  # fmt: skip
    files = [path for path in (data_dir / "outputs" / "alice").rglob("*") if path.is_file()]
    assert len(files) == 3  # two records and an image
    assert info["account"] == {"user": "alice", "jobs": 2, "in_progress": 1, "storage_bytes": sum(path.stat().st_size for path in files)}


def test_info_of_a_user_without_jobs(bob):
    assert bob.get("/api/info").json()["account"] == {"user": "bob", "jobs": 0, "in_progress": 0, "storage_bytes": 0}


def test_models_list_the_options_and_their_values_without_the_keys(client):
    response = client.get("/api/models")
    models = response.json()
    assert models["default_model"] == "gpt-image"
    assert [model["name"] for model in models["models"]] == ["gpt-image", "nano-banana", "plain"]
    gpt = models["models"][0]
    assert {key: value for key, value in gpt.items() if key != "options"} == {
        "name": "gpt-image", "description": "OpenAI's image model", "provider": "openrouter",
        "upstream_model": "openai/gpt-5-image", "api_type": "images", "default": True, "max_reference_images": 16,
        "min_reference_images": 0, "options_from": "config",
    }  # fmt: skip
    options = {option["name"]: option for option in gpt["options"]}
    assert list(options) == ["aspect_ratio", "size", "quality", "background", "output_format", "n"]
    assert options["quality"] == {
        "name": "quality", "description": "Rendering quality", "type": "enum",
        "values": ["auto", "low", "medium", "high", "xhigh", "max"], "minimum": None, "maximum": None, "pattern": None,
        "default": "high",
    }  # fmt: skip
    assert (options["n"]["type"], options["n"]["minimum"], options["n"]["maximum"], options["n"]["values"]) == ("integer", 1, 10, None)
    assert (options["size"]["type"], options["size"]["pattern"]) == ("string", r"auto|\d{2,5}x\d{2,5}")
    banana = models["models"][1]
    assert (banana["default"], banana["api_type"], banana["max_reference_images"]) == (False, "chat", 3)
    assert models["models"][2]["options"] == []
    for secret in (API_KEY, BASE_URL, "X-OpenRouter-Title"):
        assert secret not in response.text


def test_models_follow_the_configuration_file(client, data_dir):
    seedream = {"provider": "openrouter", "model": "bytedance-seed/seedream-4.5", "api_type": "images", "options": ["seed"]}
    write_json(data_dir / "config.json", config_with(seedream=seedream))
    assert [model["name"] for model in client.get("/api/models").json()["models"]][-1] == "seedream"
    assert generate(client, model="seedream", seed=3)["status"] == "completed"


# --- creating a job ---


def test_a_job_from_a_prompt(client):
    response = submit(client, "a red fox in the snow")
    assert response.status_code == 201
    job = response.json()
    assert re.fullmatch(r"\d{13}_[0-9a-f]{4}", job["id"])
    assert re.fullmatch(DATE, job["created_at"]) and job["started_at"] == job["created_at"]
    assert job == {
        "id": job["id"], "status": "generating", "prompt": "a red fox in the snow", "model": "gpt-image",
        "provider": "openrouter", "upstream_model": "openai/gpt-5-image", "api_type": "images",
        "options": {"quality": "high"}, "source": "api", "client_ip": "testclient", "created_at": job["created_at"],
        "started_at": job["started_at"], "finished_at": None, "duration_seconds": None, "reference_count": 0,
        "references": [], "images": [], "text": None, "usage": None, "error": None, "upload_url": None,
    }  # fmt: skip

    done = wait_for(client, job["id"])
    assert re.fullmatch(DATE, done["finished_at"])
    assert done | {"finished_at": None, "duration_seconds": None, "images": None} == job | {
        "status": "completed", "images": None, "usage": {"prompt_tokens": 12, "completion_tokens": 1290, "total_tokens": 1302, "cost_usd": 0.04},
    }  # fmt: skip
    [image] = done["images"]
    assert image | {"bytes": 0} == {
        "index": 1, "filename": f"{job['id']}.png", "content_type": "image/png", "bytes": 0, "width": 1024, "height": 1024,
        "original_name": None, "url": f"http://testserver/api/jobs/{job['id']}/images/1",
        "medium_url": f"http://testserver/api/jobs/{job['id']}/images/1?size=medium",
    }  # fmt: skip


def test_wait_answers_once_the_job_is_finished(client):
    job = submit(client, wait=30).json()
    assert job["status"] == "completed" and len(job["images"]) == 1
    assert client.get(f"/api/jobs/{job['id']}", params={"wait": 600}).json() == job  # finished: at once


@pytest.mark.parametrize("wait", ["601", "-1", "soon"])
def test_wait_is_bounded(client, wait):
    assert client.get("/api/jobs/last", params={"wait": wait}).status_code == 422
    assert client.post("/api/jobs", params={"wait": wait}, data={"prompt": "x"}).status_code == 422


def test_a_job_with_reference_images(client, data_dir, upstream):
    response = submit(client, "the knight from behind", references=[("knight.png", png(64, 48)), ("photo.png", jpeg())])
    assert response.status_code == 201
    job = response.json()
    url = f"http://testserver/api/jobs/{job['id']}/references"
    assert (job["status"], job["reference_count"]) == ("generating", 2)
    assert job["references"] == [
        {"index": 1, "filename": "1_knight.png", "content_type": "image/png", "bytes": len(png(64, 48)), "width": 64, "height": 48,
         "original_name": "knight.png", "url": f"{url}/1", "medium_url": f"{url}/1?size=medium"},
        {"index": 2, "filename": "2_photo.jpg", "content_type": "image/jpeg", "bytes": len(jpeg()), "width": 64, "height": 64,
         "original_name": "photo.png", "url": f"{url}/2", "medium_url": f"{url}/2?size=medium"},  # its real type
    ]  # fmt: skip
    directory = data_dir / "outputs" / "alice" / f"{job['id']}_reference"
    assert sorted(path.name for path in directory.iterdir()) == ["1_knight.png", "2_photo.jpg"]
    assert (directory / "1_knight.png").read_bytes() == png(64, 48)
    assert wait_for(client, job["id"])["status"] == "completed"
    assert len(upstream.bodies[-1]["input_references"]) == 2
    assert leftovers(data_dir) == []


def test_the_job_record_on_disk(client, data_dir):
    job = generate(client, references=[("knight.png", png())], quality="low", headers={"User-Agent": "curl/8.5.0"})
    record = record_of(data_dir, "alice", job["id"])
    assert list(record) == RECORD_FIELDS
    assert (record["id"], record["user"], record["status"], record["source"]) == (job["id"], "alice", "completed", "api")
    assert (record["client_ip"], record["user_agent"], record["prompt"]) == ("testclient", "curl/8.5.0", "a red fox")
    assert (record["model"], record["provider"], record["upstream_model"], record["api_type"]) == ("gpt-image", "openrouter", "openai/gpt-5-image", "images")
    assert (record["options"], record["reference_count"]) == ({"quality": "low"}, 1)
    assert record["references"] == [
        {"index": 1, "filename": "1_knight.png", "content_type": "image/png", "bytes": len(png()), "width": 64, "height": 64, "original_name": "knight.png"}
    ]
    assert list(record["images"][0]) == ["index", "filename", "content_type", "bytes", "width", "height"]
    assert record["usage"] == {"prompt_tokens": 312, "completion_tokens": 1290, "total_tokens": 1602, "cost": 0.04}
    for name in ("created_at", "started_at", "finished_at"):
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record[name])  # UTC, with milliseconds
    names = sorted(path.relative_to(data_dir / "outputs").as_posix() for path in (data_dir / "outputs").rglob("*"))
    assert names == [
        "alice", f"alice/{job['id']}.json", f"alice/{job['id']}.png",
        f"alice/{job['id']}_reference", f"alice/{job['id']}_reference/1_knight.png",
    ]  # fmt: skip


def test_reference_images_announced_and_uploaded_later(client, data_dir, upstream):
    job = submit(client, "the knight from behind", reference_count=2).json()
    upload_url = f"http://testserver/api/jobs/{job['id']}/references"
    assert (job["status"], job["reference_count"], job["references"], job["upload_url"], job["started_at"]) == ("pending", 2, [], upload_url, None)
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "pending"
    assert upstream.bodies == []

    first = client.post(upload_url, files=[reference("front.png", png())])
    assert first.status_code == 200
    assert (first.json()["status"], len(first.json()["references"]), first.json()["upload_url"]) == ("pending", 1, upload_url)
    second = client.post(upload_url, files=[reference("back.gif", gif())])
    assert (second.json()["status"], second.json()["upload_url"]) == ("generating", None)
    assert re.fullmatch(DATE, second.json()["started_at"])

    done = wait_for(client, job["id"])
    assert done["status"] == "completed"
    assert [picture["filename"] for picture in done["references"]] == ["1_front.png", "2_back.gif"]
    assert len(upstream.bodies[-1]["input_references"]) == 2


def test_all_announced_references_at_once(client):
    job = submit(client, reference_count=3).json()
    files = [reference(f"{n}.png", png(8 * n, 8)) for n in (1, 2, 3)]
    assert client.post(f"/api/jobs/{job['id']}/references", files=files).json()["status"] == "generating"
    assert [picture["width"] for picture in wait_for(client, job["id"])["references"]] == [8, 16, 24]


def test_reference_uploads_that_do_not_fit_the_job(client, data_dir):
    pending = submit(client, reference_count=1).json()
    url = f"/api/jobs/{pending['id']}/references"
    too_many = client.post(url, files=[reference("a.png", png()), reference("b.png", png())])
    assert (too_many.status_code, too_many.json()) == (400, {"detail": "the job expects 1 more reference image(s), got 2"})
    nothing = client.post(url, data={"x": "1"})
    assert (nothing.status_code, nothing.json()) == (400, {"detail": "no reference image was sent: add them as form fields named 'reference'"})
    broken = client.post(url, files=[reference("broken.png", png(200, 200)[:-200])])
    assert (broken.status_code, broken.json()) == (400, {"detail": "broken.png: the image is damaged or cannot be read"})
    assert client.get(f"/api/jobs/{pending['id']}").json()["references"] == []  # still waiting for a good one

    done = generate(client)
    conflict = client.post(f"/api/jobs/{done['id']}/references", files=[reference("a.png", png())])
    assert (conflict.status_code, conflict.json()) == (409, {"detail": "the job is completed: reference images can be added only while it is pending"})
    missing = client.post("/api/jobs/1793456280000_0000/references", files=[reference("a.png", png())])
    assert (missing.status_code, missing.json()) == (404, {"detail": "no such job"})
    assert client.post("/api/jobs/last/references", files=[reference("a.png", png())]).status_code == 422
    assert leftovers(data_dir) == []


@pytest.mark.parametrize(
    ("form", "detail"),
    [
        ({"model": "nope"}, "unknown model 'nope'; available: gpt-image, nano-banana, plain"),
        ({"model": "nano-banana", "quality": "high"}, "model 'nano-banana' does not support 'quality'; its options: aspect_ratio, resolution, seed"),
        ({"model": "plain", "n": "2"}, "model 'plain' does not support 'n'; its options: none"),
        ({"quality": "ultra"}, "quality must be one of: auto, low, medium, high, xhigh, max"),
        ({"aspect_ratio": "7:3"}, "aspect_ratio must be one of: "),
        ({"n": "11"}, "n must be an integer from 1 to 10"),
        ({"n": "two"}, "n must be an integer from 1 to 10"),
        ({"size": "big"}, "size must match auto|\\d{2,5}x\\d{2,5} (got 'big')"),
        ({"background": "transparent", "output_format": "jpeg"}, "a transparent background needs output_format png or webp, not jpeg"),
        ({"prompt": "   "}, "the prompt is empty"),
        ({"prompt": "x" * 32_001}, "the prompt is too long: 32001 characters (at most 32000)"),
        ({"model": "nano-banana", "reference_count": "4"}, "model 'nano-banana' accepts at most 3 reference image(s), got 4"),
    ],
)
def test_invalid_requests_are_refused_with_the_reason(client, data_dir, form, detail):
    response = client.post("/api/jobs", data={"prompt": "a red fox"} | form)
    assert response.status_code == 400
    assert response.json()["detail"].startswith(detail)
    assert jobs_of(client) == ([], 0)
    assert not (data_dir / "outputs" / "alice").exists() or list((data_dir / "outputs" / "alice").iterdir()) == []


@pytest.mark.parametrize("form", [{}, {"prompt": "x", "reference_count": "17"}, {"prompt": "x", "reference_count": "-1"}, {"prompt": "x", "reference_count": "two"}])
def test_malformed_forms_are_refused(client, form):
    assert client.post("/api/jobs", data=form).status_code == 422


def test_empty_option_fields_mean_not_given(client):
    # The web page's form sends every field, the empty ones too.
    job = submit(client, quality="", n="", size="", aspect_ratio="", reference_count="0").json()
    assert job["options"] == {"quality": "high"}


@pytest.mark.parametrize(
    ("model", "files", "detail"),
    [
        ("gpt-image", [("notes.txt", b"hello")], "notes.txt: unsupported image type: use PNG, JPEG, WebP, GIF or SVG"),
        ("gpt-image", [("empty.png", b"")], "empty.png: the file is empty"),
        ("gpt-image", [("broken.png", png(200, 200)[:-200])], "broken.png: the image is damaged or cannot be read"),
        ("gpt-image", [("big.png", b"\0" * (1024**2 + 1))], "a reference image exceeds the 1 MB limit (MAX_UPLOAD_MB)"),
        ("gpt-image", [("a.png", png()), ("/home/me/evil.exe", b"MZ")], "evil.exe: unsupported image type"),
        ("gpt-image", [(f"{n}.png", png()) for n in range(17)], "at most 16 reference images per job, got 17"),
        ("nano-banana", [(f"{n}.png", png()) for n in range(4)], "model 'nano-banana' accepts at most 3 reference image(s), got 4"),
        ("plain", [("a.png", png())], "model 'plain' accepts at most 0 reference image(s), got 1"),
    ],
)
def test_reference_images_are_checked(client, data_dir, model, files, detail):
    response = submit(client, model=model, references=files)
    assert response.status_code == 400
    assert response.json()["detail"].startswith(detail)
    assert jobs_of(client) == ([], 0)
    assert leftovers(data_dir) == []  # the uploads are not kept


def test_too_many_jobs_in_progress(make_app, data_dir):
    with TestClient(make_app(max_active_jobs=2), headers=dict(ALICE)) as client:
        submit(client, reference_count=1)
        submit(client, "HANG")
        response = submit(client, references=[("a.png", png())])
        assert (response.status_code, response.json()) == (
            429, {"detail": "you have 2 job(s) in progress, the most allowed (MAX_ACTIVE_JOBS); wait until one finishes"},
        )  # fmt: skip
        assert leftovers(data_dir) == []  # the upload is not kept
        assert jobs_of(client)[1] == 2
        assert submit(client, headers={"Authorization": f"Bearer {TOKENS['bob']}"}).status_code == 201  # per user


def test_reference_count_must_match_the_files_sent(client):
    response = submit(client, reference_count=2, references=[("a.png", png())])
    assert (response.status_code, response.json()["detail"]) == (400, "reference_count is 2 but 1 reference image(s) were sent")


def test_an_empty_file_field_is_ignored(client):
    # A browser sends the file input even when no file was picked: a part without name nor content.
    response = client.post("/api/jobs", data={"prompt": "a red fox"}, files=[("reference", ("", b"", "application/octet-stream"))])
    assert response.status_code == 201, response.text
    assert response.json()["reference_count"] == 0


def multipart(*parts: tuple[bytes, bytes]) -> tuple[bytes, dict[str, str]]:
    """A multipart body written by hand, byte for byte (as a client other than httpx may send it)."""
    body = b"".join(b"--b0undary\r\nContent-Disposition: form-data; " + head + b"\r\n\r\n" + content + b"\r\n" for head, content in parts)
    return body + b"--b0undary--\r\n", {"Content-Type": "multipart/form-data; boundary=b0undary"}


def test_the_names_of_uploaded_files_are_cleaned(client):
    body, headers = multipart(
        (b'name="prompt"', b"a red fox"),
        (b'name="reference"; filename="my knight\x07\x1b.png"', png()),
        (b'name="reference"; filename="../../etc/\xce\xb5\xce\xb9\xce\xba\xcf\x8c\xce\xbd\xce\xb1.PNG"', png()),
        (b'name="reference"; filename="C:\\\\fakepath\\\\photo.png"', png()),
    )
    response = client.post("/api/jobs", content=body, headers=headers)
    assert response.status_code == 201, response.text
    assert [(picture["filename"], picture["original_name"]) for picture in response.json()["references"]] == [
        ("1_my-knight.png", "my knight.png"),  # control characters dropped
        ("2_reference.png", "εικόνα.PNG"),  # no directories; letters kept in the original name only
        ("3_photo.png", "photo.png"),
    ]


# --- request bodies ---


def test_a_body_without_its_length_is_refused(client):
    for path in ("/api/jobs", "/api/jobs/1793456280000_0000/references", "/api/session", "/mcp"):
        response = client.post(path, content=iter([b"prompt=x"]), headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert (response.status_code, response.json()) == (411, {"detail": "Content-Length required"})


def test_a_body_above_the_limit_is_refused_before_it_is_read(client):
    limit = 16 * 1024**2 + 1024**2  # 16 reference images of MAX_UPLOAD_MB, and the other fields
    headers = {"Content-Type": "multipart/form-data; boundary=x"}
    response = client.post("/api/jobs", content=b"x" * (limit + 1), headers=headers)
    assert (response.status_code, response.json()) == (413, {"detail": "the request exceeds the 17 MB limit"})
    assert client.post("/api/jobs", content=b"x" * limit, headers=headers).status_code in (400, 422)  # read, then refused as a form


# --- reading jobs ---


def test_jobs_are_listed_newest_first_a_page_at_a_time(client, data_dir):
    ids = [add_job(data_dir, "alice", T0 + minute * 60).id for minute in range(15)]
    add_job(data_dir, "bob", T0)
    listed = client.get("/api/jobs").json()
    assert (listed["total"], listed["page"], listed["per_page"]) == (15, 1, 12)
    assert [job["id"] for job in listed["jobs"]] == ids[:2:-1]  # the newest 12
    assert jobs_of(client, page=2) == (ids[2::-1], 15)
    assert jobs_of(client, page=3) == ([], 15)
    assert jobs_of(client, page=2, per_page=5) == (ids[9:4:-1], 15)
    assert jobs_of(client, per_page=100, page=1_000_000) == ([], 15)
    assert listed["jobs"][0] == client.get(f"/api/jobs/{ids[-1]}").json()


@pytest.mark.parametrize("params", [{"page": 0}, {"page": 1_000_001}, {"page": "x"}, {"per_page": 0}, {"per_page": 101}, {"per_page": "all"}])
def test_paging_is_validated(client, params):
    assert client.get("/api/jobs", params=params).status_code == 422


def test_last_is_the_newest_job(client, bob, data_dir):
    none_yet = bob.get("/api/jobs/last")
    assert (none_yet.status_code, none_yet.json()) == (404, {"detail": "you have no jobs yet"})
    generate(client, "first")
    newest = generate(client, "second")
    assert client.get("/api/jobs/last").json()["id"] == newest["id"]
    image = client.get("/api/jobs/last/images/1")
    assert image.content == (data_dir / "outputs" / "alice" / f"{newest['id']}.png").read_bytes()


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs/abc",
        "/api/jobs/1793456280412_A9F3",
        "/api/jobs/1793456280412_a9f3x",
        "/api/jobs/179345628041_a9f3",
        "/api/jobs/..%2F..%2Fusers.json",
        "/api/jobs/last/images/0",
        "/api/jobs/last/images/17",
        "/api/jobs/last/images/one",
        "/api/jobs/last/references/0",
        "/api/jobs/last/images/1?size=small",
    ],
)
def test_invalid_job_references_are_refused(client, path):
    assert client.get(path).status_code in (404, 422)
    assert "detail" in client.get(path).json()


def test_a_job_that_does_not_exist(client):
    response = client.get("/api/jobs/1793456280412_a9f3")
    assert (response.status_code, response.json()) == (404, {"detail": "no such job"})


# --- images ---


def test_the_full_and_medium_image(client, data_dir):
    job = generate(client)
    image = job["images"][0]
    full = client.get(image["url"])
    assert full.status_code == 200
    assert full.headers["content-type"] == "image/png"
    assert full.headers["content-disposition"] == f'inline; filename="{job["id"]}.png"'
    assert full.headers["cache-control"] == "private, max-age=86400"
    assert full.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    assert full.content == (data_dir / "outputs" / "alice" / image["filename"]).read_bytes()
    assert client.get(image["url"], params={"size": "full"}).content == full.content

    medium = client.get(image["medium_url"])
    assert medium.headers["content-type"] == "image/png"
    assert medium.headers["content-disposition"] == f'inline; filename="{job["id"]}_medium.png"'
    assert medium.headers["content-security-policy"] == full.headers["content-security-policy"]
    assert size_of(medium.content) == (512, 512)
    assert not any("medium" in path.name for path in (data_dir / "outputs").rglob("*"))  # never stored


@pytest.mark.parametrize(("output_format", "content_type", "extension"), [("jpeg", "image/jpeg", ".jpg"), ("webp", "image/webp", ".webp")])
def test_the_medium_size_keeps_the_format(client, output_format, content_type, extension):
    job = generate(client, output_format=output_format)
    medium = client.get(job["images"][0]["medium_url"])
    assert medium.headers["content-type"] == content_type
    assert medium.headers["content-disposition"] == f'inline; filename="{job["id"]}_medium{extension}"'
    assert size_of(medium.content) == (512, 512)


def test_reference_images_full_and_medium(client):
    references = [("knight.png", png(64, 48)), ("big.gif", gif(1000, 500)), ("logo.svg", svg('width="100" height="50"'))]
    job = generate(client, references=references)
    prefix = f"{job['id']}_reference"
    small = client.get(job["references"][0]["url"])
    assert (small.content, small.headers["content-type"]) == (png(64, 48), "image/png")
    assert small.headers["content-disposition"] == f'inline; filename="{prefix}_1_knight.png"'
    small_medium = client.get(job["references"][0]["medium_url"])  # never scaled up: the full size
    assert (small_medium.content, small_medium.headers["content-disposition"]) == (png(64, 48), small.headers["content-disposition"])

    gif_medium = client.get(job["references"][1]["medium_url"])
    assert (gif_medium.headers["content-type"], size_of(gif_medium.content)) == ("image/png", (512, 256))
    assert gif_medium.headers["content-disposition"] == f'inline; filename="{prefix}_2_big_medium.png"'

    drawing = client.get(job["references"][2]["url"])
    assert drawing.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in drawing.headers["content-security-policy"]  # an SVG opened alone cannot run scripts
    assert size_of(client.get(job["references"][2]["medium_url"]).content) == (512, 256)


def test_missing_images(client, data_dir):
    done = generate(client)
    second = client.get(f"/api/jobs/{done['id']}/images/2")
    assert (second.status_code, second.json()) == (404, {"detail": f"job {done['id']} has no image 2"})
    no_reference = client.get(f"/api/jobs/{done['id']}/references/1")
    assert (no_reference.status_code, no_reference.json()) == (404, {"detail": f"job {done['id']} has no reference image 1"})
    failed = generate(client, "FAIL")
    assert client.get(f"/api/jobs/{failed['id']}/images/1").json() == {"detail": f"job {failed['id']} has no image 1"}
    running = submit(client, "HANG").json()
    assert client.get(f"/api/jobs/{running['id']}/images/1").json() == {"detail": f"job {running['id']} has no image 1 yet: it is generating"}

    (data_dir / "outputs" / "alice" / f"{done['id']}.png").unlink()  # removed by the administrator
    gone = client.get(done["images"][0]["url"])
    assert (gone.status_code, gone.json()) == (404, {"detail": "the image file is missing on the server"})


# --- users see only their own jobs ---


def test_users_never_see_each_others_jobs(client, bob, data_dir):
    done = generate(client, references=[("knight.png", png())])
    pending = submit(client, reference_count=1).json()
    assert jobs_of(bob) == ([], 0)
    for job in (done, pending):
        assert bob.get(f"/api/jobs/{job['id']}").json() == {"detail": "no such job"}
    for path in ("images/1", "images/1?size=medium", "references/1", "references/1?size=medium"):
        assert bob.get(f"/api/jobs/{done['id']}/{path}").status_code == 404
    refused = bob.post(f"/api/jobs/{pending['id']}/references", files=[reference("a.png", png())])
    assert refused.status_code == 404
    assert bob.get("/api/jobs/last").status_code == 404
    assert bob.get("/api/stats").json()["totals"]["jobs"] == 0
    assert bob.get("/api/info").json()["account"]["jobs"] == 0
    assert not (data_dir / "outputs" / "bob").exists()

    assert client.get(f"/api/jobs/{pending['id']}").json()["references"] == []  # untouched
    assert jobs_of(client)[1] == 2
    bobs = generate(bob, "bob's cat")
    assert client.get(f"/api/jobs/{bobs['id']}").status_code == 404
    assert jobs_of(client)[1] == 2 and jobs_of(bob)[1] == 1


# --- where requests come from ---


def test_urls_use_public_base_url(make_app):
    with TestClient(make_app(public_base_url="https://pixdrop.example.com"), headers=dict(ALICE)) as client:
        job = submit(client, wait=30).json()
        assert job["images"][0]["url"] == f"https://pixdrop.example.com/api/jobs/{job['id']}/images/1"
        assert job["images"][0]["medium_url"] == f"https://pixdrop.example.com/api/jobs/{job['id']}/images/1?size=medium"
        pending = submit(client, reference_count=1).json()
        assert pending["upload_url"] == f"https://pixdrop.example.com/api/jobs/{pending['id']}/references"
        server = client.get("/api/info").json()["server"]
        assert (server["base_url"], server["mcp_url"]) == ("https://pixdrop.example.com", "https://pixdrop.example.com/mcp")


def test_without_public_base_url_urls_follow_the_request(app):
    with TestClient(app, base_url="https://pix.internal:8443", headers=dict(ALICE)) as client:
        job = submit(client, reference_count=1).json()
        assert job["upload_url"] == f"https://pix.internal:8443/api/jobs/{job['id']}/references"


@pytest.mark.parametrize(
    ("headers", "client_ip"),
    [
        ({"X-Forwarded-For": "203.0.113.7"}, "203.0.113.7"),
        ({"X-Forwarded-For": " 203.0.113.7 , 10.0.0.1, 10.0.0.2"}, "203.0.113.7"),  # the original client comes first
        ({"CF-Connecting-IP": "198.51.100.4", "X-Forwarded-For": "203.0.113.7"}, "203.0.113.7"),  # the proxy's header decides
        ({"CF-Connecting-IP": "198.51.100.4"}, "198.51.100.4"),  # Cloudflare alone
        ({"X-Forwarded-For": "2001:DB8::1"}, "2001:db8::1"),
        ({"X-Forwarded-For": "unknown"}, "testclient"),  # not an address: ignored
        ({"CF-Connecting-IP": "<script>", "X-Forwarded-For": "203.0.113.7"}, "203.0.113.7"),
        ({}, "testclient"),
    ],
)
def test_with_any_peer_trusted_the_clients_address_comes_from_its_headers(make_app, data_dir, headers, client_ip):
    with TestClient(make_app(forwarded_allow_ips="*"), headers=dict(ALICE)) as client:
        job = submit(client, headers=ALICE | headers).json()
    assert job["client_ip"] == client_ip
    assert record_of(data_dir, "alice", job["id"])["client_ip"] == client_ip


def test_by_default_the_clients_headers_are_ignored(client, data_dir):
    # Only a proxy on this machine (127.0.0.1) is trusted, and uvicorn reads its headers.
    job = submit(client, headers=ALICE | {"X-Forwarded-For": "203.0.113.7", "CF-Connecting-IP": "198.51.100.4"}).json()
    assert job["client_ip"] == "testclient"


def test_forwarded_headers_count_only_when_every_peer_may_be_a_proxy(make_app):
    # With FORWARDED_ALLOW_IPS naming the proxies, uvicorn alone reads X-Forwarded-For (from those proxies):
    # the app takes the connection's address as uvicorn gives it (test_app runs the real server for that).
    with TestClient(make_app(forwarded_allow_ips="10.0.0.1"), headers=dict(ALICE)) as client:
        job = submit(client, headers=ALICE | {"X-Forwarded-For": "203.0.113.7", "CF-Connecting-IP": "198.51.100.4"}).json()
        assert job["client_ip"] == "testclient"


def test_the_user_agent_is_recorded_without_control_characters(client, data_dir):
    job = submit(client, headers=ALICE | {"User-Agent": "agent/1.0\x01\x7f (x)"}).json()
    assert record_of(data_dir, "alice", job["id"])["user_agent"] == "agent/1.0 (x)"


def test_the_source_of_each_job(client, data_dir):
    by_header = submit(client).json()
    by_api_key = share(client, headers={"X-API-Key": TOKENS["alice"]}).post("/api/jobs", data={"prompt": "x"}).json()
    by_query = share(client).post("/api/jobs", params={"token": TOKENS["alice"]}, data={"prompt": "x"}).json()
    browser = browser_of(client, headers={"X-Requested-With": "fetch"})
    by_cookie = browser.post("/api/jobs", data={"prompt": "x"}).json()
    assert [job["source"] for job in (by_header, by_api_key, by_query, by_cookie)] == ["api", "api", "api", "web"]
    assert record_of(data_dir, "alice", by_cookie["id"])["source"] == "web"


# --- nothing is ever deleted ---


@pytest.mark.parametrize(
    "path",
    ["/api/jobs", "/api/jobs/{id}", "/api/jobs/last", "/api/jobs/{id}/images/1", "/api/jobs/{id}/references/1", "/api/jobs/{id}/references"],
)
def test_there_is_no_way_to_delete_a_job(client, data_dir, path):
    job = generate(client, references=[("knight.png", png())])
    for method in ("DELETE", "PUT", "PATCH"):
        assert client.request(method, path.format(id=job["id"])).status_code == 405
    assert client.get(f"/api/jobs/{job['id']}").json() == job
    assert len([path for path in (data_dir / "outputs" / "alice").rglob("*") if path.is_file()]) == 3


def test_the_only_delete_route_is_logging_out(app):
    routes = list(routes_of(app.routes))
    assert ("/api/jobs/{job}", {"GET"}) in routes  # the walk finds the API's routes
    assert {path for path, methods in routes if "DELETE" in methods} == {"/api/session"}
    documented = {path for path, operations in TestClient(app).get("/openapi.json").json()["paths"].items() if "delete" in operations}
    assert documented == {"/api/session"}


def test_the_api_is_documented(app):
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert {
        "/api/health", "/api/info", "/api/models", "/api/jobs", "/api/jobs/{job}", "/api/jobs/{job}/references",
        "/api/jobs/{job}/images/{index}", "/api/jobs/{job}/references/{index}", "/api/stats", "/api/session",
        "/api/links/{secret}",
    } <= set(paths)  # fmt: skip
    assert json.dumps(paths).count("multipart/form-data") >= 2


def test_finished_jobs_have_no_upload_url_and_pending_ones_no_images(client):
    pending = submit(client, reference_count=1).json()
    assert pending["upload_url"] and pending["images"] == [] and pending["status"] == Status.PENDING
    assert generate(client)["upload_url"] is None


def test_the_images_of_last_are_never_cached(client):
    job = generate(client)
    assert client.get(f"/api/jobs/{job['id']}/images/1").headers["Cache-Control"] == "private, max-age=86400"
    assert client.get("/api/jobs/last/images/1").headers["Cache-Control"] == "no-cache"  # the next job changes it
    assert client.get("/api/jobs/last/images/1", params={"size": "medium"}).headers["Cache-Control"] == "no-cache"
