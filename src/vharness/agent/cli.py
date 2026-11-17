"""Runnable Vharness Next entry point: a thin parser over the application.

The parser maps operator commands onto ``application.py`` (the coordinator and
composition root) and ``control.py`` (the local control client). Legacy
fixture/Gymnasium runs keep their observable behavior and test compatibility;
workspace runs use the durable run directory, real runtime, and control socket.
"""

# The application boundary has several terminal outcomes and one small policy
# generator; keeping those branches explicit is clearer than hiding them.
# pylint: disable=too-many-return-statements,too-few-public-methods,too-many-locals,duplicate-code,too-many-lines

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, TextIO
from urllib.parse import urlsplit, urlunsplit

from ..config import ConfigError, EndpointConfig
from ..core import Generation, VERSION
from ..generators.openai_compat import OpenAICompatible
from .application import (
    Application,
    RunDirectory,
    RunManifestRecord,
    RunSettings,
    build_model,
    new_session_id,
    provider_extra_headers,
)
from .artifacts import ArtifactStore
from .codec import MAX_JSON_BYTES
from .run_settings import (
    ARC_OPERATION_MODES,
    DEFAULT_ARC_CALL_TIMEOUT_SECONDS,
    ArcRunSettings,
)
from .context import COEFFICIENTS, POLICY_VERSION, TOKEN_COUNTER
from .control import (
    ACCEPTED,
    ControlClient,
    ControlReply,
    command_frame,
    control_socket_path,
)
from .errors import AgentError, ContractError, TransitionError
from .kernel import (
    RESULT_SCHEMAS,
    CompatibleModel,
    Model,
    ModelCallError,
    ScriptedModel,
)
from .models import (
    BudgetLimits,
    ComponentIdentity,
    CompletionMode,
    ArtifactRef,
    Event,
    EvaluationDecision,
    EvaluationMode,
    Message,
    OPERATOR_ANSWER_WAIT,
    OPERATOR_EVALUATION_WAIT,
    PROVIDER_RECOVERY_WAIT,
    Observation,
    OperatorCommand,
    Pause,
    Resume,
    RunIdentity,
    SessionStatus,
    SessionView,
    Stop,
    TaskSpec,
)
from .operator import OperatorView, build_operator_view, runner_exit_reason
from .environments import SoftwareEvaluator, SoftwareFixture
from .journal import SqliteJournal
from .ports import Environment, Evaluator, Runtime
from .replay import replay
from .session_state import state_ref_from
from .session import Session
from .transitions import EvaluationOperationStatus, SessionState


# CLI options are one explicit boundary for the runnable application.
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class RunOptions:
    """Validated settings for one bounded alpha run."""

    objective: str
    journal: Path
    artifacts: Path
    local_smoke: bool
    provider: str
    base_url: str | None
    model: str | None
    api_key_env: str
    max_actions: int
    max_model_calls: int
    max_evaluations: int
    max_steps: int
    token_budget: int
    max_output_tokens: int
    repair_budget: int
    session_id: str
    success_evidence: str
    environment: str
    gym_id: str
    gym_seed: int
    scripted_checkpoint: bool


# A waiting session that needs the operator is a stopping point for the CLI; a
# transient receipt-pending wait is not, so the coordinator keeps ticking.
_OPERATOR_WAITS = frozenset({OPERATOR_ANSWER_WAIT, OPERATOR_EVALUATION_WAIT})
_APPLICATION_TERMINAL = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.STOPPED}
)


def main(
    argv: list[str] | None = None,
    *,
    output: TextIO | None = None,
    error: TextIO | None = None,
) -> int:
    """Parse and dispatch one operator command."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    sink = output or sys.stdout
    err = error or sys.stderr
    return arguments.handler(arguments, output=sink, error=err)


def _handle_run(arguments: argparse.Namespace, *, output: TextIO, error: TextIO) -> int:
    """Dispatch a ``run`` to the workspace application or the legacy fixture."""
    if arguments.workspace is not None or arguments.arc_game is not None:
        return _run_workspace(arguments, output=output, error=error)
    if _arc_options_given(arguments):
        _write_error(
            error, "run", "--arc-seed/--arc-mode/--arc-timeout need --arc-game"
        )
        return 2
    if arguments.run_dir is not None:
        _write_error(error, "run", "--run-dir requires --workspace")
        return 2
    if arguments.target is not None:
        _write_error(error, "run", "--target requires --workspace")
        return 2
    if arguments.check_command is not None:
        _write_error(error, "run", "--check-command requires --workspace")
        return 2
    return run(_options_from_namespace(arguments), output=output, error=error)


# Preflight collects and reports independent configuration checks.
# pylint: disable-next=too-many-branches
def _handle_preflight(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Check effective workspace settings and assemble context without a call."""
    if arguments.workspace is None:
        _write_error(error, "preflight", "--workspace is required")
        return 2
    if arguments.local_smoke or arguments.scripted_checkpoint:
        _write_error(error, "preflight", "fixture-only options are not supported")
        return 2
    if arguments.environment is not None:
        _write_error(error, "preflight", "--environment is not supported")
        return 2
    try:
        settings = _workspace_settings(arguments)
    except (AgentError, ConfigError, ValueError) as exc:
        _write_error(error, "preflight", str(exc))
        return 2

    effective_base_url = settings.base_url or os.environ.get("VHARNESS_BASE_URL")
    effective_model = settings.model or os.environ.get("VHARNESS_MODEL")
    api_key_available = bool(os.environ.get(settings.api_key_env))
    provider_error: str | None = None
    model: Model | None = None
    model_close: Callable[[], None] | None = None
    try:
        model, model_close = build_model(settings)
    except (ConfigError, ValueError, OSError) as exc:
        provider_error = str(exc)
    if model is None:
        model = CompatibleModel(
            _PreflightGenerator(settings.max_output_tokens),
            model_id=effective_model or "unconfigured-provider-model",
            repair_budget=settings.repair_budget,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="vharness-preflight-") as scratch:
            app = Application.create(
                settings,
                Path(scratch) / "run",
                session_id=f"preflight-{uuid.uuid4().hex[:12]}",
                model=model,
            )
            try:
                context_error: str | None = None
                try:
                    handle = app.session.prepare_decision(
                        model, token_budget=settings.token_budget, dispatch=False
                    )
                except TransitionError as exc:
                    handle = None
                    context_error = str(exc)
                context = None if handle is None else handle.manifest
                if context_error is not None:
                    context_report: dict[str, object] | None = {
                        "fits": False,
                        "error": _redact_provider_error(
                            context_error, settings.api_key_env
                        ),
                    }
                elif context is None:
                    context_report = None
                else:
                    context_report = {
                        "fits": True,
                        "token_budget": context.token_budget,
                        "tokens_used": context.tokens_used,
                        "serialized_input_tokens": context.serialized_input_tokens,
                        "system_prompt_tokens": context.system_prompt_tokens,
                        "reserved_output_tokens": context.reserved_output_tokens,
                        "included": context.included,
                        "omitted": context.omitted,
                    }
                report = {
                    "provider": {
                        "adapter": "openai-compatible",
                        "base_url": _redact_endpoint(effective_base_url),
                        "model": effective_model,
                        "api_key_env": settings.api_key_env,
                        "api_key_available": api_key_available,
                        "response_format": settings.response_format,
                        "timeout_seconds": settings.provider_timeout,
                        "ready_for_request": provider_error is None,
                        "connectivity_checked": False,
                        "error": (
                            None
                            if provider_error is None
                            else _redact_provider_error(
                                provider_error, settings.api_key_env
                            )
                        ),
                    },
                    "settings": {
                        "objective": settings.objective,
                        "workspace": str(Path(settings.workspace).resolve()),
                        "shell": settings.shell or os.environ.get("SHELL", "/bin/sh"),
                        "target": settings.target,
                        "check_command": settings.check_command,
                        "success_evidence": settings.success_evidence,
                        "max_actions": settings.max_actions,
                        "max_model_calls": settings.max_model_calls,
                        "max_evaluations": settings.max_evaluations,
                        "max_steps": settings.max_steps,
                        "token_budget": settings.token_budget,
                        "max_output_tokens": settings.max_output_tokens,
                        "repair_budget": settings.repair_budget,
                    },
                    "context": context_report,
                    "provider_call_made": False,
                }
                print(json.dumps(report, sort_keys=True, default=list), file=output)
            finally:
                app.close()
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(
            error, "preflight", _redact_provider_error(str(exc), settings.api_key_env)
        )
        return 1
    finally:
        if model_close is not None:
            model_close()
    return 0 if provider_error is None and context is not None else 1


class _PreflightGenerator:
    """Provider-shaped configuration used only to size a prompt offline."""

    provider_id = "openai-compatible"
    max_retries = 0
    length_retries = 0

    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    def generate(self, system: str, prompt: str) -> Generation:
        """Reject accidental provider calls during an offline preflight."""
        del system, prompt
        raise RuntimeError("preflight must not call a provider")


