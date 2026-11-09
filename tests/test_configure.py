"""The commands on config.json (python -m pixdrop setup and modify-models): what they check with the
provider, what they write, what they keep and what they refuse. The keys are really typed, into the
real prompts."""

import errno
import io
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace

import httpx2
import pytest
from fake_provider import MANAGEMENT_KEY, REJECTED_KEY
from helpers import run_cli, write_json
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pixdrop import configure, discovery
from pixdrop.config import parse_config

KEY = "sk-or-v1-new-key-0123456789"
OLD_KEY = "sk-or-v1-old-key-0123456789"
UNREACHABLE = "http://127.0.0.1:9/api/v1"
DOWN, ENTER, BACKSPACE, CTRL_C = "\x1b[B", "\r", "\x7f", "\x03"
SPACE = " "


@pytest.fixture
def defaults(tmp_path, provider_url, monkeypatch):
    """Default models that the fake provider offers, in place of the real ones."""
    document = {
        "providers": {"openrouter": {"base_url": provider_url, "api_key": "<your key>", "headers": {"X-OpenRouter-Title": "PixDrop"}}},
        "default_model": "pictures",
        "models": {
            "pictures": {"provider": "openrouter", "model": "fake/images", "api_type": "images", "defaults": {"quality": "high"}},
            "editor": {"provider": "openrouter", "model": "fake/editor", "api_type": "images"},
        },
    }
    monkeypatch.setattr(configure, "DEFAULTS", write_json(tmp_path / "defaults.json", document))
    return document


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Where the commands write, through DATA_DIR as in a real run."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    return data / "config.json"


@pytest.fixture
def configured(config_file, defaults):
    """A config.json with the defaults and a key, set up as a command does."""
    configure.set_up(config_file, OLD_KEY)
    return config_file


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def mode_of(path):
    return stat.S_IMODE(path.stat().st_mode)


def files_in(directory):
    return sorted(path.name for path in directory.iterdir())


@contextmanager
def terminal(monkeypatch, keys):
    """The prompts run as in a terminal, where these keys are typed and which then closes, so
    that a prompt waiting for more keys ends the command, as cancelled, instead of hanging."""
    monkeypatch.setattr(configure, "_in_a_terminal", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        pipe.close()
        yield


def modify(monkeypatch, keys):
    with terminal(monkeypatch, keys):
        return configure.run_modify_models()


def setup_with(monkeypatch, typed):
    monkeypatch.setattr(sys, "stdin", io.StringIO(typed))
    return configure.run_setup()


# --- setup ---


def test_setup_writes_the_defaults_with_the_key(config_file, defaults, monkeypatch, capsys):
    assert setup_with(monkeypatch, f"  {KEY}\n") == 0
    expected = json.loads(json.dumps(defaults))
    expected["providers"]["openrouter"]["api_key"] = KEY
    assert read(config_file) == expected
    assert parse_config(read(config_file)).default == "pictures"
    assert mode_of(config_file) == 0o600
    assert files_in(config_file.parent) == ["config.json"]
    out = capsys.readouterr()
    assert "with 2 models" in out.out and "pictures: fake/images" in out.out
    assert "reference images 0-4; options: aspect_ratio, quality" in out.out  # read from the catalogue
    assert "reference images 1-2; options: -" in out.out
    assert "This replaces" not in out.out  # nothing to replace
    assert KEY not in out.out + out.err


def test_setup_writes_no_options_so_that_they_stay_up_to_date(configured):
    assert all("options" not in model and "max_reference_images" not in model for model in read(configured)["models"].values())
    assert all(model.auto_options and model.auto_references for model in parse_config(read(configured)).models.values())


def test_setup_again_deletes_the_old_key_and_models(configured, monkeypatch, capsys):
    document = read(configured)
    document["providers"]["other"] = {"api_key": "another-provider-key"}
    document["models"]["mine"] = {"provider": "other", "model": "x/mine", "api_type": "images"}
    write_json(configured, document)
    configured.chmod(0o644)  # as a file copied by hand
    assert setup_with(monkeypatch, f"{KEY}\n") == 0
    assert f"This replaces {configured}: its API key and its models will be deleted" in capsys.readouterr().out
    text = configured.read_text()
    assert OLD_KEY not in text and "another-provider-key" not in text and "mine" not in text
    assert read(configured)["providers"]["openrouter"]["api_key"] == KEY
    assert list(read(configured)["models"]) == ["pictures", "editor"]
    assert mode_of(configured) == 0o600
    assert files_in(configured.parent) == ["config.json"]


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("", "no API key given"),
        ("\n", "no API key given"),
        (f"{REJECTED_KEY}\n", "openrouter refused this API key"),
        (f"{MANAGEMENT_KEY}\n", "this is a management key, which cannot generate images"),
        ("with a space\n", "the API key may hold only visible ASCII characters"),
        ("kéy\n", "the API key may hold only visible ASCII characters"),
    ],
)
def test_setup_refuses_a_bad_key_and_keeps_the_old_file(configured, monkeypatch, capsys, typed, message):
    before = configured.read_bytes(), configured.stat().st_mtime_ns
    assert setup_with(monkeypatch, typed) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and message in err
    assert typed.strip() == "" or typed.strip() not in err  # the message never shows the key
    assert (configured.read_bytes(), configured.stat().st_mtime_ns) == before
    assert files_in(configured.parent) == ["config.json"]


