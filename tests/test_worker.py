"""The generations (worker.Worker): images saved with the right names, results recorded, and every way a
generation can fail, each recorded with its reason."""

import base64
import logging
import re

import fake_provider
import httpx2
from fastapi.testclient import TestClient
from helpers import ALICE, BASE_URL, config_with, generate, gif, jpeg, png, record_of, submit, svg, wait_for, write_json

from pixdrop.worker import INTERRUPTED_BY_STOP


def files_of(data_dir, user="alice"):
    return sorted(path.name for path in (data_dir / "outputs" / user).iterdir())


def test_a_generation_saves_its_image_and_records_the_results(client, data_dir):
    job = generate(client)
    assert (job["status"], job["error"]) == ("completed", None)
    image = data_dir / "outputs" / "alice" / f"{job['id']}.png"
    assert image.read_bytes() == fake_provider.picture("a red fox #0 refs=0")  # the provider's image, as it sent it
    record = record_of(data_dir, "alice", job["id"])
    assert record["status"] == "completed"
    assert record["images"] == [
        {"index": 1, "filename": f"{job['id']}.png", "content_type": "image/png", "bytes": image.stat().st_size, "width": 1024, "height": 1024}
    ]
    assert record["usage"] == {"prompt_tokens": 12, "completion_tokens": 1290, "total_tokens": 1302, "cost": 0.04}
    assert record["duration_seconds"] >= 0
    assert record["created_at"] <= record["started_at"] <= record["finished_at"]
    assert (record["text"], record["upstream_id"], record["error"]) == (None, None, None)
    assert files_of(data_dir) == [f"{job['id']}.json", f"{job['id']}.png"]


def test_a_chat_model_records_its_text_and_id(client, data_dir):
    job = generate(client, model="nano-banana")
    record = record_of(data_dir, "alice", job["id"])
    assert (record["status"], record["text"]) == ("completed", "Here is your image.")
    assert record["upstream_id"].startswith("gen-fake-")
    assert record["images"][0]["filename"] == f"{job['id']}.png"


def test_several_images_are_numbered(client, data_dir):
    job = generate(client, n=3)
    assert [image["filename"] for image in job["images"]] == [f"{job['id']}.png", f"{job['id']}_2.png", f"{job['id']}_3.png"]
    assert [image["index"] for image in job["images"]] == [1, 2, 3]
    assert job["usage"]["cost_usd"] == 0.12
    assert files_of(data_dir) == sorted([f"{job['id']}.json", f"{job['id']}.png", f"{job['id']}_2.png", f"{job['id']}_3.png"])


def test_the_extension_follows_the_detected_format(client, data_dir):
    jpeg_job = generate(client, output_format="jpeg")
    webp_job = generate(client, output_format="webp", background="transparent")
    assert (jpeg_job["images"][0]["filename"], jpeg_job["images"][0]["content_type"]) == (f"{jpeg_job['id']}.jpg", "image/jpeg")
    assert (webp_job["images"][0]["filename"], webp_job["images"][0]["content_type"]) == (f"{webp_job['id']}.webp", "image/webp")


def test_the_extension_comes_from_the_content_not_from_what_was_asked(client, upstream):
    jpeg_bytes = base64.b64encode(jpeg(40, 20)).decode()
    upstream.answers.append(httpx2.Response(200, json={"data": [{"b64_json": jpeg_bytes, "media_type": "image/png"}]}))
    job = generate(client, output_format="png")
    assert job["images"][0] | {"url": None, "medium_url": None} == {
        "index": 1, "filename": f"{job['id']}.jpg", "content_type": "image/jpeg", "bytes": len(jpeg(40, 20)),
        "width": 40, "height": 20, "original_name": None, "url": None, "medium_url": None,
    }  # fmt: skip


def test_the_options_reach_the_provider(client, upstream):
    job = generate(client, aspect_ratio="16:9", quality="medium", n=2)
    assert upstream.bodies[-1] == {"model": "openai/gpt-5-image", "prompt": "a red fox", "quality": "medium", "aspect_ratio": "16:9", "n": 2}
    assert (job["images"][0]["width"], job["images"][0]["height"]) == (1024, 576)
    generate(client, model="nano-banana", resolution="1k", seed=7)
    assert upstream.bodies[-1]["image_config"] == {"image_size": "1K"}
    assert upstream.bodies[-1]["seed"] == 7


