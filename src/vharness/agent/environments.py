"""Software fixture and shared Gymnasium contract/evaluator mechanics."""

# pylint: disable=too-few-public-methods

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Mapping, cast

from .artifacts import ArtifactStore
from .codec import MAX_JSON_BYTES, canonical_json, parse_json
from .errors import ContractError, PersistenceError
from .models import (
    ActionDefinition,
    ActionProposal,
    ArtifactRef,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    EvaluationRequest,
    ExecutionReceipt,
    ExecutionStatus,
    JsonValue,
    Observation,
    RecordEnvelope,
    RuntimeCapabilities,
    StateRef,
    UsageMeasurement,
    frozen_mapping,
)
from .ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from .session_state import receipt_from_payload

_PASSING_SOFTWARE = "value = 1"


@dataclass
class _SoftwareFixtureState:
    """One visible or candidate software-fixture checkpoint."""

    files: dict[str, str]
    last_check_passed: bool
    last_tested: bool
    revision: int
    receipts: dict[str, ExecutionReceipt]
    fingerprints: dict[str, str]


@dataclass(frozen=True)
class _StagedSoftwareAction:
    """Artifacts and candidate state prepared before one durable commit."""

    state: _SoftwareFixtureState
    artifact: ArtifactRef | None
    cleanup_digest: str | None


@dataclass(frozen=True, slots=True)
class NativeGoal:
    """Predeclared native acceptance rule. Evaluators own this, not runtimes."""

    require_terminated: bool = False
    reject_truncated: bool = True
    min_return: float | None = None

    def accepts(self, payload: Mapping[str, JsonValue]) -> bool:
        """Accept only the requested-state official tuple against this goal."""
        if self.reject_truncated and bool(payload.get("truncated")):
            return False
        if self.require_terminated and not bool(payload.get("terminated")):
            return False
        if self.min_return is None:
            return True
        episode_return = payload.get("return")
        if isinstance(episode_return, bool) or not isinstance(
            episode_return, (int, float)
        ):
            return False
        return float(episode_return) >= self.min_return


DISCRETE_REACH_GOAL = NativeGoal(
    require_terminated=True, reject_truncated=True, min_return=1.0
)
CONTINUOUS_DRIVE_GOAL = NativeGoal(require_terminated=True, reject_truncated=True)
FROZENLAKE_GOAL = NativeGoal(
    require_terminated=True, reject_truncated=True, min_return=1.0
)
_SOFTWARE_ACTIONS = (
    ActionDefinition(
        "read",
        "Read a workspace file",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ("path",),
            "additionalProperties": False,
        },
    ),
    ActionDefinition(
        "write",
        "Write a workspace file",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ("path", "content"),
            "additionalProperties": False,
        },
    ),
    ActionDefinition(
        "test",
        "Run the workspace check",
        {"type": "object", "additionalProperties": False},
    ),
)


