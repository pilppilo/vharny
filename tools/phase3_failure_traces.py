"""Run and verify the PHASE-0003 real failure-trace capture.

One unscripted LongCat run against a bounded, visible software fixture whose
test fails until both workspace files are fixed. The harness supplies no
action sequence and no hidden validator data: the workspace files and the
test are readable through the contract, and the evaluator implements the same
two assertions the visible test declares. The retained journal is expected to
contain genuine failure traces (failed test runs, rejected evaluations, or
malformed model output) and the repair path that follows them.

The retained bundle also carries a deterministic ``stagnation.json`` sidecar:
it asserts the amended PHASE-0003 entrance criterion by evaluating the
EXP-selected monitor policy (``monitor.p1.v1``) over the retained journal and
recording the first and terminal firing boundaries. Writing the sidecar leaves
the retained run files byte-identical.

Usage::

    uv run python tools/phase3_failure_traces.py run
    uv run python tools/phase3_failure_traces.py verify
    uv run python tools/phase3_failure_traces.py rebuild
    uv run python tools/phase3_failure_traces.py stagnation
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from typing import Mapping, Sequence

from vharness.agent import (
    ActionDefinition,
    ActionProposal,
    BudgetLimits,
    CompatibleModel,
    CompletionMode,
    ComponentIdentity,
    ContractError,
    EnvironmentContract,
    EvaluationReceipt,
    ExecutionReceipt,
    Observation,
    RecordEnvelope,
    RunIdentity,
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
from vharness.agent.kernel import ModelCallError
from vharness.agent.models import (
    ArtifactRef,
    EvaluationRequest,
    Event,
    ExecutionStatus,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.replay import projection_payload, replay
from vharness.agent.supervision import (
    decision_segments,
    evaluate_supervision,
    facts_history,
    monitor_diagnostics,
    monitor_policy_p1,
)
from vharness.generators.openai_compat import OpenAICompatible

# pylint: disable=too-many-arguments,too-many-locals,too-many-statements
# pylint: disable=too-many-instance-attributes,too-few-public-methods
# pylint: disable=too-many-lines,too-many-return-statements

SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = SOURCE_ROOT / "evidence" / "phase-0003-failure-traces"
TOOL_RELPATH = "tools/phase3_failure_traces.py"
STAGNATION_RELPATH = "stagnation.json"

BUNDLE_ID = "phase-0003-failure-traces"
SESSION_ID = "vharness-phase3-failure-traces-longcat"
TASK_ID = "vharness-phase3-failure-traces"
OBJECTIVE = (
    "Make the fixture test pass. The workspace holds main.py (declares VALUE) "
    "and test_fixture.py (requires VALUE == 1). First run the visible fixture "
    "test to capture the current failure, then fix main.py, and re-run the "
    "test until it passes."
)
PROVIDER_NAME = "LongCat"
BASE_URL = "https://api.longcat.ai/openai/v1/"
MODEL_ID = "LongCat-2.0"
CREDENTIAL_ENV = "LONGCAT_API_KEY"
CONTEXT_WINDOW_TOKENS = 8192
MAX_OUTPUT_TOKENS = 2048
REPAIR_BUDGET = 1
MAX_MODEL_CALLS = 12
MAX_ACTIONS = 12
MAX_EVALUATIONS = 2

TEST_SOURCE = (
    "from main import VALUE\n"
    "\n"
    "\n"
    "def test_fixture():\n"
    '    assert VALUE == 1, f"VALUE is {VALUE}, expected 1"\n'
    '    print("fixture test passed")\n'
)

INITIAL_WORKSPACE: Mapping[str, str] = {
    "main.py": "VALUE = 0\n",
    "test_fixture.py": TEST_SOURCE,
}

CONFIGURATION = {
    "evidence": BUNDLE_ID,
    "mode": "unscripted-compatible-model-failure-capture",
    "provider": {
        "name": PROVIDER_NAME,
        "base_url": BASE_URL,
        "credential_env": CREDENTIAL_ENV,
    },
    "model": {
        "model_id": MODEL_ID,
        "temperature": 0.0,
        "timeout_seconds": 120.0,
        "max_retries": 0,
        "length_retries": 0,
        "repair_budget": REPAIR_BUDGET,
        "reserved_output_tokens": MAX_OUTPUT_TOKENS,
        "per_decision_context_window_tokens": CONTEXT_WINDOW_TOKENS,
    },
    "task": {
        "task_id": TASK_ID,
        "objective": OBJECTIVE,
        "completion_mode": "finite",
        "success_evidence": "external fixture-test evaluator acceptance",
    },
    "fixture": {
        "environment_id": "phase3-visible-software-fixture",
        "initial_files": INITIAL_WORKSPACE,
        "test": TEST_SOURCE,
        "hidden_validator_data": False,
    },
    "budgets": {
        "model_calls": MAX_MODEL_CALLS,
        "actions": MAX_ACTIONS,
        "evaluations": MAX_EVALUATIONS,
        "per_decision_context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "output_tokens": MAX_OUTPUT_TOKENS,
        "repair_budget": REPAIR_BUDGET,
    },
}


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


def _configuration_digest() -> str:
    return _digest(canonical_json(CONFIGURATION))


def _workspace_digest(workspace: Mapping[str, str]) -> str:
    return _digest(canonical_json(dict(sorted(workspace.items()))))


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _credential() -> str:
    """Read the operator-named key from the process or Windows user scope."""
    value = os.environ.get(CREDENTIAL_ENV, "")
    if value:
        return value
    if os.name != "nt":
        raise SystemExit(
            f"{CREDENTIAL_ENV} is not set in this environment; export the "
            "operator-authorized key or stop and ask the operator"
        )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"[Environment]::GetEnvironmentVariable('{CREDENTIAL_ENV}','User')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.rstrip("\r\n")
    if completed.returncode != 0 or not value:
        raise SystemExit(
            f"{CREDENTIAL_ENV} is not available in the process or Windows user "
            "scope; stop and ask the operator"
        )
    return value


def _source_identity() -> dict[str, object]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=SOURCE_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    return {
        "git_revision": revision,
        "git_dirty": dirty,
        "tool": TOOL_RELPATH,
        "tool_sha256": _sha256_file(Path(__file__)),
    }


def _check_workspace(workspace: Mapping[str, str]) -> tuple[bool, str]:
    """Evaluate the visible test assertion mechanically."""
    value_match = re.search(r"(?m)^VALUE\s*=\s*(\S+)\s*$", workspace.get("main.py", ""))
    if value_match is None or value_match.group(1) != "1":
        actual = value_match.group(1) if value_match else "<missing>"
        return False, f"VALUE is {actual}, expected 1 (test_fixture.py)"
    return True, "fixture test passed"


def _action(
    name: str,
    description: str,
    properties: Mapping[str, Mapping[str, str]],
    *,
    required: Sequence[str] = (),
) -> ActionDefinition:
    return ActionDefinition(
        name,
        description,
        {
            "type": "object",
            "properties": {key: dict(value) for key, value in properties.items()},
            "required": tuple(required),
        },
    )


class FixtureEnvironment:
    """Visible workspace contract for the bounded failure-trace fixture."""

    def __init__(self, runtime: "FixtureRuntime") -> None:
        self._runtime = runtime

    def describe(self) -> EnvironmentContract:
        """Return the fixture's mechanical contract."""
        return EnvironmentContract(
            environment_id="phase3-visible-software-fixture",
            version="v1",
            initial_state=self._runtime.state_ref(),
            actions=(
                _action(
                    "read_file",
                    "Read one workspace file; the receipt carries its content",
                    {"path": {"type": "string"}},
                    required=("path",),
                ),
                _action(
                    "write_file",
                    "Replace one workspace file's content",
                    {"path": {"type": "string"}, "content": {"type": "string"}},
                    required=("path", "content"),
                ),
                _action("run_tests", "Run the visible fixture test", {}),
            ),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class FixtureRuntime:
    """Deterministic workspace runtime; failures are first-class receipts."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts
        self._workspace = dict(INITIAL_WORKSPACE)
        self._revision = 0
        self._history: dict[str, dict[str, str]] = {
            self._state_value(): dict(self._workspace)
        }

    def identity(self) -> ComponentIdentity:
        """Return the runtime-owned identity."""
        return ComponentIdentity("phase3-visible-runtime", "v1")

    def state_ref(self) -> StateRef:
        """Return the current externally owned state reference."""
        return StateRef(
            owner="phase3-workspace",
            value=self._state_value(),
            digest=_workspace_digest(self._workspace),
        )

    def snapshot(self, state: StateRef) -> dict[str, str] | None:
        """Return the workspace content for one externally reported state."""
        snapshot = self._history.get(state.value)
        return None if snapshot is None else dict(snapshot)

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Apply one action mechanically and return its receipt."""
        status = ExecutionStatus.SUCCEEDED
        error: str | None = None
        raw_artifact: ArtifactRef | None = None
        action = proposal.action_name
        arguments = proposal.arguments
        if action == "read_file":
            path = str(arguments.get("path", ""))
            content = self._workspace.get(path)
            if content is None:
                status = ExecutionStatus.FAILED
                error = f"no such workspace file: {path}"
            else:
                raw_artifact = self._artifacts.put(
                    content.encode("utf-8"),
                    media_type="text/plain",
                    provenance="phase3-visible-runtime",
                )
        elif action == "write_file":
            path = str(arguments.get("path", ""))
            if path not in self._workspace:
                status = ExecutionStatus.FAILED
                error = f"no such workspace file: {path}"
            else:
                self._workspace[path] = str(arguments.get("content", ""))
                self._revision += 1
                self._history[self._state_value()] = dict(self._workspace)
        elif action == "run_tests":
            passed, message = _check_workspace(self._workspace)
            if not passed:
                status = ExecutionStatus.FAILED
                error = message
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
            raw_artifact=raw_artifact,
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Report no runtime-side knowledge of an interrupted operation."""
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNKNOWN)

    def _state_value(self) -> str:
        return f"rev-{self._revision}"


class FixtureEvaluator:
    """External evaluator bound to the requested workspace state."""

    def __init__(self, runtime: FixtureRuntime) -> None:
        self._runtime = runtime

    def identity(self) -> ComponentIdentity:
        """Return the evaluator-owned identity."""
        return ComponentIdentity("phase3-visible-evaluator", "v1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Accept only a requested state whose workspace passes the test."""
        snapshot = self._runtime.snapshot(request.evaluated_state)
        passed = False if snapshot is None else _check_workspace(snapshot)[0]
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=passed,
            comparison="improved" if passed else "regressed",
            objectives={"tests_passed": passed},
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


def _run_identity() -> RunIdentity:
    return RunIdentity(
        package_version="0.2.0",
        configuration_digest=_configuration_digest(),
        runtime=ComponentIdentity("phase3-visible-runtime", "v1"),
        evaluator=ComponentIdentity("phase3-visible-evaluator", "v1"),
    )


def _task(runtime: FixtureRuntime) -> TaskSpec:
    environment = FixtureEnvironment(runtime)
    return TaskSpec(
        task_id=TASK_ID,
        objective=OBJECTIVE,
        completion_mode=CompletionMode.FINITE,
        success_evidence="external fixture-test evaluator acceptance",
        budgets=BudgetLimits(
            actions=MAX_ACTIONS,
            model_calls=MAX_MODEL_CALLS,
            evaluations=MAX_EVALUATIONS,
        ),
        environment_id=environment.describe().environment_id,
        evaluation_contract_id="phase3-visible-evaluator:v1",
    )


def _initial_observation(runtime: FixtureRuntime) -> Observation:
    observed = _now()
    return Observation(
        f"initial-observation-{SESSION_ID}",
        runtime.state_ref(),
        observed,
        "fixture contract",
        RecordEnvelope(
            f"initial-observation-{SESSION_ID}",
            SESSION_ID,
            1,
            observed,
            "fixture contract",
        ),
    )


def _read_events(path: Path) -> tuple[Event, ...]:
    """Read all canonical events from a retained sqlite journal."""
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
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


def _transcript(events: Sequence[Event]) -> list[dict[str, object]]:
    transcript: list[dict[str, object]] = []
    for event in events:
        if event.kind == "model_decided":
            transcript.append(
                {
                    "sequence": event.sequence,
                    "kind": "model_decided",
                    "result_kind": event.payload.get("result_kind"),
                    "output": event.payload.get("output"),
                    "finish_reason": event.payload.get("finish_reason"),
                }
            )
        elif event.kind == "model_malformed":
            transcript.append(
                {
                    "sequence": event.sequence,
                    "kind": "model_malformed",
                    "error": event.payload.get("error"),
                }
            )
        elif event.kind == "receipt_received":
            mapped = _mapping(event.payload.get("receipt"))
            transcript.append(
                {
                    "sequence": event.sequence,
                    "kind": "receipt_received",
                    "status": mapped.get("status"),
                    "error": mapped.get("error"),
                }
            )
        elif event.kind == "evaluation_received":
            transcript.append(
                {
                    "sequence": event.sequence,
                    "kind": "evaluation_received",
                    "disposition": event.payload.get("disposition"),
                }
            )
    return transcript


def _failure_traces(events: Sequence[Event]) -> dict[str, object]:
    failed_receipts: list[dict[str, object]] = []
    rejected_evaluations: list[int] = []
    malformed: list[int] = []
    completion_rejected: list[int] = []
    for event in events:
        if event.kind == "receipt_received":
            mapped = _mapping(event.payload.get("receipt"))
            if mapped.get("status") not in {"succeeded", "accepted"}:
                failed_receipts.append(
                    {
                        "sequence": event.sequence,
                        "status": mapped.get("status"),
                        "error": mapped.get("error"),
                    }
                )
        elif event.kind == "evaluation_received":
            if event.payload.get("disposition") != "accepted":
                rejected_evaluations.append(event.sequence)
        elif event.kind == "model_malformed":
            malformed.append(event.sequence)
        elif event.kind == "completion_rejected":
            completion_rejected.append(event.sequence)
    return {
        "failed_receipts": failed_receipts,
        "rejected_evaluation_sequences": rejected_evaluations,
        "model_malformed_sequences": malformed,
        "completion_rejected_sequences": completion_rejected,
        "present": bool(
            failed_receipts or rejected_evaluations or malformed or completion_rejected
        ),
    }


def generate_bundle(  # pylint: disable=too-many-branches
    output: Path,
) -> dict[str, object]:
    """Run the capture once from a clean revision and retain its evidence."""
    source = _source_identity()
    if source["git_dirty"]:
        raise SystemExit("refusing to run the capture with a dirty worktree")
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"evidence output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    key = _credential()

    journal = SqliteJournal(output / "session.sqlite3")
    artifacts = ArtifactStore(output / "artifacts")
    runtime = FixtureRuntime(artifacts)
    environment = FixtureEnvironment(runtime)
    evaluator = FixtureEvaluator(runtime)
    session = Session.create(
        SESSION_ID,
        _task(runtime),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        run_identity=_run_identity(),
        initial_observation=_initial_observation(runtime),
    )
    generator = OpenAICompatible(
        BASE_URL,
        MODEL_ID,
        key,
        timeout=120.0,
        max_retries=0,
        length_retries=0,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.0,
        cache_path=None,
    )
    setattr(generator, "provider_id", PROVIDER_NAME)
    model = CompatibleModel(generator, model_id=MODEL_ID, repair_budget=REPAIR_BUDGET)
    terminal = "model_budget_exhausted"
    try:
        for _ in range(MAX_MODEL_CALLS):
            if len(session.accepted_lineage()) > 1:
                terminal = "accepted"
                break
            if session.view().status in {
                SessionStatus.COMPLETED,
                SessionStatus.STOPPED,
                SessionStatus.FAILED,
            }:
                terminal = f"status:{session.view().status.value}"
                break
            try:
                session.step(model, token_budget=CONTEXT_WINDOW_TOKENS)
            except ModelCallError:
                terminal = "model_error"
                break
            except TransitionError:
                terminal = "budget_exhausted"
                break
            except ContractError:
                terminal = "contract_rejected"
                break
    finally:
        generator.close()
        journal.close()
    journal_path = output / "session.sqlite3"
    for suffix in ("-wal", "-shm"):
        sidecar = journal_path.with_name(journal_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    events = _read_events(output / "session.sqlite3")
    if not events:
        raise SystemExit("capture produced no durable events")
    counters = derive_counters(events)
    state = replay(events)
    traces = _failure_traces(events)
    if not traces["present"]:
        raise SystemExit(
            "no failure trace was recorded; the entrance evidence is not "
            "satisfied and the run must be repeated"
        )
    report = {
        "evidence_id": BUNDLE_ID,
        "generated_at": _now(),
        "terminal": terminal,
        "accepted": counters.commits > 0,
        "source": source,
        "configuration": CONFIGURATION,
        "configuration_digest": _configuration_digest(),
        "counters": {
            "actions": counters.actions,
            "evaluations": counters.evaluations,
            "model_calls": counters.model_calls,
            "commits": counters.commits,
            "reconciliations": counters.reconciliations,
        },
        "failure_traces": traces,
        "acceptance_claims": {
            "scripted_action_sequence_supplied": False,
            "hidden_validator_data_supplied": False,
            "provider_rejected": False,
        },
        "projection_sha256": _digest(canonical_json(projection_payload(state))),
    }
    _write_json(output / "transcript.json", _transcript(events))
    _write_json(output / "report.json", report)
    manifest = {
        "evidence_id": BUNDLE_ID,
        "files": {
            "session.sqlite3": _sha256_file(output / "session.sqlite3"),
            "transcript.json": _sha256_file(output / "transcript.json"),
            "report.json": _sha256_file(output / "report.json"),
        },
        "source": source,
        "event_count": len(events),
        "terminal": terminal,
        "failure_traces_present": traces["present"],
    }
    _write_json(output / "manifest.json", manifest)
    return manifest


def _write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_bundle(  # pylint: disable=too-many-branches
    bundle: Path,
) -> list[str]:
    """Replay the retained journal and recheck hashes and failure traces."""
    problems: list[str] = []
    manifest = _read_json(bundle / "manifest.json")
    report = _read_json(bundle / "report.json")
    if not isinstance(manifest, Mapping) or not isinstance(report, Mapping):
        return ["manifest or report is not a JSON object"]
    files = _mapping(manifest.get("files"))
    for name, digest in files.items():
        path = bundle / str(name)
        if not path.exists():
            problems.append(f"{name}: missing")
        elif _sha256_file(path) != digest:
            problems.append(f"{name}: sha256 mismatch")
    journal_path = bundle / "session.sqlite3"
    if not journal_path.exists():
        return problems + ["session.sqlite3: missing"]
    events = _read_events(journal_path)
    if len(events) != manifest.get("event_count"):
        problems.append("event count mismatch")
    replay(events)
    actual = _failure_traces(events)
    if report.get("failure_traces") != actual:
        problems.append("failure traces are not reproducible from the journal")
    if actual["present"] is not True:
        problems.append("retained journal contains no failure trace")
    source = _mapping(manifest.get("source"))
    if not isinstance(source.get("tool_sha256"), str) or not isinstance(
        source.get("git_revision"), str
    ):
        problems.append("manifest source does not record tool hash and revision")
    expected_stagnation = _stagnation_payload(bundle, manifest)
    stagnation_path = bundle / STAGNATION_RELPATH
    if not stagnation_path.exists():
        problems.append(f"{STAGNATION_RELPATH}: missing")
    elif _read_json(stagnation_path) != expected_stagnation:
        problems.append(
            f"{STAGNATION_RELPATH}: does not match the deterministic recomputation"
        )
    if expected_stagnation["stagnation_detected"] is not True:
        problems.append(f"{STAGNATION_RELPATH}: reports no stagnation")
    return problems


def rebuild_report(  # pylint: disable=too-many-return-statements
    bundle: Path,
) -> list[str]:
    """Rebuild report.json from the retained journal and refresh its manifest hash.

    The report is a derived summary. Every field except ``generated_at`` is
    re-derived from the journal, the manifest, and the runner configuration;
    ``generated_at`` uses the last recorded event so repeated rebuilds are
    byte-identical. Returns verification problems; an empty list means the
    rebuilt bundle verifies.
    """
    manifest_path = bundle / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        return ["manifest.json is not a JSON object"]
    files = _mapping(manifest.get("files"))
    for name, digest in files.items():
        if str(name) == "report.json":
            continue
        path = bundle / str(name)
        if not path.exists():
            return [f"{name}: missing"]
        if _sha256_file(path) != digest:
            return [f"{name}: sha256 mismatch"]
    journal_path = bundle / "session.sqlite3"
    if not journal_path.exists():
        return ["session.sqlite3: missing"]
    events = _read_events(journal_path)
    if len(events) != manifest.get("event_count"):
        return ["event count mismatch"]
    run_identity: Mapping[str, object] = {}
    for event in events:
        if event.kind == "session_created":
            run_identity = _mapping(event.payload.get("run_identity"))
            break
    if run_identity.get("configuration_digest") != _configuration_digest():
        return ["journal configuration digest does not match this runner"]
    source = _mapping(manifest.get("source"))
    if not source:
        return ["manifest source is missing"]
    counters = derive_counters(events)
    state = replay(events)
    report = {
        "evidence_id": BUNDLE_ID,
        "generated_at": max(event.recorded_at for event in events),
        "terminal": manifest.get("terminal"),
        "accepted": counters.commits > 0,
        "source": dict(source),
        "configuration": CONFIGURATION,
        "configuration_digest": _configuration_digest(),
        "counters": {
            "actions": counters.actions,
            "evaluations": counters.evaluations,
            "model_calls": counters.model_calls,
            "commits": counters.commits,
            "reconciliations": counters.reconciliations,
        },
        "failure_traces": _failure_traces(events),
        "acceptance_claims": {
            "scripted_action_sequence_supplied": False,
            "hidden_validator_data_supplied": False,
            "provider_rejected": False,
        },
        "projection_sha256": _digest(canonical_json(projection_payload(state))),
    }
    _write_json(bundle / "report.json", report)
    rebuilt = dict(files)
    rebuilt["report.json"] = _sha256_file(bundle / "report.json")
    manifest["files"] = rebuilt
    _write_json(manifest_path, manifest)
    return verify_bundle(bundle)


def _stagnation_payload(
    bundle: Path, manifest: Mapping[str, object]
) -> dict[str, object]:
    """Compute the formal stagnation evidence over the retained journal.

    Every decision boundary is evaluated with the EXP-selected policy
    (``monitor.p1.v1``). A firing boundary records its repeated fingerprint,
    window occupancy, and the journal events that carried the repetition.
    The payload is deterministic: it depends only on the retained journal,
    ``manifest.json``, and the monitor semantics.
    """
    events = _read_events(bundle / "session.sqlite3")
    # The retained bundle records EXP-0001 semantics; the ADR-0011 switch must
    # not change their recomputation.
    policy = monitor_policy_p1()
    history = facts_history(events)
    segments = decision_segments(events)
    first: dict[str, object] | None = None
    terminal: dict[str, object] | None = None
    for call in range(2, len(segments) + 2):
        prefix = tuple(event for segment in segments[: call - 1] for event in segment)
        trigger = evaluate_supervision(prefix, call, policy)
        if trigger is None:
            continue
        window = history[: call - 1][-policy.window_calls :]
        boundary: dict[str, object] = {
            "call": trigger.call,
            "fingerprint": trigger.fingerprint,
            "repeat_count": trigger.repeat_count,
            "window_calls": len(window),
            "window_supported_progress": sum(
                fact.supported_progress for fact in window
            ),
            "input_event_ids": list(trigger.input_event_ids),
        }
        if first is None:
            first = boundary
        terminal = boundary
    diagnostics = monitor_diagnostics(events)
    failures = _failure_traces(events)
    return {
        "evidence_id": BUNDLE_ID,
        "generated_at": max(event.recorded_at for event in events),
        "policy": {
            "version": policy.version,
            "window_calls": policy.window_calls,
            "repeat_threshold": policy.repeat_threshold,
            "cooldown_calls": policy.cooldown_calls,
        },
        "decisions": diagnostics.decisions,
        "supported_progress": diagnostics.supported_progress,
        "stagnation_detected": first is not None,
        "first_trigger": first,
        "terminal_trigger": terminal,
        "failure_receipts": failures["failed_receipts"],
        "journal_sha256": _sha256_file(bundle / "session.sqlite3"),
        "source": dict(_mapping(manifest.get("source"))),
    }


def write_stagnation(bundle: Path) -> list[str]:
    """Write the formal stagnation sidecar computed from the retained journal.

    Kills no retained data: the run files stay byte-identical and the sidecar
    rebuilds byte-identically. Returns verification problems; an empty list
    means the rewritten bundle verifies.
    """
    manifest = _read_json(bundle / "manifest.json")
    if not isinstance(manifest, Mapping):
        return ["manifest.json is not a JSON object"]
    _write_json(bundle / STAGNATION_RELPATH, _stagnation_payload(bundle, manifest))
    return verify_bundle(bundle)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the capture, rebuild the report, or verify the retained bundle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("run", "verify", "rebuild", "stagnation"),
        nargs="?",
        default="run",
    )
    parser.add_argument("--bundle", type=Path, default=BUNDLE_DIR)
    args = parser.parse_args(argv)
    if args.command == "run":
        manifest = generate_bundle(args.bundle)
        print(canonical_json(manifest))
        return 0
    if args.command == "rebuild":
        problems = rebuild_report(args.bundle)
        if problems:
            for problem in problems:
                print(f"problem: {problem}")
            return 1
        print("rebuild: OK")
        return 0
    if args.command == "stagnation":
        problems = write_stagnation(args.bundle)
        if problems:
            for problem in problems:
                print(f"problem: {problem}")
            return 1
        print("stagnation: OK")
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
