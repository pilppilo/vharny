"""Command line for the attempt note renderer.

This entry point parses arguments and calls ``vharness.agent.runview_note``.
"""

from __future__ import annotations

import sys

from vharness.agent.runview_note import main

if __name__ == "__main__":
    sys.exit(main())
