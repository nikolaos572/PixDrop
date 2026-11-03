"""Statistics: the sums and averages, months in the server's time zone, the API's per-user statistics,
and the administrator's command `python -m pixdrop stats`."""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from helpers import add_job, generate, png, run_cli, submit

from pixdrop.__main__ import show_stats
from pixdrop.stats import statistics
from pixdrop.store import Job, Picture, Status, Usage

ATHENS = ZoneInfo("Europe/Athens")
OCT_31_LATE = datetime(2026, 10, 31, 22, 30, tzinfo=UTC).timestamp()  # already November in Athens (UTC+2)
NOV_2 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC).timestamp()
SEP_1 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC).timestamp()
PICTURE = Picture(1, "x.png", "image/png", 10, 8, 8)


def job(created_at, status=Status.COMPLETED, model="gpt-image", usage=None, duration=None, images=0, references=0, source="api"):
    return Job(
        id="1793456280412_a9f3", user="alice", status=status, created_at=created_at, prompt="x", model=model,
        provider="openrouter", upstream_model="u", api_type="images", source=source, usage=usage,
        duration_seconds=duration, images=(PICTURE,) * images, references=(PICTURE,) * references,
    )  # fmt: skip


JOBS = [
    job(NOV_2, usage=Usage(12, 1290, 1302, 0.04), duration=10.0, images=1, references=2, source="web"),
    job(OCT_31_LATE, model="nano-banana", usage=Usage(300, 2000, 2300, 0.08), duration=20.5, images=2, source="mcp"),
    job(SEP_1, status=Status.FAILED, usage=Usage(None, None, None, None)),
    job(SEP_1 + 60, status=Status.FAILED, model="nano-banana"),
    job(NOV_2 + 60, status=Status.PENDING, references=1),
    job(NOV_2 + 120, status=Status.GENERATING, usage=Usage(5, None, None, 0.01)),
]


def test_the_totals():
    totals = statistics("alice", JOBS, ATHENS).totals
    assert totals.model_dump() == {
        "jobs": 6, "completed": 2, "failed": 2, "in_progress": 2, "images": 3, "references": 3,
        "cost_usd": 0.13, "average_cost_usd": round(0.13 / 3, 6),  # per job with a reported cost
        "prompt_tokens": 317, "completion_tokens": 3290, "total_tokens": 3602,
        "average_tokens": 1801.0,  # per job with reported tokens
        "average_duration_seconds": 15.25,  # of completed jobs
    }  # fmt: skip


def test_by_model_the_most_used_first():
    models = statistics("alice", JOBS, ATHENS).models
    assert [(line.model, line.jobs, line.completed, line.failed, line.in_progress) for line in models] == [
        ("gpt-image", 4, 1, 1, 2),
        ("nano-banana", 2, 1, 1, 0),
    ]
    assert (models[1].cost_usd, models[1].average_duration_seconds) == (0.08, 20.5)


def test_equally_used_models_are_in_alphabetical_order():
    models = statistics("alice", [job(NOV_2, model="zeta"), job(NOV_2, model="alpha")], ATHENS).models
    assert [line.model for line in models] == ["alpha", "zeta"]


def test_months_are_those_of_the_servers_time_zone():
    athens = statistics("alice", JOBS, ATHENS)
    assert [(line.month, line.jobs) for line in athens.months] == [("2026-11", 4), ("2026-09", 2)]  # the newest first
    utc = statistics("alice", JOBS, ZoneInfo("UTC"))
    assert [(line.month, line.jobs) for line in utc.months] == [("2026-11", 3), ("2026-10", 1), ("2026-09", 2)]
    assert athens.months[0].cost_usd == 0.13


def test_first_and_last_job_and_sources():
    stats = statistics("alice", JOBS, ATHENS)
    assert (stats.first_job_at, stats.last_job_at) == ("2026-09-01T11:00:00+03:00", "2026-11-02T14:02:00+02:00")
    assert stats.sources == {"web": 1, "mcp": 1, "api": 4}
    assert stats.user == "alice"


def test_without_jobs():
    stats = statistics("bob", [], ATHENS)
    assert stats.totals.jobs == 0
    assert (stats.totals.average_cost_usd, stats.totals.average_tokens, stats.totals.average_duration_seconds) == (None, None, None)
    assert (stats.first_job_at, stats.last_job_at, stats.models, stats.months, stats.sources) == (None, None, [], [], {})


def test_cost_is_rounded_to_a_millionth_of_a_dollar():
    jobs = [job(NOV_2, usage=Usage(cost=0.1)) for _ in range(3)]  # 0.30000000000000004 in floating point
    assert statistics("alice", jobs, ATHENS).totals.cost_usd == 0.3


# --- the API ---


