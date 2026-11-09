"""The providers and models file: validation, the option catalogue, and reloading while the server runs."""

import json
import logging
import re
from pathlib import Path

import pytest
from helpers import API_KEY, BASE_URL, CONFIG, config_with, touch, write_json

from pixdrop.config import OPENROUTER_URL, OPTIONS, ModelRegistry, parse_config
from pixdrop.errors import ConfigError, InvalidInput

EXAMPLE = Path(__file__).parent.parent / "src" / "pixdrop" / "default_config.json"


def provider(**fields):
    return {"providers": {"or": {"api_key": "sk-test", **fields}}, "models": {}}


def model(**fields):
    return {"providers": {"or": {"api_key": "sk-test"}}, "models": {"m": {"provider": "or", "model": "x/y", "api_type": "images", **fields}}}


def test_the_example_file_is_valid():
    catalog = parse_config(json.loads(EXAMPLE.read_text()))
    assert catalog.default == "sunburst"
    assert list(catalog.models) == ["sunburst", "qwen", "nano-banana", "flux", "vector"]
    assert all(model.auto_options and model.auto_references for model in catalog.models.values())  # the provider's catalogue
    assert dict(catalog.models["sunburst"].defaults) == {"quality": "high"}


def test_a_valid_configuration():
    catalog = parse_config(CONFIG, env={})
    assert (catalog.default, list(catalog.models)) == ("gpt-image", ["gpt-image", "nano-banana", "plain"])
    gpt = catalog.models["gpt-image"]
    assert (gpt.upstream, gpt.api_type, gpt.description) == ("openai/gpt-5-image", "images", "OpenAI's image model")
    assert gpt.options == ("aspect_ratio", "size", "quality", "background", "output_format", "n")
    assert (dict(gpt.defaults), gpt.max_reference_images, dict(gpt.request)) == ({"quality": "high"}, 16, {})
    assert (gpt.provider.name, gpt.provider.base_url, gpt.provider.api_key) == ("openrouter", BASE_URL, API_KEY)
    assert dict(gpt.provider.headers) == {"X-OpenRouter-Title": "PixDrop"}
    banana = catalog.models["nano-banana"]
    assert (banana.modalities, banana.max_reference_images) == (("image", "text"), 3)
    assert API_KEY not in repr(gpt)  # the key stays out of logs and tracebacks


def test_provider_defaults():
    catalog = parse_config({"providers": {"or": {"api_key": " sk-test "}}, "models": {"m": {"provider": "or", "model": "x/y", "api_type": "chat"}}})
    m = catalog.models["m"]
    assert (m.provider.base_url, m.provider.api_key, dict(m.provider.headers)) == (OPENROUTER_URL, "sk-test", {})
    assert (catalog.default, m.options, dict(m.defaults), m.description) == ("m", (), {}, "")


def test_the_base_url_loses_its_trailing_slash():
    document = model()
    document["providers"]["or"]["base_url"] = "http://127.0.0.1:9000/api/v1/"
    assert parse_config(document, env={}).models["m"].provider.base_url == "http://127.0.0.1:9000/api/v1"


def test_the_api_key_may_come_from_the_environment():
    document = {"providers": {"or": {"api_key_env": "OPENROUTER_KEY"}}, "models": model()["models"]}
    assert parse_config(document, env={"OPENROUTER_KEY": " sk-env "}).models["m"].provider.api_key == "sk-env"
    with pytest.raises(ConfigError, match="the environment variable OPENROUTER_KEY \\(api_key_env\\) is not set"):
        parse_config(document, env={})
    with pytest.raises(ConfigError, match="is not set"):
        parse_config(document, env={"OPENROUTER_KEY": "  "})


