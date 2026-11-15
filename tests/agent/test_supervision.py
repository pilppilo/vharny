"""Deterministic progress monitor: policy, facts, triggers, and diagnostics."""

from __future__ import annotations

from typing import Mapping, Sequence

import pytest

from vharness.agent.errors import ContractError
from vharness.agent.kernel import ModelCallError, safe_model_error
from vharness.agent.models import (
    BudgetLimits,
    CommittedNode,
    CompletionMode,
    Event,
    SessionStatus,
    StateRef,
    TaskSpec,
)
from vharness.agent.supervision import (
    MONITOR_POLICY_VERSION,
    SUPERVISOR_SYSTEM_PROMPT,
    Guidance,
    ModelSupervisor,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisorDigest,
    SupervisorResponse,
    SupervisionTrigger,
    action_fingerprint,
    build_digest,
    decision_facts,
    decision_segments,
    evaluate_supervision,
    evaluation_fingerprint,
    facts_history,
    last_trigger_call,
    monitor_diagnostics,
    monitor_policy_p1,
    parse_guidance,
    receipt_fingerprint,
    render_digest,
    trigger_payload,
)
from vharness.agent.transitions import SessionState
from vharness.agent.usage_ledger import UsageLedger
from vharness.core import Generation

_SESSION = "session-monitor"
_RECORDED_AT = "2026-09-30T00:00:00+00:00"


def _event(sequence: int, kind: str, payload: Mapping[str, object]) -> Event:
    return Event(
        event_id=f"event-{sequence}",
        session_id=_SESSION,
        sequence=sequence,
        kind=kind,
        objective_version=1,
        payload=payload,
        recorded_at=_RECORDED_AT,
    )


def _decision_events(
    sequence: int,
    *,
    action: str = "inspect",
    arguments: Mapping[str, object] | None = None,
    receipt_status: str = "succeeded",
    receipt_error: str | None = None,
    proposal_id: str | None = None,
    tokens: int = 100,
    progress_claims: int = 0,
    failed_evaluations: int = 0,
) -> tuple[list[Event], int]:
    """Build one decision's events and return them with the next sequence."""
    proposal: dict[str, object] = {
        "action_name": action,
        "arguments": dict(arguments or {}),
    }
    receipt: dict[str, object] = {"status": receipt_status, "error": receipt_error}
    if proposal_id is not None:
        proposal["proposal_id"] = proposal_id
        receipt["proposal_id"] = proposal_id
    events = [
        _event(sequence, "model_intended", {"request_id": f"decision-{sequence}"}),
        _event(sequence + 1, "model_decided", {"result_kind": "ActionChoice"}),
        _event(
            sequence + 2,
            "context_assembled",
            {
                "manifest": {
                    "serialized_input_tokens": tokens,
                    "tokens_used": tokens,
                }
            },
        ),
        _event(sequence + 3, "action_intended", {"proposal": proposal}),
        _event(sequence + 4, "receipt_received", {"receipt": receipt}),
    ]
    cursor = sequence + 5
    for _ in range(progress_claims):
        events.append(
            _event(
                cursor, "progress_claim_recorded", {"claim": {"status": "supported"}}
            )
        )
        cursor += 1
    for _ in range(failed_evaluations):
        events.append(
            _event(cursor, "evaluation_received", {"disposition": "rejected"})
        )
        cursor += 1
    return events, cursor


def _build_events(
    decisions: Sequence[Mapping[str, object]],
    *,
    trigger_calls: frozenset[int] = frozenset(),
) -> list[Event]:
    """Build a journal for one run.

    A trigger at call ``N`` is recorded after decision ``N-1``, matching the
    monitor's before-the-next-decision evaluation point.
    """
    events: list[Event] = []
    cursor = 1
    for index, spec in enumerate(decisions, start=1):
        decision, cursor = _decision_events(cursor, **spec)
        events.extend(decision)
        if index + 1 in trigger_calls:
            cursor += 1
            events.append(
                _event(
                    cursor,
                    "supervision_triggered",
                    {
                        "call": index + 1,
                        "policy_version": MONITOR_POLICY_VERSION,
                        "cause": "repetition",
                    },
                )
            )
    return events


def _repeat_run(repeats: int, *, action: str = "inspect") -> list[Event]:
    return _build_events([{"action": action} for _ in range(repeats)])


