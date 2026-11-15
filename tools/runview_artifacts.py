"""Command line for the offline artifact index.

This entry point parses arguments and calls
``vharness.agent.runview_artifacts``.
"""

from __future__ import annotations

import sys

from vharness.agent.runview_artifacts import main

if __name__ == "__main__":
    sys.exit(main())
