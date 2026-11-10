"""The users file: its format (TeXdrop's), token checks, and reloading it while the server runs."""

import json
import logging

import pytest
from helpers import touch, write_json

from pixdrop.errors import ConfigError
from pixdrop.users import UserRegistry, parse_users

GOOD_TOKEN = "0123456789abcdef"


def test_parse_valid_users():
    tokens = parse_users({"users": {"alice": GOOD_TOKEN, "bob_2": "x" * 256, "c-3": "A._~+/=-" * 2}})
    assert tokens == {GOOD_TOKEN: "alice", "x" * 256: "bob_2", "A._~+/=-" * 2: "c-3"}


def test_the_same_file_serves_texdrop():
    # TeXdrop's users.json is {"users": {...}}: PixDrop reads the same document.
    assert parse_users({"users": {"alice": GOOD_TOKEN}}) == {GOOD_TOKEN: "alice"}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "expected an object"),
        ({"users": []}, "expected an object"),
        ({}, "expected an object"),
        ({"users": {"Alice": GOOD_TOKEN}}, "invalid user name 'Alice'"),  # upper case
        ({"users": {"../etc": GOOD_TOKEN}}, "invalid user name"),
        ({"users": {"": GOOD_TOKEN}}, "invalid user name"),
        ({"users": {"-alice": GOOD_TOKEN}}, "invalid user name"),
        ({"users": {"a" * 33: GOOD_TOKEN}}, "invalid user name"),
        ({"users": {"alice": "short"}}, "invalid token for 'alice'"),
        ({"users": {"alice": "x" * 257}}, "invalid token for 'alice'"),
        ({"users": {"alice": 12345678901234567890}}, "invalid token for 'alice'"),
        ({"users": {"alice": GOOD_TOKEN + " space"}}, "invalid token"),
        ({"users": {"alice": GOOD_TOKEN + ";"}}, "invalid token"),
        ({"users": {"alice": GOOD_TOKEN + "é"}}, "invalid token"),
        ({"users": {"alice": GOOD_TOKEN, "bob": GOOD_TOKEN}}, "users 'alice' and 'bob' share a token"),
    ],
)
def test_invalid_documents_are_refused(document, message):
    with pytest.raises(ConfigError, match=message):
        parse_users(document)


@pytest.fixture
def users_file(tmp_path):
    return write_json(tmp_path / "users.json", {"users": {"alice": GOOD_TOKEN, "bob": "b" * 20}})


def test_authenticate(users_file):
    registry = UserRegistry(users_file)
    assert registry.authenticate(GOOD_TOKEN) == "alice"
    assert registry.authenticate("b" * 20) == "bob"
    assert registry.authenticate(GOOD_TOKEN[:-1]) is None
    assert registry.authenticate(GOOD_TOKEN + "0") is None
    assert registry.authenticate("") is None
    assert registry.authenticate("ünïcode-token-value") is None
    assert registry.names() == ["alice", "bob"]


def test_a_missing_file_means_no_users(tmp_path, caplog):
    registry = UserRegistry(tmp_path / "missing.json")
    assert registry.authenticate(GOOD_TOKEN) is None
    assert registry.names() == []
    assert "users file" in caplog.text and "not found" in caplog.text


@pytest.mark.parametrize("content", ["{not json", '{"users": {"Bad Name": "0123456789abcdef"}}', "[]"])
def test_an_invalid_file_at_startup_is_an_error(tmp_path, content):
    path = tmp_path / "users.json"
    path.write_text(content)
    with pytest.raises(ConfigError, match=str(path)):
        UserRegistry(path)


def test_changes_are_picked_up_without_a_restart(users_file):
    registry = UserRegistry(users_file)
    write_json(users_file, {"users": {"carol": "c" * 20}})
    assert registry.authenticate(GOOD_TOKEN) is None
    assert registry.authenticate("c" * 20) == "carol"
    assert (registry.exists("alice"), registry.exists("carol")) == (False, True)

    users_file.unlink()  # every request is refused then
    assert registry.authenticate("c" * 20) is None
    write_json(users_file, {"users": {"alice": GOOD_TOKEN}})
    assert registry.authenticate(GOOD_TOKEN) == "alice"


@pytest.mark.parametrize("content", ["{broken", json.dumps({"users": {"alice": "short"}})])
def test_a_broken_edit_keeps_the_previous_users(users_file, content, caplog):
    registry = UserRegistry(users_file)
    users_file.write_text(content)
    touch(users_file)
    with caplog.at_level(logging.ERROR):
        assert registry.authenticate(GOOD_TOKEN) == "alice"
    assert f"ignoring invalid users file {users_file} (keeping the previous one)" in caplog.text
    write_json(users_file, {"users": {"alice": GOOD_TOKEN, "dave": "d" * 20}})  # fixed: read again
    assert registry.authenticate("d" * 20) == "dave"