def test_action_fingerprint_normalizes_name_and_arguments() -> None:
    first = action_fingerprint("Inspect", {"path": "main.py"})
    second = action_fingerprint("  inspect ", {"path": "main.py"})
    assert first == second
    different_args = action_fingerprint("inspect", {"path": "other.py"})
    different_name = action_fingerprint("read", {"path": "main.py"})
    assert first != different_args
    assert first != different_name


def test_receipt_and_evaluation_fingerprints_are_stable() -> None:
    assert receipt_fingerprint("failed", "boom") == receipt_fingerprint(
        "failed", "boom"
    )
    assert receipt_fingerprint("failed", "boom") != receipt_fingerprint(
        "failed", "bang"
    )
    assert evaluation_fingerprint("rejected") == evaluation_fingerprint("rejected")
    assert evaluation_fingerprint("rejected") != evaluation_fingerprint("accepted")


def test_budget_rejected_action_decisions_feed_the_repetition_monitor() -> None:
    events: list[Event] = []
    for call in range(1, 4):
        request_id = f"decision-{call}"
        events.extend(
            (
                _event(call * 10, "model_intended", {"request_id": request_id}),
                _event(
                    call * 10 + 1,
                    "model_decided",
                    {
                        "request_id": request_id,
                        "result_kind": "ActionChoice",
                        "output": (
                            '{"kind":"ActionChoice","action_name":"inspect",'
                            '"arguments":{"path":"main.py"}}'
                        ),
                    },
                ),
                _event(
                    call * 10 + 2,
                    "model_application_rejected",
                    {
                        "request_id": request_id,
                        "reason": "resource budget exhausted",
                    },
                ),
            )
        )

    trigger = evaluate_supervision(
        events, 4, MonitorPolicy(window_calls=8, repeat_threshold=3, cooldown_calls=4)
    )

    assert trigger is not None
    assert trigger.fingerprint == action_fingerprint("inspect", {"path": "main.py"})


def test_budget_rejected_evaluations_feed_the_repetition_monitor() -> None:
    events: list[Event] = []
    for call in range(1, 4):
        request_id = f"decision-{call}"
        events.extend(
            (
                _event(call * 10, "model_intended", {"request_id": request_id}),
                _event(
                    call * 10 + 1,
                    "model_decided",
                    {"request_id": request_id, "result_kind": "EvaluationChoice"},
                ),
                _event(
                    call * 10 + 2,
                    "model_application_rejected",
                    {
                        "request_id": request_id,
                        "reason": "resource budget exhausted",
                    },
                ),
            )
        )

    trigger = evaluate_supervision(
        events, 4, MonitorPolicy(window_calls=8, repeat_threshold=3, cooldown_calls=4)
    )

    assert trigger is not None
    assert trigger.fingerprint == evaluation_fingerprint("budget exhausted")


def test_decision_facts_counts_actions_failures_tokens_and_progress() -> None:
    events, _ = _decision_events(
        1,
        action="inspect",
        arguments={"path": "main.py"},
        receipt_status="failed",
        receipt_error="boom",
        tokens=250,
        progress_claims=1,
    )
    facts = decision_facts(events, call=1)
    assert facts.call == 1
    assert facts.actions == 1
    assert facts.failed_receipts == 1
    assert facts.input_tokens == 250
    assert facts.supported_progress == 1
    assert len(facts.fingerprints) == 2


def test_decision_facts_ignores_pending_claims_as_progress() -> None:
    events = [
        _event(1, "progress_claim_recorded", {"claim": {"status": "pending"}}),
        _event(2, "knowledge_recorded", {"item": {"status": "supported"}}),
    ]
    facts = decision_facts(events, call=1)
    assert facts.supported_progress == 1


def test_facts_history_segments_by_model_intended() -> None:
    events = _build_events([{"action": "a"}, {"action": "b"}, {"action": "c"}])
    history = facts_history(events)
    assert [fact.call for fact in history] == [1, 2, 3]
    assert [fact.actions for fact in history] == [1, 1, 1]
    segments = decision_segments(events)
    assert len(segments) == 3


