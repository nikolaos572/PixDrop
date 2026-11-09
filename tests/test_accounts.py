"""The commands that change the users file (python -m pixdrop adduser and reset-token): what they
write, what they keep, what they refuse, and that a running server follows them."""

import errno
import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from helpers import TOKENS, run_cli, write_json

from pixdrop.accounts import add_user, reset_token
from pixdrop.errors import ConfigError
from pixdrop.users import parse_users

OTHER_TOKEN = "0123456789abcdef"


def users_in(path):
    return json.loads(path.read_text(encoding="utf-8"))["users"]


def mode_of(path):
    return stat.S_IMODE(path.stat().st_mode)


def files_in(directory):
    """The names in a directory, where a temporary file left behind would show."""
    return sorted(path.name for path in directory.iterdir())


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def refuse_replacing(monkeypatch, error):
    def refuse(*_):
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(os, "replace", refuse)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "users.json"


@pytest.fixture
def alice_file(path):
    return write_json(path, {"users": {"alice": OTHER_TOKEN}})


# --- what they write ---


def test_the_first_user_creates_a_private_file(path):
    token = add_user(path, "nikos")
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert parse_users(json.loads(path.read_text())) == {token: "nikos"}
    assert mode_of(path) == 0o600
    assert files_in(path.parent) == ["users.json"]


def test_adding_a_user_keeps_the_rest_of_the_file(path):
    write_json(path, {"título": {"límite": 1}, "users": {"alice": OTHER_TOKEN}})
    path.chmod(0o640)
    token = add_user(path, "nikos")
    assert json.loads(path.read_text(encoding="utf-8")) == {"título": {"límite": 1}, "users": {"alice": OTHER_TOKEN, "nikos": token}}
    assert "título" in path.read_text(encoding="utf-8")  # not escaped: as the administrator wrote it
    assert mode_of(path) == 0o640
    assert files_in(path.parent) == ["users.json"]


def test_a_new_token_changes_only_that_users(path):
    write_json(path, {"users": {"alice": OTHER_TOKEN, "bob": "b" * 20}})
    path.chmod(0o640)
    first = reset_token(path, "alice")
    assert first != OTHER_TOKEN
    assert users_in(path) == {"alice": first, "bob": "b" * 20}
    second = reset_token(path, "alice")
    assert second != first
    assert users_in(path) == {"alice": second, "bob": "b" * 20}
    assert mode_of(path) == 0o640
    assert files_in(path.parent) == ["users.json"]


def test_a_link_stays_a_link(tmp_path):
    shared = tmp_path / "texdrop" / "config.json"
    shared.parent.mkdir()
    write_json(shared, {"users": {"alice": OTHER_TOKEN}})
    link = tmp_path / "users.json"
    link.symlink_to(shared)
    token = add_user(link, "nikos")
    assert link.is_symlink()
    assert users_in(shared) == {"alice": OTHER_TOKEN, "nikos": token}
    assert files_in(tmp_path) == ["texdrop", "users.json"]


def test_commands_at_the_same_time_lose_nothing(path):
    names = [f"user-{number}" for number in range(24)]
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        tokens = dict(zip(names, pool.map(lambda name: add_user(path, name), names), strict=True))
    assert users_in(path) == tokens


# --- what they refuse ---


@pytest.mark.parametrize(
    ("command", "name", "message"),
    [
        (add_user, "alice", "user 'alice' already exists: reset-token gives them a new token"),
        (reset_token, "carol", "no user 'carol' in "),
        (add_user, "Alice", "invalid user name 'Alice'"),
        (add_user, "../etc", "invalid user name"),
        (add_user, "", "invalid user name"),
        (add_user, "-alice", "invalid user name"),
        (add_user, "a" * 33, "invalid user name"),
    ],
)
def test_a_refused_command_changes_nothing(alice_file, command, name, message):
    before = alice_file.read_bytes(), alice_file.stat().st_mtime_ns
    with pytest.raises(ConfigError, match=re.escape(message)):
        command(alice_file, name)
    assert (alice_file.read_bytes(), alice_file.stat().st_mtime_ns) == before
    assert files_in(alice_file.parent) == ["users.json"]


@pytest.mark.parametrize(("command", "name"), [(add_user, "../etc"), (reset_token, "alice")])
def test_a_refused_command_creates_no_file(path, command, name):
    with pytest.raises(ConfigError):
        command(path, name)
    assert files_in(path.parent) == []


