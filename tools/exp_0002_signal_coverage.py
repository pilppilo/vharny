"""Run, replay, aggregate, and verify the EXP-0002 signal-coverage experiment.

This is experiment-local scaffolding over public kernel and journal APIs. It
does not change the kernel or PHASE-0003 modules: the EXP-0001 scripted
fixtures are reused unchanged, and the candidate monitor policies P4/P5 are
versioned run policy implemented here, exactly as EXP-0002 declares. The
shipped supervision implementation stays on ``monitor.p1.v1``; the replay
cross-checks the local B0 monitor against it over the verified run2 journal
copy.

Usage::

    uv run python tools/exp_0002_signal_coverage.py manifest
    uv run python tools/exp_0002_signal_coverage.py run
    uv run python tools/exp_0002_signal_coverage.py replay --run2-dir <dir>
    uv run python tools/exp_0002_signal_coverage.py aggregate
    uv run python tools/exp_0002_signal_coverage.py verify
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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence, cast

from vharness.agent import (
    ActionProposal,
    BudgetLimits,
    CompletionMode,
    ComponentIdentity,
    ContractError,
    ExecutionReceipt,
    KnowledgeSource,
    RecordEnvelope,
    Session,
    SessionStatus,
    TaskSpec,
    TransitionError,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json, parse_json_object
from vharness.agent.evidence import counters as derive_counters
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import ModelCallError
from vharness.agent.models import ArtifactRef, Event, ExecutionStatus, JsonValue
from vharness.agent.ports import ReconciliationKind, ReconciliationResult
from vharness.agent.supervision import (
    decision_facts,
    decision_segments,
    evaluate_supervision,
    monitor_policy_p1,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# astroid does not resolve the repository's implicit tools namespace package,
# so the source root is added to sys.path above; the import is exercised by
# the unit tests and every fixture run.
from tools.exp_0001_supervisor_policy import (  # pylint: disable=import-error,wrong-import-position
    CONTROL_CLASSES,
    FIXTURES,
    FIXTURE_CLASSES,
    STAGNATION_CLASSES,
    SEEDS,
    Decision,
    FixtureEnvironment,
    FixtureEvaluator,
    FixtureModel,
    FixtureRuntime,
    FixtureSpec,
    TriggerReport,
    _deliver_operator_cue,
)

# This is a bounded, auditable evidence harness, not a product module.
# pylint: disable=too-many-arguments,too-many-instance-attributes
# pylint: disable=too-many-lines,too-many-locals,too-many-statements
# pylint: disable=too-few-public-methods

BUNDLE_DIR = SOURCE_ROOT / "evidence" / "exp-0002-signal-coverage"
TOOL_RELPATH = "tools/exp_0002_signal_coverage.py"
EVENT_STREAM_NAME = "events.jsonl.gz"
ORACLE_RELPATH = (
    "coordination/diagnostics/acceptance-20261028/review-data/"
    "counterfactual_a_fingerprints.csv"
)

POLICIES = ("B0", "P4", "P5")
CANDIDATES = ("P4", "P5")

MAIN_CALL_BUDGET = 12
ACTION_BUDGET = 24
CONTEXT_TOKEN_BUDGET = 4096
EVALUATION_BUDGET = 3
SUPERVISOR_CALL_ALLOCATION = 4
SUPERVISOR_TOKENS_PER_CALL = 512
SUPERVISOR_TOKEN_ALLOCATION = 2048
OPERATOR_CUE_CALL = 10

WINDOW_CALLS = 8
REPEAT_THRESHOLD = 3
COOLDOWN_CALLS = 4
BURN_FRACTION = 0.6
BURN_COOLDOWN_CALLS = 4

SUCCESSFUL_RECEIPT_STATUSES = ("succeeded", "accepted")
EVIDENCE_ACTION = "exec"

STDOUT_STRICT_PREFIX = "stdout-strict"
STDOUT_NORMALIZED_PREFIX = "stdout-normalized"
ACTION_FINGERPRINT_PREFIX = "action"
RECEIPT_FINGERPRINT_PREFIX = "receipt"
EVALUATION_FINGERPRINT_PREFIX = "evaluation"

RUN2_JOURNAL_SHA256 = "ca9adf8f2efdca1e5dec48de9e9d87a09081df681c82a609c83112512f2719ec"

# Pre-registered run2 expectations (EXP-0002 Controlled change and Run plan;
# review-data/counterfactual_a_fires.csv and counterfactual_b.csv). The burn
# numbers are consumed calls; the replay records both the fire boundary
# (threshold + 1) and the consumed count so the table compares like for like.
PRE_REGISTRATION: Mapping[str, Mapping[str, object]] = {
    "B0": {"repetition": [], "burn_consumed": []},
    "P4": {
        "repetition": [12, 17],
        "burn_consumed": [24, 28, 32, 36],
    },
    "P5": {
        "repetition": [12, 17],
        "burn_consumed": [],
        "burn_pre_registration": "B2 must NOT fire on run2; an unexpected fire "
        "falsifies the pre-registration and is recorded while P5 remains "
        "admissible (EXP-0002 Tolerances).",
    },
}

POLICY_PARAMETERS: Mapping[str, Mapping[str, object]] = {
    "B0": {"kind": "operator-only", "operator_cue_call": OPERATOR_CUE_CALL},
    "P4": {
        "kind": "carried-families + stdout-evidence + claim-based-burn",
        "version": "monitor.p4.v1",
        "window_calls": WINDOW_CALLS,
        "repeat_threshold": REPEAT_THRESHOLD,
        "cooldown_calls": COOLDOWN_CALLS,
        "burn_fraction": BURN_FRACTION,
        "burn_cooldown_calls": BURN_COOLDOWN_CALLS,
    },
    "P5": {
        "kind": "carried-families + stdout-evidence + claim-free-burn",
        "version": "monitor.p5.v1",
        "window_calls": WINDOW_CALLS,
        "repeat_threshold": REPEAT_THRESHOLD,
        "cooldown_calls": COOLDOWN_CALLS,
        "burn_fraction": BURN_FRACTION,
        "burn_cooldown_calls": BURN_COOLDOWN_CALLS,
    },
}

BURN_SEMANTICS = {
    "formula": (
        "fire at decision boundary c when (c - 1) - last_progress_call >= "
        "ceil(burn_fraction * model_call_budget); consumed calls at fire = c - 1"
    ),
    "boundary_convention": (
        "boundary c is evaluated with completed decisions 1..c-1, matching "
        "evaluate_supervision; the pre-registered burn numbers are consumed "
        "calls (review-data/counterfactual_b.csv consumed_at_fire), so the "
        "pre-registered consumed 24/28/32/36 correspond to boundaries "
        "25/29/33/37"
    ),
    "B1_progress": (
        "claim-based: a call is progress-bearing when a supported "
        "progress_claim_recorded or knowledge_recorded event is recorded in it "
        "(the EXP-0001 definition, deliberately retaining the run2 finding 1.2 "
        "bookkeeping dependency)"
    ),
    "B2_progress": (
        "claim-free: a call is progress-bearing when, since the last "
        "progress-bearing call, the agent received at least one successful "
        "receipt whose R1 or R2 fingerprint had not been seen before in the "
        "attempt (streak semantics; the cumulative reading would keep B2 "
        "silent forever after any early exploration)"
    ),
    "cooldown": (
        "one shared cooldown across a policy's repetition and burn triggers; "
        "after any trigger the policy re-arms after the cooldown calls"
    ),
}

NORMALIZATION_RULE = (
    "R2 (stdout_normalized): drop leading HTTP transport header blocks (from a "
    "line starting with 'HTTP/' through the following blank line), drop lines "
    "consisting solely of '-' echo markers, lowercase, and collapse whitespace "
    "with single spaces; the empty payload stays the empty digest"
)

FIXTURE_OUTPUTS: Mapping[str, str] = {
    "inspect": "inspect: fixture baseline recorded",
    "focus": "focus: corrective target isolated",
    "repair": "repair: corrective change applied",
    "verify": "verify: corrective change confirmed",
    "revert": "revert: fixture returned to initial stage",
}

FIXTURE_OUTPUT_NOTE = (
    "Fixture exec receipts attach a stdout payload artifact so R1/R2 have "
    "evidence to fingerprint. Outputs mirror each class's defining behavior: "
    "stagnation classes repeat identical evidence across calls (stalled and "
    "repetitive probe the same target, which returns the run2-shaped empty "
    "stdout; regressing cycles the same focus/revert outputs), while control "
    "classes produce fresh evidence on every call (plateaued-exploring probes "
    "distinct targets; productive acts once through its goal script)."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


# --- Fixture stdout payloads -------------------------------------------------


def _exec_output(action: str, arguments: Mapping[str, object]) -> str:
    """Return the scripted stdout payload for one fixture action.

    Outputs mirror each class's defining behavior: stagnation scripts repeat
    identical evidence (the default probe target returns the run2-shaped empty
    stdout), control scripts produce fresh evidence on every call.
    """
    fixed = FIXTURE_OUTPUTS.get(action)
    if fixed is not None:
        return fixed
    if action == "probe":
        target = _as_str(arguments.get("target"))
        if not target or target == "a":
            return ""
        if target == "interrupted":
            return "interrupted probe settled"
        return f"probe {target}: fresh observation {target}"
    return ""


class OutputFixtureRuntime(FixtureRuntime):
    """Scripted fixture runtime that attaches stdout payload artifacts.

    Stage transitions and receipts are the EXP-0001 mechanics inherited from
    :class:`FixtureRuntime`; successful receipts additionally carry a
    ``raw_artifact`` holding the scripted stdout payload so the evidence
    fingerprints have content to hash.
    """

    def __init__(self, spec: FixtureSpec, artifacts: ArtifactStore) -> None:
        super().__init__(spec)
        self._artifacts = artifacts
        self._pending: dict[str, ActionProposal] = {}

    def identity(self) -> ComponentIdentity:
        """Return the experiment-scoped runtime identity."""
        return ComponentIdentity(f"exp0002-{self._spec.name}-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Apply one proposal and attach its stdout payload on success."""
        receipt = super().submit(proposal)
        if receipt.status == ExecutionStatus.RUNNING:
            self._pending[receipt.proposal_id] = proposal
            return receipt
        return self._with_output(receipt, proposal)

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Resolve an interrupted probe and attach its stdout payload."""
        result = super().reconcile(operation_id)
        receipt = result.receipt
        if (
            result.kind != ReconciliationKind.RECEIPT
            or receipt is None
            or receipt.status != ExecutionStatus.SUCCEEDED
            or receipt.raw_artifact is not None
        ):
            return result
        proposal = self._pending.pop(receipt.proposal_id, None)
        if proposal is None:
            return result
        attached = self._with_output(receipt, proposal)
        return ReconciliationResult(result.kind, attached)

    def _with_output(
        self, receipt: ExecutionReceipt, proposal: ActionProposal
    ) -> ExecutionReceipt:
        if receipt.status != ExecutionStatus.SUCCEEDED or receipt.raw_artifact:
            return receipt
        output = _exec_output(proposal.action_name, proposal.arguments)
        artifact = self._artifacts.put(
            output.encode("utf-8"),
            media_type="text/plain",
            provenance="fixture-exec-stdout",
        )
        return replace(receipt, raw_artifact=artifact)


class OutputFixtureEnvironment(FixtureEnvironment):
    """Fixture environment scoped to the EXP-0002 experiment IDs."""

    @property
    def environment_id(self) -> str:
        """Return the experiment-scoped environment ID."""
        return f"exp0002-{self._spec.name}"


class OutputFixtureEvaluator(FixtureEvaluator):
    """Fixture evaluator scoped to the EXP-0002 experiment IDs."""

    def identity(self) -> ComponentIdentity:
        """Return the experiment-scoped evaluator identity."""
        return ComponentIdentity(f"exp0002-{self._spec.name}-evaluator", "v1")


# --- Evidence fingerprints ---------------------------------------------------


def _normalize_stdout(text: str) -> str:
    """Normalize one stdout payload per the frozen NORMALIZATION_RULE."""
    lines = text.splitlines()
    kept: list[str] = []
    index = 0
    if lines and lines[0].startswith("HTTP/"):
        index = 1
        while index < len(lines) and lines[index].strip():
            index += 1
        if index < len(lines):
            index += 1
    for line in lines[index:]:
        if line.strip() and set(line.strip()) == {"-"}:
            continue
        kept.append(line)
    return _normalize("\n".join(kept))


def _artifact_ref(payload: object) -> ArtifactRef | None:
    """Parse one serialized ArtifactRef mapping, or return None."""
    mapping = _as_mapping(payload)
    digest = _as_str(mapping.get("digest"))
    if len(digest) != 64:
        return None
    return ArtifactRef(
        digest=digest,
        size=_as_int(mapping.get("size")),
        media_type=_as_str(mapping.get("media_type"), "application/octet-stream"),
        provenance=_as_str(mapping.get("provenance"), "unknown"),
    )


def _envelope_stdout(payload: bytes, store: ArtifactStore) -> bytes | None:
    """Extract the stdout payload from a run2-style receipt envelope.

    The envelope references per-stream artifacts by digest; the stdout payload
    is the concatenation of the ``stream == "stdout"`` entries in offset
    order, or the envelope's inline ``stdout`` field (possibly empty) when the
    exec recorded no stdout artifact entries. Returns None when the payload is
    not such an envelope, in which case the artifact content itself is the
    stdout payload (the fixture shape).
    """
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    entries = envelope.get("output_artifacts")
    if not isinstance(entries, list):
        return None
    chunks: list[tuple[int, bytes]] = []
    for entry in entries:
        mapping = _as_mapping(entry)
        if _as_str(mapping.get("stream")) != "stdout":
            continue
        reference = _artifact_ref(mapping.get("artifact"))
        if reference is None:
            continue
        chunks.append((_as_int(mapping.get("offset")), store.read(reference)))
    if chunks:
        chunks.sort(key=lambda item: item[0])
        return b"".join(chunk for _, chunk in chunks)
    inline = envelope.get("stdout")
    return inline.encode("utf-8") if isinstance(inline, str) else b""


def _stdout_fingerprints(
    receipt: Mapping[str, object], store: ArtifactStore
) -> tuple[str, ...]:
    """Return the R1/R2 fingerprints for one successful receipt."""
    reference = _artifact_ref(receipt.get("raw_artifact"))
    if reference is None:
        return ()
    payload = store.read(reference)
    stdout = _envelope_stdout(payload, store)
    if stdout is None:
        stdout = payload
    strict = f"{STDOUT_STRICT_PREFIX}:{hashlib.sha256(stdout).hexdigest()}"
    text = stdout.decode("utf-8", errors="replace")
    normalized_digest = hashlib.sha256(
        _normalize_stdout(text).encode("utf-8")
    ).hexdigest()
    normalized = f"{STDOUT_NORMALIZED_PREFIX}:{normalized_digest}"
    return (strict, normalized)


@dataclass(frozen=True, slots=True)
class StepFacts:
    """Journal-derived facts for one completed model decision.

    ``carried_fingerprints`` are the shipped ``monitor.p1.v1`` fingerprints
    (action arguments, failed receipts, non-accepted evaluation dispositions
    including the synthetic ``budget exhausted`` fingerprint), derived with the
    shipped :func:`decision_facts`. ``evidence_fingerprints`` are the R1/R2
    stdout digests of this call's successful evidence-bearing receipts.
    """

    call: int
    carried_fingerprints: tuple[str, ...]
    evidence_fingerprints: tuple[str, ...]
    actions: int
    failed_receipts: int
    input_tokens: int
    progress_signals: int

    @property
    def fingerprints(self) -> tuple[str, ...]:
        """Return every fingerprint this decision contributed."""
        return self.carried_fingerprints + self.evidence_fingerprints


def _step_facts(
    events: Sequence[Event],
    call: int,
    store: ArtifactStore,
    evidence_actions: frozenset[str] | None,
) -> StepFacts:
    """Derive one decision's facts, including the evidence fingerprints.

    ``evidence_actions`` restricts R1/R2 to receipts of the named actions
    (``{"exec"}`` for the run2 replay per EXP-0002); ``None`` treats every
    successful receipt as evidence-bearing (the fixture shape, where every
    scripted action returns a stdout payload).
    """
    base = decision_facts(events, call)
    intended: dict[str, str] = {}
    for event in events:
        if event.kind != "action_intended":
            continue
        proposal = _as_mapping(event.payload.get("proposal"))
        proposal_id = _as_str(proposal.get("proposal_id"))
        name = _as_str(proposal.get("action_name"))
        if proposal_id and name:
            intended[proposal_id] = name
    evidence: list[str] = []
    for event in events:
        if event.kind != "receipt_received":
            continue
        receipt = _as_mapping(event.payload.get("receipt"))
        if _as_str(receipt.get("status")) not in SUCCESSFUL_RECEIPT_STATUSES:
            continue
        if (
            evidence_actions is not None
            and intended.get(_as_str(receipt.get("proposal_id")))
            not in evidence_actions
        ):
            continue
        evidence.extend(_stdout_fingerprints(receipt, store))
    return StepFacts(
        call=call,
        carried_fingerprints=base.fingerprints,
        evidence_fingerprints=tuple(evidence),
        actions=base.actions,
        failed_receipts=base.failed_receipts,
        input_tokens=base.input_tokens,
        progress_signals=base.supported_progress,
    )


# --- Candidate monitors ------------------------------------------------------


def _repetition_hit(
    facts: Sequence[StepFacts],
    window: int,
    threshold: int,
    *,
    include_evidence: bool,
) -> tuple[str, int] | None:
    """Return the most-repeated fingerprint in the window past the threshold.

    B0 counts only the carried ``monitor.p1.v1`` fingerprints; P4/P5 count the
    carried fingerprints together with the R1/R2 evidence fingerprints.
    """
    counts: Counter[str] = Counter()
    for fact in facts[-window:]:
        counts.update(
            fact.fingerprints if include_evidence else fact.carried_fingerprints
        )
    if not counts:
        return None
    fingerprint, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    if count >= threshold:
        return fingerprint, count
    return None


class CandidateMonitor:
    """Versioned candidate monitor policy evaluated at each decision boundary.

    B0 is the shipped ``monitor.p1.v1`` repetition policy over the carried
    fingerprints. P4 adds the R1/R2 evidence fingerprints to the repetition
    window and the claim-based B1 burn trigger; P5 is identical with the
    claim-free B2 burn trigger. One cooldown timer is shared across a
    policy's triggers.
    """

    def __init__(self, policy: str, call_budget: int) -> None:
        if policy not in POLICY_PARAMETERS:
            raise ContractError(f"unknown monitor policy {policy!r}")
        self._policy = policy
        self._call_budget = call_budget
        self._last_fire: int | None = None
        self._seen_evidence: set[str] = set()
        self._last_evidence_call = 0

    @property
    def policy(self) -> str:
        """Return the policy identifier."""
        return self._policy

    def burn_threshold(self) -> int:
        """Return the burn trigger's consumed-call threshold."""
        fraction = _as_float(
            POLICY_PARAMETERS[self._policy].get("burn_fraction"), BURN_FRACTION
        )
        return math.ceil(fraction * self._call_budget)

    def observe(self, facts: Sequence[StepFacts], call: int) -> TriggerReport | None:
        """Return a trigger report when the policy fires before this call."""
        self._observe_evidence(facts)
        if self._last_fire is not None and call - self._last_fire < COOLDOWN_CALLS:
            return None
        reason: str | None = None
        fingerprint: str | None = None
        repeats: int | None = None
        hit = _repetition_hit(
            facts,
            WINDOW_CALLS,
            REPEAT_THRESHOLD,
            include_evidence=self._policy != "B0",
        )
        if hit is not None:
            reason, fingerprint, repeats = "repetition", hit[0], hit[1]
        if reason is None and self._policy != "B0":
            last_progress = self._last_progress_call(facts)
            if (call - 1) - last_progress >= self.burn_threshold():
                reason = (
                    "budget-burn-claim"
                    if self._policy == "P4"
                    else "budget-burn-evidence"
                )
        if reason is None:
            return None
        self._last_fire = call
        counted = facts[-WINDOW_CALLS:] if reason == "repetition" else facts
        return TriggerReport(
            policy=self._policy,
            call=call,
            reason=reason,
            fingerprint=fingerprint,
            repeat_count=repeats,
            actions_used=sum(fact.actions for fact in counted),
            tokens_used=sum(fact.input_tokens for fact in counted),
        )

    def _observe_evidence(self, facts: Sequence[StepFacts]) -> None:
        """Track first-seen evidence fingerprints for the B2 streak."""
        for fact in facts:
            fresh = [
                fingerprint
                for fingerprint in fact.evidence_fingerprints
                if fingerprint not in self._seen_evidence
            ]
            if not fresh:
                continue
            self._seen_evidence.update(fresh)
            self._last_evidence_call = max(self._last_evidence_call, fact.call)

    def _last_progress_call(self, facts: Sequence[StepFacts]) -> int:
        """Return the last progress-bearing call under this policy's definition."""
        if self._policy == "P4":
            for fact in reversed(facts):
                if fact.progress_signals:
                    return fact.call
            return 0
        return self._last_evidence_call