def test_facts_history_ignores_events_before_the_first_model_call() -> None:
    leading = [
        _event(1, "session_created", {"run_identity": {}}),
        _event(2, "attempt_started", {"attempt": 1}),
        _event(3, "context_assembled", {"manifest": {"tokens_used": 10}}),
    ]
    first, cursor = _decision_events(4, action="inspect")
    second, _ = _decision_events(cursor, action="inspect")
    events = leading + first + second

    segments = decision_segments(events)
    assert len(segments) == 2
    assert all(segment[0].kind == "model_intended" for segment in segments)
    assert [fact.call for fact in facts_history(events)] == [1, 2]
    assert (
        evaluate_supervision(tuple(leading), call=1, policy=monitor_policy_p1()) is None
    )


def test_repetition_triggers_at_threshold() -> None:
    events = _repeat_run(3)
    trigger = evaluate_supervision(events, call=4)
    assert trigger is not None
    assert trigger.policy_version == MONITOR_POLICY_VERSION
    assert trigger.call == 4
    assert trigger.cause == "repetition"
    assert trigger.repeat_count == 3
    assert trigger.fingerprint == action_fingerprint("inspect", {})
    assert trigger.actions_used == 3
    assert trigger.tokens_used == 300


def test_no_trigger_below_threshold() -> None:
    events = _repeat_run(2)
    assert evaluate_supervision(events, call=3, policy=monitor_policy_p1()) is None


def test_no_trigger_on_first_decision() -> None:
    assert evaluate_supervision([], call=1, policy=monitor_policy_p1()) is None


def test_no_trigger_without_repetition() -> None:
    events = _build_events([{"action": f"action-{i}"} for i in range(8)])
    assert evaluate_supervision(events, call=9, policy=monitor_policy_p1()) is None


def test_window_ignores_stale_repetitions() -> None:
    decisions = [{"action": "a"}, {"action": "a"}, {"action": "a"}]
    decisions += [{"action": f"distinct-{i}"} for i in range(7)]
    events = _build_events(decisions)
    assert evaluate_supervision(events, call=11, policy=monitor_policy_p1()) is None


def _events_for_decisions(
    count: int, *, action: str = "a", trigger_calls: frozenset[int] = frozenset()
) -> list[Event]:
    return _build_events(
        [{"action": action} for _ in range(count)], trigger_calls=trigger_calls
    )


def test_cooldown_suppresses_repeat_triggers() -> None:
    events = _events_for_decisions(4, trigger_calls=frozenset({4}))
    assert last_trigger_call(events) == 4
    for call in (5, 6, 7):
        events = _events_for_decisions(call - 1, trigger_calls=frozenset({4}))
        assert evaluate_supervision(events, call=call) is None
    events = _events_for_decisions(7, trigger_calls=frozenset({4}))
    trigger = evaluate_supervision(events, call=8)
    assert trigger is not None
    assert trigger.call == 8


def test_trigger_records_window_event_ids() -> None:
    events = _repeat_run(3)
    trigger = evaluate_supervision(events, call=4)
    assert trigger is not None
    window_ids = {
        event.event_id
        for segment in decision_segments(events)[-8:]
        for event in segment
    }
    assert set(trigger.input_event_ids) <= window_ids
    assert trigger.input_event_ids


def test_failed_receipts_form_repetition_signal() -> None:
    events = _build_events(
        [
            {"action": "a", "receipt_status": "failed", "receipt_error": "boom"},
            {"action": "b", "receipt_status": "failed", "receipt_error": "boom"},
            {"action": "c", "receipt_status": "failed", "receipt_error": "boom"},
        ]
    )
    trigger = evaluate_supervision(events, call=4)
    assert trigger is not None
    assert trigger.cause == "repetition"
    assert trigger.fingerprint == receipt_fingerprint("failed", "boom")


def test_recovery_events_do_not_reset_stagnation() -> None:
    events = _repeat_run(3)
    events.append(_event(99, "operation_reconciled", {"operation_id": "op-1"}))
    trigger = evaluate_supervision(events, call=4)
    assert trigger is not None


def test_diagnostics_distinguish_cycles_from_plateaus() -> None:
    cycles = monitor_diagnostics(_repeat_run(6))
    assert cycles.decisions == 6
    assert cycles.distinct_fingerprints == 1
    assert cycles.repetition_ratio > 0.8
    assert cycles.supported_progress == 0

    plateau = monitor_diagnostics(
        _build_events(
            [{"action": f"action-{i}", "progress_claims": 1} for i in range(6)]
        )
    )
    assert plateau.decisions == 6
    assert plateau.distinct_fingerprints == 6
    assert plateau.repetition_ratio == 0.0
    assert plateau.supported_progress == 6


