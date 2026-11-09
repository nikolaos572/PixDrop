"""The administrator's commands on config.json: `setup` writes the default models with an API
key, and `modify-models` chooses which of the provider's models are offered.

Both go by what the provider offers now. `setup` writes nothing unless the provider accepts
the key and still offers every default model (default_config.json), and it replaces the whole
file, so running it again restores the defaults with a new key. The models are written
without "options": the server reads those from the catalogue (discovery), so they stay up
to date.
"""

from __future__ import annotations

import getpass
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx2

from .config import Provider, parse_config
from .discovery import auth_headers, read_entries, summary_of
from .errors import ConfigError
from .settings import Settings
from .storage import locked_file, write_secret_json

DEFAULTS = Path(__file__).with_name("default_config.json")
DEFAULT_PROVIDER = "openrouter"
KEYS_PAGE = "https://openrouter.ai/settings/keys"
CANCELLED = "Cancelled: nothing changed."
PICKED_UP = "The server picks this up by itself, and the models' options within 30 seconds."

_ModelKey = tuple[str, str]  # a provider's name, and its id of a model


def set_up(path: Path, key: str) -> list[str]:
    """Replace the file with the default models and this key, and describe the models in a line
    each. Writes nothing unless the provider accepts the key and offers every model."""
    document = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    document["providers"][DEFAULT_PROVIDER]["api_key"] = key
    catalog = parse_config(document)  # (a message about the key never shows it)
    provider = catalog.providers[DEFAULT_PROVIDER]
    _check_key(provider)
    offered = _catalogue(provider)
    missing = [model.upstream for model in catalog.models.values() if model.upstream not in offered]
    if missing:
        raise ConfigError(
            f"{provider.name} no longer offers these default models: {', '.join(missing)}. "
            f"Nothing was written: update PixDrop, or write {path} by hand (docs/CONFIGURATION.md)"
        )
    with locked_file(path) as real_path:
        write_secret_json(real_path, document, fresh=True)
    return [f"  {model.name}: {model.upstream}\n      {summary_of(offered[model.upstream])}" for model in catalog.models.values()]


def _check_key(provider: Provider) -> None:
    """Ask the provider about the key: its catalogue is public, and accepts any key."""
    try:
        response = httpx2.get(f"{provider.base_url}/key", headers=auth_headers(provider), timeout=30)
        document = response.json() if response.status_code == 200 else None
    except (httpx2.HTTPError, ValueError) as exc:
        raise ConfigError(f"cannot check the API key with {provider.name}: {exc or type(exc).__name__}") from exc
    if response.status_code in (401, 403):
        raise ConfigError(f"{provider.name} refused this API key")
    if response.status_code != 200:
        raise ConfigError(f"cannot check the API key with {provider.name}: HTTP {response.status_code}")
    details = document.get("data") if isinstance(document, dict) else None
    if isinstance(details, dict) and (details.get("is_management_key") or details.get("is_provisioning_key")):
        raise ConfigError("this is a management key, which cannot generate images: create a normal API key")


def _catalogue(provider: Provider) -> dict[str, dict[str, Any]]:
    """The provider's models now, by id."""
    try:
        return {entry["id"]: entry for entry in read_entries(provider)}
    except (httpx2.HTTPError, ValueError) as exc:
        raise ConfigError(f"cannot read the model catalogue of {provider.name}: {exc or type(exc).__name__}") from exc


def with_selection(document: dict[str, Any], chosen: set[_ModelKey], offered: Mapping[_ModelKey, Mapping[str, Any]]) -> dict[str, Any]:
    """The document with the models `chosen`: those it has stay exactly as they are, and those
    it lacks are added with the catalogue's description."""
    models = {name: model for name, model in document.get("models", {}).items() if _key_of(model) in chosen}
    present = {_key_of(model) for model in models.values()}
    for key in sorted(chosen - present):
        if key in offered:
            models[_new_name(key, models)] = _new_model(key, offered[key])
    if not models:
        raise ConfigError("no models would be left")
    return {**document, "models": models}


def _key_of(model: Mapping[str, Any]) -> _ModelKey:
    return model["provider"].strip(), model["model"].strip()


def _new_name(key: _ModelKey, taken: Iterable[str]) -> str:
    """The name users choose a new model by: the end of its id, or more of it if that is taken."""
    provider, upstream = key
    candidates = (upstream.split("/")[-1], upstream, f"{provider}/{upstream}")
    return next((name for name in candidates if name not in taken), candidates[-1])


def _new_model(key: _ModelKey, entry: Mapping[str, Any]) -> dict[str, Any]:
    provider, upstream = key
    model = {"provider": provider, "model": upstream, "api_type": "images"}
    if isinstance(description := entry.get("description"), str) and description.strip():
        model["description"] = description.strip()
    return model


