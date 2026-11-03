"""The models' options from their providers' catalogues (discovery), and the models command."""

import asyncio
import copy
import json
import logging
import time

import fake_provider
import httpx2
import pytest
from fastapi.testclient import TestClient
from helpers import ALICE, API_KEY, BASE_URL, CONFIG, Clock, png, reference, run_cli, size_of, submit, wait_for, write_json

from pixdrop import discovery
from pixdrop.config import MAX_REFERENCES, OPTIONS, ModelRegistry
from pixdrop.discovery import Catalogs, capabilities_of, parse_catalog

# An entry as OpenRouter's GET /api/v1/images/models gives it (2026-11).
SUNBURST = {
    "id": "openai/gpt-image-2.5-sunburst",
    "name": "OpenAI: GPT Image 2.5 Sunburst",
    "supported_parameters": {
        "aspect_ratio": {"type": "enum", "values": ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "auto"]},
        "quality": {"type": "enum", "values": ["auto", "low", "medium", "high", "xhigh", "max"]},
        "background": {"type": "enum", "values": ["auto", "transparent", "opaque"]},
        "n": {"type": "range", "min": 1, "max": 10},
        "input_references": {"type": "range", "min": 0, "max": 16},
        "output_compression": {"type": "range", "min": 0, "max": 100},
    },
}
AUTO_CONFIG = {
    "providers": {"openrouter": {"base_url": BASE_URL, "api_key": API_KEY}},
    "default_model": "auto",
    "models": {
        "auto": {"provider": "openrouter", "model": "fake/images", "api_type": "images", "defaults": {"quality": "high", "size": "auto"}},
        "chat": {"provider": "openrouter", "model": "fake/chat", "api_type": "chat"},
        "editor": {"provider": "openrouter", "model": "fake/editor", "api_type": "images"},
        "pinned": {"provider": "openrouter", "model": "fake/images", "api_type": "images", "options": ["seed"], "max_reference_images": 1},
        "unlisted": {"provider": "openrouter", "model": "nobody/unlisted", "api_type": "images"},
    },
}


# --- reading an entry ---


