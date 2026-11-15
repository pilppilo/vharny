"""Opt-in deterministic Phase 1 evidence demo."""

from __future__ import annotations

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from .artifacts import ArtifactStore
from .fakes import CandidateRestorableEnvironment, TrajectoryOnlyEnvironment
from .journal import SqliteJournal
from .models import (
    BudgetLimits,
    ComponentIdentity,
    CompletionMode,
    ExecutionReceipt,
    ExecutionStatus,
    EvaluationReceipt,
    RunIdentity,
    SessionStatus,
    TaskSpec,
    UsageMeasurement,
    ActionProposal,
    EvaluationRequest,
)
from .ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from .session import Session


class _Runtime:
    def identity(self) -> ComponentIdentity:
        """Identify the deterministic runtime fixture."""
        return ComponentIdentity("deterministic-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Return a deterministic terminal receipt."""
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=proposal.expected_state,
            usage=UsageMeasurement(
                measurement_id=f"runtime-usage-{proposal.proposal_id}", actions=1
            ),
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Report that no recovery submission is needed."""
        del operation_id
        return ReconciliationResult(ReconciliationKind.NOT_SUBMITTED)


class _Evaluator:
    def __init__(self, completion_evidence) -> None:
        self._completion_evidence = completion_evidence

    def identity(self) -> ComponentIdentity:
        """Identify the deterministic evaluator fixture."""
        return ComponentIdentity("deterministic-evaluator", "v1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Return deterministic external acceptance for the supplied request."""
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            accepted=True,
            comparison="improved",
            completed=True,
            completion_evidence=(self._completion_evidence,),
            required_evidence=(self._completion_evidence.digest,),
            usage=UsageMeasurement(
                measurement_id=f"evaluator-usage-{request.request_id}", evaluations=1
            ),
            **request.receipt_identity(),
        )

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """Report evaluator recovery as intentionally unsupported."""
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNSUPPORTED)


def main(argv: list[str] | None = None) -> int:
    """Run one deterministic session only when explicitly invoked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("candidate", "trajectory"), required=True)
    parser.add_argument("--directory", type=Path)
    arguments = parser.parse_args(argv)
    environment = (
        CandidateRestorableEnvironment()
        if arguments.profile == "candidate"
        else TrajectoryOnlyEnvironment()
    )
    if arguments.directory is None:
        with TemporaryDirectory(prefix="vharness-demo-") as directory:
            _run(Path(directory), environment)
    else:
        arguments.directory.mkdir(parents=True, exist_ok=True)
        _run(arguments.directory, environment)
    return 0


def _run(directory: Path, environment) -> None:
    journal = SqliteJournal(directory / "session.sqlite")
    artifacts = ArtifactStore(directory / "artifacts")
    task = TaskSpec(
        task_id="deterministic-demo",
        objective="exercise durable session evidence",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluation",
        budgets=BudgetLimits(actions=1),
        environment_id=environment.environment_id,
        evaluation_contract_id="demo-evaluator-v1",
    )
    completion_evidence = artifacts.put(
        b'{"completion":"verified by deterministic evaluator"}',
        media_type="application/json",
        provenance="deterministic-evaluator",
    )
    identity = RunIdentity(
        package_version="0.2.0",
        configuration_digest="d" * 64,
        runtime=ComponentIdentity("deterministic-runtime", "v1"),
        evaluator=ComponentIdentity("deterministic-evaluator", "v1"),
    )
    session = Session.create(
        "deterministic-demo",
        task,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=_Runtime(),
        evaluator=_Evaluator(completion_evidence),
        run_identity=identity,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="deterministic demo")
    session.request_evaluation(receipt.state)
    session.checkpoint()
    reopened = Session.open(
        "deterministic-demo",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=_Runtime(),
        evaluator=_Evaluator(completion_evidence),
        run_identity=identity,
    )
    if (
        session.manifest() != reopened.manifest()
        or session.counters() != reopened.counters()
        or reopened.view().status is not SessionStatus.COMPLETED
    ):
        raise RuntimeError("demo replay evidence disagrees after reopen")
    print(reopened.manifest())
    print(reopened.counters())
    print(reopened.view())


if __name__ == "__main__":
    raise SystemExit(main())
