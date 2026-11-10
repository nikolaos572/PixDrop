"""Statistics, computed on demand from the job records.

There is no counter to keep up to date: every request for statistics reads all of a
user's records (a few hundred files, a few milliseconds) and adds them up in memory. The
web page, the API and the MCP server show a user their own statistics; the administrator
sees every user's with `python -m pixdrop stats` on the server (see __main__).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .store import Job, Status


class Totals(BaseModel):
    jobs: int = 0
    completed: int = 0
    failed: int = 0
    in_progress: int = 0  # pending or generating
    images: int = 0  # generated
    references: int = 0  # reference images uploaded
    cost_usd: float = 0.0  # as the provider reported it
    average_cost_usd: float | None = None  # per job with a reported cost
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    average_tokens: float | None = None  # per job with reported tokens
    average_duration_seconds: float | None = None  # of completed jobs

    @classmethod
    def of(cls, jobs: Iterable[Job]) -> Totals:
        totals = cls()
        costed = tokened = 0
        durations: list[float] = []
        for job in jobs:
            totals.jobs += 1
            if job.status is Status.COMPLETED:
                totals.completed += 1
                if job.duration_seconds is not None:
                    durations.append(job.duration_seconds)
            elif job.status is Status.FAILED:
                totals.failed += 1
            else:
                totals.in_progress += 1
            totals.images += len(job.images)
            totals.references += len(job.references)
            if usage := job.usage:
                if usage.cost is not None:
                    totals.cost_usd += usage.cost
                    costed += 1
                if usage.total_tokens is not None:
                    totals.total_tokens += usage.total_tokens
                    tokened += 1
                totals.prompt_tokens += usage.prompt_tokens or 0
                totals.completion_tokens += usage.completion_tokens or 0
        totals.cost_usd = round(totals.cost_usd, 6)
        totals.average_cost_usd = round(totals.cost_usd / costed, 6) if costed else None
        totals.average_tokens = round(totals.total_tokens / tokened, 1) if tokened else None
        totals.average_duration_seconds = round(sum(durations) / len(durations), 2) if durations else None
        return totals


class ModelTotals(Totals):
    model: str


class MonthTotals(Totals):
    month: str  # YYYY-MM, in the server's time zone


class StatsOut(BaseModel):
    user: str
    totals: Totals
    first_job_at: str | None  # ISO 8601, in the server's time zone
    last_job_at: str | None
    models: list[ModelTotals]  # the most used first
    months: list[MonthTotals]  # the newest first
    sources: dict[str, int]  # jobs made through the web page, the API and MCP


def statistics(user: str, jobs: list[Job], timezone: ZoneInfo) -> StatsOut:
    """The statistics of `user`, whose jobs are `jobs` (in any order)."""
    by_model: dict[str, list[Job]] = {}
    by_month: dict[str, list[Job]] = {}
    for job in jobs:
        by_model.setdefault(job.model, []).append(job)
        by_month.setdefault(_local(job.created_at, timezone).strftime("%Y-%m"), []).append(job)
    times = sorted(job.created_at for job in jobs)
    models = [ModelTotals(model=name, **Totals.of(group).model_dump()) for name, group in by_model.items()]
    return StatsOut(
        user=user,
        totals=Totals.of(jobs),
        first_job_at=_local(times[0], timezone).isoformat(timespec="seconds") if times else None,
        last_job_at=_local(times[-1], timezone).isoformat(timespec="seconds") if times else None,
        models=sorted(models, key=lambda line: (-line.jobs, line.model)),
        months=[MonthTotals(month=month, **Totals.of(by_month[month]).model_dump()) for month in sorted(by_month, reverse=True)],
        sources=dict(Counter(job.source for job in jobs)),
    )


def _local(timestamp: float, timezone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone)
