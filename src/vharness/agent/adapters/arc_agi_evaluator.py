"""ARC-AGI-3 evaluator: acceptance follows the official scorecard alone (COD-37).

Each evaluation reads the scorecard through a timed call, stores the raw
scorecard (secrets removed) as an artifact, and copies the official measures
into the receipt. Nothing is recomputed.

* **Accepted** only when the scorecard marks the game completed. The model's
  own claims, the frame the runtime last saw and the requested state play no
  part in the verdict.
* **Not evaluated** when the scorecard is missing, unreachable, times out or
  is malformed. The receipt is invalid (``valid=False``), which the kernel
  treats as INVALID, never as a pass, and its objectives give the reason.
* Anything short of completion is a valid, rejected receipt that reports the
  official progress (levels, actions, score).
"""

from __future__ import annotations

import hashlib

from ..artifacts import ArtifactStore
from ..codec import canonical_json
from ..errors import ContractError
from ..models import (
    ArtifactRef,
    ComponentIdentity,
    EvaluationReceipt,
    EvaluationRequest,
    JsonValue,
    UsageMeasurement,
    frozen_mapping,
)
from ..ports import EvaluationReconciliationResult, ReconciliationKind
from .arc_agi_scorecard import ArcScorecard, redact_scorecard
from .arc_agi_toolkit import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    ArcCallBusy,
    ArcCallError,
    ArcScorecardSource,
    TimedCaller,
)

ARC_EVALUATOR_ID = "arc-agi-3-scorecard-evaluator"
#: Bumped when receipt or objective shapes change.
ARC_EVALUATOR_VERSION = "1"
NOT_EVALUATED = "not evaluated"
COMPLETED = "completed"
INCOMPLETE = "incomplete"


class ArcScorecardEvaluator:
    """Evaluator port whose only authority is the official scorecard."""

    def __init__(
        self,
        source: ArcScorecardSource,
        artifacts: ArtifactStore,
        *,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._source = source
        self._info = source.info
        self._artifacts = artifacts
        self._caller = TimedCaller(call_timeout_seconds)
        self._receipts: dict[str, EvaluationReceipt] = {}

    def identity(self) -> ComponentIdentity:
        """Pin the evaluator shape, the toolkit setup and the call timeout."""
        configuration = canonical_json(
            {
                "evaluator_version": ARC_EVALUATOR_VERSION,
                "toolkit": dict(self._info.to_json()),
                "call_timeout_seconds": self._caller.timeout_seconds,
            }
        )
        return ComponentIdentity(
            ARC_EVALUATOR_ID,
            self._info.toolkit_version,
            configuration_digest=hashlib.sha256(
                configuration.encode("utf-8")
            ).hexdigest(),
        )

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Read the scorecard and report it; never a pass without one."""
        try:
            raw = self._caller.call("get_scorecard", self._source.scorecard)
        except (ArcCallError, ArcCallBusy) as exc:
            return self._remember(_not_evaluated(request, str(exc)))
        if raw is None:
            return self._remember(
                _not_evaluated(request, "the ARC toolkit has no scorecard")
            )
        try:
            redacted = redact_scorecard(raw)
        except ContractError as exc:
            reason = f"the scorecard is malformed: {exc}"
            return self._remember(_not_evaluated(request, reason))
        stored = self._store(redacted)
        try:
            card = ArcScorecard.from_json(redacted, self._info.game_id)
        except ContractError as exc:
            reason = f"the scorecard is malformed: {exc}"
            return self._remember(_not_evaluated(request, reason, stored))
        return self._remember(_scored(request, card, stored))

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """Return a receipt this evaluator produced, or UNKNOWN."""
        receipt = self._receipts.get(request_id)
        if receipt is None:
            return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)
        return EvaluationReconciliationResult(ReconciliationKind.RECEIPT, receipt)

    def _remember(self, receipt: EvaluationReceipt) -> EvaluationReceipt:
        self._receipts[receipt.request_id] = receipt
        return receipt

    def _store(self, scorecard: JsonValue) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json(scorecard).encode("utf-8"),
            media_type="application/json",
            provenance="arc-agi-3-scorecard",
        )


def _scored(
    request: EvaluationRequest, card: ArcScorecard, scorecard: ArtifactRef
) -> EvaluationReceipt:
    evidence = (scorecard,)
    accepted = card.completed
    return EvaluationReceipt(
        receipt_id=f"evaluation-{request.request_id}",
        accepted=accepted,
        comparison=COMPLETED if accepted else INCOMPLETE,
        objectives=card.to_json(),
        evidence=evidence,
        completed=accepted,
        completion_evidence=evidence if accepted else (),
        required_evidence=(scorecard.digest,) if accepted else (),
        usage=_usage(request),
        raw_receipt=scorecard,
        **request.receipt_identity(),
    )


def _not_evaluated(
    request: EvaluationRequest, reason: str, scorecard: ArtifactRef | None = None
) -> EvaluationReceipt:
    return EvaluationReceipt(
        receipt_id=f"evaluation-{request.request_id}",
        accepted=False,
        comparison=NOT_EVALUATED,
        objectives=frozen_mapping({"not_evaluated": reason}),
        evidence=() if scorecard is None else (scorecard,),
        valid=False,
        completed=False,
        usage=_usage(request),
        raw_receipt=scorecard,
        **request.receipt_identity(),
    )


def _usage(request: EvaluationRequest) -> UsageMeasurement:
    return UsageMeasurement(
        measurement_id=f"evaluator-usage-{request.request_id}", evaluations=1
    )
