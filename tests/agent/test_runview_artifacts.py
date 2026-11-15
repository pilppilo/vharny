"""Checks for the read-only artifact index and verified body fetch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from vharness.agent import (
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
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json
from vharness.agent.errors import ContractError, IntegrityError
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import CompatibleModel
from vharness.agent.models import (
    ArtifactRef,
    EnvironmentContract,
    EvaluationReceipt,
    EvaluationRequest,
    ExecutionStatus,
    RuntimeCapabilities,
    Stop,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.run_settings import RunManifestRecord, RunSettings
from tools.runview import main as runview_main
from vharness.agent.runview_artifacts import index_run, main, read_artifact

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


class _Generator:
    def __init__(self, texts: tuple[str, ...]) -> None:
        self._texts = list(texts)

    def generate(self, system: str, prompt: str) -> _Generation:
        del system, prompt
        return _Generation(self._texts.pop(0))


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


class _ChunkedRuntime:
    """Publishes output chunks and a summary that references them."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts
        self._count = 0

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        self._count += 1
        stdout = self._artifacts.put(
            f"stdout {self._count}".encode("utf-8"),
            media_type="application/octet-stream",
            provenance="process-output-stdout",
        )
        stderr = self._artifacts.put(
            b"warning",
            media_type="application/octet-stream",
            provenance="process-output-stderr",
        )
        summary = {
            "exit_code": 0,
            "output_artifacts": (
                _chunk_reference(stdout, 0, "stdout"),
                _chunk_reference(stderr, 0, "stderr"),
            ),
            "stdout": "stdout",
        }
        artifact = self._artifacts.put(
            canonical_json(summary).encode("utf-8"),
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


def _chunk_reference(
    reference: ArtifactRef, offset: int, stream: str
) -> dict[str, object]:
    """Return the summary entry shape the runtime writes for one chunk."""
    return {
        "artifact": {
            "digest": reference.digest,
            "media_type": reference.media_type,
            "provenance": reference.provenance,
            "size": reference.size,
        },
        "length": reference.size,
        "offset": offset,
        "stream": stream,
    }


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


def _build_run(tmp_path: Path, *, chunked: bool = False) -> Path:
    """Build a stopped run directory through the journal API."""
    root = tmp_path / "run"
    root.mkdir()
    journal = SqliteJournal(root / "session.sqlite3")
    artifacts = ArtifactStore(root / "artifacts")
    runtime = _ChunkedRuntime(artifacts) if chunked else _Runtime(artifacts)
    session = Session.create(
        "session-artifacts",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=_Environment(),
        runtime=runtime,
        evaluator=_Evaluator(),
    )
    texts = (_action(0), _action(1), '{"kind": "LineageQuery"}')
    model = CompatibleModel(_Generator(texts), repair_budget=0)
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
            max_actions=4,
            max_model_calls=4,
            max_evaluations=1,
            max_steps=10,
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


def _as_list(value: object) -> list[object]:
    """Return a JSON list or fail the test."""
    if not isinstance(value, list):
        raise AssertionError(value)
    return value


def _rows(index: dict[str, object]) -> list[dict[str, object]]:
    """Return the index rows as typed mappings."""
    return [row for row in _as_list(index["artifacts"]) if isinstance(row, dict)]


def _digest(index: dict[str, object]) -> str:
    """Return the first indexed digest or fail the test."""
    rows = _rows(index)
    digest = rows[0].get("digest")
    if not isinstance(digest, str):
        raise AssertionError(rows)
    return digest


def _path_for(root: Path, digest: str) -> Path:
    return root / "artifacts" / digest[:2] / digest[2:]


def test_index_maps_digests_to_proposal_action_and_receipt(tmp_path: Path) -> None:
    """Every recorded artifact is reachable through the proposal chain."""
    root = _build_run(tmp_path)
    rows = _rows(index_run(root))
    assert len(rows) == 2
    for row in rows:
        digest = row["digest"]
        assert isinstance(digest, str) and len(digest) == 64
        path = _path_for(root, digest)
        assert path.is_file()
        assert row["available"] is True
        assert row["size"] == path.stat().st_size
        assert row["receipt_status"] == "succeeded"
        assert row["action_name"] == "inspect"
        assert isinstance(row["proposal_id"], str)
        assert isinstance(row["request_id"], str)
        assert isinstance(row["action_sequence"], int)
        assert isinstance(row["receipt_sequence"], int)


def test_index_enumerates_the_output_chunks(tmp_path: Path) -> None:
    """Each summary's output references become reachable chunk rows."""
    root = _build_run(tmp_path, chunked=True)
    index = index_run(root)
    summaries = _rows(index)
    outputs = [row for row in _as_list(index["outputs"]) if isinstance(row, dict)]
    assert len(outputs) == 4
    assert {row["stream"] for row in outputs} == {"stdout", "stderr"}
    summary_digests = {row["digest"] for row in summaries}
    for row in outputs:
        assert row["summary_digest"] in summary_digests
        assert row["available"] is True
        assert row["offset"] == 0
        assert isinstance(row["length"], int) and row["length"] > 0
        body = read_artifact(root, str(row["digest"]))
        assert hashlib.sha256(body).hexdigest() == row["digest"]
        assert len(body) == row["length"] == row["size"]


def test_index_without_output_references_reports_none(tmp_path: Path) -> None:
    """A summary that references no chunks yields an empty output list."""
    root = _build_run(tmp_path)
    assert index_run(root)["outputs"] == []


def test_index_screen_lists_the_output_chunks(tmp_path: Path, capsys) -> None:
    """The screen surfaces the chunk rows the JSON projection carries."""
    root = _build_run(tmp_path, chunked=True)
    assert main(["index", str(root)]) == 0
    printed = capsys.readouterr().out
    assert "outputs: 4" in printed
    assert "stdout @0+" in printed


def test_read_artifact_returns_the_verified_bytes(tmp_path: Path) -> None:
    """A fetched body hashes to the digest that addressed it."""
    root = _build_run(tmp_path)
    digest = _digest(index_run(root))
    body = read_artifact(root, digest)
    assert hashlib.sha256(body).hexdigest() == digest
    assert body == _path_for(root, digest).read_bytes()


def test_read_artifact_rejects_tampered_bytes(tmp_path: Path) -> None:
    """Bytes that no longer hash to the digest are refused."""
    root = _build_run(tmp_path)
    digest = _digest(index_run(root))
    _path_for(root, digest).write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        read_artifact(root, digest)


def test_read_artifact_rejects_an_invalid_digest(tmp_path: Path) -> None:
    """A digest that is not a sha-256 hex value is a contract failure."""
    root = _build_run(tmp_path)
    with pytest.raises(ContractError):
        read_artifact(root, "not-a-digest")


def test_runview_artifacts_subcommand_lists_the_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``runview artifacts`` is the same index as the sibling command."""
    root = _build_run(tmp_path)
    assert runview_main(["artifacts", "index", str(root)]) == 0
    assert "inspect" in capsys.readouterr().out


def test_index_command_json_matches_the_library(tmp_path: Path, capsys) -> None:
    """The command line prints the same rows the library returns."""
    root = _build_run(tmp_path)
    assert main(["index", str(root), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["artifacts"] == index_run(root)["artifacts"]


def test_body_command_writes_the_verified_body(tmp_path: Path) -> None:
    """The body command writes the verified bytes to the requested file."""
    root = _build_run(tmp_path)
    digest = _digest(index_run(root))
    output = tmp_path / "body.bin"
    assert main(["body", str(root), digest, "--output", str(output)]) == 0
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest


def test_body_command_reports_a_missing_artifact(tmp_path: Path, capsys) -> None:
    """A digest with no stored body fails without writing anything."""
    root = _build_run(tmp_path)
    assert main(["body", str(root), "0" * 64, "--output", str(tmp_path / "x")]) == 1
    assert "not stored" in capsys.readouterr().err
