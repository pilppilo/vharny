"""SQLite append-only journal for canonical session events."""

from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .codec import canonical_json, parse_json_object
from .errors import IntegrityError, PersistenceError
from .models import Event, JsonValue

SEARCH_LIMIT = 32
EXCERPT_TOKENS = 48
_FTS_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_FTS_QUERY_TOKENS = 32


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """Structured constraints applied after FTS5 matching."""

    kinds: tuple[str, ...] = ()
    min_sequence: int | None = None
    max_sequence: int | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(kind, str) or not kind.strip() for kind in self.kinds):
            raise PersistenceError("search kinds must be non-empty strings")
        if self.min_sequence is not None and self.min_sequence < 0:
            raise PersistenceError("search min_sequence must not be negative")
        if self.max_sequence is not None and self.max_sequence < 0:
            raise PersistenceError("search max_sequence must not be negative")
        if (
            self.min_sequence is not None
            and self.max_sequence is not None
            and self.min_sequence > self.max_sequence
        ):
            raise PersistenceError("search sequence range is inverted")
        if self.correlation_id is not None and not self.correlation_id.strip():
            raise PersistenceError("search correlation_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One FTS5 match with inspectable BM25 and a bounded excerpt."""

    event_id: str
    session_id: str
    sequence: int
    kind: str
    excerpt: str
    bm25: float
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("session_id", self.session_id),
            ("kind", self.kind),
            ("excerpt", self.excerpt),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PersistenceError(f"search hit {name} must be a non-empty string")
        if self.sequence < 1:
            raise PersistenceError("search hit sequence must be positive")
        if not math.isfinite(self.bm25):
            raise PersistenceError("search hit bm25 must be finite")

    @property
    def relevance(self) -> float:
        """Map FTS5 bm25 (more negative is better) onto a non-negative score."""
        return max(0.0, -self.bm25)


class SqliteJournal:
    """Owns one SQLite connection and atomic ordered event appends."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self._path = Path(path).resolve()
        self._read_only = read_only
        try:
            if read_only:
                if not self._path.is_file():
                    raise PersistenceError(f"journal does not exist: {self._path}")
                self._connection = sqlite3.connect(
                    f"{self._path.as_uri()}?mode=ro", uri=True
                )
            else:
                self._connection = sqlite3.connect(str(self._path))
            self._connection.execute("PRAGMA foreign_keys = ON")
            if not read_only:
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._create_schema()
                self._ensure_search_index()
            self._closed = False
        except (sqlite3.Error, PersistenceError) as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            if isinstance(exc, sqlite3.Error):
                raise PersistenceError(f"could not open journal {path}: {exc}") from exc
            raise

    @property
    def path(self) -> Path:
        """Return the resolved SQLite file path."""
        return self._path

    def close(self) -> None:
        """Checkpoint WAL into the database file, then close the connection."""
        if self._closed:
            return
        self._closed = True
        try:
            if not self._read_only:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"could not checkpoint journal {self._path}: {exc}"
            ) from exc
        finally:
            self._connection.close()

    def __enter__(self) -> "SqliteJournal":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def append(self, events: Iterable[Event], *, expected_cursor: int) -> int:
        """Append one or more events through the transactional batch boundary."""
        return self.append_batch(events, expected_cursor=expected_cursor)

    def append_batch(self, events: Iterable[Event], *, expected_cursor: int) -> int:
        """Atomically append one contiguous event batch after an expected cursor."""
        batch = tuple(events)
        if not batch:
            return expected_cursor
        session_ids = {event.session_id for event in batch}
        if len(session_ids) != 1:
            raise PersistenceError("an event batch must belong to one session")
        if self._is_duplicate_batch(batch):
            return self.cursor(batch[0].session_id)
        expected_sequences = tuple(
            range(expected_cursor + 1, expected_cursor + len(batch) + 1)
        )
        if tuple(event.sequence for event in batch) != expected_sequences:
            raise PersistenceError("event batch sequences must be consecutive")
        session_id = batch[0].session_id
        try:
            with self._connection:
                current = self.cursor(session_id)
                if current != expected_cursor:
                    raise PersistenceError(
                        "journal cursor conflict for "
                        f"{session_id}: expected {expected_cursor}, found {current}"
                    )
                self._connection.execute(
                    """
                    INSERT INTO sessions (session_id, cursor, schema_version)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO NOTHING
                    """,
                    (session_id, 0, batch[0].schema_version),
                )
                self._connection.executemany(
                    """
                    INSERT INTO events (
                        event_id, session_id, sequence, kind, schema_version,
                        objective_version, payload, recorded_at, causation_id,
                        correlation_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            event.event_id,
                            event.session_id,
                            event.sequence,
                            event.kind,
                            event.schema_version,
                            event.objective_version,
                            canonical_json(event.payload),
                            event.recorded_at,
                            event.causation_id,
                            event.correlation_id,
                        )
                        for event in batch
                    ],
                )
                self._connection.executemany(
                    """
                    INSERT INTO event_fts (
                        body, event_id, session_id, kind, sequence, correlation_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            _event_body(event),
                            event.event_id,
                            event.session_id,
                            event.kind,
                            event.sequence,
                            event.correlation_id,
                        )
                        for event in batch
                    ],
                )
                self._connection.execute(
                    """
                    INSERT INTO sessions (session_id, cursor, schema_version)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET cursor = excluded.cursor
                    """,
                    (session_id, batch[-1].sequence, batch[-1].schema_version),
                )
        except PersistenceError:
            raise
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(f"journal integrity failure: {exc}") from exc
        except sqlite3.Error as exc:
            raise PersistenceError(f"could not append journal events: {exc}") from exc
        return batch[-1].sequence

    def _is_duplicate_batch(self, batch: tuple[Event, ...]) -> bool:
        """Recognize an exact redelivery without accepting conflicting ID reuse."""
        rows = self._connection.execute(
            "SELECT event_id, session_id, sequence, kind, schema_version, "
            "objective_version, payload, recorded_at, causation_id, correlation_id "
            f"FROM events WHERE event_id IN ({','.join('?' for _ in batch)})",
            tuple(event.event_id for event in batch),
        ).fetchall()
        if not rows:
            return False
        if len(rows) != len(batch):
            raise IntegrityError("event batch mixes new and already-used event IDs")
        existing = {row[0]: row for row in rows}
        for event in batch:
            row = existing[event.event_id]
            expected = (
                event.event_id,
                event.session_id,
                event.sequence,
                event.kind,
                event.schema_version,
                event.objective_version,
                canonical_json(event.payload),
                event.recorded_at,
                event.causation_id,
                event.correlation_id,
            )
            if row != expected:
                raise IntegrityError("event ID was reused with different content")
        return True

    def cursor(self, session_id: str) -> int:
        """Return the latest durable sequence, or zero for an unseen session."""
        try:
            row = self._connection.execute(
                "SELECT cursor FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError(f"could not read journal cursor: {exc}") from exc
        return int(row[0]) if row else 0

    def session_ids(self) -> tuple[str, ...]:
        """Return every session recorded in this journal, ordered by identity."""
        try:
            rows = self._connection.execute(
                "SELECT session_id FROM sessions ORDER BY session_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError(f"could not list journal sessions: {exc}") from exc
        return tuple(str(row[0]) for row in rows)

    def events(
        self, session_id: str, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[Event, ...]:
        """Read a bounded ordered event window."""
        if after_sequence < 0 or limit < 1:
            raise PersistenceError("event query bounds are invalid")
        try:
            rows = self._connection.execute(
                """
                SELECT event_id, session_id, sequence, kind, objective_version, payload,
                       recorded_at, causation_id, correlation_id, schema_version
                FROM events
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (session_id, after_sequence, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError(f"could not read journal events: {exc}") from exc
        return tuple(
            Event(
                event_id=row[0],
                session_id=row[1],
                sequence=row[2],
                kind=row[3],
                objective_version=row[4],
                payload=parse_json_object(row[5]),
                recorded_at=row[6],
                causation_id=row[7],
                correlation_id=row[8],
                schema_version=row[9],
            )
            for row in rows
        )

    def search(
        self,
        session_id: str,
        query: str,
        *,
        filters: SearchFilter | None = None,
        limit: int = SEARCH_LIMIT,
    ) -> tuple[SearchHit, ...]:
        """Return BM25-ranked event excerpts with optional structured filters."""
        if not isinstance(session_id, str) or not session_id.strip():
            raise PersistenceError("search session_id must be a non-empty string")
        if not isinstance(query, str):
            raise PersistenceError("search query must be a string")
        if limit < 1:
            raise PersistenceError("search limit must be positive")
        match = _match_query(query)
        if not match:
            return ()
        applied = filters if filters is not None else SearchFilter()
        clauses = ["event_fts MATCH ?", "events.session_id = ?"]
        params: list[object] = [match, session_id]
        if applied.kinds:
            clauses.append(f"events.kind IN ({','.join('?' for _ in applied.kinds)})")
            params.extend(applied.kinds)
        if applied.min_sequence is not None:
            clauses.append("events.sequence >= ?")
            params.append(applied.min_sequence)
        if applied.max_sequence is not None:
            clauses.append("events.sequence <= ?")
            params.append(applied.max_sequence)
        if applied.correlation_id is not None:
            clauses.append("events.correlation_id = ?")
            params.append(applied.correlation_id)
        sql = (
            "SELECT events.event_id, events.session_id, events.kind, "
            "events.sequence, events.correlation_id, event_fts.body, "
            "bm25(event_fts) "
            "FROM event_fts INNER JOIN events ON events.event_id = event_fts.event_id "
            "WHERE "
            + " AND ".join(clauses)
            + " ORDER BY bm25(event_fts) ASC, events.sequence ASC, "
            "events.event_id ASC LIMIT ?"
        )
        params.append(limit)
        try:
            rows = self._connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError(f"could not search journal events: {exc}") from exc
        return tuple(
            SearchHit(
                event_id=row[0],
                session_id=row[1],
                kind=row[2],
                sequence=int(row[3]),
                correlation_id=row[4],
                excerpt=_excerpt(row[5]),
                bm25=float(row[6]),
            )
            for row in rows
        )

    def _create_schema(self) -> None:
        with closing(self._connection.cursor()) as cursor:
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    cursor INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    objective_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    causation_id TEXT,
                    correlation_id TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    UNIQUE(session_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS events_by_correlation
                    ON events(session_id, correlation_id, sequence);
                CREATE INDEX IF NOT EXISTS events_by_kind
                    ON events(session_id, kind, sequence);
                CREATE TABLE IF NOT EXISTS artifacts (
                    digest TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """)

    def _ensure_search_index(self) -> None:
        self._require_fts5()
        self._connection.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
                body,
                event_id UNINDEXED,
                session_id UNINDEXED,
                kind UNINDEXED,
                sequence UNINDEXED,
                correlation_id UNINDEXED,
                tokenize = 'unicode61'
            )
            """)
        try:
            indexed = self._connection.execute(
                "SELECT count(*) FROM event_fts"
            ).fetchone()
            stored = self._connection.execute("SELECT count(*) FROM events").fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"could not inspect journal search index: {exc}"
            ) from exc
        if indexed is None or stored is None:
            raise PersistenceError("could not inspect journal search index")
        if int(indexed[0]) != int(stored[0]):
            self._rebuild_search_index()

    def _rebuild_search_index(self) -> None:
        try:
            with self._connection:
                self._connection.execute("DELETE FROM event_fts")
                rows = self._connection.execute("""
                    SELECT event_id, session_id, sequence, kind, payload,
                           correlation_id
                    FROM events
                    ORDER BY session_id, sequence
                    """).fetchall()
                self._connection.executemany(
                    """
                    INSERT INTO event_fts (
                        body, event_id, session_id, kind, sequence, correlation_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            _search_body(row[3], parse_json_object(row[4]), row[5]),
                            row[0],
                            row[1],
                            row[3],
                            row[2],
                            row[5],
                        )
                        for row in rows
                    ],
                )
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"could not rebuild journal search index: {exc}"
            ) from exc

    def _require_fts5(self) -> None:
        try:
            options = {
                str(row[0])
                for row in self._connection.execute("PRAGMA compile_options")
            }
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"could not inspect SQLite compile options: {exc}"
            ) from exc
        if "ENABLE_FTS5" not in options:
            raise PersistenceError("SQLite FTS5 is required for journal retrieval")


def _event_body(event: Event) -> str:
    return _search_body(event.kind, event.payload, event.correlation_id)


def _search_body(
    kind: str, payload: Mapping[str, JsonValue], correlation_id: str | None
) -> str:
    parts = [kind]
    if correlation_id:
        parts.append(correlation_id)
    parts.extend(_text_fragments(payload))
    return " ".join(parts)


def _text_fragments(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        text = " ".join(value.split())
        return (text,) if text else ()
    if isinstance(value, Mapping):
        fragments: list[str] = []
        for item in value.values():
            fragments.extend(_text_fragments(item))
        return tuple(fragments)
    if isinstance(value, (list, tuple)):
        fragments = []
        for item in value:
            fragments.extend(_text_fragments(item))
        return tuple(fragments)
    if isinstance(value, bool) or value is None:
        return ()
    if isinstance(value, (int, float)) and math.isfinite(value):
        return (str(value),)
    return ()


def _match_query(text: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _FTS_TOKEN.findall(text.lower()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= _FTS_QUERY_TOKENS:
            break
    return " OR ".join(f'"{token}"' for token in tokens)


def _excerpt(body: str) -> str:
    tokens = body.split()
    if not tokens:
        return body
    return " ".join(tokens[:EXCERPT_TOKENS])