def test_reference_images_reach_the_provider_as_data_urls(client, upstream):
    references = [("knight.png", png(30, 20)), ("photo.jpg", jpeg(30, 20)), ("anim.gif", gif(30, 20, frames=2)), ("logo.svg", svg())]
    job = generate(client, "the knight from the back", references=references)
    urls = [part["image_url"]["url"] for part in upstream.bodies[-1]["input_references"]]
    assert urls[0] == f"data:image/png;base64,{base64.b64encode(png(30, 20)).decode()}"
    assert urls[1] == f"data:image/jpeg;base64,{base64.b64encode(jpeg(30, 20)).decode()}"
    assert urls[2].startswith("data:image/png;base64,")  # providers do not take GIF and SVG
    assert urls[3].startswith("data:image/png;base64,")
    assert job["usage"]["prompt_tokens"] == 12 + 4 * 300  # the fake counts what it got

    generate(client, "the knight from the back", model="nano-banana", references=references[:1])
    content = upstream.bodies[-1]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "the knight from the back"}
    assert content[1]["image_url"]["url"] == urls[0]


def test_the_server_answers_while_generations_run(client, upstream):
    first = submit(client, "GATE one").json()
    second = submit(client, "GATE two").json()
    assert client.get("/api/info").json()["account"]["in_progress"] == 2
    assert client.get(f"/api/jobs/{first['id']}", params={"wait": 0}).json()["status"] == "generating"
    upstream.gate.set()
    assert wait_for(client, first["id"])["status"] == "completed"
    assert wait_for(client, second["id"])["status"] == "completed"


# --- failures ---


def failed_with(client, prompt, error, **form):
    job = generate(client, prompt, **form)
    assert (job["status"], job["error"], job["images"]) == ("failed", error, [])
    assert job["duration_seconds"] is not None and job["finished_at"] is not None
    return job


def test_a_provider_error_is_recorded(client, data_dir):
    job = failed_with(client, "FAIL", "openrouter refused the request (HTTP 402): Insufficient credits. Add more using https://openrouter.ai/credits")
    assert record_of(data_dir, "alice", job["id"])["error"] == job["error"]
    assert files_of(data_dir) == [f"{job['id']}.json"]


def test_an_error_inside_an_answer_with_status_200_is_recorded(client):
    failed_with(client, "FAIL200", "openrouter reported an error: Provider returned error (code 502) [Fake]: upstream exploded")
    failed_with(client, "FAIL200", "openrouter reported an error: Provider returned error (code 502) [Fake]: upstream exploded", model="nano-banana")


def test_an_answer_without_an_image_is_recorded(client):
    failed_with(client, "NOIMAGE", "the model returned no image")
    failed_with(client, "NOIMAGE", "the model returned no image; it answered: I cannot draw that.", model="nano-banana")


def test_an_image_that_is_not_one_is_recorded_and_not_saved(client, data_dir):
    job = failed_with(client, "BADIMAGE", "the provider returned an unusable image: unsupported image type: use PNG, JPEG, WebP, GIF or SVG")
    assert files_of(data_dir) == [f"{job['id']}.json"]


def test_one_bad_image_among_several_saves_none(client, upstream, data_dir):
    good = base64.b64encode(png()).decode()
    damaged = base64.b64encode(png(200, 200)[:-200]).decode()
    upstream.answers.append(httpx2.Response(200, json={"data": [{"b64_json": good}, {"b64_json": damaged}]}))
    job = failed_with(client, "two", "the provider returned an unusable image: the image is damaged or cannot be read", n=2)
    assert files_of(data_dir) == [f"{job['id']}.json"]


def test_a_generation_that_takes_too_long_fails(make_app, data_dir):
    with TestClient(make_app(generation_timeout_seconds=0.2), headers=dict(ALICE)) as client:
        failed_with(client, "HANG", "no image after 0.2 seconds (GENERATION_TIMEOUT_SECONDS)")


def test_an_unreachable_provider_is_recorded(client, upstream):
    upstream.answers.append(httpx2.ConnectError("[Errno -2] Name or service not known"))
    failed_with(client, "x", "cannot reach openrouter: [Errno -2] Name or service not known")
    upstream.answers.append(httpx2.ReadTimeout("timed out"))
    failed_with(client, "x", "openrouter did not answer in time")


def test_an_unexpected_error_is_recorded_and_logged(client, upstream, caplog):
    upstream.answers.append(RuntimeError("a bug"))
    job = failed_with(client, "x", "internal error while generating; see the server log")
    [record] = [record for record in caplog.records if record.levelno == logging.ERROR and record.name == "pixdrop.worker"]
    assert record.getMessage() == f"job {job['id']} (alice): unexpected error"
    assert "RuntimeError: a bug" in caplog.text  # with its traceback