def test_the_registry_reads_the_key_from_its_environment(tmp_path, monkeypatch):
    path = write_json(tmp_path / "config.json", {"providers": {"or": {"api_key_env": "PIXDROP_TEST_KEY"}}, "models": model()["models"]})
    monkeypatch.setenv("PIXDROP_TEST_KEY", "sk-from-env")
    assert ModelRegistry(path).catalog().models["m"].provider.api_key == "sk-from-env"
    assert ModelRegistry(path, env={"PIXDROP_TEST_KEY": "other"}).catalog().models["m"].provider.api_key == "other"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "the file must be a JSON object"),
        ({"providers": {}, "users": {}}, "the file: unknown key\\(s\\) users"),
        ({"providers": []}, '"providers" must be a JSON object'),
        ({"models": []}, '"models" must be a JSON object'),
        ({"providers": {"bad name": {"api_key": "k"}}}, "invalid provider name 'bad name'"),
        (provider(api_key_env="X"), "give exactly one of api_key and api_key_env"),
        ({"providers": {"or": {}}}, "give exactly one of api_key and api_key_env"),
        (provider(api_key=""), "api_key must be a non-empty string"),
        ({"providers": {"or": {"api_key": 42}}}, "api_key must be a non-empty string"),
        (provider(base_url="ftp://example.com"), "base_url must be an http\\(s\\) URL"),
        (provider(base_url="https://example.com/v1?x=1"), "base_url must be an http\\(s\\) URL"),
        (provider(base_url="https:// example.com"), "base_url must be an http\\(s\\) URL"),
        (provider(headers={"Bad Header": "x"}), "invalid header 'Bad Header'"),
        (provider(headers={"X-Title": "a\r\nInjected: yes"}), "invalid header 'X-Title'"),
        (provider(headers={"X-Title": 5}), "invalid header 'X-Title'"),
        (provider(timeout=5), "unknown key\\(s\\) timeout"),
    ],
)
def test_invalid_providers_are_refused(document, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(document, env={})


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"provider": "nope"}, "unknown provider 'nope'"),
        ({"provider": None}, "provider must be a non-empty string"),
        ({"model": ""}, "model must be a non-empty string"),
        ({"api_type": "edits"}, 'api_type must be "images" or "chat"'),
        ({"api_type": None}, 'api_type must be "images" or "chat"'),
        ({"options": "quality"}, "options must be a list of distinct option names"),
        ({"options": ["quality", "quality"]}, "options must be a list of distinct option names"),
        ({"options": ["style"]}, "unknown option 'style'"),
        ({"api_type": "chat", "options": ["n"]}, "option 'n' is not available with api_type 'chat'"),
        ({"options": ["seed"], "defaults": {"quality": "high"}}, "default for 'quality', which is not in its options"),
        ({"defaults": {"style": "x"}}, "default for 'style', which is not in its options"),
        ({"options": ["quality"], "defaults": {"quality": "ultra"}}, "defaults: quality must be one of"),
        ({"options": ["n"], "defaults": {"n": 11}}, "defaults: n must be an integer from 1 to 10"),
        ({"max_reference_images": 17}, "max_reference_images must be an integer from 0 to 16"),
        ({"max_reference_images": -1}, "max_reference_images must be an integer from 0 to 16"),
        ({"max_reference_images": True}, "max_reference_images must be an integer from 0 to 16"),
        ({"max_reference_images": "4"}, "max_reference_images must be an integer from 0 to 16"),
        ({"modalities": ["image"]}, 'modalities is only for api_type "chat"'),
        ({"api_type": "chat", "modalities": ["text"]}, "modalities must be"),
        ({"api_type": "chat", "modalities": ["image", "audio"]}, "modalities must be"),
        ({"api_type": "chat", "modalities": "image"}, "modalities must be"),
        ({"request": []}, "request must be a JSON object"),
        ({"request": {"model": "other/model"}}, "request cannot set model"),
        ({"request": {"stream": True, "messages": []}}, "request cannot set messages, stream"),
        ({"request": {"prompt": "x", "input_references": [], "modalities": []}}, "request cannot set input_references, modalities, prompt"),
        ({"description": 5}, "description must be a non-empty string"),
        ({"price": 1}, "unknown key\\(s\\) price"),
    ],
)
def test_invalid_models_are_refused(fields, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(model(**fields), env={})


@pytest.mark.parametrize("name", ["", "-model", "a b", "a" * 101, "x?y"])
def test_invalid_model_names_are_refused(name):
    document = model()
    document["models"] = {name: document["models"]["m"]}
    with pytest.raises(ConfigError, match="invalid model name"):
        parse_config(document, env={})


def test_model_names_may_look_like_upstream_ids():
    document = model()
    document["models"] = {"openai/gpt-5-image:free@v2+x": document["models"]["m"]}
    assert list(parse_config(document, env={}).models) == ["openai/gpt-5-image:free@v2+x"]


def test_the_default_model():
    assert parse_config(model(), env={}).default == "m"  # the first one when not named
    with pytest.raises(ConfigError, match="default_model 'other' is not one of the models"):
        parse_config(model() | {"default_model": "other"}, env={})
    empty = parse_config({}, env={})
    assert (empty.models, empty.default) == ({}, None)


def test_choosing_a_model():
    catalog = parse_config(CONFIG, env={})
    assert catalog.model(None).name == "gpt-image"
    assert catalog.model("  ").name == "gpt-image"
    assert catalog.model(" nano-banana ").name == "nano-banana"
    with pytest.raises(InvalidInput, match="^unknown model 'dall-e'; available: gpt-image, nano-banana, plain$"):
        catalog.model("dall-e")
    with pytest.raises(InvalidInput, match="^no models are configured on this server$"):
        parse_config({}, env={}).model(None)


# --- the option catalogue ---


def test_the_catalogue():
    assert list(OPTIONS) == [
        "aspect_ratio", "resolution", "size", "quality", "background", "output_format", "output_compression", "n", "seed",
    ]  # fmt: skip
    assert OPTIONS["resolution"].values == ("512", "768", "1K", "1.5K", "2K", "4K")
    assert OPTIONS["quality"].values == ("auto", "low", "medium", "high", "xhigh", "max")
    assert OPTIONS["background"].values == ("auto", "transparent", "opaque")
    assert OPTIONS["output_format"].values == ("png", "jpeg", "webp", "svg")  # svg: vector models
    assert (OPTIONS["output_compression"].kind, OPTIONS["output_compression"].minimum, OPTIONS["output_compression"].maximum) == ("integer", 0, 100)
    assert {"1:1", "16:9", "9:16", "21:9", "auto"} <= set(OPTIONS["aspect_ratio"].values)
    assert (OPTIONS["n"].kind, OPTIONS["n"].minimum, OPTIONS["n"].maximum, OPTIONS["n"].api_types) == ("integer", 1, 10, ("images",))
    assert (OPTIONS["seed"].kind, OPTIONS["seed"].minimum, OPTIONS["seed"].maximum) == ("integer", 0, 2**32 - 1)
    assert OPTIONS["size"].kind == "string"


@pytest.mark.parametrize(
    ("name", "raw", "value"),
    [
        ("quality", "HIGH", "high"),  # case-insensitive, given back as listed
        ("quality", " low ", "low"),
        ("resolution", "1k", "1K"),
        ("resolution", "1.5k", "1.5K"),
        ("aspect_ratio", "16:9", "16:9"),
        ("aspect_ratio", "AUTO", "auto"),
        ("n", "3", 3),
        ("n", 10, 10),
        ("n", " 1 ", 1),
        ("seed", "0", 0),
        ("seed", str(2**32 - 1), 2**32 - 1),
        ("seed", "007", 7),
        ("size", "1024x1536", "1024x1536"),
        ("size", "auto", "auto"),
    ],
)
def test_option_values_are_parsed(name, raw, value):
    assert OPTIONS[name].parse(raw) == value


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("quality", "ultra", "quality must be one of: auto, low, medium, high, xhigh, max"),
        ("quality", 5, "quality must be one of"),
        ("aspect_ratio", "7:3", "aspect_ratio must be one of"),
        ("output_format", "gif", "output_format must be one of: png, jpeg, webp"),
        ("n", "0", "n must be an integer from 1 to 10"),
        ("n", "11", "n must be an integer from 1 to 10"),
        ("n", "two", "n must be an integer from 1 to 10"),
        ("n", "1.5", "n must be an integer from 1 to 10"),
        ("n", True, "n must be an integer from 1 to 10"),
        ("seed", "-1", "seed must be an integer from 0 to 4294967295"),
        ("seed", str(2**32), "seed must be an integer from 0 to 4294967295"),
        ("seed", "1e3", "seed must be an integer"),
        ("size", "1024", "size must match"),
        ("size", "1024X1024", "size must match"),
        ("size", "1x1", "size must match"),
        ("size", "1024x1024; rm -rf /", "size must match"),
        ("size", 1024, "size must match"),
    ],
)
def test_invalid_option_values_are_refused(name, raw, message):
    with pytest.raises(InvalidInput, match=message):
        OPTIONS[name].parse(raw)