@pytest.mark.parametrize("command", [add_user, reset_token])
@pytest.mark.parametrize("content", [b"", b"{broken", b"[]", b"{}", b'{"users": {"alice": "short"}}', b"\xff"])
def test_a_broken_file_is_not_written_over(path, command, content):
    path.write_bytes(content)
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        command(path, "alice")
    assert path.read_bytes() == content
    assert files_in(path.parent) == ["users.json"]


# --- files that cannot simply be replaced ---


def test_a_file_mounted_by_itself_is_written_over(path, monkeypatch):
    write_json(path, {"título": 1, "users": {"alice": "a" * 200, "bob": OTHER_TOKEN}})  # longer than what replaces it
    inode = path.stat().st_ino
    refuse_replacing(monkeypatch, errno.EBUSY)  # what renaming over a mount point gives
    token = reset_token(path, "alice")
    assert path.stat().st_ino == inode
    assert json.loads(path.read_text(encoding="utf-8")) == {"título": 1, "users": {"alice": token, "bob": OTHER_TOKEN}}
    assert files_in(path.parent) == ["users.json"]


def test_a_file_that_cannot_be_replaced_stays_as_it_is(alice_file, monkeypatch):
    before = alice_file.read_bytes()
    refuse_replacing(monkeypatch, errno.EACCES)
    with pytest.raises(PermissionError):
        add_user(alice_file, "nikos")
    assert alice_file.read_bytes() == before
    assert files_in(alice_file.parent) == ["users.json"]


def test_an_owner_that_cannot_be_kept_does_not_stop_the_command(path, monkeypatch):
    def refuse(*_):
        raise PermissionError(errno.EPERM, "only root can do this")

    monkeypatch.setattr(os, "fchown", refuse)
    token = add_user(path, "nikos")
    assert users_in(path) == {"nikos": token}


# --- the commands ---


def test_adduser_prints_the_token_alone_on_standard_output(data_dir):
    result = run_cli(data_dir, "adduser", "nikos")
    assert result.returncode == 0, result.stderr
    assert result.stderr == "Added user nikos. Their token:\n"
    assert result.stdout == users_in(data_dir / "users.json")["nikos"] + "\n"


def test_reset_token_prints_the_token_alone_on_standard_output(data_dir):
    result = run_cli(data_dir, "reset-token", "alice")
    assert result.returncode == 0, result.stderr
    assert result.stderr == "New token of user alice (the old one no longer works):\n"
    assert result.stdout == users_in(data_dir / "users.json")["alice"] + "\n"
    assert result.stdout.strip() != TOKENS["alice"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("adduser", "alice"), "error: user 'alice' already exists"),
        (("adduser", "Nikos"), "error: invalid user name 'Nikos'"),
        (("reset-token", "carol"), "error: no user 'carol'"),
    ],
)
def test_the_commands_explain_what_they_refuse(data_dir, arguments, message):
    before = (data_dir / "users.json").read_bytes()
    result = run_cli(data_dir, *arguments)
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr.startswith(message) and "Traceback" not in result.stderr
    assert (data_dir / "users.json").read_bytes() == before


def test_the_commands_explain_a_file_they_cannot_write(data_dir, tmp_path):
    result = run_cli(data_dir, "adduser", "nikos", USERS_FILE=str(tmp_path / "missing" / "users.json"))
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr.startswith("error: ") and "Traceback" not in result.stderr


def test_the_commands_explain_invalid_settings(data_dir):
    result = run_cli(data_dir, "adduser", "nikos", PORT="not-a-number")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr.startswith("error: PORT must be an integer")


def test_the_commands_need_a_name(data_dir):
    assert run_cli(data_dir, "adduser").returncode == 2
    assert run_cli(data_dir, "reset-token").returncode == 2


def test_the_commands_follow_users_file(data_dir, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    result = run_cli(data_dir, "adduser", "nikos", USERS_FILE=str(elsewhere))
    assert result.returncode == 0, result.stderr
    assert users_in(elsewhere) == {"nikos": result.stdout.strip()}
    assert "nikos" not in users_in(data_dir / "users.json")


def test_a_running_server_follows_the_commands(client, data_dir):
    first = run_cli(data_dir, "adduser", "nikos").stdout.strip()
    assert client.get("/api/session", headers=bearer(first)).json() == {"user": "nikos"}
    assert client.get("/api/session").json() == {"user": "alice"}

    second = run_cli(data_dir, "reset-token", "nikos").stdout.strip()
    assert client.get("/api/session", headers=bearer(first)).status_code == 401
    assert client.get("/api/session", headers=bearer(second)).json() == {"user": "nikos"}
    assert client.get("/api/session").json() == {"user": "alice"}
