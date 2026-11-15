"""The retained Gymnasium checkpoint must ship a replayable journal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterator, Mapping

from vharness.agent.artifacts import ArtifactStore
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import ArtifactRef, SessionStatus
from vharness.agent.replay import replay

_EVIDENCE = (
    Path(__file__).resolve().parents[2] / "evidence" / "phase-0002-gymnasium-frozenlake"
)


def _artifact_references(value: object) -> Iterator[ArtifactRef]:
    """Yield every serialized artifact reference reachable from an event payload."""
    if isinstance(value, Mapping):
        required = {"digest", "size", "media_type", "provenance"}
        if required <= value.keys():
            yield ArtifactRef(
                digest=str(value["digest"]),
                size=int(value["size"]),
                media_type=str(value["media_type"]),
                provenance=str(value["provenance"]),
            )
        for nested in value.values():
            yield from _artifact_references(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _artifact_references(nested)


def test_retained_gymnasium_checkpoint_journal_replays() -> None:
    """Fail if the in-repo checkpoint is missing or not independently replayable."""
    journal_path = _EVIDENCE / "session.sqlite3"
    manifest_path = _EVIDENCE / "manifest.json"
    artifacts_root = _EVIDENCE / "artifacts"
    assert journal_path.is_file(), (
        "retained checkpoint journal is missing: "
        f"{journal_path.as_posix()} (gitignore must not drop session.sqlite3)"
    )
    assert not Path(f"{journal_path}-wal").exists()
    assert manifest_path.is_file()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["journal"] == "session.sqlite3"
    assert payload["artifacts"] == "artifacts"
    assert payload["status"] == "completed"
    assert payload["git_dirty"] is False
    assert payload["git_revision"] == "7f11889a5872600395946ddeea3fab502bad04d3"
    pointer = (artifacts_root / ".journals").read_text(encoding="utf-8").strip()
    assert pointer == "../session.sqlite3"
    assert not Path(pointer).is_absolute()
    store = ArtifactStore(artifacts_root)
    assert journal_path.resolve() in store.registered_journals()
    journal = SqliteJournal(journal_path)
    try:
        session_id = payload["session_id"]
        events = journal.events(session_id, limit=max(journal.cursor(session_id), 1))
        assert events
        assert replay(events).status is SessionStatus.COMPLETED
        references = {
            reference.digest
            for event in events
            for reference in _artifact_references(event.payload)
        }
        assert references
        for digest in references:
            matching = next(
                reference
                for event in events
                for reference in _artifact_references(event.payload)
                if reference.digest == digest
            )
            content = store.read(matching)
            assert hashlib.sha256(content).hexdigest() == matching.digest
    finally:
        journal.close()