def test_diagnostics_report_last_trigger_call() -> None:
    events = _build_events(
        [{"action": "a"} for _ in range(3)],
        trigger_calls=frozenset({4}),
    )
    diagnostics = monitor_diagnostics(events)
    assert diagnostics.last_trigger_call == 4


def test_trigger_payload_is_durable() -> None:
    events = _repeat_run(3)
    trigger = evaluate_supervision(events, call=4)
    assert trigger is not None
    payload = trigger_payload(trigger)
    assert payload["policy_version"] == MONITOR_POLICY_VERSION
    assert payload["call"] == 4
    assert payload["cause"] == "repetition"
    assert payload["repeat_count"] == 3
    assert len(payload["input_event_ids"]) == len(trigger.input_event_ids)


def test_policy_validation_rejects_invalid_values() -> None:
    with pytest.raises(ContractError):
        MonitorPolicy(window_calls=0)
    with pytest.raises(ContractError):
        MonitorPolicy(repeat_threshold=0)
    with pytest.raises(ContractError):
        MonitorPolicy(cooldown_calls=0)
    with pytest.raises(ContractError):
        MonitorPolicy(version=" ")


def test_evaluate_supervision_validates_call_alignment() -> None:
    events = _repeat_run(3)
    with pytest.raises(ContractError):
        evaluate_supervision(events, call=6)
    with pytest.raises(ContractError):
        evaluate_supervision([], call=2)


def test_custom_policy_parameters_are_honored() -> None:
    policy = MonitorPolicy(window_calls=3, repeat_threshold=2, cooldown_calls=1)
    events = _repeat_run(2)
    trigger = evaluate_supervision(events, call=3, policy=policy)
    assert trigger is not None
    assert trigger.repeat_count == 2


def _state() -> SessionState:
    return SessionState(
        task=TaskSpec(
            task_id="task-1",
            objective="fix the fixture",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=8, evaluations=2, model_calls=12),
            environment_id="fake",
            evaluation_contract_id="fake-evaluator-v1",
        ),
        status=SessionStatus.RUNNING,
        lineage_head=CommittedNode(
            node_id="node-root",
            objective_version=1,
            state=StateRef("fake", "initial", digest="0" * 64),
        ),
        ledger=UsageLedger(),
    )


def _trigger(call: int = 4) -> SupervisionTrigger:
    return SupervisionTrigger(
        policy_version=MONITOR_POLICY_VERSION,
        call=call,
        cause="repetition",
        fingerprint="action:inspect:{}",
        repeat_count=3,
        actions_used=3,
        tokens_used=300,
        input_event_ids=("event-1", "event-2"),
    )


def test_guidance_requires_diagnosis_and_positive_expiry() -> None:
    with pytest.raises(ContractError):
        Guidance(
            diagnosis=" ",
            constraints=(),
            suggested_experiments=(),
            expires_after_call=3,
        )
    with pytest.raises(ContractError):
        Guidance(
            diagnosis="diagnose",
            constraints=(),
            suggested_experiments=(),
            expires_after_call=0,
        )


def test_build_digest_carries_bounded_signals() -> None:
    events = _build_events(
        [
            {"action": "a", "receipt_status": "failed", "receipt_error": "boom"},
            {"action": "b", "receipt_status": "failed", "receipt_error": "boom"},
            {"action": "c", "receipt_status": "failed", "receipt_error": "boom"},
        ]
    )
    digest = build_digest(_state(), events, _trigger())
    assert isinstance(digest, SupervisorDigest)
    assert digest.objective == "fix the fixture"
    assert digest.attempt_count == 0
    assert digest.committed_objectives == ()
    assert receipt_fingerprint("failed", "boom") in digest.recent_failures
    assert digest.unexplored_directions == ()
    assert digest.trigger.call == 4


def test_build_digest_respects_bounds() -> None:
    decisions = [
        {"action": f"a{i}", "receipt_status": "failed", "receipt_error": f"e{i}"}
        for i in range(15)
    ]
    events = _build_events(decisions)
    digest = build_digest(
        _state(), events, _trigger(), max_failures=3, max_directions=2
    )
    assert len(digest.recent_failures) == 3
    assert len(digest.unexplored_directions) <= 2