class SoftwareFixture:
    """In-process software workspace. The kernel still only proposes actions."""

    @property
    def files(self) -> dict[str, str]:
        """Expose the current workspace files to fixture callers."""
        return self._checkpoint.files

    @files.setter
    def files(self, value: dict[str, str]) -> None:
        self._checkpoint.files = value

    @property
    def last_check_passed(self) -> bool:
        """Expose the last workspace check result."""
        return self._checkpoint.last_check_passed

    @last_check_passed.setter
    def last_check_passed(self, value: bool) -> None:
        self._checkpoint.last_check_passed = value

    @property
    def _revision(self) -> int:
        return self._checkpoint.revision

    @_revision.setter
    def _revision(self, value: int) -> None:
        self._checkpoint.revision = value

    @property
    def _last_tested(self) -> bool:
        return self._checkpoint.last_tested

    @_last_tested.setter
    def _last_tested(self, value: bool) -> None:
        self._checkpoint.last_tested = value

    @property
    def _receipts(self) -> dict[str, ExecutionReceipt]:
        return self._checkpoint.receipts

    @_receipts.setter
    def _receipts(self, value: dict[str, ExecutionReceipt]) -> None:
        self._checkpoint.receipts = value

    @property
    def _receipt_fingerprints(self) -> dict[str, str]:
        return self._checkpoint.fingerprints

    @_receipt_fingerprints.setter
    def _receipt_fingerprints(self, value: dict[str, str]) -> None:
        self._checkpoint.fingerprints = value

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        environment_id: str = "software-fixture",
        initial_state: StateRef | None = None,
    ) -> None:
        """Restore the workspace while retaining a supplied session creation state."""
        self.environment_id = environment_id
        self._checkpoint = _SoftwareFixtureState(
            files={"main.py": "value = 0\n"},
            last_check_passed=False,
            last_tested=False,
            revision=0,
            receipts={},
            fingerprints={},
        )
        self._artifacts = artifacts
        self._state_record = self._artifacts.root / ".software-fixture-state"
        self._snapshots: dict[str, tuple[dict[str, str], bool, bool]] = {}
        self._record_snapshot(tested=False, check_passed=False)
        with self._artifacts.synchronized():
            self._load_state()
        if initial_state is not None and (
            not isinstance(initial_state, StateRef)
            or initial_state.owner != self.environment_id
        ):
            raise ContractError("software fixture initial state owner is invalid")
        self._initial_state = initial_state or self._state()
        self._initial_state_bound = initial_state is not None
        self._record_snapshot(
            tested=self._last_tested, check_passed=self.last_check_passed
        )

    def describe(self) -> EnvironmentContract:
        """Declare a stable contract with durable receipt recovery."""
        return EnvironmentContract(
            environment_id=self.environment_id,
            version="1",
            initial_state=self._initial_state,
            actions=_SOFTWARE_ACTIONS,
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )

    def identity(self) -> ComponentIdentity:
        """Identify the software-fixture runtime."""
        return ComponentIdentity("software-fixture-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Apply one proposal with durable idempotency by proposal identity."""
        fingerprint = canonical_json(proposal)
        with self._artifacts.synchronized():
            prior = self._receipts.get(proposal.proposal_id)
            if prior is not None:
                if self._receipt_fingerprints.get(proposal.proposal_id) != fingerprint:
                    raise ContractError(
                        "idempotency key was reused with a different proposal"
                    )
                return prior
            return self._submit_once(proposal, fingerprint)

    def _submit_once(
        self, proposal: ActionProposal, fingerprint: str
    ) -> ExecutionReceipt:
        """Stage one action and publish its receipt with one durable checkpoint."""
        staged = self._stage_action(proposal)
        if isinstance(staged, ExecutionReceipt):
            self._remember_receipt(proposal, fingerprint, staged)
            return staged
        return self._commit_staged_action(proposal, fingerprint, staged)

    def _stage_action(
        self, proposal: ActionProposal
    ) -> _StagedSoftwareAction | ExecutionReceipt:
        if proposal.action_name == "read":
            return self._stage_read(proposal)
        if proposal.action_name == "write":
            return self._stage_write(proposal)
        if proposal.action_name == "test":
            return self._stage_test()
        return _action_receipt(
            proposal,
            ExecutionStatus.FAILED,
            self._state(),
            error=f"unknown action {proposal.action_name!r}",
        )

    def _stage_read(
        self, proposal: ActionProposal
    ) -> _StagedSoftwareAction | ExecutionReceipt:
        path = _string_argument(proposal, "path")
        if path not in self.files:
            return _action_receipt(
                proposal,
                ExecutionStatus.FAILED,
                self._state(),
                error=f"missing path {path!r}",
            )
        read_bytes = canonical_json(
            {"path": path, "content": self.files[path]}
        ).encode()
        digest = hashlib.sha256(read_bytes).hexdigest()
        preexisting = self._artifacts.available(digest)
        artifact = self._artifacts.put(
            read_bytes,
            media_type="application/json",
            provenance="software-fixture-read",
        )
        cleanup_digest = None if preexisting else artifact.digest
        return _StagedSoftwareAction(
            replace(self._checkpoint), artifact, cleanup_digest
        )

    def _stage_write(self, proposal: ActionProposal) -> _StagedSoftwareAction:
        files = dict(self.files)
        files[_string_argument(proposal, "path")] = _string_argument(
            proposal, "content"
        )
        state = replace(
            self._checkpoint,
            files=files,
            last_check_passed=False,
            last_tested=False,
            revision=self._revision + 1,
        )
        self._encode_state(state)
        workspace_bytes = canonical_json(
            {"files": dict(files), "paths": sorted(files)}
        ).encode()
        digest = hashlib.sha256(workspace_bytes).hexdigest()
        preexisting = self._artifacts.available(digest)
        artifact = self.workspace_record(
            provenance="software-fixture-write", files=files
        )
        cleanup_digest = None if preexisting else artifact.digest
        return _StagedSoftwareAction(state, artifact, cleanup_digest)

    def _stage_test(self) -> _StagedSoftwareAction:
        check_passed = _PASSING_SOFTWARE in self.files.get("main.py", "")
        state = replace(
            self._checkpoint,
            last_check_passed=check_passed,
            last_tested=True,
            revision=self._revision + 1,
        )
        test_bytes = canonical_json({"check_passed": check_passed}).encode()
        digest = hashlib.sha256(test_bytes).hexdigest()
        preexisting = self._artifacts.available(digest)
        artifact = self._artifacts.put(
            test_bytes,
            media_type="application/json",
            provenance="software-fixture-test",
        )
        cleanup_digest = None if preexisting else artifact.digest
        return _StagedSoftwareAction(state, artifact, cleanup_digest)

    def _commit_staged_action(
        self,
        proposal: ActionProposal,
        fingerprint: str,
        staged: _StagedSoftwareAction,
    ) -> ExecutionReceipt:
        status = (
            ExecutionStatus.FAILED
            if proposal.action_name == "test" and not staged.state.last_check_passed
            else ExecutionStatus.SUCCEEDED
        )
        receipt = _action_receipt(
            proposal,
            status,
            self._state_for(staged.state.files, staged.state.revision),
            raw_artifact=staged.artifact,
        )
        state = replace(
            staged.state,
            receipts={**self._receipts, proposal.proposal_id: receipt},
            fingerprints={
                **self._receipt_fingerprints,
                proposal.proposal_id: fingerprint,
            },
        )
        try:
            self._persist_state(state=state)
        except Exception:
            if staged.cleanup_digest is not None:
                self._artifacts.drop(staged.cleanup_digest)
            raise
        self._checkpoint = state
        self._record_snapshot(
            tested=state.last_tested, check_passed=state.last_check_passed
        )
        return receipt

    def _load_state(self) -> None:
        """Restore workspace and receipt state when a fixture is reopened."""
        if not self._state_record.is_file():
            return
        try:
            payload = parse_json(self._state_record.read_bytes())
        except (ContractError, OSError) as exc:
            raise PersistenceError("software fixture state record is invalid") from exc
        if not isinstance(payload, Mapping):
            raise PersistenceError("software fixture state record must be an object")
        if payload.get("environment_id") != self.environment_id:
            raise PersistenceError(
                "software fixture state belongs to another environment"
            )
        if not _valid_fixture_state_fields(payload):
            raise PersistenceError("software fixture state record fields are invalid")
        files = cast("Mapping[str, str]", payload["files"])
        revision = cast(int, payload["revision"])
        last_tested = cast(bool, payload["last_tested"])
        last_check_passed = cast(bool, payload["last_check_passed"])
        receipts = cast("Mapping[str, object]", payload.get("receipts", {}))
        fingerprints = cast(
            "Mapping[str, object]", payload.get("receipt_fingerprints", {})
        )
        restored_receipts: dict[str, ExecutionReceipt] = {}
        for proposal_id, receipt_payload in receipts.items():
            fingerprint = fingerprints.get(proposal_id)
            if (
                not isinstance(proposal_id, str)
                or not isinstance(fingerprint, str)
                or not isinstance(receipt_payload, Mapping)
            ):
                raise PersistenceError("software fixture receipt state is invalid")
            try:
                receipt = receipt_from_payload(receipt_payload)
            except (ContractError, ValueError) as exc:
                raise PersistenceError(
                    "software fixture receipt state contains an invalid receipt"
                ) from exc
            if receipt.proposal_id != proposal_id:
                raise PersistenceError("software fixture receipt identity is invalid")
            restored_receipts[proposal_id] = receipt
        self._checkpoint = _SoftwareFixtureState(
            files=dict(files),
            last_check_passed=last_check_passed,
            last_tested=last_tested,
            revision=revision,
            receipts=restored_receipts,
            fingerprints={
                proposal_id: cast(str, fingerprints[proposal_id])
                for proposal_id in restored_receipts
            },
        )

    def _remember_receipt(
        self, proposal: ActionProposal, fingerprint: str, receipt: ExecutionReceipt
    ) -> None:
        """Persist one idempotency result before exposing it to the session."""
        self._commit_receipt(proposal, fingerprint, receipt)

    def _commit_receipt(
        self,
        proposal: ActionProposal,
        fingerprint: str,
        receipt: ExecutionReceipt,
        *,
        cleanup_digest: str | None = None,
    ) -> None:
        """Publish a receipt checkpoint and then update the visible indexes."""
        state = replace(
            self._checkpoint,
            receipts={**self._receipts, proposal.proposal_id: receipt},
            fingerprints={
                **self._receipt_fingerprints,
                proposal.proposal_id: fingerprint,
            },
        )
        try:
            self._persist_state(state=state)
        except Exception:
            if cleanup_digest is not None:
                self._artifacts.drop(cleanup_digest)
            raise
        self._checkpoint = state

    def _persist_state(self, *, state: _SoftwareFixtureState | None = None) -> None:
        """Atomically persist workspace and receipt state as one checkpoint."""
        encoded = self._encode_state(self._checkpoint if state is None else state)
        temporary = self._state_record.with_name(f".{self._state_record.name}.pending")
        try:
            with temporary.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_record)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PersistenceError("could not persist software fixture state") from exc

    def workspace_record(
        self,
        files: Mapping[str, str] | None = None,
        *,
        provenance: str = "software-fixture-workspace",
        revision: int | str | None = None,
        tested: bool | None = None,
        check_passed: bool | None = None,
    ) -> ArtifactRef:
        """Publish workspace files, optionally checkpointing a restored state."""
        recorded_files = dict(self.files if files is None else files)
        revision_value = self._revision if revision is None else int(revision)
        tested_value = self._last_tested if tested is None else tested
        check_passed_value = (
            self.last_check_passed if check_passed is None else check_passed
        )
        if revision is not None or tested is not None or check_passed is not None:
            state = replace(
                self._checkpoint,
                files=recorded_files,
                last_check_passed=check_passed_value,
                last_tested=tested_value,
                revision=revision_value,
            )
            with self._artifacts.synchronized():
                self._persist_state(state=state)
                self._checkpoint = state
                if not self._initial_state_bound:
                    self._initial_state = self._state()
                self._record_snapshot(
                    tested=tested_value, check_passed=check_passed_value
                )
        return self._artifacts.put(
            canonical_json(
                {"files": recorded_files, "paths": sorted(recorded_files)}
            ).encode(),
            media_type="application/json",
            provenance=provenance,
        )

    def _encode_state(self, state: _SoftwareFixtureState) -> bytes:
        """Validate and bound a candidate checkpoint before publication."""
        try:
            encoded = canonical_json(
                {
                    "environment_id": self.environment_id,
                    "files": state.files,
                    "last_check_passed": state.last_check_passed,
                    "last_tested": state.last_tested,
                    "revision": state.revision,
                    "receipts": state.receipts,
                    "receipt_fingerprints": state.fingerprints,
                }
            ).encode("utf-8")
        except (ContractError, TypeError, ValueError) as exc:
            raise PersistenceError("software fixture state is not persistable") from exc
        if len(encoded) > MAX_JSON_BYTES:
            raise PersistenceError("software fixture state exceeds byte bound")
        return encoded

    def _state_for(self, files: Mapping[str, str], revision: int) -> StateRef:
        payload = "".join(f"{path}:{files[path]}" for path in sorted(files))
        return StateRef(
            self.environment_id,
            f"rev-{revision}",
            digest=_digest(payload),
            revision=str(revision),
            restorable=True,
        )

    def initial_observation(
        self, session_id: str, *, objective_version: int = 1
    ) -> Observation:
        """Seed a session with the published workspace files."""
        if not self._initial_state_bound:
            self._initial_state = self._state()
        elif self._initial_state != self._state():
            raise ContractError(
                "a bound software fixture cannot seed another stateful session"
            )
        self._initial_state_bound = True
        return published_observation(
            session_id=session_id,
            state=self._initial_state,
            provenance="software-fixture-workspace",
            artifact=self.workspace_record(provenance="software-fixture-initial"),
            objective_version=objective_version,
        )

    def snapshot(self, state: StateRef) -> tuple[dict[str, str], bool, bool] | None:
        """Return files and native test result recorded for one complete state."""
        recorded = self._snapshots.get(_snapshot_key(state))
        if recorded is None:
            return None
        files, tested, check_passed = recorded
        return dict(files), tested, check_passed

    def _record_snapshot(self, *, tested: bool, check_passed: bool) -> None:
        current = self._state()
        self._snapshots[_snapshot_key(current)] = (
            dict(self.files),
            tested,
            check_passed,
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Return a durably stored receipt, or prove no submission is known."""
        receipt = self._receipts.get(operation_id)
        if receipt is None:
            return ReconciliationResult(ReconciliationKind.NOT_SUBMITTED)
        return ReconciliationResult(ReconciliationKind.RECEIPT, receipt)

    def _state(self) -> StateRef:
        return self._state_for(self.files, self._revision)


class SoftwareEvaluator:
    """External software check. Acceptance is the fixture's native test result."""

    def __init__(self, fixture: SoftwareFixture, artifacts: ArtifactStore) -> None:
        self._fixture = fixture
        self._artifacts = artifacts

    def identity(self) -> ComponentIdentity:
        """Identify the software-fixture evaluator."""
        return ComponentIdentity("software-fixture-evaluator", "v1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Accept only the native check recorded for the requested state."""
        snapshot = self._fixture.snapshot(request.evaluated_state)
        passed = bool(snapshot and snapshot[1] and snapshot[2])
        return _native_evaluation(
            request,
            self._artifacts,
            accepted=passed,
            objectives={"check_passed": passed},
            provenance="software-fixture-evaluator",
        )

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """Report evaluator recovery as unsupported for this in-process fixture."""
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNSUPPORTED)


# Official reset/step fields are retained so evaluators report native values.
# pylint: disable=too-many-instance-attributes
class GymnasiumRuntimeCore:
    """Shared official-tuple receipts. Not an environment registry."""

    def __init__(
        self,
        environment_id: str,
        artifacts: ArtifactStore | None,
        tuple_provenance: str,
    ) -> None:
        self.environment_id = environment_id
        self.observation: JsonValue = 0
        self.reward = 0.0
        self.terminated = False
        self.truncated = False
        self.info: Mapping[str, JsonValue] = frozen_mapping()
        self.episode_return = 0.0
        self.episode_length = 0
        self._artifacts = artifacts
        self._receipts: dict[str, ExecutionReceipt] = {}
        self._tuple_provenance = tuple_provenance
        self._initial_state: StateRef | None = None

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Declare recovery unsupported for in-process stateful environments."""
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNSUPPORTED)

    def _clear_episode(
        self, observation: JsonValue, info: Mapping[str, JsonValue]
    ) -> None:
        self.observation = observation
        self.reward = 0.0
        self.terminated = False
        self.truncated = False
        self.info = frozen_mapping(info)
        self.episode_return = 0.0
        self.episode_length = 0
        if self._initial_state is None:
            self._initial_state = self._state()

    def _record_step(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        observation: JsonValue,
        reward: float,
        terminated: bool,
        truncated: bool,
        info: Mapping[str, JsonValue],
    ) -> None:
        self.observation = observation
        self.reward = reward
        self.terminated = terminated
        self.truncated = truncated
        self.info = frozen_mapping(info)
        self.episode_return += reward
        self.episode_length += 1

    def _fail_unknown(self, proposal: ActionProposal) -> ExecutionReceipt:
        receipt = _action_receipt(
            proposal,
            ExecutionStatus.FAILED,
            self._state(),
            error=f"unknown action {proposal.action_name!r}",
        )
        self._receipts[proposal.proposal_id] = receipt
        return receipt

    def _succeed(
        self, proposal: ActionProposal, *, provenance: str | None = None
    ) -> ExecutionReceipt:
        receipt = _action_receipt(
            proposal,
            ExecutionStatus.SUCCEEDED,
            self._state(),
            raw_artifact=self._tuple_artifact(provenance or self._tuple_provenance),
        )
        self._receipts[proposal.proposal_id] = receipt
        return receipt

    def _tuple_payload(self) -> dict[str, JsonValue]:
        return gymnasium_tuple_payload(
            episode_length=self.episode_length,
            info=self.info,
            observation=self.observation,
            episode_return=self.episode_return,
            reward=self.reward,
            terminated=self.terminated,
            truncated=self.truncated,
        )

    def _tuple_artifact(self, provenance: str) -> ArtifactRef | None:
        if self._artifacts is None:
            return None
        return self._artifacts.put(
            canonical_json(self._tuple_payload()).encode("utf-8"),
            media_type="application/json",
            provenance=provenance,
        )

    def _state(self) -> StateRef:
        return gymnasium_state_ref(self.environment_id, self._tuple_payload())

    def _contract(self, step_schema: Mapping[str, JsonValue]) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id=self.environment_id,
            version="1",
            initial_state=self._initial_state or self._state(),
            actions=gymnasium_reset_step_actions(step_schema),
            capabilities=RuntimeCapabilities(idempotency=False, reconciliation=False),
        )


class GymnasiumEvaluator:
    """Reports native return and episode length. Does not invent a score."""

    # Identity, goal, and provenance are constructor-injected for fixture vs official.
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        artifacts: ArtifactStore,
        goal: NativeGoal,
        *,
        component_id: str = "gymnasium-fixture-evaluator",
        version: str = "v1",
        configuration_digest: str | None = None,
        provenance: str = "gymnasium-fixture-evaluator",
    ) -> None:
        self._artifacts = artifacts
        self._goal = goal
        self._component_id = component_id
        self._version = version
        self._configuration_digest = configuration_digest
        self._provenance = provenance

    def identity(self) -> ComponentIdentity:
        """Identify this Gymnasium evaluator contract."""
        return ComponentIdentity(
            self._component_id,
            self._version,
            configuration_digest=self._configuration_digest,
        )

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Accept the predeclared goal on the requested state. Do not rescore."""
        payload = gym_state_payload(request.evaluated_state)
        accepted = self._goal.accepts(payload)
        return _native_evaluation(
            request,
            self._artifacts,
            accepted=accepted,
            objectives={
                "episode_length": payload.get("episode_length"),
                "observation": payload.get("observation"),
                "return": payload.get("return"),
                "terminated": payload.get("terminated"),
                "truncated": bool(payload.get("truncated")),
            },
            provenance=self._provenance,
        )

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """Report evaluator recovery as unsupported for this in-process fixture."""
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNSUPPORTED)


