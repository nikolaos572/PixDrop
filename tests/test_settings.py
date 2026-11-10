"""Settings: the environment variables, their defaults, and the values refused at startup."""

from pathlib import Path

import pytest

from pixdrop.settings import Settings, SettingsError


def test_defaults():
    settings = Settings.from_env({})
    assert settings.data_dir == Path("/data")
    assert (settings.config_file, settings.users_file) == (Path("/data/config.json"), Path("/data/users.json"))
    assert settings.timezone.key == "UTC"
    assert settings.public_base_url == ""  # the request's base URL
    assert settings.generation_timeout_seconds == 180
    assert settings.max_upload_bytes == 20 * 1024**2
    assert settings.max_active_jobs == 4
    assert settings.medium_image_px == 512
    assert (settings.enable_mcp, settings.link_expiry_seconds) == (False, 3600)  # off by default, as in TeXdrop
    assert settings.forwarded_allow_ips == "*"  # any peer may be the reverse proxy, which works behind any
    assert (settings.host, settings.port) == ("0.0.0.0", 8000)


def test_values_from_the_environment():
    settings = Settings.from_env(
        {
            "DATA_DIR": "/srv/pixdrop",
            "CONFIG_FILE": "/etc/pixdrop/models.json",
            "USERS_FILE": "/etc/pixdrop/users.json",
            "PUBLIC_BASE_URL": "https://pixdrop.example.com/",
            "TZ": "Europe/Athens",
            "GENERATION_TIMEOUT_SECONDS": "300",
            "MAX_UPLOAD_MB": " 5 ",
            "MAX_ACTIVE_JOBS": "10",
            "MEDIUM_IMAGE_PX": "768",
            "ENABLE_MCP": "1",
            "LINK_EXPIRY_MINUTES": "15",
            "FORWARDED_ALLOW_IPS": "10.0.0.1",
            "HOST": "127.0.0.1",
            "PORT": "8200",
        }
    )
    assert settings.data_dir == Path("/srv/pixdrop")
    assert settings.config_file == Path("/etc/pixdrop/models.json")
    assert settings.users_file == Path("/etc/pixdrop/users.json")
    assert settings.public_base_url == "https://pixdrop.example.com"  # without its trailing slash
    assert settings.timezone.key == "Europe/Athens"
    assert settings.generation_timeout_seconds == 300
    assert settings.max_upload_bytes == 5 * 1024**2
    assert settings.max_active_jobs == 10
    assert settings.medium_image_px == 768
    assert (settings.enable_mcp, settings.link_expiry_seconds) == (True, 15 * 60)
    assert settings.forwarded_allow_ips == "10.0.0.1"
    assert (settings.host, settings.port) == ("127.0.0.1", 8200)


def test_the_files_follow_the_data_directory():
    settings = Settings.from_env({"DATA_DIR": "/srv/pixdrop"})
    assert settings.config_file == Path("/srv/pixdrop/config.json")
    assert settings.users_file == Path("/srv/pixdrop/users.json")


def test_empty_values_mean_the_default():
    settings = Settings.from_env({"DATA_DIR": "", "TZ": " ", "MAX_ACTIVE_JOBS": "", "ENABLE_MCP": "", "PUBLIC_BASE_URL": ""})
    assert (settings.data_dir, settings.timezone.key, settings.max_active_jobs) == (Path("/data"), "UTC", 4)
    assert (settings.enable_mcp, settings.public_base_url) == (False, "")


def test_relative_paths_are_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env({"DATA_DIR": "data", "CONFIG_FILE": "conf/models.json"})
    assert settings.data_dir == tmp_path / "data"
    assert settings.config_file == tmp_path / "conf" / "models.json"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://pixdrop.example.com", "https://pixdrop.example.com"),
        ("https://pixdrop.example.com/", "https://pixdrop.example.com"),
        (" http://10.0.0.5:8000 ", "http://10.0.0.5:8000"),
        ("http://[2001:db8::1]:8000/", "http://[2001:db8::1]:8000"),
    ],
)
def test_public_base_url_is_a_scheme_a_host_and_maybe_a_port(url, expected):
    assert Settings.from_env({"PUBLIC_BASE_URL": url}).public_base_url == expected


