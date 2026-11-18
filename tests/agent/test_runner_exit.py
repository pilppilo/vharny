"""Runner-exit terminal evidence and dead-run operator-surface tests.

The attempt-2 acceptance run ended with the runner process dead on budget
exhaustion while the journal held no terminal fact and `status` still printed
`running` for the dead run (attempt-2 record §1.4a, trap 6). These tests pin
the fix: the bounded runner journals a `runner_exited` observation, the
operator surface reports a dead mid-flight run as failed with the reason, and
the stalled-wait report stays silent while a dispatch is outstanding.
"""

from __future__ import annotations

from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from vharness.agent import ActionChoice, EvaluationChoice, ScriptedModel
from vharness.agent.application import Application, RunDirectory, RunSettings
from vharness.agent.cli import _drive_application, main
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import Event, SessionStatus, SessionView
from vharness.agent.operator import build_operator_view, runner_exit_reason
from vharness.agent.replay import replay


def _settings(workspace: Path, **overrides) -> RunSettings:
    settings = RunSettings(
        objective="fix the fixture",
        workspace=str(workspace),
        base_url=None,
        model=None,
        api_key_env="TEST_KEY",
        max_actions=8,
        max_model_calls=1,
        max_evaluations=4,
        max_steps=20,
        token_budget=16384,
        max_output_tokens=256,
        repair_budget=0,
    )
    return replace(settings, **overrides) if overrides else settings


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 0\n", encoding="utf-8")
    return workspace


def _write_then_evaluate_model() -> ScriptedModel:
    return ScriptedModel(
        (
            ActionChoice(
                "write",
                "fix the fixture",
                {"path": "main.py", "content": "value = 1\n"},
            ),
            EvaluationChoice("request external evaluation"),
        )
    )


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    output = StringIO()
    error = StringIO()
    code = main(argv, output=output, error=error)
    return code, output.getvalue(), error.getvalue()


def _journal_events(run_dir: Path) -> tuple[Event, ...]:
    _directory, manifest = RunDirectory.open(run_dir)
    journal = SqliteJournal(_directory.journal_path, read_only=True)
    try:
        limit = max(journal.cursor(manifest.session_id), 1)
        return journal.events(manifest.session_id, limit=limit)
    finally:
        journal.close()


def test_budget_exhausted_run_records_its_outcome_and_status_reports_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "vharness.agent.cli.build_model",
        lambda _settings: (_write_then_evaluate_model(), None),
    )
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"

    code, output, error = _invoke(
        [
            "run",
            "--objective",
            "fix the fixture",
            "--workspace",
            str(workspace),
            "--run-dir",
            str(run_dir),
            "--max-model-calls",
            "1",
        ]
    )

    # ADR-0024: running out of budget is its own outcome, not a failure.
    assert code == 1
    assert "model call budget exhausted" in error
    assert "TERMINAL budget exhausted; model call budget exhausted" in output

    events = _journal_events(run_dir)
    assert events[-1].kind == "budget_exhausted"
    assert events[-1].payload == {
        "resource": "model call",
        "reason": "model call budget exhausted",
    }
    assert replay(events).status is SessionStatus.STOPPED

    code, status, status_error = _invoke(["status", "--run-dir", str(run_dir)])
    assert code == 0, status_error
    assert "STATUS stopped" in status
    assert "BUDGET-EXHAUSTED model call budget exhausted" in status


def test_operator_view_overlays_failure_only_for_live_statuses(tmp_path):
    run_dir = tmp_path / "run"

    # Reuse the real journal shape: a mid-flight session whose journal tail is
    # the runner exit reads as failed with the reason.
    workspace = _workspace(tmp_path)
    app = Application.create(
        _settings(workspace),
        run_dir,
        session_id="session-runner-exit-view",
        model=ScriptedModel(()),
    )
    try:
        app.session.record_runner_exit("model call budget exhausted")
        events = app.session.events(limit=1000)
        state = replay(events)
        view = build_operator_view(app.session.session_id, state, events)
        assert view.status is SessionStatus.FAILED
        assert runner_exit_reason(events) == "model call budget exhausted"

        # Events after the exit mean the run resumed: history, not state.
        tail = build_operator_view(app.session.session_id, state, events[:-1])
        assert tail.status is state.status
        assert runner_exit_reason(events[:-1]) is None
    finally:
        app.close()


def test_runner_exit_reason_reads_only_a_tail_event() -> None:
    exit_event = Event(
        event_id="event-exit",
        session_id="session-reason",
        sequence=2,
        kind="runner_exited",
        objective_version=1,
        payload={"reason": "step limit exhausted"},
        recorded_at="2026-11-02T00:00:00+00:00",
    )

    assert runner_exit_reason((exit_event,)) == "step limit exhausted"
    followup = replace(
        exit_event,
        event_id="event-followup",
        sequence=3,
        kind="checkpoint",
        payload={},
    )
    assert runner_exit_reason((exit_event, followup)) is None
    assert runner_exit_reason(()) is None
    blank = replace(exit_event, payload={})
    assert runner_exit_reason((blank,)) == "reason not recorded"


def test_stalled_wait_reports_only_without_outstanding_dispatch(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    app = Application.create(
        _settings(workspace),
        tmp_path / "run",
        session_id="session-stalled-wait",
        model=ScriptedModel(()),
    )
    base = app.view()
    ticks = {"n": 0}

    def waiting_view(pending: str | None):
        def view(_self: Application) -> SessionView:
            ticks["n"] += 1
            if ticks["n"] > 2:
                raise KeyboardInterrupt
            return replace(
                base,
                status=SessionStatus.WAITING,
                wait_reason="runtime receipt pending",
                pending_proposal_id=pending,
            )

        return view

    try:
        monkeypatch.setattr(
            Application, "tick", lambda self, allow_decision=True: "idle"
        )

        monkeypatch.setattr(Application, "view", waiting_view("proposal-1"))
        output = StringIO()
        code = _drive_application(app, 5, output=output, error=StringIO())
        assert code == 130
        # The receipt for an outstanding dispatch resolves the wait; reporting
        # it as stalled is the per-action noise the attempt-2 console suffered.
        assert "no outstanding dispatch" not in output.getvalue()

        ticks["n"] = 0
        monkeypatch.setattr(Application, "view", waiting_view(None))
        output = StringIO()
        code = _drive_application(app, 5, output=output, error=StringIO())
        assert code == 130
        assert output.getvalue().count("no outstanding dispatch") == 1
    finally:
        app.close()


def test_record_runner_exit_requires_a_reason(tmp_path):
    workspace = _workspace(tmp_path)
    app = Application.create(
        _settings(workspace),
        tmp_path / "run",
        session_id="session-exit-validation",
        model=ScriptedModel(()),
    )
    try:
        with pytest.raises(Exception, match="runner exit requires a reason"):
            app.session.record_runner_exit("   ")
    finally:
        app.close()
