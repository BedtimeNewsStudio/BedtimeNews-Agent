from datetime import datetime
from pathlib import Path

import pytest
from src import scheduler


class _ImmediateSchedule:
    def get_next(self, _return_type):
        return datetime.now().astimezone()


def _disable_scheduler_side_effects(monkeypatch):
    monkeypatch.setattr(scheduler.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_configure_file_logging", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown_requested", False)


def test_scheduler_runs_pipeline_when_schedule_is_due(monkeypatch):
    _disable_scheduler_side_effects(monkeypatch)
    monkeypatch.setattr(
        scheduler,
        "croniter",
        lambda _expression, _start_time: _ImmediateSchedule(),
    )
    calls = []

    def run_pipeline():
        calls.append("run")
        scheduler.shutdown_requested = True

    scheduler.run_scheduler(run_pipeline)

    assert calls == ["run"]


def test_scheduler_rejects_invalid_cron_expression(monkeypatch):
    _disable_scheduler_side_effects(monkeypatch)

    def invalid_schedule(_expression, _start_time):
        raise ValueError("invalid schedule")

    monkeypatch.setattr(scheduler, "croniter", invalid_schedule)

    with pytest.raises(SystemExit) as exc_info:
        scheduler.run_scheduler(lambda: None)

    assert exc_info.value.code == 1


def test_overrunning_run_does_not_trigger_catch_up(monkeypatch):
    """The next slot is computed from now, so missed slots are skipped."""
    _disable_scheduler_side_effects(monkeypatch)
    starts = []

    class _Schedule:
        def __init__(self, start):
            self.start = start

        def get_next(self, _return_type):
            starts.append(self.start)
            return self.start

    monkeypatch.setattr(scheduler, "croniter", lambda _expr, start: _Schedule(start))
    runs = []

    def run_pipeline():
        runs.append(1)
        if len(runs) == 2:
            scheduler.shutdown_requested = True

    scheduler.run_scheduler(run_pipeline)

    # Each loop builds a fresh schedule anchored at the current time instead of
    # stepping through a pre-computed sequence of (possibly missed) slots.
    assert len(runs) == 2
    assert len(starts) == 2
    assert starts == sorted(starts)


def test_release_workflow_runs_the_database_suite():
    """A version tag does not run ci.yml, so the release job must not skip it.

    indexer/tests/test_pipeline.py and agent/tests/test_snapshots.py skip the
    whole database suite when PGTEST_HOST is unset. That is a silent pass on
    a release that never started Postgres.
    """
    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
    ).read_text()
    test_job = workflow.split("build-and-push:", 1)[0]
    assert "pgvector/pgvector:pg18" in test_job
    assert "PGTEST_HOST: localhost" in test_job