def test_build_digest_excludes_action_fingerprints_from_failures() -> None:
    events = _build_events(
        [
            {"action": "a", "receipt_status": "failed", "receipt_error": "boom"},
            {"action": "b", "receipt_status": "failed", "receipt_error": "boom"},
        ]
    )
    digest = build_digest(_state(), events, _trigger())
    assert action_fingerprint("a", {}) not in digest.recent_failures
    assert action_fingerprint("b", {}) not in digest.recent_failures


def test_build_digest_carries_receipted_effects_without_content() -> None:
    events, _ = _decision_events(
        1,
        action="write",
        arguments={"path": "main.py", "content": "top secret"},
        proposal_id="proposal-1",
    )
    digest = build_digest(_state(), events, _trigger())
    assert digest.recent_effects == ("write path=main.py",)
    rendered = render_digest(digest)
    assert "Receipted durable effects:" in rendered
    assert "- write path=main.py" in rendered
    assert "top secret" not in rendered


def test_build_digest_omits_failed_and_unreceipted_effects() -> None:
    failed, _ = _decision_events(
        1,
        action="write",
        arguments={"path": "main.py"},
        receipt_status="failed",
        receipt_error="boom",
        proposal_id="proposal-1",
    )
    unreceipted = [
        _event(
            9,
            "action_intended",
            {
                "proposal": {
                    "proposal_id": "proposal-2",
                    "action_name": "read",
                    "arguments": {"path": "notes.md"},
                }
            },
        )
    ]
    digest = build_digest(_state(), failed + unreceipted, _trigger())
    assert digest.recent_effects == ()


def test_build_digest_bounds_receipted_effects() -> None:
    events: list[Event] = []
    cursor = 1
    for index in range(15):
        chunk, cursor = _decision_events(
            cursor,
            action="read",
            arguments={"path": f"file-{index}.txt"},
            proposal_id=f"proposal-{index}",
        )
        events.extend(chunk)
    digest = build_digest(_state(), events, _trigger(), max_effects=3)
    assert digest.recent_effects == (
        "read path=file-12.txt",
        "read path=file-13.txt",
        "read path=file-14.txt",
    )


def test_scripted_supervisor_yields_guidance_in_order() -> None:
    first = Guidance("first", ("c1",), ("e1",), expires_after_call=5)
    second = Guidance("second", (), ("e2", "e3"), expires_after_call=6)
    supervisor = ScriptedSupervisor(
        (
            SupervisorResponse(first, None, "scripted"),
            SupervisorResponse(second, None, "scripted"),
        )
    )
    digest = build_digest(_state(), [], _trigger())
    assert supervisor.advise(digest).guidance == first
    assert supervisor.advise(digest).guidance == second
    assert supervisor.calls == 2
    assert len(supervisor.digests) == 2


def test_scripted_supervisor_raises_when_exhausted() -> None:
    supervisor = ScriptedSupervisor(())
    with pytest.raises(ContractError):
        supervisor.advise(build_digest(_state(), [], _trigger()))


def test_render_digest_contains_objective_and_trigger() -> None:
    events = _build_events(
        [
            {"action": "a", "receipt_status": "failed", "receipt_error": "boom"},
            {"action": "b", "receipt_status": "failed", "receipt_error": "boom"},
        ]
    )
    digest = build_digest(_state(), events, _trigger())
    rendered = render_digest(digest)
    assert "fix the fixture" in rendered
    assert "repetition" in rendered
    assert receipt_fingerprint("failed", "boom") in rendered


def test_parse_guidance_valid() -> None:
    guidance = parse_guidance(
        '{"kind": "Guidance", "diagnosis": "looping", '
        '"constraints": ["no tools"], "suggested_experiments": ["replan"], '
        '"expires_after_call": 6}'
    )
    assert guidance.diagnosis == "looping"
    assert guidance.constraints == ("no tools",)
    assert guidance.suggested_experiments == ("replan",)
    assert guidance.expires_after_call == 6


def test_parse_guidance_rejects_invalid_kind() -> None:
    with pytest.raises(ContractError):
        parse_guidance('{"kind": "ActionChoice", "action_name": "x", "rationale": "y"}')


