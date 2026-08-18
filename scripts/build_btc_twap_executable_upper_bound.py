#!/usr/bin/env python3
"""Build the Gate-0 perfect-information executable upper-bound diagnostic.

The builder is deliberately hindsight-only and non-promotional.  It reuses the
strict v0.7 capture loader so every quote comes from causal decision-time books,
captured fee/rule metadata, and verified common-terminal labels.  One fixed tau
is selected per common expiry; tau aliases can never inflate the attempt count.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.build_btc_twap_relative_value_v07_counterfactual as v07_builder  # noqa: E402
from src.edge_lab.btc_twap_pair_pricing import PairPricingPolicy  # noqa: E402
from src.edge_lab.btc_twap_relative_value_readiness import (  # noqa: E402
    PerfectInformationAttempt,
    evaluate_perfect_information_upper_bound,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402

REPORT_SCHEMA = "btc-5m-15m-gate-0-executable-upper-bound-report.v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_contexts(manifest_path: Path) -> tuple[tuple[Any, ...], Any, Path]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = v07_builder._load_json(manifest_path, label="counterfactual manifest")
    v07_builder._assert_exact_keys(
        manifest,
        allowed=v07_builder._MANIFEST_KEYS,
        required=v07_builder._REQUIRED_MANIFEST_KEYS,
        label="counterfactual manifest",
    )
    if manifest.get("schema_version") != v07_builder.MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported counterfactual manifest schema")
    v07_builder._confirm_paper_only_boundary(manifest)
    manifest_dir = manifest_path.parent
    preregistration_path = v07_builder._path(
        manifest_dir,
        manifest.get("preregistration_path"),
        label="preregistration_path",
    )
    preregistration = v07_builder._load_json(
        preregistration_path,
        label="v0.7 preregistration",
    )
    model, strategy, _weights, _sensitivity = v07_builder._verify_preregistration(
        preregistration,
        preregistration_path=preregistration_path,
    )
    cases = manifest.get("cases")
    if (
        not isinstance(cases, list)
        or not cases
        or any(not isinstance(case, Mapping) for case in cases)
    ):
        raise ValueError("counterfactual manifest cases must be non-empty objects")
    contexts = tuple(
        context
        for case_index, case in enumerate(cases)
        for context in v07_builder._load_case_contexts(
            case,
            manifest_case_index=case_index,
            manifest_dir=manifest_dir,
            model=model,
            strategy=strategy,
        )
    )
    pair_by_expiry: dict[int, str] = {}
    case_by_expiry: dict[int, int] = {}
    for context in contexts:
        prior_pair = pair_by_expiry.setdefault(
            context.expiry_ms, context.canonical_pair_id
        )
        prior_case = case_by_expiry.setdefault(
            context.expiry_ms,
            context.manifest_case_index,
        )
        if (
            prior_pair != context.canonical_pair_id
            or prior_case != context.manifest_case_index
        ):
            raise ValueError(
                "Gate-0 manifest aliases one common expiry as multiple pairs or cases"
            )
    return contexts, strategy, preregistration_path


def build_upper_bound_report(
    *,
    manifest_path: Path,
    decision_tau_seconds: int,
    expected_clean_attempts: int,
) -> dict[str, Any]:
    if isinstance(decision_tau_seconds, bool) or decision_tau_seconds <= 0:
        raise ValueError("decision_tau_seconds must be a positive integer")
    if isinstance(expected_clean_attempts, bool) or expected_clean_attempts <= 0:
        raise ValueError("expected_clean_attempts must be a positive integer")
    manifest_path = manifest_path.expanduser().resolve()
    contexts, strategy, preregistration_path = _load_contexts(manifest_path)
    selected = tuple(
        context
        for context in contexts
        if context.decision_tau_seconds == decision_tau_seconds
    )
    if len(selected) != expected_clean_attempts:
        raise ValueError(
            "Gate-0 input count mismatch: "
            f"expected {expected_clean_attempts} clean common expiries at tau "
            f"{decision_tau_seconds}, observed {len(selected)}"
        )
    expiry_ids = [context.expiry_cluster_id for context in selected]
    if len(set(expiry_ids)) != len(expiry_ids):
        raise ValueError("Gate-0 input repeats a common expiry")

    attempts: list[PerfectInformationAttempt] = []
    pricing_policy = PairPricingPolicy(
        maximum_spread_each_leg=strategy.maximum_spread_each_leg,
        minimum_market_price=strategy.minimum_market_price,
        maximum_market_price=strategy.maximum_market_price,
        pair_risk_usdc=None,
    )
    for context in sorted(selected, key=lambda item: item.expiry_ms):
        token_ids = (
            context.pair.market_5.up_token_id,
            context.pair.market_5.down_token_id,
            context.pair.market_15.up_token_id,
            context.pair.market_15.down_token_id,
        )
        signal = context.replay.signal_books(
            token_ids=token_ids,
            decision_at_ms=context.decision_at_ms,
            maximum_age_ms=strategy.maximum_book_staleness_ms,
            require_full_depth=True,
        )
        if set(signal) != set(token_ids):
            raise ValueError(
                f"Gate-0 attempt {context.expiry_cluster_id} lacks four causal "
                "full-depth books"
            )
        attempts.append(
            PerfectInformationAttempt(
                attempt_id=context.expiry_cluster_id,
                pair=context.pair,
                settlement_state=context.settlement_state,
                books={token_id: signal[token_id].snapshot for token_id in token_ids},
                actual_5_up=context.actual_5_up,
                actual_15_up=context.actual_15_up,
                pricing_policy=pricing_policy,
            )
        )

    diagnostic = evaluate_perfect_information_upper_bound(tuple(attempts))
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "preregistration_path": str(preregistration_path),
        "preregistration_sha256": _sha256(preregistration_path),
        "decision_tau_seconds": decision_tau_seconds,
        "expected_clean_attempts": expected_clean_attempts,
        "observed_unique_common_expiry_attempts": len(selected),
        "diagnostic": diagnostic.to_document(),
        "policy": {
            "gate": 0,
            "perfect_information": True,
            "decision_time_executable_full_depth": True,
            "quantity_risk_cap_usdc": None,
            "quantity_scope": "all_captured_joint_depth_breakpoints",
            "route": "structural_floor_only",
            "dynamic_captured_fees_and_rounding": True,
            "no_trade_available": True,
            "non_positive_aggregate_upper_bound_action": "stop_route",
            "counts_as_locked_oos_evidence": False,
            "can_authorize_live": False,
        },
        "safety": {
            "paper_only": True,
            "network_calls_made": 0,
            "authenticated_endpoints_used": 0,
            "orders_submitted": 0,
        },
    }
    report["report_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    return report


def _write_report(path: Path, document: Mapping[str, Any]) -> None:
    v07_builder._atomic_write_json(path.expanduser().resolve(), document)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--decision-tau-seconds", type=int, required=True)
    parser.add_argument("--expected-clean-attempts", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_upper_bound_report(
        manifest_path=args.manifest,
        decision_tau_seconds=args.decision_tau_seconds,
        expected_clean_attempts=args.expected_clean_attempts,
    )
    _write_report(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
