"""Composition root and coordinator loop for the runnable agent application.

The coordinator is the single writer of session state. Bounded workers perform
blocking model, supervisor, runtime, and evaluator calls and return typed
results; the coordinator applies every result through the session's typed
intent/receipt path. Operator controls arrive as serialized frames on a local
socket and are admitted into the session inbox by the coordinator alone.

This module owns no execution or evaluation mechanics itself: it builds the
concrete workspace runtime, evaluator, and supervisor, and drives ``Session``.
"""

# The composition root gathers explicit dependencies and run settings by design;
# collapsing them into untyped bags would hide the real boundaries.
# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-instance-attributes

from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, TypeVar
from uuid import uuid4

from ..config import ConfigError, EndpointConfig
from ..core import (
    OPERATOR_ACTION_FAILURE_CLASSES,
    RETRYABLE_FAILURE_CLASSES,
    VERSION,
    ModelFailureClass,
)
from ..generators.openai_compat import OpenAICompatible
from .adapters.arc_agi_setup import ARC_EVALUATION_CONTRACT, build_arc_dependencies
from .adapters.child_environment import ChildEnvironment
from .adapters.process_runtime import ProcessRuntime
from .adapters.workspace import WorkspaceEnvironment
from .adapters.workspace_evaluator import CheckCommandEvaluator
from .artifacts import ArtifactStore
from .codec import canonical_json, parse_json_object
from .control import (
    ACCEPTED,
    REJECTED,
    ControlReply,
    ControlServer,
    TransportInbox,
    frame_command,
)
from .errors import AgentError, ContractError, IntegrityError, TransitionError
from .journal import SqliteJournal
from .kernel import CompatibleModel, Model, ModelCallError, decide
from .models import (
    BudgetLimits,
    ComponentIdentity,
    CompletionMode,
    EvaluationMode,
    EvaluationReceipt,
    EvaluationRequest,
    JsonValue,
    Observation,
    PendingDispatch,
    RunIdentity,
    SessionStatus,
    SessionView,
    TaskSpec,
)
from .ports import (
    Environment,
    EvaluationReconciliationResult,
    Evaluator,
    ReconciliationKind,
    Runtime,
)
from .run_settings import RunManifestRecord, RunSettings, TransportRecoverySettings
from .session import DecisionHandle, Session
from .session_state import command_from_payload
from .supervision import ModelSupervisor, SupervisionCapability, SupervisorResponse

MANIFEST_NAME = "run.json"
DEFAULT_POLL_SECONDS = 0.05

_TERMINAL = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.STOPPED}
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _RetryWake:
    """One scheduled transport-retry wake for a settled failed decision."""

    next_attempt_at: float
    failures: int
    request_id: str
    failure_class: str


# --- Run directory ---


@dataclass(frozen=True, slots=True)
class RunDirectory:
    """Owns the manifest, journal, artifacts, and runtime records of one run."""

    root: Path

    @property
    def manifest_path(self) -> Path:
        """Return the run manifest path."""
        return self.root / MANIFEST_NAME

    @property
    def journal_path(self) -> Path:
        """Return the session journal path."""
        return self.root / "session.sqlite3"

    @property
    def artifacts_path(self) -> Path:
        """Return the artifact store path."""
        return self.root / "artifacts"

    @property
    def runtime_store_path(self) -> Path:
        """Return the separate runtime execution store path."""
        return self.root / "runtime.sqlite3"

    @property
    def control_socket_path(self) -> Path:
        """Return the local control socket path for this run."""
        return self.root / "control.sock"

    @classmethod
    def create(cls, root: str | Path) -> RunDirectory:
        """Prepare a fresh run directory, refusing one that already has a run."""
        directory = cls(Path(root))
        if directory.manifest_path.exists():
            raise ContractError("run directory already contains a run manifest")
        directory.root.mkdir(parents=True, exist_ok=True)
        return directory

    @classmethod
    def open(cls, root: str | Path) -> tuple[RunDirectory, RunManifestRecord]:
        """Reopen a run directory and its persisted manifest."""
        directory = cls(Path(root))
        if not directory.manifest_path.is_file():
            raise ContractError("run directory has no run manifest")
        manifest = RunManifestRecord.from_payload(
            parse_json_object(directory.manifest_path.read_bytes())
        )
        return directory, manifest

    def write_manifest(self, record: RunManifestRecord) -> None:
        """Write the manifest atomically so a partial run never reads as valid."""
        payload = canonical_json(record.to_payload()).encode("utf-8")
        temporary = self.manifest_path.with_name(f"{MANIFEST_NAME}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, self.manifest_path)


# --- Bounded worker pool ---


class WorkerPool:
    """A bounded pool that runs blocking calls off the coordinator writer thread."""

    def __init__(self, size: int = 4) -> None:
        if size < 1:
            raise ContractError("worker pool size must be positive")
        self._executor = ThreadPoolExecutor(
            max_workers=size, thread_name_prefix="vharness-worker"
        )
        self._closed = False

    def submit(self, call: Callable[[], T]) -> Future[T]:
        """Schedule one blocking call and return its future."""
        return self._executor.submit(call)

    def close(self) -> None:
        """Wait for in-flight calls and release the worker threads."""
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True)


