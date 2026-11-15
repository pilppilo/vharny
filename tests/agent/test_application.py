"""Composition-root and coordinator-loop tests for the runnable application."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from vharness.agent import (
    ActionChoice,
    ContractError,
    EvaluationChoice,
    EvaluationDecision,
    OPERATOR_EVALUATION_WAIT,
    ScriptedModel,
    SessionStatus,
)
from vharness.agent.application import Application, RunSettings
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.control import (
    ACCEPTED,
    ControlServer,
    TransportInbox,
    command_frame,
)
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import Pause, Resume, Steer, Stop, UsageMeasurement
from vharness.agent.replay import replay
from vharness.agent.session_state import receipt_from_payload
from vharness.agent.supervision import (
    Guidance,
    ModelSupervisor,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
    stdout_fingerprints,
)
from vharness.core import Generation


def _settings(workspace: Path, **overrides) -> RunSettings:
    settings = RunSettings(
        objective="fix the fixture",
        workspace=str(workspace),
        base_url="http://localhost:1/v1",
        model="test-model",
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


def _passing_check() -> tuple[str, ...]:
    return (sys.executable, "-c", "raise SystemExit(0)")


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 0\n", encoding="utf-8")
    return workspace


def _kinds(app: Application) -> list[str]:
    return [event.kind for event in app.session.events(limit=1000)]


def _write(content: str) -> ActionChoice:
    return ActionChoice(
        "write",
        "fix the fixture",
        {"path": "main.py", "content": content},
    )


def test_run_settings_rejects_unknown_response_format(tmp_path):
    with pytest.raises(ContractError):
        _settings(tmp_path, response_format="bogus")


def test_run_settings_rejects_nonpositive_provider_timeout(tmp_path):
    with pytest.raises(ContractError):
        _settings(tmp_path, provider_timeout=0.0)


def test_run_settings_provider_timeout_round_trips_through_payload(tmp_path):
    settings = _settings(tmp_path, provider_timeout=300.0)
    payload = settings.to_payload()
    assert payload["provider_timeout"] == 300.0
    restored = RunSettings.from_payload(payload)
    assert restored == settings


def test_run_settings_from_payload_defaults_missing_response_format(tmp_path):
    settings = _settings(tmp_path)
    payload = settings.to_payload()
    del payload["response_format"]
    del payload["provider_timeout"]
    restored = RunSettings.from_payload(payload)
    assert restored.response_format is None
    assert restored.provider_timeout == 120.0


def test_run_directory_manifest_round_trips(tmp_path):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-1",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    try:
        assert (tmp_path / "run" / "run.json").is_file()
        assert app.session.session_id == "session-1"
    finally:
        app.close()

    reopened = Application.open(
        tmp_path / "run", model=ScriptedModel(()), poll_seconds=0
    )
    try:
        assert reopened.session.session_id == "session-1"
        assert reopened.view().task.objective == "fix the fixture"
        assert reopened.view().status is SessionStatus.CREATED
    finally:
        reopened.close()


def test_check_command_acceptance_dispatches_through_a_worker(tmp_path):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    model = ScriptedModel((_write("value = 1\n"), EvaluationChoice("run the check")))
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-2",
        model=model,
        poll_seconds=0,
    )
    try:
        app.run()
        kinds = _kinds(app)
        assert "action_intended" in kinds
        assert "receipt_received" in kinds
        assert "evaluation_received" in kinds
        assert (workspace / "main.py").read_text(encoding="utf-8") == "value = 1\n"
        received = [
            event
            for event in app.session.events(limit=1000)
            if event.kind == "evaluation_received"
        ]
        assert received
        assert received[-1].payload["receipt"]["accepted"] is True
    finally:
        app.close()


def test_deferred_exec_receipt_journals_evidence_fingerprints(tmp_path):
    """A deferred runtime dispatch journals the receipt's evidence fingerprints.

    Regression: the coordinator applied runtime receipts through
    ``receive_receipt`` without ever recording the intended action name, so
    the receipt-time evidence gate filtered out every fingerprint during live
    runs.
    """
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    model = ScriptedModel(
        (
            ActionChoice("exec", "run the probe", {"command": "echo probe-ok"}),
            EvaluationChoice("run the check"),
        )
    )
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-11",
        model=model,
        poll_seconds=0,
    )
    try:
        app.run()
        events = app.session.events(limit=1000)
        intended = next(
            event
            for event in events
            if event.kind == "action_intended"
            and event.payload["proposal"]["action_name"] == "exec"
        )
        receipt = next(
            event
            for event in events
            if event.kind == "receipt_received"
            and event.payload["receipt"]["proposal_id"]
            == intended.payload["proposal"]["proposal_id"]
        )
        assert receipt.payload["receipt"]["status"] == "succeeded"
        assert "evidence_fingerprints" in receipt.payload
        store = ArtifactStore(app.run_directory.artifacts_path)
        parsed = receipt_from_payload(receipt.payload["receipt"])
        artifact = parsed.raw_artifact
        assert artifact is not None
        expected = stdout_fingerprints(store.read(artifact), store)
        assert list(receipt.payload["evidence_fingerprints"]) == list(expected)
    finally:
        app.close()


def test_operator_evaluation_decision_completes_the_run(tmp_path):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace)  # no check command selects operator mode
    model = ScriptedModel((_write("value = 1\n"), EvaluationChoice("ask the operator")))
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-3",
        model=model,
        poll_seconds=0,
    )
    try:
        while app.view().wait_reason != OPERATOR_EVALUATION_WAIT:
            if app.tick() == "terminal":
                break
        assert app.view().status is SessionStatus.WAITING
        assert app.view().wait_reason == OPERATOR_EVALUATION_WAIT

        request_id = _pending_request_id(app)
        reference = ArtifactStore(app.run_directory.artifacts_path).put(
            b"operator confirmed the parser test passes",
            media_type="text/plain",
            provenance="operator evaluation",
        )
        decision = EvaluationDecision(
            command_id="decision-1",
            session_id="session-3",
            request_id=request_id,
            accepted=True,
            reason="the parser test now passes",
            completed=True,
            evidence_ref=reference,
        )
        reply = app.submit_frame(command_frame(decision))
        assert reply.status == ACCEPTED
        assert app.tick() == "terminal"
        assert app.view().status is SessionStatus.COMPLETED
    finally:
        app.close()


def test_pause_blocks_scheduling_and_stop_is_terminal(tmp_path):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    model = ScriptedModel(tuple(_write(f"value = {index}\n") for index in range(1, 5)))
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-4",
        model=model,
        poll_seconds=0,
    )
    try:
        assert app.tick() == "decided"
        assert app.tick() == "dispatched"

        assert (
            app.submit_frame(command_frame(Pause("pause-1", "session-4"))).status
            == ACCEPTED
        )
        assert app.tick() == "idle"
        assert app.view().status is SessionStatus.PAUSED
        cursor = app.session.view().cursor
        assert app.tick() == "idle"
        assert app.session.view().cursor == cursor  # paused persists no new work

        assert (
            app.submit_frame(command_frame(Resume("resume-1", "session-4"))).status
            == ACCEPTED
        )
        assert app.tick() == "decided"

        assert (
            app.submit_frame(command_frame(Stop("stop-1", "session-4"))).status
            == ACCEPTED
        )
        assert app.tick() == "terminal"
        assert app.view().status is SessionStatus.STOPPED
    finally:
        app.close()


def test_reopen_reconciles_an_unresolved_action_as_unknown(tmp_path):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    model = ScriptedModel((_write("value = 1\n"),))
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-5",
        model=model,
        poll_seconds=0,
    )
    try:
        handle = app.session.prepare_decision(model, dispatch=False)
        assert handle is not None
        app.session.apply_decision(handle, model.decide(handle.request), dispatch=False)
        assert app.session.take_pending_dispatch() is not None
        proposal_id = app.session.view().pending_proposal_id
        assert proposal_id is not None
    finally:
        app.close()

    reopened = Application.open(
        tmp_path / "run", model=ScriptedModel(()), poll_seconds=0
    )
    try:
        # An unknown outcome is never treated as not-submitted: the unresolved
        # operation stays visible and is not resent or fabricated as a receipt.
        assert reopened.view().pending_proposal_id == proposal_id
        assert "unknown" in (reopened.view().wait_reason or "")
        reconciled = [
            event
            for event in reopened.session.events(limit=1000)
            if event.kind == "operation_reconciled"
        ]
        assert reconciled
        assert reconciled[-1].payload["result"] == "unknown"
        assert "receipt_received" not in _kinds(reopened)
    finally:
        reopened.close()


def test_run_directory_refuses_to_overwrite_an_existing_run(tmp_path):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-6",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    app.close()
    with pytest.raises(ContractError, match="already contains"):
        Application.create(
            settings,
            tmp_path / "run",
            session_id="session-7",
            model=ScriptedModel(()),
            poll_seconds=0,
        )


def test_start_controls_serves_and_closes_the_run_socket(tmp_path):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-8",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    try:
        app.start_controls()
        assert app.run_directory.control_socket_path.exists()
    finally:
        app.close()
    assert not app.run_directory.control_socket_path.exists()


def test_deferred_action_is_not_submitted_after_a_durable_steer(tmp_path):
    """A steer applied before the dispatch prevents the stale write entirely."""
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    model = ScriptedModel((_write("value = 1\n"), _write("value = 2\n")))
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-9",
        model=model,
        poll_seconds=0,
    )
    try:
        handle = app.session.prepare_decision(model, dispatch=False)
        assert handle is not None
        app.session.apply_decision(handle, model.decide(handle.request), dispatch=False)
        # The operator steers before the coordinator dispatches the pending write.
        app.session.enqueue(
            Steer("steer-1", "session-9", "new objective", "operator evidence")
        )

        app.tick()  # applies the steer first, then would dispatch stale work

        # The stale write never reached the workspace.
        assert (workspace / "main.py").read_text(encoding="utf-8") == "value = 0\n"
        assert "receipt_received" not in _kinds(app)
        reconciled = [
            event
            for event in app.session.events(limit=1000)
            if event.kind == "operation_reconciled"
        ]
        assert reconciled
        assert reconciled[-1].payload["result"] == "not_submitted"
    finally:
        app.close()


def _guidance() -> Guidance:
    return Guidance(
        diagnosis="repeated writes without new information",
        constraints=("avoid rewriting the same file",),
        suggested_experiments=("read the receipt instead",),
        expires_after_call=20,
    )


def _supervisor_response() -> SupervisorResponse:
    return SupervisorResponse(
        _guidance(),
        UsageMeasurement("application-supervisor-usage", model_calls=1),
        "scripted-supervisor",
    )


def test_pause_before_a_deferred_consult_settles_it_without_calling(tmp_path):
    """A pause applied before dispatch prevents the supervisor model call."""
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    capability = SupervisionCapability(
        supervisor,
        MonitorPolicy(window_calls=4, repeat_threshold=3, cooldown_calls=1),
    )
    model = ScriptedModel(tuple(_write("value = 1\n") for _ in range(5)))
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-10",
        model=model,
        supervisor=capability,
        poll_seconds=0,
    )
    try:
        # Three repeated decisions arm the monitor; the fourth defers a consult.
        for _ in range(6):
            app.tick()
        assert app.tick() == "idle"

        assert (
            app.submit_frame(command_frame(Pause("pause-1", "session-10"))).status
            == ACCEPTED
        )
        assert app.tick() == "idle"
        assert app.view().status is SessionStatus.PAUSED
        assert supervisor.calls == 0
        assert "supervisor_failed" in _kinds(app)
        assert "guidance_received" not in _kinds(app)
    finally:
        app.close()


def _pending_request_id(app: Application) -> str:
    for event in reversed(app.session.events(limit=1000)):
        if event.kind == "evaluation_intended":
            request = event.payload["request"]
            return str(request["request_id"])
    raise AssertionError("no pending evaluation request")


class _RecordingGenerator:
    """Text generator that records calls and returns one guidance payload."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, prompt: str):
        self.calls.append((system, prompt))
        return Generation(
            text=self._text,
            model="supervisor-model",
            finish_reason="stop",
            prompt_tokens=17,
            completion_tokens=5,
        )


