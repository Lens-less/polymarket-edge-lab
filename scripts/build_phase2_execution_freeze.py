#!/usr/bin/env python3
"""Discover, seal, and optionally promote one real Phase 2 replay candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.edge_lab.compatibility import (
    LiveExecutionBlocked,
    assert_new_orders_disabled,
)
from src.edge_lab.execution_replay_assembly import (
    build_replay_request_for_candidate,
    discover_replay_candidates,
)
from src.edge_lab.execution_replay_promotion import (
    verify_and_promote_execution_replay,
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
            "Offline discovery and sealing of finalized dynamic short-crypto "
            "execution-replay evidence"
        )
    )
    parser.add_argument(
        "--dynamic-run-root",
        action="append",
        required=True,
        help="Closed/current unique dynamic run root; repeat as needed",
    )
    parser.add_argument("--main-capture-root", required=True)
    parser.add_argument("--request-base", required=True)
    parser.add_argument(
        "--slug",
        help="Exact candidate slug to seal; omit for read-only listing",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Run the strict offline promoter after sealing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _guard()
    args = _parser().parse_args(argv)
    candidates = discover_replay_candidates(
        args.dynamic_run_root,
        main_capture_root=args.main_capture_root,
    )
    result: dict[str, Any] = {
        "schema_version": "edge-lab.phase2-replay-assembly-cli.v1",
        "candidate_count": len(candidates),
        "candidates": [
            {
                "slug": candidate.slug,
                "condition_id": candidate.condition_id,
                "opens_at_ms": candidate.opens_at_ms,
                "closes_at_ms": candidate.closes_at_ms,
                "group_id": candidate.group_id,
                "dynamic_run_root": str(candidate.dynamic_run_root),
                "source_manifest_count": len(
                    candidate.source_manifest_paths
                ),
            }
            for candidate in candidates
        ],
        "network_requests": 0,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    if args.slug is None:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    selected = [
        candidate
        for candidate in candidates
        if candidate.slug == args.slug
    ]
    if len(selected) != 1:
        raise SystemExit(
            f"expected exactly one candidate for {args.slug}, found {len(selected)}"
        )
    built = build_replay_request_for_candidate(
        selected[0],
        request_base=Path(args.request_base),
    )
    result["sealed"] = {
        "slug": selected[0].slug,
        "request_path": str(built.request_path),
        "freeze_path": str(built.freeze_path),
        "freeze_id": built.freeze_id,
        "source_manifest_count": built.source_manifest_count,
        "source_record_count": built.source_record_count,
    }
    if args.promote:
        promoted = verify_and_promote_execution_replay(
            built.request_path
        )
        result["promotion"] = {
            "verified": promoted.verified,
            "promoted": promoted.promoted,
            "status": promoted.status,
            "production_evidence": promoted.production_evidence,
            "classification": promoted.classification,
            "run_id": promoted.run_id,
            "output_dir": (
                None
                if promoted.output_dir is None
                else str(promoted.output_dir)
            ),
            "reason_codes": list(promoted.reason_codes),
        }
        if not promoted.promoted:
            print(
                json.dumps(
                    result,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