# --- Concrete dependency construction ---


class OperatorEvaluator:
    """Evaluator placeholder for operator mode; the session never dispatches it.

    Operator evaluation is a typed, persisted run setting. The session records a
    durable request and waits for an ``EvaluationDecision``; it never calls this
    evaluator, so calling it is a programming error rather than a rejection.
    """

    # A placeholder connector still reports a stable identity for the run.
    # pylint: disable=too-few-public-methods

    def identity(self) -> ComponentIdentity:
        """Identify the operator-evaluation connector."""
        return ComponentIdentity("operator-evaluation", "1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Fail closed: operator mode must never dispatch an evaluator."""
        raise ContractError("operator evaluation mode does not dispatch an evaluator")

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """Report that the operator connector owns no recoverable receipt."""
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNSUPPORTED)


@dataclass(frozen=True, slots=True)
class RunDependencies:
    """Concrete adapters built for one run: a workspace or an ARC-AGI-3 game."""

    environment: Environment
    runtime: Runtime
    evaluator: Evaluator
    evaluation_mode: EvaluationMode
    evaluation_contract_id: str
    #: Publishes the session's first observation; called only on creation.
    initial_observation: Callable[[str], Observation]


def build_run_dependencies(
    settings: RunSettings,
    artifacts: ArtifactStore,
    runtime_store_path: str | Path,
) -> RunDependencies:
    """Build the adapters the settings select."""
    if settings.arc is None:
        return build_workspace_dependencies(settings, artifacts, runtime_store_path)
    arc = build_arc_dependencies(
        settings.arc,
        max_actions=settings.max_actions,
        games_dir=settings.workspace,
        artifacts=artifacts,
        runtime_store_path=runtime_store_path,
    )
    return RunDependencies(
        arc.runtime,
        arc.runtime,
        arc.evaluator,
        settings.evaluation_mode,
        ARC_EVALUATION_CONTRACT,
        arc.runtime.initial_observation,
    )


def build_workspace_dependencies(
    settings: RunSettings,
    artifacts: ArtifactStore,
    runtime_store_path: str | Path,
) -> RunDependencies:
    """Build the real workspace environment, runtime, and evaluator."""
    environment = WorkspaceEnvironment(settings.workspace, artifacts)
    child_environment = ChildEnvironment(settings.api_key_env, settings.child_env_allow)
    runtime = ProcessRuntime(
        settings.workspace,
        artifacts,
        runtime_store_path,
        shell=settings.shell,
        child_environment=child_environment,
    )
    if settings.check_command is not None:
        evaluator: Evaluator = CheckCommandEvaluator(
            settings.check_command,
            Path(settings.workspace),
            artifacts,
            timeout_seconds=settings.check_timeout_seconds,
            environment=child_environment,
        )
        return RunDependencies(
            environment,
            runtime,
            evaluator,
            EvaluationMode.EXTERNAL_CHECK,
            "check-command-evaluator-v1",
            _workspace_observation(environment, settings.target),
        )
    return RunDependencies(
        environment,
        runtime,
        OperatorEvaluator(),
        EvaluationMode.OPERATOR,
        "operator-evaluation-v1",
        _workspace_observation(environment, settings.target),
    )


def _workspace_observation(
    environment: WorkspaceEnvironment, target: str | None
) -> Callable[[str], Observation]:
    def observe(session_id: str) -> Observation:
        return environment.initial_observation(session_id, target=target)

    return observe


