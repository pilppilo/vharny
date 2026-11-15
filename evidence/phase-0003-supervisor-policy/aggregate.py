"""Recompute the EXP-0001 aggregate from the retained run journals."""

from __future__ import annotations

import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
ROOT = BUNDLE.parents[1]
sys.path.insert(0, str(ROOT))

from tools.exp_0001_supervisor_policy import write_aggregate  # noqa: E402

if __name__ == "__main__":
    write_aggregate(BUNDLE)