PROBE_SYSTEM_PROMPT = "You are a provider readiness probe. Answer exactly."
PROBE_USER_PROMPT = (
    'Reply with exactly this JSON object and nothing else: {"probe": "ok"}'
)
PROBE_MAX_OUTPUT_TOKENS = 256
_PROBE_ERROR_BYTES = 300


def _handle_probe_provider(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Make exactly one live provider call and report readiness as JSON.

    Preflight validates settings offline but never proves the endpoint accepts
    the credential. This closes that gap before a live run: one call through
    the production ``build_model`` transport path, classified outcome, no
    session state, no workspace writes, and the key value never printed. The
    probe generator bypasses coordinator retry ownership deliberately — it is
    a readiness check, not a decision — so the raw ``OpenAICompatible`` stats
    carry the usage fields into the report.
    """
    settings = RunSettings(
        objective="provider readiness probe",
        workspace=str(Path.cwd()),
        base_url=arguments.base_url,
        model=arguments.model,
        api_key_env=arguments.api_key_env,
        max_actions=0,
        max_model_calls=1,
        max_evaluations=0,
        max_steps=1,
        token_budget=4096,
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        repair_budget=0,
        response_format=None,
        provider_timeout=arguments.provider_timeout,
        provider_stream=arguments.provider_stream,
    )
    report: dict[str, object] = {
        "adapter": "openai-compatible",
        "base_url": _redact_endpoint(settings.base_url),
        "model": settings.model,
        "api_key_env": settings.api_key_env,
        "api_key_available": bool(os.environ.get(settings.api_key_env)),
        "extra_headers_present": bool(provider_extra_headers()),
        "stream": settings.provider_stream,
        "provider_call_made": False,
        "ready": False,
        "failure_class": None,
        "error": None,
    }
    try:
        endpoint = _probe_endpoint(settings)
    except (ConfigError, ValueError, OSError) as exc:
        report["error"] = _redact_provider_error(str(exc), settings.api_key_env)[
            :_PROBE_ERROR_BYTES
        ]
        print(json.dumps(report, sort_keys=True), file=output)
        return 1
    del error  # the probe reports every failure in its JSON document
    generator = OpenAICompatible(
        endpoint.base_url,
        endpoint.model,
        endpoint.api_key,
        max_retries=0,
        length_retries=0,
        max_tokens=settings.max_output_tokens,
        timeout=settings.provider_timeout,
        extra_headers=provider_extra_headers() or None,
        stream=settings.provider_stream,
    )
    try:
        generation = generator.generate(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT)
        report["provider_call_made"] = True
        if generation.ok and generation.text.strip() and not generation.prompt_tokens:
            # ADR-0020 calibration needs usage; a streamed reply without it
            # would leave every run on the word count.
            report["failure_class"] = "usage-missing"
            report["error"] = "the provider returned no usage counts"
        elif generation.ok and generation.text.strip():
            report["ready"] = True
            report["finish_reason"] = generation.finish_reason
            report["latency_seconds"] = round(generation.latency, 2)
            report["prompt_tokens"] = generation.prompt_tokens
            report["completion_tokens"] = generation.completion_tokens
            report["reasoning_tokens"] = generation.reasoning_tokens
        else:
            report["failure_class"] = generation.failure_class
            report["error"] = _redact_provider_error(
                (generation.error or "empty probe output"), settings.api_key_env
            )[:_PROBE_ERROR_BYTES]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        report["error"] = _redact_provider_error(
            f"{type(exc).__name__}", settings.api_key_env
        )[:_PROBE_ERROR_BYTES]
    finally:
        generator.close()
    print(json.dumps(report, sort_keys=True), file=output)
    return 0 if report["ready"] is True else 1


def _probe_endpoint(settings: RunSettings) -> EndpointConfig:
    """Resolve the probe endpoint exactly as ``build_model`` composes it."""
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
    return EndpointConfig(base_url.rstrip("/"), api_key, model_name)


def _handle_status(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Print the operator view of a stored session or run directory."""
    if arguments.run_dir is not None:
        return _status_run_directory(arguments, output=output, error=error)
    return status_command(arguments, output=output, error=error)


def _handle_message(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Apply one durable operator message over IPC or the stored session."""
    if arguments.run_dir is not None:
        return _message_run_directory(arguments, output=output, error=error)
    return message_command(arguments, output=output, error=error)


def _handle_pause(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Pause a running coordinator over the run's control socket."""
    return _submit_control(
        Path(arguments.run_dir),
        lambda session_id: Pause(f"cli-{uuid.uuid4().hex[:12]}", session_id),
        verb="pause",
        output=output,
        error=error,
    )


def _handle_stop(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Stop a running coordinator over the run's control socket."""
    return _submit_control(
        Path(arguments.run_dir),
        lambda session_id: Stop(f"cli-{uuid.uuid4().hex[:12]}", session_id),
        verb="stop",
        output=output,
        error=error,
    )


def _handle_resume(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Reattach to a live coordinator or reopen a run directory and drive it."""
    return resume_run_directory(arguments, output=output, error=error)


def _handle_evaluate(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Submit a typed operator decision to the owner of a workspace run."""
    run_dir = Path(arguments.run_dir)
    try:
        manifest = _read_manifest(run_dir)
        evidence_ref = _read_decision_evidence(arguments)
        command_id = _evaluation_command_id(
            arguments.request_id,
            arguments.decision,
            arguments.reason,
            arguments.completed,
            evidence_ref,
        )
        command = EvaluationDecision(
            command_id=command_id,
            session_id=manifest.session_id,
            request_id=arguments.request_id,
            accepted=arguments.decision == "accept",
            reason=arguments.reason,
            completed=arguments.completed,
            evidence_ref=evidence_ref,
        )
    except (AgentError, OSError, ValueError) as exc:
        _write_error(error, "evaluate", str(exc))
        return 2

    reply = _control_submit(run_dir, command)
    if reply is not None:
        if reply.status != ACCEPTED:
            _write_error(error, "evaluate rejected", reply.reason or "command rejected")
            return 1
        print(
            f"EVALUATION decision admitted request_id={arguments.request_id} "
            f"command_id={command_id}",
            file=output,
        )
        return 0

    # The socket is the run lock. Acquire it before opening the writer or
    # reconciling so a detached decision cannot race a new coordinator.
    app: Application | None = None
    try:
        app = Application.open(run_dir, model=ScriptedModel(()))
        app.session.enqueue(command)
        app.session.advance()
        command_events = tuple(
            event
            for event in app.session.events()
            if event.payload.get("command_id") == command_id
        )
        if any(event.kind == "command_rejected" for event in command_events):
            _write_error(
                error, "evaluate rejected", "decision was rejected by the session"
            )
            return 1
        applied = any(event.kind == "command_applied" for event in command_events)
        disposition = "applied" if applied else "admitted"
        print(
            f"EVALUATION decision {disposition} request_id={arguments.request_id} "
            f"command_id={command_id}",
            file=output,
        )
        return 0
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(error, "evaluate", str(exc))
        return 1
    finally:
        if app is not None:
            app.close()


def _read_decision_evidence(arguments: argparse.Namespace) -> ArtifactRef | None:
    """Publish bounded evidence bytes for an accepted operator decision."""
    evidence_path = arguments.evidence
    if arguments.decision == "accept" and evidence_path is None:
        raise ContractError("an accepted decision requires --evidence PATH")
    if evidence_path is None:
        return None
    path = Path(evidence_path)
    with path.open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ContractError("evaluation evidence exceeds the supported byte bound")
    return ArtifactStore(Path(arguments.run_dir) / "artifacts").put(
        data,
        media_type=arguments.media_type,
        provenance="operator evaluation decision",
    )


def _evaluation_command_id(
    request_id: str,
    decision: str,
    reason: str,
    completed: bool,
    evidence_ref: ArtifactRef | None,
) -> str:
    """Derive a stable command identity so retries do not duplicate decisions."""
    material = {
        "request_id": request_id,
        "decision": decision,
        "reason": reason,
        "completed": completed,
        "evidence": (
            None
            if evidence_ref is None
            else {
                "digest": evidence_ref.digest,
                "size": evidence_ref.size,
                "media_type": evidence_ref.media_type,
                "provenance": evidence_ref.provenance,
            }
        ),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return f"cli-evaluation-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def run(options: RunOptions, *, output: TextIO, error: TextIO) -> int:
    """Run one bounded opt-in session and stream operator progress."""
    try:
        options = _validated_run_options(options)
        model, model_close = _build_model(options)
    except ConfigError as exc:
        _write_error(error, "provider configuration", str(exc))
        return 2
    except (ValueError, OSError) as exc:
        _write_error(error, "provider setup", str(exc))
        return 2

    journal: SqliteJournal | None = None
    runtime = None
    try:
        options.journal.parent.mkdir(parents=True, exist_ok=True)
        options.artifacts.mkdir(parents=True, exist_ok=True)
        journal = SqliteJournal(options.journal)
        artifacts = ArtifactStore(options.artifacts)
        environment, runtime, evaluator, evaluation_contract_id, observation = (
            _build_environment(options, artifacts)
        )
        session = Session.create(
            options.session_id,
            TaskSpec(
                task_id=f"vharness-agent-{options.session_id}",
                objective=options.objective,
                completion_mode=CompletionMode.FINITE,
                success_evidence=options.success_evidence,
                budgets=BudgetLimits(
                    actions=options.max_actions,
                    model_calls=options.max_model_calls,
                    evaluations=options.max_evaluations,
                ),
                environment_id=environment.describe().environment_id,
                evaluation_contract_id=evaluation_contract_id,
            ),
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            run_identity=_run_identity(options, runtime, evaluator),
            initial_observation=observation,
        )
        _print_header(options, session, output, model)
        return _drive_session(
            options,
            session,
            model,
            artifacts,
            runtime,
            evaluator,
            output,
            error,
        )
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(error, "session setup", _redact(str(exc), options))
        print("TERMINAL failure; no accepted evaluator result", file=output)
        return 1
    finally:
        if journal is not None:
            journal.close()
        _close_runtime(runtime)
        if model_close is not None:
            model_close()


# --- Workspace run-directory surface ---


def _workspace_settings(arguments: argparse.Namespace) -> RunSettings:
    """Build non-secret run settings from the workspace run arguments.

    An ARC run has no workspace: the toolkit downloads its game into the run
    directory's ``arc-games`` folder, recorded as the workspace.
    """
    arc = _arc_settings(arguments)
    workspace = arguments.workspace
    if arc is not None:
        workspace = str(Path(arguments.run_dir).resolve() / "arc-games")
    return RunSettings(
        objective=arguments.objective,
        workspace=workspace,
        base_url=arguments.base_url,
        model=arguments.model,
        response_format=arguments.response_format,
        provider_timeout=arguments.provider_timeout,
        provider_stream=arguments.provider_stream,
        api_key_env=arguments.api_key_env,
        max_actions=arguments.max_actions,
        max_model_calls=arguments.max_model_calls,
        max_evaluations=arguments.max_evaluations,
        max_steps=arguments.max_steps,
        token_budget=arguments.token_budget,
        max_output_tokens=arguments.max_output_tokens,
        repair_budget=arguments.repair_budget,
        target=arguments.target,
        check_command=_check_command(arguments.check_command),
        child_env_allow=_child_env_allow(arguments),
        arc=arc,
    )


def _child_env_allow(arguments: argparse.Namespace) -> tuple[str, ...] | None:
    """Return the operator's child-environment allow-list, if one was given."""
    names = getattr(arguments, "child_env", None)
    return None if names is None else tuple(names)


def _check_command(value: str | None) -> tuple[str, ...] | None:
    """Split an operator check command into an argv tuple, failing closed."""
    if value is None:
        return None
    argv = tuple(shlex.split(value))
    if not argv:
        raise ConfigError("--check-command must not be empty")
    return argv


def _reject_ambiguous_workspace_options(arguments: argparse.Namespace) -> None:
    """Reject fixture switches and store switches combined with a workspace."""
    if arguments.run_dir is None:
        raise ConfigError("workspace runs require --run-dir")
    if arguments.journal is not None or arguments.artifacts is not None:
        raise ConfigError(
            "--workspace cannot be combined with --journal/--artifacts; "
            "use --run-dir"
        )
    if arguments.local_smoke:
        raise ConfigError("--workspace cannot be combined with --local-smoke")
    if arguments.scripted_checkpoint:
        raise ConfigError("--workspace cannot be combined with --scripted-checkpoint")
    if arguments.environment is not None:
        raise ConfigError("--workspace cannot be combined with --environment")
    if arguments.arc_game is not None and arguments.workspace is not None:
        raise ConfigError("--arc-game cannot be combined with --workspace")
    if arguments.arc_game is None and _arc_options_given(arguments):
        raise ConfigError("--arc-seed/--arc-mode/--arc-timeout need --arc-game")


def _arc_options_given(arguments: argparse.Namespace) -> bool:
    return any(
        getattr(arguments, name, None) is not None
        for name in ("arc_seed", "arc_mode", "arc_timeout")
    )


def _arc_settings(arguments: argparse.Namespace) -> ArcRunSettings | None:
    """The ARC game selected on the command line, if any."""
    game = getattr(arguments, "arc_game", None)
    if game is None:
        return None
    return ArcRunSettings(
        game_id=game,
        seed=0 if arguments.arc_seed is None else arguments.arc_seed,
        operation_mode=arguments.arc_mode or "normal",
        call_timeout_seconds=(
            DEFAULT_ARC_CALL_TIMEOUT_SECONDS
            if arguments.arc_timeout is None
            else arguments.arc_timeout
        ),
    )


def _run_workspace(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Run one real workspace task through the coordinator application."""
    try:
        _reject_ambiguous_workspace_options(arguments)
        settings = _workspace_settings(arguments)
    except (ConfigError, ContractError) as exc:
        _write_error(error, "run", str(exc))
        return 2
    try:
        model, model_close = build_model(settings)
    except ConfigError as exc:
        _write_error(error, "provider configuration", str(exc))
        return 2
    except (ValueError, OSError) as exc:
        _write_error(error, "provider setup", str(exc))
        return 2
    app: Application | None = None
    try:
        app = Application.create(
            settings,
            arguments.run_dir,
            session_id=arguments.session_id or new_session_id(),
            model=model,
        )
        app.start_controls()
        _print_workspace_header(settings, app, output)
        return _drive_application(app, settings.max_steps, output=output, error=error)
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(error, "session setup", str(exc))
        return 1
    finally:
        if app is not None:
            app.close()
        if model_close is not None:
            model_close()


def _print_workspace_header(
    settings: RunSettings, app: Application, output: TextIO
) -> None:
    print(f"OBJECTIVE {settings.objective}", file=output)
    if settings.arc is not None:
        arc = settings.arc
        print(f"SESSION {app.session.session_id} mode=arc-agi-3", file=output)
        print(
            f"GAME {arc.game_id} seed={arc.seed} mode={arc.operation_mode}",
            file=output,
        )
        print(f"RUN-DIR {app.run_directory.root}", file=output)
        print("CHECK official ARC-AGI-3 scorecard", file=output)
        return
    print(f"SESSION {app.session.session_id} mode=workspace", file=output)
    if settings.target is not None:
        print(f"TARGET {settings.target}", file=output)
    print(f"WORKSPACE {settings.workspace}", file=output)
    print(f"RUN-DIR {app.run_directory.root}", file=output)
    check = (
        " ".join(settings.check_command)
        if settings.check_command is not None
        else "operator evaluation"
    )
    print(f"CHECK {check}", file=output)


def _drive_application(
    app: Application, max_steps: int, *, output: TextIO, error: TextIO
) -> int:
    """Stream coordinator progress until terminal or bounded work exhaustion.

    An operator wait keeps the coordinator alive: the session blocks and keeps
    admitting controls, so a message, pause, or stop reaches it over IPC. A
    wait that no outstanding dispatch can resolve is reported once and left for
    the operator to stop instead of spinning silently.
    """
    cursor = 0
    steps = 0
    wait_reported = False
    recovery_reported = False
    stalled_reported = False
    try:
        while True:
            outcome = app.tick(allow_decision=steps < max_steps)
            if outcome == "decided":
                steps += 1
            cursor = _print_new_events(app.session, cursor, output)
            view = app.view()
            if view.status in _APPLICATION_TERMINAL:
                return _finish_application(app, output)
            if _is_operator_wait(view):
                if not wait_reported:
                    _report_operator_wait(app, view, output)
                    wait_reported = True
                continue
            if (
                view.status is SessionStatus.WAITING
                and view.wait_reason == PROVIDER_RECOVERY_WAIT
            ):
                # Automatic provider recovery ended: the operator can resume
                # after fixing availability, steer, or stop; the run stays live.
                if not recovery_reported:
                    _report_recovery_wait(app, view, output)
                    recovery_reported = True
                continue
            if (
                view.status is SessionStatus.WAITING
                and view.pending_proposal_id is None
            ):
                # A wait with no outstanding dispatch can never self-resolve;
                # a pending proposal's receipt or reconciliation will.
                if not stalled_reported:
                    _report_stalled_wait(app, view, output)
                    stalled_reported = True
                continue
            wait_reported = False
            recovery_reported = False
            stalled_reported = False
            if (
                steps >= max_steps
                and view.status is not SessionStatus.PAUSED
                and outcome not in {"decided", "dispatched"}
            ):
                break
    except KeyboardInterrupt:
        cursor = _print_new_events(app.session, cursor, output)
        output.flush()
        app.session.record_runner_exit("interrupted by operator")
        print(
            "TERMINAL interrupted; journal is safe to inspect or retry",
            file=output,
            flush=True,
        )
        return 130
    except (AgentError, ModelCallError, RuntimeError) as exc:
        cursor = _print_new_events(app.session, cursor, output)
        output.flush()
        app.session.record_runner_exit(str(exc))
        _write_error(error, "run failed", str(exc))
        print(
            "TERMINAL failure; inspect the journal and adjust the bounded run",
            file=output,
            flush=True,
        )
        return 1
    output.flush()
    app.session.record_runner_exit("step limit exhausted")
    _write_error(error, "run", "step limit exhausted; the run is resumable")
    print(
        "TERMINAL failure; step limit exhausted (resume to continue)",
        file=output,
        flush=True,
    )
    return 1


def _finish_application(app: Application, output: TextIO) -> int:
    view = app.view()
    _print_terminal(view.status, view.wait_reason, output)
    if view.status is SessionStatus.STOPPED and app.survivors:
        print(f"SURVIVORS {_compact(app.survivors)}", file=output)
    return _exit_for_application_status(view.status)


def _is_operator_wait(view: SessionView) -> bool:
    """Return True when a waiting session needs an operator answer or decision."""
    return view.status is SessionStatus.WAITING and view.wait_reason in _OPERATOR_WAITS


def _report_operator_wait(app: Application, view: SessionView, output: TextIO) -> None:
    """Print the one actionable instruction for an operator wait."""
    if view.wait_reason == OPERATOR_EVALUATION_WAIT:
        request_id = _pending_evaluation_request_id(replay(app.session.events()))
        print(
            "WAIT operator-evaluation "
            f"request_id={request_id or 'unknown'} "
            f"use: vharness-agent evaluate --run-dir "
            f"{app.run_directory.root} --request-id {request_id or '<id>'} "
            "--decision accept|reject --reason ...",
            file=output,
        )
        return
    _print_operator_surface(app.session.operator_view(), output)
    print(
        "WAIT operator-answer "
        f"use: vharness-agent message --run-dir {app.run_directory.root} "
        "--text ...",
        file=output,
    )


def _report_stalled_wait(app: Application, view: SessionView, output: TextIO) -> None:
    """Report a wait that no outstanding dispatch can resolve, without exit."""
    print(
        f"WAIT {view.wait_reason}: no outstanding dispatch can resolve it; "
        f"use: vharness-agent stop --run-dir {app.run_directory.root} to end "
        "the session (unknown effects are not reversed)",
        file=output,
    )


def _latest_recovery_failure_class(events: tuple[Event, ...]) -> str | None:
    """Return the failure class of the last provider-recovery wait, if any."""
    for event in reversed(events):
        if event.kind == "provider_recovery_waiting":
            value = event.payload.get("failure_class")
            return value if isinstance(value, str) else None
    return None


def _report_recovery_wait(app: Application, view: SessionView, output: TextIO) -> None:
    """Report a durable provider-recovery wait the operator can end or resume."""
    failure_class = _latest_recovery_failure_class(app.session.events())
    if failure_class == "malformed-output":
        # ADR-0015 rung 3: there is nothing external to fix.
        print(
            f"WAIT {view.wait_reason}: malformed provider output after the repair "
            "budget; the run burns no calls while waiting. Resume re-decides: "
            f"vharness-agent resume --run-dir {app.run_directory.root} "
            "(a steer or stop also ends the wait)",
            file=output,
        )
        return
    print(
        f"WAIT {view.wait_reason}: automatic provider recovery ended; the run "
        "burns no calls while waiting. Fix availability, then use: "
        f"vharness-agent resume --run-dir {app.run_directory.root} "
        "(a steer or stop also ends the wait)",
        file=output,
    )


def _status_run_directory(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Print the operator view of a run directory without writing session state."""
    journal: SqliteJournal | None = None
    try:
        directory, manifest = RunDirectory.open(arguments.run_dir)
        journal = SqliteJournal(directory.journal_path, read_only=True)
        limit = max(journal.cursor(manifest.session_id), 1)
        events = journal.events(manifest.session_id, limit=limit)
        state = replay(events)
        operator_mode = manifest.settings.evaluation_mode is EvaluationMode.OPERATOR
        _print_operator_surface(
            build_operator_view(manifest.session_id, state, events),
            output,
            pending_evaluation_id=_pending_evaluation_request_id(
                state, operator_mode=operator_mode
            ),
            runner_exit=runner_exit_reason(events),
        )
        return 0
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(error, "status", str(exc))
        return 1
    finally:
        if journal is not None:
            journal.close()


def _message_run_directory(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Submit one durable message to a running coordinator over IPC."""
    run_dir = Path(arguments.run_dir)
    try:
        manifest = _read_manifest(run_dir)
    except (AgentError, OSError, ValueError) as exc:
        _write_error(error, "message", str(exc))
        return 1
    command = Message(
        f"cli-{uuid.uuid4().hex[:12]}",
        manifest.session_id,
        arguments.text,
        arguments.question_id,
    )
    reply = _control_submit(run_dir, command)
    if reply is None:
        _write_error(
            error,
            "message",
            "no running coordinator owns this run directory; use resume to reopen it",
        )
        return 1
    if reply.status != ACCEPTED:
        _write_error(error, "message rejected", reply.reason or "command was rejected")
        return 1
    print("MESSAGE accepted", file=output)
    return 0


def _submit_control(
    run_dir: Path,
    factory: Callable[[str], OperatorCommand],
    *,
    verb: str,
    output: TextIO,
    error: TextIO,
) -> int:
    """Submit one simple control command to a running coordinator over IPC."""
    try:
        manifest = _read_manifest(run_dir)
    except (AgentError, OSError, ValueError) as exc:
        _write_error(error, verb, str(exc))
        return 1
    reply = _control_submit(run_dir, factory(manifest.session_id))
    if reply is None:
        _write_error(error, verb, "no running coordinator owns this run directory")
        return 1
    if reply.status != ACCEPTED:
        _write_error(error, verb, reply.reason or "command was rejected")
        return 1
    print(f"{verb.upper()} accepted", file=output)
    return 0


def resume_run_directory(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Reattach to a live coordinator or reopen a run directory and drive it."""
    run_dir = Path(arguments.run_dir)
    try:
        manifest = _read_manifest(run_dir)
    except (AgentError, OSError, ValueError) as exc:
        _write_error(error, "resume", str(exc))
        return 1
    reply = _control_submit(
        run_dir, Resume(f"cli-{uuid.uuid4().hex[:12]}", manifest.session_id)
    )
    if reply is not None:
        if reply.status != ACCEPTED:
            _write_error(error, "resume", reply.reason or "command was rejected")
            return 1
        print("RESUME accepted by the running coordinator", file=output)
        return 0
    try:
        settings = _apply_provider_overrides(manifest.settings, arguments)
        model, model_close = build_model(settings)
    except ConfigError as exc:
        _write_error(error, "provider configuration", str(exc))
        return 2
    except (ValueError, OSError) as exc:
        _write_error(error, "provider setup", str(exc))
        return 2
    app: Application | None = None
    try:
        app = Application.open(run_dir, model=model)
        view = app.view()
        if view.status in _APPLICATION_TERMINAL:
            _print_terminal(view.status, view.wait_reason, output)
            return _exit_for_application_status(view.status)
        app.start_controls()
        print(
            f"RESUME session={app.session.session_id} "
            f"status={view.status.value} run-dir={app.run_directory.root}",
            file=output,
        )
        return _drive_application(
            app, manifest.settings.max_steps, output=output, error=error
        )
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(error, "resume", str(exc))
        return 1
    finally:
        if app is not None:
            app.close()
        if model_close is not None:
            model_close()


def _apply_provider_overrides(
    settings: RunSettings, arguments: argparse.Namespace
) -> RunSettings:
    """Apply operator-supplied provider parameters to the persisted settings."""
    overrides: dict[str, object] = {}
    for name in ("base_url", "model", "api_key_env"):
        value = getattr(arguments, name, None)
        if value is not None:
            overrides[name] = value
    return replace(settings, **overrides) if overrides else settings


def _read_manifest(run_dir: Path) -> RunManifestRecord:
    """Read the non-secret manifest of one run directory."""
    _directory, manifest = RunDirectory.open(run_dir)
    return manifest


def _control_submit(run_dir: Path, command: OperatorCommand) -> ControlReply | None:
    """Submit one control command, returning ``None`` when no coordinator serves."""
    client = ControlClient(control_socket_path(run_dir))
    try:
        return client.submit(command_frame(command))
    except ContractError:
        return None


def _handle_operator_answer_wait(session: Session, view, output: TextIO) -> int | None:
    """Return an exit code when waiting for an operator answer; else None."""
    if view.status is not SessionStatus.WAITING:
        return None
    if view.wait_reason != OPERATOR_ANSWER_WAIT:
        return None
    _print_operator_surface(session.operator_view(), output)
    if sys.stdin.isatty():
        print("TYPE answer then Enter", file=output)
        answer = sys.stdin.readline()
        if answer.strip():
            session.enqueue(
                Message(
                    f"msg-{uuid.uuid4().hex[:12]}",
                    session.session_id,
                    answer.strip(),
                )
            )
            session.advance()
            return None
    print(
        "WAITING operator-answer; "
        f"use: vharness-agent message --session-id {session.session_id} --text ...",
        file=output,
    )
    return 0


def _print_operator_surface(
    view: OperatorView,
    output: TextIO,
    *,
    pending_evaluation_id: str | None = None,
    runner_exit: str | None = None,
) -> None:
    print(f"STATUS {view.status.value}", file=output)
    if runner_exit is not None:
        print(f"RUNNER-EXIT {runner_exit}", file=output)
    print(f"OBJECTIVE {view.objective}", file=output)
    print(f"PLAN {view.current_plan}", file=output)
    print(f"BUDGET remaining {view.remaining_budgets}", file=output)
    if view.wait_reason:
        print(f"WAIT {view.wait_reason}", file=output)
    if pending_evaluation_id is not None:
        print(f"EVALUATION request_id={pending_evaluation_id}", file=output)
    for question in view.questions:
        print(f"QUESTION {question}", file=output)
    for item in view.action_receipt_stream:
        print(f"STREAM {item}", file=output)


def status_command(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Print the operator view of a stored session."""
    if arguments.session_id is None:
        _write_error(error, "status", "status requires --run-dir or --session-id")
        return 2
    journal = None
    runtime = None
    try:
        session, journal, runtime = _open_existing_session(
            _store_journal(arguments),
            _store_artifacts(arguments),
            arguments.session_id,
        )
        _print_operator_surface(session.operator_view(), output)
        return 0
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(error, "status", str(exc))
        return 1
    finally:
        if journal is not None:
            journal.close()
        _close_runtime(runtime)


def message_command(
    arguments: argparse.Namespace, *, output: TextIO, error: TextIO
) -> int:
    """Apply one durable operator message to a stored session."""
    if arguments.session_id is None:
        _write_error(error, "message", "message requires --run-dir or --session-id")
        return 2
    journal = None
    runtime = None
    try:
        session, journal, runtime = _open_existing_session(
            _store_journal(arguments),
            _store_artifacts(arguments),
            arguments.session_id,
        )
        command_id = f"msg-{uuid.uuid4().hex[:12]}"
        cursor = session.view().cursor
        session.enqueue(
            Message(
                command_id,
                arguments.session_id,
                arguments.text,
                arguments.question_id,
            )
        )
        session.advance()
        _print_operator_surface(session.operator_view(), output)
        rejected = next(
            (
                event
                for event in session.events(after_sequence=cursor, limit=100)
                if event.kind == "command_rejected"
                and event.payload.get("command_id") == command_id
            ),
            None,
        )
        if rejected is not None:
            _write_error(
                error,
                "message rejected",
                str(rejected.payload.get("reason", "command was rejected")),
            )
            return 1
        return 0
    except (AgentError, OSError, ValueError, ConfigError) as exc:
        _write_error(error, "message", str(exc))
        return 1
    finally:
        if journal is not None:
            journal.close()
        _close_runtime(runtime)


def _open_existing_session(journal_path: Path, artifacts_path: Path, session_id: str):
    """Reopen a stored session with the mechanical adapter recorded at creation."""
    journal = SqliteJournal(journal_path)
    artifacts = ArtifactStore(artifacts_path)
    created = journal.events(session_id, limit=1)
    if not created or created[0].kind != "session_created":
        journal.close()
        raise ConfigError(f"session {session_id!r} has no creation event")
    environment_payload = created[0].payload.get("environment")
    environment_id = (
        environment_payload.get("environment_id")
        if isinstance(environment_payload, Mapping)
        else None
    )
    try:
        if environment_id == "software-fixture":
            if not isinstance(environment_payload, Mapping):
                raise ConfigError("session creation lacks environment contract")
            initial_state_payload = environment_payload.get("initial_state")
            if not isinstance(initial_state_payload, Mapping):
                raise ConfigError("session creation lacks environment initial state")
            environment = SoftwareFixture(
                artifacts, initial_state=state_ref_from(initial_state_payload)
            )
            runtime = environment
            evaluator = SoftwareEvaluator(environment, artifacts)
        else:
            environment, runtime, evaluator, _contract, _observation = (
                _build_environment(
                    _open_environment_options(environment_id, session_id), artifacts
                )
            )
        run_identity = _run_identity_from_payload(
            created[0].payload.get("run_identity")
        )
        session = Session.open(
            session_id,
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            run_identity=run_identity,
        )
    except (AgentError, ConfigError):
        journal.close()
        raise
    return session, journal, runtime


def _run_identity_from_payload(value: object) -> RunIdentity:
    """Decode the connector identity persisted by a CLI-created session."""
    if not isinstance(value, Mapping):
        raise ConfigError("session creation lacks run identity")
    runtime = _component_identity_from_payload(value.get("runtime"), "runtime")
    evaluator = _component_identity_from_payload(value.get("evaluator"), "evaluator")
    package_version = value.get("package_version")
    configuration_digest = value.get("configuration_digest")
    if not isinstance(package_version, str) or not package_version.strip():
        raise ConfigError("run identity package_version is invalid")
    if not isinstance(configuration_digest, str):
        raise ConfigError("run identity configuration_digest is invalid")
    try:
        return RunIdentity(
            package_version,
            configuration_digest,
            runtime,
            evaluator,
        )
    except (ContractError, TypeError) as exc:
        raise ConfigError("run identity is invalid") from exc


def _component_identity_from_payload(value: object, name: str) -> ComponentIdentity:
    """Decode one persisted connector identity without weakening validation."""
    if not isinstance(value, Mapping):
        raise ConfigError(f"run identity {name} is invalid")
    component_id = value.get("component_id")
    version = value.get("version")
    configuration_digest = value.get("configuration_digest")
    if not isinstance(component_id, str) or not component_id.strip():
        raise ConfigError(f"run identity {name} component_id is invalid")
    if not isinstance(version, str) or not version.strip():
        raise ConfigError(f"run identity {name} version is invalid")
    if configuration_digest is not None and not isinstance(configuration_digest, str):
        raise ConfigError(f"run identity {name} configuration_digest is invalid")
    try:
        return ComponentIdentity(component_id, version, configuration_digest)
    except (ContractError, TypeError) as exc:
        raise ConfigError(f"run identity {name} is invalid") from exc


def _open_environment_options(environment_id: object, session_id: str) -> RunOptions:
    environment = "gymnasium" if environment_id == "FrozenLake-v1" else "software"
    scripted = environment == "gymnasium"
    return RunOptions(
        objective="reopen",
        journal=Path("."),
        artifacts=Path("."),
        local_smoke=not scripted,
        provider="openai-compatible",
        base_url=None,
        model=None,
        api_key_env="OPENAI_API_KEY",
        max_actions=8,
        max_model_calls=10,
        max_evaluations=2,
        max_steps=16,
        token_budget=16384,
        max_output_tokens=512,
        repair_budget=1,
        session_id=session_id,
        success_evidence="external evaluator acceptance",
        environment=environment,
        gym_id="FrozenLake-v1",
        gym_seed=0,
        scripted_checkpoint=scripted,
    )


def _close_runtime(runtime: object | None) -> None:
    """Close an official env if the runtime exposes close()."""
    if runtime is None:
        return
    closer = getattr(runtime, "close", None)
    if closer is None:
        return
    closer()


def _drive_session(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    options: RunOptions,
    session: Session,
    model: CompatibleModel | ScriptedModel,
    artifacts: ArtifactStore,
    runtime: object,
    evaluator: object,
    output: TextIO,
    error: TextIO,
) -> int:
    """Advance until a terminal status, budget, or interrupt."""
    cursor = 0
    for _ in range(options.max_steps):
        view = session.view()
        handled = _handle_operator_answer_wait(session, view, output)
        if handled is not None:
            return handled
        if view.status in _terminal_statuses():
            return _finish_terminal(options, session, runtime, evaluator, output)
        _print_observation(session, artifacts, output)
        try:
            session.step(model, token_budget=options.token_budget)
        except KeyboardInterrupt:
            cursor = _print_new_events(session, cursor, output)
            _print_budget(session, output)
            print(
                "TERMINAL interrupted; journal is safe to inspect or retry",
                file=output,
            )
            return 130
        except (AgentError, ModelCallError, RuntimeError) as exc:
            cursor = _print_new_events(session, cursor, output)
            _print_budget(session, output)
            _write_error(error, "run failed", _redact(str(exc), options))
            print(
                "TERMINAL failure; inspect the journal and adjust the bounded run",
                file=output,
            )
            return 1
        cursor = _print_new_events(session, cursor, output)
        _print_budget(session, output)
        view = session.view()
        handled = _handle_operator_answer_wait(session, view, output)
        if handled is not None:
            return handled
        if view.status in _terminal_statuses():
            return _finish_terminal(options, session, runtime, evaluator, output)
    print(
        "TERMINAL failure; step limit exhausted "
        "(increase --max-steps or inspect the journal)",
        file=output,
    )
    return 1


def _finish_terminal(
    options: RunOptions,
    session: Session,
    runtime: object,
    evaluator: object,
    output: TextIO,
) -> int:
    view = session.view()
    _print_terminal(view.status, view.wait_reason, output)
    if options.scripted_checkpoint and view.status is SessionStatus.COMPLETED:
        _write_checkpoint_manifest(options, session, runtime, evaluator)
    return _exit_for_status(view.status)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vharness-agent",
        description="Vharness Next operator entry point.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser(
        "run", help="run one bounded task (workspace or legacy fixture)"
    )
    _add_run_arguments(run_parser)
    run_parser.set_defaults(handler=_handle_run)

    preflight_parser = commands.add_parser(
        "preflight", help="show effective workspace settings and context fit offline"
    )
    _add_run_arguments(preflight_parser)
    preflight_parser.set_defaults(handler=_handle_preflight)

    probe_parser = commands.add_parser(
        "probe-provider",
        help="make exactly one live provider call to confirm readiness before a run",
    )
    probe_parser.add_argument(
        "--base-url", default=None, required=True, help="provider base URL"
    )
    probe_parser.add_argument(
        "--model", default=None, required=True, help="provider model identity"
    )
    probe_parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        metavar="NAME",
        help="environment variable containing the provider key (never printed)",
    )
    probe_parser.add_argument(
        "--provider-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="per-request HTTP timeout for the probe call",
    )
    probe_parser.add_argument(
        "--provider-stream",
        action="store_true",
        help="probe with a streamed reply, as runs with --provider-stream make",
    )
    probe_parser.set_defaults(handler=_handle_probe_provider)

    resume_parser = commands.add_parser(
        "resume", help="reattach to or reopen a run directory"
    )
    _add_provider_arguments(resume_parser)
    _add_run_dir_argument(resume_parser, required=True)
    resume_parser.set_defaults(handler=_handle_resume)

    evaluate_parser = commands.add_parser(
        "evaluate", help="accept or reject a pending operator evaluation"
    )
    _add_run_dir_argument(evaluate_parser, required=True)
    evaluate_parser.add_argument("--request-id", required=True)
    evaluate_parser.add_argument(
        "--decision", choices=("accept", "reject"), required=True
    )
    evaluate_parser.add_argument("--reason", required=True)
    evaluate_parser.add_argument("--evidence", type=Path, default=None)
    evaluate_parser.add_argument("--media-type", default="text/plain")
    evaluate_parser.add_argument("--completed", action="store_true")
    evaluate_parser.set_defaults(handler=_handle_evaluate)

    status_parser = commands.add_parser(
        "status", help="print the live operator view of a stored session"
    )
    _add_run_dir_argument(status_parser, required=False)
    _add_store_arguments(status_parser)
    status_parser.set_defaults(handler=_handle_status)

    message_parser = commands.add_parser(
        "message", help="answer or steer a stored or running session"
    )
    _add_run_dir_argument(message_parser, required=False)
    _add_store_arguments(message_parser)
    message_parser.add_argument("--text", required=True, help="operator message text")
    message_parser.add_argument(
        "--question-id",
        default=None,
        help="open question this message answers",
    )
    message_parser.set_defaults(handler=_handle_message)

    pause_parser = commands.add_parser("pause", help="pause a running session")
    _add_run_dir_argument(pause_parser, required=True)
    pause_parser.set_defaults(handler=_handle_pause)

    stop_parser = commands.add_parser("stop", help="stop a running session")
    _add_run_dir_argument(stop_parser, required=True)
    stop_parser.set_defaults(handler=_handle_stop)
    return parser


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the run command's task, workspace, provider, and fixture switches."""
    parser.add_argument("--objective", required=True, help="task objective")
    parser.add_argument(
        "--success-evidence",
        default="external evaluator acceptance",
        help="human-readable evidence expected for completion",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="real workspace directory; selects the workspace runtime",
    )
    parser.add_argument(
        "--target", default=None, help="optional task target visible to the model"
    )
    parser.add_argument(
        "--check-command",
        default=None,
        metavar="COMMAND",
        help="external check command (shell-split); omit for operator evaluation",
    )
    parser.add_argument(
        "--child-env",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "pass only these environment variables to commands and checks "
            "(repeatable); by default they get everything except the provider "
            "key variable and VHARNESS_*/OPENAI_* variables"
        ),
    )
    parser.add_argument(
        "--arc-game",
        default=None,
        metavar="GAME_ID",
        help="play this ARC-AGI-3 game instead of a workspace (needs --run-dir)",
    )
    parser.add_argument("--arc-seed", type=_nonnegative, default=None)
    parser.add_argument(
        "--arc-mode",
        choices=ARC_OPERATION_MODES,
        default=None,
        help="ARC toolkit mode: normal (local game, API scorecard), online, offline",
    )
    parser.add_argument(
        "--arc-timeout",
        type=_positive_seconds,
        default=None,
        metavar="SECONDS",
        help="bound on each ARC toolkit call (default 30)",
    )
    _add_run_dir_argument(parser, required=False)
    parser.add_argument(
        "--local-smoke",
        action="store_true",
        help="use the named deterministic local policy; no provider call",
    )
    parser.add_argument(
        "--provider",
        choices=("openai-compatible",),
        default="openai-compatible",
        help="compatible provider adapter (default: openai-compatible)",
    )
    parser.add_argument("--base-url", default=None, help="provider base URL")
    parser.add_argument("--model", default=None, help="provider model identity")
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        metavar="NAME",
        help="environment variable containing the provider key (never persisted)",
    )
    parser.add_argument(
        "--response-format",
        choices=("json_object",),
        default=None,
        help="provider-enforced output format for models that need it "
        "(e.g. tool-tuned models that ignore JSON-in-text instructions)",
    )
    parser.add_argument(
        "--provider-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="per-request HTTP timeout for the provider endpoint; "
        "reasoning models with large output budgets need more than the default. "
        "The client honours it, but a gateway may end a silent call sooner "
        "(LongCat's ended calls at 300 s); use --provider-stream past that",
    )
    parser.add_argument(
        "--provider-stream",
        action="store_true",
        help="stream provider replies, so long reasoning never leaves the "
        "connection idle (some gateways cut idle calls at 300 s); the provider "
        "must support streamed usage counts; check with probe-provider",
    )
    parser.add_argument("--max-actions", type=_nonnegative, default=6)
    parser.add_argument("--max-model-calls", type=_nonnegative, default=8)
    parser.add_argument("--max-evaluations", type=_nonnegative, default=2)
    parser.add_argument("--max-steps", type=_positive, default=12)
    parser.add_argument(
        "--token-budget",
        type=_positive,
        default=16384,
        help="model context budget per decision",
    )
    parser.add_argument("--max-output-tokens", type=_positive, default=512)
    parser.add_argument("--repair-budget", type=_nonnegative, default=1)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--artifacts", type=Path, default=None)
    parser.add_argument(
        "--session-id", default=None, help="stable ID for this new session"
    )
    parser.add_argument(
        "--environment",
        choices=("software", "gymnasium"),
        default=None,
        help="legacy mechanical adapter and task input (default: software)",
    )
    parser.add_argument(
        "--gym-id",
        default="FrozenLake-v1",
        help="official Gymnasium env id (FrozenLake-v1 only in this slice)",
    )
    parser.add_argument("--gym-seed", type=_nonnegative, default=0)
    parser.add_argument(
        "--scripted-checkpoint",
        action="store_true",
        help="run the diagnostic FrozenLake checkpoint; no provider call",
    )


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    """Add re-suppliable provider parameters for a reopened run."""
    parser.add_argument("--base-url", default=None, help="provider base URL")
    parser.add_argument("--model", default=None, help="provider model identity")
    parser.add_argument(
        "--api-key-env",
        default=None,
        metavar="NAME",
        help="environment variable containing the provider key",
    )


def _add_run_dir_argument(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=required,
        default=None,
        help="durable run directory (manifest, journal, artifacts, runtime records)",
    )


def _add_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--artifacts", type=Path, default=None)
    parser.add_argument(
        "--session-id",
        default=None,
        help="session identity to reopen (legacy journal store)",
    )


def _store_journal(arguments: argparse.Namespace) -> Path:
    """Resolve the legacy journal path, applying its default when omitted."""
    return arguments.journal or Path(".vharness/session.sqlite3")


def _store_artifacts(arguments: argparse.Namespace) -> Path:
    """Resolve the legacy artifact path, applying its default when omitted."""
    return arguments.artifacts or Path(".vharness/artifacts")


def _options_from_namespace(namespace: argparse.Namespace) -> RunOptions:
    """Convert parser values into the application boundary record."""
    return RunOptions(
        objective=namespace.objective,
        journal=_store_journal(namespace),
        artifacts=_store_artifacts(namespace),
        local_smoke=namespace.local_smoke,
        provider=namespace.provider,
        base_url=namespace.base_url,
        model=namespace.model,
        api_key_env=namespace.api_key_env,
        max_actions=namespace.max_actions,
        max_model_calls=namespace.max_model_calls,
        max_evaluations=namespace.max_evaluations,
        max_steps=namespace.max_steps,
        token_budget=namespace.token_budget,
        max_output_tokens=namespace.max_output_tokens,
        repair_budget=namespace.repair_budget,
        session_id=namespace.session_id or f"session-{uuid.uuid4().hex[:12]}",
        success_evidence=namespace.success_evidence,
        environment=namespace.environment or "software",
        gym_id=namespace.gym_id,
        gym_seed=namespace.gym_seed,
        scripted_checkpoint=namespace.scripted_checkpoint,
    )


def _validated_run_options(options: RunOptions) -> RunOptions:
    """Reject mixed profiles and insufficient checkpoint budgets."""
    if options.local_smoke and options.environment != "software":
        raise ConfigError("local smoke is software-only")
    if options.environment == "gymnasium" and not options.scripted_checkpoint:
        raise ConfigError("Gymnasium runs in this slice require --scripted-checkpoint")
    if options.scripted_checkpoint and options.environment != "gymnasium":
        raise ConfigError("--scripted-checkpoint requires --environment gymnasium")
    if options.scripted_checkpoint and options.gym_seed != 0:
        raise ConfigError("scripted Gymnasium checkpoint supports --gym-seed 0 only")
    if options.scripted_checkpoint and options.gym_id != "FrozenLake-v1":
        raise ConfigError(
            "scripted Gymnasium checkpoint supports --gym-id FrozenLake-v1 only"
        )
    if not options.scripted_checkpoint:
        return options
    required = {
        "max_actions": (options.max_actions, 8),
        "max_model_calls": (options.max_model_calls, 10),
        "max_evaluations": (options.max_evaluations, 2),
        "max_steps": (options.max_steps, 16),
    }
    insufficient = [
        f"--{name.replace('_', '-')}={actual} (requires >= {minimum})"
        for name, (actual, minimum) in required.items()
        if actual < minimum
    ]
    if insufficient:
        raise ConfigError(
            "scripted Gymnasium checkpoint has insufficient budgets: "
            + ", ".join(insufficient)
        )
    return options


def _build_environment(
    options: RunOptions, artifacts: ArtifactStore
) -> tuple[Environment, Runtime, Evaluator, str, Observation]:
    """Construct the mechanical adapter selected by task input."""
    if options.environment == "software":
        fixture = SoftwareFixture(artifacts)
        return (
            fixture,
            fixture,
            SoftwareEvaluator(fixture, artifacts),
            "software-fixture-evaluator-v1",
            fixture.initial_observation(options.session_id),
        )
    from .adapters.gymnasium import (  # pylint: disable=import-outside-toplevel
        FROZENLAKE_GOAL,
        OFFICIAL_EVALUATOR_CONTRACT,
        OfficialGymnasiumEvaluator,
        OfficialGymnasiumRuntime,
    )

    try:
        runtime = OfficialGymnasiumRuntime(
            artifacts, env_id=options.gym_id, seed=options.gym_seed
        )
    except ContractError as exc:
        raise ConfigError(str(exc)) from exc
    evaluator = OfficialGymnasiumEvaluator(artifacts, FROZENLAKE_GOAL)
    return (
        runtime,
        runtime,
        evaluator,
        OFFICIAL_EVALUATOR_CONTRACT,
        runtime.initial_observation(options.session_id),
    )


def _build_model(
    options: RunOptions,
) -> tuple[CompatibleModel | ScriptedModel, Callable[[], None] | None]:
    if options.scripted_checkpoint:
        from .checkpoint_policy import (  # pylint: disable=import-outside-toplevel
            frozenlake_diagnostic_checkpoint_results,
        )

        return (
            ScriptedModel(
                frozenlake_diagnostic_checkpoint_results(),
                model_id="gymnasium-frozenlake-scripted-v1",
            ),
            None,
        )
    if options.local_smoke:
        return (
            CompatibleModel(
                _LocalSmokeGenerator(),
                model_id="local-smoke-policy-v1",
                repair_budget=options.repair_budget,
            ),
            None,
        )
    if options.provider != "openai-compatible":
        raise ConfigError(f"unsupported provider {options.provider!r}")
    base_url = options.base_url or os.environ.get("VHARNESS_BASE_URL")
    model_name = options.model or os.environ.get("VHARNESS_MODEL")
    if not base_url:
        raise ConfigError("no endpoint: pass --base-url or set VHARNESS_BASE_URL")
    if not model_name:
        raise ConfigError("no model name: pass --model or set VHARNESS_MODEL")
    if not _env_name(options.api_key_env):
        raise ConfigError("--api-key-env must be a valid environment variable name")
    api_key = os.environ.get(options.api_key_env, "no-key")
    endpoint = EndpointConfig(base_url.rstrip("/"), api_key, model_name)
    generator = OpenAICompatible(
        endpoint.base_url,
        endpoint.model,
        endpoint.api_key,
        max_retries=0,
        length_retries=0,
        max_tokens=options.max_output_tokens,
        extra_headers=provider_extra_headers() or None,
    )
    return (
        CompatibleModel(
            generator,
            model_id=endpoint.model,
            repair_budget=options.repair_budget,
        ),
        generator.close,
    )


class _LocalSmokeGenerator:
    """Deterministic visible policy for zero-secret wiring verification."""

    provider_id = "local-smoke-policy"
    max_retries = 0
    length_retries = 0

    def generate(self, system: str, prompt: str) -> Generation:
        """Choose from published observation and action data only."""
        del system
        request = json.loads(prompt)
        observation = request.get("observation") if isinstance(request, dict) else {}
        receipt = request.get("latest_receipt") if isinstance(request, dict) else {}
        content = observation.get("content") if isinstance(observation, dict) else None
        status = ""
        if isinstance(receipt, dict) and isinstance(receipt.get("status"), str):
            status = receipt["status"]
        elif isinstance(observation, dict) and isinstance(
            observation.get("status"), str
        ):
            status = observation["status"]
        if isinstance(content, dict) and content.get("check_passed") is True:
            result = {"kind": "EvaluationChoice", "rationale": "native check passed"}
        elif isinstance(content, dict) and content.get("check_passed") is False:
            result = _smoke_write()
        elif isinstance(content, dict) and isinstance(content.get("content"), str):
            result = (
                _smoke_test() if "value = 1" in content["content"] else _smoke_write()
            )
        elif status == "succeeded":
            result = _smoke_test()
        elif status == "failed":
            result = _smoke_write()
        else:
            result = _smoke_read()
        return Generation(
            text=json.dumps(result, sort_keys=True),
            model="local-smoke-policy-v1",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )


def _smoke_read() -> dict[str, object]:
    return {
        "kind": "ActionChoice",
        "action_name": "read",
        "rationale": "inspect the published workspace schema",
        "arguments": {"path": "main.py"},
    }


def _smoke_write() -> dict[str, object]:
    return {
        "kind": "ActionChoice",
        "action_name": "write",
        "rationale": "satisfy the bounded software objective",
        "arguments": {"path": "main.py", "content": "value = 1\n"},
    }


def _smoke_test() -> dict[str, object]:
    return {
        "kind": "ActionChoice",
        "action_name": "test",
        "rationale": "request the fixture native check",
        "arguments": {},
    }


def _run_identity(options: RunOptions, runtime, evaluator) -> RunIdentity:
    safe = {
        "environment": options.environment,
        "gym_id": options.gym_id,
        "gym_seed": options.gym_seed,
        "provider": options.provider,
        "base_url": options.base_url or os.environ.get("VHARNESS_BASE_URL", ""),
        "model": options.model or os.environ.get("VHARNESS_MODEL", ""),
        "api_key_env": options.api_key_env,
        "limits": {
            "actions": options.max_actions,
            "model_calls": options.max_model_calls,
            "evaluations": options.max_evaluations,
        },
        "token_budget": options.token_budget,
        "max_output_tokens": options.max_output_tokens,
        "repair_budget": options.repair_budget,
        "scripted_checkpoint": options.scripted_checkpoint,
    }
    digest = hashlib.sha256(
        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RunIdentity(
        package_version=VERSION,
        configuration_digest=digest,
        runtime=runtime.identity(),
        evaluator=evaluator.identity(),
    )


def _write_checkpoint_manifest(
    options: RunOptions, session: Session, runtime, evaluator
) -> None:
    """Retain checkpoint identity next to the journal. Pytest tmp is not this file."""
    from .adapters.gymnasium import (  # pylint: disable=import-outside-toplevel
        FROZENLAKE_GOAL,
        FROZENLAKE_MAKE_KWARGS,
    )

    identity = session.manifest()
    payload = {
        **_source_identity(),
        "package_version": VERSION,
        "configuration_digest": identity.configuration_digest,
        "gymnasium_version": runtime.identity().version,
        "env_id": options.gym_id,
        "seed": options.gym_seed,
        "make_kwargs": dict(FROZENLAKE_MAKE_KWARGS),
        "native_goal": {
            "require_terminated": FROZENLAKE_GOAL.require_terminated,
            "reject_truncated": FROZENLAKE_GOAL.reject_truncated,
            "min_return": FROZENLAKE_GOAL.min_return,
        },
        "budgets": {
            "actions": options.max_actions,
            "model_calls": options.max_model_calls,
            "evaluations": options.max_evaluations,
        },
        "context": {
            "policy_version": POLICY_VERSION,
            "token_counter": TOKEN_COUNTER,
            "coefficients": dict(COEFFICIENTS),
        },
        "result_schema_kinds": sorted(RESULT_SCHEMAS),
        "checkpoint_policy": "diagnostic scripted route; not model-reasoning proof",
        "journal": _manifest_relative(options.journal, options.journal.parent),
        "artifacts": _manifest_relative(options.artifacts, options.journal.parent),
        "runtime": {
            "component_id": runtime.identity().component_id,
            "version": runtime.identity().version,
            "configuration_digest": runtime.identity().configuration_digest,
        },
        "evaluator": {
            "component_id": evaluator.identity().component_id,
            "version": evaluator.identity().version,
            "configuration_digest": evaluator.identity().configuration_digest,
        },
        "session_id": session.session_id,
        "status": session.view().status.value,
    }
    path = options.journal.parent / "manifest.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _source_identity() -> dict[str, object]:
    """Record HEAD plus whether the checkpoint ran against uncommitted files."""
    root = Path(__file__).resolve().parents[3]
    revision = _git_output(["git", "rev-parse", "HEAD"], root) or "unknown"
    porcelain = _git_output(["git", "status", "--porcelain"], root) or ""
    return {
        "git_revision": revision,
        "git_dirty": bool(porcelain.strip()),
    }


def _git_output(command: list[str], cwd: Path) -> str | None:
    try:
        return subprocess.check_output(
            command, cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _manifest_relative(path: Path, root: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), root.resolve())).as_posix()
    except ValueError:
        return str(path)


def _print_header(options: RunOptions, session: Session, output: TextIO, model) -> None:
    if options.scripted_checkpoint:
        mode = "scripted gymnasium checkpoint"
    elif options.local_smoke:
        mode = "deterministic local smoke"
    else:
        mode = "live compatible provider"
    print(f"OBJECTIVE {options.objective}", file=output)
    print(f"SESSION {session.session_id} mode={mode}", file=output)
    print(f"MODEL {getattr(model, 'model_id', 'unknown')}", file=output)
    print(f"JOURNAL {options.journal}", file=output)
    print(f"ARTIFACTS {options.artifacts}", file=output)


def _print_observation(
    session: Session, artifacts: ArtifactStore, output: TextIO
) -> None:
    events = session.events(limit=1000)
    observation: dict[str, object] = {}
    for event in reversed(events):
        if event.kind != "receipt_received":
            continue
        receipt = event.payload.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        observation["status"] = receipt.get("status")
        observation["state"] = receipt.get("state")
        raw = receipt.get("raw_artifact")
        if isinstance(raw, Mapping):
            try:
                data = artifacts.read(_artifact_ref(raw))
                observation["content"] = json.loads(data.decode("utf-8"))
            except (AgentError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        break
    if not observation:
        observation = _initial_printed_observation(session, artifacts)
    print(f"OBSERVATION {_compact(observation)}", file=output)


def _initial_printed_observation(
    session: Session, artifacts: ArtifactStore
) -> dict[str, object]:
    state = session.view().lineage_head.state
    observation: dict[str, object] = {
        "state": {"owner": state.owner, "value": state.value}
    }
    for event in session.events(limit=20):
        if event.kind != "session_created":
            continue
        initial = event.payload.get("initial_observation")
        if not isinstance(initial, Mapping):
            break
        raw = initial.get("raw_record")
        if isinstance(raw, Mapping):
            try:
                data = artifacts.read(_artifact_ref(raw))
                observation["content"] = json.loads(data.decode("utf-8"))
            except (
                AgentError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ):
                pass
        break
    return observation


def _print_new_events(  # pylint: disable=too-many-branches
    session: Session, cursor: int, output: TextIO
) -> int:
    events = session.events(after_sequence=cursor, limit=1000)
    for event in events:
        payload = event.payload
        if event.kind == "model_decided":
            print(
                f"MODEL decision={payload.get('result_kind', 'malformed')} "
                f"model={payload.get('model_id', 'unknown')} "
                f"calls={_usage_calls(payload)}",
                file=output,
            )
        elif event.kind == "model_malformed":
            print(
                f"MODEL decision failed detail={payload.get('error', 'unknown')}",
                file=output,
            )
        elif event.kind == "model_repaired":
            print(
                f"MODEL repaired defect={payload.get('failure_class', 'unknown')} "
                f"kind={payload.get('result_kind', 'unknown')} "
                f"field={payload.get('field', 'unknown')}",
                file=output,
            )
        elif event.kind == "action_intended":
            proposal = payload.get("proposal")
            if isinstance(proposal, Mapping):
                print(
                    f"ACTION {proposal.get('action_name')} "
                    f"arguments={_compact(proposal.get('arguments', {}))}",
                    file=output,
                )
        elif event.kind == "evaluation_intended":
            request = payload.get("request")
            request_id = _object_field(request, "request_id")
            if isinstance(request_id, str):
                print(f"EVALUATION request_id={request_id}", file=output)
        elif event.kind == "receipt_received":
            receipt = payload.get("receipt")
            if isinstance(receipt, Mapping):
                detail = (
                    receipt.get("error") or receipt.get("operation_id") or "terminal"
                )
                print(
                    f"RUNTIME receipt status={receipt.get('status')} detail={detail}",
                    file=output,
                )
        elif event.kind == "evaluation_received":
            receipt = payload.get("receipt")
            accepted = receipt.get("accepted") if isinstance(receipt, Mapping) else None
            comparison = (
                receipt.get("comparison") if isinstance(receipt, Mapping) else None
            )
            print(
                f"EVALUATION disposition={payload.get('disposition')} "
                f"accepted={accepted} comparison={comparison}",
                file=output,
            )
        elif event.kind == "agent_waited":
            print(f"WAIT reason={payload.get('reason')}", file=output)
        elif event.kind == "runner_exited":
            print(f"RUNNER-EXIT {payload.get('reason')}", file=output)
        elif event.kind == "question_recorded":
            print(f"OPERATOR question={payload.get('text')}", file=output)
        elif event.kind == "completion_rejected":
            print(
                f"EVALUATION completion rejected reason={payload.get('reason')}",
                file=output,
            )
    return events[-1].sequence if events else cursor


def _pending_evaluation_request_id(
    state: SessionState, *, operator_mode: bool = True
) -> str | None:
    """Return an evaluation still eligible for an operator decision."""
    if not operator_mode:
        return None
    for operation in reversed(state.evaluation_operations):
        if operation.status is EvaluationOperationStatus.INTENDED:
            return operation.request.request_id
    return None


def _object_field(value: object, name: str) -> object:
    """Read one typed or serialized event field without assuming its encoding."""
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _print_budget(session: Session, output: TextIO) -> None:
    view = session.view()
    counters = session.counters()
    limits = view.task.budgets
    print(
        "BUDGET "
        f"actions={_budget(counters.actions, limits.actions)} "
        f"model_calls={_budget(counters.model_calls, limits.model_calls)} "
        f"evaluations={_budget(counters.evaluations, limits.evaluations)}",
        file=output,
    )


def _print_terminal(status: SessionStatus, reason: str | None, output: TextIO) -> None:
    if status is SessionStatus.COMPLETED:
        print("TERMINAL success; external evaluator accepted the task", file=output)
    elif status is SessionStatus.WAITING:
        print(f"TERMINAL wait; reason={reason or 'external condition'}", file=output)
    else:
        print(f"TERMINAL {status.value}", file=output)


def _artifact_ref(value: Mapping[str, object]) -> ArtifactRef:
    digest = value.get("digest")
    size = value.get("size")
    media_type = value.get("media_type")
    provenance = value.get("provenance")
    if not isinstance(digest, str) or not isinstance(size, int):
        raise ValueError("artifact reference is malformed")
    if not isinstance(media_type, str) or not isinstance(provenance, str):
        raise ValueError("artifact reference is malformed")
    return ArtifactRef(
        digest,
        size,
        media_type,
        provenance,
    )


def _usage_calls(payload: Mapping[str, object]) -> object:
    usage = payload.get("usage")
    return (
        usage.get("model_calls", "unknown") if isinstance(usage, Mapping) else "unknown"
    )


def _budget(used: int, limit: int | None) -> str:
    return f"{used}/{limit if limit is not None else 'unbounded'}"


def _compact(value: object) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _terminal_statuses() -> frozenset[SessionStatus]:
    return frozenset(
        {
            SessionStatus.WAITING,
            SessionStatus.STOPPED,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        }
    )


def _exit_for_status(status: SessionStatus) -> int:
    return 0 if status is SessionStatus.COMPLETED else 1


def _exit_for_application_status(status: SessionStatus) -> int:
    """Return the operator exit code for a terminal or waiting application."""
    if status in (SessionStatus.COMPLETED, SessionStatus.WAITING):
        return 0
    return 1


def _write_error(stream: TextIO, category: str, message: str) -> None:
    print(f"ERROR {category}: {message}", file=stream)


def _redact(message: str, options: RunOptions) -> str:
    secret = os.environ.get(options.api_key_env)
    return message.replace(secret, "[REDACTED]") if secret else message


def _redact_provider_error(message: str, api_key_env: str) -> str:
    secret = os.environ.get(api_key_env)
    return message.replace(secret, "[REDACTED]") if secret else message


def _redact_endpoint(value: str | None) -> str | None:
    """Show effective endpoint location without URL credentials or query data."""
    if value is None:
        return None
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _env_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return parsed


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
