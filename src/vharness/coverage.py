"""How much of a run actually completed: attempt outcomes and failures.

A scan whose model calls failed has not analyzed the code it reports on, so
reports and exit codes need to say so rather than present "no findings".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .core import Attempt

#: Attempt statuses that mean the attempt produced no usable analysis.
FAILED_STATUSES = ("api_error", "parse_error", "internal_error")

#: Exit code for a scan or replay that finished with failed attempts.
EXIT_INCOMPLETE = 3


@dataclass(frozen=True)
class AttemptFailure:
    """One attempt that ended without a usable result."""

    source: str
    status: str
    message: str


@dataclass(frozen=True)
class RunCoverage:
    """Counts of attempt outcomes for one run, plus each failure."""

    total: int
    ok: int
    api_errors: int
    parse_errors: int
    internal_errors: int
    skipped: int
    failures: tuple[AttemptFailure, ...]

    @property
    def failed(self) -> int:
        """Attempts that ended in an API, parse or internal error."""
        return self.api_errors + self.parse_errors + self.internal_errors

    @property
    def complete(self) -> bool:
        """True when no attempt failed."""
        return self.failed == 0

    def summary(self) -> str:
        """One line, e.g. '8 of 10 attempts ok; 2 failed (api 2, parse 0, ...)'."""
        line = (
            f"{self.ok} of {self.total} attempts ok; {self.failed} failed "
            f"(api {self.api_errors}, parse {self.parse_errors}, "
            f"internal {self.internal_errors})"
        )
        if self.skipped:
            line += f"; {self.skipped} skipped"
        return line

    def as_dict(self) -> dict[str, int | bool]:
        """JSON-ready counts, as written to reports."""
        return {
            "complete": self.complete,
            "attempts": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "api_errors": self.api_errors,
            "parse_errors": self.parse_errors,
            "internal_errors": self.internal_errors,
            "skipped": self.skipped,
        }

    @classmethod
    def from_attempts(cls, attempts: Iterable[Attempt]) -> RunCoverage:
        """Tally the outcome of each attempt in a finished run."""
        attempts = list(attempts)
        failures = tuple(
            AttemptFailure(
                source=a.source or "unknown",
                status=a.status,
                message=_failure_message(a),
            )
            for a in attempts
            if a.status in FAILED_STATUSES
        )
        return cls(
            total=len(attempts),
            ok=sum(a.status == "ok" for a in attempts),
            api_errors=sum(a.status == "api_error" for a in attempts),
            parse_errors=sum(a.status == "parse_error" for a in attempts),
            internal_errors=sum(a.status == "internal_error" for a in attempts),
            skipped=sum(a.status == "skipped" for a in attempts),
            failures=failures,
        )


def _failure_message(attempt: Attempt) -> str:
    if attempt.generation is not None and attempt.generation.error:
        return attempt.generation.error
    if attempt.detector_notes:
        return "; ".join(attempt.detector_notes)
    return attempt.status
