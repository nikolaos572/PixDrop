"""The web page's sessions: started by logging in, ended by logging out, by time, or by a change
of the user's token, and kept in a file that holds nothing that logs in."""

import json
import logging
import stat

import pytest
from helpers import TOKENS, write_json

from pixdrop import sessions as sessions_module
from pixdrop.sessions import Sessions
from pixdrop.users import UserRegistry, digest

DAY = 86_400


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def users_file(tmp_path):
    return write_json(tmp_path / "users.json", {"users": TOKENS})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(tmp_path, users_file, clock):
    def make() -> Sessions:
        return Sessions(tmp_path / "sessions.json", UserRegistry(users_file), 30 * DAY, clock)

    return make


def test_a_session_has_its_user_until_it_ends(make):
    sessions = make()
    session_id = sessions.start("alice", TOKENS["alice"])
    assert sessions.user(session_id) == "alice"
    assert sessions.user(session_id + "x") is None
    assert sessions.end(session_id) == "alice"
    assert sessions.user(session_id) is None
    assert sessions.end(session_id) is None  # once only


def test_sessions_expire(make, clock):
    sessions = make()
    session_id = sessions.start("alice", TOKENS["alice"])
    clock.now += 30 * DAY - 1
    assert sessions.user(session_id) == "alice"
    clock.now += 1
    assert sessions.user(session_id) is None


def test_the_file_holds_only_digests(make, tmp_path):
    sessions = make()
    session_id = sessions.start("alice", TOKENS["alice"])
    path = tmp_path / "sessions.json"
    content = path.read_text()
    assert session_id not in content and TOKENS["alice"] not in content
    assert json.loads(content) == {
        "sessions": {digest(session_id): {"user": "alice", "token": digest(TOKENS["alice"]), "expires_at": 1_800_000_000.0 + 30 * DAY}}
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sessions_are_read_back(make):
    session_id = make().start("bob", TOKENS["bob"])
    assert make().user(session_id) == "bob"


def test_a_session_is_bound_to_the_token_it_started_with(make, users_file):
    sessions = make()
    session_id = sessions.start("alice", TOKENS["alice"])
    write_json(users_file, {"users": {"alice": "alice-new-token-0123456789", "bob": TOKENS["bob"]}})
    assert sessions.user(session_id) is None
    write_json(users_file, {"users": TOKENS})
    assert sessions.user(session_id) == "alice"


def test_writing_drops_the_sessions_that_ended(make, users_file, clock, tmp_path):
    sessions = make()
    old = sessions.start("alice", TOKENS["alice"])
    clock.now += 31 * DAY
    write_json(users_file, {"users": {"alice": TOKENS["alice"]}})  # bob removed
    sessions.start("bob", TOKENS["bob"])
    current = sessions.start("alice", TOKENS["alice"])
    assert list(json.loads((tmp_path / "sessions.json").read_text())["sessions"]) == [digest(current)]
    assert sessions.user(old) is None


def test_each_user_keeps_the_newest_sessions(make, clock, monkeypatch):
    monkeypatch.setattr(sessions_module, "MAX_PER_USER", 2)
    sessions = make()
    started = []
    for _ in range(3):
        started.append(sessions.start("alice", TOKENS["alice"]))
        clock.now += 1
    bob = sessions.start("bob", TOKENS["bob"])
    assert [sessions.user(session_id) for session_id in started] == [None, "alice", "alice"]
    assert sessions.user(bob) == "bob"


@pytest.mark.parametrize(
    "content",
    ["{broken", "[]", '{"sessions": []}', '{"sessions": {"x": {"user": "alice"}}}', '{"sessions": {"x": {"user": "alice", "token": "ü", "expires_at": 1}}}'],
)
def test_a_broken_file_logs_everyone_out(make, tmp_path, caplog, content):
    (tmp_path / "sessions.json").write_text(content)
    sessions = make()
    assert "ignoring the invalid sessions file" in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR
    session_id = sessions.start("alice", TOKENS["alice"])  # and it works again
    assert make().user(session_id) == "alice"
