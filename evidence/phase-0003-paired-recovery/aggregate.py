"""Recompute the paired-recovery aggregate from the retained runs."""

from __future__ import annotations

import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
ROOT = BUNDLE.parents[1]
sys.path.insert(0, str(ROOT))

from tools.phase3_paired_recovery import write_aggregate  # noqa: E402

if __name__ == "__main__":
    write_aggregate(BUNDLE)
