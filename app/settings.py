"""Runtime settings, read once from environment variables at startup."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SettingsError(ValueError):
    """An environment variable has an invalid value."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    timezone: ZoneInfo
    config_file: Path  # providers and models
    users_file: Path  # users and their tokens
    public_base_url: str = ""  # base of the URLs handed out, e.g. https://pixdrop.example.com (no path); "": the request's
    generation_timeout_seconds: float = 180  # one request to the provider
    max_upload_bytes: int = 20 * 1024**2  # one reference image
    max_active_jobs: int = 4  # per user: jobs waiting for references or generating
    medium_image_px: int = 512  # longest side of the medium size
    enable_mcp: bool = False  # serve the MCP endpoint for AI agents, and its transfer links (as TeXdrop: off by default)
    link_expiry_seconds: float = 3600  # a transfer link (and a job waiting for its references) expires then
    forwarded_allow_ips: str = "127.0.0.1"  # reverse proxies whose X-Forwarded-* headers are trusted ("*": any peer)
    host: str = "0.0.0.0"  # where the server listens (without Docker; Docker maps the port instead)
    port: int = 8000

    @staticmethod
    def data_dir_from_env(env: Mapping[str, str] = os.environ) -> Path:
        """DATA_DIR alone, which is valid even when other variables are not."""
        return Path(env.get("DATA_DIR", "").strip() or "/data").absolute()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        data_dir = cls.data_dir_from_env(env)
        return cls(
            data_dir=data_dir,
            timezone=_timezone(env.get("TZ", "UTC")),
            config_file=_path(env, "CONFIG_FILE", data_dir / "config.json"),
            users_file=_path(env, "USERS_FILE", data_dir / "users.json"),
            public_base_url=_base_url(env.get("PUBLIC_BASE_URL", "")),
            generation_timeout_seconds=_integer(env, "GENERATION_TIMEOUT_SECONDS", default=180, minimum=10),
            max_upload_bytes=1024**2 * _integer(env, "MAX_UPLOAD_MB", default=20, minimum=1, maximum=1024),
            max_active_jobs=_integer(env, "MAX_ACTIVE_JOBS", default=4, minimum=1),
            medium_image_px=_integer(env, "MEDIUM_IMAGE_PX", default=512, minimum=64, maximum=4096),
            enable_mcp=_flag(env, "ENABLE_MCP", default=False),
            link_expiry_seconds=60 * _integer(env, "LINK_EXPIRY_MINUTES", default=60, minimum=1),
            forwarded_allow_ips=_proxies(env.get("FORWARDED_ALLOW_IPS", "")),
            host=env.get("HOST", "").strip() or "0.0.0.0",
            port=_integer(env, "PORT", default=8000, minimum=1, maximum=65535),
        )


def _integer(env: Mapping[str, str], name: str, *, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} must be at most {maximum}, got {value}")
    return value


def _flag(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    raise SettingsError(f"{name} must be 1 or 0, got {raw!r}")


def _proxies(raw: str) -> str:
    """FORWARDED_ALLOW_IPS, checked: uvicorn ignores an entry it cannot read (such as
    172.17.0.1/16) without a word, and every visitor would keep the proxy's address."""
    raw = raw.strip()
    if raw in ("", "*"):
        return raw or "127.0.0.1"
    entries = [entry.strip() for entry in raw.split(",")]
    for entry in entries:
        try:
            ipaddress.ip_network(entry) if "/" in entry else ipaddress.ip_address(entry)
        except ValueError:
            raise SettingsError(
                "FORWARDED_ALLOW_IPS must be IP addresses or networks separated by commas, such as 172.17.0.1 "
                f"or 172.18.0.0/16, or * alone, got {entry!r}"
            ) from None
    return ",".join(entries)


def _path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name, "").strip()
    return Path(raw).absolute() if raw else default


def _base_url(raw: str) -> str:
    """PUBLIC_BASE_URL without its trailing slash. The service is served at the root of its
    address (the web page uses paths such as /api/jobs), so the URL has no path."""
    url = raw.strip().rstrip("/")
    if not url:
        return ""
    parts = urlsplit(url)
    if url_origin(url) is None or "@" in parts.netloc or parts.path or parts.query or parts.fragment:
        raise SettingsError(f"PUBLIC_BASE_URL must look like https://pixdrop.example.com (without a path), got {raw!r}")
    return url


_DEFAULT_PORTS = {"http": 80, "https": 443}


def url_origin(url: str) -> str | None:
    """The origin of an http(s) URL, written one way: scheme://host[:port], lowercased and
    without the scheme's default port, as browsers write their Origin header. None for anything
    else, such as the "null" of sandboxed pages."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), parts.hostname  # lowercased, without an IPv6 address's brackets
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}" if port in (None, _DEFAULT_PORTS[scheme]) else f"{scheme}://{host}:{port}"


def _timezone(name: str) -> ZoneInfo:
    # TZ may be a POSIX path like ":/etc/localtime"; only IANA names are supported here.
    try:
        return ZoneInfo(name.strip() or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise SettingsError(f"TZ must be an IANA time zone such as 'Europe/Athens', got {name!r}") from None
