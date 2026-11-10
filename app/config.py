"""Providers and models, read from a JSON file the administrator edits (CONFIG_FILE).

    {
      "providers": {"openrouter": {"api_key": "sk-or-..."}},
      "default_model": "sunburst",
      "models": {
        "sunburst": {"provider": "openrouter", "model": "openai/gpt-image-2.5-sunburst", "api_type": "images",
                     "defaults": {"quality": "high"}}
      }
    }

A provider is an OpenRouter-compatible API: a base URL and a key. A model is the name
clients use (the key of "models"), the provider's model id, and the provider's API it is
called through: "images" (POST /images) or "chat" (POST /chat/completions with image
output). Each model accepts some options of a fixed catalogue (OPTIONS): those its
provider lists for it, with the model's own values and its reference-image limits (see
discovery), unless config.json names them ("options", "max_reference_images"). The values
are checked here, the same way for the web page, the API and the MCP server. The file is
re-read when it changes, so models can be added without a restart.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError, InvalidInput
from .watched import WatchedJsonFile

OPENROUTER_URL = "https://openrouter.ai/api/v1"
API_TYPES = ("images", "chat")
MAX_REFERENCES = 16  # reference images per job: OpenRouter's limit
MODEL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}")
PROVIDER_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
_HEADER_VALUE = re.compile(r"[\x20-\x7e]*")  # visible ASCII and spaces: what HTTP headers carry safely
_API_KEY = re.compile(r"[\x21-\x7e]+")
# Fields of the upstream request that PixDrop fills in itself, which "request" cannot replace.
PROTECTED_FIELDS = frozenset({"model", "prompt", "messages", "input_references", "modalities", "stream"})


@dataclass(frozen=True)
class OptionSpec:
    """An option of the catalogue: an enumeration, an integer range or a string pattern."""

    name: str
    description: str
    values: tuple[str, ...] = ()  # an enumeration
    minimum: int | None = None  # an integer
    maximum: int | None = None
    pattern: str | None = None  # a string
    api_types: tuple[str, ...] = API_TYPES

    @property
    def kind(self) -> str:
        return "enum" if self.values else "integer" if self.minimum is not None else "string"

    def parse(self, raw: object) -> str | int:
        """The option's value from what a client (or the config) gave, or InvalidInput."""
        if self.kind == "integer":
            value = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
            if isinstance(raw, str) and re.fullmatch(r"\s*\d{1,12}\s*", raw):
                value = int(raw)
            assert self.minimum is not None and self.maximum is not None
            if value is None or not self.minimum <= value <= self.maximum:
                raise InvalidInput(f"{self.name} must be an integer from {self.minimum} to {self.maximum}")
            return value
        text = raw.strip() if isinstance(raw, str) else None
        if self.kind == "enum":
            for value in self.values:  # case-insensitive, returned as listed
                if text is not None and text.lower() == value.lower():
                    return value
            raise InvalidInput(f"{self.name} must be one of: {', '.join(self.values)}")
        assert self.pattern is not None
        if text is None or not re.fullmatch(self.pattern, text):
            raise InvalidInput(f"{self.name} must match {self.pattern} (got {raw!r})")
        return text


OPTIONS: dict[str, OptionSpec] = {
    spec.name: spec
    for spec in (
        OptionSpec(
            "aspect_ratio",
            "Width to height; providers clamp to the ratios they support",
            values=(
                "1:1", "1:2", "1:4", "1:8", "2:1", "2:3", "2.35:1", "3:2", "3:4", "4:1", "4:3", "4:5", "5:2",
                "5:4", "5:7", "7:5", "8:1", "9:16", "16:9", "9:19.5", "19.5:9", "9:20", "20:9", "9:21", "21:9",
                "auto",
            ),
        ),  # fmt: skip
        OptionSpec("resolution", "Resolution tier", values=("512", "768", "1K", "1.5K", "2K", "4K")),
        OptionSpec("size", "Exact size in pixels, WIDTHxHEIGHT (e.g. 1024x1024), or auto", pattern=r"auto|\d{2,5}x\d{2,5}"),
        OptionSpec("quality", "Rendering quality", values=("auto", "low", "medium", "high", "xhigh", "max")),
        OptionSpec(
            "background",
            "Background; transparent needs png or webp output",
            values=("auto", "transparent", "opaque"),
        ),
        OptionSpec("output_format", "File format of the image (svg: vector models)", values=("png", "jpeg", "webp", "svg")),
        OptionSpec("output_compression", "Compression of jpeg and webp images, 0-100", minimum=0, maximum=100),
        OptionSpec("n", "Number of images to generate in one job", minimum=1, maximum=10, api_types=("images",)),
        OptionSpec("seed", "Seed, for repeatable results where the model supports it", minimum=0, maximum=2**32 - 1),
    )
}


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Model:
    name: str  # what clients ask for
    provider: Provider
    upstream: str  # the provider's model id
    api_type: str  # "images" or "chat"
    description: str = ""
    specs: Mapping[str, OptionSpec] = field(default_factory=dict)  # its options, with its own values
    defaults: Mapping[str, str | int] = field(default_factory=dict)
    max_reference_images: int = MAX_REFERENCES
    min_reference_images: int = 0
    modalities: tuple[str, ...] = ("image", "text")  # chat only
    request: Mapping[str, Any] = field(default_factory=dict)  # merged into every upstream body
    auto_options: bool = False  # its options come from the provider's catalogue ("options" not in config.json)
    auto_references: bool = False  # its reference-image limits too ("max_reference_images" not given)
    discovered: bool = False  # the provider's catalogue has been applied

    @property
    def options(self) -> tuple[str, ...]:
        return tuple(self.specs)

    def resolve_options(self, requested: Mapping[str, object]) -> dict[str, str | int]:
        """The options of a job: the model's defaults overridden by the requested ones, which
        must be options of this model. None (a field left empty) means not requested."""
        options: dict[str, str | int] = dict(self.defaults)
        for name, raw in requested.items():
            if raw is None or raw == "":
                continue
            if name not in OPTIONS:
                raise InvalidInput(f"unknown option {name!r}")
            if name not in self.specs:
                supported = ", ".join(self.specs) or "none"
                raise InvalidInput(f"model {self.name!r} does not support {name!r}; its options: {supported}")
            options[name] = self.specs[name].parse(raw)
        if options.get("background") == "transparent" and options.get("output_format") == "jpeg":
            raise InvalidInput("a transparent background needs output_format png or webp, not jpeg")
        return options


