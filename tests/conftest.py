from __future__ import annotations

import dataclasses
import functools
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import fake_provider
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import ALICE, BOB, CONFIG, TOKENS, FakeUpstream, share, write_json

from pixdrop import images
from pixdrop.app import create_app
from pixdrop.settings import Settings

AppFactory = Callable[..., FastAPI]


@pytest.fixture(autouse=True)
def _fresh_medium_cache() -> None:
    """The medium sizes are cached by path; each test has its own files."""
    images.clear_cache()


@pytest.fixture(autouse=True, scope="session")
def _quick_fake_pictures() -> Iterator[None]:
    """The fake provider draws the same picture for the same prompt: draw each once (it takes 40 ms)."""
    original = fake_provider.picture
    fake_provider.picture = functools.lru_cache(maxsize=256)(original)
    yield
    fake_provider.picture = original


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data directory with the test users (users.json) and models (config.json)."""
    data = tmp_path / "data"
    data.mkdir()
    write_json(data / "users.json", {"users": TOKENS})
    write_json(data / "config.json", CONFIG)
    return data


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        timezone=ZoneInfo("Europe/Athens"),
        config_file=data_dir / "config.json",
        users_file=data_dir / "users.json",
        max_upload_bytes=1024**2,
        max_active_jobs=4,
        enable_mcp=True,  # off by default (test_settings); most tests use it
    )


@pytest.fixture
def upstream() -> FakeUpstream:
    """The providers, faked: see helpers.FakeUpstream."""
    return FakeUpstream()


@pytest.fixture
def make_app(settings: Settings, upstream: FakeUpstream) -> AppFactory:
    """make_app(**changes) -> an app with these settings changed, talking to the fake providers."""

    def make(**changes: object) -> FastAPI:
        return create_app(dataclasses.replace(settings, **changes), transport=httpx2.MockTransport(upstream))

    return make


@pytest.fixture
def app(make_app: AppFactory) -> FastAPI:
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Alice's client, with the app running (its lifespan starts the generations)."""
    with TestClient(app, headers=dict(ALICE)) as client:
        yield client


@pytest.fixture
def bob(client: TestClient) -> TestClient:
    """Bob's client, of the app that `client` runs."""
    return share(client, headers=BOB)


@pytest.fixture
def anonymous(client: TestClient) -> TestClient:
    """A client without a token (a browser before logging in, or curl with a transfer link)."""
    return share(client)


@pytest.fixture
def provider_url() -> Iterator[str]:
    """tests/fake_provider.py listening on a free port, for the real server: its base URL."""
    server = fake_provider.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture
def real_data(data_dir: Path, provider_url: str) -> Path:
    """A data directory whose models use the fake provider over HTTP, for the real server."""
    config = {
        "providers": {"fake": {"base_url": provider_url, "api_key": "fake-key"}},
        "models": {
            "fake-images": {"provider": "fake", "model": "fake/images", "api_type": "images", "options": ["n"]},
            "fake-chat": {"provider": "fake", "model": "fake/chat", "api_type": "chat"},
        },
    }
    write_json(data_dir / "config.json", config)
    return data_dir