PROVIDER_HEADERS_ENV = "VHARNESS_PROVIDER_HEADERS"


def provider_extra_headers() -> dict[str, str]:
    """Return operator-configured extra headers for provider transports.

    ``VHARNESS_PROVIDER_HEADERS`` holds a JSON object of header name to value,
    for example ``{"x-opencode-session": "..."}`` for gateways that require a
    session header. Header names and values are transport mechanics only; the
    application never interprets them. Unset or empty yields an empty dict.
    """
    raw = os.environ.get(PROVIDER_HEADERS_ENV)
    if not raw:
        return {}
    try:
        parsed = parse_json_object(raw)
    except ContractError as exc:
        raise ConfigError(
            f"{PROVIDER_HEADERS_ENV} must be a JSON object "
            "of header names to string values"
        ) from exc
    if not all(isinstance(value, str) for value in parsed.values()):
        raise ConfigError(f"{PROVIDER_HEADERS_ENV} header values must all be strings")
    return {str(name): str(value) for name, value in parsed.items()}


def build_model(settings: RunSettings) -> tuple[Model, Callable[[], None] | None]:
    """Build the OpenAI-compatible model connection from non-secret settings."""
    base_url = settings.base_url or os.environ.get("VHARNESS_BASE_URL")
    model_name = settings.model or os.environ.get("VHARNESS_MODEL")
    if not base_url:
        raise ConfigError("no endpoint: pass --base-url or set VHARNESS_BASE_URL")
    if not model_name:
        raise ConfigError("no model name: pass --model or set VHARNESS_MODEL")
    api_key = os.environ.get(settings.api_key_env)
    if not api_key:
        raise ConfigError(
            f"environment variable {settings.api_key_env!r} is not set; "
            "export the provider key and try again"
        )
    endpoint = EndpointConfig(base_url.rstrip("/"), api_key, model_name)
    generator = OpenAICompatible(
        endpoint.base_url,
        endpoint.model,
        endpoint.api_key,
        max_retries=0,
        length_retries=0,
        max_tokens=settings.max_output_tokens,
        response_format=settings.response_format,
        timeout=settings.provider_timeout,
        extra_headers=provider_extra_headers() or None,
        stream=settings.provider_stream,
    )
    model = CompatibleModel(
        generator,
        model_id=endpoint.model,
        repair_budget=settings.repair_budget,
        initial_output_tokens=settings.initial_output_tokens,
    )
    return model, generator.close


def build_supervisor(
    settings: RunSettings,
) -> tuple[SupervisionCapability, Callable[[], None]] | None:
    """Build the model-backed advisory supervisor from non-secret settings.

    Returns ``None`` when no live provider is configured or the named key
    environment variable is unavailable: a run with no live model has no live
    supervisor. The generator is separate from the decision model's, so the
    supervisor connection has its own lifecycle and no shared mutable state.
    """
    base_url = settings.base_url or os.environ.get("VHARNESS_BASE_URL")
    model_name = settings.model or os.environ.get("VHARNESS_MODEL")
    if not base_url or not model_name:
        return None
    api_key = os.environ.get(settings.api_key_env)
    if not api_key:
        return None
    generator = OpenAICompatible(
        base_url.rstrip("/"),
        model_name,
        api_key,
        max_retries=0,
        length_retries=0,
        max_tokens=settings.max_output_tokens,
        response_format=settings.response_format,
        timeout=settings.provider_timeout,
        extra_headers=provider_extra_headers() or None,
        stream=settings.provider_stream,
    )
    capability = SupervisionCapability(ModelSupervisor(generator, model_id=model_name))
    return capability, generator.close


def run_identity(
    settings: RunSettings, runtime: Runtime, evaluator: Evaluator
) -> RunIdentity:
    """Derive the immutable run identity from non-secret settings and connectors."""
    safe = {
        "objective": settings.objective,
        "workspace": settings.workspace,
        "target": settings.target,
        "check_command": (
            None if settings.check_command is None else list(settings.check_command)
        ),
        "base_url": settings.base_url,
        "model": settings.model,
        "api_key_env": settings.api_key_env,
        "shell": settings.shell,
        "limits": {
            "actions": settings.max_actions,
            "model_calls": settings.max_model_calls,
            "evaluations": settings.max_evaluations,
        },
        "token_budget": settings.token_budget,
        "max_output_tokens": settings.max_output_tokens,
        "repair_budget": settings.repair_budget,
    }
    if settings.arc is not None:
        # Added only for ARC runs, so existing run identities are unchanged.
        safe["arc"] = settings.arc.to_payload()
    if settings.initial_output_tokens is not None:
        # COD-88: growth changes what each call asks for; set-only, as above.
        safe["initial_output_tokens"] = settings.initial_output_tokens
    digest = hashlib.sha256(canonical_json(safe).encode("utf-8")).hexdigest()
    return RunIdentity(VERSION, digest, runtime.identity(), evaluator.identity())