def test_resolving_the_options_of_a_job():
    gpt = parse_config(CONFIG, env={}).models["gpt-image"]
    assert gpt.resolve_options({}) == {"quality": "high"}  # the model's defaults
    assert gpt.resolve_options({"quality": "LOW", "n": "2", "size": None, "background": ""}) == {"quality": "low", "n": 2}
    assert gpt.resolve_options({"background": "transparent", "output_format": "webp"}) == {
        "quality": "high", "background": "transparent", "output_format": "webp",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("model_name", "options", "message"),
    [
        ("nano-banana", {"quality": "high"}, "^model 'nano-banana' does not support 'quality'; its options: aspect_ratio, resolution, seed$"),
        ("plain", {"seed": 1}, "^model 'plain' does not support 'seed'; its options: none$"),
        ("gpt-image", {"style": "vivid"}, "^unknown option 'style'$"),
        ("gpt-image", {"background": "transparent", "output_format": "JPEG"}, "transparent background needs output_format png or webp"),
    ],
)
def test_options_a_model_does_not_take_are_refused(model_name, options, message):
    with pytest.raises(InvalidInput, match=message):
        parse_config(CONFIG, env={}).models[model_name].resolve_options(options)


# --- reloading ---


@pytest.fixture
def config_file(tmp_path):
    return write_json(tmp_path / "config.json", CONFIG)