def _snapshot_key(state: StateRef) -> str:
    return canonical_json(
        {
            "digest": state.digest,
            "owner": state.owner,
            "revision": state.revision,
            "value": state.value,
        }
    )


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _string_argument(proposal: ActionProposal, name: str) -> str:
    value = proposal.arguments.get(name)
    if not isinstance(value, str):
        raise ContractError(f"action argument {name!r} must be a string")
    return value


def _action_receipt(
    proposal: ActionProposal,
    status: ExecutionStatus,
    state: StateRef,
    *,
    error: str | None = None,
    raw_artifact: ArtifactRef | None = None,
) -> ExecutionReceipt:
    return ExecutionReceipt(
        receipt_id=f"receipt-{proposal.proposal_id}",
        proposal_id=proposal.proposal_id,
        objective_version=proposal.objective_version,
        status=status,
        operation_id=proposal.proposal_id,
        state=state,
        error=error,
        usage=UsageMeasurement(
            measurement_id=f"runtime-usage-{proposal.proposal_id}", actions=1
        ),
        raw_artifact=raw_artifact,
    )


def gymnasium_tuple_payload(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    episode_length: int,
    info: Mapping[str, JsonValue],
    observation: JsonValue,
    episode_return: float,
    reward: float,
    terminated: bool,
    truncated: bool,
) -> dict[str, JsonValue]:
    """Official reset/step fields stored on a state reference and artifact."""
    return {
        "episode_length": episode_length,
        "info": dict(info),
        "observation": observation,
        "return": episode_return,
        "reward": reward,
        "terminated": terminated,
        "truncated": truncated,
    }