def _guidance_json() -> str:
    return (
        '{"kind": "Guidance", "diagnosis": "repeating the same write", '
        '"constraints": [], "suggested_experiments": ["read the receipt"], '
        '"expires_after_call": 20}'
    )


def test_application_enables_supervision_for_a_live_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "unit-test-key")
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-live",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    try:
        assert "supervision_enabled" in _kinds(app)
    finally:
        app.close()


def test_application_skips_supervision_without_a_provider_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-nokey",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    try:
        assert "supervision_enabled" not in _kinds(app)
    finally:
        app.close()


def test_application_invokes_the_configured_supervisor(tmp_path, monkeypatch):
    """A triggered consult runs the model-backed supervisor through a worker."""
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    generator = _RecordingGenerator(_guidance_json())
    capability = SupervisionCapability(
        ModelSupervisor(generator, model_id="supervisor-model"),
        MonitorPolicy(window_calls=4, repeat_threshold=3, cooldown_calls=1),
    )
    monkeypatch.setattr(
        "vharness.agent.application.build_supervisor",
        lambda _settings: (capability, None),
    )
    model = ScriptedModel(tuple(_write("value = 1\n") for _ in range(8)))
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-11",
        model=model,
        poll_seconds=0,
    )
    try:
        for _ in range(12):
            if app.tick() == "terminal":
                break
        kinds = _kinds(app)
        assert "supervision_triggered" in kinds
        assert "guidance_received" in kinds
        assert generator.calls, "the supervisor model was never invoked"
        assert "Objective:" in generator.calls[0][1]
    finally:
        app.close()


