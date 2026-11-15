"""Run, aggregate, and verify the EXP-0001 supervisor-policy experiment.

This is experiment-local scaffolding over public kernel and journal APIs. It
does not change the kernel or PHASE-0003 modules: scripted fixtures stand in
for agent behavior, and the monitor policies P1-P3 are versioned run policy
implemented here, exactly as EXP-0001 declares.

Usage::

    uv run python tools/exp_0001_supervisor_policy.py run
    uv run python tools/exp_0001_supervisor_policy.py aggregate
    uv run python tools/exp_0001_supervisor_policy.py verify
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence, cast

from vharness.agent import (
    ActionChoice,
    ActionDefinition,
    ActionProposal,
    BudgetLimits,
    CompletionMode,
    ComponentIdentity,
    ContractError,
    EnvironmentContract,
    EvaluationChoice,
    EvaluationReceipt,
    ExecutionReceipt,
    KnowledgeSource,
    RecordEnvelope,
    RuntimeCapabilities,
    Session,
    SessionStatus,
    StateRef,
    TaskSpec,
    TransitionError,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json, parse_json_object
from vharness.agent.evidence import counters as derive_counters
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import (
    DecisionInput,
    MemoryUpdate,
    ModelAttempt,
    ModelCallError,
    ModelResponse,
    ProgressClaimChange,
)
from vharness.agent.memory import KnowledgeStatus, ProgressCategory
from vharness.agent.models import (
    EvaluationRequest,
    Event,
    ExecutionStatus,
    JsonValue,
    Message,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)

# This is a bounded, auditable evidence harness, not a product module.
# pylint: disable=too-many-arguments,too-many-instance-attributes
# pylint: disable=too-many-lines,too-many-locals,too-many-statements
# pylint: disable=too-few-public-methods

SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = SOURCE_ROOT / "evidence" / "phase-0003-supervisor-policy"
TOOL_RELPATH = "tools/exp_0001_supervisor_policy.py"
EVENT_STREAM_NAME = "events.jsonl.gz"

SEEDS = (0, 1, 2, 3, 4)
POLICIES = ("P0", "P1", "P2", "P3")
FIXTURE_CLASSES = (
    "productive",
    "plateaued-exploring",
    "stalled",
    "repetitive",
    "regressing",
    "recovery-drill",
)
STAGNATION_CLASSES = ("stalled", "repetitive", "regressing", "recovery-drill")
CONTROL_CLASSES = ("productive", "plateaued-exploring")

MAIN_CALL_BUDGET = 12
ACTION_BUDGET = 24
CONTEXT_TOKEN_BUDGET = 4096
EVALUATION_BUDGET = 3
SUPERVISOR_CALL_ALLOCATION = 4
SUPERVISOR_TOKENS_PER_CALL = 512
SUPERVISOR_TOKEN_ALLOCATION = 2048
OPERATOR_CUE_CALL = 10

POLICY_PARAMETERS: Mapping[str, Mapping[str, object]] = {
    "P0": {"kind": "operator-only", "operator_cue_call": OPERATOR_CUE_CALL},
    "P1": {
        "kind": "repetition",
        "window_calls": 8,
        "repeat_threshold": 3,
        "cooldown_calls": 4,
    },
    "P2": {
        "kind": "budget-burn",
        "action_fraction": 0.6,
        "token_fraction": 0.6,
        "cooldown_calls": 4,
    },
    "P3": {
        "kind": "combined",
        "window_calls": 8,
        "repeat_threshold": 3,
        "action_fraction": 0.6,
        "token_fraction": 0.6,
        "cooldown_calls": 6,
    },
}

ACTION_FINGERPRINT_PREFIX = "action"
RECEIPT_FINGERPRINT_PREFIX = "receipt"
EVALUATION_FINGERPRINT_PREFIX = "evaluation"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_list(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else None


def _event_stream_bytes(events: Sequence[Event]) -> tuple[bytes, int]:
    """Serialize the canonical event stream and gzip it deterministically."""
    raw = "".join(
        canonical_json(
            {
                "event_id": event.event_id,
                "session_id": event.session_id,
                "sequence": event.sequence,
                "kind": event.kind,
                "objective_version": event.objective_version,
                "payload": event.payload,
                "recorded_at": event.recorded_at,
                "causation_id": event.causation_id,
                "correlation_id": event.correlation_id,
                "schema_version": event.schema_version,
            }
        )
        + "\n"
        for event in events
    ).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(raw)
    return buffer.getvalue(), len(raw)


@dataclass(frozen=True, slots=True)
class Decision:
    """One scripted fixture decision: action, evaluation, or progress claim."""

    kind: str
    name: str = ""
    arguments: tuple[tuple[str, str], ...] = ()
    claim_after: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {
            "action",
            "evaluate",
            "knowledge-item",
            "knowledge-progress",
        }:
            raise ValueError(f"unsupported decision kind {self.kind!r}")

    def resolved_arguments(self) -> dict[str, str]:
        """Return the action arguments as a plain mapping."""
        return dict(self.arguments)


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """A labeled scripted trajectory class for the paired comparison."""

    name: str
    plateau: tuple[Decision, ...]
    recovery: tuple[Decision, ...]
    opening: tuple[Decision, ...] = ()
    onset_call: int = 1
    probe_failure: bool = False
    interrupt_opening: bool = False
    success_expected_without_intervention: bool = False


def _probe(target: str = "a") -> Decision:
    return Decision("action", "probe", (("target", target),))


FOCUS = Decision("action", "focus")
REPAIR = Decision("action", "repair")
VERIFY = Decision("action", "verify")
REVERT = Decision("action", "revert")
INSPECT = Decision("action", "inspect")
EVALUATE = Decision("evaluate")

RECOVERY_SCRIPT = (FOCUS, REPAIR, VERIFY, EVALUATE)

FIXTURES: Mapping[str, FixtureSpec] = {
    "productive": FixtureSpec(
        name="productive",
        plateau=(
            INSPECT,
            Decision(
                "knowledge-item",
                claim_after="the fixture goal requires focus, repair, and verify",
            ),
            Decision(
                "knowledge-progress",
                claim_after="the fixture goal requires focus, repair, and verify",
            ),
            FOCUS,
            REPAIR,
            VERIFY,
            EVALUATE,
        ),
        recovery=RECOVERY_SCRIPT,
        success_expected_without_intervention=True,
    ),
    "plateaued-exploring": FixtureSpec(
        name="plateaued-exploring",
        plateau=(
            _probe("t1"),
            _probe("t2"),
            _probe("t3"),
            FOCUS,
            _probe("t4"),
            _probe("t5"),
            REPAIR,
            _probe("t6"),
            VERIFY,
            EVALUATE,
        ),
        recovery=RECOVERY_SCRIPT,
        success_expected_without_intervention=True,
    ),
    "stalled": FixtureSpec(
        name="stalled",
        plateau=(_probe(),),
        recovery=RECOVERY_SCRIPT,
    ),
    "repetitive": FixtureSpec(
        name="repetitive",
        plateau=(_probe(),),
        recovery=RECOVERY_SCRIPT,
        probe_failure=True,
    ),
    "regressing": FixtureSpec(
        name="regressing",
        plateau=(FOCUS, REVERT, FOCUS, EVALUATE, REVERT),
        recovery=RECOVERY_SCRIPT,
    ),
    "recovery-drill": FixtureSpec(
        name="recovery-drill",
        plateau=(_probe(),),
        recovery=RECOVERY_SCRIPT,
        opening=(_probe("interrupted"),),
        interrupt_opening=True,
        onset_call=2,
    ),
}


def _fixture_actions() -> tuple[ActionDefinition, ...]:
    return (
        ActionDefinition("inspect", "Inspect the fixture state"),
        ActionDefinition(
            "probe",
            "Probe one fixture target",
            {
                "type": "object",
                "properties": {"target": {"type": "string"}},
            },
        ),
        ActionDefinition("focus", "Focus the fixture on the known issue"),
        ActionDefinition("repair", "Apply the corrective change"),
        ActionDefinition("verify", "Verify the corrective change"),
        ActionDefinition("revert", "Revert the fixture to its initial stage"),
    )


class FixtureEnvironment:
    """Mechanical contract for one scripted fixture class."""

    def __init__(self, spec: FixtureSpec) -> None:
        self._spec = spec

    def describe(self) -> EnvironmentContract:
        """Return the fixture's mechanical contract."""
        return EnvironmentContract(
            environment_id=self.environment_id,
            version="v1",
            initial_state=_state_ref(self._spec, 0),
            actions=_fixture_actions(),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )

    @property
    def environment_id(self) -> str:
        """Return the fixture-scoped environment ID."""
        return f"exp0001-{self._spec.name}"