# --- Coordinator and application ---


class Application:
    """Own one run: dependencies, session, worker pool, and control surface.

    The application never mutates session state itself. It drives the
    coordinator loop (``tick``/``run``) and applies worker results through the
    session's typed receipt paths.
    """

    def __init__(
        self,
        session: Session,
        model: Model,
        *,
        run_directory: RunDirectory,
        runtime: Runtime,
        evaluator: Evaluator,
        supervisor: SupervisionCapability | None,
        pool: WorkerPool,
        inbox: TransportInbox,
        journal: SqliteJournal,
        token_budget: int,
        max_steps: int,
        poll_seconds: float,
        supervisor_close: Callable[[], None] | None = None,
        recovery: TransportRecoverySettings | None = None,
    ) -> None:
        self._session = session
        self._model = model
        self._run_directory = run_directory
        self._runtime = runtime
        self._evaluator = evaluator
        self._supervisor = supervisor
        self._supervisor_close = supervisor_close
        self._pool = pool
        self._inbox = inbox
        self._journal = journal
        self._token_budget = token_budget
        self._max_steps = max_steps
        self._poll_seconds = poll_seconds
        self._recovery = recovery or TransportRecoverySettings()
        self._retry: _RetryWake | None = None
        self._server: ControlServer | None = None
        self._survivors: tuple[Mapping[str, object], ...] = ()
        self.last_error: str | None = None

    # -- construction --

    @classmethod
    def create(
        cls,
        settings: RunSettings,
        run_dir: str | Path,
        *,
        session_id: str,
        model: Model,
        supervisor: SupervisionCapability | None = None,
        worker_count: int = 4,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Application:
        """Create a fresh run directory and its durable session."""
        directory = RunDirectory.create(run_dir)
        effective = _resolved_settings(settings)
        application = cls._build(
            directory,
            effective,
            session_id,
            model,
            supervisor,
            worker_count,
            poll_seconds,
            create=True,
        )
        directory.write_manifest(
            RunManifestRecord(
                session_id=session_id,
                evaluation_mode=effective.evaluation_mode.value,
                created_at=datetime.now(timezone.utc).isoformat(),
                package_version=VERSION,
                settings=effective,
            )
        )
        return application

    @classmethod
    def open(
        cls,
        run_dir: str | Path,
        *,
        model: Model,
        supervisor: SupervisionCapability | None = None,
        worker_count: int = 4,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Application:
        """Reopen a run directory and reconcile any pending operation."""
        directory, manifest = RunDirectory.open(run_dir)
        inbox = TransportInbox()
        server = ControlServer(inbox, directory.control_socket_path)
        # Acquire the coordinator socket before opening the journal writer. A
        # second process must not reconcile or mutate a live run concurrently.
        server.start()
        application: Application | None = None
        try:
            application = cls._build(
                directory,
                manifest.settings,
                manifest.session_id,
                model,
                supervisor,
                worker_count,
                poll_seconds,
                create=False,
                inbox=inbox,
            )
            application._server = server
            application._session.reconcile_pending()
            return application
        except Exception:
            if application is not None:
                application.close()
            else:
                server.close()
            raise

    @classmethod
    # The composition root gathers explicit dependencies and teardown owners.
    # pylint: disable=too-many-locals
    def _build(
        cls,
        directory: RunDirectory,
        settings: RunSettings,
        session_id: str,
        model: Model,
        supervisor: SupervisionCapability | None,
        worker_count: int,
        poll_seconds: float,
        *,
        create: bool,
        inbox: TransportInbox | None = None,
    ) -> Application:
        artifacts = ArtifactStore(directory.artifacts_path)
        journal = SqliteJournal(directory.journal_path)
        dependencies: RunDependencies | None = None
        supervisor_close: Callable[[], None] | None = None
        try:
            dependencies = build_run_dependencies(
                settings, artifacts, directory.runtime_store_path
            )
            if supervisor is None:
                built = build_supervisor(settings)
                if built is not None:
                    supervisor, supervisor_close = built
            if create:
                session = _create_session(
                    settings, session_id, dependencies, journal, artifacts, supervisor
                )
            else:
                session = Session.open(
                    session_id,
                    journal=journal,
                    artifacts=artifacts,
                    environment=dependencies.environment,
                    runtime=dependencies.runtime,
                    evaluator=dependencies.evaluator,
                    run_identity=run_identity(
                        settings, dependencies.runtime, dependencies.evaluator
                    ),
                    supervision=supervisor,
                    evaluation_mode=dependencies.evaluation_mode,
                )
        except Exception:
            if supervisor_close is not None:
                supervisor_close()
            if dependencies is not None:
                _close_runtime(dependencies.runtime)
            journal.close()
            raise
        return cls(
            session,
            model,
            run_directory=directory,
            runtime=dependencies.runtime,
            evaluator=dependencies.evaluator,
            supervisor=supervisor,
            pool=WorkerPool(worker_count),
            inbox=inbox if inbox is not None else TransportInbox(),
            journal=journal,
            token_budget=settings.token_budget,
            max_steps=settings.max_steps,
            poll_seconds=poll_seconds,
            supervisor_close=supervisor_close,
            recovery=settings.transport_recovery,
        )

    # -- read-only accessors --

    @property
    def session(self) -> Session:
        """Return the durable session the coordinator drives."""
        return self._session

    @property
    def run_directory(self) -> RunDirectory:
        """Return this run's directory."""
        return self._run_directory

    @property
    def survivors(self) -> tuple[Mapping[str, object], ...]:
        """Return jobs reported unknown after a stop, if any."""
        return self._survivors

    def view(self) -> SessionView:
        """Return the current session view."""
        return self._session.view()

    # -- control surface --

    def start_controls(self) -> None:
        """Serve the local control socket for this run."""
        if self._server is None:
            self._server = ControlServer(
                self._inbox, self._run_directory.control_socket_path
            )
            self._server.start()

    def submit_frame(self, frame: Mapping[str, JsonValue]) -> ControlReply:
        """Admit one in-process control frame and return the coordinator's answer.

        Used by an in-process operator surface and by tests; the socket server
        feeds the same inbox from ``start_controls``.
        """
        request = self._inbox.post(frame)
        self._drain_controls()
        return request.wait(timeout=0)

    def _drain_controls(self) -> None:
        """Admit every pending control frame and answer it."""
        for request in self._inbox.drain():
            try:
                command = command_from_payload(dict(frame_command(request.frame)))
                self._session.enqueue(command)
            except (AgentError, ValueError, TypeError) as exc:
                request.respond(REJECTED, str(exc))
            else:
                request.respond(ACCEPTED)

    # -- coordinator loop --

    def tick(  # pylint: disable=too-many-return-statements
        self, *, allow_decision: bool = True
    ) -> str:
        """Advance one bounded scheduling unit and report what it did."""
        self._drain_controls()
        self._session.advance()
        status = self._session.view().status
        if status in _TERMINAL:
            self._retry = None
            return "terminal"
        if status is SessionStatus.PAUSED:
            self._idle()
            return "idle"
        # A deferred local dispatch is resolved before anything else: the session
        # waits for its receipt, but that wait is the coordinator's own work.
        pending = self._session.take_pending_dispatch()
        if pending is not None:
            self._resolve(pending)
            return "dispatched"
        if status is SessionStatus.WAITING:
            # Waiting for operator input or provider recovery: no calls burn.
            self._retry = None
            self._idle()
            return "idle"
        if not allow_decision:
            self._idle()
            return "idle"
        wake = self._retry
        if wake is not None:
            if time.monotonic() < wake.next_attempt_at:
                self._idle()
                return "backoff"
            self._retry = None
        try:
            handle = self._session.prepare_decision(
                self._model, token_budget=self._token_budget, dispatch=False
            )
        except TransitionError as exc:
            if wake is None:
                raise
            # Recovery recheck failed before the retry could dispatch (most
            # commonly an exhausted budget): end automatic recovery durably.
            self._session.record_provider_recovery_waiting(
                request_id=wake.request_id,
                failure_class=wake.failure_class,
                consumed_calls=wake.failures,
                operator_action=False,
                detail=_recovery_block_reason(exc),
            )
            return "idle"
        if handle is not None:
            self._decide(handle)
            return "decided"
        self._idle()
        return "idle"

    def run(self) -> SessionView:
        """Drive the session until it is terminal, then report survivors.

        A paused, operator-waiting, or provider-recovery-waiting session blocks
        here while still admitting controls, exactly as the operator surface
        requires; a terminal status, a step limit, or a non-recovered settled
        model failure ends the loop.
        """
        steps = 0
        try:
            while steps < self._max_steps:
                outcome = self.tick()
                if outcome == "terminal":
                    break
                if outcome == "decided":
                    steps += 1
            else:
                self.last_error = "step limit exhausted; run is resumable"
        except AgentError as exc:
            self.last_error = str(exc)
        finally:
            self._finalize()
        return self._session.view()

    def _decide(self, handle: DecisionHandle) -> None:
        """Run one model call on a worker and apply its typed result."""
        try:
            response = self._run_worker(lambda: decide(handle.request, self._model))
        except ModelCallError as exc:
            self._settle_decision_failure(handle, exc)
            return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._session.fail_decision(handle, exc)
            raise
        self._retry = None
        self._session.apply_decision(handle, response, dispatch=False)

    def _settle_decision_failure(
        self, handle: DecisionHandle, exc: ModelCallError
    ) -> None:
        """Settle one failed decision, then retry, wait, or propagate per policy.

        Retryable transport classes are scheduled again up to the configured
        retry bound; authentication and configuration classes enter the durable
        provider-recovery wait immediately; every other class propagates after
        settlement exactly as before this recovery policy existed.
        """
        failure_class = exc.failure_class or ModelFailureClass.UNKNOWN.value
        if (
            failure_class not in RETRYABLE_FAILURE_CLASSES
            and failure_class not in OPERATOR_ACTION_FAILURE_CLASSES
            and failure_class != ModelFailureClass.MALFORMED_OUTPUT.value
        ):
            self._session.fail_decision(handle, exc)
            return
        self._session.settle_decision_failure(handle, exc)
        window = self._session.transport_recovery_window()
        if (
            failure_class in RETRYABLE_FAILURE_CLASSES
            and window.retryable_failures <= self._recovery.max_retries
        ):
            self._retry = _RetryWake(
                next_attempt_at=time.monotonic()
                + self._recovery.retry_delays_seconds[window.retryable_failures - 1],
                failures=window.retryable_failures,
                request_id=handle.request_id,
                failure_class=failure_class,
            )
            return
        self._session.record_provider_recovery_waiting(
            request_id=handle.request_id,
            failure_class=failure_class,
            consumed_calls=window.failed_calls,
            operator_action=failure_class in OPERATOR_ACTION_FAILURE_CLASSES,
            detail=_recovery_wait_detail(failure_class, window.failed_calls),
        )

    def _resolve(self, pending: PendingDispatch) -> None:
        """Run one deferred port call on a worker and apply its typed result."""
        if pending.kind == "action":
            proposal = pending.proposal
            if proposal is None:  # pragma: no cover - PendingDispatch invariant
                raise IntegrityError("action dispatch lacks its proposal")
            receipt = self._run_worker(lambda: self._runtime.submit(proposal))
            self._session.receive_receipt(receipt)
            return
        if pending.kind == "evaluation":
            request = pending.request
            if request is None:  # pragma: no cover - PendingDispatch invariant
                raise IntegrityError("evaluation dispatch lacks its request")
            receipt = self._run_worker(lambda: self._evaluator.evaluate(request))
            self._session.receive_evaluation(request, receipt)
            return
        if pending.kind == "supervision":
            response = self._run_worker(lambda: self._advise(pending))
            self._session.receive_guidance(response)
            return
        raise IntegrityError(f"unknown pending dispatch kind {pending.kind!r}")

    def _advise(
        self, pending: PendingDispatch
    ) -> SupervisorResponse | ModelCallError | None:
        """Run one advisory call, preserving measured usage on typed failures."""
        capability = self._supervisor
        digest = pending.digest
        if capability is None or digest is None:  # pragma: no cover - invariant
            raise IntegrityError("supervision dispatch lacks its supervisor")
        try:
            return capability.supervisor.advise(digest)
        except ModelCallError as exc:
            return exc
        except Exception:  # pylint: disable=broad-exception-caught
            return None

    def _run_worker(self, call: Callable[[], T]) -> T:
        """Run one blocking call on the pool while still admitting controls."""
        future = self._pool.submit(call)
        while True:
            self._drain_controls()
            if future.done():
                return future.result()
            self._idle()

    def _idle(self) -> None:
        if self._poll_seconds > 0:
            time.sleep(self._poll_seconds)

    def _finalize(self) -> None:
        if self._session.view().status is SessionStatus.STOPPED:
            self._survivors = _stop_owned_jobs(self._runtime)

    # -- teardown --

    def close(self, *, stop_jobs: bool = False) -> None:
        """Release workers, control socket, runtime, and journal."""
        server = self._server
        if server is not None:
            server.stop_serving()
        try:
            try:
                if stop_jobs and not self._survivors:
                    self._survivors = _stop_owned_jobs(self._runtime)
            finally:
                try:
                    self._pool.close()
                finally:
                    try:
                        if self._supervisor_close is not None:
                            self._supervisor_close()
                            self._supervisor_close = None
                    finally:
                        try:
                            _close_runtime(self._runtime)
                        finally:
                            self._journal.close()
        finally:
            if server is not None:
                server.close()
                self._server = None


def _create_session(
    settings: RunSettings,
    session_id: str,
    dependencies: RunDependencies,
    journal: SqliteJournal,
    artifacts: ArtifactStore,
    supervisor: SupervisionCapability | None,
) -> Session:
    environment = dependencies.environment
    observation = dependencies.initial_observation(session_id)
    identity = run_identity(settings, dependencies.runtime, dependencies.evaluator)
    task = TaskSpec(
        task_id=f"vharness-agent-{session_id}",
        objective=settings.objective,
        completion_mode=CompletionMode.FINITE,
        success_evidence=settings.success_evidence,
        budgets=BudgetLimits(
            actions=settings.max_actions,
            model_calls=settings.max_model_calls,
            evaluations=settings.max_evaluations,
        ),
        environment_id=environment.describe().environment_id,
        evaluation_contract_id=dependencies.evaluation_contract_id,
    )
    return Session.create(
        session_id,
        task,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=dependencies.runtime,
        evaluator=dependencies.evaluator,
        run_identity=identity,
        initial_observation=observation,
        supervision=supervisor,
        evaluation_mode=dependencies.evaluation_mode,
    )


def _resolved_settings(settings: RunSettings) -> RunSettings:
    """Resolve environment-derived non-secret settings before persisting them."""
    return replace(
        settings,
        base_url=settings.base_url or os.environ.get("VHARNESS_BASE_URL"),
        model=settings.model or os.environ.get("VHARNESS_MODEL"),
        shell=settings.shell or os.environ.get("SHELL", "/bin/sh"),
    )


def _stop_owned_jobs(runtime: Runtime) -> tuple[Mapping[str, object], ...]:
    """Request shutdown of owned jobs, returning any unknown survivors."""
    stopper = getattr(runtime, "stop_owned_jobs", None)
    if stopper is None:
        return ()
    return tuple(stopper())


def _recovery_wait_detail(failure_class: str, failures: int) -> str:
    """Return the bounded, provider-text-free reason for the recovery wait."""
    if failure_class in OPERATOR_ACTION_FAILURE_CLASSES:
        return f"operator action required: {failure_class} failure"
    if failure_class == ModelFailureClass.MALFORMED_OUTPUT.value:
        # ADR-0015 rung 3: nothing external to fix; Resume re-decides.
        return (
            "malformed provider output after the repair budget; "
            "Resume re-decides, Stop ends the attempt"
        )
    return f"transport recovery exhausted after {failures} failed calls"


def _recovery_block_reason(exc: TransitionError) -> str:
    """Bound a retry-scheduling rejection to a stable safe reason."""
    if "budget" in str(exc):
        return "resource budget exhausted"
    return "retry is not currently schedulable"


def _close_runtime(runtime: Runtime | None) -> None:
    if runtime is None:
        return
    closer = getattr(runtime, "close", None)
    if closer is not None:
        closer()


def new_session_id() -> str:
    """Return a fresh run session identity."""
    return f"run-{uuid4().hex[:12]}"
