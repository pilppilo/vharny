"""Command line for the offline run reviewer.

The review, artifact index, and attempt note modules stay acyclic; this entry
point is where their commands are composed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from vharness.agent.runview import main as review_main
from vharness.agent.runview_artifacts import main as artifacts_main
from vharness.agent.runview_note import main as note_main

_DELEGATED: dict[str, Callable[[Sequence[str] | None], int]] = {
    "artifacts": artifacts_main,
    "note": note_main,
}
_REVIEWED = frozenset({"show", "compare"})


def main(argv: Sequence[str] | None = None) -> int:
    """Run one composed runview command line and return a process status."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in _DELEGATED:
        return _DELEGATED[raw[0]](raw[1:])
    if raw and raw[0] in _REVIEWED:
        return review_main(raw)
    return _help_or_error(raw)


def _help_or_error(argv: Sequence[str]) -> int:
    """Print the composed command help or the usual argument error."""
    try:
        _parser().parse_args(list(argv))
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else int(code)
    return 0


def _parser() -> argparse.ArgumentParser:
    """Build the composed top-level parser."""
    parser = argparse.ArgumentParser(
        prog="runview",
        description="Review an operator run from its journal and run.json.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    summaries = (
        ("show", "print one run review"),
        ("compare", "compare run directories"),
        ("artifacts", "index or fetch recorded artifacts"),
        ("note", "render an attempt note from runview --json"),
    )
    for name, summary in summaries:
        commands.add_parser(name, help=summary, add_help=False)
    return parser


if __name__ == "__main__":
    sys.exit(main())
