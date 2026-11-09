"""Run the server, set up the models, manage the users, or show every user's statistics.

    python -m pixdrop                          the server
    python -m pixdrop setup                    ask for the provider's API key and write
                                               config.json with the default models, replacing
                                               any that is there; with Docker:
                                               docker exec -it pixdrop python -m pixdrop setup
    python -m pixdrop modify-models            add and remove models, from the providers' lists
                                               (needs a terminal too)
    python -m pixdrop adduser NAME             add a user and print their new token; with
                                               Docker:
                                               docker exec pixdrop python -m pixdrop adduser NAME
    python -m pixdrop reset-token NAME         give a user a new token and print it (the old
                                               one stops working)
    python -m pixdrop stats [--user NAME] [--json]
                                               statistics of all users (or one), from the
                                               job records in DATA_DIR; with Docker:
                                               docker exec pixdrop python -m pixdrop stats
    python -m pixdrop models [SEARCH] [--json]
                                               the image models of the providers in
                                               config.json, with the options each accepts
"""

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx2
import uvicorn

from . import __version__, logs
from .accounts import add_user, reset_token
from .app import create_app
from .config import parse_config
from .configure import run_modify_models, run_setup
from .discovery import capabilities_of, read_entries, references_of, summary_of
from .errors import ConfigError
from .settings import Settings, SettingsError
from .stats import StatsOut, Totals, statistics
from .storage import Storage
from .store import JobStore

log = logging.getLogger(__name__)

# At shutdown, requests still running after this long (a client waiting with ?wait=, say)
# are cut off, so that the server stops before docker stop kills it (after 10 seconds).
SHUTDOWN_SECONDS = 5


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m pixdrop", description="PixDrop: generate images with AI models.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("setup", help="ask for the API key and write config.json with the default models, replacing any that is there")
    commands.add_parser("modify-models", help="add and remove models, from the providers' lists")
    add_parser = commands.add_parser("adduser", help="add a user and print their new token")
    add_parser.add_argument("name", help="the user's name: 1-32 of a-z, 0-9, '_' and '-', starting with a letter or a digit")
    reset_parser = commands.add_parser("reset-token", help="give a user a new token and print it (the old one stops working)")
    reset_parser.add_argument("name", help="the user's name")
    stats_parser = commands.add_parser("stats", help="show the statistics of every user (or of one)")
    stats_parser.add_argument("--user", help="only this user, in detail")
    stats_parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    models_parser = commands.add_parser("models", help="the image models of the providers, with their options")
    models_parser.add_argument("search", nargs="?", help="only the models whose id or name contains this, in detail")
    models_parser.add_argument("--json", action="store_true", help="print the providers' entries as JSON")
    arguments = parser.parse_args(argv)
    # Times (in the log and the statistics) are in TZ, as the dates the API shows, so in UTC
    # without it (as in Docker) rather than in the system's time zone.
    os.environ.setdefault("TZ", "UTC")
    time.tzset()
    if arguments.command == "setup":
        sys.exit(run_setup())
    if arguments.command == "modify-models":
        sys.exit(run_modify_models())
    if arguments.command == "adduser":
        sys.exit(print_new_token(add_user, arguments.name, f"Added user {arguments.name}. Their token:"))
    if arguments.command == "reset-token":
        sys.exit(print_new_token(reset_token, arguments.name, f"New token of user {arguments.name} (the old one no longer works):"))
    if arguments.command == "stats":
        sys.exit(show_stats(arguments.user, arguments.json))
    if arguments.command == "models":
        sys.exit(show_models(arguments.search, arguments.json))
    serve()


def serve() -> None:
    logs.log_to_console()
    logs.log_to_file(Settings.data_dir_from_env() / "logs")  # before the other settings, which may be invalid
    log.info("PixDrop %s starting, data in %s", __version__, Settings.data_dir_from_env())
    try:
        settings = Settings.from_env()
        app = create_app(settings)
    except (SettingsError, ConfigError) as exc:
        log.error("cannot start: %s", exc)
        sys.exit(1)
    except Exception:
        log.exception("cannot start")
        sys.exit(1)
    # One process, which keeps the jobs in progress in memory: a second one would not see them.
    # Logging is set up above; requests are logged by logs.access_log.
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )


def print_new_token(change: Callable[[Path, str], str], name: str, done: str) -> int:
    """Change the users file, and print the new token alone on standard output (for scripts),
    with `done` on standard error; returns the exit status."""
    try:
        token = change(Settings.from_env().users_file, name)
    except (OSError, ValueError) as exc:  # SettingsError and ConfigError are ValueErrors
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(done, file=sys.stderr)
    print(token)
    return 0


def show_stats(only: str | None, as_json: bool) -> int:
    """Print the statistics read from the job records; returns the exit status."""
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    store = JobStore(Storage(settings.data_dir))
    users = store.users()
    if only is not None:
        if only not in users:
            print(f"error: no jobs of {only!r} in {settings.data_dir / 'outputs'}", file=sys.stderr)
            return 1
        users = [only]
    elif not users and not as_json:
        print(f"no jobs yet in {settings.data_dir / 'outputs'}")
        return 0
    jobs = {user: store.all(user) for user in users}
    report = [statistics(user, jobs[user], settings.timezone) for user in users]
    if as_json:
        print(json.dumps([line.model_dump() for line in report], indent=2))
    elif only is not None:
        print(_details(report[0]))
    else:
        print(_table(report, Totals.of(job for user_jobs in jobs.values() for job in user_jobs)))
    return 0


