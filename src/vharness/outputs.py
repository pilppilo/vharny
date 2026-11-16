"""Opening report and log files without writing through planted symlinks.

Default output names (``report.md``, ``scan_log.jsonl``, ...) land in the
current directory, which may be an untrusted repository that ships those names
as symlinks. Default paths are therefore opened with ``O_NOFOLLOW``; a path the
operator named explicitly may still be a symlink on purpose.
"""

from __future__ import annotations

import errno
import os
from typing import TextIO

#: CLI fields naming output files; those not given explicitly are defaults.
OUTPUT_FIELDS = (
    "log_file",
    "out",
    "sarif_out",
    "markdown_out",
    "json_out",
    "metrics_out",
)


class OutputPathError(RuntimeError):
    """A default output path is a symlink and was not written through."""


def refusal(path: str) -> str:
    """The error text for a default output path that is a symlink."""
    return (
        f"refusing to write {path}: it is a symbolic link, and default output "
        "paths are never written through symlinks (it may have been planted by "
        "the scanned repository); remove it or name an output path explicitly"
    )


def open_output(path: str, mode: str, *, follow_symlinks: bool) -> TextIO:
    """Open ``path`` for writing (``"w"``) or appending (``"a"``) as UTF-8 text.

    With ``follow_symlinks=False`` a symlink at ``path`` raises
    ``OutputPathError`` instead of being written through.
    """
    if mode not in ("w", "a"):
        raise ValueError(f"unsupported output mode {mode!r}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if follow_symlinks:
        return open(path, mode, encoding="utf-8")  # pylint: disable=consider-using-with
    if os.path.islink(path):
        raise OutputPathError(refusal(path))
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_APPEND if mode == "a" else os.O_TRUNC
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # replaced by a symlink after the check
            raise OutputPathError(refusal(path)) from exc
        raise
    return os.fdopen(fd, mode, encoding="utf-8")
