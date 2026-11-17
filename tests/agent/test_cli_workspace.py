"""Workspace, run-directory, and operator-control tests for the thin CLI."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import replace
from io import StringIO
from pathlib import Path

from vharness.agent import (
    ActionChoice,
    EvaluationChoice,
    OPERATOR_ANSWER_WAIT,
    OPERATOR_EVALUATION_WAIT,
    OperatorQuestion,
    ScriptedModel,
    SessionStatus,
)
from vharness.agent.application import Application, RunSettings
from vharness.agent.cli import _workspace_settings, main
from vharness.agent.journal import SqliteJournal


def _passing_check() -> str:
    return f"{sys.executable} -c pass"


def _base_settings(workspace: Path, **overrides) -> RunSettings:
    settings = RunSettings(
        objective="fix the fixture",
        workspace=str(workspace),
        base_url=None,
        model=None,
        api_key_env="TEST_KEY",
        max_actions=8,
        max_model_calls=8,
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


def _write_model() -> ScriptedModel:
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


def _run_argv(workspace: Path, run_dir: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--objective",
        "fix the fixture",
        "--workspace",
        str(workspace),
        "--run-dir",
        str(run_dir),
        *extra,
    ]


def test_workspace_run_streams_a_check_command_milestone(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vharness.agent.cli.build_model", lambda _settings: (_write_model(), None)
    )
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"

    code, output, error = _invoke(
        _run_argv(workspace, run_dir, "--check-command", _passing_check())
    )

    # A check-command pass records a milestone; a finite task completes only on
    # an accepted operator decision, so the scripted model then runs dry.
    assert "mode=workspace" in output
    assert "EVALUATION disposition=accepted accepted=True" in output
    assert (run_dir / "run.json").is_file()
    assert (workspace / "main.py").read_text(encoding="utf-8") == "value = 1\n"
    assert code == 1
    assert "run failed" in error


def test_workspace_run_waits_for_operator_evaluation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vharness.agent.cli.build_model", lambda _settings: (_write_model(), None)
    )
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    result: dict[str, tuple[int, str, str]] = {}

    def run_foreground() -> None:
        result["run"] = _invoke(_run_argv(workspace, run_dir))

    thread = threading.Thread(target=run_foreground, daemon=True)
    thread.start()
    request_id = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and thread.is_alive():
        code, status, _error = _invoke(["status", "--run-dir", str(run_dir)])
        if code == 0:
            for line in status.splitlines():
                if line.startswith("EVALUATION request_id="):
                    request_id = line.split("=", 1)[1]
                    break
        if request_id is not None:
            break
        time.sleep(0.02)
    assert request_id is not None
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("operator verified the completed result", encoding="utf-8")
    code, output, error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "accept",
            "--reason",
            "the requested change is present",
            "--evidence",
            str(evidence),
            "--completed",
        ]
    )
    thread.join(timeout=10)

    assert not thread.is_alive(), "foreground run did not apply the IPC decision"
    assert code == 0, output + error
    assert "EVALUATION decision admitted" in output
    run_code, run_output, run_error = result["run"]
    assert run_code == 0, run_output + run_error
    assert f"request_id={request_id}" in run_output
    assert "TERMINAL success" in run_output
    assert OPERATOR_EVALUATION_WAIT in run_output
    assert (workspace / "main.py").read_text(encoding="utf-8") == "value = 1\n"


def test_evaluate_applies_a_rejection_to_a_detached_waiting_run(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-detached-evaluation",
        model=_write_model(),
        poll_seconds=0,
    )
    try:
        for _ in range(20):
            if app.view().wait_reason == OPERATOR_EVALUATION_WAIT:
                break
            app.tick()
        assert app.view().wait_reason == OPERATOR_EVALUATION_WAIT
        request_id = next(
            event.payload["request"]["request_id"]
            for event in app.session.events()
            if event.kind == "evaluation_intended"
        )
    finally:
        app.close()

    code, output, error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "reject",
            "--reason",
            "the evidence does not establish completion",
        ]
    )

    assert code == 0, output + error
    assert "EVALUATION decision applied" in output
    retry_code, retry_output, retry_error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "reject",
            "--reason",
            "the evidence does not establish completion",
        ]
    )
    assert retry_code == 0, retry_output + retry_error
    assert "EVALUATION decision applied" in retry_output
    conflict_code, _conflict_output, conflict_error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "reject",
            "--reason",
            "a conflicting retry must be rejected",
        ]
    )
    assert conflict_code == 1
    assert "rejected" in conflict_error
    status_code, status, status_error = _invoke(["status", "--run-dir", str(run_dir)])
    assert status_code == 0, status_error
    assert f"EVALUATION request_id={request_id}" not in status


def test_evaluate_requires_evidence_for_acceptance(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-evidence-required",
        model=_write_model(),
        poll_seconds=0,
    )
    try:
        for _ in range(20):
            if app.view().wait_reason == OPERATOR_EVALUATION_WAIT:
                break
            app.tick()
        request_id = next(
            event.payload["request"]["request_id"]
            for event in app.session.events()
            if event.kind == "evaluation_intended"
        )
    finally:
        app.close()

    code, output, error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "accept",
            "--reason",
            "accepted without proof",
        ]
    )
    assert code == 2
    assert not output
    assert "requires --evidence" in error
    rejected_completion, _output, reject_error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "reject",
            "--reason",
            "a rejection cannot complete the task",
            "--completed",
        ]
    )
    assert rejected_completion == 2
    assert "cannot complete" in reject_error


def test_workspace_run_rejects_journal_artifacts_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vharness.agent.cli.build_model", lambda _settings: (_write_model(), None)
    )
    workspace = _workspace(tmp_path)
    code, output, error = _invoke(
        [
            *_run_argv(workspace, tmp_path / "run"),
            "--journal",
            str(tmp_path / "session.sqlite3"),
        ]
    )
    assert code == 2
    assert not output
    assert "--journal" in error


def test_workspace_run_requires_a_run_directory(tmp_path):
    workspace = _workspace(tmp_path)
    code, output, error = _invoke(
        ["run", "--objective", "x", "--workspace", str(workspace)]
    )
    assert code == 2
    assert not output
    assert "--run-dir" in error


def test_run_dir_without_workspace_is_rejected(tmp_path):
    code, output, error = _invoke(
        [
            "run",
            "--objective",
            "x",
            "--run-dir",
            str(tmp_path / "run"),
            "--local-smoke",
        ]
    )
    assert code == 2
    assert not output
    assert "--workspace" in error


def test_workspace_run_rejects_local_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vharness.agent.cli.build_model", lambda _settings: (_write_model(), None)
    )
    workspace = _workspace(tmp_path)
    code, output, error = _invoke(
        [*_run_argv(workspace, tmp_path / "run"), "--local-smoke"]
    )
    assert code == 2
    assert not output
    assert "--local-smoke" in error


def test_status_reads_a_run_directory_without_writing(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-status-readonly",
        model=_write_model(),
        poll_seconds=0,
    )
    app.start_controls()
    try:
        for _ in range(20):
            if app.view().wait_reason == OPERATOR_EVALUATION_WAIT:
                break
            app.tick()
        assert app.view().wait_reason == OPERATOR_EVALUATION_WAIT
        before = app.view().cursor

        code, output, error = _invoke(["status", "--run-dir", str(run_dir)])

        assert code == 0, error
        assert "STATUS waiting" in output
        assert "fix the fixture" in output
        assert "EVALUATION request_id=" in output
        assert app.view().cursor == before
    finally:
        app.close()


def test_status_does_not_create_a_missing_run_journal(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-missing-journal",
        model=ScriptedModel(()),
    )
    app.close()
    journal_path = run_dir / "session.sqlite3"
    journal_path.unlink()

    code, output, error = _invoke(["status", "--run-dir", str(run_dir)])

    assert code == 1
    assert not output
    assert "journal does not exist" in error
    assert not journal_path.exists()
    assert not Path(f"{journal_path}-wal").exists()
    assert not Path(f"{journal_path}-shm").exists()


def test_resume_reopens_a_run_directory_and_drives_it(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-resume",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    app.close()
    monkeypatch.setattr(
        "vharness.agent.cli.build_model", lambda _settings: (_write_model(), None)
    )

    result: dict[str, tuple[int, str, str]] = {}

    def resume_foreground() -> None:
        result["resume"] = _invoke(["resume", "--run-dir", str(run_dir)])

    thread = threading.Thread(target=resume_foreground, daemon=True)
    thread.start()
    request_id = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and thread.is_alive():
        code, status, _error = _invoke(["status", "--run-dir", str(run_dir)])
        if code == 0:
            for line in status.splitlines():
                if line.startswith("EVALUATION request_id="):
                    request_id = line.split("=", 1)[1]
                    break
        if request_id is not None:
            break
        time.sleep(0.02)
    assert request_id is not None
    evidence = tmp_path / "resume-evidence.txt"
    evidence.write_text("accepted result", encoding="utf-8")
    code, output, error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "accept",
            "--reason",
            "the change is complete",
            "--evidence",
            str(evidence),
            "--completed",
        ]
    )
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert code == 0, output + error
    resume_code, resume_output, resume_error = result["resume"]
    assert resume_code == 0, resume_output + resume_error
    assert "RESUME session=session-resume" in resume_output
    assert "ACTION write" in resume_output
    assert "TERMINAL success" in resume_output


def test_max_steps_keeps_evaluation_wait_open_but_does_not_decide_again(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "vharness.agent.cli.build_model",
        lambda _settings: (ScriptedModel((EvaluationChoice("inspect result"),)), None),
    )
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    result: dict[str, tuple[int, str, str]] = {}

    def run_one_step() -> None:
        result["run"] = _invoke(_run_argv(workspace, run_dir, "--max-steps", "1"))

    thread = threading.Thread(target=run_one_step, daemon=True)
    thread.start()
    request_id = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and thread.is_alive():
        code, status, _error = _invoke(["status", "--run-dir", str(run_dir)])
        if code == 0:
            for line in status.splitlines():
                if line.startswith("EVALUATION request_id="):
                    request_id = line.split("=", 1)[1]
                    break
        if request_id is not None:
            break
        time.sleep(0.02)
    assert request_id is not None
    evidence = tmp_path / "milestone.txt"
    evidence.write_text("milestone verified", encoding="utf-8")
    code, output, error = _invoke(
        [
            "evaluate",
            "--run-dir",
            str(run_dir),
            "--request-id",
            request_id,
            "--decision",
            "accept",
            "--reason",
            "the milestone is demonstrated",
            "--evidence",
            str(evidence),
        ]
    )
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert code == 0, output + error
    run_code, run_output, run_error = result["run"]
    assert run_code == 1, run_output + run_error
    assert "step limit exhausted" in run_error
    journal = SqliteJournal(run_dir / "session.sqlite3", read_only=True)
    try:
        events = journal.events(
            json.loads((run_dir / "run.json").read_text())["session_id"]
        )
    finally:
        journal.close()
    assert sum(event.kind == "model_intended" for event in events) == 1


def _answer_operator_question(run_dir: Path, text: str) -> tuple[int, str, str]:
    """Submit one answer until the coordinator accepts it, or time out."""
    result = _invoke(["message", "--run-dir", str(run_dir), "--text", text])
    deadline = time.monotonic() + 10
    while result[0] != 0 and time.monotonic() < deadline:
        time.sleep(0.02)
        result = _invoke(["message", "--run-dir", str(run_dir), "--text", text])
    return result


def test_workspace_run_keeps_an_operator_answer_wait_answerable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vharness.agent.cli.build_model",
        lambda _settings: (ScriptedModel((OperatorQuestion("need a hand?"),)), None),
    )
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    result: dict[str, tuple[int, str, str]] = {}

    def run_foreground() -> None:
        result["run"] = _invoke(_run_argv(workspace, run_dir))

    thread = threading.Thread(target=run_foreground, daemon=True)
    thread.start()
    parked = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and thread.is_alive():
        code, status, _error = _invoke(["status", "--run-dir", str(run_dir)])
        if code == 0 and "need a hand?" in status and "QUESTION question-" in status:
            parked = True
            break
        time.sleep(0.02)

    assert parked, "the run never parked on the operator question"
    assert thread.is_alive(), "the run exited instead of blocking on the answer wait"

    code, output, error = _answer_operator_question(run_dir, "use the workspace notes")
    thread.join(timeout=10)

    assert code == 0, output + error
    assert "MESSAGE accepted" in output
    assert not thread.is_alive(), "the run did not resume after the answer"
    run_code, run_output, run_error = result["run"]
    assert run_code == 1, run_output + run_error
    assert "run failed" in run_error


def test_resume_drives_an_operator_answer_wait(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-answer-wait",
        model=ScriptedModel((OperatorQuestion("need a hand?"),)),
        poll_seconds=0,
    )
    try:
        for _ in range(10):
            if app.view().wait_reason == OPERATOR_ANSWER_WAIT:
                break
            app.tick()
        assert app.view().wait_reason == OPERATOR_ANSWER_WAIT
    finally:
        app.close()
    monkeypatch.setattr(
        "vharness.agent.cli.build_model",
        lambda _settings: (ScriptedModel(()), None),
    )
    result: dict[str, tuple[int, str, str]] = {}

    def resume_foreground() -> None:
        result["resume"] = _invoke(["resume", "--run-dir", str(run_dir)])

    thread = threading.Thread(target=resume_foreground, daemon=True)
    thread.start()
    time.sleep(0.5)

    assert thread.is_alive(), "resume exited instead of driving the answer wait"
    code, output, error = _answer_operator_question(run_dir, "use the workspace notes")
    thread.join(timeout=10)

    assert code == 0, output + error
    assert "MESSAGE accepted" in output
    assert not thread.is_alive(), "resume did not continue after the answer"
    resume_code, resume_output, resume_error = result["resume"]
    assert "RESUME session=session-answer-wait" in resume_output
    assert resume_code == 1, resume_output + resume_error


def _submit_with_coordinator(app: Application, argv: list[str]) -> int:
    """Run one CLI control while a background loop drives the coordinator."""
    result: dict[str, int] = {}

    def worker() -> None:
        result["code"] = _invoke(argv)[0]

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while thread.is_alive() and time.monotonic() < deadline:
        app.tick()
        time.sleep(0.01)
    thread.join(timeout=10.0)
    return result.get("code", -1)


def _park_on_operator_evaluation(app: Application) -> None:
    for _ in range(20):
        if app.view().wait_reason == OPERATOR_EVALUATION_WAIT:
            return
        if app.tick() == "terminal":
            break
    raise AssertionError("session never reached the operator evaluation wait")


def test_pause_and_stop_reach_a_running_coordinator(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-control",
        model=_write_model(),
        poll_seconds=0,
    )
    app.start_controls()
    try:
        _park_on_operator_evaluation(app)

        code = _submit_with_coordinator(app, ["pause", "--run-dir", str(run_dir)])
        assert code == 0
        for _ in range(50):
            app.tick()
            if app.view().status is SessionStatus.PAUSED:
                break
        assert app.view().status is SessionStatus.PAUSED

        code = _submit_with_coordinator(app, ["stop", "--run-dir", str(run_dir)])
        assert code == 0
        for _ in range(50):
            app.tick()
            if app.view().status is SessionStatus.STOPPED:
                break
        assert app.view().status is SessionStatus.STOPPED
    finally:
        app.close()


def test_pause_without_a_coordinator_is_actionable(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    app = Application.create(
        _base_settings(workspace),
        run_dir,
        session_id="session-offline",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    app.close()

    code, output, error = _invoke(["pause", "--run-dir", str(run_dir)])

    assert code == 1
    assert not output
    assert "no running coordinator" in error


def test_workspace_settings_carry_response_format():
    arguments = argparse.Namespace(
        objective="inspect the workspace",
        workspace="/tmp/workspace",
        base_url="https://example/v1",
        model="test-model",
        response_format="json_object",
        provider_timeout=300.0,
        provider_stream=True,
        api_key_env="TEST_KEY",
        max_actions=6,
        max_model_calls=8,
        max_evaluations=2,
        max_steps=12,
        token_budget=16384,
        max_output_tokens=512,
        repair_budget=1,
        target=None,
        check_command=None,
    )

    settings = _workspace_settings(arguments)

    assert settings.response_format == "json_object"
    assert settings.provider_timeout == 300.0
    assert settings.provider_stream is True
