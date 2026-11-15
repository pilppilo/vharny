"""Reviewer checks for journal replay and one real-shaped run directory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = (
    ROOT
    / "evidence"
    / "phase-0003-paired-recovery"
    / "runs"
    / "recovery-drill"
    / "s0"
    / "supervised"
    / "events.jsonl.gz"
)

sys.path.insert(0, str(ROOT))
from tools.phase3_paired_recovery import _read_events  # noqa: E402

from vharness.agent import (  # noqa: E402
    ActionDefinition,
    ActionProposal,
    BudgetLimits,
    CompletionMode,
    ComponentIdentity,
    ExecutionReceipt,
    Session,
    StateRef,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore  # noqa: E402
from vharness.agent.codec import canonical_json  # noqa: E402
from vharness.agent.evidence import counters as derive_counters  # noqa: E402
from vharness.agent.journal import SqliteJournal  # noqa: E402
from vharness.agent.kernel import CompatibleModel, ModelCallError  # noqa: E402
from vharness.agent.replay import replay  # noqa: E402
from vharness.agent.models import (  # noqa: E402
    EnvironmentContract,
    EvaluationReceipt,
    EvaluationRequest,
    ExecutionStatus,
    RuntimeCapabilities,
    Stop,
    UsageMeasurement,
)
from vharness.agent.ports import (  # noqa: E402
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.run_settings import (  # noqa: E402
    RunManifestRecord,
    RunSettings,
)
from vharness.agent.runview import (  # noqa: E402
    compare_runs,
    format_show,
    main,
    parse_gate_log,
    review,
    show_run,
)

RATIONALE = "because the fixture port list is stale"


@dataclass
class _Generation:
    text: str
    model: str = "fake-llm"
    finish_reason: str = "stop"
    error: str | None = None
    failure_class: str | None = None
    prompt_tokens: int | None = 3
    completion_tokens: int | None = 5
    reasoning_tokens: int | None = None


class _Generator:
    def __init__(
        self, texts: tuple[str, ...], reasoning_tokens: int | None = None
    ) -> None:
        self._texts = list(texts)
        self._reasoning_tokens = reasoning_tokens

    def generate(self, system: str, prompt: str) -> _Generation:
        del system, prompt
        return _Generation(self._texts.pop(0), reasoning_tokens=self._reasoning_tokens)


class _TransportGenerator:
    """Returns a provider transport failure instead of a decision."""

    def generate(self, system: str, prompt: str) -> _Generation:
        del system, prompt
        return _Generation("", error="transport down")


class _Environment:
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef("fake", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class _Runtime:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts
        self._count = 0

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        self._count += 1
        stdout = " ".join(["port"] * 40) + f" item {self._count}"
        artifact = self._artifacts.put(
            json.dumps({"stdout": stdout}).encode("utf-8"),
            media_type="application/json",
            provenance="process-runtime-result",
        )
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("fake", f"after-{self._count}", digest="1" * 64),
            usage=UsageMeasurement(
                measurement_id=f"runtime-usage-{proposal.proposal_id}",
                actions=1,
            ),
            raw_artifact=artifact,
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class _Evaluator:
    """Satisfies the evaluator port. The fixture never requests evaluation."""

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        del request
        raise AssertionError("fixture evaluator is not called")

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


def _action(index: int) -> str:
    return json.dumps(
        {
            "kind": "ActionChoice",
            "action_name": "inspect",
            "rationale": RATIONALE,
            "arguments": {"command": f"ss -lnt {index}"},
        }
    )


def _build_run(tmp_path: Path, reasoning_tokens: int | None = None) -> Path:
    """Build a run directory through the journal API and close it."""
    root = tmp_path / "run"
    root.mkdir()
    journal = SqliteJournal(root / "session.sqlite3")
    artifacts = ArtifactStore(root / "artifacts")
    session = Session.create(
        "session-runview",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=16, evaluations=1, model_calls=20),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=_Environment(),
        runtime=_Runtime(artifacts),
        evaluator=_Evaluator(),
    )
    texts = tuple(_action(index) for index in range(16)) + ('{"kind": "LineageQuery"}',)
    model = CompatibleModel(
        _Generator(texts, reasoning_tokens=reasoning_tokens), repair_budget=0
    )
    for _step in range(len(texts)):
        session.step(model)
    session.enqueue(Stop("stop-1", session.session_id))
    session.advance()
    record = RunManifestRecord(
        session_id=session.session_id,
        evaluation_mode="operator",
        created_at="2026-11-10T00:00:00Z",
        package_version="0.2.0",
        settings=RunSettings(
            objective="inspect the fixture",
            workspace=str(tmp_path / "workspace"),
            base_url="https://example.invalid/v1",
            model="fixture-model",
            api_key_env="LONGCAT_API_KEY",
            max_actions=16,
            max_model_calls=20,
            max_evaluations=1,
            max_steps=40,
            token_budget=16384,
            max_output_tokens=256,
            repair_budget=0,
            target="127.0.0.1",
            shell="/bin/sh",
        ),
    )
    (root / "run.json").write_bytes(canonical_json(record.to_payload()).encode("utf-8"))
    journal.close()
    return root


def _as_mapping(value: object) -> dict[str, object]:
    """Return a JSON object or fail the test."""
    if not isinstance(value, dict):
        raise AssertionError(value)
    return value


def _as_list(value: object) -> list[object]:
    """Return a JSON list or fail the test."""
    if not isinstance(value, list):
        raise AssertionError(value)
    return value


def _walk_keys(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        found.extend(str(key) for key in value)
        for item in value.values():
            found.extend(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_keys(item))
    return found


def test_runner_exit_reports_failed_while_the_projection_stays_open(
    tmp_path: Path,
) -> None:
    """A runner exit tail is a failed run even though replay stays non-terminal."""
    root = tmp_path / "run"
    root.mkdir()
    journal = SqliteJournal(root / "session.sqlite3")
    session = Session.create(
        "session-runner-exit",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=2),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(root / "artifacts"),
        environment=_Environment(),
        runtime=_Runtime(ArtifactStore(root / "artifacts")),
        evaluator=_Evaluator(),
    )
    reason = "provider output exhausted: empty or length-limited output"
    session.record_runner_exit(reason)
    events = session.events(limit=100)
    journal.close()
    assert replay(events).status.value != "failed"
    view = review(events)
    stopped = _as_mapping(view["why_stopped"])
    assert view["status"] == "failed"
    assert stopped["stop_class"] == "runner_exit"
    assert stopped["event_kind"] == "runner_exited"
    assert stopped["reason"] == reason


def test_review_shows_reasoning_tokens_per_call(tmp_path: Path) -> None:
    """A reasoning-only decode is distinguishable from an empty one."""
    root = _build_run(tmp_path, reasoning_tokens=77)

    text = format_show(show_run(root))

    assert "reasoning=77" in text


def test_open_journal_cites_a_terminal_model_failure(tmp_path: Path) -> None:
    """A still-running session that died on the last call names that failure."""
    root = tmp_path / "run"
    root.mkdir()
    journal = SqliteJournal(root / "session.sqlite3")
    artifacts = ArtifactStore(root / "artifacts")
    session = Session.create(
        "session-open-failure",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=2),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=_Environment(),
        runtime=_Runtime(artifacts),
        evaluator=_Evaluator(),
    )
    model = CompatibleModel(_TransportGenerator(), repair_budget=0)
    with pytest.raises(ModelCallError):
        session.step(model)
    (root / "run.json").write_bytes(
        canonical_json(
            RunManifestRecord(
                session_id=session.session_id,
                evaluation_mode="operator",
                created_at="2026-11-10T00:00:00Z",
                package_version="0.2.0",
                settings=RunSettings(
                    objective="inspect the fixture",
                    workspace=str(tmp_path / "workspace"),
                    base_url="https://example.invalid/v1",
                    model="fixture-model",
                    api_key_env="LONGCAT_API_KEY",
                    max_actions=2,
                    max_model_calls=2,
                    max_evaluations=1,
                    max_steps=4,
                    token_budget=16384,
                    max_output_tokens=256,
                    repair_budget=0,
                ),
            ).to_payload()
        ).encode("utf-8")
    )
    journal.close()
    stopped = _as_mapping(show_run(root)["why_stopped"])
    assert stopped["stop_class"] == "open"
    assert stopped["event_kind"] == "model_malformed"
    assert stopped["reason"] == "model transport failure"


def test_show_json_includes_rationale_omission_and_artifact(tmp_path: Path) -> None:
    """A journal-built run exposes the three Phase 1 audit fields."""
    root = _build_run(tmp_path)
    view = show_run(root)
    encoded = json.dumps(view)
    assert RATIONALE in encoded
    reasons: list[str] = []
    for call in _as_list(view["calls"]):
        if not isinstance(call, dict):
            continue
        manifest = call.get("context_manifest")
        if not isinstance(manifest, dict):
            continue
        omission = manifest.get("omission_reasons")
        if isinstance(omission, dict):
            reasons.extend(str(reason) for reason in omission)
    assert reasons
    digests = [
        action.get("artifact_digest")
        for action in _as_list(view["actions"])
        if isinstance(action, dict)
    ]
    assert any(isinstance(digest, str) and len(digest) == 64 for digest in digests)
    assert "input" not in _walk_keys(view)
    assert "result_schemas" not in encoded
    assert view["supervision_policy"] == "not recorded"
    assert _as_mapping(view["why_stopped"])["stop_class"] == "operator_stop"
    settings = _as_mapping(view["settings"])
    assert settings["api_key_env"] == "LONGCAT_API_KEY"
    assert settings["model"] == "fixture-model"
    assert "runtime.sqlite3" not in encoded


def test_show_command_json_matches_the_library(tmp_path: Path, capsys) -> None:
    """The command line prints the same JSON projection."""
    root = _build_run(tmp_path)
    assert main(["show", str(root), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["why_stopped"]["stop_class"] == "operator_stop"
    assert RATIONALE in json.dumps(printed)


def test_compare_keeps_stop_class(tmp_path: Path) -> None:
    """Compare rows come from the same projector as the screen."""
    root = _build_run(tmp_path)
    report = compare_runs([root])
    row = _as_mapping(_as_list(report["runs"])[0])
    actions = row["actions"]
    assert row["plan_proposed"] is False
    assert row["evaluation_requested"] is False
    assert row["supervision_fires"] == 0
    assert row["stop_class"] == "operator_stop"
    assert isinstance(actions, int) and actions > 0


def test_gate_log_names_failures(tmp_path: Path) -> None:
    """An external gate log contributes failing check names only."""
    path = tmp_path / "gate.log"
    path.write_text(
        "PASS 1: provider probe\nFAIL 4: output budget\nGATE PASS: may launch\n",
        encoding="utf-8",
    )
    parsed = parse_gate_log(path)
    assert parsed["passed"] == 1
    assert parsed["failed"] == 1
    assert parsed["failures"] == [{"check": "4", "detail": "output budget"}]
    assert parsed["source"] == "external"


@pytest.mark.skipif(
    not BUNDLE.exists(), reason="paired-recovery bundle is not retained"
)
def test_paired_recovery_review_matches_evidence_counters() -> None:
    """The retained supervised stream replays to the same counters."""
    events = _read_events(BUNDLE)
    view = review(events)
    counted = derive_counters(events)
    counters = _as_mapping(view["counters"])
    assert counters["actions"] == counted.actions
    assert counters["model_calls"] == counted.model_calls
    assert counters["supervisor_calls"] == counted.supervisor_calls
    sequences = [
        call["sequence"]
        for call in _as_list(view["calls"])
        if isinstance(call, dict) and isinstance(call.get("sequence"), int)
    ]
    assert sequences == sorted(sequences)
    assert _as_mapping(view["compare"])["calls"] == counted.model_calls
