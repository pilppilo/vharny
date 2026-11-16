"""ARC-AGI-3 runtime: submit ``reset`` and ``step`` to one toolkit game (COD-36).

Each proposal is claimed durably before anything is sent, and its receipt is
stored afterwards (the same ``RuntimeStore`` the process runtime uses). A
proposal seen again returns its stored receipt; one claimed but never answered
is INDETERMINATE and is never sent again.

**Unknown outcomes.** A toolkit call that raises, times out, answers ``None``
or answers a malformed frame may still have reached the game. Its receipt is
INDETERMINATE, the runtime forgets the current frame, and ``step`` is refused
until a ``reset`` succeeds. Nothing is resent automatically.

**Action limit.** ``max_actions`` bounds the game actions sent over the whole
run, across reopens: every ``step`` is counted durably just before it is sent,
including ones whose outcome turns out unknown. Resets do not count (the
scorecard counts them separately); they are bounded by the session's own
action budget.

**What the model sees.** Each receipt's artifact holds the outcome, any error,
the actions left and the bounded observation (``ArcFrame.to_json``). Every
frame's full evidence (all layers, guid, action echo) is stored as its own
artifact and named by digest in the receipt's state.

**Reopening.** A reopened runtime does not know the game's current frame, so
its first action must be ``reset``.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from ..artifacts import ArtifactStore
from ..codec import canonical_json
from ..environments import _action_receipt, published_observation
from ..errors import ContractError
from ..models import (
    ActionProposal,
    ArtifactRef,
    ComponentIdentity,
    EnvironmentContract,
    ExecutionReceipt,
    ExecutionStatus,
    JsonValue,
    Observation,
    StateRef,
    UsageMeasurement,
    frozen_mapping,
)
from ..ports import ReconciliationKind, ReconciliationResult
from .arc_agi import (
    ArcAction,
    ArcFrame,
    ArcStepOutcome,
    ToolkitFrame,
    arc_environment_contract,
)
from .arc_agi_toolkit import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    ArcCallBusy,
    ArcCallError,
    ArcGameConnection,
    TimedCaller,
)
from .runtime_store import RuntimeStore, proposal_fingerprint

ARC_RUNTIME_ID = "arc-agi-3-runtime"
#: Bumped when receipt, artifact or state shapes change.
ARC_RUNTIME_VERSION = "1"
_STEP = "step"
_RESET = "reset"


class ArcRuntime:  # pylint: disable=too-many-instance-attributes
    """Runtime port for one ARC-AGI-3 game through an ``ArcGameConnection``."""

    def __init__(
        self,
        connection: ArcGameConnection,
        artifacts: ArtifactStore,
        store_path: str | Path,
        *,
        max_actions: int,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(max_actions, bool)
            or not isinstance(max_actions, int)
            or max_actions < 1
        ):
            raise ContractError("ARC max_actions must be a positive integer")
        self._connection = connection
        self._info = connection.info
        self._artifacts = artifacts
        self._caller = TimedCaller(call_timeout_seconds)
        self._store = RuntimeStore(store_path)
        self.max_actions = max_actions
        self._frame: ArcFrame | None = None
        self._evidence: ArtifactRef | None = None
        self._closed = False

    @property
    def game_id(self) -> str:
        """The toolkit game this runtime plays."""
        return self._info.game_id

    @property
    def actions_taken(self) -> int:
        """Game actions sent over the whole run, including unknown outcomes."""
        return self._store.count_sends(_STEP)

    @property
    def frame(self) -> ArcFrame | None:
        """The current frame, or ``None`` when it is not known."""
        return self._frame

    def describe(self) -> EnvironmentContract:
        """The fixed contract for this game; it never depends on a frame."""
        return arc_environment_contract(
            self.game_id,
            StateRef(f"arc-agi-3:{self.game_id}", "initial", restorable=False),
        )

    def identity(self) -> ComponentIdentity:
        """Pin toolkit versions, game, seed, mode, base URL and limits."""
        configuration = canonical_json(
            {
                "runtime_version": ARC_RUNTIME_VERSION,
                "toolkit": dict(self._info.to_json()),
                "max_actions": self.max_actions,
                "call_timeout_seconds": self._caller.timeout_seconds,
            }
        )
        return ComponentIdentity(
            ARC_RUNTIME_ID,
            self._info.toolkit_version,
            configuration_digest=hashlib.sha256(
                configuration.encode("utf-8")
            ).hexdigest(),
        )

    def initial_observation(
        self, session_id: str, *, objective_version: int = 1
    ) -> Observation:
        """Reset the game and publish its first frame as the session start."""
        self._require_open()
        try:
            raw = self._caller.call("reset", self._connection.reset)
        except ArcCallBusy as exc:
            raise ArcCallError(f"cannot start the session: {exc}") from exc
        if raw is None:
            raise ArcCallError("the ARC toolkit answered the first reset with no frame")
        frame = self._accept(raw)
        return published_observation(
            session_id=session_id,
            state=self._state_ref(),
            provenance="arc-agi-3-reset",
            artifact=self._publish_result("succeeded", None, frame),
            objective_version=objective_version,
        )

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Send ``reset`` or ``step`` once, with write-ahead intent.

        Refusals are REJECTED receipts, never escaping exceptions, so the
        model gets feedback. Nothing refused is sent to the toolkit.
        """
        self._require_open()
        started = time.monotonic()
        fingerprint = proposal_fingerprint(proposal)
        existing, prior = self._store.prepare(proposal.proposal_id, fingerprint)
        if prior is not None:
            return prior
        if existing:
            receipt = self._unknown(
                proposal,
                "this proposal was claimed earlier without a receipt; "
                "whether it reached the game is unknown",
                started,
                count_action=False,
            )
        elif proposal.action_name == _RESET:
            receipt = self._send(proposal, _RESET, self._connection.reset, started)
        elif proposal.action_name == _STEP:
            receipt = self._step(proposal, started)
        else:
            receipt = self._refuse(
                proposal, f"unknown ARC action {proposal.action_name!r}", started
            )
        return self._store.finish(receipt, fingerprint)

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Return a stored receipt, or UNKNOWN for an unanswered claim."""
        receipt = self._store.receipt(operation_id)
        if receipt is not None:
            return ReconciliationResult(ReconciliationKind.RECEIPT, receipt)
        return ReconciliationResult(ReconciliationKind.UNKNOWN)

    def close(self) -> None:
        """Release the game and the store. A call still running is abandoned."""
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        finally:
            self._store.close()

    # ---- submission -----------------------------------------------------------

    def _step(self, proposal: ActionProposal, started: float) -> ExecutionReceipt:
        frame = self._frame
        if frame is None:
            return self._refuse(
                proposal,
                "the current frame is unknown (the runtime was reopened or an "
                "earlier outcome is unknown); reset first",
                started,
            )
        try:
            action = ArcAction.from_arguments(proposal.arguments, frame)
        except ContractError as exc:
            return self._refuse(proposal, str(exc), started)
        if self.actions_taken >= self.max_actions:
            return self._refuse(
                proposal,
                f"the action limit of {self.max_actions} is reached; "
                "no more game actions can be sent",
                started,
            )
        data = action.toolkit_data()
        return self._send(
            proposal,
            _STEP,
            lambda: self._connection.step(action.action_id, data),
            started,
        )

    def _send(
        self,
        proposal: ActionProposal,
        kind: str,
        call: Callable[[], ToolkitFrame | None],
        started: float,
    ) -> ExecutionReceipt:
        if self._caller.busy:
            return self._refuse(
                proposal,
                "an earlier ARC toolkit call is still running; nothing was sent",
                started,
            )
        self._store.record_send(proposal.proposal_id, kind)
        try:
            raw = self._caller.call(kind, call)
        except (ArcCallError, ArcCallBusy) as exc:
            return self._unknown(proposal, str(exc), started)
        if raw is None:
            return self._unknown(
                proposal,
                f"the ARC toolkit answered {kind} with no frame; "
                "whether it took effect is unknown",
                started,
            )
        try:
            frame = self._accept(raw)
        except ContractError as exc:
            return self._unknown(proposal, f"{exc}; the game state is unknown", started)
        return self._receipt(
            proposal,
            ExecutionStatus.SUCCEEDED,
            None,
            self._publish_result("succeeded", None, frame),
            started,
        )

    def _accept(self, raw: ToolkitFrame) -> ArcFrame:
        """Validate a toolkit frame and make it current; forget it on failure."""
        self._frame = None
        self._evidence = None
        frame = ArcFrame.from_toolkit(raw)
        if frame.game_id != self.game_id:
            raise ContractError(
                f"ARC toolkit answered for game {frame.game_id!r}, "
                f"not {self.game_id!r}"
            )
        self._evidence = self._artifacts.put(
            canonical_json(frame.evidence_json()).encode("utf-8"),
            media_type="application/json",
            provenance="arc-agi-3-frame-evidence",
        )
        self._frame = frame
        return frame

    def _refuse(
        self, proposal: ActionProposal, error: str, started: float
    ) -> ExecutionReceipt:
        return self._receipt(
            proposal,
            ExecutionStatus.REJECTED,
            error,
            self._publish_result("rejected", error, self._frame),
            started,
        )

    def _unknown(
        self,
        proposal: ActionProposal,
        error: str,
        started: float,
        *,
        count_action: bool = True,
    ) -> ExecutionReceipt:
        self._frame = None
        self._evidence = None
        return self._receipt(
            proposal,
            ExecutionStatus.INDETERMINATE,
            error,
            self._publish_result("unknown", error, None),
            started,
            count_action=count_action,
        )

    def _receipt(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        proposal: ActionProposal,
        status: ExecutionStatus,
        error: str | None,
        artifact: ArtifactRef,
        started: float,
        *,
        count_action: bool = True,
    ) -> ExecutionReceipt:
        receipt = _action_receipt(
            proposal, status, self._state_ref(), error=error, raw_artifact=artifact
        )
        usage = (
            UsageMeasurement(
                measurement_id=f"runtime-usage-{proposal.proposal_id}",
                actions=1,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if count_action
            else None
        )
        return replace(receipt, usage=usage)

    # ---- published records ----------------------------------------------------

    def _publish_result(
        self, outcome: str, error: str | None, frame: ArcFrame | None
    ) -> ArtifactRef:
        """The model-facing record: outcome, error, actions left, observation."""
        payload: Mapping[str, JsonValue] = frozen_mapping(
            {
                "outcome": outcome,
                "error": error,
                "actions_left": self._actions_left(frame),
                "observation": None if frame is None else frame.to_json(),
            }
        )
        return self._artifacts.put(
            canonical_json(payload).encode("utf-8"),
            media_type="application/json",
            provenance="arc-agi-3-result",
        )

    def _actions_left(self, frame: ArcFrame | None) -> int:
        taken = min(self.actions_taken, self.max_actions)
        if (
            frame is not None
            and ArcStepOutcome(frame, taken, self.max_actions).truncated
        ):
            return 0
        return self.max_actions - taken

    def _state_ref(self) -> StateRef:
        """A short, content-addressed cursor; the evidence artifact holds the rest."""
        owner = f"arc-agi-3:{self.game_id}"
        frame, evidence = self._frame, self._evidence
        if frame is None or evidence is None:
            return StateRef(owner, "unknown", restorable=False)
        value = canonical_json(
            {
                "state": frame.state.value,
                "levels_completed": frame.levels_completed,
                "actions_taken": self.actions_taken,
                "evidence": evidence.digest,
            }
        )
        return StateRef(owner, value, digest=evidence.digest, restorable=False)

    def _require_open(self) -> None:
        if self._closed:
            raise ContractError("ARC runtime is closed")
