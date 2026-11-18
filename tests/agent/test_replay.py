"""Additive operational evidence remains replay-valid and strictly typed."""

from dataclasses import replace
from pathlib import Path

import pytest

from vharness.agent.errors import IntegrityError
from vharness.agent.evidence import OPERATIONAL_MEASUREMENT_SCOPE
from vharness.agent.models import SessionStatus
from vharness.agent.replay import replay
from vharness.agent.supervision import BURN_CAUSE, CHECKPOINT_CAUSE
from tools.phase3_paired_recovery import _read_events

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "evidence/phase-0003-paired-recovery/runs/recovery-drill/s0/supervised"


def _read_event(events, payload):
    template = events[-1]
    return replace(
        template,
        event_id="event-artifact-read-test",
        sequence=template.sequence + 1,
        kind="artifact_read",
        payload=payload,
    )


def _appended_event(events, kind, payload):
    template = events[-1]
    return replace(
        template,
        event_id="event-supervision-cause-test",
        sequence=template.sequence + 1,
        kind=kind,
        payload=payload,
    )


def _burn_trigger_payload(**overrides):
    payload = {
        "policy_version": "monitor.p5.v1",
        "call": 5,
        "cause": BURN_CAUSE,
        "fingerprint": None,
        "repeat_count": None,
        "actions_used": 4,
        "tokens_used": 400,
        "input_event_ids": ["event-burn-input-1", "event-burn-input-2"],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "resource", ["action", "model call", "evaluation", "token", "time", "cost"]
)
def test_replay_records_budget_exhaustion(resource: str) -> None:
    events = _read_events(RUN / "events.jsonl.gz")[:1]
    stop = _appended_event(
        events,
        "budget_exhausted",
        {"resource": resource, "reason": f"{resource} budget exhausted"},
    )
    assert replay((*events, stop)).status is SessionStatus.STOPPED


def test_replay_accepts_measured_artifact_read_without_changing_projection() -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    original_state = replay(events)
    read_event = _read_event(
        events,
        {
            "scope": OPERATIONAL_MEASUREMENT_SCOPE,
            "purpose": "evaluation_evidence",
            "digest": "a" * 64,
            "bytes_read": 12,
        },
    )

    assert replay((*events, read_event)) == original_state


@pytest.mark.parametrize(
    "payload",
    [
        {
            "scope": OPERATIONAL_MEASUREMENT_SCOPE,
            "purpose": "evaluation_evidence",
            "digest": "a" * 64,
            "bytes_read": True,
        },
        {
            "scope": OPERATIONAL_MEASUREMENT_SCOPE,
            "purpose": [],
            "digest": "a" * 64,
            "bytes_read": 12,
        },
    ],
)
def test_replay_rejects_malformed_artifact_read(payload) -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    with pytest.raises(IntegrityError):
        replay((*events, _read_event(events, payload)))


def test_replay_rejects_invalid_context_duration() -> None:
    events = list(_read_events(RUN / "events.jsonl.gz"))
    index = next(
        i for i, event in enumerate(events) if event.kind == "context_assembled"
    )
    event = events[index]
    events[index] = replace(
        event,
        payload={**event.payload, "duration_ns": True},
    )

    with pytest.raises(IntegrityError, match="context duration_ns"):
        replay(events)


def test_replay_accepts_the_burn_cause_on_trigger_and_escalation() -> None:
    """The P5 budget-burn payloads persist and replay without a projection change.

    The Timing attempt-3 acceptance run died at this validation: the trigger
    validator accepted only the repetition cause, so the live persist of the
    burn-caused trigger raised IntegrityError and killed the runner.
    """
    events = _read_events(RUN / "events.jsonl.gz")
    original_state = replay(events)
    trigger = _appended_event(events, "supervision_triggered", _burn_trigger_payload())
    escalation = _appended_event(
        events,
        "operator_escalated",
        {"stage": 3, "call": 9, "cause": BURN_CAUSE, "fingerprint": None},
    )

    assert replay((*events, trigger)) == original_state
    assert replay((*events, escalation)) == original_state


@pytest.mark.parametrize(
    "payload",
    [
        _burn_trigger_payload(cause="boredom"),
        _burn_trigger_payload(repeat_count=3),
        _burn_trigger_payload(fingerprint="stdout-strict:" + "a" * 64),
        _burn_trigger_payload(cause="repetition"),
        _burn_trigger_payload(cause="repetition", repeat_count=-1),
        _burn_trigger_payload(cause=["repetition"]),
    ],
)
def test_replay_rejects_malformed_supervision_trigger_cause(payload) -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    with pytest.raises(IntegrityError, match="supervision_triggered"):
        replay((*events, _appended_event(events, "supervision_triggered", payload)))


def _checkpoint_trigger_payload(**overrides):
    payload = {
        "policy_version": "monitor.p6.v1",
        "call": 7,
        "cause": CHECKPOINT_CAUSE,
        "fingerprint": None,
        "repeat_count": None,
        "actions_used": 6,
        "tokens_used": 600,
        "input_event_ids": ["event-checkpoint-input-1", "event-checkpoint-input-2"],
        "consumed_calls": 6,
        "threshold": 6,
        "progress_anchor": 0,
    }
    payload.update(overrides)
    return payload


