"""Capture one bounded unsupervised live repair diagnostic, outside EXP-0001."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Direct script invocation requires adding the repository root before imports.
# Shared experiment-local helpers preserve the frozen fixture mechanics.
# pylint: disable=wrong-import-position,protected-access
from tools import exp_0001_supervisor_policy as export
from tools import phase3_failure_traces as fixture
from vharness.agent import CompatibleModel, ContractError, Session, TransitionError
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json
from vharness.agent.evidence import counters
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import ModelCallError
from vharness.agent.models import Event
from vharness.agent.replay import replay
from vharness.generators.openai_compat import OpenAICompatible


@dataclass(frozen=True)
class DiagnosticConfig:
    """Operator-selected transport with fixed diagnostic resource bounds."""

    base_url: str
    model_id: str
    key_env: str = "LONGCAT_API_KEY"


def _write(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_json(value) + "\n")


def _summary(events: tuple[Event, ...], terminal: str) -> dict[str, object]:
    state = replay(events)
    return {
        "terminal": terminal,
        "accepted": len(state.accepted_lineage) > 1,
        "counters": counters(events),
        "actions": [
            {"sequence": event.sequence, "proposal": event.payload["proposal"]}
            for event in events
            if event.kind == "action_intended"
        ],
        "decisions": [
            {
                "sequence": event.sequence,
                "result_kind": event.payload.get("result_kind"),
            }
            for event in events
            if event.kind == "model_decided"
        ],
        "replay_verified": True,
        "limitations": [
            "Single unscripted software-fixture diagnostic, not the frozen EXP sample.",
            "An external failing test is seeded before model calls; "
            "no scripted model decisions.",
            "No supervisor or operator hints; "
            "task exposes the native test requirement.",
            "This checks basic recovery capability, "
            "not independent escape from a pre-existing model loop.",
        ],
    }


def _manifest(output: Path, config: DiagnosticConfig) -> str:
    """Freeze source, configuration and visible fixture state before calls."""
    configuration = {
        "provider": config,
        "model_calls": fixture.MAX_MODEL_CALLS,
        "actions": fixture.MAX_ACTIONS,
        "evaluations": fixture.MAX_EVALUATIONS,
        "context_tokens": fixture.CONTEXT_WINDOW_TOKENS,
        "max_output_tokens": fixture.MAX_OUTPUT_TOKENS,
        "repair_budget": fixture.REPAIR_BUDGET,
        "temperature": 0.0,
        "provider_retries": 0,
        "length_retries": 0,
        "supervision": False,
        "seeded_external_action": "run_tests",
        "initial_workspace": dict(fixture.INITIAL_WORKSPACE),
    }
    source = fixture._source_identity()
    source["tool"] = "tools/phase3_p0_diagnostic.py"
    source["tool_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    source["fixture_sha256"] = hashlib.sha256(
        Path(fixture.__file__).read_bytes()
    ).hexdigest()
    digest = hashlib.sha256(canonical_json(configuration).encode()).hexdigest()
    _write(
        output / "manifest.json",
        {
            "source": source,
            "configuration": configuration,
            "configuration_digest": digest,
            "claim": "Single unsupervised diagnostic; no general benefit claim.",
        },
    )
    return digest


def _drive(session: Session, model: CompatibleModel) -> str:
    """Advance within the fixed budget and preserve distinguishable stop causes."""
    for _ in range(fixture.MAX_MODEL_CALLS):
        try:
            session.step(model, token_budget=fixture.CONTEXT_WINDOW_TOKENS)
        except ModelCallError:
            return "model_or_provider_error"
        except TransitionError:
            return "budget_or_transition_boundary"
        except ContractError:
            return "contract_rejected"
        measured = session.counters()
        print(
            f"calls={measured.model_calls} actions={measured.actions} "
            f"evaluations={measured.evaluations} commits={measured.commits}",
            flush=True,
        )
        if len(session.accepted_lineage()) > 1:
            return "accepted"
        if session.view().status.value in {
            "waiting",
            "paused",
            "stopped",
            "failed",
            "completed",
        }:
            return session.view().status.value
    return "model_budget_exhausted"


def capture(output: Path, config: DiagnosticConfig) -> dict[str, object]:
    """Retain inputs, outputs, manifests and receipts without retaining credentials."""
    key = os.environ.get(config.key_env)
    if not key:
        raise RuntimeError(f"{config.key_env} is unavailable; ask the operator")
    output.mkdir(parents=True, exist_ok=False)
    digest = _manifest(output, config)
    journal = SqliteJournal(output / "session.sqlite3")
    generator = None
    events: tuple[Event, ...] = ()
    terminal = "model_budget_exhausted"
    try:
        artifacts = ArtifactStore(output / "artifacts")
        runtime = fixture.FixtureRuntime(artifacts)
        session = Session.create(
            "phase3-p0-live-diagnostic",
            replace(fixture._task(runtime), task_id="phase3-p0-live-diagnostic"),
            journal=journal,
            artifacts=artifacts,
            environment=fixture.FixtureEnvironment(runtime),
            runtime=runtime,
            evaluator=fixture.FixtureEvaluator(runtime),
            run_identity=replace(fixture._run_identity(), configuration_digest=digest),
        )
        session.begin_attempt()
        session.submit_action(
            "run_tests", {}, rationale="Seed one externally observed failing test"
        )
        generator = OpenAICompatible(
            config.base_url,
            config.model_id,
            key,
            timeout=60.0,
            max_retries=0,
            length_retries=0,
            max_tokens=fixture.MAX_OUTPUT_TOKENS,
            temperature=0.0,
            cache_path=None,
        )
        setattr(generator, "provider_id", "LongCat")
        model = CompatibleModel(
            generator, model_id=config.model_id, repair_budget=fixture.REPAIR_BUDGET
        )
        terminal = _drive(session, model)
        events = session.events(limit=10000)
        if replay(events) != session._state:
            raise RuntimeError("Diagnostic projection differs from replay")
    finally:
        if generator is not None:
            generator.close()
        journal.close()
    stream, _ = export._event_stream_bytes(events)
    (output / export.EVENT_STREAM_NAME).write_bytes(stream)
    summary = _summary(events, terminal)
    _write(output / "summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Parse diagnostic options and construct the explicitly selected provider."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    summary = capture(args.output, DiagnosticConfig(args.base_url, args.model))
    print(
        canonical_json(
            {"accepted": summary["accepted"], "terminal": summary["terminal"]}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
