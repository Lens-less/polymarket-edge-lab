#!/usr/bin/env python3
"""Evaluate the continuous official RTDS status and emit a health snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_continuous_rtds import (
    evaluate_continuous_rtds_health,
)
from src.edge_lab.btc_twap_relative_value_service import (
    write_json_document,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        value = json.loads(
            args.status.expanduser().resolve().read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        status = None
    else:
        status = value if isinstance(value, dict) else None
    result = evaluate_continuous_rtds_health(
        status,
        now=datetime.now(timezone.utc),
    )
    write_json_document(args.output.expanduser().resolve(), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
