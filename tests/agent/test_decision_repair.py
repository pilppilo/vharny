"""ADR-0015 decision decode repair ladder: rung 1 and rung 3.

Rung 1 accepts a result whose only schema defect is one missing
observational field, substituting a deterministic marker at no provider
cost, and journals a ``model_repaired`` note in the terminal batch. Rung 3
routes a ``malformed-output`` failure that exhausted its repair budget into
the durable, operator-resumable provider-recovery wait instead of a runner
failure; Resume re-decides. Fixtures carry the payload shape run6 received
twice from the provider (journal event 260 of
``/home/vhrun/runs/htb-timing/run6``): a valid object omitting only the
required ``rationale``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from vharness.agent import SessionStatus
from vharness.agent.application import Application, RunSettings
from vharness.agent.control import ACCEPTED, command_frame
from vharness.agent.kernel import (
    OBSERVATIONAL_REPAIR_FAILURE_CLASS,
    OPERATIONAL_REPAIR_MARKER,
    CompatibleModel,
    repair_observational_output,
)
from vharness.agent.models import Resume
from vharness.agent.replay import replay
from vharness.agent.run_settings import TransportRecoverySettings
from vharness.core import Generation

# The exact ActionChoice shape of run6's failing payload (event 260): a
# well-formed object omitting only the required rationale.
RUN6_JOB_READ = (
    '{"kind":"ActionChoice","action_name":"job_read",'
    '"arguments":{"job_id":"job-proposal-290293ad834c4f799c147bbbe47a8064",'
    '"stdout_offset":1670}}'
)

# The same defect shape over a dispatchable workspace action.
RUN6_READ_ACTION = (
    '{"kind": "ActionChoice", "action_name": "read",'
    ' "arguments": {"path": "main.py"}}'
)
RUN6_WAIT_CHOICE = '{"kind": "WaitChoice"}'
RUN6_GUIDANCE = '{"kind": "GuidanceResponse", "reason": "kept brief"}'
MARKER = "<rationale omitted by provider; recovered by decode>"
REASON_MARKER = "<reason omitted by provider; recovered by decode>"

# Rung 2 fixtures: one missing operational field, then either a local
# correction or a restatement that changes a pinned field.
OPERATIONAL_MISSING_ACTION = (
    '{"kind": "ActionChoice", "arguments": {"path": "main.py"},'
    ' "rationale": "read the file"}'
)
OPERATIONAL_REPAIRED_ACTION = (
    '{"kind": "ActionChoice", "action_name": "read",'
    ' "arguments": {"path": "main.py"}, "rationale": "read the file"}'
)
OPERATIONAL_CONFLICTING_ACTION = (
    '{"kind": "ActionChoice", "action_name": "read",'
    ' "arguments": {"path": "other.py"}, "rationale": "read the file"}'
)


class _ScriptedTransport:
    def __init__(self, script: tuple[Generation, ...]) -> None:
        self._script = list(script)
        self.max_retries = 0
        self.length_retries = 0
        self.calls = 0
        self.prompts: list[str] = []

    def extend(self, *entries: Generation) -> None:
        self._script.extend(entries)

    def generate(self, system: str, prompt: str) -> Generation:
        del system
        self.calls += 1
        self.prompts.append(prompt)
        if self._script:
            return self._script.pop(0)
        raise RuntimeError("scripted transport is exhausted")


def _lineage_success() -> Generation:
    return Generation(
        text=json.dumps({"kind": "LineageQuery", "node_id": None}),
        model="scripted-transport",
        finish_reason="stop",
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 0\n", encoding="utf-8")
    return workspace


def _settings(workspace: Path) -> RunSettings:
    return RunSettings(
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


def _fast_recovery() -> TransportRecoverySettings:
    return TransportRecoverySettings(max_retries=2, retry_delays_seconds=(0.001, 0.001))


def _application(
    tmp_path: Path,
    session_id: str,
    transport: _ScriptedTransport,
    *,
    repair_budget: int = 0,
) -> Application:
    """One application over the scripted transport with a flat repair budget."""
    settings = replace(
        _settings(_workspace(tmp_path)),
        repair_budget=repair_budget,
        transport_recovery=_fast_recovery(),
    )
    model = CompatibleModel(
        transport,
        model_id="scripted-transport",
        repair_budget=repair_budget,
    )
    return Application.create(
        settings,
        tmp_path / "run",
        session_id=session_id,
        model=model,
        poll_seconds=0,
    )


def _kinds(app: Application) -> list[str]:
    return [event.kind for event in app.session.events(limit=1000)]


def _events_of(app: Application, kind: str) -> list:
    return [event for event in app.session.events(limit=1000) if event.kind == kind]


# --- rung 1: the decode-side repair -------------------------------------


def test_repair_observational_output_accepts_run6_shape() -> None:
    """The run6 payload repairs to a typed ActionChoice with the marker."""
    outcome = repair_observational_output(RUN6_JOB_READ)
    assert outcome is not None
    result, note = outcome
    assert result.action_name == "job_read"
    assert result.arguments["job_id"].startswith("job-proposal-290293ad")
    assert result.rationale == MARKER
    assert note == {
        "failure_class": OBSERVATIONAL_REPAIR_FAILURE_CLASS,
        "result_kind": "ActionChoice",
        "field": "rationale",
        "marker": MARKER,
    }


def test_repair_recovers_each_observational_field() -> None:
    outcome = repair_observational_output(RUN6_WAIT_CHOICE)
    assert outcome is not None
    result, note = outcome
    assert note["result_kind"] == "WaitChoice"
    assert note["field"] == "reason"
    assert note["marker"] == REASON_MARKER
    assert result.reason == REASON_MARKER


def test_repair_accepts_the_dispatchable_action_fixture() -> None:
    outcome = repair_observational_output(RUN6_READ_ACTION)
    assert outcome is not None
    result, note = outcome
    assert result.action_name == "read"
    assert dict(result.arguments) == {"path": "main.py"}
    assert note["marker"] == MARKER


def test_repair_never_touches_operational_defects() -> None:
    assert repair_observational_output('{"kind": "ActionChoice"}') is None
    # Unparseable anywhere: rung 1 re-walks the parse; corrupt input passes.
    assert repair_observational_output(RUN6_JOB_READ[:-1]) is None
    # Unknown fields are never stripped to manufacture a repair.
    assert repair_observational_output('{"kind": "ActionChoice", "x": 1}') is None
    # GuidanceResponse keeps decision operational even with reason present.
    assert repair_observational_output(RUN6_GUIDANCE) is None
    # A deeper type defect stays operational even when marked up.
    bad_arguments = '{"kind": "ActionChoice", "action_name": "read", "arguments": "no"}'
    assert repair_observational_output(bad_arguments) is None


# --- rung 1 end to end: decided, journaled, replayable -------------------


def test_rung1_runs_the_repaired_decision(tmp_path) -> None:
    """One provider call decides; the repaired rationale reaches the proposal."""
    transport = _ScriptedTransport((Generation(text=RUN6_READ_ACTION),))
    app = _application(tmp_path, "session-rep-run", transport)
    try:
        assert app.tick() == "decided"
        assert transport.calls == 1
        assert _events_of(app, "model_repaired") != []
        assert "provider_recovery_waiting" not in _kinds(app)
        intended = _events_of(app, "action_intended")
        assert intended
        proposal = intended[-1].payload["proposal"]
        assert proposal["action_name"] == "read"
        assert proposal["rationale"] == MARKER
        decided = _events_of(app, "model_decided")[0].payload
        assert decided["output"].strip() == RUN6_READ_ACTION
        assert decided["result_kind"] == "ActionChoice"
    finally:
        app.close()


def test_rung1_journal_replays(tmp_path) -> None:
    transport = _ScriptedTransport((Generation(text=RUN6_READ_ACTION),))
    app = _application(tmp_path, "session-rep-replay", transport)
    try:
        assert app.tick() == "decided"
        replay(app.session.events(limit=10000))
    finally:
        app.close()


# --- rung 3: the durable wait -------------------------------------------


def test_rung3_malformed_exhaustion_enters_the_durable_wait(tmp_path) -> None:
    transport = _ScriptedTransport((Generation(text="{not json"),))
    app = _application(tmp_path, "session-rep-wait", transport)
    try:
        assert app.tick() == "decided"  # the decision unit commits its failure
        assert app.view().status is SessionStatus.WAITING
        payload = _events_of(app, "provider_recovery_waiting")[0].payload
        assert payload["failure_class"] == "malformed-output"
        assert payload["operator_action"] is False
        assert payload["detail"] == (
            "malformed provider output after the repair budget; "
            "Resume re-decides, Stop ends the attempt"
        )
        assert transport.calls == 1  # malformed is never a transport retry
    finally:
        app.close()


def test_rung3_resume_redecides_after_the_wait(tmp_path) -> None:
    transport = _ScriptedTransport((Generation(text="{not json"),))
    transport.extend(_lineage_success())
    app = _application(tmp_path, "session-rep-resume", transport)
    try:
        assert app.tick() == "decided"  # the failed decision, then the wait
        reply = app.submit_frame(
            command_frame(Resume("resume-1", app.session.session_id))
        )
        assert reply.status == ACCEPTED
        assert app.tick() == "decided"
        assert app.view().status is SessionStatus.RUNNING
        assert len(_events_of(app, "provider_recovery_waiting")) == 1
        assert transport.calls == 2
    finally:
        app.close()


def test_operational_defect_exhausts_the_budget_before_the_wait(
    tmp_path,
) -> None:
    """Rung 1 rejects operational defects; the budgeted repair both fires.

    The blind repair round stays one provider call; after it, wait.
    """
    transport = _ScriptedTransport(
        (
            Generation(text='{"kind": "ActionChoice"}'),
            Generation(text='{"kind": "ActionChoice"}'),
        )
    )
    app = _application(tmp_path, "session-rep-operational", transport, repair_budget=1)
    try:
        assert app.tick() == "decided"  # the decision unit commits its failure
        assert app.view().status is SessionStatus.WAITING
        payload = _events_of(app, "provider_recovery_waiting")[0].payload
        assert payload["failure_class"] == "malformed-output"
        assert transport.calls == 2
        assert _events_of(app, "model_repaired") == []
    finally:
        app.close()


# --- rung 2: the compact pinned re-ask ----------------------------------


def test_rung2_compact_reask_repairs_one_operational_field(tmp_path) -> None:
    """One bounded re-ask adds the missing operational field and restores it."""
    transport = _ScriptedTransport(
        (
            Generation(text=OPERATIONAL_MISSING_ACTION),
            Generation(text=OPERATIONAL_REPAIRED_ACTION),
        )
    )
    app = _application(tmp_path, "session-rep-reask", transport, repair_budget=1)
    try:
        assert app.tick() == "decided"
        assert transport.calls == 2
        notes = _events_of(app, "model_repaired")
        assert len(notes) == 1
        note = notes[0].payload
        assert note["failure_class"] == "operational-field"
        assert note["result_kind"] == "ActionChoice"
        assert note["field"] == "action_name"
        assert note["marker"] == OPERATIONAL_REPAIR_MARKER
        proposal = _events_of(app, "action_intended")[-1].payload["proposal"]
        assert proposal["action_name"] == "read"
        assert dict(proposal["arguments"]) == {"path": "main.py"}
        assert "provider_recovery_waiting" not in _kinds(app)
        replay(app.session.events(limit=10000))
    finally:
        app.close()


def test_rung2_reask_does_not_resend_the_decision_context(tmp_path) -> None:
    transport = _ScriptedTransport(
        (
            Generation(text=OPERATIONAL_MISSING_ACTION),
            Generation(text=OPERATIONAL_REPAIRED_ACTION),
        )
    )
    app = _application(tmp_path, "session-rep-compact", transport, repair_budget=1)
    try:
        assert app.tick() == "decided"
        prompt = transport.prompts[1]
        assert "request_id=" in prompt
        assert OPERATIONAL_MISSING_ACTION in prompt
        assert "defect=" in prompt
        assert "fix the fixture" not in prompt
        assert "result_schemas" not in prompt
    finally:
        app.close()


def test_rung2_conflicting_repair_enters_the_wait(tmp_path) -> None:
    """A re-ask that changes a pinned field escalates instead of applying."""
    transport = _ScriptedTransport(
        (
            Generation(text=OPERATIONAL_MISSING_ACTION),
            Generation(text=OPERATIONAL_CONFLICTING_ACTION),
        )
    )
    app = _application(tmp_path, "session-rep-conflict", transport, repair_budget=1)
    try:
        assert app.tick() == "decided"
        assert app.view().status is SessionStatus.WAITING
        payload = _events_of(app, "provider_recovery_waiting")[0].payload
        assert payload["failure_class"] == "malformed-output"
        assert transport.calls == 2
        assert _events_of(app, "model_repaired") == []
        assert _events_of(app, "action_intended") == []
    finally:
        app.close()