# --- Fixture task and runner -------------------------------------------------


def _knowledge_source(spec: FixtureSpec, session_id: str) -> KnowledgeSource:
    source_id = f"exp0002-{spec.name}-brief"
    observed_at = _now()
    return KnowledgeSource(
        source_id=source_id,
        provenance="fixture brief",
        observed_at=observed_at,
        envelope=RecordEnvelope(source_id, session_id, 1, observed_at, "fixture brief"),
    )


def _task(spec: FixtureSpec, seed: int) -> TaskSpec:
    return TaskSpec(
        task_id=f"exp0002-{spec.name}-s{seed}",
        objective=f"Reach the fixture goal for {spec.name} (seed {seed}).",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external fixture-evaluator acceptance",
        budgets=BudgetLimits(
            actions=ACTION_BUDGET,
            model_calls=MAIN_CALL_BUDGET,
            evaluations=EVALUATION_BUDGET,
        ),
        environment_id=f"exp0002-{spec.name}",
        evaluation_contract_id=f"exp0002-{spec.name}-evaluator:v1",
    )


def _facts_json(facts: Sequence[StepFacts]) -> list[dict[str, object]]:
    return [
        {
            "call": fact.call,
            "carried_fingerprints": list(fact.carried_fingerprints),
            "evidence_fingerprints": list(fact.evidence_fingerprints),
            "actions": fact.actions,
            "failed_receipts": fact.failed_receipts,
            "input_tokens": fact.input_tokens,
            "progress_signals": fact.progress_signals,
        }
        for fact in facts
    ]