def test_each_user_gets_their_own_statistics(client, bob):
    generate(client)
    generate(client, "FAIL")
    generate(client, model="nano-banana", references=[("a.png", png())])
    submit(client, reference_count=1)
    stats = client.get("/api/stats").json()
    assert stats["user"] == "alice"
    totals = stats["totals"]
    assert (totals["jobs"], totals["completed"], totals["failed"], totals["in_progress"]) == (4, 2, 1, 1)
    assert (totals["images"], totals["references"], totals["cost_usd"]) == (2, 1, 0.08)
    assert [line["model"] for line in stats["models"]] == ["gpt-image", "nano-banana"]
    assert stats["sources"] == {"api": 4}
    assert bob.get("/api/stats").json()["totals"]["jobs"] == 0


# --- python -m pixdrop stats ---


@pytest.fixture
def history(data_dir):
    """Jobs of two users, as records in the data directory."""
    add_job(data_dir, "alice", NOV_2, usage=Usage(12, 1290, 1302, 0.04), duration_seconds=10.0, images=(PICTURE,))
    add_job(data_dir, "alice", NOV_2 + 60, status=Status.FAILED, error="boom")
    add_job(data_dir, "bob", OCT_31_LATE, model="nano-banana", usage=Usage(300, 2000, 2300, 0.08), duration_seconds=20.0, images=(PICTURE, PICTURE), source="mcp")
    return data_dir


@pytest.fixture
def cli(monkeypatch, capsys):
    """show_stats(user, as_json) with DATA_DIR and TZ set: (exit status, standard output, standard error)."""

    def run(data_dir, user=None, as_json=False, tz="Europe/Athens"):
        monkeypatch.setenv("DATA_DIR", str(data_dir))
        monkeypatch.setenv("TZ", tz)
        status = show_stats(user, as_json)
        out, err = capsys.readouterr()
        return status, out, err

    return run


def test_the_table_of_every_user(cli, history):
    status, out, _ = cli(history)
    assert status == 0
    lines = out.splitlines()
    assert lines[0].split() == [
        "User", "Jobs", "Completed", "Failed", "Active", "Images", "Cost", "(USD)", "Avg", "cost", "Tokens", "Avg", "tokens",
        "Avg", "time", "(s)",
    ]  # fmt: skip
    assert set(lines[1]) == {"-", " "} and lines[4] == lines[1]
    assert lines[2].split() == ["alice", "2", "1", "1", "0", "1", "0.0400", "0.0400", "1302", "1302", "10.0"]
    assert lines[3].split() == ["bob", "1", "1", "0", "0", "2", "0.0800", "0.0800", "2300", "2300", "20.0"]
    assert lines[5].split() == ["ALL", "USERS", "3", "2", "1", "0", "3", "0.1200", "0.0600", "3602", "1801", "15.0"]
    assert len({len(line) for line in lines}) == 1  # aligned columns


def test_the_details_of_one_user(cli, history):
    status, out, _ = cli(history, user="alice")
    assert status == 0
    assert out.splitlines()[:6] == [
        "User alice: 2 job(s), 1 completed, 1 failed, 0 in progress",
        "Images 1, reference images 0",
        "Cost $0.0400 ($0.0400 per job)",
        "Tokens 1302 (12 prompt, 1290 completion), 1302 per job",
        "First job 2026-11-02T14:00:00+02:00, last job 2026-11-02T14:01:00+02:00",
        "Sources api 2",
    ]
    assert "By model:\n  gpt-image: 2 job(s), 1 failed, $0.0400" in out
    assert "By month:\n  2026-11: 2 job(s), 1 failed, $0.0400" in out


def test_json_for_scripts(cli, history):
    status, out, _ = cli(history, as_json=True)
    report = json.loads(out)
    assert status == 0
    assert [line["user"] for line in report] == ["alice", "bob"]
    assert report[1]["months"][0]["month"] == "2026-11"  # in TZ
    assert json.loads(cli(history, as_json=True, tz="UTC")[1])[1]["months"][0]["month"] == "2026-10"
    one = json.loads(cli(history, user="bob", as_json=True)[1])
    assert [line["user"] for line in one] == ["bob"]


def test_an_unknown_user(cli, history):
    status, out, err = cli(history, user="carol")
    assert (status, out) == (1, "")
    assert err == f"error: no jobs of 'carol' in {history / 'outputs'}\n"


def test_an_empty_data_directory(cli, tmp_path):
    assert cli(tmp_path) == (0, f"no jobs yet in {tmp_path / 'outputs'}\n", "")
    assert cli(tmp_path, as_json=True) == (0, "[]\n", "")


def test_invalid_settings(cli, tmp_path):
    status, _, err = cli(tmp_path, tz="Mars/Olympus")
    assert status == 1 and err.startswith("error: TZ must be an IANA time zone")


def test_the_command(history):
    result = run_cli(history, "stats", "--json", TZ="UTC")
    assert result.returncode == 0, result.stderr
    assert [line["user"] for line in json.loads(result.stdout)] == ["alice", "bob"]
    assert json.loads(result.stdout)[1]["months"][0]["month"] == "2026-10"  # in TZ, here UTC
    unknown = run_cli(history, "stats", "--user", "carol")
    assert (unknown.returncode, unknown.stderr) == (1, f"error: no jobs of 'carol' in {history / 'outputs'}\n")
