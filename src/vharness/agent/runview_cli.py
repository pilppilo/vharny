"""Shared argument and error boundary for the offline runview commands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from .errors import AgentError


def run_command(
    parser: argparse.ArgumentParser,
    argv: Sequence[str] | None,
    execute: Callable[[argparse.Namespace], None],
) -> int:
    """Parse one command line, run its body, and return a process status."""
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else int(code)
    try:
        execute(args)
    except (AgentError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
