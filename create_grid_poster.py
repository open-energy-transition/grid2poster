#!/usr/bin/env python3
"""Backwards-compatible entry point for ``python create_grid_poster.py ...``.

The implementation moved to the ``grid2poster`` package under ``src/``; this
shim keeps older commands, scripts, and README snippets working. It runs
straight from a checkout, installed or not.

Prefer the ``grid2poster`` command (or ``python -m grid2poster``).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from grid2poster.cli import run

if __name__ == "__main__":
    print(
        "note: create_grid_poster.py is deprecated - use the 'grid2poster' command instead.",
        file=sys.stderr,
    )
    run()