def _run_once(  # pylint: disable=too-many-branches
    spec: FixtureSpec,
    seed: int,
    policy: str,
    run_dir: Path,
    *,
    repeat: bool = False,
) -> dict[str, object]:
    run_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-repeat" if repeat else ""
    session_id = f"exp0002-{spec.name}-s{seed}-{policy.lower()}{suffix}"
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
        runtime = OutputFixtureRuntime(spec, artifacts)
        evaluator = OutputFixtureEvaluator(spec, runtime)
        environment = OutputFixtureEnvironment(spec)
        source = _knowledge_source(spec, session_id)
        model = FixtureModel(spec, seed, source.source_id)
        monitor = CandidateMonitor(policy, MAIN_CALL_BUDGET) if policy != "B0" else None
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
                policy == "B0"
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
            facts.append(
                _step_facts(new_events, call, artifacts, evidence_actions=None)
            )
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
        "facts": _facts_json(facts),
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
        "artifacts": {
            "path": "artifacts",
            "retained": True,
            "file_count": sum(1 for _ in artifacts.root.rglob("*") if _.is_file()),
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


# --- Run2 replay -------------------------------------------------------------


def verify_artifact_store_copy(store_dir: Path) -> list[str]:
    """Verify every content object against its content-addressed name.

    Only files in the ``<2 hex>/<62 hex>`` layout are content objects; other
    entries (for example the store's ``.journals`` registry) are metadata and
    are skipped.
    """
    problems: list[str] = []
    for path in sorted(store_dir.glob("*/*")):
        if not path.is_file():
            continue
        digest = path.parent.name + path.name
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            problems.append(f"{path}: not content-addressed")
            continue
        if _sha256_file(path) != digest:
            problems.append(f"{path}: content hash mismatch")
    return problems


def _load_run2_events(journal_path: Path) -> tuple[Event, ...]:
    """Load the verified run2 journal copy through the public journal API."""
    journal = SqliteJournal(journal_path, read_only=True)
    try:
        session_ids = journal.session_ids()
        if len(session_ids) != 1:
            raise ContractError(
                f"run2 journal holds {len(session_ids)} sessions; expected 1"
            )
        return journal.events(session_ids[0], limit=100000)
    finally:
        journal.close()


def _run2_call_budget(events: Sequence[Event]) -> int:
    """Return run2's main model-call budget from the journaled task."""
    for event in events:
        if event.kind != "session_created":
            continue
        task = _as_mapping(event.payload.get("task"))
        budgets = _as_mapping(task.get("budgets"))
        model_calls = _as_int(budgets.get("model_calls"))
        if model_calls:
            return model_calls
    raise ContractError("run2 journal lacks a session_created task budget")


def _decision_prefixes(
    events: Sequence[Event],
) -> tuple[list[Event], ...]:
    """Return the journal prefix through the end of each decision segment."""
    segments = decision_segments(events)
    ends: list[int] = []
    cursor = -1
    for segment in segments:
        cursor = segment[-1].sequence
        ends.append(cursor)
    prefixes: list[list[Event]] = []
    for end in ends:
        prefixes.append([event for event in events if event.sequence <= end])
    return tuple(prefixes)


def _load_oracle() -> dict[int, dict[str, str]]:
    """Load the committed per-call fingerprint oracle, keyed by call."""
    oracle_path = SOURCE_ROOT / ORACLE_RELPATH
    if not oracle_path.exists():
        return {}
    rows: dict[int, dict[str, str]] = {}
    lines = oracle_path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split(",")
        row = dict(zip(header, values, strict=True))
        call = row.get("call", "")
        if not call.strip().isdigit():
            continue
        rows[int(call)] = row
    return rows


def replay_run2(run2_dir: Path, bundle: Path) -> dict[str, object]:
    """Replay B0/P4/P5 over the verified run2 journal and artifact copies."""
    journal_path = run2_dir / "session.sqlite3"
    if not journal_path.exists():
        raise SystemExit(f"{journal_path} does not exist")
    observed_sha256 = _sha256_file(journal_path)
    if observed_sha256 != RUN2_JOURNAL_SHA256:
        raise SystemExit(
            f"run2 journal copy sha256 {observed_sha256} does not verify "
            f"against the pre-registered {RUN2_JOURNAL_SHA256}; stop the "
            "replay slice per the EXP-0002 handoff"
        )
    store_dir = run2_dir / "artifacts"
    store_problems = (
        verify_artifact_store_copy(store_dir) if store_dir.exists() else ["missing"]
    )
    if store_problems:
        raise SystemExit(
            f"run2 artifact-store copy did not verify ({len(store_problems)} "
            "problems, first: " + store_problems[0] + "); stop the replay "
            "slice per the EXP-0002 handoff"
        )
    store = ArtifactStore(store_dir)
    events = _load_run2_events(journal_path)
    budget = _run2_call_budget(events)
    segments = decision_segments(events)
    prefixes = _decision_prefixes(events)
    # Facts are derived per decision segment (the shipped facts_history
    # contract); the full journal prefixes exist only for the shipped
    # evaluate_supervision cross-check, which segments internally.
    decision_facts_list = [
        _step_facts(segment, call, store, frozenset({EVIDENCE_ACTION}))
        for call, segment in enumerate(segments, start=1)
    ]

    monitors = {
        policy: CandidateMonitor(policy, budget) for policy in ("B0", "P4", "P5")
    }
    replay_rows: dict[str, list[dict[str, object]]] = {
        policy: [] for policy in ("B0", "P4", "P5")
    }
    fingerprint_rows: list[dict[str, object]] = []
    oracle = _load_oracle()
    for boundary in range(2, len(prefixes) + 2):
        prefix = prefixes[boundary - 2]
        shipped_facts = decision_facts_list[: boundary - 1]
        # B0 is monitor.p1.v1 by definition; the kernel default moved to
        # monitor.p5.v1 (ADR-0011) and must not change this cross-check.
        shipped = evaluate_supervision(prefix, boundary, monitor_policy_p1())
        if shipped is not None:
            replay_rows["B0"].append(
                {
                    "call": boundary,
                    "consumed_calls": boundary - 1,
                    "reason": shipped.cause,
                    "fingerprint": shipped.fingerprint,
                    "repeat_count": shipped.repeat_count,
                    "source": "shipped-evaluate_supervision",
                }
            )
        local = CandidateMonitor("B0", budget).observe(tuple(shipped_facts), boundary)
        if local is not None:
            row = local.to_json()
            row["source"] = "local-B0"
            replay_rows["B0"].append(row)
        for policy in ("P4", "P5"):
            report = monitors[policy].observe(tuple(shipped_facts), boundary)
            if report is not None:
                replay_rows[policy].append(report.to_json())
        if boundary - 1 <= len(decision_facts_list):
            latest = decision_facts_list[boundary - 2]
            if latest.evidence_fingerprints:
                strict = next(
                    (
                        fingerprint
                        for fingerprint in latest.evidence_fingerprints
                        if fingerprint.startswith(STDOUT_STRICT_PREFIX)
                    ),
                    "",
                )
                normalized = next(
                    (
                        fingerprint
                        for fingerprint in latest.evidence_fingerprints
                        if fingerprint.startswith(STDOUT_NORMALIZED_PREFIX)
                    ),
                    "",
                )
                row = oracle.get(boundary - 1, {})
                fingerprint_rows.append(
                    {
                        "call": boundary - 1,
                        "stdout_strict": strict,
                        "stdout_normalized": normalized,
                        "oracle_strict": row.get("stdout_strict_sha256", ""),
                        "oracle_normalized": row.get("stdout_normalized_sha256", ""),
                        "strict_matches": bool(row)
                        and strict.endswith(row.get("stdout_strict_sha256", "")),
                        "normalized_matches": bool(row)
                        and normalized.endswith(
                            row.get("stdout_normalized_sha256", "")
                        ),
                    }
                )

    summary = _replay_summary(replay_rows, fingerprint_rows, budget, len(prefixes))
    summary["journal"] = {
        "path": str(journal_path),
        "sha256": observed_sha256,
        "expected_sha256": RUN2_JOURNAL_SHA256,
        "verified": True,
        "event_count": len(events),
    }
    summary["artifact_store"] = {
        "path": str(store_dir),
        "files_verified": sum(1 for path in store_dir.rglob("*") if path.is_file()),
        "problems": store_problems,
    }
    replay_dir = bundle / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    for policy in ("B0", "P4", "P5"):
        _write_boundary_csv(
            replay_dir / f"{policy.lower()}-boundaries.csv", replay_rows[policy]
        )
    _write_fingerprint_csv(replay_dir / "stdout-fingerprints.csv", fingerprint_rows)
    (replay_dir / "replay-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _write_boundary_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    fields = (
        "call",
        "consumed_calls",
        "reason",
        "fingerprint",
        "repeat_count",
        "source",
    )
    _write_csv(path, fields, rows)


def _write_fingerprint_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    fields = (
        "call",
        "stdout_strict",
        "stdout_normalized",
        "oracle_strict",
        "oracle_normalized",
        "strict_matches",
        "normalized_matches",
    )
    _write_csv(path, fields, rows)


def _write_csv(
    path: Path, fields: tuple[str, ...], rows: Sequence[Mapping[str, object]]
) -> None:
    def cell(value: object) -> str:
        text = value if isinstance(value, str) else repr(value)
        return text.replace(",", ";")

    lines = [",".join(fields)]
    for row in rows:
        lines.append(",".join(cell(row.get(field)) for field in fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _replay_summary(
    replay_rows: Mapping[str, Sequence[Mapping[str, object]]],
    fingerprint_rows: Sequence[Mapping[str, object]],
    budget: int,
    decisions: int,
) -> dict[str, object]:
    """Compare the replay tables against the pre-registration."""
    policies: dict[str, dict[str, object]] = {}
    for policy in ("B0", "P4", "P5"):
        expected_repetition = [
            _as_int(value)
            for value in _as_list(PRE_REGISTRATION[policy].get("repetition"))
        ]
        expected_burn = [
            _as_int(value) + 1
            for value in _as_list(PRE_REGISTRATION[policy].get("burn_consumed"))
        ]
        observed_repetition = [
            _as_int(row.get("call"))
            for row in replay_rows[policy]
            if row.get("reason") == "repetition"
            and _as_str(row.get("source")) != "shipped-evaluate_supervision"
        ]
        observed_burn = [
            _as_int(row.get("call"))
            for row in replay_rows[policy]
            if _as_str(row.get("reason")).startswith("budget-burn")
        ]
        missing = sorted(set(expected_repetition) - set(observed_repetition)) + sorted(
            set(expected_burn) - set(observed_burn)
        )
        extra = sorted(
            set(observed_repetition) - set(expected_repetition) - set(expected_burn)
        ) + sorted(set(observed_burn) - set(expected_burn))
        entry: dict[str, object] = {
            "expected_repetition_boundaries": expected_repetition,
            "expected_burn_boundaries": expected_burn,
            "expected_burn_consumed": list(
                _as_list(PRE_REGISTRATION[policy].get("burn_consumed"))
            ),
            "observed_repetition_boundaries": observed_repetition,
            "observed_burn_boundaries": observed_burn,
            "observed_burn_consumed": [boundary - 1 for boundary in observed_burn],
            "observed_triggers": [
                row
                for row in replay_rows[policy]
                if _as_str(row.get("source")) != "shipped-evaluate_supervision"
            ],
            "missing_pre_registered_fires": missing,
            "extra_fires": extra,
        }
        if policy == "B0":
            entry["shipped_cross_check_triggers"] = [
                row
                for row in replay_rows[policy]
                if _as_str(row.get("source")) == "shipped-evaluate_supervision"
            ]
        if policy == "P5":
            entry["b2_pre_registration_falsified"] = bool(observed_burn)
        policies[policy] = entry
    compared = [row for row in fingerprint_rows if row.get("oracle_strict")]
    strict_mismatched = [
        _as_int(row.get("call"))
        for row in compared
        if not bool(row.get("strict_matches"))
    ]
    normalized_mismatched = [
        _as_int(row.get("call"))
        for row in compared
        if not bool(row.get("normalized_matches"))
    ]
    return {
        "budget": {"model_calls": budget, "decisions": decisions},
        "policies": policies,
        "stdout_fingerprints": {
            "oracle": ORACLE_RELPATH,
            "rows_compared": len(compared),
            "strict_matched": len(compared) - len(strict_mismatched),
            "normalized_matched": len(compared) - len(normalized_mismatched),
            "strict_mismatched_calls": strict_mismatched,
            "normalized_mismatched_calls": normalized_mismatched,
        },
    }


# --- Manifest, aggregate, verify --------------------------------------------


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
        "claim_after": decision.claim_after,
    }


def _fixture_manifest() -> dict[str, object]:
    manifest: dict[str, object] = {}
    for name in FIXTURE_CLASSES:
        spec = FIXTURES[name]
        outputs = [
            {
                "decision": _decision_json(item),
                "stdout": _exec_output(item.name, item.resolved_arguments()),
            }
            for item in (*spec.opening, *spec.plateau)
            if item.kind == "action"
        ]
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
            "outputs": outputs,
            "output_note": FIXTURE_OUTPUT_NOTE,
        }
    return manifest


def build_manifest() -> dict[str, object]:
    """Return the frozen run manifest for review and execution."""
    revision, dirty = _git_revision()
    return {
        "experiment": "EXP-0002",
        "document": "plans/experiments/exp-0002-supervisor-signal-coverage.md",
        "source": {
            "git_revision": revision,
            "git_dirty": dirty,
            "tool": TOOL_RELPATH,
            "tool_sha256": _sha256_file(Path(__file__)),
        },
        "fixtures": _fixture_manifest(),
        "seeds": list(SEEDS),
        "policies": {name: POLICY_PARAMETERS[name] for name in POLICIES},
        "signal_definitions": {
            "carried_families": (
                "action-argument, failed-receipt, and non-accepted "
                "evaluation-disposition fingerprints including the synthetic "
                "'budget exhausted' fingerprint for budget-rejected dispatches, "
                "derived with the shipped supervision.decision_facts"
            ),
            "R1": (
                "sha256 of a successful exec receipt's stdout payload exactly "
                "as received (the review's stdout_strict variant); stdout is "
                "extracted from the receipt envelope's output_artifacts entries "
                "with stream 'stdout' in offset order, or is the artifact "
                "content itself when no envelope is present"
            ),
            "R2": (
                "the same stdout payloads after the normalization rule below "
                "(the review's stdout_normalized variant)"
            ),
            "normalization_rule": NORMALIZATION_RULE,
            "burn_semantics": BURN_SEMANTICS,
        },
        "budgets": {
            "model_calls": MAIN_CALL_BUDGET,
            "actions": ACTION_BUDGET,
            "context_tokens": CONTEXT_TOKEN_BUDGET,
            "evaluations": EVALUATION_BUDGET,
            "supervisor_calls": SUPERVISOR_CALL_ALLOCATION,
            "supervisor_tokens": SUPERVISOR_TOKEN_ALLOCATION,
            "supervisor_tokens_per_call": SUPERVISOR_TOKENS_PER_CALL,
        },
        "run2_replay": {
            "journal_sha256": RUN2_JOURNAL_SHA256,
            "pre_registration": PRE_REGISTRATION,
            "provider_calls": 0,
        },
        "executions": {
            "paired": "6 classes x 5 seeds x {B0, P4, P5} = 90 runs",
            "baseline_repeats": "30 B0 re-executions",
            "total": 120,
        },
        "terminal_conditions": [
            "external fixture-evaluator acceptance",
            "decision-budget end without acceptance",
        ],
        "retention": {
            "per_run": [
                EVENT_STREAM_NAME,
                "summary.json",
                "triggers.json",
                "artifacts/",
            ],
            "replay": [
                "replay/b0-boundaries.csv",
                "replay/p4-boundaries.csv",
                "replay/p5-boundaries.csv",
                "replay/stdout-fingerprints.csv",
                "replay/replay-summary.json",
            ],
            "aggregate": ["manifest.json", "results.json", "report.md", "aggregate.py"],
        },
        "retention_note": (
            "Each run's sqlite journal is compacted to a canonical gzip JSONL "
            "event-stream export with the source journal hash recorded; the "
            "artifact payloads are retained so R1/R2 fingerprints recompute "
            "from the bundle; summaries and tables are derived from the "
            "exported streams."
        ),
    }


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
    """Recompute the EXP-0002 result table from retained run summaries."""
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
            base = index.get((fixture, seed, "B0"))
            repeat = index.get((fixture, seed, "B0-repeat"))
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

    replay: dict[str, object] = {}
    replay_path = bundle / "replay" / "replay-summary.json"
    if replay_path.exists():
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay_policies = _as_mapping(replay.get("policies"))

    candidates: dict[str, dict[str, object]] = {}
    for policy in CANDIDATES:
        improved_pairs = 0
        class_improvements: dict[str, int] = {}
        false_productive = 0
        false_exploring: dict[str, int] = {}
        supervisor_calls = 0
        supervisor_tokens = 0
        allocation_exceeded = 0
        latencies: list[int] = []
        harms: list[str] = []
        exhausted_by_class: dict[str, int] = {}
        for fixture in STAGNATION_CLASSES:
            improved_in_class = 0
            exhausted = 0
            for seed in SEEDS:
                baseline = index.get((fixture, seed, "B0"), {})
                candidate = index.get((fixture, seed, policy), {})
                if bool(candidate.get("accepted")) and not bool(
                    baseline.get("accepted")
                ):
                    improved_pairs += 1
                    improved_in_class += 1
                if _as_str(candidate.get("terminal")) == "model_budget_exhausted":
                    exhausted += 1
            class_improvements[fixture] = improved_in_class
            exhausted_by_class[fixture] = exhausted
        for fixture in CONTROL_CLASSES:
            for seed in SEEDS:
                candidate = index.get((fixture, seed, policy), {})
                baseline = index.get((fixture, seed, "B0"), {})
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
        replay_entry = _as_mapping(replay_policies.get(policy))
        replay_missing = _as_list(replay_entry.get("missing_pre_registered_fires"))
        if replay and replay_missing:
            rejected = True
            reasons.append(
                "run2 replay missed pre-registered fires at boundaries "
                f"{replay_missing}"
            )
        b2_falsified = bool(replay_entry.get("b2_pre_registration_falsified"))
        candidates[policy] = {
            "stagnation_pairs_improved": improved_pairs,
            "class_improvements": class_improvements,
            "class_budget_exhausted": exhausted_by_class,
            "false_triggers_productive": false_productive,
            "false_triggers_exploring": false_exploring,
            "supervisor_calls": supervisor_calls,
            "supervisor_tokens": supervisor_tokens,
            "trigger_latencies": latencies,
            "acceptance_harm_runs": harms,
            "meets_acceptance_tolerance": accepted,
            "rejected": rejected,
            "rejection_reasons": reasons,
            "run2_replay_admissible": (None if not replay else not replay_missing),
            "b2_pre_registration_falsified": b2_falsified,
        }

    passing = [
        policy
        for policy in CANDIDATES
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
    inconclusive = not reproducible or bool(censored)
    return {
        "experiment": "EXP-0002",
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
        "inconclusive": inconclusive,
        "replay": replay,
        "candidates": candidates,
        "passing_candidates": passing,
        "selected_policy": selected,
        "no_policy_selected": selected is None,
        "tolerance_interpretation": {
            "stagnation_pairs": [
                f"{fixture}/s{seed}" for fixture in STAGNATION_CLASSES for seed in SEEDS
            ],
            "censored": (
                "a run is censored only when it terminates on an unexpected "
                "error; ending at the predeclared decision budget without "
                "acceptance is a valid negative observation (EXP-0001 "
                "interpretation, reused unchanged)"
            ),
            "budget_exhausted": (
                "the inconclusive tolerance's 'budget-exhausted' runs are "
                "counted per candidate over the stagnation classes, where "
                "exhaustion means the intervened run never recovered; baseline "
                "B0 exhaustion is the expected negative observation"
            ),
            "b2_falsification": (
                "an unexpected B2 fire on run2 falsifies its pre-registered "
                "non-fire expectation, is recorded, and leaves P5 admissible "
                "(EXP-0002 Tolerances)"
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
        "# EXP-0002 signal-coverage results",
        "",
        f"- Runs: {results.get('runs')}",
        f"- Baseline reproducible: {results.get('baseline_reproducible')}",
        f"- Inconclusive: {results.get('inconclusive')}",
        f"- Selected policy: {results.get('selected_policy')}",
        "",
        "| Policy | Improved pairs | Classes >=3/5 | False (productive) | "
        "False (exploring) | Supervisor calls | Meets tolerance | Rejected | "
        "Run2 admissible |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    candidates = results.get("candidates") or {}
    if isinstance(candidates, Mapping):
        for policy in CANDIDATES:
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
                f"{data.get('rejected')} | "
                f"{data.get('run2_replay_admissible')} |"
            )
    replay = results.get("replay") or {}
    if isinstance(replay, Mapping) and replay:
        lines.append("")
        lines.append("## Run2 replay versus pre-registration")
        lines.append("")
        replay_policies = replay.get("policies") or {}
        if isinstance(replay_policies, Mapping):
            for policy in ("B0", "P4", "P5"):
                entry = replay_policies.get(policy)
                if not isinstance(entry, Mapping):
                    continue
                lines.append(
                    f"- {policy}: repetition at "
                    f"{entry.get('observed_repetition_boundaries')} "
                    f"(expected {entry.get('expected_repetition_boundaries')}); "
                    f"burn at boundaries {entry.get('observed_burn_boundaries')} "
                    f"(expected {entry.get('expected_burn_boundaries')}, "
                    f"consumed {entry.get('expected_burn_consumed')}); "
                    f"missing {entry.get('missing_pre_registered_fires')}; "
                    f"extra {entry.get('extra_fires')}"
                )
        fingerprints = replay.get("stdout_fingerprints") or {}
        if isinstance(fingerprints, Mapping):
            lines.append(
                f"- stdout fingerprint oracle: strict "
                f"{fingerprints.get('strict_matched')}/"
                f"{fingerprints.get('rows_compared')}, normalized "
                f"{fingerprints.get('normalized_matched')}/"
                f"{fingerprints.get('rows_compared')} rows match "
                f"(strict mismatched calls: "
                f"{fingerprints.get('strict_mismatched_calls')}; normalized "
                f"mismatched calls: "
                f"{fingerprints.get('normalized_mismatched_calls')})"
            )
    lines.append("")
    lines.append(
        "Tolerances and selection follow "
        "`plans/experiments/exp-0002-supervisor-signal-coverage.md`."
    )
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
        expected_actions = sum(1 for event in events if event.kind == "action_intended")
        expected_tokens = 0
        for event in events:
            if event.kind != "context_assembled":
                continue
            manifest_data = _as_mapping(event.payload.get("manifest"))
            expected_tokens += _as_int(
                manifest_data.get("serialized_input_tokens"),
                _as_int(manifest_data.get("tokens_used")),
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
        store = run_dir / "artifacts"
        if not store.exists():
            problems.append(f"{summary_path}: artifact payloads missing")
    try:
        results = json.loads((bundle / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"results.json unreadable: {exc}")
        return problems
    recomputed = aggregate_bundle(bundle)
    for field_name in (
        "selected_policy",
        "baseline_reproducible",
        "censored_runs",
        "inconclusive",
    ):
        if results.get(field_name) != recomputed.get(field_name):
            problems.append(f"results field {field_name} is not reproducible")
    for policy in CANDIDATES:
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
            "run2_replay_admissible",
        ):
            if recorded_map.get(field_name) != fresh_map.get(field_name):
                problems.append(
                    f"candidate {policy} field {field_name} is not reproducible"
                )
    return problems


def run_experiment(*, force: bool = False) -> dict[str, object]:
    """Generate the EXP-0002 bundle from a clean tree."""
    if _git_revision()[1]:
        raise SystemExit(
            "refusing to run EXP-0002 with tracked working-tree changes; "
            "commit them first"
        )
    if BUNDLE_DIR.exists() and any(BUNDLE_DIR.iterdir()):
        if not force:
            raise SystemExit(f"{BUNDLE_DIR} is not empty; pass --force to regenerate")
        shutil.rmtree(BUNDLE_DIR)
    (BUNDLE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
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
            repeat_dir = BUNDLE_DIR / "runs" / name / f"s{seed}" / "B0-repeat"
            _run_once(spec, seed, "B0", repeat_dir, repeat=True)
            total += 1
    results = aggregate_bundle(BUNDLE_DIR)
    results["executions"] = total
    write_aggregate(BUNDLE_DIR, results)
    return results


def _aggregate_script_text() -> str:
    return (
        '"""Recompute the EXP-0002 aggregate from the retained run journals."""\n'
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
        "from tools.exp_0002_signal_coverage import write_aggregate  # noqa: E402\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    write_aggregate(BUNDLE)\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the experiment, replay run2, or aggregate and verify the bundle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("run", "replay", "manifest", "aggregate", "verify"),
        nargs="?",
        default="run",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--bundle", type=Path, default=BUNDLE_DIR)
    parser.add_argument("--run2-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "manifest":
        print(canonical_json(build_manifest()))
        return 0
    if args.command == "run":
        results = run_experiment(force=args.force)
        print(canonical_json(results))
        return 0
    if args.command == "replay":
        if args.run2_dir is None:
            parser.error("replay requires --run2-dir (a verified local copy)")
        summary = replay_run2(args.run2_dir, args.bundle)
        print(canonical_json(summary))
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