def _state_ref(spec: FixtureSpec, stage: int) -> StateRef:
    return StateRef(
        owner=f"exp0001-{spec.name}",
        value=f"stage-{stage}",
        digest=_digest(f"{spec.name}:{stage}"),
    )


class FixtureRuntime:
    """Scripted external runtime with four mechanical fixture stages."""

    def __init__(self, spec: FixtureSpec) -> None:
        self._spec = spec
        self._stage = 0
        self._interrupt_pending = spec.interrupt_opening
        self._running_receipts: dict[str, ExecutionReceipt] = {}

    @property
    def stage(self) -> int:
        """Return the current fixture stage."""
        return self._stage

    def identity(self) -> ComponentIdentity:
        """Return the runtime-owned identity."""
        return ComponentIdentity(f"exp0001-{self._spec.name}-runtime", "v1")

    def state_ref(self) -> StateRef:
        """Return the current externally owned state reference."""
        return _state_ref(self._spec, self._stage)

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Apply one proposal mechanically and return its receipt."""
        action = proposal.action_name
        if action == "probe" and self._interrupt_pending:
            self._interrupt_pending = False
            receipt = ExecutionReceipt(
                receipt_id=f"receipt-{proposal.proposal_id}",
                proposal_id=proposal.proposal_id,
                objective_version=proposal.objective_version,
                status=ExecutionStatus.RUNNING,
                state=self.state_ref(),
            )
            self._running_receipts[proposal.proposal_id] = receipt
            return receipt
        status = ExecutionStatus.SUCCEEDED
        error: str | None = None
        if action == "focus":
            self._stage = max(self._stage, 1)
        elif action == "repair":
            self._stage = max(self._stage, 2) if self._stage >= 1 else self._stage
        elif action == "verify":
            self._stage = max(self._stage, 3) if self._stage >= 2 else self._stage
        elif action == "revert":
            self._stage = 0
        elif action == "probe":
            if self._spec.probe_failure:
                status = ExecutionStatus.FAILED
                error = "probe did not change the fixture state"
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=status,
            state=self.state_ref(),
            error=error,
            usage=UsageMeasurement(
                measurement_id=f"receipt-{proposal.proposal_id}-usage",
                actions=1,
                duration_ms=0,
            ),
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Resolve an interrupted probe with its final receipt."""
        previous = self._running_receipts.pop(operation_id, None)
        if previous is None:
            return ReconciliationResult(ReconciliationKind.UNKNOWN)
        settled = ExecutionReceipt(
            receipt_id=f"{previous.receipt_id}-settled",
            proposal_id=previous.proposal_id,
            objective_version=previous.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=self.state_ref(),
            usage=UsageMeasurement(
                measurement_id=f"{previous.receipt_id}-settled-usage",
                actions=1,
                duration_ms=0,
            ),
        )
        return ReconciliationResult(ReconciliationKind.RECEIPT, settled)