def test_setup_refuses_a_bad_key_and_writes_no_file(config_file, defaults, monkeypatch):
    assert setup_with(monkeypatch, f"{REJECTED_KEY}\n") == 1
    assert files_in(config_file.parent) == []


def test_setup_writes_nothing_when_a_default_model_is_gone(config_file, defaults, monkeypatch, capsys):
    defaults["models"]["retired"] = {"provider": "openrouter", "model": "old/retired-model", "api_type": "images"}
    write_json(configure.DEFAULTS, defaults)
    assert setup_with(monkeypatch, f"{KEY}\n") == 1
    err = capsys.readouterr().err
    assert "no longer offers these default models: old/retired-model" in err and "Nothing was written" in err
    assert "fake/images" not in err
    assert files_in(config_file.parent) == []


def test_setup_keeps_the_old_file_when_a_default_model_is_gone(configured, defaults, monkeypatch):
    before = configured.read_bytes()
    defaults["models"]["retired"] = {"provider": "openrouter", "model": "old/retired-model", "api_type": "images"}
    write_json(configure.DEFAULTS, defaults)
    assert setup_with(monkeypatch, f"{KEY}\n") == 1
    assert configured.read_bytes() == before


@pytest.mark.parametrize("what", ["the key check", "the catalogue"])
def test_setup_writes_nothing_when_the_provider_cannot_be_reached(config_file, defaults, monkeypatch, capsys, what):
    if what == "the catalogue":
        monkeypatch.setattr(configure, "read_entries", lambda provider: discovery.read_entries(replace(provider, base_url=UNREACHABLE)))
    else:
        defaults["providers"]["openrouter"]["base_url"] = UNREACHABLE
        write_json(configure.DEFAULTS, defaults)
    assert setup_with(monkeypatch, f"{KEY}\n") == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot") and "Traceback" not in err and KEY not in err
    assert files_in(config_file.parent) == []


def test_setup_says_when_it_cannot_check_the_key(config_file, defaults, monkeypatch, capsys):
    defaults["providers"]["openrouter"]["base_url"] += "/nowhere"  # every answer is 404
    write_json(configure.DEFAULTS, defaults)
    assert setup_with(monkeypatch, f"{KEY}\n") == 1
    assert "cannot check the API key with openrouter: HTTP 404" in capsys.readouterr().err
    assert files_in(config_file.parent) == []


def test_setup_says_when_the_answer_about_the_key_makes_no_sense(config_file, defaults, monkeypatch, capsys):
    monkeypatch.setattr(configure.httpx2, "get", lambda *_, **__: httpx2.Response(200, text="not JSON"))
    assert setup_with(monkeypatch, f"{KEY}\n") == 1
    assert "cannot check the API key with openrouter" in capsys.readouterr().err
    assert files_in(config_file.parent) == []