def gymnasium_state_ref(
    environment_id: str, payload: Mapping[str, JsonValue]
) -> StateRef:
    """Content-address a Gymnasium official tuple as a state reference."""
    encoded = canonical_json(payload)
    return StateRef(environment_id, encoded, digest=_digest(encoded))


def gymnasium_reset_step_actions(
    step_schema: Mapping[str, JsonValue],
) -> tuple[ActionDefinition, ...]:
    """Declare official reset plus one step action schema."""
    return (
        ActionDefinition(
            "reset",
            "Official Gymnasium reset",
            {"type": "object", "additionalProperties": False},
        ),
        ActionDefinition("step", "Official Gymnasium step", dict(step_schema)),
    )


def published_observation(
    *,
    session_id: str,
    state: StateRef,
    provenance: str,
    artifact: ArtifactRef | None,
    objective_version: int = 1,
) -> Observation:
    """Build a content-addressed initial observation envelope."""
    observed_at = datetime.now(timezone.utc).isoformat()
    observation_id = f"initial-observation-{session_id}"
    return Observation(
        observation_id,
        state,
        observed_at,
        provenance,
        RecordEnvelope(
            observation_id,
            session_id,
            objective_version,
            observed_at,
            provenance,
        ),
        raw_record=artifact,
    )


