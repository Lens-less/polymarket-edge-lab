#!/usr/bin/env python3
"""CLI wrapper for the v0.7 prospective shadow refresh."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main(argv: Sequence[str] | None = None) -> int:
    from src.edge_lab.btc_twap_relative_value_v07_shadow import main as _main

    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
