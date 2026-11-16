"""External check-command evaluation and operator decision construction.

The check command is operator-selected; its native exit and output are retained
with the evaluated request identity. Timeouts and unavailable checkers never
count as passes. Operator answers become typed ``EvaluationDecision`` commands;
receipt derivation for those lives in the session, which owns promotion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol
import hashlib
import os
import subprocess

from ..artifacts import ArtifactStore
from ..codec import canonical_json
from ..models import (
    ArtifactRef,
    ComponentIdentity,
    EvaluationDecision,
    EvaluationReceipt,
    EvaluationRequest,
)
from ..ports import EvaluationReconciliationResult, ReconciliationKind
from .child_environment import ChildEnvironment
from .process_jobs import terminate_process_group

CHECK_EXIT_ZERO = "exit-zero"
CHECK_EXIT_NONZERO = "exit-nonzero"
CHECK_TIMEOUT = "timeout"
CHECK_UNAVAILABLE = "unavailable"

SURVIVORS_NONE = "none"
SURVIVORS_UNKNOWN = "unknown"

_TERMINATION_GRACE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """The native result of one bounded check-command run."""

    kind: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    survivors: str = SURVIVORS_NONE


class CheckRunner(Protocol):
    """Runs one check command; the shape of ``run_check_command``."""

    # A single-call protocol has exactly one method by design.
    # pylint: disable=too-few-public-methods

    def __call__(
        self,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> CheckOutcome: ...


def run_check_command(
    command: tuple[str, ...],
    cwd: Path,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
) -> CheckOutcome:
    """Run one check command in its own process group, bounded by the timeout.

    A timeout terminates the whole owned group; descendants that cannot be
    confirmed dead are reported as survivors rather than hidden.
    """
    try:
        process = subprocess.Popen(  # pylint: disable=consider-using-with
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
    except OSError as exc:
        return CheckOutcome(CHECK_UNAVAILABLE, None, b"", str(exc).encode("utf-8"))
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        survivors = terminate_process_group(process, _TERMINATION_GRACE_SECONDS)
        stdout, stderr, drained = _drain_output(process)
        if not drained:
            survivors = SURVIVORS_UNKNOWN
        return CheckOutcome(CHECK_TIMEOUT, None, stdout, stderr, survivors)
    kind = CHECK_EXIT_ZERO if process.returncode == 0 else CHECK_EXIT_NONZERO
    return CheckOutcome(kind, process.returncode, stdout or b"", stderr or b"")


def _drain_output(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes, bool]:
    """Read a terminated checker's output without blocking on survivors.

    Returns the captured streams and whether the pipes reached EOF within the
    drain bound. A descendant that escaped the group can hold the pipes open,
    so the final drain is bounded and reported as undrained.
    """
    try:
        stdout, stderr = process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        return b"", b"", False
    return stdout or b"", stderr or b"", True


@dataclass(frozen=True, slots=True)
class CheckCommandEvaluator:
    """An ``Evaluator`` over one operator-selected external check command."""

    command: tuple[str, ...]
    workspace: Path
    artifacts: ArtifactStore
    timeout_seconds: float = 600.0
    runner: CheckRunner = run_check_command
    environment: ChildEnvironment = field(default_factory=ChildEnvironment)

    def __post_init__(self) -> None:
        if not self.command or not all(
            isinstance(item, str) and item for item in self.command
        ):
            raise ValueError("check command must be a non-empty argv tuple")

    def identity(self) -> ComponentIdentity:
        """Identify this checker by its command and workspace."""
        digest = hashlib.sha256(
            "\0".join((*self.command, str(self.workspace))).encode("utf-8")
        ).hexdigest()
        return ComponentIdentity("check-command", digest[:16])

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Run the check once and retain its native result as evidence."""
        outcome = self.runner(
            self.command,
            self.workspace,
            self.timeout_seconds,
            env=self.environment.build(os.environ),
        )
        stdout_ref = self.artifacts.put(
            outcome.stdout, media_type="text/plain", provenance="check stdout"
        )
        stderr_ref = self.artifacts.put(
            outcome.stderr, media_type="text/plain", provenance="check stderr"
        )
        native_ref = self.artifacts.put(
            canonical_json(
                {
                    "command": list(self.command),
                    "cwd": str(self.workspace),
                    "timeout_seconds": self.timeout_seconds,
                    "kind": outcome.kind,
                    "exit_code": outcome.exit_code,
                    "survivors": outcome.survivors,
                    "stdout_digest": stdout_ref.digest,
                    "stderr_digest": stderr_ref.digest,
                }
            ).encode("utf-8"),
            media_type="application/json",
            provenance="check native result",
        )
        accepted = outcome.kind == CHECK_EXIT_ZERO
        digest = hashlib.sha256(
            b"".join(
                (
                    request.request_id.encode("utf-8"),
                    outcome.kind.encode("utf-8"),
                    str(outcome.exit_code).encode("utf-8"),
                    outcome.survivors.encode("utf-8"),
                    stdout_ref.digest.encode("utf-8"),
                    stderr_ref.digest.encode("utf-8"),
                )
            )
        ).hexdigest()
        comparison = f"check {outcome.kind}"
        if outcome.survivors != SURVIVORS_NONE:
            comparison = f"{comparison} with {outcome.survivors} survivors"
        return EvaluationReceipt(
            receipt_id=f"check-receipt-{digest[:32]}",
            accepted=accepted,
            comparison=comparison,
            evidence=(stdout_ref, stderr_ref),
            completed=False,
            raw_receipt=native_ref,
            **request.receipt_identity(),
        )

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """A lost checker run leaves its outcome unknown, never assumed."""
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


@dataclass(frozen=True, slots=True)
class OperatorAnswer:
    """One operator-surface answer to an evaluation request."""

    accepted: bool
    reason: str
    completed: bool = False
    evidence_path: Path | None = None
    media_type: str = "text/plain"


def operator_decision(
    *,
    command_id: str,
    session_id: str,
    request_id: str,
    answer: OperatorAnswer,
    artifacts: ArtifactStore,
) -> EvaluationDecision:
    """Build one typed operator decision from an operator-surface answer.

    Evidence supplied by the operator is published to the artifact store first;
    the decision model rejects empty reasons and evidence-less acceptances.
    """
    reference: ArtifactRef | None = None
    if answer.evidence_path is not None:
        reference = artifacts.put(
            answer.evidence_path.read_bytes(),
            media_type=answer.media_type,
            provenance="operator evaluation",
        )
    return EvaluationDecision(
        command_id=command_id,
        session_id=session_id,
        request_id=request_id,
        accepted=answer.accepted,
        completed=answer.completed,
        reason=answer.reason,
        evidence_ref=reference,
    )