def test_malformed_supervisor_response_settles_measured_usage_once(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    settings = _settings(workspace, check_command=_passing_check())
    generator = _RecordingGenerator("not a JSON object")
    capability = SupervisionCapability(
        ModelSupervisor(generator, model_id="supervisor-model"),
        MonitorPolicy(window_calls=4, repeat_threshold=3, cooldown_calls=1),
    )
    monkeypatch.setattr(
        "vharness.agent.application.build_supervisor",
        lambda _settings: (capability, None),
    )
    app = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-supervisor-usage-failure",
        model=ScriptedModel(tuple(_write("value = 1\n") for _ in range(8))),
        poll_seconds=0,
    )
    try:
        for _ in range(12):
            if "supervisor_failed" in _kinds(app):
                break
            app.tick()
        failures = [
            event for event in app.session.events() if event.kind == "supervisor_failed"
        ]
        assert len(failures) == 1
        usage = failures[0].payload["usage"]
        assert usage["model_calls"] == 1
        assert usage["input_tokens"] == 17
        assert usage["output_tokens"] == 5
        assert failures[0].payload["error"] == "malformed model output"
        assert len(generator.calls) == 1
        counters = app.session.counters()
        assert counters.model_calls == 4
        assert counters.supervisor_calls == 1
        assert counters.supervisor_input_tokens == 17
        assert counters.supervisor_output_tokens == 5
        assert counters.conservative_supervisor_calls == 1
        assert replay(app.session.events()) == app.session._state
    finally:
        app.close()


def test_open_refuses_a_live_coordinator_without_opening_a_second_writer(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = tmp_path / "run"
    owner = Application.create(
        _settings(workspace, check_command=_passing_check()),
        run_dir,
        session_id="session-live-owner",
        model=ScriptedModel(()),
        poll_seconds=0,
    )
    owner.start_controls()
    journal = SqliteJournal(run_dir / "session.sqlite3", read_only=True)
    try:
        before = journal.cursor("session-live-owner")
        with pytest.raises(ContractError, match="another coordinator owns"):
            Application.open(run_dir, model=ScriptedModel(()))
        contender = ControlServer(TransportInbox(), run_dir / "control.sock")
        with pytest.raises(ContractError, match="another coordinator owns"):
            contender.start()
        contender.close()
        assert (run_dir / "control.sock").exists()
        assert journal.cursor("session-live-owner") == before
    finally:
        journal.close()
        owner.close()
