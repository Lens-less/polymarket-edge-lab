#!/usr/bin/env python3
"""Build the Gate-0 perfect-information executable upper-bound diagnostic.

The builder is deliberately hindsight-only and non-promotional.  It reuses the
strict v0.7 capture loader so every quote comes from causal decision-time books,
captured fee/rule metadata, and verified common-terminal labels.  By default it
scans every integer second in the live 5m window and retains one best point per
common expiry; a fixed tau remains available only for legacy reproduction.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.build_btc_twap_relative_value_v07_counterfactual as v07_builder  # noqa: E402
from src.edge_lab.btc_twap_pair_pricing import (  # noqa: E402
    PairExecutionMode,
    PairPricingPolicy,
)
from src.edge_lab.btc_twap_relative_value_readiness import (  # noqa: E402
    PerfectInformationAttempt,
    evaluate_perfect_information_upper_bound,
    validate_structural_floor,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402

REPORT_SCHEMA = "btc-5m-15m-gate-0-executable-upper-bound-report.v2"
ZERO = Decimal(0)
ONE = Decimal(1)
MINIMUM_AVERAGE_PNL_PER_EXPIRY = Decimal("0.5")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _structural_token_ids(context: Any) -> tuple[str, str]:
    state = context.settlement_state
    pair = context.pair
    if state.strike_5 > state.strike_15:
        return pair.market_15.up_token_id, pair.market_5.down_token_id
    if state.strike_5 < state.strike_15:
        return pair.market_5.up_token_id, pair.market_15.down_token_id
    return pair.market_15.up_token_id, pair.market_5.down_token_id


def _implicit_split_probability(
    context: Any,
    books: Mapping[str, Any],
) -> Decimal | None:
    first_token_id, second_token_id = _structural_token_ids(context)
    selected = (books.get(first_token_id), books.get(second_token_id))
    if any(
        book is None or book.best_bid is None or book.best_ask is None
        for book in selected
    ):
        return None
    first, second = selected
    return (
        (first.best_bid + first.best_ask) / Decimal(2)
        + (second.best_bid + second.best_ask) / Decimal(2)
        - ONE
    )


def _execution_mode_diagnostics(
    *,
    observations_by_expiry: Mapping[str, Sequence[tuple[Any, Mapping[str, Any]]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    mode_documents: dict[str, Any] = {}
    for mode in PairExecutionMode:
        attempts: list[dict[str, Any]] = []
        aggregate = ZERO
        for expiry_id, observations in sorted(observations_by_expiry.items()):
            ranked: list[tuple[Decimal, Decimal, int, Any]] = []
            for context, books in observations:
                verdict = validate_structural_floor(
                    pair=context.pair,
                    settlement_state=context.settlement_state,
                    books=books,
                    pricing_policy=PairPricingPolicy(
                        pair_risk_usdc=None,
                        structural_only=True,
                    ),
                    execution_mode=mode,
                )
                selected = verdict.selected_level
                if selected is None:
                    continue
                executable_total = max(ZERO, selected.guaranteed_total_pnl)
                ranked.append(
                    (
                        executable_total,
                        selected.structural_net_floor_per_pair,
                        context.decision_tau_seconds,
                        selected,
                    )
                )
            if not ranked:
                attempts.append(
                    {
                        "attempt_id": expiry_id,
                        "observation_count": len(observations),
                        "best_ttc_seconds": None,
                        "best_net_floor_per_pair": "0",
                        "best_total_pnl": "0",
                        "selected_level": None,
                    }
                )
                continue
            executable_total, net_floor, best_ttc, selected = max(
                ranked,
                key=lambda item: (item[0], item[1], -item[2]),
            )
            aggregate += executable_total
            attempts.append(
                {
                    "attempt_id": expiry_id,
                    "observation_count": len(observations),
                    "best_ttc_seconds": best_ttc,
                    "best_net_floor_per_pair": str(net_floor),
                    "best_total_pnl": str(executable_total),
                    "selected_level": selected.to_document(),
                }
            )
        attempt_count = len(attempts)
        average = ZERO if attempt_count == 0 else aggregate / attempt_count
        gate_passed = (
            aggregate > ZERO
            and average >= MINIMUM_AVERAGE_PNL_PER_EXPIRY
        )
        mode_documents[mode.value] = {
            "execution_mode": mode.value,
            "attempt_count": attempt_count,
            "attempts": attempts,
            "aggregate_best_total_pnl": str(aggregate),
            "average_best_total_pnl_per_expiry": str(average),
            "positive_expiry_count": sum(
                Decimal(item["best_total_pnl"]) > ZERO for item in attempts
            ),
            "minimum_average_pnl_per_expiry": str(
                MINIMUM_AVERAGE_PNL_PER_EXPIRY
            ),
            "gate_0_passed": gate_passed,
            "stop_recommended": not gate_passed,
            "fill_assumption": (
                "decision_time_executable"
                if mode is PairExecutionMode.TAKER_TAKER
                else "hypothetical_full_fill_requires_shadow_validation"
            ),
        }

    curves: list[dict[str, Any]] = []
    negative_count = 0
    observation_count = 0
    for expiry_id, observations in sorted(observations_by_expiry.items()):
        points: list[dict[str, Any]] = []
        for context, books in sorted(
            observations,
            key=lambda item: item[0].decision_tau_seconds,
            reverse=True,
        ):
            split_probability = _implicit_split_probability(context, books)
            if split_probability is None:
                continue
            observation_count += 1
            if split_probability < ZERO:
                negative_count += 1
            points.append(
                {
                    "ttc_seconds": context.decision_tau_seconds,
                    "b": str(split_probability),
                }
            )
        curves.append(
            {
                "attempt_id": expiry_id,
                "points": points,
                "negative_b_observation_count": sum(
                    Decimal(point["b"]) < ZERO for point in points
                ),
            }
        )
    split_diagnostics = {
        "definition": "sum_of_structural_leg_midprices_minus_one",
        "observation_count": observation_count,
        "negative_b_observation_count": negative_count,
        "curves": curves,
    }
    return mode_documents, split_diagnostics


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
    decision_tau_seconds: int | None,
    expected_clean_attempts: int,
) -> dict[str, Any]:
    if decision_tau_seconds is not None and (
        isinstance(decision_tau_seconds, bool) or decision_tau_seconds <= 0
    ):
        raise ValueError("decision_tau_seconds must be a positive integer")
    if isinstance(expected_clean_attempts, bool) or expected_clean_attempts <= 0:
        raise ValueError("expected_clean_attempts must be a positive integer")
    manifest_path = manifest_path.expanduser().resolve()
    contexts, strategy, preregistration_path = _load_contexts(manifest_path)
    pricing_policy = PairPricingPolicy(
        maximum_spread_each_leg=strategy.maximum_spread_each_leg,
        minimum_market_price=strategy.minimum_market_price,
        maximum_market_price=strategy.maximum_market_price,
        pair_risk_usdc=None,
        structural_only=True,
    )

    def _books_for_context(
        context: Any,
        *,
        required: bool = True,
    ) -> Mapping[str, Any] | None:
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
            if not required:
                return None
            raise ValueError(
                f"Gate-0 attempt {context.expiry_cluster_id} lacks four causal "
                "full-depth books"
            )
        return {
            token_id: signal[token_id].snapshot for token_id in token_ids
        }

    def _context_at_ttc(context: Any, ttc_seconds: int) -> Any:
        decision_at_ms = context.expiry_ms - ttc_seconds * 1_000
        try:
            return replace(
                context,
                decision_tau_seconds=ttc_seconds,
                decision_at_ms=decision_at_ms,
            )
        except TypeError:
            values = dict(vars(context))
            values.update(
                decision_tau_seconds=ttc_seconds,
                decision_at_ms=decision_at_ms,
            )
            return SimpleNamespace(**values)

    def _attempt_for_context(
        context: Any,
        *,
        unique_attempt_id: str,
    ) -> PerfectInformationAttempt:
        return PerfectInformationAttempt(
            attempt_id=unique_attempt_id,
            pair=context.pair,
            settlement_state=context.settlement_state,
            books=_books_for_context(context, required=False) or {},
            actual_5_up=context.actual_5_up,
            actual_15_up=context.actual_15_up,
            pricing_policy=pricing_policy,
            execution_mode=PairExecutionMode.MAKER_MAKER,
        )

    if decision_tau_seconds is None:
        selected_by_expiry: dict[str, tuple[Any, Any]] = {}
        for context in contexts:
            provisional = _attempt_for_context(
                context,
                unique_attempt_id=(
                    f"{context.expiry_cluster_id}:tau{context.decision_tau_seconds}"
                ),
            )
            provisional_report = evaluate_perfect_information_upper_bound((provisional,))
            provisional_attempt = provisional_report.attempts[0]
            ranking = (
                provisional_attempt.best_total_pnl,
                provisional_attempt.unrestricted_best_total_pnl,
                -context.decision_tau_seconds,
            )
            prior = selected_by_expiry.get(context.expiry_cluster_id)
            if prior is None or ranking > prior[1]:
                selected_by_expiry[context.expiry_cluster_id] = (context, ranking)
        selected = tuple(
            entry[0]
            for entry in sorted(
                selected_by_expiry.values(),
                key=lambda item: item[0].expiry_ms,
            )
        )
    else:
        selected = tuple(
            context
            for context in contexts
            if context.decision_tau_seconds == decision_tau_seconds
        )

    if len(selected) != expected_clean_attempts:
        tau_label = "all taus" if decision_tau_seconds is None else f"tau {decision_tau_seconds}"
        raise ValueError(
            "Gate-0 input count mismatch: "
            f"expected {expected_clean_attempts} clean common expiries at {tau_label}, "
            f"observed {len(selected)}"
        )
    expiry_ids = [context.expiry_cluster_id for context in selected]
    if len(set(expiry_ids)) != len(expiry_ids):
        raise ValueError("Gate-0 input repeats a common expiry")

    attempts = tuple(
        replace(
            _attempt_for_context(
                context,
                unique_attempt_id=context.expiry_cluster_id,
            ),
            attempt_id=context.expiry_cluster_id,
        )
        for context in sorted(selected, key=lambda item: item.expiry_ms)
    )
    diagnostic = evaluate_perfect_information_upper_bound(tuple(attempts))
    contexts_by_expiry: defaultdict[str, list[Any]] = defaultdict(list)
    for context in contexts:
        contexts_by_expiry[context.expiry_cluster_id].append(context)
    observation_slots: defaultdict[
        str, dict[int, tuple[Any, Mapping[str, Any]]]
    ] = defaultdict(dict)
    if decision_tau_seconds is None:
        for expiry_id, expiry_contexts in sorted(contexts_by_expiry.items()):
            representative = max(
                expiry_contexts,
                key=lambda item: item.decision_tau_seconds,
            )
            for ttc_seconds in range(300, -1, -1):
                scanned_context = _context_at_ttc(representative, ttc_seconds)
                books = _books_for_context(scanned_context, required=False)
                if books is not None:
                    observation_slots[expiry_id][ttc_seconds] = (
                        scanned_context,
                        books,
                    )
            # Preserve the exact preregistered contexts at their timestamps.
            # Real contexts share one replay; this also prevents a synthetic
            # fixture from hiding a distinct captured book at the same tau.
            for context in expiry_contexts:
                books = _books_for_context(context, required=False)
                if books is not None:
                    observation_slots[expiry_id][context.decision_tau_seconds] = (
                        context,
                        books,
                    )
    else:
        for context in selected:
            books = _books_for_context(context)
            assert books is not None
            observation_slots[context.expiry_cluster_id][
                context.decision_tau_seconds
            ] = (context, books)
    observations_by_expiry = {
        expiry_id: tuple(
            observation_slots[expiry_id][ttc_seconds]
            for ttc_seconds in sorted(observation_slots[expiry_id], reverse=True)
        )
        for expiry_id in sorted(contexts_by_expiry)
    }
    execution_modes, split_probability_diagnostics = (
        _execution_mode_diagnostics(
            observations_by_expiry=observations_by_expiry,
        )
    )
    maker_maker_gate = execution_modes[PairExecutionMode.MAKER_MAKER.value]
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "preregistration_path": str(preregistration_path),
        "preregistration_sha256": _sha256(preregistration_path),
        "decision_tau_seconds": decision_tau_seconds,
        "expected_clean_attempts": expected_clean_attempts,
        "observed_unique_common_expiry_attempts": len(selected),
        "selected_decision_tau_seconds_by_expiry": {
            str(attempt["attempt_id"]): attempt["best_ttc_seconds"]
            for attempt in maker_maker_gate["attempts"]
        },
        "diagnostic": diagnostic.to_document(),
        "execution_modes": execution_modes,
        "split_probability_diagnostics": split_probability_diagnostics,
        "scan": {
            "ttc_seconds_start": (
                300 if decision_tau_seconds is None else decision_tau_seconds
            ),
            "ttc_seconds_end": (
                0 if decision_tau_seconds is None else decision_tau_seconds
            ),
            "candidate_ttc_count": (
                301 if decision_tau_seconds is None else 1
            ),
            "observed_complete_point_count": sum(
                len(items) for items in observations_by_expiry.values()
            ),
            "sampling": (
                "every_integer_second_in_5m_live_window"
                if decision_tau_seconds is None
                else "legacy_fixed_tau_override"
            ),
        },
        "gate_0_passed": maker_maker_gate["gate_0_passed"],
        "stop_recommended": maker_maker_gate["stop_recommended"],
        "policy": {
            "gate": 0,
            "perfect_information": True,
            "decision_time_executable_full_depth": True,
            "quantity_risk_cap_usdc": None,
            "quantity_scope": "all_captured_joint_depth_breakpoints",
            "route": "structural_floor_only",
            "decision_route": PairExecutionMode.MAKER_MAKER.value,
            "minimum_average_pnl_per_expiry": str(
                MINIMUM_AVERAGE_PNL_PER_EXPIRY
            ),
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
    parser.add_argument(
        "--decision-tau-seconds",
        type=int,
        default=None,
        help=(
            "Legacy fixed-tau override. Omit to scan every second from "
            "300 seconds to common expiry."
        ),
    )
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
