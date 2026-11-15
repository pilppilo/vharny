"""Read-only artifact index over one reviewed run directory.

The index maps each stored artifact digest to the proposal that produced it,
the action that carried it, and the receipt that recorded it. It also reads
each stored summary to list the output chunks that summary references, so the
bodies behind one action's output are reachable by digest. Bodies are read
only when the index needs a summary, or when the operator asks for one, and
the bytes returned are verified against the digest that addressed them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .codec import MAX_JSON_BYTES, parse_json_object
from .errors import ContractError, IntegrityError
from .runview import show_run
from .runview_cli import run_command

# RunDirectory.artifacts_path. The name is local so this module keeps the same
# POSIX-free import surface as the reviewer.
_ARTIFACTS_NAME = "artifacts"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def index_run(root: str | Path) -> dict[str, object]:
    """Return one digest-keyed row per receipt that stored an artifact."""
    directory = Path(root)
    view = show_run(directory)
    recorded = view.get("actions")
    rows: list[dict[str, object]] = []
    if isinstance(recorded, Sequence):
        for action in recorded:
            if not isinstance(action, Mapping):
                continue
            digest = action.get("artifact_digest")
            if not isinstance(digest, str):
                continue
            if not _DIGEST.fullmatch(digest):
                raise IntegrityError(
                    f"journal recorded an invalid artifact digest: {digest!r}"
                )
            path = _artifact_path(directory, digest)
            available = path.is_file()
            rows.append(
                {
                    "digest": digest,
                    "proposal_id": action.get("proposal_id"),
                    "request_id": action.get("request_id"),
                    "action_name": action.get("action_name"),
                    "action_sequence": action.get("sequence"),
                    "receipt_status": action.get("receipt_status"),
                    "receipt_sequence": action.get("receipt_sequence"),
                    "available": available,
                    "size": path.stat().st_size if available else None,
                }
            )
    return {
        "directory": str(directory),
        "artifacts": rows,
        "outputs": _outputs(directory, rows),
    }


def read_artifact(root: str | Path, digest: str) -> bytes:
    """Read and verify one stored artifact body."""
    path = _artifact_path(Path(root), digest)
    if not path.is_file():
        raise IntegrityError(f"artifact is not stored: {digest}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"could not read artifact {digest}: {exc}") from exc
    if len(data) > MAX_JSON_BYTES:
        raise IntegrityError(f"artifact exceeds the supported byte bound: {digest}")
    if hashlib.sha256(data).hexdigest() != digest:
        raise IntegrityError(f"artifact verification failed: {digest}")
    return data


def _outputs(
    directory: Path, summaries: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Collect the output chunks each stored summary references."""
    collected: list[dict[str, object]] = []
    for summary in summaries:
        digest = summary.get("digest")
        if summary.get("available") is not True or not isinstance(digest, str):
            continue
        payload = _summary_payload(read_artifact(directory, digest))
        if payload is None:
            continue
        collected.extend(_chunk_entries(directory, digest, payload))
    return collected


def _summary_payload(body: bytes) -> Mapping[str, object] | None:
    """Decode an execution summary; other artifacts are not summaries."""
    try:
        return parse_json_object(body)
    except ContractError:
        return None


def _chunk_entries(
    directory: Path, summary_digest: str, summary: Mapping[str, object]
) -> list[dict[str, object]]:
    """Return one row per output chunk a summary references."""
    recorded = summary.get("output_artifacts")
    if isinstance(recorded, (str, bytes)) or not isinstance(recorded, Sequence):
        return []
    rows: list[dict[str, object]] = []
    for entry in recorded:
        if not isinstance(entry, Mapping):
            continue
        digest = _reference_digest(entry.get("artifact"))
        if digest is None:
            continue
        path = _artifact_path(directory, digest)
        available = path.is_file()
        rows.append(
            {
                "digest": digest,
                "summary_digest": summary_digest,
                "stream": _string_value(entry.get("stream")),
                "offset": _int_value(entry.get("offset")),
                "length": _int_value(entry.get("length")),
                "media_type": _reference_text(entry.get("artifact"), "media_type"),
                "available": available,
                "size": path.stat().st_size if available else None,
            }
        )
    return rows


def _reference_digest(reference: object) -> str | None:
    """Return the digest a stored artifact reference carries."""
    if isinstance(reference, str):
        candidate: object = reference
    else:
        candidate = _reference_text(reference, "digest")
    if isinstance(candidate, str) and _DIGEST.fullmatch(candidate):
        return candidate
    return None


def _reference_text(reference: object, name: str) -> str | None:
    """Return one text field from a stored artifact reference."""
    if not isinstance(reference, Mapping):
        return None
    value = reference.get(name)
    return value if isinstance(value, str) else None


def _string_value(value: object) -> str | None:
    """Return a string value or an explicit absence."""
    return value if isinstance(value, str) else None


def _int_value(value: object) -> int | None:
    """Return an integer value or an explicit absence."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def format_index(index: Mapping[str, object]) -> str:
    """Render the receipt artifacts and their output chunks."""
    directory = index.get("directory")
    lines = [f"artifacts: {directory if isinstance(directory, str) else 'unknown'}"]
    recorded = index.get("artifacts")
    rows = recorded if isinstance(recorded, Sequence) else ()
    entries = [row for row in rows if isinstance(row, Mapping)]
    if not entries:
        lines.append("no artifacts recorded")
    for row in entries:
        digest = row.get("digest")
        size = row.get("size")
        state = f"{size} bytes" if isinstance(size, int) else "missing"
        lines.append(
            f"- {digest}  {row.get('action_name')}  "
            f"{row.get('receipt_status')}  {row.get('proposal_id')}  {state}"
        )
    outputs = index.get("outputs")
    collected = outputs if isinstance(outputs, Sequence) else ()
    chunks = [row for row in collected if isinstance(row, Mapping)]
    lines.append("")
    if not chunks:
        lines.append("outputs: none recorded")
        return "\n".join(lines) + "\n"
    lines.append(f"outputs: {len(chunks)}")
    for row in chunks:
        size = row.get("size")
        state = f"{size} bytes" if isinstance(size, int) else "missing"
        lines.append(
            f"- {row.get('digest')}  {row.get('stream')} "
            f"@{row.get('offset')}+{row.get('length')}  {state}"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the artifact index command line and return a process status."""
    return run_command(_parser(), argv, _execute)


def _execute(args: argparse.Namespace) -> None:
    """Run the parsed artifact index command."""
    if args.command == "index":
        index = index_run(args.run_dir)
        if args.as_json:
            sys.stdout.write(json.dumps(index, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(format_index(index))
        return
    body = read_artifact(args.run_dir, args.digest)
    if args.output is None:
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()
    else:
        Path(args.output).write_bytes(body)


def _parser() -> argparse.ArgumentParser:
    """Build the artifact index argument parser."""
    parser = argparse.ArgumentParser(
        prog="runview-artifacts",
        description="Index or fetch the artifacts one run recorded.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="list the recorded artifacts")
    index.add_argument("run_dir")
    index.add_argument("--json", action="store_true", dest="as_json")
    body = commands.add_parser("body", help="write one verified artifact body")
    body.add_argument("run_dir")
    body.add_argument("digest")
    body.add_argument("--output", "-o")
    return parser


def _artifact_path(root: Path, digest: str) -> Path:
    """Return the content-addressed path for one validated digest."""
    if not _DIGEST.fullmatch(digest):
        raise ContractError("artifact digest must be a sha-256 hex digest")
    return root / _ARTIFACTS_NAME / digest[:2] / digest[2:]
