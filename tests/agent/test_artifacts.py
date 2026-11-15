"""Artifact publication checks."""

import hashlib

import pytest

from vharness.agent.artifacts import MAX_JSON_BYTES, ArtifactStore
from vharness.agent.errors import IntegrityError, PersistenceError
from vharness.agent.models import ArtifactRef


def test_artifact_store_publishes_and_verifies_content(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    reference = store.put(b"evidence", media_type="text/plain", provenance="fixture")

    assert store.read(reference) == b"evidence"
    assert (
        store.put(b"evidence", media_type="text/plain", provenance="fixture")
        == reference
    )


def test_artifact_store_measures_only_a_successful_read(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    reference = store.put(b"evidence", media_type="text/plain", provenance="fixture")

    measured = store.read_measured(reference)

    assert measured.data == b"evidence"
    assert measured.bytes_read == len(b"evidence")
    with pytest.raises(IntegrityError, match="missing"):
        store.read_measured(ArtifactRef("f" * 64, 1, "text/plain", "fixture"))


def test_artifact_store_rejects_missing_or_changed_required_evidence(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    reference = ArtifactRef("f" * 64, 1, "text/plain", "fixture")

    with pytest.raises(IntegrityError, match="missing"):
        store.read(reference)


def test_artifact_store_rejects_oversized_retained_reference_before_read(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    data = b"x" * (MAX_JSON_BYTES + 1)
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / "objects" / digest[:2] / digest[2:]
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    reference = ArtifactRef(digest, len(data), "text/plain", "fixture")

    with pytest.raises(PersistenceError, match="byte bound"):
        store.read(reference)


def test_artifact_publication_failure_leaves_no_published_object(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "objects")

    def fail_publish(_source, _target):
        raise OSError("injected publication failure")

    monkeypatch.setattr("vharness.agent.artifacts.os.link", fail_publish)
    with pytest.raises(PersistenceError, match="could not publish artifact"):
        store.put(b"evidence", media_type="text/plain", provenance="fixture")

    assert not any(path.is_file() for path in (tmp_path / "objects").rglob("*"))


def test_artifact_write_failure_leaves_no_pending_file(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "objects")

    def fail_fsync(_fileno):
        raise OSError("injected fsync failure")

    monkeypatch.setattr("vharness.agent.artifacts.os.fsync", fail_fsync)
    with pytest.raises(PersistenceError, match="could not publish artifact"):
        store.put(b"evidence", media_type="text/plain", provenance="fixture")

    assert not any(
        path.is_file() and path.name.startswith(".pending-")
        for path in (tmp_path / "objects").rglob("*")
    )


def test_register_journal_stores_a_portable_relative_path(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    journal = tmp_path / "session.sqlite3"
    journal.write_bytes(b"")
    store.register_journal(journal)
    recorded = (
        (tmp_path / "artifacts" / ".journals").read_text(encoding="utf-8").strip()
    )
    assert recorded == "../session.sqlite3"
    assert journal.resolve() in store.registered_journals()


def test_inventory_and_drop_leave_unrelated_objects(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    kept = store.put(b"keep", media_type="text/plain", provenance="fixture")
    gone = store.put(b"drop-me", media_type="text/plain", provenance="fixture")
    listed = dict(store.inventory())
    assert listed[kept.digest] == kept.size
    assert listed[gone.digest] == gone.size
    assert store.drop(gone.digest) is True
    assert store.drop(gone.digest) is False
    assert dict(store.inventory()) == {kept.digest: kept.size}
    assert store.read(kept) == b"keep"
