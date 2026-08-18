#!/usr/bin/env python3
"""Write or audit the rolling v0.8 locked-shadow receipt chain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_locked_shadow_v08 import (
    RollingCohortAdmission,
    RollingCohortOutcome,
    RollingDecisionReceipt,
    RollingInclusionPolicy,
    admit_rolling_cohort,
    append_rolling_decision,
    audit_rolling_shadow,
    finalize_rolling_cohort,
    initialize_rolling_shadow,
)
from src.edge_lab.data_store import canonical_json_bytes


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("receipt input must be a JSON object")
    value.pop("schema_version", None)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, argument in (
        ("init", "policy"),
        ("admit", "admission"),
        ("decision", "decision"),
        ("finalize", "outcome"),
    ):
        child = subparsers.add_parser(command)
        child.add_argument("--journal-root", type=Path, required=True)
        child.add_argument(f"--{argument}", type=Path, required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--journal-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        result = initialize_rolling_shadow(
            args.journal_root,
            RollingInclusionPolicy(**_document(args.policy)),
        )
    elif args.command == "admit":
        result = admit_rolling_cohort(
            args.journal_root,
            RollingCohortAdmission(**_document(args.admission)),
        )
    elif args.command == "decision":
        result = append_rolling_decision(
            args.journal_root,
            RollingDecisionReceipt(**_document(args.decision)),
        )
    elif args.command == "finalize":
        result = finalize_rolling_cohort(
            args.journal_root,
            RollingCohortOutcome(**_document(args.outcome)),
        )
    else:
        result = audit_rolling_shadow(args.journal_root)
    print(canonical_json_bytes(result).decode("utf-8"))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