def test_parse_guidance_rejects_invalid_expiry() -> None:
    with pytest.raises(ContractError):
        parse_guidance(
            '{"kind": "Guidance", "diagnosis": "x", "constraints": [], '
            '"suggested_experiments": [], "expires_after_call": 0}'
        )


def test_parse_guidance_filters_non_string_items() -> None:
    guidance = parse_guidance(
        '{"kind": "Guidance", "diagnosis": "x", "constraints": ["a", 1, null], '
        '"suggested_experiments": [], "expires_after_call": 3}'
    )
    assert guidance.constraints == ("a",)


class _FakeGenerator:
    """Minimal text generator returning one pre-built generation."""

    def __init__(self, generation: Generation) -> None:
        self._generation = generation
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, prompt: str) -> Generation:
        self.calls.append((system, prompt))
        return self._generation


def _digest() -> SupervisorDigest:
    return SupervisorDigest(
        objective="fix the fixture",
        attempt_count=1,
        committed_objectives=(),
        recent_failures=("receipt:failed:boom",),
        recent_effects=("write path=main.py",),
        unexplored_directions=("check the parser",),
        trigger=_trigger(),
    )


def _guidance_text() -> str:
    return (
        '{"kind": "Guidance", "diagnosis": "looping on the same write", '
        '"constraints": ["do not rewrite main.py"], '
        '"suggested_experiments": ["read the last receipt"], '
        '"expires_after_call": 6}'
    )


def test_model_supervisor_parses_guidance_and_reports_usage() -> None:
    generator = _FakeGenerator(
        Generation(
            text=_guidance_text(),
            model="longcat-supervisor",
            finish_reason="stop",
            prompt_tokens=41,
            completion_tokens=9,
        )
    )
    supervisor = ModelSupervisor(generator, model_id="fallback-model")

    response = supervisor.advise(_digest())

    assert response.guidance.diagnosis == "looping on the same write"
    assert response.guidance.expires_after_call == 6
    assert response.model_id == "longcat-supervisor"
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.model_calls == 1
    assert response.usage.input_tokens == 41
    assert response.usage.output_tokens == 9
    assert generator.calls and "Objective: fix the fixture" in generator.calls[0][1]
    assert "Use only evidence in this digest" in generator.calls[0][1]
    assert generator.calls[0][0] == SUPERVISOR_SYSTEM_PROMPT


def test_model_supervisor_raises_on_provider_error() -> None:
    generator = _FakeGenerator(
        Generation(text="", error="provider transport failure", prompt_tokens=17)
    )
    with pytest.raises(ModelCallError) as raised:
        ModelSupervisor(generator, model_id="m").advise(_digest())
    assert raised.value.usage is not None
    assert raised.value.usage.model_calls == 1
    assert raised.value.usage.input_tokens == 17
    assert raised.value.usage.output_tokens is None


def test_model_supervisor_raises_on_malformed_guidance() -> None:
    generator = _FakeGenerator(
        Generation(text="not a JSON object", prompt_tokens=17, completion_tokens=5)
    )
    with pytest.raises(ModelCallError) as raised:
        ModelSupervisor(generator, model_id="m").advise(_digest())
    assert raised.value.usage is not None
    assert raised.value.usage.model_calls == 1
    assert raised.value.usage.input_tokens == 17
    assert raised.value.usage.output_tokens == 5


@pytest.mark.parametrize(("text", "finish_reason"), (("", "stop"), ("{", "length")))
def test_model_supervisor_classifies_provider_output_exhaustion(
    text: str, finish_reason: str
) -> None:
    generator = _FakeGenerator(
        Generation(
            text=text,
            finish_reason=finish_reason,
            prompt_tokens=17,
            completion_tokens=5,
        )
    )
    with pytest.raises(ModelCallError) as raised:
        ModelSupervisor(generator, model_id="m").advise(_digest())
    assert safe_model_error(raised.value) == "provider output exhausted"
    assert raised.value.usage is not None
    assert raised.value.usage.model_calls == 1


def test_model_supervisor_rejects_empty_model_id() -> None:
    with pytest.raises(ContractError):
        ModelSupervisor(_FakeGenerator(Generation(text="{}")), model_id="  ")