def test_stopping_the_server_fails_the_running_generations(app, data_dir, upstream):
    with TestClient(app, headers=dict(ALICE)) as client:
        job = submit(client, "HANG").json()
        for _ in range(1000):  # until the provider has the request
            if upstream.requests:
                break
            client.get("/api/health")
        assert upstream.requests
    record = record_of(data_dir, "alice", job["id"])
    assert (record["status"], record["error"]) == ("failed", INTERRUPTED_BY_STOP)
    assert INTERRUPTED_BY_STOP == "interrupted: the server stopped during the generation"
    assert record["finished_at"] is not None and record["duration_seconds"] is not None


# --- the configuration changes between the request and the generation ---


def pending_job(client):
    response = submit(client, reference_count=1)
    assert response.status_code == 201
    return response.json()


def upload_reference(client, job):
    response = client.post(f"/api/jobs/{job['id']}/references", files=[("reference", ("k.png", png()))])
    assert response.status_code == 200, response.text
    return wait_for(client, job["id"])


def test_a_model_changed_while_its_job_waits_fails_the_job(client, data_dir, upstream):
    job = pending_job(client)
    changed = config_with(**{"gpt-image": {"provider": "openrouter", "model": "openai/gpt-6-image", "api_type": "images"}})
    write_json(data_dir / "config.json", changed)
    done = upload_reference(client, job)
    assert (done["status"], done["error"]) == ("failed", "the configuration of the model 'gpt-image' changed since the job was made; submit it again")
    assert upstream.bodies == []  # never sent


def test_a_model_removed_while_its_job_waits_fails_the_job(client, data_dir, upstream):
    job = pending_job(client)
    write_json(data_dir / "config.json", config_with(**{"gpt-image": None}))
    done = upload_reference(client, job)
    assert (done["status"], done["error"]) == ("failed", "the model 'gpt-image' is no longer configured on this server")
    assert upstream.bodies == []


def test_a_job_keeps_its_options_when_the_models_options_change(client, data_dir, upstream):
    job = pending_job(client)
    write_json(data_dir / "config.json", config_with(**{"gpt-image": {"provider": "openrouter", "model": "openai/gpt-5-image", "api_type": "images"}}))
    done = upload_reference(client, job)
    assert done["status"] == "completed"
    assert upstream.bodies[-1]["quality"] == "high"  # as accepted when the job was made


def test_a_moved_provider_is_used_by_waiting_jobs(client, data_dir, upstream):
    job = pending_job(client)
    config = config_with()
    config["providers"]["openrouter"]["base_url"] = "https://eu.openrouter.test/api/v1"
    write_json(data_dir / "config.json", config)
    assert upload_reference(client, job)["status"] == "completed"
    assert str(upstream.requests[-1].url) == "https://eu.openrouter.test/api/v1/images"
    assert BASE_URL not in str(upstream.requests[-1].url)


# --- the log ---


def test_one_log_line_per_finished_job(client, caplog):
    caplog.set_level(logging.INFO)
    done = generate(client)
    failed = generate(client, "FAIL\nforged line")
    lines = [record.getMessage() for record in caplog.records if record.name == "pixdrop.worker"]
    assert len(lines) == 2
    assert re.fullmatch(rf"job {done['id']} \(alice\): gpt-image, completed in \d+\.\d s, 1 image\(s\), \$0\.0400", lines[0])
    assert re.fullmatch(
        rf"job {failed['id']} \(alice\): gpt-image, failed after \d+\.\d s: openrouter refused the request \(HTTP 402\): Insufficient credits\. .*",
        lines[1],
    )
    assert not any("forged" in line for line in lines)  # prompts stay out of the log


def test_a_failure_the_provider_answered_keeps_its_usage(client):
    # The provider answered (and may charge): the cost counts in the statistics.
    job = failed_with(client, "BADIMAGE", "the provider returned an unusable image: unsupported image type: use PNG, JPEG, WebP, GIF or SVG")
    assert job["usage"]["cost_usd"] == fake_provider.COST
    assert failed_with(client, "NOIMAGE", "the model returned no image")["usage"]["prompt_tokens"] == 12
    assert failed_with(client, "FAIL", "openrouter refused the request (HTTP 402): Insufficient credits. Add more using https://openrouter.ai/credits")["usage"] is None