class FixtureEvaluator:
    """Fixture-owned external acceptance; it never rescores Vharness state."""

    def __init__(self, spec: FixtureSpec, runtime: FixtureRuntime) -> None:
        self._spec = spec
        self._runtime = runtime

    def identity(self) -> ComponentIdentity:
        """Return the evaluator-owned identity."""
        return ComponentIdentity(f"exp0001-{self._spec.name}-evaluator", "v1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Accept only the fixture's verified goal stage."""
        accepted = self._runtime.stage >= 3
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=accepted,
            comparison="improved" if accepted else "regressed",
            objectives={"stage": self._runtime.stage},
            usage=UsageMeasurement(
                measurement_id=f"evaluation-{request.request_id}-usage",
                evaluations=1,
                duration_ms=0,
            ),
        )

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """Report no evaluator-side knowledge of an interrupted request."""
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


class FixtureModel:
    """Scripted agent behavior with an intervention switch."""

    model_id = "scripted-exp0001"
    repair_budget = 0

    def __init__(self, spec: FixtureSpec, seed: int, source_id: str) -> None:
        self._spec = spec
        self._source_id = source_id
        self._claim_id = f"claim-{spec.name}-{seed}"
        self._item_id = f"finding-{spec.name}-{seed}"
        self._opening_index = 0
        self._plateau_index = 0
        self._recovery_index = 0
        self._mode = "plateau"

    def intervene(self) -> None:
        """Switch the scripted trajectory to its recovery script."""
        self._mode = "recovery"

    def decide(self, request: DecisionInput) -> ModelResponse:
        """Return the next scripted result with settled usage."""
        decision = self._next_decision()
        result = self._result_for(decision)
        usage = UsageMeasurement(
            measurement_id=f"{request.request_id}-usage",
            model_calls=1,
            duration_ms=0,
        )
        return ModelResponse(
            result=result,
            output=type(result).__name__,
            model_id=self.model_id,
            finish_reason="stop",
            usage=usage,
            attempts=(
                ModelAttempt(
                    output=type(result).__name__, finish_reason="stop", duration_ms=0
                ),
            ),
            duration_ms=0,
        )

    def _next_decision(self) -> Decision:
        if self._opening_index < len(self._spec.opening):
            decision = self._spec.opening[self._opening_index]
            self._opening_index += 1
            return decision
        if self._mode == "plateau":
            decision = self._spec.plateau[self._plateau_index % len(self._spec.plateau)]
            self._plateau_index += 1
            return decision
        decision = self._spec.recovery[
            min(self._recovery_index, len(self._spec.recovery) - 1)
        ]
        self._recovery_index += 1
        return decision

    def _result_for(self, decision: Decision):
        if decision.kind == "action":
            return ActionChoice(
                action_name=decision.name,
                rationale=f"fixture {self._spec.name} action",
                arguments=decision.resolved_arguments(),
            )
        if decision.kind == "evaluate":
            return EvaluationChoice("evaluate the fixture state")
        if decision.kind == "knowledge-item":
            return MemoryUpdate(
                item_id=self._item_id,
                statement=decision.claim_after,
                scope=self._spec.name,
                status=KnowledgeStatus.SUPPORTED,
                evidence_ids=(self._source_id,),
            )
        return ProgressClaimChange(
            claim_id=self._claim_id,
            category=ProgressCategory.KNOWLEDGE,
            before="fixture objective unverified",
            after=decision.claim_after,
            scope=self._spec.name,
            evidence_ids=(self._item_id,),
            assessor="fixture",
        )


@dataclass(frozen=True, slots=True)
class StepFacts:
    """Journal-derived facts for one completed model decision."""

    call: int
    fingerprints: tuple[str, ...]
    actions: int
    failed_receipts: int
    input_tokens: int
    progress_signals: int


@dataclass(frozen=True, slots=True)
class TriggerReport:
    """One supervisor-policy trigger with its recorded inputs."""

    policy: str
    call: int
    reason: str
    fingerprint: str | None
    repeat_count: int | None
    actions_used: int
    tokens_used: int

    def to_json(self) -> dict[str, object]:
        """Return the trigger-input log entry."""
        return {
            "policy": self.policy,
            "call": self.call,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "repeat_count": self.repeat_count,
            "actions_used": self.actions_used,
            "tokens_used": self.tokens_used,
        }


class PolicyMonitor:
    """Versioned monitor policy evaluated after each completed decision."""

    def __init__(self, policy: str, params: Mapping[str, object]) -> None:
        self._policy = policy
        self._params = params
        self._last_fire: int | None = None

    def observe(self, facts: Sequence[StepFacts], call: int) -> TriggerReport | None:
        """Return a trigger report when the policy fires before this call."""
        cooldown = _as_int(self._params.get("cooldown_calls"))
        if self._last_fire is not None and call - self._last_fire < cooldown:
            return None
        reason: str | None = None
        fingerprint: str | None = None
        repeats: int | None = None
        if self._policy in {"P1", "P3"}:
            hit = _repetition_hit(
                facts,
                _as_int(self._params.get("window_calls"), 8),
                _as_int(self._params.get("repeat_threshold"), 3),
            )
            if hit is not None:
                reason, fingerprint, repeats = "repetition", hit[0], hit[1]
        actions_used = sum(fact.actions for fact in facts)
        tokens_used = sum(fact.input_tokens for fact in facts)
        supported_progress = sum(fact.progress_signals for fact in facts)
        if reason is None and self._policy in {"P2", "P3"} and supported_progress == 0:
            action_limit = math.ceil(
                _as_float(self._params.get("action_fraction"), 0.6) * ACTION_BUDGET
            )
            token_limit = math.ceil(
                _as_float(self._params.get("token_fraction"), 0.6)
                * CONTEXT_TOKEN_BUDGET
            )
            if actions_used >= action_limit or tokens_used >= token_limit:
                reason = "budget-burn"
        if reason is None:
            return None
        self._last_fire = call
        return TriggerReport(
            policy=self._policy,
            call=call,
            reason=reason,
            fingerprint=fingerprint,
            repeat_count=repeats,
            actions_used=actions_used,
            tokens_used=tokens_used,
        )


