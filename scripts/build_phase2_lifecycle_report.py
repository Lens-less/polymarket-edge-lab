#!/usr/bin/env python3
"""Build a finalized-only Phase 2 lifecycle completeness report."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal

from src.edge_lab.compatibility import (
    LiveExecutionBlocked,
    assert_new_orders_disabled,
)
from src.edge_lab.phase2_lifecycle_audit import (
    audit_phase2_lifecycle,
    write_phase2_lifecycle_report,
)


def _guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the earliest committed prospective short-crypto cohort "
            "when present, retain the global-first baseline, and use "
            "finalized public capture evidence only"
        )
    )
    parser.add_argument(
        "--dynamic-run-root",
        action="append",
        required=True,
        help="Finalized/recovery dynamic run root; repeat as needed",
    )
    parser.add_argument("--main-capture-root", required=True)
    parser.add_argument(
        "--as-of-ms",
        required=True,
        type=int,
        help="Deterministic audit cutoff in Unix epoch milliseconds",
    )
    parser.add_argument("--output-base", required=True)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument(
        "--settlement-timeout-ms",
        type=int,
        default=3_600_000,
    )
    parser.add_argument("--threshold", default="0.8")
    return parser


def main(argv: list[str] | None = None) -> int:
    _guard()
    args = _parser().parse_args(argv)
    report = audit_phase2_lifecycle(
        args.dynamic_run_root,
        main_capture_root=args.main_capture_root,
        as_of_ms=args.as_of_ms,
        sample_size=args.sample_size,
        settlement_timeout_ms=args.settlement_timeout_ms,
        threshold=Decimal(args.threshold),
    )
    report_path = write_phase2_lifecycle_report(
        report,
        output_base=args.output_base,
    )
    root_commitment = report["cohort_commitment"]
    active_commitment = report["active_cohort_commitment"]
    result = {
        "schema_version": (
            "edge-lab.phase2-lifecycle-completeness-cli.v3"
        ),
        "report_id": report["report_id"],
        "report_path": str(report_path),
        "cohort_mode": report["cohort_mode"],
        "root_cohort_commitment_record_id": (
            None
            if root_commitment is None
            else root_commitment["record_id"]
        ),
        "root_cohort_eligibility_start_ms": (
            None
            if root_commitment is None
            else root_commitment["eligibility_start_ms"]
        ),
        "active_cohort_commitment_record_id": (
            None
            if active_commitment is None
            else active_commitment["record_id"]
        ),
        "active_cohort_eligibility_start_ms": (
            None
            if active_commitment is None
            else active_commitment["eligibility_start_ms"]
        ),
        "active_cohort_index": report["active_cohort_index"],
        "prior_failed_cohort_count": report[
            "prior_failed_cohort_count"
        ],
        "mature_target_count": report["mature_target_count"],
        "sampled_target_count": report["sampled_target_count"],
        "complete_target_count": report["complete_target_count"],
        "completeness_ratio": report["completeness_ratio"],
        "gate_status": report["gate_status"],
        "historical_global_first_mature_gate_status": report[
            "historical_global_first_mature"
        ]["gate_status"],
        "network_requests": 0,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    gate_status = report["gate_status"]
    if gate_status in {
        "passed",
        "passed_after_remediation_with_retained_failures",
    }:
        return 0
    if gate_status in {
        "failed",
        "remediation_failed_with_retained_failures",
    }:
        return 2
    if gate_status in {
        "waiting_for_mature_targets",
        "waiting_for_remediation_mature_targets",
    }:
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