def gym_state_payload(state: StateRef) -> dict[str, JsonValue]:
    """Parse official reset/step fields stored on a Gymnasium state reference."""
    try:
        parsed = json.loads(state.value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _valid_fixture_state_fields(payload: Mapping[str, object]) -> bool:
    files = payload.get("files")
    revision = payload.get("revision")
    last_tested = payload.get("last_tested")
    check_passed = payload.get("last_check_passed")
    receipts = payload.get("receipts", {})
    fingerprints = payload.get("receipt_fingerprints", {})
    return (
        _valid_fixture_files(files)
        and _valid_fixture_revision(revision)
        and isinstance(last_tested, bool)
        and isinstance(check_passed, bool)
        and _valid_receipt_indexes(receipts, fingerprints)
    )


def _valid_fixture_files(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(path, str) and isinstance(content, str)
        for path, content in value.items()
    )


def _valid_fixture_revision(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_receipt_indexes(receipts: object, fingerprints: object) -> bool:
    return (
        isinstance(receipts, Mapping)
        and isinstance(fingerprints, Mapping)
        and set(receipts) == set(fingerprints)
    )


def goal_digest(goal: NativeGoal) -> str:
    """Digest the evaluator-owned goal fields for connector identity."""
    return _digest(
        canonical_json(
            {
                "min_return": goal.min_return,
                "reject_truncated": goal.reject_truncated,
                "require_terminated": goal.require_terminated,
            }
        )
    )


# Receipt construction matches other fixture evaluators by contract, not copy.
# pylint: disable=duplicate-code
def _native_evaluation(
    request: EvaluationRequest,
    artifacts: ArtifactStore,
    *,
    accepted: bool,
    objectives: Mapping[str, JsonValue],
    provenance: str,
) -> EvaluationReceipt:
    evidence = ()
    required: tuple[str, ...] = ()
    if accepted:
        published = artifacts.put(
            canonical_json(objectives).encode("utf-8"),
            media_type="application/json",
            provenance=provenance,
        )
        evidence = (published,)
        required = (published.digest,)
    return EvaluationReceipt(
        receipt_id=f"evaluation-{request.request_id}",
        request_id=request.request_id,
        objective_version=request.objective_version,
        evaluated_state=request.evaluated_state,
        baseline_node_id=request.baseline_node_id,
        evaluator_version=request.evaluator_version,
        accepted=accepted,
        comparison="improved" if accepted else "regressed",
        objectives=objectives,
        evidence=evidence,
        completed=accepted,
        completion_evidence=evidence,
        required_evidence=required,
        usage=UsageMeasurement(
            measurement_id=f"evaluator-usage-{request.request_id}", evaluations=1
        ),
    )