@dataclass(frozen=True)
class Catalog:
    models: Mapping[str, Model] = field(default_factory=dict)
    default: str | None = None
    providers: Mapping[str, Provider] = field(default_factory=dict)

    def model(self, name: str | None) -> Model:
        """The named model, or the default one; InvalidInput if there is none."""
        if not self.models:
            raise InvalidInput("no models are configured on this server")
        chosen = (name or "").strip() or self.default
        assert chosen is not None  # there is a default whenever there are models
        model = self.models.get(chosen)
        if model is None:
            raise InvalidInput(f"unknown model {chosen!r}; available: {', '.join(self.models)}")
        return model


class ModelRegistry:
    """The current providers and models, reloaded when the file changes, with what the
    providers' catalogues say about the models applied (`capabilities`, see discovery).
    Without the file, there are no models and every generation is refused."""

    def __init__(self, path: Path, env: Mapping[str, str] = os.environ) -> None:
        self._file = WatchedJsonFile(path, lambda raw: parse_config(raw, env), Catalog(), "models")
        self.capabilities: Callable[[Model], Capabilities | None] = lambda model: None
        self.version = 0  # raised when the capabilities change
        self._applied: tuple[tuple[int, int], Catalog] | None = None

    def configured(self) -> Catalog:
        """The models as config.json describes them."""
        return self._file.current()

    def catalog(self) -> Catalog:
        """The models as they are offered: with their providers' capabilities applied."""
        configured = self._file.current()
        key = (id(configured), self.version)
        if self._applied is None or self._applied[0] != key:
            models = {name: _applied(model, self.capabilities(model)) for name, model in configured.models.items()}
            self._applied = (key, replace(configured, models=models))
        return self._applied[1]


@dataclass(frozen=True)
class Capabilities:
    """What a provider's catalogue says a model accepts (see discovery)."""

    specs: Mapping[str, OptionSpec]  # the options PixDrop knows, with the model's values
    min_references: int = 0
    max_references: int = MAX_REFERENCES
    unknown: tuple[str, ...] = ()  # parameters the catalogue lists that PixDrop does not offer


def _applied(model: Model, capabilities: Capabilities | None) -> Model:
    """The model with its provider's capabilities, for what config.json leaves to them."""
    if capabilities is None or not (model.auto_options or model.auto_references):
        return model
    changes: dict[str, Any] = {"discovered": True}
    if model.auto_options:
        specs = {name: spec for name, spec in capabilities.specs.items() if model.api_type in spec.api_types}
        defaults = {}
        for name, value in model.defaults.items():  # those the model accepts: a default it lacks is dropped
            try:
                defaults[name] = specs[name].parse(value)
            except (KeyError, InvalidInput):
                pass
        changes |= {"specs": specs, "defaults": defaults}
    if model.auto_references:
        changes |= {"min_reference_images": capabilities.min_references, "max_reference_images": capabilities.max_references}
    return replace(model, **changes)


def parse_config(raw: object, env: Mapping[str, str] = os.environ) -> Catalog:
    """Validate the decoded JSON document (api_key_env is looked up in `env`)."""
    document = _object(raw, "the file", {"providers", "models", "default_model"})
    providers = {
        name: _provider(name, value, env)
        for name, value in _object(document.get("providers", {}), '"providers"').items()
    }
    models: dict[str, Model] = {}
    for name, value in _object(document.get("models", {}), '"models"').items():
        if not MODEL_NAME_PATTERN.fullmatch(name):
            raise ConfigError(f"invalid model name {name!r}: use up to 100 of A-Z a-z 0-9 . _ : / @ + -")
        models[name] = _model(name, value, providers)
    default = document.get("default_model")
    if default is not None and default not in models:
        raise ConfigError(f"default_model {default!r} is not one of the models")
    return Catalog(models=models, default=default or next(iter(models), None), providers=providers)


