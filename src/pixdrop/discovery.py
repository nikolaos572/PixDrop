"""What the providers say their models accept, read from their model catalogues.

OpenRouter lists every image model with the parameters it accepts and their values
(GET {base_url}/images/models), for example:

    {"id": "openai/gpt-image-2.5-sunburst", "supported_parameters": {
        "quality": {"type": "enum", "values": ["auto", "low", "medium", "high", "xhigh", "max"]},
        "background": {"type": "enum", "values": ["auto", "transparent", "opaque"]},
        "n": {"type": "range", "min": 1, "max": 10},
        "input_references": {"type": "range", "min": 0, "max": 16}, ...}}

A model configured without "options" offers the parameters of its entry that PixDrop
knows (config.OPTIONS), with the model's own values and ranges; without
"max_reference_images", the entry's input_references gives its reference-image limits.
Parameters PixDrop does not know are logged, for the administrator (who can still set
them with "request"). The catalogues are read at startup, then every REFRESH_SECONDS,
and again within CHECK_SECONDS when config.json starts using a provider; a failed read
is retried after RETRY_SECONDS, and the last good catalogue is kept meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import httpx2

from .config import MAX_REFERENCES, OPTIONS, Capabilities, Model, ModelRegistry, OptionSpec, Provider

log = logging.getLogger(__name__)

REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 300
CHECK_SECONDS = 30
_ProviderKey = tuple[str, str, str]  # name, base URL, key: a provider whose URL or key changes is read again


def capabilities_of(entry: Mapping[str, Any]) -> Capabilities:
    """A model's capabilities from its entry in a catalogue."""
    parameters = entry.get("supported_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    specs: dict[str, OptionSpec] = {}
    unknown = []
    minimum, maximum = 0, MAX_REFERENCES
    for name, described in parameters.items():
        described = described if isinstance(described, dict) else {}
        if name == "input_references":
            low, high = _range(described)
            minimum, maximum = max(0, low or 0), min(MAX_REFERENCES, high if high is not None else MAX_REFERENCES)
        elif name in OPTIONS:
            spec = _narrowed(OPTIONS[name], described)
            if spec is not None:
                specs[name] = spec
        else:
            unknown.append(name)
    ordered = {name: specs[name] for name in OPTIONS if name in specs}  # in the catalogue's order, for the web page
    return Capabilities(specs=ordered, min_references=min(minimum, maximum), max_references=maximum, unknown=tuple(unknown))


def _narrowed(spec: OptionSpec, described: Mapping[str, Any]) -> OptionSpec | None:
    """The option with the model's values: an enumeration's own values, a range's own bounds.
    None when the model leaves no choice (n from 1 to 1, an empty enumeration)."""
    kind = described.get("type")
    if kind == "enum" and spec.kind == "integer":
        numbers = [value for value in described.get("values", []) if isinstance(value, int) and not isinstance(value, bool)]
        low, high = (min(numbers), max(numbers)) if numbers else (None, None)
        kind, described = "range", {"min": low, "max": high}
    if kind == "enum":
        values = tuple(str(value) for value in described.get("values", []) if isinstance(value, str | int | float))
        return replace(spec, values=values, minimum=None, maximum=None, pattern=None) if values else None
    if kind == "range" and spec.kind == "integer":
        assert spec.minimum is not None and spec.maximum is not None
        low, high = _range(described)
        low = spec.minimum if low is None else max(spec.minimum, low)
        high = spec.maximum if high is None else min(spec.maximum, high)
        return replace(spec, minimum=low, maximum=high) if low < high else None
    return spec  # "boolean" (supported, with PixDrop's own rule for its values) or a form PixDrop does not read


def _range(described: Mapping[str, Any]) -> tuple[int | None, int | None]:
    def integer(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return integer(described.get("min")), integer(described.get("max"))


def parse_catalog(document: object) -> dict[str, Capabilities]:
    """The capabilities of every model of a catalogue, by model id."""
    entries = document.get("data") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("the catalogue has no data list")
    return {entry["id"]: capabilities_of(entry) for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)}


class Catalogs:
    """The providers' catalogues, kept up to date for the models that rely on them."""

    def __init__(self, models: ModelRegistry, http: httpx2.AsyncClient, clock: Callable[[], float] = time.monotonic) -> None:
        self._models = models
        self._http = http
        self._clock = clock
        self._catalogs: dict[_ProviderKey, dict[str, Capabilities]] = {}
        self._attempts: dict[_ProviderKey, tuple[float, bool]] = {}  # when each was last read, and whether it worked
        models.capabilities = self.capabilities

    def capabilities(self, model: Model) -> Capabilities | None:
        return self._catalogs.get(_key(model.provider), {}).get(model.upstream)

    async def keep_up_to_date(self) -> None:
        """Read the catalogues when they are due, forever (the app's lifespan runs this)."""
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("reading the providers' model catalogues failed")
            await asyncio.sleep(CHECK_SECONDS)

    async def refresh(self, *, force: bool = False) -> None:
        """Read the catalogue of every provider that a model relies on, if it is due."""
        configured = self._models.configured()
        relying = [model for model in configured.models.values() if model.auto_options or model.auto_references]
        providers = {_key(model.provider): model.provider for model in relying}
        now = self._clock()
        for key, provider in providers.items():
            last = self._attempts.get(key)
            if not force and last is not None and now - last[0] < (REFRESH_SECONDS if last[1] else RETRY_SECONDS):
                continue
            try:
                catalog = await self._read(provider)
            except (httpx2.HTTPError, ValueError) as exc:
                self._attempts[key] = (now, False)
                kept = " (keeping the one read before)" if key in self._catalogs else ""
                log.warning("cannot read the model catalogue of %s%s: %s; retrying in %d minutes",
                            provider.name, kept, exc or type(exc).__name__, RETRY_SECONDS // 60)  # fmt: skip
                continue
            self._attempts[key] = (now, True)
            self._catalogs[key] = catalog
            self._models.version += 1
            self._report(provider, catalog, [model for model in relying if _key(model.provider) == key])

    async def _read(self, provider: Provider) -> dict[str, Capabilities]:
        headers = {**provider.headers, "Authorization": f"Bearer {provider.api_key}"}
        response = await self._http.get(f"{provider.base_url}/images/models", headers=headers, timeout=30)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        try:
            return parse_catalog(response.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"unexpected answer: {exc}") from None

    def _report(self, provider: Provider, catalog: dict[str, Capabilities], models: list[Model]) -> None:
        found = [model for model in models if model.upstream in catalog]
        log.info("read the model catalogue of %s (%d image models): options and limits for %d model(s)",
                 provider.name, len(catalog), len(found))  # fmt: skip
        for model in models:
            capabilities = catalog.get(model.upstream)
            if capabilities is None:
                missing = "it offers no options; name them with \"options\"" if model.auto_options else (
                    "its reference images stay limited to 16; set \"max_reference_images\"")
                log.warning("model %r (%s) is not in the catalogue of %s: %s in config.json",
                            model.name, model.upstream, provider.name, missing)  # fmt: skip
            elif capabilities.unknown and model.auto_options:
                log.info("model %r (%s) also accepts %s, which PixDrop does not offer (\"request\" in config.json can set them)",
                         model.name, model.upstream, ", ".join(capabilities.unknown))  # fmt: skip


def _key(provider: Provider) -> _ProviderKey:
    return (provider.name, provider.base_url, provider.api_key)
