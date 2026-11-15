"""Durable contract tests for the operator evaluation decision command."""

from __future__ import annotations

import pytest

from vharness.agent.codec import to_json_value
from vharness.agent.errors import ContractError, IntegrityError
from vharness.agent.models import ArtifactRef, EvaluationDecision
from vharness.agent.session_state import command_from_payload


def _evidence() -> ArtifactRef:
    return ArtifactRef("a" * 64, 12, "text/plain", "operator")


def test_evaluation_decision_round_trips_through_the_durable_decoder():
    decision = EvaluationDecision(
        "decision-1",
        "session-1",
        "evaluation-1",
        accepted=True,
        reason="checker passed",
        completed=True,
        evidence_ref=_evidence(),
    )
    decoded = command_from_payload(to_json_value(decision))
    assert decoded == decision
    assert isinstance(decoded, EvaluationDecision)


def test_evaluation_decision_decodes_a_milestone_with_evidence():
    decision = EvaluationDecision(
        "decision-2",
        "session-1",
        "evaluation-1",
        accepted=True,
        reason="partial progress",
        evidence_ref=_evidence(),
    )
    decoded = command_from_payload(to_json_value(decision))
    assert decoded == decision
    assert decoded.evidence_ref == _evidence()
    assert decoded.completed is False


@pytest.mark.parametrize("completed", (False, True))
def test_accepted_decision_requires_evidence_for_milestone_and_completion(
    completed,
):
    with pytest.raises(ContractError, match="requires evidence"):
        EvaluationDecision(
            "decision-missing-evidence",
            "session-1",
            "evaluation-1",
            accepted=True,
            reason="accepted without evidence",
            completed=completed,
        )


def test_rejected_decision_may_omit_evidence():
    decision = EvaluationDecision(
        "decision-rejected-without-evidence",
        "session-1",
        "evaluation-1",
        accepted=False,
        reason="not done",
    )
    assert decision.evidence_ref is None


def test_rejected_decision_cannot_complete():
    with pytest.raises(ContractError, match="cannot complete"):
        EvaluationDecision(
            "decision-3",
            "session-1",
            "evaluation-1",
            accepted=False,
            reason="not done",
            completed=True,
        )


def test_decoder_rejects_non_boolean_acceptance():
    payload = dict(
        to_json_value(
            EvaluationDecision(
                "decision-4",
                "session-1",
                "evaluation-1",
                accepted=True,
                reason="ok",
                evidence_ref=_evidence(),
            )
        )
    )
    payload["accepted"] = "yes"
    with pytest.raises(IntegrityError, match="accepted must be a boolean"):
        command_from_payload(payload)