@pytest.mark.parametrize(("value", "expected"), [("0", False), ("no", False), ("False", False), ("1", True), ("YES", True)])
def test_enable_mcp_flag(value, expected):
    assert Settings.from_env({"ENABLE_MCP": value}).enable_mcp is expected


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("GENERATION_TIMEOUT_SECONDS", "9", "at least 10"),
        ("GENERATION_TIMEOUT_SECONDS", "1.5", "must be an integer"),
        ("MAX_UPLOAD_MB", "0", "at least 1"),
        ("MAX_UPLOAD_MB", "1025", "at most 1024"),
        ("MAX_UPLOAD_MB", "lots", "must be an integer"),
        ("MAX_ACTIVE_JOBS", "0", "at least 1"),
        ("MEDIUM_IMAGE_PX", "63", "at least 64"),
        ("MEDIUM_IMAGE_PX", "4097", "at most 4096"),
        ("LINK_EXPIRY_MINUTES", "0", "at least 1"),
        ("LINK_EXPIRY_MINUTES", "1h", "must be an integer"),
        ("PORT", "0", "at least 1"),
        ("PORT", "65536", "at most 65535"),
        ("ENABLE_MCP", "on", "must be 1 or 0"),
        ("TZ", "Mars/Olympus", "IANA time zone"),
        ("TZ", "../../etc/passwd", "IANA time zone"),
        ("PUBLIC_BASE_URL", "pixdrop.example.com", "PUBLIC_BASE_URL must look like"),
        ("PUBLIC_BASE_URL", "ftp://pixdrop.example.com", "PUBLIC_BASE_URL must look like"),
        ("PUBLIC_BASE_URL", "https://", "PUBLIC_BASE_URL must look like"),
        ("PUBLIC_BASE_URL", "https://example.com/?a=1", "PUBLIC_BASE_URL must look like"),
        ("PUBLIC_BASE_URL", "https://example.com/#top", "PUBLIC_BASE_URL must look like"),
        # The service is served at the root of its address: the web page uses paths such as /api/jobs.
        ("PUBLIC_BASE_URL", "https://example.com/pixdrop", "PUBLIC_BASE_URL must look like"),
        ("PUBLIC_BASE_URL", "https://example.com/a/b/", "PUBLIC_BASE_URL must look like"),
        ("PUBLIC_BASE_URL", "https://pixdrop.example.com:99999", "PUBLIC_BASE_URL must look like"),
        ("PUBLIC_BASE_URL", "https://user@pixdrop.example.com", "PUBLIC_BASE_URL must look like"),
        # uvicorn would ignore these without a word: a network with host bits, a name, a list with *.
        ("FORWARDED_ALLOW_IPS", "172.17.0.1/16", "FORWARDED_ALLOW_IPS must be IP addresses or networks"),
        ("FORWARDED_ALLOW_IPS", "proxy.example.com", "got 'proxy.example.com'"),
        ("FORWARDED_ALLOW_IPS", "10.0.0.1,*", "or \\* alone, got '\\*'"),
        ("FORWARDED_ALLOW_IPS", "10.0.0.1,,10.0.0.2", "got ''"),
    ],
)
def test_invalid_values_are_refused(name, value, message):
    with pytest.raises(SettingsError, match=message):
        Settings.from_env({name: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", "*"), ("*", "*"), (" 172.17.0.1 ", "172.17.0.1"), ("172.17.0.1, 172.18.0.0/16,::1", "172.17.0.1,172.18.0.0/16,::1")],
)
def test_the_trusted_proxies(value, expected):
    assert Settings.from_env({"FORWARDED_ALLOW_IPS": value}).forwarded_allow_ips == expected


def test_the_data_directory_is_known_even_when_other_settings_are_invalid():
    environment = {"DATA_DIR": "/srv/pixdrop", "PORT": "nope"}
    assert Settings.data_dir_from_env(environment) == Path("/srv/pixdrop")  # where the log goes
    with pytest.raises(SettingsError):
        Settings.from_env(environment)
