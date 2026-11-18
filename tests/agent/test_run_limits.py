"""Whole-run limits (COD-86, ADR-0024): tokens and wall-clock time.

Each test drives the real coordinator over a scripted provider. Running out
of either limit ends the run with a ``budget_exhausted`` outcome that names
the resource, and replay accepts the journal.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from vharness.agent import SessionStatus
from vharness.agent.application import Application, RunSettings
from vharness.agent.kernel import CompatibleModel
from vharness.agent.replay import replay
from vharness.core import Generation


class _Transport:
    """Scripted provider: answers LineageQuery, reporting fixed token usage.

    It reports a realistic prompt size: the calibrated estimate of later
    prompts follows what the provider reports (ADR-0020).
    """

    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.max_retries = 0
        self.length_retries = 0
        self.max_tokens = 256
        self.calls = 0

    def generate(self, system: str, prompt: str) -> Generation:
        del system, prompt
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return Generation(
            text=json.dumps({"kind": "LineageQuery", "node_id": None}),
            model="scripted",
            finish_reason="stop",
            prompt_tokens=8000,
            completion_tokens=20,
        )


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


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 0\n", encoding="utf-8")
    return workspace


def _app(tmp_path: Path, transport: _Transport, **overrides) -> Application:
    return Application.create(
        _settings(_workspace(tmp_path), **overrides),
        tmp_path / "run",
        session_id="session-limits",
        model=CompatibleModel(transport, model_id="scripted", repair_budget=0),
        poll_seconds=0,
    )


def _events(app: Application, kind: str) -> list:
    return [event for event in app.session.events(limit=1000) if event.kind == kind]


def _drive_until_stopped(app: Application, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if app.view().status is SessionStatus.STOPPED:
            return
        app.tick()
        time.sleep(0.001)
    raise AssertionError("the run did not stop")


def _first_reservation_tokens(tmp_path: Path) -> int:
    """Worst-case tokens the first decision reserves, under a generous limit."""
    probe = tmp_path / "probe"
    probe.mkdir()
    app = _app(probe, _Transport(), max_tokens=10_000_000)
    try:
        app.tick()
        reservation = _events(app, "model_intended")[0].payload["reservation"]
        return reservation["tokens"]
    finally:
        app.close()


def test_a_decision_reserves_its_worst_case_and_settles_to_billed_use(tmp_path):
    worst_case = _first_reservation_tokens(tmp_path)
    assert worst_case > 8020  # above the 8,000 + 20 the provider bills
    # Room for one worst case, not for the billed use plus a second one.
    transport = _Transport()
    app = _app(tmp_path, transport, max_tokens=worst_case + 50)
    try:
        _drive_until_stopped(app)
        assert transport.calls == 1
        assert len(_events(app, "model_decided")) == 1
        payload = _events(app, "budget_exhausted")[0].payload
        assert payload == {"resource": "token", "reason": "token budget exhausted"}
        assert replay(app.session.events(limit=1000)).status is SessionStatus.STOPPED
    finally:
        app.close()


def test_a_limit_below_one_worst_case_stops_before_any_call(tmp_path):
    transport = _Transport()
    app = _app(tmp_path, transport, max_tokens=50)
    try:
        _drive_until_stopped(app)
        assert transport.calls == 0
        assert _events(app, "budget_exhausted")[0].payload["resource"] == "token"
    finally:
        app.close()


def test_without_a_token_limit_reservations_carry_no_tokens(tmp_path):
    app = _app(tmp_path, _Transport())
    try:
        app.tick()
        reservation = _events(app, "model_intended")[0].payload["reservation"]
        assert "tokens" not in reservation  # older journals' shape, unchanged
    finally:
        app.close()


def test_the_deadline_stops_a_run_between_decisions(tmp_path):
    transport = _Transport()
    app = _app(tmp_path, transport, max_wall_clock_seconds=1)
    try:
        time.sleep(1.1)
        assert app.tick() == "terminal"
        assert transport.calls == 0
        payload = _events(app, "budget_exhausted")[0].payload
        assert payload == {"resource": "time", "reason": "time budget exhausted"}
        replay(app.session.events(limit=1000))
    finally:
        app.close()


def test_the_deadline_discards_a_decision_still_in_flight(tmp_path):
    transport = _Transport(delay_seconds=2.0)
    app = _app(tmp_path, transport, max_wall_clock_seconds=1)
    try:
        _drive_until_stopped(app, timeout_seconds=5.0)
        assert not _events(app, "model_decided")
        assert len(_events(app, "model_malformed")) == 1
        assert _events(app, "budget_exhausted")[0].payload["resource"] == "time"
        replay(app.session.events(limit=1000))
    finally:
        app.close()


def test_a_resumed_run_keeps_its_original_deadline(tmp_path):
    settings = _settings(_workspace(tmp_path), max_wall_clock_seconds=1)
    model = CompatibleModel(_Transport(), model_id="scripted", repair_budget=0)
    first = Application.create(
        settings,
        tmp_path / "run",
        session_id="session-resume",
        model=model,
        poll_seconds=0,
    )
    first.close()
    time.sleep(1.1)  # the run is down; elapsed time still counts
    resumed = Application.open(tmp_path / "run", model=model, poll_seconds=0)
    try:
        assert resumed.tick() == "terminal"
        assert _events(resumed, "budget_exhausted")[0].payload["resource"] == "time"
    finally:
        resumed.close()