def _repetition_hit(
    facts: Sequence[StepFacts], window: int, threshold: int
) -> tuple[str, int] | None:
    counts: Counter[str] = Counter()
    for fact in facts[-window:]:
        counts.update(fact.fingerprints)
    if not counts:
        return None
    fingerprint, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    if count >= threshold:
        return fingerprint, count
    return None


def _action_fingerprint(name: str, arguments: object) -> str:
    args = _as_mapping(arguments)
    canonical = canonical_json({key: args[key] for key in sorted(args)})
    return f"{ACTION_FINGERPRINT_PREFIX}:{_normalize(name)}:{_normalize(canonical)}"


def _facts_from_events(events: Sequence[Event], call: int) -> StepFacts:
    fingerprints: list[str] = []
    actions = 0
    failed = 0
    tokens = 0
    progress = 0
    for event in events:
        if event.kind == "action_intended":
            proposal = _as_mapping(event.payload.get("proposal"))
            name = _as_str(proposal.get("action_name"))
            fingerprints.append(_action_fingerprint(name, proposal.get("arguments")))
            actions += 1
        elif event.kind == "receipt_received":
            receipt = _as_mapping(event.payload.get("receipt"))
            status = _as_str(receipt.get("status"))
            if status not in {"succeeded", "accepted"}:
                failed += 1
                fingerprints.append(
                    f"{RECEIPT_FINGERPRINT_PREFIX}:{_normalize(status)}:"
                    f"{_normalize(_as_str(receipt.get('error')))}"
                )
        elif event.kind == "evaluation_received":
            disposition = _as_str(event.payload.get("disposition"))
            if disposition != "accepted":
                fingerprints.append(
                    f"{EVALUATION_FINGERPRINT_PREFIX}:{_normalize(disposition)}"
                )
        elif event.kind == "context_assembled":
            manifest = _as_mapping(event.payload.get("manifest"))
            serialized = manifest.get("serialized_input_tokens")
            tokens += _as_int(serialized, _as_int(manifest.get("tokens_used")))
        elif event.kind == "progress_claim_recorded":
            claim = _as_mapping(event.payload.get("claim"))
            if _as_str(claim.get("status")) == "supported":
                progress += 1
        elif event.kind == "knowledge_recorded":
            item = _as_mapping(event.payload.get("item"))
            if _as_str(item.get("status")) == "supported":
                progress += 1
    return StepFacts(
        call=call,
        fingerprints=tuple(fingerprints),
        actions=actions,
        failed_receipts=failed,
        input_tokens=tokens,
        progress_signals=progress,
    )


def _knowledge_source(spec: FixtureSpec, session_id: str) -> KnowledgeSource:
    source_id = f"exp0001-{spec.name}-brief"
    observed_at = _now()
    return KnowledgeSource(
        source_id=source_id,
        provenance="fixture brief",
        observed_at=observed_at,
        envelope=RecordEnvelope(source_id, session_id, 1, observed_at, "fixture brief"),
    )


def _task(spec: FixtureSpec, seed: int) -> TaskSpec:
    return TaskSpec(
        task_id=f"exp0001-{spec.name}-s{seed}",
        objective=f"Reach the fixture goal for {spec.name} (seed {seed}).",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external fixture-evaluator acceptance",
        budgets=BudgetLimits(
            actions=ACTION_BUDGET,
            model_calls=MAIN_CALL_BUDGET,
            evaluations=EVALUATION_BUDGET,
        ),
        environment_id=FixtureEnvironment(spec).environment_id,
        evaluation_contract_id=f"exp0001-{spec.name}-evaluator:v1",
    )


def _deliver_operator_cue(
    session: Session, session_id: str, call: int
) -> dict[str, object]:
    message = Message(
        command_id=f"{session_id}-operator-cue",
        session_id=session_id,
        text=(
            "Operator hint: repeated probing has not changed the fixture state; "
            "consider focusing, repairing, and verifying instead."
        ),
    )
    session.enqueue(message)
    session.advance()
    return {"call": call, "source": "operator", "scheduled": True}


