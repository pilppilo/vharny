"""Focused regressions for the retained live-evidence verifier."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIVE_BUNDLE = ROOT / "evidence" / "phase-0002-live-software"

sys.path.insert(0, str(ROOT))
from tools import phase2_live_software_evidence as evidence  # noqa: E402
from tools import phase2_operator_evidence as operator_evidence  # noqa: E402
from vharness.agent.errors import IntegrityError  # noqa: E402
from vharness.agent.models import SessionStatus, SessionView  # noqa: E402
from vharness.agent.replay import replay  # noqa: E402


def _live_events():
    journal = evidence._ReadOnlyJournal(LIVE_BUNDLE / "session.sqlite3")
    try:
        return evidence._all_events(journal, evidence.SESSION_ID)
    finally:
        journal.close()


def test_live_verifier_rejects_forged_terminal_identity() -> None:
    """A terminal event with altered input fails durable intent correlation."""
    events = list(_live_events())
    terminal_index = next(
        index for index, event in enumerate(events) if event.kind == "model_decided"
    )
    terminal = events[terminal_index]
    payload = dict(terminal.payload)
    payload["input"] = "{}"
    events[terminal_index] = replace(terminal, payload=payload)
    report = json.loads((LIVE_BUNDLE / "report.json").read_text(encoding="utf-8"))

    with pytest.raises(IntegrityError, match="terminal input"):
        evidence._validate_live_events(events, report)


def test_live_verifier_rejects_current_view_mismatch() -> None:
    events = _live_events()
    state = replay(events)
    view = SessionView(
        session_id=evidence.SESSION_ID,
        status=state.status,
        task=state.task,
        cursor=events[-1].sequence,
        lineage_head=state.lineage_head,
        active_attempt=state.active_attempt,
        pending_proposal_id=state.pending_proposal_id,
        wait_reason=state.wait_reason,
    )

    evidence._validate_current_projection(view, state, events[-1].sequence, "test")

    with pytest.raises(RuntimeError, match="test current projection mismatch"):
        evidence._validate_current_projection(
            replace(view, status=SessionStatus.FAILED),
            state,
            events[-1].sequence,
            "test",
        )


def test_live_verifier_recomputes_model_attempt_rows() -> None:
    report = json.loads((LIVE_BUNDLE / "report.json").read_text(encoding="utf-8"))
    report["run"]["attempts"][0]["finish_reason"] = "forged"

    with pytest.raises(RuntimeError, match="report attempts mismatch"):
        evidence._validate_report_against_events(report, _live_events())


def test_live_verifier_rejects_forged_report_fresh_process_field() -> None:
    stored = json.loads(
        (LIVE_BUNDLE / "fresh-process-reopen.json").read_text(encoding="utf-8")
    )
    report_claim = json.loads(
        (LIVE_BUNDLE / "report.json").read_text(encoding="utf-8")
    )["fresh_process_verification"]
    del report_claim["run"]["status"]

    with pytest.raises(
        RuntimeError, match="report fresh-process verification mismatch"
    ):
        evidence._validate_report_fresh_process(
            report_claim,
            stored,
            "0138047de4fe60cd15a2c9ab461768a68d322160",
            evidence.derive_counters(_live_events()),
        )
    assert "supervisor_calls" not in report_claim["run"]["counters"]


def test_live_verifier_rejects_wrong_legacy_supervisor_counter() -> None:
    stored = json.loads(
        (LIVE_BUNDLE / "fresh-process-reopen.json").read_text(encoding="utf-8")
    )
    report_claim = json.loads(
        (LIVE_BUNDLE / "report.json").read_text(encoding="utf-8")
    )["fresh_process_verification"]
    report_claim["run"]["counters"]["supervisor_calls"] = 1

    with pytest.raises(
        RuntimeError, match="report fresh-process verification mismatch"
    ):
        evidence._validate_report_fresh_process(
            report_claim,
            stored,
            "0138047de4fe60cd15a2c9ab461768a68d322160",
            evidence.derive_counters(_live_events()),
        )


def _skip_unless_pinned_revision_is_present(bundle: Path) -> None:
    """This copy has no Harny history, so a missing pin cannot be verified."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    source = manifest.get("source")
    revision = source.get("git_revision") if isinstance(source, dict) else None
    if not isinstance(revision, str):
        return
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if present.returncode != 0:
        pytest.skip(f"pinned source revision {revision} is not in this repository")


def test_verify_accepts_valid_live_bundle_without_mutation() -> None:
    _skip_unless_pinned_revision_is_present(LIVE_BUNDLE)
    before = evidence._tree_bytes(LIVE_BUNDLE)

    evidence.verify_bundle(LIVE_BUNDLE, ROOT)

    assert evidence._tree_bytes(LIVE_BUNDLE) == before


@pytest.mark.parametrize(
    "safe_path",
    [evidence._safe_bundle_path, operator_evidence._safe_bundle_path],
)
def test_evidence_path_rejects_reparse_component(
    safe_path: Callable[[Path, str, str], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both verifiers reject a junction-like component before resolution."""
    bundle = tmp_path / "bundle"
    component = bundle / "nested"
    component.mkdir(parents=True)
    module = sys.modules[safe_path.__module__]
    monkeypatch.setattr(module, "_is_reparse_point", lambda path: path == component)

    with pytest.raises(RuntimeError, match="reparse point"):
        safe_path(bundle, "nested/file", "test path")


@pytest.mark.parametrize(
    "make_environment",
    [evidence._python_environment, operator_evidence._python_environment],
)
def test_evidence_fresh_process_environment_is_allowlisted(
    make_environment: Callable[[Path], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither verifier forwards unrelated environment variables to children."""
    monkeypatch.setenv("UNRELATED_EVIDENCE_SECRET", "must-not-cross-boundary")
    environment = make_environment(ROOT)

    assert "UNRELATED_EVIDENCE_SECRET" not in environment
    assert set(environment) <= {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "PYTHONPATH",
        "PYTHONIOENCODING",
    }
    assert environment["PYTHONPATH"] == str((ROOT / "src").resolve())
    assert environment["PYTHONIOENCODING"] == "utf-8"