def test_setup_asks_for_the_key_in_a_terminal_without_showing_it(config_file, defaults, monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    asked = []
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(configure.getpass, "getpass", lambda prompt: asked.append(prompt) or f" {KEY} ")
    assert configure.run_setup() == 0
    assert asked == ["API key (not shown as you type): "]
    assert read(config_file)["providers"]["openrouter"]["api_key"] == KEY
    assert configure.KEYS_PAGE in capsys.readouterr().out


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, EOFError])
def test_setup_can_be_cancelled(configured, monkeypatch, capsys, interruption):
    before = configured.read_bytes()

    def interrupted():
        raise interruption

    monkeypatch.setattr(configure, "_ask_key", interrupted)
    assert configure.run_setup() == 1
    assert configure.CANCELLED in capsys.readouterr().err
    assert configured.read_bytes() == before


def test_setup_writes_over_a_file_mounted_by_itself(configured, defaults, monkeypatch):
    document = read(configured)
    document["models"]["long"] = {"provider": "openrouter", "model": "x" * 500, "api_type": "images"}  # longer than what replaces it
    write_json(configured, document)
    inode = configured.stat().st_ino

    def busy(*_):
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

    monkeypatch.setattr(os, "replace", busy)
    assert setup_with(monkeypatch, f"{KEY}\n") == 0
    assert configured.stat().st_ino == inode
    assert list(read(configured)["models"]) == ["pictures", "editor"]
    assert files_in(configured.parent) == ["config.json"]


def test_a_running_server_follows_setup(client, defaults, settings):
    assert client.get("/api/models").json()["default_model"] == "gpt-image"
    configure.set_up(settings.config_file, KEY)
    models = client.get("/api/models").json()
    assert models["default_model"] == "pictures"
    assert [model["name"] for model in models["models"]] == ["pictures", "editor"]


def test_the_packaged_defaults_are_the_providers():
    catalog = parse_config(json.loads(configure.DEFAULTS.read_text()))
    assert list(catalog.providers) == [configure.DEFAULT_PROVIDER]
    assert {model.provider.name for model in catalog.models.values()} == {configure.DEFAULT_PROVIDER}


def test_the_server_does_not_load_the_prompts():
    code = "import sys, pixdrop.__main__; sys.exit('questionary' in sys.modules or 'prompt_toolkit' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False, timeout=60).returncode == 0


# --- modify-models: the choices are, in order: fake/editor and fake/images (configured), then fake/chat ---


def test_ticking_a_model_adds_it(configured, monkeypatch, capsys):
    before = read(configured)
    assert modify(monkeypatch, DOWN + DOWN + SPACE + ENTER + ENTER) == 0
    after = read(configured)
    assert after["models"]["chat"] == {
        "provider": "openrouter",
        "model": "fake/chat",
        "api_type": "images",
        "description": "A fake model that draws through chat completions.",
    }
    assert {name: model for name, model in after["models"].items() if name != "chat"} == before["models"]
    assert (after["default_model"], after["providers"]) == ("pictures", before["providers"])
    assert parse_config(after).models["chat"].auto_options
    out = capsys.readouterr().out
    assert "Added chat: reference images 0-3; options: aspect_ratio, resolution" in out  # what it accepts, from the catalogue
    assert "Removed" not in out and "Default model: pictures" in out


def test_unticking_a_model_removes_it(configured, monkeypatch, capsys):
    assert modify(monkeypatch, SPACE + ENTER) == 0  # fake/editor; one model is left, so no question about the default
    assert list(read(configured)["models"]) == ["pictures"]
    assert "Removed editor" in capsys.readouterr().out


def test_unticking_the_default_model_makes_another_the_default(configured, monkeypatch):
    assert modify(monkeypatch, DOWN + SPACE + ENTER) == 0  # fake/images, the model "pictures"
    after = read(configured)
    assert (list(after["models"]), after["default_model"]) == (["editor"], "editor")


def test_the_default_model_can_be_chosen(configured, monkeypatch):
    assert modify(monkeypatch, ENTER + DOWN + ENTER) == 0
    assert read(configured)["default_model"] == "editor"


def test_choosing_the_same_changes_nothing(configured, monkeypatch, capsys):
    before = configured.read_bytes(), configured.stat().st_mtime_ns
    assert modify(monkeypatch, ENTER + ENTER) == 0
    assert "Nothing changed." in capsys.readouterr().out
    assert (configured.read_bytes(), configured.stat().st_mtime_ns) == before