def _run_once(  # pylint: disable=too-many-branches
    spec: FixtureSpec, seed: int, policy: str, run_dir: Path, *, repeat: bool = False
) -> dict[str, object]:
    run_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-repeat" if repeat else ""
    session_id = f"exp0001-{spec.name}-s{seed}-{policy.lower()}{suffix}"
    run_id = f"{spec.name}/s{seed}/{policy}{suffix}"

    journal = SqliteJournal(run_dir / "session.sqlite3")
    session: Session | None = None
    events: tuple[Event, ...] = ()
    counters = None
    facts: list[StepFacts] = []
    triggers: list[TriggerReport] = []
    operator_cues: list[dict[str, object]] = []
    supervisor_calls = 0
    supervisor_tokens = 0
    accepted_at: int | None = None
    terminal = "model_budget_exhausted"
    error: str | None = None
    cursor = 0
    steps = 0
    try:
        artifacts = ArtifactStore(run_dir / "artifacts")
        runtime = FixtureRuntime(spec)
        evaluator = FixtureEvaluator(spec, runtime)
        environment = FixtureEnvironment(spec)
        source = _knowledge_source(spec, session_id)
        model = FixtureModel(spec, seed, source.source_id)
        monitor = (
            PolicyMonitor(policy, POLICY_PARAMETERS[policy]) if policy != "P0" else None
        )
        session = Session.create(
            session_id,
            _task(spec, seed),
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            knowledge_sources=(source,),
        )
        for call in range(1, MAIN_CALL_BUDGET + 1):
            if (
                policy == "P0"
                and spec.name in STAGNATION_CLASSES
                and call == OPERATOR_CUE_CALL
            ):
                operator_cues.append(_deliver_operator_cue(session, session_id, call))
                model.intervene()
            if monitor is not None:
                report = monitor.observe(tuple(facts), call)
                if report is not None:
                    triggers.append(report)
                    supervisor_calls += 1
                    supervisor_tokens += SUPERVISOR_TOKENS_PER_CALL
                    model.intervene()
            view = session.step(model, token_budget=CONTEXT_TOKEN_BUDGET)
            steps = call
            if view.pending_proposal_id is not None:
                session.reconcile_pending()
            new_events = session.events(after_sequence=cursor, limit=10000)
            if new_events:
                cursor = new_events[-1].sequence
            facts.append(_facts_from_events(new_events, call))
            if len(session.accepted_lineage()) > 1:
                accepted_at = call
                terminal = "accepted"
                break
            if view.status in {
                SessionStatus.COMPLETED,
                SessionStatus.STOPPED,
                SessionStatus.FAILED,
            }:
                terminal = f"status:{view.status.value}"
                break
    except ModelCallError as exc:
        terminal = "model_error"
        error = f"{type(exc).__name__}: {exc}"
    except (TransitionError, ContractError) as exc:
        terminal = "error"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            events = session.events(limit=10000)
            counters = derive_counters(events)
        journal.close()
    if session is None or counters is None:
        raise RuntimeError(f"run {run_id} did not create a session: {error}")

    journal_path = run_dir / "session.sqlite3"
    source_journal_sha256 = _sha256_file(journal_path)
    wal_sidecars = [
        sidecar
        for sidecar in (
            journal_path.with_name(journal_path.name + "-wal"),
            journal_path.with_name(journal_path.name + "-shm"),
        )
        if sidecar.exists()
    ]
    stream, uncompressed_bytes = _event_stream_bytes(events)
    (run_dir / EVENT_STREAM_NAME).write_bytes(stream)
    journal_path.unlink()
    for sidecar in wal_sidecars:
        sidecar.unlink()
    summary: dict[str, object] = {
        "run_id": run_id,
        "session_id": session_id,
        "fixture": spec.name,
        "seed": seed,
        "policy": policy,
        "repeat": repeat,
        "onset_call": spec.onset_call,
        "steps": steps,
        "terminal": terminal,
        "error": error,
        "accepted": accepted_at is not None,
        "accepted_at_call": accepted_at,
        "actions_to_recovery": (
            sum(fact.actions for fact in facts if fact.call >= spec.onset_call)
            if accepted_at is not None
            else None
        ),
        "calls_to_recovery": (
            accepted_at - spec.onset_call if accepted_at is not None else None
        ),
        "triggers": [report.to_json() for report in triggers],
        "operator_cues": operator_cues,
        "supervisor_calls": supervisor_calls,
        "supervisor_tokens": supervisor_tokens,
        "supervisor_allocation_exceeded": (
            supervisor_calls > SUPERVISOR_CALL_ALLOCATION
            or supervisor_tokens > SUPERVISOR_TOKEN_ALLOCATION
        ),
        "facts": [
            {
                "call": fact.call,
                "fingerprints": list(fact.fingerprints),
                "actions": fact.actions,
                "failed_receipts": fact.failed_receipts,
                "input_tokens": fact.input_tokens,
                "progress_signals": fact.progress_signals,
            }
            for fact in facts
        ],
        "counters": {
            "actions": counters.actions,
            "evaluations": counters.evaluations,
            "model_calls": counters.model_calls,
            "commits": counters.commits,
            "attempts": counters.attempts,
            "reconciliations": counters.reconciliations,
        },
        "journal": {
            "path": EVENT_STREAM_NAME,
            "format": "canonical-event-stream-gzip-jsonl",
            "sha256": hashlib.sha256(stream).hexdigest(),
            "uncompressed_bytes": uncompressed_bytes,
            "source_journal_sha256": source_journal_sha256,
            "event_count": len(events),
            "wal_sidecars": [sidecar.name for sidecar in wal_sidecars],
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "triggers.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "policy_parameters": POLICY_PARAMETERS[policy],
                "triggers": [report.to_json() for report in triggers],
                "operator_cues": operator_cues,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def _git_revision() -> tuple[str, bool]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return revision, bool(status)


def _fixture_manifest() -> dict[str, object]:
    manifest: dict[str, object] = {}
    for name in FIXTURE_CLASSES:
        spec = FIXTURES[name]
        manifest[name] = {
            "onset_call": spec.onset_call,
            "probe_failure": spec.probe_failure,
            "interrupt_opening": spec.interrupt_opening,
            "success_expected_without_intervention": (
                spec.success_expected_without_intervention
            ),
            "opening": [_decision_json(item) for item in spec.opening],
            "plateau": [_decision_json(item) for item in spec.plateau],
            "recovery": [_decision_json(item) for item in spec.recovery],
        }
    return manifest


def _decision_json(decision: Decision) -> dict[str, object]:
    return {
        "kind": decision.kind,
        "name": decision.name,
        "arguments": dict(decision.arguments),
        "claim_after": decision.claim_after,
    }


def run_experiment(*, force: bool = False) -> dict[str, object]:
    """Generate the EXP-0001 bundle from a clean tree."""
    revision, dirty = _git_revision()
    if dirty:
        raise SystemExit(
            "refusing to run EXP-0001 with tracked working-tree changes; "
            "commit them first"
        )
    if BUNDLE_DIR.exists() and any(BUNDLE_DIR.iterdir()):
        if not force:
            raise SystemExit(f"{BUNDLE_DIR} is not empty; pass --force to regenerate")
        shutil.rmtree(BUNDLE_DIR)
    (BUNDLE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "experiment": "EXP-0001",
        "document": "plans/experiments/exp-0001-supervisor-policy.md",
        "source": {
            "git_revision": revision,
            "git_dirty": False,
            "tool": TOOL_RELPATH,
            "tool_sha256": _sha256_file(Path(__file__)),
        },
        "fixtures": _fixture_manifest(),
        "seeds": list(SEEDS),
        "policies": {name: POLICY_PARAMETERS[name] for name in POLICIES},
        "budgets": {
            "model_calls": MAIN_CALL_BUDGET,
            "actions": ACTION_BUDGET,
            "context_tokens": CONTEXT_TOKEN_BUDGET,
            "evaluations": EVALUATION_BUDGET,
            "supervisor_calls": SUPERVISOR_CALL_ALLOCATION,
            "supervisor_tokens": SUPERVISOR_TOKEN_ALLOCATION,
            "supervisor_tokens_per_call": SUPERVISOR_TOKENS_PER_CALL,
        },
        "terminal_conditions": [
            "external fixture-evaluator acceptance",
            "decision-budget end without acceptance",
        ],
        "retention": {
            "per_run": [EVENT_STREAM_NAME, "summary.json", "triggers.json"],
            "aggregate": ["manifest.json", "results.json", "report.md", "aggregate.py"],
        },
        "provider_calls": 0,
        "retention_note": (
            "Each run's sqlite journal is compacted to a canonical gzip JSONL "
            "event-stream export with the source journal hash recorded; "
            "summaries and result tables are derived from the exported streams."
        ),
    }
    (BUNDLE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (BUNDLE_DIR / "aggregate.py").write_text(_aggregate_script_text(), encoding="utf-8")
    total = 0
    for name in FIXTURE_CLASSES:
        spec = FIXTURES[name]
        for seed in SEEDS:
            for policy in POLICIES:
                run_dir = BUNDLE_DIR / "runs" / name / f"s{seed}" / policy
                _run_once(spec, seed, policy, run_dir)
                total += 1
            repeat_dir = BUNDLE_DIR / "runs" / name / f"s{seed}" / "P0-repeat"
            _run_once(spec, seed, "P0", repeat_dir, repeat=True)
            total += 1
    results = aggregate_bundle(BUNDLE_DIR)
    results["executions"] = total
    write_aggregate(BUNDLE_DIR, results)
    return results


def _aggregate_script_text() -> str:
    return (
        '"""Recompute the EXP-0001 aggregate from the retained run journals."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "BUNDLE = Path(__file__).resolve().parent\n"
        "ROOT = BUNDLE.parents[1]\n"
        "sys.path.insert(0, str(ROOT))\n"
        "\n"
        "from tools.exp_0001_supervisor_policy import write_aggregate  # noqa: E402\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    write_aggregate(BUNDLE)\n"
    )


def _load_summaries(bundle: Path) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for path in sorted(bundle.glob("runs/*/s*/*/summary.json")):
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    return summaries


def _bundle_label(bundle: Path) -> str:
    """Render the bundle path recorded in the aggregate results.

    Bundles under the repository are recorded repo-relative; anything else
    (including a relative path, which resolves against the caller's working
    directory) is recorded by its resolved absolute POSIX path.
    """
    resolved = bundle.resolve()
    try:
        return resolved.relative_to(SOURCE_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def aggregate_bundle(  # pylint: disable=too-many-branches
    bundle: Path,
) -> dict[str, object]:
    """Recompute the EXP-0001 result table from retained run summaries."""
    summaries = _load_summaries(bundle)
    index: dict[tuple[str, int, str], dict[str, object]] = {}
    for summary in summaries:
        key = (
            _as_str(summary.get("fixture")),
            _as_int(summary.get("seed")),
            _as_str(summary.get("policy"))
            + ("-repeat" if summary.get("repeat") else ""),
        )
        index[key] = summary

    reproducible = True
    for fixture in FIXTURE_CLASSES:
        for seed in SEEDS:
            base = index.get((fixture, seed, "P0"))
            repeat = index.get((fixture, seed, "P0-repeat"))
            if base is None or repeat is None:
                reproducible = False
                continue
            compare = ("accepted", "accepted_at_call", "steps", "terminal")
            if any(base.get(field) != repeat.get(field) for field in compare):
                reproducible = False

    censored = [
        summary
        for summary in summaries
        if _as_str(summary.get("terminal"))
        not in {"accepted", "model_budget_exhausted"}
    ]

    candidates: dict[str, dict[str, object]] = {}
    for policy in ("P1", "P2", "P3"):
        improved_pairs = 0
        class_improvements: dict[str, int] = {}
        false_productive = 0
        false_exploring: dict[str, int] = {}
        supervisor_calls = 0
        supervisor_tokens = 0
        allocation_exceeded = 0
        latencies: list[int] = []
        harms: list[str] = []
        for fixture in STAGNATION_CLASSES:
            improved_in_class = 0
            for seed in SEEDS:
                baseline = index.get((fixture, seed, "P0"), {})
                candidate = index.get((fixture, seed, policy), {})
                if bool(candidate.get("accepted")) and not bool(
                    baseline.get("accepted")
                ):
                    improved_pairs += 1
                    improved_in_class += 1
            class_improvements[fixture] = improved_in_class
        for fixture in CONTROL_CLASSES:
            for seed in SEEDS:
                candidate = index.get((fixture, seed, policy), {})
                baseline = index.get((fixture, seed, "P0"), {})
                count = len(_as_list(candidate.get("triggers")))
                if count:
                    false_exploring[fixture] = false_exploring.get(fixture, 0) + count
                    if fixture == "productive":
                        false_productive += count
                if bool(baseline.get("accepted")) and not bool(
                    candidate.get("accepted")
                ):
                    harms.append(f"{fixture}/s{seed}")
        for summary in summaries:
            if _as_str(summary.get("policy")) != policy:
                continue
            supervisor_calls += _as_int(summary.get("supervisor_calls"))
            supervisor_tokens += _as_int(summary.get("supervisor_tokens"))
            if summary.get("supervisor_allocation_exceeded"):
                allocation_exceeded += 1
            for trigger in _as_list(summary.get("triggers")):
                if isinstance(trigger, Mapping):
                    latencies.append(
                        _as_int(trigger.get("call"))
                        - _as_int(summary.get("onset_call"))
                    )
        accepted = (
            improved_pairs >= 3
            and len([value for value in class_improvements.values() if value >= 3]) >= 2
        )
        rejected = False
        reasons: list[str] = []
        if false_productive:
            rejected = True
            reasons.append(f"{false_productive} trigger(s) on productive control runs")
        exploring_over_one = sum(
            1
            for fixture in ("plateaued-exploring",)
            for seed in SEEDS
            if len(_as_list(index.get((fixture, seed, policy), {}).get("triggers"))) > 1
        )
        if exploring_over_one:
            rejected = True
            reasons.append(
                f"{exploring_over_one} plateaued-exploring run(s) with "
                "more than one trigger"
            )
        if allocation_exceeded:
            rejected = True
            reasons.append(
                f"{allocation_exceeded} run(s) exceeded the supervisor allocation"
            )
        if harms:
            reasons.append(f"acceptance harm on {harms}")
        candidates[policy] = {
            "stagnation_pairs_improved": improved_pairs,
            "class_improvements": class_improvements,
            "false_triggers_productive": false_productive,
            "false_triggers_exploring": false_exploring,
            "supervisor_calls": supervisor_calls,
            "supervisor_tokens": supervisor_tokens,
            "trigger_latencies": latencies,
            "acceptance_harm_runs": harms,
            "meets_acceptance_tolerance": accepted,
            "rejected": rejected,
            "rejection_reasons": reasons,
        }

    passing = [
        policy
        for policy in ("P1", "P2", "P3")
        if candidates[policy].get("meets_acceptance_tolerance")
        and not candidates[policy].get("rejected")
    ]

    def rank(policy: str) -> tuple[int, int, float, str]:
        data = candidates[policy]
        exploring = sum(
            _as_int(value)
            for value in _as_mapping(data.get("false_triggers_exploring")).values()
        )
        latencies = [_as_int(item) for item in _as_list(data.get("trigger_latencies"))]
        return (
            _as_int(data.get("false_triggers_productive")) + exploring,
            _as_int(data.get("supervisor_calls")),
            _median(latencies),
            policy,
        )

    selected = min(passing, key=rank) if passing else None
    if not reproducible:
        selected = None
    return {
        "experiment": "EXP-0001",
        "bundle": _bundle_label(bundle),
        "runs": len(summaries),
        "fixtures": list(FIXTURE_CLASSES),
        "seeds": list(SEEDS),
        "policies": list(POLICIES),
        "baseline_reproducible": reproducible,
        "censored_runs": [
            {
                "run_id": summary.get("run_id"),
                "terminal": summary.get("terminal"),
                "error": summary.get("error"),
            }
            for summary in censored
        ],
        "candidates": candidates,
        "passing_candidates": passing,
        "selected_policy": selected,
        "tolerance_interpretation": {
            "stagnation_pairs": [
                f"{fixture}/s{seed}" for fixture in STAGNATION_CLASSES for seed in SEEDS
            ],
            "censored": (
                "a run is censored only when it terminates on an unexpected error; "
                "ending at the predeclared decision budget without acceptance is a "
                "valid negative observation"
            ),
        },
    }


def _median(values: Sequence[int]) -> float:
    if not values:
        return float("inf")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def write_aggregate(
    bundle: Path, results: dict[str, object] | None = None
) -> dict[str, object]:
    """Write results.json and report.md for the bundle and return results."""
    computed = results if results is not None else aggregate_bundle(bundle)
    (bundle / "results.json").write_text(
        json.dumps(computed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (bundle / "report.md").write_text(_report_text(computed), encoding="utf-8")
    return computed


def _report_text(results: Mapping[str, object]) -> str:
    lines = [
        "# EXP-0001 supervisor-policy results",
        "",
        f"- Runs: {results.get('runs')}",
        f"- Baseline reproducible: {results.get('baseline_reproducible')}",
        f"- Selected policy: {results.get('selected_policy')}",
        "",
        "| Policy | Improved pairs | Classes >=3/5 | False (productive) | "
        "False (exploring) | Supervisor calls | Meets tolerance | Rejected |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    candidates = results.get("candidates") or {}
    if isinstance(candidates, Mapping):
        for policy in ("P1", "P2", "P3"):
            data = candidates.get(policy)
            if not isinstance(data, Mapping):
                continue
            classes = data.get("class_improvements") or {}
            class_count = (
                len([value for value in classes.values() if value >= 3])
                if isinstance(classes, Mapping)
                else 0
            )
            exploring = data.get("false_triggers_exploring") or {}
            exploring_total = (
                sum(exploring.values()) if isinstance(exploring, Mapping) else 0
            )
            lines.append(
                f"| {policy} | {data.get('stagnation_pairs_improved')} | "
                f"{class_count} | {data.get('false_triggers_productive')} | "
                f"{exploring_total} | {data.get('supervisor_calls')} | "
                f"{data.get('meets_acceptance_tolerance')} | "
                f"{data.get('rejected')} |"
            )
    lines.append("")
    lines.append(
        "Tolerances and selection follow "
        "`plans/experiments/exp-0001-supervisor-policy.md`."
    )
    return "\n".join(lines) + "\n"


def verify_bundle(  # pylint: disable=too-many-branches
    bundle: Path,
) -> list[str]:
    """Check manifest identity, journal replay, and aggregate consistency."""
    problems: list[str] = []
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    source = _as_mapping(manifest.get("source"))
    tool = SOURCE_ROOT / TOOL_RELPATH
    if tool.exists() and source.get("tool_sha256") != _sha256_file(tool):
        problems.append("tool sha256 does not match the manifest")
    for summary_path in sorted(bundle.glob("runs/*/s*/*/summary.json")):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        run_dir = summary_path.parent
        recorded = _as_mapping(summary.get("journal"))
        journal_path = run_dir / _as_str(recorded.get("path"), EVENT_STREAM_NAME)
        if not journal_path.exists():
            problems.append(f"{summary_path}: journal missing")
            continue
        if recorded.get("sha256") != _sha256_file(journal_path):
            problems.append(f"{summary_path}: journal sha256 mismatch")
        try:
            events = _read_events(journal_path)
        except (sqlite3.Error, ValueError) as exc:
            problems.append(f"{summary_path}: journal unreadable: {exc}")
            continue
        if len(events) != _as_int(recorded.get("event_count")):
            problems.append(f"{summary_path}: event count mismatch")
        counters = derive_counters(events)
        expected = _as_mapping(summary.get("counters"))
        for name in ("actions", "evaluations", "model_calls", "commits"):
            if _as_int(expected.get(name)) != getattr(counters, name):
                problems.append(f"{summary_path}: counter {name} mismatch")
        if bool(summary.get("accepted")) != (counters.commits > 0):
            problems.append(f"{summary_path}: acceptance mismatch")
        expected_actions = sum(1 for event in events if event.kind == "action_intended")
        expected_tokens = 0
        for event in events:
            if event.kind != "context_assembled":
                continue
            manifest = _as_mapping(event.payload.get("manifest"))
            expected_tokens += _as_int(
                manifest.get("serialized_input_tokens"),
                _as_int(manifest.get("tokens_used")),
            )
        recorded_facts = _as_list(summary.get("facts"))
        recorded_actions = sum(
            _as_int(_as_mapping(recorded).get("actions"))
            for recorded in recorded_facts
            if isinstance(recorded, Mapping)
        )
        recorded_tokens = sum(
            _as_int(_as_mapping(recorded).get("input_tokens"))
            for recorded in recorded_facts
            if isinstance(recorded, Mapping)
        )
        if recorded_actions != expected_actions:
            problems.append(f"{summary_path}: action facts mismatch")
        if recorded_tokens != expected_tokens:
            problems.append(f"{summary_path}: token facts mismatch")
    try:
        results = json.loads((bundle / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"results.json unreadable: {exc}")
        return problems
    recomputed = aggregate_bundle(bundle)
    for field_name in ("selected_policy", "baseline_reproducible", "censored_runs"):
        if results.get(field_name) != recomputed.get(field_name):
            problems.append(f"results field {field_name} is not reproducible")
    for policy in ("P1", "P2", "P3"):
        recorded = _as_mapping(results.get("candidates", {})).get(policy)
        fresh = _as_mapping(recomputed.get("candidates", {})).get(policy)
        recorded_map = _as_mapping(recorded)
        fresh_map = _as_mapping(fresh)
        for field_name in (
            "stagnation_pairs_improved",
            "meets_acceptance_tolerance",
            "rejected",
            "supervisor_calls",
            "false_triggers_productive",
        ):
            if recorded_map.get(field_name) != fresh_map.get(field_name):
                problems.append(
                    f"candidate {policy} field {field_name} is not reproducible"
                )
    return problems


def _read_events(path: Path) -> tuple[Event, ...]:
    """Read a canonical event-stream export or a live sqlite journal."""
    if path.name.endswith(".jsonl.gz"):
        raw = gzip.decompress(path.read_bytes()).decode("utf-8")
        return tuple(
            _event_from_json(parse_json_object(line))
            for line in raw.splitlines()
            if line.strip()
        )
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute("""
            SELECT event_id, session_id, sequence, kind, objective_version,
                   payload, recorded_at, causation_id, correlation_id,
                   schema_version
            FROM events
            ORDER BY sequence
            """).fetchall()
    finally:
        connection.close()
    return tuple(
        Event(
            event_id=row[0],
            session_id=row[1],
            sequence=row[2],
            kind=row[3],
            objective_version=row[4],
            payload=parse_json_object(row[5]),
            recorded_at=row[6],
            causation_id=row[7],
            correlation_id=row[8],
            schema_version=row[9],
        )
        for row in rows
    )


def _event_from_json(payload: Mapping[str, JsonValue]) -> Event:
    """Rebuild one canonical event from its exported JSON object."""
    return Event(
        event_id=_as_str(payload.get("event_id")),
        session_id=_as_str(payload.get("session_id")),
        sequence=_as_int(payload.get("sequence")),
        kind=_as_str(payload.get("kind")),
        objective_version=_as_int(payload.get("objective_version"), 1),
        payload=cast(Mapping[str, JsonValue], _as_mapping(payload.get("payload"))),
        recorded_at=_as_str(payload.get("recorded_at")),
        causation_id=_optional_str(payload.get("causation_id")),
        correlation_id=_optional_str(payload.get("correlation_id")),
        schema_version=_as_int(payload.get("schema_version"), 1),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the experiment, recompute aggregates, or verify the bundle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("run", "aggregate", "verify"), nargs="?", default="run"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--bundle", type=Path, default=BUNDLE_DIR)
    args = parser.parse_args(argv)
    if args.command == "run":
        results = run_experiment(force=args.force)
        print(canonical_json(results))
        return 0
    if args.command == "aggregate":
        results = write_aggregate(args.bundle)
        print(canonical_json(results))
        return 0
    problems = verify_bundle(args.bundle)
    if problems:
        for problem in problems:
            print(f"problem: {problem}")
        return 1
    print("verify: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