def test_changes_are_picked_up_without_a_restart(config_file):
    registry = ModelRegistry(config_file, env={})
    assert list(registry.catalog().models) == ["gpt-image", "nano-banana", "plain"]
    new = {"provider": "openrouter", "model": "bytedance-seed/seedream-4.5", "api_type": "images", "options": ["seed"]}
    write_json(config_file, config_with(seedream=new, plain=None))
    assert list(registry.catalog().models) == ["gpt-image", "nano-banana", "seedream"]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("{not json", "Expecting property name"),
        (json.dumps(config_with(plain={"provider": "nope", "model": "x", "api_type": "images"})), "unknown provider 'nope'"),
    ],
)
def test_a_broken_edit_keeps_the_previous_configuration(config_file, caplog, content, reason):
    registry = ModelRegistry(config_file, env={})
    config_file.write_text(content)
    touch(config_file)
    with caplog.at_level(logging.ERROR):
        assert list(registry.catalog().models) == ["gpt-image", "nano-banana", "plain"]
        registry.catalog()  # read once: the error is logged once
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].startswith(f"ignoring invalid models file {config_file} (keeping the previous one): ")
    assert reason in errors[0]
    write_json(config_file, config_with(plain=None))  # fixed
    assert list(registry.catalog().models) == ["gpt-image", "nano-banana"]


def test_an_invalid_file_at_startup_is_an_error(tmp_path):
    path = write_json(tmp_path / "config.json", model(api_type="edits"))
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: model 'm': api_type must be"):
        ModelRegistry(path, env={})


def test_a_missing_file_means_no_models(tmp_path, caplog):
    registry = ModelRegistry(tmp_path / "config.json", env={})
    assert registry.catalog().models == {}
    assert "models file" in caplog.text and "not found" in caplog.text
    write_json(tmp_path / "config.json", CONFIG)  # created later: read then
    assert registry.catalog().default == "gpt-image"
    (tmp_path / "config.json").unlink()  # removed again: no models
    assert registry.catalog().models == {}


@pytest.mark.parametrize("key", ["sk-or-SECRET\u000bTAIL", "sk or key", "sk-ö-key"])
def test_an_api_key_must_be_visible_ascii(key):
    with pytest.raises(ConfigError) as refused:
        parse_config(provider(api_key=key), env={})
    assert str(refused.value) == "provider 'or': the API key may hold only visible ASCII characters, without spaces"
    assert key not in str(refused.value)


@pytest.mark.parametrize("value", ["tab\there", "line\nbreak", "naïve"])
def test_header_values_must_be_visible_ascii(value):
    with pytest.raises(ConfigError, match="invalid header 'X-Title'"):
        parse_config(provider(headers={"X-Title": value}), env={})
