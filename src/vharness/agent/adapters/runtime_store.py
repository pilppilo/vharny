"""Durable dispatch facts for the local process runtime."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from ..codec import canonical_json
from ..errors import ContractError, PersistenceError
from ..codec import parse_json_object
from ..models import ActionProposal, ExecutionReceipt
from ..session_state import receipt_from_payload


class RuntimeStore:
    """Persist proposal fingerprints before effects and receipts afterward."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(self._path, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("""CREATE TABLE IF NOT EXISTS dispatches (
                    proposal_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    receipt TEXT
                )""")
            self._connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    proposal_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    process_group INTEGER NOT NULL,
                    session_id INTEGER NOT NULL,
                    start_marker TEXT
                )""")
            self._connection.commit()
        except sqlite3.Error as exc:
            raise PersistenceError(f"could not open runtime store: {exc}") from exc

    def prepare(
        self, proposal_id: str, fingerprint: str
    ) -> tuple[bool, ExecutionReceipt | None]:
        """Durably claim an ID before an effect, returning a prior receipt."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT fingerprint, receipt FROM dispatches WHERE proposal_id=?",
                    (proposal_id,),
                ).fetchone()
                if row is not None:
                    if row[0] != fingerprint:
                        raise ContractError(
                            "proposal ID was reused with different content"
                        )
                    self._connection.commit()
                    prior = (
                        receipt_from_payload(parse_json_object(row[1]))
                        if row[1] is not None
                        else None
                    )
                    return True, prior
                self._connection.execute(
                    "INSERT INTO dispatches(proposal_id, fingerprint) VALUES (?, ?)",
                    (proposal_id, fingerprint),
                )
                self._connection.commit()
                return False, None
            except ContractError:
                self._connection.rollback()
                raise
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise PersistenceError(
                    f"could not record dispatch intent: {exc}"
                ) from exc

    def finish(self, receipt: ExecutionReceipt, fingerprint: str) -> ExecutionReceipt:
        """Persist a receipt for an earlier matching dispatch intent."""
        encoded = canonical_json(receipt)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT fingerprint, receipt FROM dispatches WHERE proposal_id=?",
                    (receipt.proposal_id,),
                ).fetchone()
                if row is None or row[0] != fingerprint:
                    raise ContractError(
                        "receipt has no matching durable dispatch intent"
                    )
                if row[1] is not None:
                    prior = receipt_from_payload(parse_json_object(row[1]))
                    if canonical_json(prior) != encoded:
                        raise ContractError(
                            "proposal receipt conflicts with recorded result"
                        )
                    self._connection.commit()
                    return prior
                self._connection.execute(
                    "UPDATE dispatches SET receipt=? WHERE proposal_id=?",
                    (encoded, receipt.proposal_id),
                )
                self._connection.commit()
                return receipt
            except ContractError:
                self._connection.rollback()
                raise
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise PersistenceError(
                    f"could not record runtime receipt: {exc}"
                ) from exc

    def receipt(self, proposal_id: str) -> ExecutionReceipt | None:
        """Return a recorded receipt, if the dispatch has completed locally."""
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT receipt FROM dispatches WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise PersistenceError(
                    f"could not read runtime receipt: {exc}"
                ) from exc
        return (
            receipt_from_payload(parse_json_object(row[0]))
            if row and row[0] is not None
            else None
        )

    # Persisted process identity is one explicit tuple of correlated facts.
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def record_job(
        self,
        proposal_id: str,
        job_id: str,
        pid: int,
        process_group: int,
        session_id: int,
        start_marker: str | None,
    ) -> None:
        """Persist observed native process identity after launch, before receipt."""
        with self._lock:
            try:
                self._connection.execute(
                    """INSERT INTO jobs
                    (proposal_id, job_id, pid, process_group, session_id, start_marker)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (proposal_id, job_id, pid, process_group, session_id, start_marker),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                raise PersistenceError(
                    f"could not record process identity: {exc}"
                ) from exc

    def close(self) -> None:
        """Close the owned SQLite connection."""
        with self._lock:
            self._connection.close()


def proposal_fingerprint(proposal: ActionProposal) -> str:
    """Hash stable proposal content while excluding incidental object layout."""
    payload = {
        "session_id": proposal.session_id,
        "objective_version": proposal.objective_version,
        "attempt_id": proposal.attempt_id,
        "action_name": proposal.action_name,
        "arguments": dict(proposal.arguments),
        "expected_state": {
            "owner": proposal.expected_state.owner,
            "value": proposal.expected_state.value,
            "digest": proposal.expected_state.digest,
            "revision": proposal.expected_state.revision,
            "restorable": proposal.expected_state.restorable,
        },
        "idempotency_key": proposal.idempotency_key,
        "rationale": proposal.rationale,
        "requires_revision": proposal.requires_revision,
        "reservation": {
            "reservation_id": proposal.reservation.reservation_id,
            "objective_version": proposal.reservation.objective_version,
            "actions": proposal.reservation.actions,
            "evaluations": proposal.reservation.evaluations,
            "model_calls": proposal.reservation.model_calls,
            "cost": (
                None
                if proposal.reservation.cost is None
                else str(proposal.reservation.cost)
            ),
        },
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