def test_replay_accepts_the_budget_checkpoint_cause() -> None:
    """The ADR-0013 checkpoint payloads persist and replay unchanged.

    The trigger journals the settled ledger state it fired on; replay accepts
    the cause and reproduces its due-ness from the recorded consumed calls,
    threshold, and anchor without changing any projection.
    """
    events = _read_events(RUN / "events.jsonl.gz")
    original_state = replay(events)
    trigger = _appended_event(
        events, "supervision_triggered", _checkpoint_trigger_payload()
    )
    late_anchor = _appended_event(
        events,
        "supervision_triggered",
        _checkpoint_trigger_payload(consumed_calls=9, threshold=6, progress_anchor=3),
    )
    escalation = _appended_event(
        events,
        "operator_escalated",
        {"stage": 3, "call": 9, "cause": CHECKPOINT_CAUSE, "fingerprint": None},
    )

    assert replay((*events, trigger)) == original_state
    assert replay((*events, late_anchor)) == original_state
    assert replay((*events, escalation)) == original_state


@pytest.mark.parametrize(
    "payload",
    [
        _checkpoint_trigger_payload(consumed_calls=None),
        _checkpoint_trigger_payload(threshold=None),
        _checkpoint_trigger_payload(progress_anchor=None),
        _checkpoint_trigger_payload(consumed_calls=5),
        _checkpoint_trigger_payload(threshold=0),
        _checkpoint_trigger_payload(consumed_calls=-1),
        _checkpoint_trigger_payload(fingerprint="stdout-strict:" + "a" * 64),
        _checkpoint_trigger_payload(repeat_count=2),
        _burn_trigger_payload(consumed_calls=6),
        _burn_trigger_payload(cause="repetition", consumed_calls=6),
    ],
)
def test_replay_rejects_malformed_budget_checkpoint_trigger(payload) -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    with pytest.raises(IntegrityError, match="supervision_triggered"):
        replay((*events, _appended_event(events, "supervision_triggered", payload)))


def test_replay_rejects_malformed_escalation_cause() -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    payload = {"stage": 3, "call": 9, "cause": "boredom", "fingerprint": None}
    with pytest.raises(IntegrityError, match="operator_escalated"):
        replay((*events, _appended_event(events, "operator_escalated", payload)))


def test_replay_accepts_a_reasoned_below_ladder_escalation() -> None:
    """ADR-0013 stage-1 no-capacity escalations replay with their reason.

    The boundary exposes a checkpoint the remaining budget cannot review as
    a blocking operator question; the payload carries the bounded reason and
    replays without changing any projection.
    """
    events = _read_events(RUN / "events.jsonl.gz")
    original_state = replay(events)
    escalation = _appended_event(
        events,
        "operator_escalated",
        {
            "stage": 1,
            "call": 9,
            "cause": CHECKPOINT_CAUSE,
            "fingerprint": None,
            "reason": "no supervisor call fits the remaining model-call budget",
        },
    )
    assert replay((*events, escalation)) == original_state


@pytest.mark.parametrize(
    "payload",
    [
        {"stage": 1, "call": 9, "cause": CHECKPOINT_CAUSE, "fingerprint": None},
        {"stage": 0, "call": 9, "cause": CHECKPOINT_CAUSE, "fingerprint": None},
        {
            "stage": 1,
            "call": 9,
            "cause": CHECKPOINT_CAUSE,
            "fingerprint": None,
            "reason": "",
        },
    ],
)
def test_replay_rejects_malformed_escalation_stage_or_reason(payload) -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    with pytest.raises(IntegrityError, match="operator_escalated"):
        replay((*events, _appended_event(events, "operator_escalated", payload)))


def _checkpoint_suppressed_payload(**overrides):
    payload = {
        "policy_version": "monitor.p6.v1",
        "call": 7,
        "cause": CHECKPOINT_CAUSE,
        "consumed_calls": 6,
        "threshold": 6,
        "progress_anchor": 0,
        "previous_trigger_call": 6,
        "cooldown_calls": 4,
        "input_event_ids": ["event-checkpoint-input-1"],
    }
    payload.update(overrides)
    return payload


def test_replay_accepts_the_suppressed_checkpoint_disposition() -> None:
    """ADR-0013: due-but-suppressed checkpoint dispositions persist and replay.

    The event changes no projection and reproduces both recorded conditions:
    the checkpoint was due on the settled ledger, and the boundary sat inside
    the shared repetition cooldown.
    """
    events = _read_events(RUN / "events.jsonl.gz")
    original_state = replay(events)
    suppressed = _appended_event(
        events, "checkpoint_suppressed", _checkpoint_suppressed_payload()
    )
    assert replay((*events, suppressed)) == original_state


@pytest.mark.parametrize(
    "payload",
    [
        _checkpoint_suppressed_payload(cause="repetition"),
        _checkpoint_suppressed_payload(consumed_calls=5),
        _checkpoint_suppressed_payload(threshold=0),
        _checkpoint_suppressed_payload(consumed_calls=-1),
        _checkpoint_suppressed_payload(previous_trigger_call=0),
        _checkpoint_suppressed_payload(cooldown_calls=0),
        _checkpoint_suppressed_payload(cooldown_calls=1),
        _checkpoint_suppressed_payload(previous_trigger_call=None),
        _checkpoint_suppressed_payload(policy_version=""),
        _checkpoint_suppressed_payload(input_event_ids=[3]),
    ],
)
def test_replay_rejects_malformed_suppressed_checkpoint(payload) -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    with pytest.raises(IntegrityError, match="checkpoint_suppressed"):
        replay((*events, _appended_event(events, "checkpoint_suppressed", payload)))