def test_typing_searches_and_ticks_only_what_it_finds(configured, monkeypatch):
    # "chat" holds an "a", which means "tick everything" in a checklist without search
    assert modify(monkeypatch, SPACE + DOWN + SPACE + "chat" + SPACE + BACKSPACE * 4 + ENTER) == 0  # unticks the two, ticks fake/chat
    assert list(read(configured)["models"]) == ["chat"]


def test_a_search_that_finds_nothing_does_not_stop_the_command(configured, monkeypatch):
    before = configured.read_bytes()
    # The checklist then shows every model: space acts on the one highlighted, here twice, so as not to change it.
    assert modify(monkeypatch, "nothing-like-this" + SPACE + SPACE + DOWN + BACKSPACE * 17 + ENTER + ENTER) == 0
    assert configured.read_bytes() == before


def test_at_least_one_model_must_be_ticked(configured, monkeypatch, capsys):
    before = configured.read_bytes()
    assert modify(monkeypatch, SPACE + DOWN + SPACE + ENTER + CTRL_C) == 1  # enter is refused; still in the checklist
    assert configure.CANCELLED in capsys.readouterr().err
    assert configured.read_bytes() == before


@pytest.mark.parametrize(
    "keys",
    [CTRL_C, DOWN + DOWN + SPACE + ENTER + CTRL_C, SPACE, DOWN + DOWN + SPACE + ENTER],
    ids=["in the checklist", "at the default model", "the terminal closes in the checklist", "the terminal closes at the default model"],
)
def test_modify_models_can_be_cancelled(configured, monkeypatch, capsys, keys):
    before = configured.read_bytes()
    assert modify(monkeypatch, keys) == 1
    assert configure.CANCELLED in capsys.readouterr().err
    assert configured.read_bytes() == before


def test_modify_models_keeps_everything_else(configured, monkeypatch):
    document = read(configured)
    document["models"]["pictures"] |= {"description": "Mine", "options": ["quality"], "max_reference_images": 2, "request": {"x": 1}}
    document["default_model"] = "editor"
    write_json(configured, document)
    configured.chmod(0o640)
    assert modify(monkeypatch, DOWN + DOWN + SPACE + ENTER + ENTER) == 0
    after = read(configured)
    assert after["models"]["pictures"] == document["models"]["pictures"]
    assert after["providers"] == document["providers"]
    assert after["default_model"] == "editor"
    assert mode_of(configured) == 0o640
    assert files_in(configured.parent) == ["config.json"]


def test_a_model_no_longer_offered_is_shown_and_can_be_removed(configured, monkeypatch):
    document = read(configured)
    document["models"]["gone"] = {"provider": "openrouter", "model": "fake/a-retired-model", "api_type": "images"}
    write_json(configured, document)
    assert modify(monkeypatch, ENTER + ENTER) == 0  # it stays unless it is unticked: the administrator decides
    assert "gone" in read(configured)["models"]
    assert modify(monkeypatch, SPACE + ENTER + ENTER) == 0  # listed first: configured, and in alphabetical order
    assert "gone" not in read(configured)["models"]


def test_two_names_of_one_model_go_together(configured, monkeypatch):
    document = read(configured)
    document["models"]["pictures-too"] = dict(document["models"]["pictures"])
    write_json(configured, document)
    assert modify(monkeypatch, ENTER + ENTER) == 0
    assert list(read(configured)["models"]) == ["pictures", "editor", "pictures-too"]
    assert modify(monkeypatch, DOWN + SPACE + ENTER) == 0
    assert list(read(configured)["models"]) == ["editor"]


def test_a_new_name_avoids_the_names_taken(configured, monkeypatch):
    document = read(configured)
    document["models"]["chat"] = document["models"].pop("editor")
    write_json(configured, document)
    # The choices are fake/editor (now named "chat") and fake/images, then fake/chat, whose short name is taken.
    assert modify(monkeypatch, DOWN + DOWN + SPACE + ENTER + ENTER) == 0
    after = read(configured)
    assert after["models"]["fake/chat"]["model"] == "fake/chat"
    assert after["models"]["chat"]["model"] == "fake/editor"