def test_an_entry_gives_the_models_own_values():
    capabilities = capabilities_of(SUNBURST)
    assert list(capabilities.specs) == ["aspect_ratio", "quality", "background", "output_compression", "n"]  # the catalogue's order
    assert capabilities.specs["aspect_ratio"].values == ("1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "auto")
    assert capabilities.specs["background"].values == ("auto", "transparent", "opaque")
    assert (capabilities.specs["n"].minimum, capabilities.specs["n"].maximum) == (1, 10)
    assert (capabilities.min_references, capabilities.max_references, capabilities.unknown) == (0, 16, ())
    assert capabilities.specs["quality"].description == OPTIONS["quality"].description


def test_an_entry_narrows_ranges_and_drops_options_without_a_choice():
    capabilities = capabilities_of({"id": "m", "supported_parameters": {
        "n": {"type": "range", "min": 1, "max": 1},  # no choice: not offered
        "output_compression": {"type": "range", "min": 50, "max": 500},  # within PixDrop's own bounds
        "seed": {"type": "boolean"},  # supported: PixDrop's own rule for its values
        "size": {"type": "boolean"},
        "resolution": {"type": "enum", "values": []},
        "input_references": {"type": "range", "min": 1, "max": 20},  # at most OpenRouter's 16
        "style": {"type": "enum", "values": ["photo"]},
        "guidance": {"type": "range", "min": 1, "max": 9},
    }})  # fmt: skip
    assert list(capabilities.specs) == ["size", "output_compression", "seed"]
    assert (capabilities.specs["output_compression"].minimum, capabilities.specs["output_compression"].maximum) == (50, 100)
    assert capabilities.specs["seed"] == OPTIONS["seed"]
    assert (capabilities.min_references, capabilities.max_references) == (1, MAX_REFERENCES)
    assert capabilities.unknown == ("style", "guidance")


def test_new_values_of_the_provider_are_accepted():
    capabilities = capabilities_of({"id": "m", "supported_parameters": {"quality": {"type": "enum", "values": ["standard", "ultra"]}}})
    assert capabilities.specs["quality"].parse("ULTRA") == "ultra"  # not in PixDrop's catalogue, but the model's


def test_a_catalogue_is_read_by_model_id():
    catalog = parse_catalog(fake_provider.CATALOGUE)
    assert list(catalog) == ["fake/images", "fake/chat", "fake/editor"]
    assert (catalog["fake/editor"].min_references, catalog["fake/editor"].max_references) == (1, 2)
    with pytest.raises(ValueError):
        parse_catalog({"models": []})


# --- applying it to the configured models ---


@pytest.fixture
def registry(tmp_path):
    write_json(tmp_path / "config.json", AUTO_CONFIG)
    return ModelRegistry(tmp_path / "config.json")


def test_before_the_catalogue_is_read_models_without_options_offer_none(registry):
    auto = registry.catalog().models["auto"]
    assert (auto.options, auto.auto_options, auto.discovered, auto.max_reference_images) == ((), True, False, MAX_REFERENCES)
    assert dict(auto.defaults) == {"quality": "high", "size": "auto"}  # the administrator's, until the catalogue says


def test_the_catalogue_gives_the_options_and_limits(registry):
    catalog = parse_catalog(fake_provider.CATALOGUE)
    registry.capabilities = lambda model: catalog.get(model.upstream)
    registry.version += 1
    models = registry.catalog().models
    auto = models["auto"]
    assert auto.options == ("aspect_ratio", "quality", "background", "output_format", "output_compression", "n", "seed")
    assert auto.specs["quality"].values == ("low", "medium", "high")
    assert dict(auto.defaults) == {"quality": "high"}  # "size" is not one of the model's: dropped
    assert (auto.min_reference_images, auto.max_reference_images, auto.discovered) == (0, 4, True)
    assert models["chat"].options == ("aspect_ratio", "resolution")  # n from 1 to 1: no choice
    assert (models["editor"].min_reference_images, models["editor"].max_reference_images) == (1, 2)
    pinned = models["pinned"]
    assert (pinned.options, pinned.max_reference_images) == (("seed",), 1)  # config.json decides
    assert models["unlisted"].options == ()
    with pytest.raises(Exception, match=r"quality must be one of: low, medium, high"):
        auto.resolve_options({"quality": "max"})


# --- reading the catalogues ---


def make_catalogs(registry, answers, clock, keys=None):
    def handler(request):
        assert request.url == f"{BASE_URL}/images/models"
        if keys is not None:
            keys.append(request.headers["Authorization"])
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return Catalogs(registry, http, clock=clock)


def test_catalogues_are_read_again_after_a_while_and_retried_after_a_failure(registry, caplog):
    clock = Clock()
    ok = httpx2.Response(200, json=fake_provider.CATALOGUE)
    answers = [ok, httpx2.Response(503, text="busy"), httpx2.ConnectError("down"), ok]
    catalogs = make_catalogs(registry, answers, clock)
    caplog.set_level(logging.INFO, logger=discovery.__name__)

    asyncio.run(catalogs.refresh())  # the first read
    assert registry.catalog().models["auto"].discovered
    asyncio.run(catalogs.refresh())  # not due yet: nothing read
    assert len(answers) == 3
    clock.now += discovery.REFRESH_SECONDS
    asyncio.run(catalogs.refresh())  # due, fails: the catalogue read before is kept
    assert registry.catalog().models["auto"].options
    clock.now += discovery.RETRY_SECONDS
    asyncio.run(catalogs.refresh())  # retried, fails again
    clock.now += discovery.RETRY_SECONDS
    asyncio.run(catalogs.refresh())
    assert answers == []
    messages = [record.getMessage() for record in caplog.records]
    assert "read the model catalogue of openrouter (3 image models): options and limits for 3 model(s)" in messages
    assert "cannot read the model catalogue of openrouter (keeping the one read before): HTTP 503; retrying in 5 minutes" in messages
    assert any(m.startswith("model 'unlisted' (nobody/unlisted) is not in the catalogue of openrouter: it offers no options") for m in messages)
    assert any(m.startswith("model 'auto' (fake/images) also accepts style, which PixDrop does not offer") for m in messages)


def test_a_changed_provider_is_read_again_at_once(registry, tmp_path):
    keys = []
    catalogs = make_catalogs(registry, [httpx2.Response(200, json=fake_provider.CATALOGUE)] * 3, Clock(), keys)
    asyncio.run(catalogs.refresh())
    asyncio.run(catalogs.refresh())  # not due
    changed = copy.deepcopy(AUTO_CONFIG)
    changed["providers"]["openrouter"]["api_key"] = "sk-another-key-0123456789"
    write_json(tmp_path / "config.json", changed)
    asyncio.run(catalogs.refresh())  # a new key: read at once, with it
    assert keys == [f"Bearer {API_KEY}", "Bearer sk-another-key-0123456789"]


# --- through the API and MCP ---


@pytest.fixture
def auto_client(make_app, data_dir):
    write_json(data_dir / "config.json", AUTO_CONFIG | {"providers": CONFIG["providers"]})
    with TestClient(make_app(), headers=dict(ALICE)) as client:
        deadline = time.monotonic() + 10
        while client.get("/api/models").json()["models"][0]["options_from"] != "provider":
            assert time.monotonic() < deadline  # the catalogue is read in the background at startup
            time.sleep(0.01)
        yield client


def test_the_api_lists_each_models_own_options(auto_client):
    models = {model["name"]: model for model in auto_client.get("/api/models").json()["models"]}
    auto = models["auto"]
    assert [option["name"] for option in auto["options"]] == [
        "aspect_ratio", "quality", "background", "output_format", "output_compression", "n", "seed",
    ]  # fmt: skip
    quality = next(option for option in auto["options"] if option["name"] == "quality")
    assert (quality["values"], quality["default"]) == (["low", "medium", "high"], "high")
    assert (auto["min_reference_images"], auto["max_reference_images"]) == (0, 4)
    assert (models["editor"]["min_reference_images"], models["pinned"]["options_from"]) == (1, "config")
    assert models["unlisted"]["options_from"] == "pending"  # not in the catalogue: nothing applied


def test_the_models_values_and_limits_are_enforced(auto_client):
    assert submit(auto_client, quality="max").json()["detail"] == "quality must be one of: low, medium, high"
    assert submit(auto_client, model="editor").json()["detail"] == "model 'editor' needs at least 1 reference image(s), got 0"
    response = submit(auto_client, model="editor", references=[("front.png", png())], wait=30)
    assert (response.status_code, response.json()["status"]) == (201, "completed")


def test_a_vector_model_gives_an_svg(auto_client):
    job = submit(auto_client, "a round logo", output_format="svg", wait=30).json()
    assert job["status"] == "completed", job
    image = job["images"][0]
    assert (image["content_type"], image["filename"]) == ("image/svg+xml", f"{job['id']}.svg")
    full = auto_client.get(f"/api/jobs/{job['id']}/images/1")
    assert full.content.startswith(b"<svg") and "sandbox" in full.headers["Content-Security-Policy"]
    medium = auto_client.get(f"/api/jobs/{job['id']}/images/1", params={"size": "medium"})
    assert (medium.headers["content-type"], size_of(medium.content)) == ("image/png", (512, 512))


def test_mcp_lists_the_models_own_values(auto_client):
    response = auto_client.post(
        "/mcp", headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_models", "arguments": {}}},
    )  # fmt: skip
    models = {model["name"]: model for model in response.json()["result"]["structuredContent"]["models"]}
    background = next(option for option in models["auto"]["options"] if option["name"] == "background")
    assert background["values"] == ["auto", "transparent", "opaque"]


def test_the_options_of_a_waiting_job_are_those_it_was_made_with(auto_client):
    job = submit(auto_client, reference_count=1, quality="low").json()
    uploaded = auto_client.post(f"/api/jobs/{job['id']}/references", files=[reference("a.png", png())])
    assert uploaded.status_code == 200
    assert wait_for(auto_client, job["id"])["options"] == {"quality": "low"}


# --- the models command ---


def test_the_models_command_lists_the_providers_models(real_data):
    result = run_cli(real_data, "models")
    assert result.returncode == 0, result.stderr
    assert "fake: 3 image models" in result.stdout
    assert "  fake/images  [configured as 'fake-images']" in result.stdout
    assert "reference images 1-2; options: -" in result.stdout  # fake/editor


def test_the_models_command_shows_a_model_in_detail_with_a_configuration(real_data):
    result = run_cli(real_data, "models", "editor")
    assert result.returncode == 0, result.stderr
    assert "fake: 3 image models, 1 matching 'editor'" in result.stdout
    assert "reference images: 1-2" in result.stdout
    assert 'config.json: "editor": {"provider": "fake", "model": "fake/editor", "api_type": "images"}' in result.stdout
    images = run_cli(real_data, "models", "fake/images").stdout
    assert "quality: low medium high" in images and "output_compression: 0-100" in images
    assert "also accepts (not offered by PixDrop" in images and "style" in images
    entries = json.loads(run_cli(real_data, "models", "chat", "--json").stdout)
    assert [(entry["provider"], entry["id"]) for entry in entries] == [("fake", "fake/chat")]


def test_the_models_command_says_when_the_catalogue_cannot_be_read(data_dir):
    write_json(data_dir / "config.json", {"providers": {"down": {"base_url": "http://127.0.0.1:9/api/v1", "api_key": "k"}}})
    result = run_cli(data_dir, "models")
    assert result.returncode == 1 and "cannot read the model catalogue of down" in result.stderr
