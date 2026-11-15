"""Journal replay, idempotency, and FTS5 retrieval checks."""

import sqlite3

import pytest

from vharness.agent.errors import IntegrityError, PersistenceError
from vharness.agent.journal import SearchFilter, SqliteJournal
from vharness.agent.models import Event


def test_exact_event_redelivery_is_idempotent(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    event = Event(
        event_id="event-1",
        session_id="session-1",
        sequence=1,
        kind="fixture",
        objective_version=1,
        payload={"value": 1},
        recorded_at="2026-09-05T00:00:00+00:00",
    )

    assert journal.append((event,), expected_cursor=0) == 1
    assert journal.append((event,), expected_cursor=1) == 1
    assert journal.events("session-1") == (event,)
    journal.close()
    wal = tmp_path / "journal.sqlite3-wal"
    assert not wal.exists()
    reopened = SqliteJournal(tmp_path / "journal.sqlite3")
    assert reopened.events("session-1") == (event,)
    reopened.close()


def test_event_id_reuse_rejects_changed_timestamp(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    event = Event("event-1", "session-1", 1, "fixture", 1, {"value": 1}, "now")
    journal.append((event,), expected_cursor=0)
    changed = Event("event-1", "session-1", 1, "fixture", 1, {"value": 1}, "later")
    with pytest.raises(IntegrityError, match="reused"):
        journal.append((changed,), expected_cursor=1)
    journal.close()


def _event(
    event_id: str,
    sequence: int,
    kind: str,
    payload: dict,
    *,
    session_id: str = "session-1",
    correlation_id: str | None = None,
) -> Event:
    return Event(
        event_id=event_id,
        session_id=session_id,
        sequence=sequence,
        kind=kind,
        objective_version=1,
        payload=payload,
        recorded_at="2026-09-12T00:00:00+00:00",
        correlation_id=correlation_id,
    )


def test_fts5_ranks_better_matches_first(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    journal.append(
        (
            _event(
                "event-1",
                1,
                "knowledge_recorded",
                {"item": {"statement": "inspect the fixture inspect fixture"}},
            ),
            _event(
                "event-2",
                2,
                "knowledge_recorded",
                {"item": {"statement": "unrelated inspect"}},
            ),
            _event(
                "event-3",
                3,
                "receipt_received",
                {"note": "totally different observation"},
            ),
        ),
        expected_cursor=0,
    )
    hits = journal.search("session-1", "inspect the fixture")
    assert [hit.event_id for hit in hits][:2] == ["event-1", "event-2"]
    assert hits[0].relevance > hits[1].relevance
    assert "inspect" in hits[0].excerpt
    journal.close()


def test_structured_filters_limit_fts_hits(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    journal.append(
        (
            _event(
                "event-1",
                1,
                "knowledge_recorded",
                {"item": {"statement": "inspect fixture"}},
                correlation_id="op-1",
            ),
            _event(
                "event-2",
                2,
                "receipt_received",
                {"note": "inspect fixture"},
                correlation_id="op-2",
            ),
        ),
        expected_cursor=0,
    )
    by_kind = journal.search(
        "session-1",
        "inspect fixture",
        filters=SearchFilter(kinds=("receipt_received",)),
    )
    assert [hit.event_id for hit in by_kind] == ["event-2"]
    by_range = journal.search(
        "session-1",
        "inspect fixture",
        filters=SearchFilter(min_sequence=2, max_sequence=2),
    )
    assert [hit.event_id for hit in by_range] == ["event-2"]
    by_correlation = journal.search(
        "session-1",
        "inspect fixture",
        filters=SearchFilter(correlation_id="op-1"),
    )
    assert [hit.event_id for hit in by_correlation] == ["event-1"]
    journal.close()


def test_sequence_filter_uses_integer_event_order(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    events = tuple(
        _event(
            f"event-{sequence}",
            sequence,
            "knowledge_recorded",
            {"item": {"statement": "inspect fixture"}},
        )
        for sequence in range(1, 12)
    )
    journal.append(events, expected_cursor=0)
    hits = journal.search(
        "session-1",
        "inspect fixture",
        filters=SearchFilter(min_sequence=10),
    )
    assert [hit.event_id for hit in hits] == ["event-10", "event-11"]
    assert all(hit.sequence >= 10 for hit in hits)
    journal.close()


def test_search_query_sanitizes_fts_syntax_and_empty_tokens(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    journal.append(
        (
            _event(
                "event-1", 1, "knowledge_recorded", {"item": {"statement": "inspect"}}
            ),
        ),
        expected_cursor=0,
    )
    hits = journal.search("session-1", 'inspect AND "orphan')
    assert [hit.event_id for hit in hits] == ["event-1"]
    assert journal.search("session-1", "???") == ()
    with pytest.raises(PersistenceError, match="session_id"):
        journal.search("  ", "inspect")
    journal.close()


def test_search_is_session_scoped(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    journal.append(
        (
            _event(
                "event-1",
                1,
                "knowledge_recorded",
                {"item": {"statement": "inspect fixture"}},
            ),
        ),
        expected_cursor=0,
    )
    journal.append(
        (
            _event(
                "event-2",
                1,
                "knowledge_recorded",
                {"item": {"statement": "inspect fixture"}},
                session_id="session-2",
            ),
        ),
        expected_cursor=0,
    )
    assert [hit.event_id for hit in journal.search("session-1", "inspect")] == [
        "event-1"
    ]
    assert [hit.event_id for hit in journal.search("session-2", "inspect")] == [
        "event-2"
    ]
    journal.close()


def test_inverted_sequence_filter_is_rejected():
    with pytest.raises(PersistenceError, match="inverted"):
        SearchFilter(min_sequence=4, max_sequence=2)


def test_search_index_backfills_and_redelivery_does_not_duplicate(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = SqliteJournal(path)
    event = _event(
        "event-1",
        1,
        "knowledge_recorded",
        {"item": {"statement": "inspect fixture"}},
    )
    journal.append((event,), expected_cursor=0)
    journal.append((event,), expected_cursor=1)
    first = journal.search("session-1", "inspect fixture")
    assert [hit.event_id for hit in first] == ["event-1"]
    journal.close()

    connection = sqlite3.connect(str(path))
    connection.execute("DELETE FROM event_fts")
    connection.commit()
    connection.close()

    reopened = SqliteJournal(path)
    restored = reopened.search("session-1", "inspect fixture")
    assert [hit.event_id for hit in restored] == ["event-1"]
    reopened.close()


def test_sqlite_journal_init_failure_closes_connection(tmp_path):
    path = tmp_path / "not-a-database.sqlite3"
    path.write_bytes(b"this is not a sqlite database")

    with pytest.raises(PersistenceError, match="could not open journal"):
        SqliteJournal(path)

    assert path.read_bytes() == b"this is not a sqlite database"