def test_models_of_every_provider_are_offered(configured, monkeypatch, provider_url):
    document = read(configured)
    document["providers"]["second"] = {"base_url": provider_url, "api_key": "another-key"}
    write_json(configured, document)
    # openrouter's two, then, alphabetically, openrouter: fake/chat and second's three
    assert modify(monkeypatch, DOWN * 3 + SPACE + ENTER + ENTER) == 0
    after = read(configured)
    assert (after["models"]["chat"]["provider"], after["providers"]["second"]["api_key"]) == ("second", "another-key")


def test_changes_made_during_the_questions_are_kept(configured, monkeypatch):
    def meanwhile(offered, chosen):
        document = read(configured)
        document["providers"]["openrouter"]["api_key"] = KEY
        write_json(configured, document)
        return {("openrouter", "fake/images"), ("openrouter", "fake/editor"), ("openrouter", "fake/chat")}

    monkeypatch.setattr(configure, "_ask_models", meanwhile)
    assert modify(monkeypatch, ENTER) == 0
    after = read(configured)
    assert after["providers"]["openrouter"]["api_key"] == KEY
    assert list(after["models"]) == ["pictures", "editor", "chat"]


def test_a_default_model_renamed_during_the_questions_gives_way(configured, monkeypatch):
    def meanwhile(names, current):
        document = read(configured)
        document["models"]["renamed"] = document["models"].pop("pictures")
        document["default_model"] = "renamed"
        write_json(configured, document)
        return "pictures"

    monkeypatch.setattr(configure, "_ask_default", meanwhile)
    assert modify(monkeypatch, ENTER) == 0
    after = read(configured)
    assert (list(after["models"]), after["default_model"]) == (["editor", "renamed"], "editor")


def test_a_model_whose_id_makes_no_valid_name_is_refused(configured, monkeypatch, capsys):
    before = configured.read_bytes()
    monkeypatch.setattr(configure, "read_entries", lambda provider: [{"id": "fake/images"}, {"id": "fake/editor"}, {"id": "fake/with space"}])
    assert modify(monkeypatch, DOWN + DOWN + SPACE + ENTER + ENTER) == 1
    assert "invalid model name 'with space'" in capsys.readouterr().err
    assert configured.read_bytes() == before


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "run python -m pixdrop setup first"),
        ("{broken", "config.json: "),
        ('{"unknown": 1}', "unknown key"),
        ("{}", "has no providers"),
    ],
)
def test_modify_models_explains_a_file_it_cannot_use(config_file, monkeypatch, capsys, content, message):
    if content is not None:
        config_file.write_text(content)
    assert modify(monkeypatch, "") == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and message in err
    assert content is None or config_file.read_text() == content


def test_modify_models_writes_nothing_when_a_catalogue_cannot_be_read(config_file, monkeypatch, capsys):
    document = {"providers": {"down": {"base_url": "http://127.0.0.1:9/api/v1", "api_key": "k"}}}
    write_json(config_file, document)
    assert modify(monkeypatch, "") == 1
    assert "cannot read the model catalogue of down" in capsys.readouterr().err
    assert read(config_file) == document


# --- the commands, as the administrator runs them ---


def test_modify_models_needs_a_terminal(real_data):
    result = run_cli(real_data, "modify-models")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == "error: this command needs a terminal: run it with docker exec -it\n"


def test_setup_without_a_key_says_how_to_give_one(data_dir):
    result = subprocess.run(
        [sys.executable, "-m", "pixdrop", "setup"],
        env=os.environ | {"DATA_DIR": str(data_dir)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "no API key given: run this in a terminal, with docker exec -it, or pipe the key to it" in result.stderr


def test_the_commands_explain_invalid_settings(data_dir):
    for command in ("setup", "modify-models"):
        result = run_cli(data_dir, command, TZ="Not/AZone")
        assert result.returncode == 1 and "Traceback" not in result.stderr


def test_the_help_lists_the_commands():
    text = subprocess.run([sys.executable, "-m", "pixdrop", "--help"], capture_output=True, text=True, timeout=60, check=True).stdout
    assert re.search(r"\bsetup\b", text) and "modify-models" in text
