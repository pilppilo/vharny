"""Run, aggregate, and verify the PHASE-0003 item-6 paired recovery evidence.

This is experiment-local scaffolding over the public kernel supervision API. It
does not change the kernel or PHASE-0003 modules: scripted fixtures stand in for
agent behavior, and supervision is enabled only through the ordinary
``SupervisionCapability`` flag on ``Session.create``.

Each labeled fixture class is run paired by class and seed:

- ``unsupervised``: the same kernel with no supervision capability. Stagnation
  is observable only through external evaluation, so a stagnating run exhausts
  its decision budget without an external acceptance.
- ``supervised``: the selected ``monitor.p1.v1`` policy (window 8, repetition
  threshold 3, cooldown 4) with an advisory ``ScriptedSupervisor``. A repeated
  fingerprint triggers the ladder: guidance, then checkpoint/replan, then an
  operator question. The scripted agent answers the guidance and recovers.

The fixed tolerances are EXP-0001's predeclared paired-sample tolerances
(``plans/experiments/exp-0001-supervisor-policy.md``); this bundle reuses them
rather than declaring new ones.

Usage::

    uv run python tools/phase3_paired_recovery.py run
    uv run python tools/phase3_paired_recovery.py aggregate
    uv run python tools/phase3_paired_recovery.py verify
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
    GuidanceResponse,
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
from vharness.agent.kernel import DecisionInput, ModelAttempt, ModelResponse
from vharness.agent.models import (
    EvaluationRequest,
    Event,
    ExecutionStatus,
    JsonValue,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.supervision import (
    SUPERVISION_TRIGGERED,
    Guidance,
    GuidanceDecision,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
    monitor_policy_p1,
)

# This is a bounded, auditable evidence harness, not a product module.
# pylint: disable=too-many-arguments,too-many-instance-attributes
# pylint: disable=too-many-lines,too-many-locals,too-many-statements
# pylint: disable=too-few-public-methods

SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = SOURCE_ROOT / "evidence" / "phase-0003-paired-recovery"
TOOL_RELPATH = "tools/phase3_paired_recovery.py"
EVENT_STREAM_NAME = "events.jsonl.gz"

SEEDS = (0, 1, 2, 3, 4)
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
CONDITIONS = ("unsupervised", "supervised")

# EXP-0001 fixed budgets; the supervisor allocation is the only permitted delta.
MAIN_CALL_BUDGET = 12
ACTION_BUDGET = 24
CONTEXT_TOKEN_BUDGET = 4096
EVALUATION_BUDGET = 3
SUPERVISOR_CALL_ALLOCATION = 4
SUPERVISOR_TOKENS_PER_CALL = 512
SUPERVISOR_TOKEN_ALLOCATION = 2048
MODEL_CALL_BUDGET = MAIN_CALL_BUDGET + SUPERVISOR_CALL_ALLOCATION

# EXP-0001 selection: the retained bundle records monitor.p1.v1 semantics,
# so the tool pins that policy explicitly (the kernel default is monitor.p5.v1
# per ADR-0011 and must not change this recomputation).
POLICY = monitor_policy_p1()

_GUIDANCE_EXPIRY = 30
_SUPERVISOR_INPUT_TOKENS = 400
_SUPERVISOR_OUTPUT_TOKENS = 112


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
    """One scripted fixture decision: action, evaluation, or guidance response."""

    kind: str
    name: str = ""
    arguments: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"action", "evaluate", "guidance"}:
            raise ValueError(f"unsupported decision kind {self.kind!r}")

    def resolved_arguments(self) -> dict[str, str]:
        """Return the action arguments as a plain mapping."""
        return dict(self.arguments)


def _probe(target: str = "a") -> Decision:
    return Decision("action", "probe", (("target", target),))


INSPECT = Decision("action", "inspect")
FOCUS = Decision("action", "focus")
REPAIR = Decision("action", "repair")
VERIFY = Decision("action", "verify")
REVERT = Decision("action", "revert")
EVALUATE = Decision("evaluate")

RECOVERY_SCRIPT = (FOCUS, REPAIR, VERIFY, EVALUATE)


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """A labeled scripted trajectory class for the paired comparison."""

    name: str
    stagnation: tuple[Decision, ...]
    opening: tuple[Decision, ...] = ()
    guidance_response: GuidanceDecision = GuidanceDecision.ACCEPT
    probe_failure: bool = False
    interrupt_opening: bool = False
    success_expected_without_intervention: bool = False


FIXTURES: Mapping[str, FixtureSpec] = {
    "productive": FixtureSpec(
        name="productive",
        stagnation=(INSPECT, FOCUS, REPAIR, VERIFY, EVALUATE),
        success_expected_without_intervention=True,
    ),
    "plateaued-exploring": FixtureSpec(
        name="plateaued-exploring",
        stagnation=(
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
        success_expected_without_intervention=True,
    ),
    "stalled": FixtureSpec(
        name="stalled",
        stagnation=(_probe(),),
    ),
    "repetitive": FixtureSpec(
        name="repetitive",
        stagnation=(
            _probe("t1"),
            _probe("t2"),
            _probe("t3"),
            _probe("t4"),
            _probe("t5"),
        ),
        # The agent rejects guidance; the next rung (replan) still recovers, so
        # the rejection stays visible without hiding the recovery.
        guidance_response=GuidanceDecision.REJECT,
        probe_failure=True,
    ),
    "regressing": FixtureSpec(
        name="regressing",
        stagnation=(FOCUS, REVERT, FOCUS, EVALUATE, REVERT),
    ),
    "recovery-drill": FixtureSpec(
        name="recovery-drill",
        stagnation=(_probe(),),
        opening=(_probe("interrupted"),),
        # The agent replans on guidance, exercising the fresh-attempt path.
        guidance_response=GuidanceDecision.REPLAN,
        interrupt_opening=True,
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


def _state_ref(spec: FixtureSpec, stage: int) -> StateRef:
    return StateRef(
        owner=f"phase3-paired-{spec.name}",
        value=f"stage-{stage}",
        digest=_digest(f"{spec.name}:{stage}"),
    )


class FixtureEnvironment:
    """Mechanical contract for one scripted fixture class."""

    def __init__(self, spec: FixtureSpec) -> None:
        self._spec = spec

    @property
    def environment_id(self) -> str:
        """Return the fixture-scoped environment ID."""
        return f"phase3-paired-{self._spec.name}"

    def describe(self) -> EnvironmentContract:
        """Return the fixture's mechanical contract."""
        return EnvironmentContract(
            environment_id=self.environment_id,
            version="v1",
            initial_state=_state_ref(self._spec, 0),
            actions=_fixture_actions(),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
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
        return ComponentIdentity(f"phase3-paired-{self._spec.name}-runtime", "v1")

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
        elif action == "probe" and self._spec.probe_failure:
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
        return ComponentIdentity(f"phase3-paired-{self._spec.name}-evaluator", "v1")

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
    """Scripted agent that responds to supervisor guidance and recovers.

    The model reads its own visible decision input: active guidance appears as a
    mandatory context item in the ``supervision`` band. It answers that guidance
    once and then follows the recovery script. A ``REJECT`` response keeps the
    stagnation script until the ladder's next rung starts a fresh attempt, which
    the model detects through the changed ``attempt_id``.
    """

    model_id = "scripted-phase3-paired"
    repair_budget = 0

    def __init__(self, spec: FixtureSpec) -> None:
        self._spec = spec
        self._phase = "stagnation"
        self._responded = False
        self._initial_attempt: str | None = None
        self._opening_index = 0
        self._stagnation_index = 0
        self._recovery_index = 0

    def decide(self, request: DecisionInput) -> ModelResponse:
        """Return the next scripted result with settled usage."""
        if self._initial_attempt is None:
            self._initial_attempt = request.attempt_id
        decision = self._next_decision(request)
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

    def _guidance_active(self, request: DecisionInput) -> bool:
        return any(item.item_id == "supervision" for item in request.context.items)

    def _next_decision(self, request: DecisionInput) -> Decision:
        if self._phase == "stagnation":
            if self._guidance_active(request) and not self._responded:
                self._responded = True
                if self._spec.guidance_response is not GuidanceDecision.REJECT:
                    self._phase = "recovery"
                return self._guidance_decision()
            if (
                self._responded
                and self._spec.guidance_response is GuidanceDecision.REJECT
                and request.attempt_id != self._initial_attempt
            ):
                self._phase = "recovery"
            if self._phase == "stagnation":
                return self._stagnation_decision()
        return self._recovery_decision()

    def _guidance_decision(self) -> Decision:
        reason = f"scripted {self._spec.guidance_response.value} for {self._spec.name}"
        return Decision(
            "guidance", self._spec.guidance_response.value, (("reason", reason),)
        )

    def _stagnation_decision(self) -> Decision:
        if self._opening_index < len(self._spec.opening):
            decision = self._spec.opening[self._opening_index]
            self._opening_index += 1
            return decision
        script = self._spec.stagnation
        decision = script[self._stagnation_index % len(script)]
        self._stagnation_index += 1
        return decision

    def _recovery_decision(self) -> Decision:
        index = min(self._recovery_index, len(RECOVERY_SCRIPT) - 1)
        self._recovery_index += 1
        return RECOVERY_SCRIPT[index]

    def _result_for(self, decision: Decision):
        if decision.kind == "action":
            return ActionChoice(
                action_name=decision.name,
                rationale=f"fixture {self._spec.name} action",
                arguments=decision.resolved_arguments(),
            )
        if decision.kind == "evaluate":
            return EvaluationChoice("evaluate the fixture state")
        return GuidanceResponse(
            GuidanceDecision(decision.name), decision.resolved_arguments()["reason"]
        )


def _supervisor_response(spec: FixtureSpec) -> SupervisorResponse:
    return SupervisorResponse(
        Guidance(
            diagnosis=f"repeated {spec.name} cycle without new information",
            constraints=("avoid repeating the same cycle",),
            suggested_experiments=("focus, repair, and verify the fixture state",),
            expires_after_call=_GUIDANCE_EXPIRY,
        ),
        UsageMeasurement(
            measurement_id=f"phase3-paired-{spec.name}-supervisor-usage",
            model_calls=1,
            input_tokens=_SUPERVISOR_INPUT_TOKENS,
            output_tokens=_SUPERVISOR_OUTPUT_TOKENS,
            duration_ms=0,
        ),
        f"phase3-paired-{spec.name}-supervisor",
    )


def _task(spec: FixtureSpec, seed: int) -> TaskSpec:
    return TaskSpec(
        task_id=f"phase3-paired-{spec.name}-s{seed}",
        objective=f"Reach the fixture goal for {spec.name} (seed {seed}).",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external fixture-evaluator acceptance",
        budgets=BudgetLimits(
            actions=ACTION_BUDGET,
            model_calls=MODEL_CALL_BUDGET,
            evaluations=EVALUATION_BUDGET,
        ),
        environment_id=FixtureEnvironment(spec).environment_id,
        evaluation_contract_id=f"phase3-paired-{spec.name}-evaluator:v1",
    )


def _trigger_calls(events: Sequence[Event]) -> list[int]:
    calls: list[int] = []
    for event in events:
        if event.kind != SUPERVISION_TRIGGERED:
            continue
        call = event.payload.get("call")
        if isinstance(call, int):
            calls.append(call)
    return calls


def _guidance_dispositions(events: Sequence[Event]) -> dict[str, int]:
    counts = {"accepted": 0, "rejected": 0, "expired": 0, "response_rejected": 0}
    mapping = {
        "guidance_accepted": "accepted",
        "guidance_rejected": "rejected",
        "guidance_expired": "expired",
        "guidance_response_rejected": "response_rejected",
    }
    for event in events:
        key = mapping.get(event.kind)
        if key is not None:
            counts[key] += 1
    return counts


def _guidance_decisions(events: Sequence[Event]) -> dict[str, int]:
    counts = {"accept": 0, "reject": 0, "replan": 0}
    for event in events:
        if event.kind not in {"guidance_accepted", "guidance_rejected"}:
            continue
        decision = _as_str(event.payload.get("decision"))
        if decision in counts:
            counts[decision] += 1
    return counts


def _supervisor_usage(events: Sequence[Event]) -> tuple[int, int]:
    calls = sum(1 for event in events if event.kind == "supervisor_intended")
    tokens = 0
    for event in events:
        if event.kind != "guidance_received":
            continue
        usage = _as_mapping(event.payload.get("usage"))
        tokens += _as_int(usage.get("input_tokens")) + _as_int(
            usage.get("output_tokens")
        )
    return calls, tokens


def _actions_to_recovery(events: Sequence[Event], accepted: bool) -> int | None:
    trigger_sequences = [
        event.sequence for event in events if event.kind == SUPERVISION_TRIGGERED
    ]
    if not accepted or not trigger_sequences:
        return None
    onset = min(trigger_sequences)
    return sum(
        1
        for event in events
        if event.kind == "action_intended" and event.sequence >= onset
    )


def _summary(events: Sequence[Event]) -> dict[str, object]:
    counters = derive_counters(events)
    triggers = _trigger_calls(events)
    supervisor_calls, supervisor_tokens = _supervisor_usage(events)
    return {
        "main_calls": sum(1 for event in events if event.kind == "model_decided"),
        "supervisor_calls": supervisor_calls,
        "supervisor_tokens": supervisor_tokens,
        "supervisor_allocation_exceeded": (
            supervisor_calls > SUPERVISOR_CALL_ALLOCATION
            or supervisor_tokens > SUPERVISOR_TOKEN_ALLOCATION
        ),
        "triggers": triggers,
        "guidance": _guidance_dispositions(events),
        "guidance_decisions": _guidance_decisions(events),
        "attempt_abandoned": sum(
            1 for event in events if event.kind == "attempt_abandoned"
        ),
        "attempt_started": sum(
            1 for event in events if event.kind == "attempt_started"
        ),
        "operator_escalated": sum(
            1 for event in events if event.kind == "operator_escalated"
        ),
        "counters": {
            "actions": counters.actions,
            "evaluations": counters.evaluations,
            "model_calls": counters.model_calls,
            "commits": counters.commits,
            "attempts": counters.attempts,
            "reconciliations": counters.reconciliations,
        },
    }


def _run_once(
    spec: FixtureSpec, seed: int, condition: str, run_dir: Path, *, repeat: bool = False
) -> dict[str, object]:
    run_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-repeat" if repeat else ""
    session_id = f"phase3-paired-{spec.name}-s{seed}-{condition}{suffix}"
    run_id = f"{spec.name}/s{seed}/{condition}{suffix}"

    journal = SqliteJournal(run_dir / "session.sqlite3")
    session: Session | None = None
    events: tuple[Event, ...] = ()
    accepted_at: int | None = None
    terminal = "model_budget_exhausted"
    error: str | None = None
    steps = 0
    try:
        runtime = FixtureRuntime(spec)
        evaluator = FixtureEvaluator(spec, runtime)
        environment = FixtureEnvironment(spec)
        model = FixtureModel(spec)
        supervision = None
        if condition == "supervised":
            supervisor = ScriptedSupervisor(
                tuple(_supervisor_response(spec) for _ in range(3))
            )
            supervision = SupervisionCapability(supervisor, POLICY)
        session = Session.create(
            session_id,
            _task(spec, seed),
            journal=journal,
            artifacts=ArtifactStore(run_dir / "artifacts"),
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            supervision=supervision,
        )
        for call in range(1, MAIN_CALL_BUDGET + 1):
            view = session.step(model, token_budget=CONTEXT_TOKEN_BUDGET)
            steps = call
            if view.pending_proposal_id is not None:
                session.reconcile_pending()
                view = session.view()
            if len(session.accepted_lineage()) > 1:
                accepted_at = call
                terminal = "accepted"
                break
            if view.status is SessionStatus.WAITING:
                terminal = "operator_waiting"
                break
            if view.status in {
                SessionStatus.COMPLETED,
                SessionStatus.STOPPED,
                SessionStatus.FAILED,
            }:
                terminal = f"status:{view.status.value}"
                break
    except (TransitionError, ContractError) as exc:
        terminal = "error"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            events = session.events(limit=10000)
        journal.close()
    if session is None:
        raise RuntimeError(f"run {run_id} did not create a session: {error}")

    accepted = accepted_at is not None
    summary: dict[str, object] = {
        "run_id": run_id,
        "session_id": session_id,
        "fixture": spec.name,
        "seed": seed,
        "condition": condition,
        "repeat": repeat,
        "steps": steps,
        "terminal": terminal,
        "error": error,
        "accepted": accepted,
        "accepted_at_call": accepted_at,
        "actions_to_recovery": _actions_to_recovery(events, accepted),
        **_summary(events),
    }
    stream, uncompressed_bytes = _event_stream_bytes(events)
    (run_dir / EVENT_STREAM_NAME).write_bytes(stream)
    journal_path = run_dir / "session.sqlite3"
    wal_sidecars = [
        sidecar
        for sidecar in (
            journal_path.with_name(journal_path.name + "-wal"),
            journal_path.with_name(journal_path.name + "-shm"),
        )
        if sidecar.exists()
    ]
    summary["journal"] = {
        "path": EVENT_STREAM_NAME,
        "format": "canonical-event-stream-gzip-jsonl",
        "sha256": hashlib.sha256(stream).hexdigest(),
        "uncompressed_bytes": uncompressed_bytes,
        "event_count": len(events),
        "wal_sidecars": [sidecar.name for sidecar in wal_sidecars],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    journal_path.unlink()
    for sidecar in wal_sidecars:
        sidecar.unlink()
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


def _decision_json(decision: Decision) -> dict[str, object]:
    return {
        "kind": decision.kind,
        "name": decision.name,
        "arguments": dict(decision.arguments),
    }


def _fixture_manifest() -> dict[str, object]:
    manifest: dict[str, object] = {}
    for name in FIXTURE_CLASSES:
        spec = FIXTURES[name]
        manifest[name] = {
            "guidance_response": spec.guidance_response.value,
            "probe_failure": spec.probe_failure,
            "interrupt_opening": spec.interrupt_opening,
            "success_expected_without_intervention": (
                spec.success_expected_without_intervention
            ),
            "opening": [_decision_json(item) for item in spec.opening],
            "stagnation": [_decision_json(item) for item in spec.stagnation],
        }
    return manifest


def run_experiment(*, force: bool = False) -> dict[str, object]:
    """Generate the paired-recovery bundle from a clean tree."""
    revision, dirty = _git_revision()
    if dirty:
        raise SystemExit(
            "refusing to run with tracked working-tree changes; commit them first"
        )
    if BUNDLE_DIR.exists() and any(BUNDLE_DIR.iterdir()):
        if not force:
            raise SystemExit(f"{BUNDLE_DIR} is not empty; pass --force to regenerate")
        shutil.rmtree(BUNDLE_DIR)
    (BUNDLE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle": "PHASE-0003 item 6 paired recovery",
        "document": "plans/phases/phase-0003-supervision-and-recovery.md",
        "tolerances": "plans/experiments/exp-0001-supervisor-policy.md",
        "source": {
            "git_revision": revision,
            "git_dirty": False,
            "tool": TOOL_RELPATH,
            "tool_sha256": _sha256_file(Path(__file__)),
        },
        "fixtures": _fixture_manifest(),
        "seeds": list(SEEDS),
        "conditions": list(CONDITIONS),
        "policy": {
            "version": POLICY.version,
            "window_calls": POLICY.window_calls,
            "repeat_threshold": POLICY.repeat_threshold,
            "cooldown_calls": POLICY.cooldown_calls,
        },
        "budgets": {
            "main_calls": MAIN_CALL_BUDGET,
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
            "operator escalation",
        ],
        "retention": {
            "per_run": [EVENT_STREAM_NAME, "summary.json"],
            "aggregate": ["manifest.json", "results.json", "report.md", "aggregate.py"],
        },
        "provider_calls": 0,
        "retention_note": (
            "Each run's sqlite journal is compacted to a canonical gzip JSONL "
            "event-stream export; summaries and result tables are derived from "
            "the exported streams."
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
            for condition in CONDITIONS:
                run_dir = BUNDLE_DIR / "runs" / name / f"s{seed}" / condition
                _run_once(spec, seed, condition, run_dir)
                total += 1
            repeat_dir = BUNDLE_DIR / "runs" / name / f"s{seed}" / "unsupervised-repeat"
            _run_once(spec, seed, "unsupervised", repeat_dir, repeat=True)
            total += 1
    results = aggregate_bundle(BUNDLE_DIR)
    results["executions"] = total
    write_aggregate(BUNDLE_DIR, results)
    return results


def _aggregate_script_text() -> str:
    return (
        '"""Recompute the paired-recovery aggregate from the retained runs."""\n'
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
        "from tools.phase3_paired_recovery import write_aggregate  # noqa: E402\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    write_aggregate(BUNDLE)\n"
    )


def _load_summaries(bundle: Path) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for path in sorted(bundle.glob("runs/*/s*/*/summary.json")):
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    return summaries


def _index(summaries: Sequence[dict[str, object]]):
    index: dict[tuple[str, int, str], dict[str, object]] = {}
    for summary in summaries:
        condition = _as_str(summary.get("condition")) + (
            "-repeat" if summary.get("repeat") else ""
        )
        index[
            (_as_str(summary.get("fixture")), _as_int(summary.get("seed")), condition)
        ] = summary
    return index


def _wilson_interval(successes: int, total: int, z: float = 1.959963984540054):
    if total == 0:
        return [0.0, 1.0]
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
        / denominator
    )
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _median(values: Sequence[int]) -> float:
    if not values:
        return float("inf")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


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
    """Recompute the paired-recovery result table from retained summaries."""
    summaries = _load_summaries(bundle)
    index = _index(summaries)

    reproducible = True
    for fixture in FIXTURE_CLASSES:
        for seed in SEEDS:
            base = index.get((fixture, seed, "unsupervised"))
            repeat = index.get((fixture, seed, "unsupervised-repeat"))
            if base is None or repeat is None:
                reproducible = False
                continue
            compare = ("accepted", "accepted_at_call", "steps", "terminal")
            if any(base.get(field) != repeat.get(field) for field in compare):
                reproducible = False

    censored = [
        {
            "run_id": summary.get("run_id"),
            "terminal": summary.get("terminal"),
            "error": summary.get("error"),
        }
        for summary in summaries
        if _as_str(summary.get("terminal"))
        not in {"accepted", "model_budget_exhausted"}
        and _as_str(summary.get("terminal")) != "operator_waiting"
    ]

    improved_pairs = 0
    class_improvements: dict[str, int] = {}
    harms: list[str] = []
    recovery_actions: list[int] = []
    for fixture in STAGNATION_CLASSES:
        improved_in_class = 0
        for seed in SEEDS:
            base = index.get((fixture, seed, "unsupervised"), {})
            candidate = index.get((fixture, seed, "supervised"), {})
            if bool(candidate.get("accepted")) and not bool(base.get("accepted")):
                improved_pairs += 1
                improved_in_class += 1
            if bool(base.get("accepted")) and not bool(candidate.get("accepted")):
                harms.append(f"{fixture}/s{seed}")
            actions = candidate.get("actions_to_recovery")
            if bool(candidate.get("accepted")) and isinstance(actions, int):
                recovery_actions.append(actions)
        class_improvements[fixture] = improved_in_class

    false_triggers: dict[str, int] = {}
    for fixture in CONTROL_CLASSES:
        for seed in SEEDS:
            for condition in ("unsupervised", "supervised"):
                candidate = index.get((fixture, seed, condition), {})
                count = len(_as_list(candidate.get("triggers")))
                if count:
                    false_triggers[fixture] = false_triggers.get(fixture, 0) + count

    supervisor_calls = 0
    supervisor_tokens = 0
    allocation_exceeded = 0
    retriggers_during_recovery = 0
    guidance_totals = {
        "accepted": 0,
        "rejected": 0,
        "expired": 0,
        "response_rejected": 0,
    }
    operator_escalations = 0
    guidance_decisions = {"accept": 0, "reject": 0, "replan": 0}
    for summary in summaries:
        if _as_str(summary.get("condition")) != "supervised":
            continue
        supervisor_calls += _as_int(summary.get("supervisor_calls"))
        supervisor_tokens += _as_int(summary.get("supervisor_tokens"))
        if summary.get("supervisor_allocation_exceeded"):
            allocation_exceeded += 1
        triggers = len(_as_list(summary.get("triggers")))
        if bool(summary.get("accepted")) and triggers > 1:
            retriggers_during_recovery += 1
        guidance = _as_mapping(summary.get("guidance"))
        for key in guidance_totals:
            guidance_totals[key] += _as_int(guidance.get(key))
        decisions = _as_mapping(summary.get("guidance_decisions"))
        for key in guidance_decisions:
            guidance_decisions[key] += _as_int(decisions.get(key))
        operator_escalations += _as_int(summary.get("operator_escalated"))

    total_pairs = len(STAGNATION_CLASSES) * len(SEEDS)
    classes_meeting = len(
        [value for value in class_improvements.values() if value >= 3]
    )
    accepted_tolerance = improved_pairs >= 3 and classes_meeting >= 2

    reasons: list[str] = []
    rejected = False
    if false_triggers.get("productive"):
        rejected = True
        reasons.append(f"{false_triggers['productive']} trigger(s) on productive runs")
    exploring_over_one = sum(
        1
        for seed in SEEDS
        if len(
            _as_list(
                index.get(("plateaued-exploring", seed, "supervised"), {}).get(
                    "triggers"
                )
            )
        )
        > 1
    )
    if exploring_over_one:
        rejected = True
        reasons.append(
            f"{exploring_over_one} plateaued-exploring run(s) with more than "
            "one trigger"
        )
    if allocation_exceeded:
        rejected = True
        reasons.append(
            f"{allocation_exceeded} run(s) exceeded the supervisor allocation"
        )
    if harms:
        reasons.append(f"acceptance harm on {harms}")

    incomplete_class = [
        fixture
        for fixture in FIXTURE_CLASSES
        if any(
            index.get((fixture, seed, condition)) is None
            for seed in SEEDS
            for condition in CONDITIONS
        )
    ]
    inconclusive_reasons: list[str] = []
    if incomplete_class:
        inconclusive_reasons.append(f"missing runs for {incomplete_class}")
    if len(censored) > 1:
        inconclusive_reasons.append(f"{len(censored)} censored run(s)")
    if not reproducible:
        inconclusive_reasons.append("baseline did not reproduce")

    return {
        "bundle": _bundle_label(bundle),
        "runs": len(summaries),
        "fixtures": list(FIXTURE_CLASSES),
        "seeds": list(SEEDS),
        "policy": {
            "version": POLICY.version,
            "window_calls": POLICY.window_calls,
            "repeat_threshold": POLICY.repeat_threshold,
            "cooldown_calls": POLICY.cooldown_calls,
        },
        "baseline_reproducible": reproducible,
        "censored_runs": censored,
        "benefit": {
            "stagnation_pairs_improved": improved_pairs,
            "stagnation_pairs_total": total_pairs,
            "class_improvements": class_improvements,
            "classes_meeting_3_of_5": classes_meeting,
            "recovery_actions_median": _median(recovery_actions),
        },
        "harm": {
            "acceptance_harm_runs": harms,
            "operator_escalations": operator_escalations,
            "retriggers_during_recovery": retriggers_during_recovery,
        },
        "false_triggers": false_triggers,
        "cost": {
            "supervisor_calls": supervisor_calls,
            "supervisor_tokens": supervisor_tokens,
            "supervisor_call_allocation": SUPERVISOR_CALL_ALLOCATION,
            "supervisor_token_allocation": SUPERVISOR_TOKEN_ALLOCATION,
            "allocation_exceeded_runs": allocation_exceeded,
        },
        "guidance": guidance_totals,
        "guidance_decisions": guidance_decisions,
        "uncertainty": {
            "measure": "stagnation-pair benefit fraction",
            "point": (improved_pairs / total_pairs if total_pairs else 0.0),
            "wilson_95": _wilson_interval(improved_pairs, total_pairs),
            "note": (
                "Scripted labeled fixtures; bounds the trigger/recovery separation "
                "on these fixtures, not live-task benefit."
            ),
        },
        "tolerances": {
            "source": "EXP-0001 predeclared paired-sample tolerances",
            "meets_acceptance": accepted_tolerance,
            "rejected": rejected,
            "rejection_reasons": reasons,
            "inconclusive": bool(inconclusive_reasons),
            "inconclusive_reasons": inconclusive_reasons,
            "interpretation": (
                "A run is censored only on an unexpected error; ending at the "
                "predeclared decision budget without acceptance is a valid "
                "negative observation."
            ),
        },
    }


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
    benefit = _as_mapping(results.get("benefit"))
    harm = _as_mapping(results.get("harm"))
    cost = _as_mapping(results.get("cost"))
    uncertainty = _as_mapping(results.get("uncertainty"))
    tolerances = _as_mapping(results.get("tolerances"))
    lines = [
        "# PHASE-0003 item 6 paired-recovery results",
        "",
        f"- Runs: {results.get('runs')}",
        f"- Baseline reproducible: {results.get('baseline_reproducible')}",
        f"- Policy: {_as_mapping(results.get('policy')).get('version')}",
        f"- Stagnation pairs improved: "
        f"{benefit.get('stagnation_pairs_improved')}"
        f"/{benefit.get('stagnation_pairs_total')}",
        f"- Classes meeting 3/5 seeds: {benefit.get('classes_meeting_3_of_5')}/4",
        f"- Acceptance harm runs: {harm.get('acceptance_harm_runs')}",
        f"- Operator escalations: {harm.get('operator_escalations')}",
        f"- Re-triggers during recovery: {harm.get('retriggers_during_recovery')}",
        f"- Supervisor calls: {cost.get('supervisor_calls')} total "
        f"(per-run allocation {cost.get('supervisor_call_allocation')})",
        f"- Supervisor tokens: {cost.get('supervisor_tokens')} total "
        f"(per-run allocation {cost.get('supervisor_token_allocation')})",
        f"- False triggers: {results.get('false_triggers')}",
        f"- Guidance dispositions: {results.get('guidance')}",
        f"- Guidance decisions: {results.get('guidance_decisions')}",
        f"- Benefit fraction (Wilson 95%): {uncertainty.get('point')} "
        f"{uncertainty.get('wilson_95')}",
        f"- Meets acceptance tolerance: {tolerances.get('meets_acceptance')}",
        f"- Rejected: {tolerances.get('rejected')} "
        f"{tolerances.get('rejection_reasons')}",
        f"- Inconclusive: {tolerances.get('inconclusive')} "
        f"{tolerances.get('inconclusive_reasons')}",
        "",
        "Tolerances are EXP-0001's predeclared paired-sample tolerances, reused.",
    ]
    return "\n".join(lines) + "\n"


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
        derived = _summary(events)
        for name in ("main_calls", "supervisor_calls", "supervisor_tokens"):
            if _as_int(summary.get(name)) != _as_int(derived.get(name)):
                problems.append(f"{summary_path}: {name} mismatch")
    try:
        results = json.loads((bundle / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"results.json unreadable: {exc}")
        return problems
    recomputed = aggregate_bundle(bundle)
    for field_name in ("baseline_reproducible", "censored_runs", "benefit", "harm"):
        if results.get(field_name) != recomputed.get(field_name):
            problems.append(f"results field {field_name} is not reproducible")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bundle, recompute aggregates, or verify the bundle."""
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