def _table(report: list[StatsOut], everyone: Totals) -> str:
    header = ("User", "Jobs", "Completed", "Failed", "Active", "Images", "Cost (USD)", "Avg cost", "Tokens", "Avg tokens",
              "Avg time (s)")  # fmt: skip
    rows = [*(_row(line.user, line.totals) for line in report), _row("ALL USERS", everyone)]
    widths = [max(len(str(row[i])) for row in (header, *rows)) for i in range(len(header))]
    lines = ["  ".join(str(cell).rjust(width) if i else str(cell).ljust(width) for i, (cell, width) in enumerate(zip(row, widths)))
             for row in (header, *rows)]  # fmt: skip
    lines.insert(1, "  ".join("-" * width for width in widths))
    lines.insert(len(lines) - 1, lines[1])
    return "\n".join(lines)


def _row(user: str, totals: Totals) -> tuple[object, ...]:
    def optional(value: float | None, form: str) -> str:
        return "-" if value is None else format(value, form)

    return (user, totals.jobs, totals.completed, totals.failed, totals.in_progress, totals.images,
            f"{totals.cost_usd:.4f}", optional(totals.average_cost_usd, ".4f"), totals.total_tokens,
            optional(totals.average_tokens, ".0f"), optional(totals.average_duration_seconds, ".1f"))  # fmt: skip


def show_models(search: str | None, as_json: bool) -> int:
    """Print the image models of the providers of config.json, as their catalogues list them
    (the same catalogues the server reads for the models configured without "options")."""
    try:
        settings = Settings.from_env()
        catalog = parse_config(json.loads(settings.config_file.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:  # SettingsError, ConfigError and JSON errors are ValueErrors
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not catalog.providers:
        print(f"error: {settings.config_file} has no providers", file=sys.stderr)
        return 1
    configured: dict[tuple[str, str], list[str]] = {}
    for name, model in catalog.models.items():
        configured.setdefault((model.provider.name, model.upstream), []).append(name)
    status, found = 0, []
    for provider in catalog.providers.values():
        try:
            entries = read_entries(provider)
        except (httpx2.HTTPError, ValueError) as exc:
            print(f"error: cannot read the model catalogue of {provider.name}: {exc}", file=sys.stderr)
            status = 1
            continue
        matches = [entry for entry in entries if not search or search.lower() in f"{entry['id']} {entry.get('name', '')}".lower()]
        found += [{"provider": provider.name, **entry} for entry in matches]
        if not as_json:
            print(f"{provider.name}: {len(entries)} image models" + (f", {len(matches)} matching {search!r}" if search else ""))
            for entry in matches:
                print(_model_entry(entry, configured.get((provider.name, entry["id"]), []), provider.name, detailed=bool(search)))
            print()
    if as_json:
        print(json.dumps(found, indent=2))
    elif not search:
        print("python -m pixdrop models <text> shows the models matching <text> in detail, with a configuration to copy.")
    return status


def _model_entry(entry: dict, configured_as: list[str], provider: str, *, detailed: bool) -> str:
    """One model of a catalogue: a line, or (detailed) its options and a configuration to copy."""
    mark = f"  [configured as {', '.join(map(repr, configured_as))}]" if configured_as else ""
    if not detailed:
        return f"  {entry['id']}{mark}\n      {summary_of(entry)}"
    capabilities = capabilities_of(entry)
    lines = [f"  {entry['id']}{mark}", f"      {entry.get('name', '')}: {entry.get('description', '')}".rstrip(": "),
             f"      reference images: {references_of(capabilities)}"]  # fmt: skip
    for name, spec in capabilities.specs.items():
        values = " ".join(spec.values) if spec.kind == "enum" else f"{spec.minimum}-{spec.maximum}" if spec.kind == "integer" else spec.pattern
        lines.append(f"      {name}: {values}")
    if capabilities.unknown:
        lines.append(f"      also accepts (not offered by PixDrop; settable with \"request\"): {', '.join(capabilities.unknown)}")
    if not configured_as:
        snippet = json.dumps({"provider": provider, "model": entry["id"], "api_type": "images"})
        lines.append(f'      config.json: "{entry["id"].split("/")[-1]}": {snippet}')
    return "\n".join(lines)


def _details(line: StatsOut) -> str:
    totals = line.totals
    out = [
        f"User {line.user}: {totals.jobs} job(s), {totals.completed} completed, {totals.failed} failed, {totals.in_progress} in progress",
        f"Images {totals.images}, reference images {totals.references}",
        f"Cost ${totals.cost_usd:.4f}" + (f" (${totals.average_cost_usd:.4f} per job)" if totals.average_cost_usd is not None else ""),
        f"Tokens {totals.total_tokens} ({totals.prompt_tokens} prompt, {totals.completion_tokens} completion)"
        + (f", {totals.average_tokens:.0f} per job" if totals.average_tokens is not None else ""),
        f"First job {line.first_job_at or '-'}, last job {line.last_job_at or '-'}",
        "Sources " + (", ".join(f"{source} {count}" for source, count in sorted(line.sources.items())) or "-"),
        "",
        "By model:",
        *(f"  {model.model}: {model.jobs} job(s), {model.failed} failed, ${model.cost_usd:.4f}" for model in line.models),
        "",
        "By month:",
        *(f"  {month.month}: {month.jobs} job(s), {month.failed} failed, ${month.cost_usd:.4f}" for month in line.months),
    ]
    return "\n".join(out)


if __name__ == "__main__":
    main()