def run_setup() -> int:
    """The `setup` command; returns the exit status."""
    try:
        path = Settings.from_env().config_file
        if path.exists():
            print(f"This replaces {path}: its API key and its models will be deleted.")
            print("Press Ctrl-C to keep them.")
        key = _ask_key()
        if not key:
            raise ConfigError("no API key given: run this in a terminal, with docker exec -it, or pipe the key to it")
        print("Checking the key and the default models with OpenRouter...")
        lines = set_up(path, key)
    except (OSError, ValueError) as exc:  # SettingsError and ConfigError are ValueErrors
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(f"\n{CANCELLED}", file=sys.stderr)
        return 1
    print(f"Wrote {path}, with {len(lines)} models:", *lines, sep="\n")
    print(PICKED_UP)
    print("To add or remove models: python -m pixdrop modify-models")
    return 0


def _ask_key() -> str:
    """The key, not shown as it is typed; a script can pipe it instead."""
    if sys.stdin.isatty():
        print(f"Your OpenRouter keys are at {KEYS_PAGE}")
        return getpass.getpass("API key (not shown as you type): ").strip()
    return sys.stdin.readline().strip()


def run_modify_models() -> int:
    """The `modify-models` command; returns the exit status."""
    try:
        if not _in_a_terminal():
            raise ConfigError("this command needs a terminal: run it with docker exec -it")
        return _modify_models(Settings.from_env().config_file)
    except (OSError, ValueError) as exc:  # SettingsError and ConfigError are ValueErrors
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(CANCELLED, file=sys.stderr)
        return 1


def _in_a_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _modify_models(path: Path) -> int:
    document = _read(path)
    catalog = parse_config(document)
    if not catalog.providers:
        raise ConfigError(f"{path} has no providers: run python -m pixdrop setup")
    print("Reading the models the providers offer now...")
    offered = {(provider.name, upstream): entry for provider in catalog.providers.values()
               for upstream, entry in _catalogue(provider).items()}  # fmt: skip
    configured = {(model.provider.name, model.upstream) for model in catalog.models.values()}
    chosen = _ask_models(offered, configured)
    default = _ask_default(list(with_selection(document, chosen, offered)["models"]), catalog.default)
    with locked_file(path) as real_path:  # the file as it is now: the questions may have taken minutes
        before = _read(real_path)
        after = with_selection(before, chosen, offered)
        after["default_model"] = default if default in after["models"] else next(iter(after["models"]))
        parse_config(after)
        if after == before:
            print("Nothing changed.")
            return 0
        write_secret_json(real_path, after)
    _print_changes(before, after, offered)
    print(f"Wrote {real_path}.")
    print(PICKED_UP)
    return 0


def _read(path: Path) -> dict[str, Any]:
    """The document, checked: a broken file stays as it is, for the administrator to fix."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        parse_config(document)
    except FileNotFoundError:
        raise ConfigError(f"there is no {path}: run python -m pixdrop setup first") from None
    except ValueError as exc:  # ConfigError, and JSON and encoding errors
        raise ConfigError(f"{path}: {exc}") from exc
    return document


def _ask_models(offered: Mapping[_ModelKey, Mapping[str, Any]], configured: set[_ModelKey]) -> set[_ModelKey]:
    """The models ticked in a checklist that starts with the configured ones."""
    import questionary  # (here because only these prompts need it: the server does not load it)

    several = len({provider for provider, _ in offered.keys() | configured}) > 1

    def choice(key: _ModelKey) -> questionary.Choice:
        title = f"{key[0]}: {key[1]}" if several else key[1]
        entry = offered.get(key)
        return questionary.Choice(
            title if entry else f"{title} (no longer offered)",
            value=key,
            checked=key in configured,
            description=summary_of(entry) if entry else "It is not in the catalogue: generating with it may fail.",
        )

    ticked = questionary.checkbox(
        "Models to offer:",
        choices=[choice(key) for key in sorted(offered.keys() | configured, key=lambda key: (key not in configured, key))],
        validate=lambda ticked: bool(ticked) or "Choose at least one model.",
        use_search_filter=True,
        use_jk_keys=False,
        instruction="(space ticks, typing searches, enter saves, ctrl-c cancels)",
    ).unsafe_ask()
    return set(ticked)


def _ask_default(names: list[str], current: str | None) -> str:
    """The default model, with the current one offered first."""
    import questionary

    if len(names) == 1:
        return names[0]
    return questionary.select("Default model:", choices=names, default=current if current in names else None).unsafe_ask()


def _print_changes(before: dict[str, Any], after: dict[str, Any], offered: Mapping[_ModelKey, Mapping[str, Any]]) -> None:
    for name in sorted(after["models"].keys() - before["models"].keys()):
        print(f"Added {name}: {summary_of(offered[_key_of(after['models'][name])])}")
    for name in sorted(before["models"].keys() - after["models"].keys()):
        print(f"Removed {name}")
    print(f"Default model: {after['default_model']}")