def _provider(name: str, raw: object, env: Mapping[str, str]) -> Provider:
    if not PROVIDER_NAME_PATTERN.fullmatch(name):
        raise ConfigError(f"invalid provider name {name!r}")
    where = f"provider {name!r}"
    value = _object(raw, where, {"base_url", "api_key", "api_key_env", "headers"})
    base_url = _string(value.get("base_url", OPENROUTER_URL), f"{where}: base_url").rstrip("/")
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", base_url):
        raise ConfigError(f"{where}: base_url must be an http(s) URL, got {base_url!r}")
    if ("api_key" in value) == ("api_key_env" in value):
        raise ConfigError(f"{where}: give exactly one of api_key and api_key_env")
    if "api_key" in value:
        api_key = _string(value["api_key"], f"{where}: api_key")
    else:
        variable = _string(value["api_key_env"], f"{where}: api_key_env")
        api_key = env.get(variable, "").strip()
        if not api_key:
            raise ConfigError(f"{where}: the environment variable {variable} (api_key_env) is not set")
    if not _API_KEY.fullmatch(api_key):  # (the message never shows the key)
        raise ConfigError(f"{where}: the API key may hold only visible ASCII characters, without spaces")
    headers = _object(value.get("headers", {}), f"{where}: headers")
    for header, content in headers.items():
        if not _HEADER_NAME.fullmatch(header) or not isinstance(content, str) or not _HEADER_VALUE.fullmatch(content):
            raise ConfigError(f"{where}: invalid header {header!r} (a name of A-Z a-z 0-9 -, a value of visible ASCII)")
    return Provider(name=name, base_url=base_url, api_key=api_key, headers=dict(headers))


def _model(name: str, raw: object, providers: Mapping[str, Provider]) -> Model:
    where = f"model {name!r}"
    keys = {"provider", "model", "api_type", "description", "options", "defaults", "max_reference_images",
            "modalities", "request"}  # fmt: skip
    value = _object(raw, where, keys)
    provider = providers.get(_string(value.get("provider"), f"{where}: provider"))
    if provider is None:
        raise ConfigError(f"{where}: unknown provider {value.get('provider')!r}")
    api_type = value.get("api_type")
    if api_type not in API_TYPES:
        raise ConfigError(f'{where}: api_type must be "images" or "chat"')
    auto_options = "options" not in value  # then the provider's catalogue says
    options = value.get("options", [])
    if not isinstance(options, list) or len(set(options)) != len(options):
        raise ConfigError(f"{where}: options must be a list of distinct option names (or left out: the provider's)")
    for option in options:
        if option not in OPTIONS:
            raise ConfigError(f"{where}: unknown option {option!r}; the options are: {', '.join(OPTIONS)}")
        if api_type not in OPTIONS[option].api_types:
            raise ConfigError(f"{where}: option {option!r} is not available with api_type {api_type!r}")
    defaults: dict[str, str | int] = {}
    for option, default in _object(value.get("defaults", {}), f"{where}: defaults").items():
        if option not in (OPTIONS if auto_options else options):
            raise ConfigError(f"{where}: default for {option!r}, which is not in its options")
        try:
            defaults[option] = OPTIONS[option].parse(default)
        except InvalidInput as exc:
            raise ConfigError(f"{where}: defaults: {exc}") from None
    auto_references = "max_reference_images" not in value
    references = value.get("max_reference_images", MAX_REFERENCES)
    if not isinstance(references, int) or isinstance(references, bool) or not 0 <= references <= MAX_REFERENCES:
        raise ConfigError(f"{where}: max_reference_images must be an integer from 0 to {MAX_REFERENCES}")
    modalities = value.get("modalities", ["image", "text"])
    if api_type != "chat" and "modalities" in value:
        raise ConfigError(f'{where}: modalities is only for api_type "chat"')
    if not isinstance(modalities, list) or "image" not in modalities or not set(modalities) <= {"image", "text"}:
        raise ConfigError(f'{where}: modalities must be ["image"] or ["image", "text"]')
    request = _object(value.get("request", {}), f"{where}: request")
    if protected := PROTECTED_FIELDS & request.keys():
        raise ConfigError(f"{where}: request cannot set {', '.join(sorted(protected))}")
    return Model(
        name=name,
        provider=provider,
        upstream=_string(value.get("model"), f"{where}: model"),
        api_type=api_type,
        description=_string(value.get("description", ""), f"{where}: description", empty=True),
        specs={option: OPTIONS[option] for option in options},  # none yet when the provider's catalogue decides
        defaults=defaults,
        max_reference_images=references,
        modalities=tuple(modalities),
        request=request,
        auto_options=auto_options,
        auto_references=auto_references,
    )


def _object(raw: object, where: str, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a JSON object")
    if keys is not None and (unknown := raw.keys() - keys):
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(keys))}")
    return raw


def _string(raw: object, where: str, *, empty: bool = False) -> str:
    if not isinstance(raw, str) or not (raw.strip() or empty):
        raise ConfigError(f"{where} must be a non-empty string")
    return raw.strip()
