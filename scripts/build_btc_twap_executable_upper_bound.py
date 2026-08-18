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

import scripts.build_btc_twap_relative_value_v07_counterfactual as v07_builder
from src.edge_lab.btc_twap_pair_pricing import (
    PairExecutionMode,
    PairPricingPolicy,
)
from src.edge_lab.btc_twap_relative_value_readiness import (
    PerfectInformationAttempt,
    evaluate_perfect_information_upper_bound,
    validate_structural_floor,
)
from src.edge_lab.data_store import canonical_json_bytes

REPORT_SCHEMA = "btc-5m-15m-gate-0-executable-upper-bound-report.v3"
ZERO = Decimal(0)
ONE = Decimal(1)
MINIMUM_AVERAGE_PNL_PER_EXPIRY = Decimal("0.5")
V08_PREREGISTRATION_PATH = (
    PROJECT_ROOT
    / "research"
    / "btc_5m_15m_edge_readiness_v08_2026-08-18"
    / "PREREGISTRATION.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_gate_0_contract(
    preregistration_path: Path,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    document = v07_builder._load_json(
        preregistration_path,
        label="gate-0 preregistration",
    )
    if not isinstance(document, Mapping):
        raise TypeError("gate-0 preregistration must be a JSON object")
    gate_0 = document.get("gate_0")
    if not isinstance(gate_0, Mapping):
        if not strict:
            return {}
        raise ValueError("preregistration is missing gate_0 contract")
    report_contract = gate_0.get("existing_41_attempt_report")
    if not isinstance(report_contract, Mapping):
        if not strict:
            return {}
        raise ValueError("preregistration is missing Gate-0 report contract")
    expected_clean_attempts = report_contract.get(
        "expected_unique_common_expiry_attempts"
    )
    scan_window = report_contract.get("scan_ttc_seconds_inclusive")
    if (
        isinstance(expected_clean_attempts, bool)
        or not isinstance(expected_clean_attempts, int)
        or expected_clean_attempts <= 0
    ):
        if not strict:
            return {}
        raise ValueError("Gate-0 prereg expected attempt count is invalid")
    if (
        not isinstance(scan_window, list)
        or len(scan_window) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in scan_window
        )
    ):
        if not strict:
            return {}
        raise ValueError("Gate-0 prereg scan window is invalid")
    scan_start, scan_end = scan_window
    if scan_start < scan_end:
        if not strict:
            return {}
        raise ValueError("Gate-0 prereg scan window must descend to expiry")
    return {
        "track_id": document.get("track_id"),
        "preregistration_path": str(preregistration_path),
        "preregistration_sha256": _sha256(preregistration_path),
        "expected_clean_attempts": expected_clean_attempts,
        "scan_ttc_seconds_inclusive": [scan_start, scan_end],
        "candidate_ttc_count": scan_start - scan_end + 1,
        "decision_execution_mode": gate_0.get("decision_execution_mode"),
        "minimum_average_best_total_pnl_per_expiry_usdc": gate_0.get(
            "minimum_average_best_total_pnl_per_expiry_usdc"
        ),
        "incomplete_existing_41_evidence_action": gate_0.get(
            "incomplete_existing_41_evidence_action"
        ),
    }


def _structural_token_ids(context: Any) -> tuple[str, str]:
    state = context.settlement_state
    pair = context.pair
    if state.strike_5 > state.strike_15:
        return pair.market_15.up_token_id, pair.market_5.down_token_id
    if state.strike_5 < state.strike_15:
        return pair.market_5.up_token_id, pair.market_15.down_token_id
    return pair.market_15.up_token_id, pair.market_5.down_token_id


def _implicit_split_probability_diagnostic(
    context: Any,
    books: Mapping[str, Any],
) -> dict[str, Decimal | str | None]:
    first_token_id, second_token_id = _structural_token_ids(context)
    first = books.get(first_token_id)
    second = books.get(second_token_id)
    if first is None or second is None:
        return {
            "b": None,
            "bid_side_sum_minus_one": None,
            "ask_side_sum_minus_one": None,
            "unavailable_reason": "structural_book_missing",
        }
    bid_side = (
        None
        if first.best_bid is None or second.best_bid is None
        else first.best_bid + second.best_bid - ONE
    )
    ask_side = (
        None
        if first.best_ask is None or second.best_ask is None
        else first.best_ask + second.best_ask - ONE
    )
    midpoint = (
        None
        if bid_side is None or ask_side is None
        else (bid_side + ask_side) / Decimal(2)
    )
    return {
        "b": midpoint,
        "bid_side_sum_minus_one": bid_side,
        "ask_side_sum_minus_one": ask_side,
        "unavailable_reason": (
            None if midpoint is not None else "two_sided_midpoint_unavailable"
        ),
    }


def _execution_mode_diagnostics(
    *,
    observations_by_expiry: Mapping[
        str,
        Sequence[
            tuple[
                Any,
                Mapping[str, Any],
                Mapping[str, Decimal] | None,
                str | None,
            ]
        ],
    ],
    required_observation_count_per_expiry: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    incomplete_expiry_ids = tuple(
        expiry_id
        for expiry_id, observations in sorted(observations_by_expiry.items())
        if len(observations) != required_observation_count_per_expiry
    )
    mode_documents: dict[str, Any] = {}
    for mode in PairExecutionMode:
        attempts: list[dict[str, Any]] = []
        aggregate = ZERO
        missing_fill_cap_expiry_ids: list[str] = []
        for expiry_id, observations in sorted(observations_by_expiry.items()):
            ranked: list[tuple[Decimal, Decimal, int, Any, str | None]] = []
            if mode is not PairExecutionMode.TAKER_TAKER and any(
                maker_fill_caps is None for _context, _books, maker_fill_caps, _source in observations
            ):
                missing_fill_cap_expiry_ids.append(expiry_id)
            for context, books, maker_fill_caps, fill_cap_source in observations:
                verdict = validate_structural_floor(
                    pair=context.pair,
                    settlement_state=context.settlement_state,
                    books=books,
                    pricing_policy=PairPricingPolicy(
                        pair_risk_usdc=None,
                        structural_only=True,
                        maker_fill_cap_by_token_id=maker_fill_caps,
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
                        fill_cap_source,
                    )
                )
            if not ranked:
                fill_cap_source = (
                    "context.future_public_trades_by_token_id"
                    if observations
                    and all(
                        source == "context.future_public_trades_by_token_id"
                        and caps is not None
                        for _context, _books, caps, source in observations
                    )
                    else None
                )
                attempts.append(
                    {
                        "attempt_id": expiry_id,
                        "observation_count": len(observations),
                        "best_ttc_seconds": None,
                        "best_net_floor_per_pair": "0",
                        "best_total_pnl": "0",
                        "selected_level": None,
                        "fill_volume_bound_source": fill_cap_source,
                    }
                )
                continue
            executable_total, net_floor, best_ttc, selected, fill_cap_source = max(
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
                    "fill_volume_bound_source": fill_cap_source,
                }
            )
        attempt_count = len(attempts)
        average = ZERO if attempt_count == 0 else aggregate / attempt_count
        economic_gate_passed = (
            aggregate > ZERO and average >= MINIMUM_AVERAGE_PNL_PER_EXPIRY
        )
        evidence_complete = not incomplete_expiry_ids and not missing_fill_cap_expiry_ids
        gate_passed = economic_gate_passed and evidence_complete
        stop_recommended = evidence_complete and not economic_gate_passed
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
            "required_observation_count_per_expiry": (
                required_observation_count_per_expiry
            ),
            "incomplete_scan_expiry_ids": list(incomplete_expiry_ids),
            "missing_fill_volume_bound_expiry_ids": missing_fill_cap_expiry_ids,
            "evidence_complete": evidence_complete,
            "economic_gate_passed": economic_gate_passed,
            "gate_0_passed": gate_passed,
            "stop_recommended": stop_recommended,
            "rerun_required": not evidence_complete,
            "decision": (
                "PASS"
                if gate_passed
                else ("STOP" if stop_recommended else "RERUN_REQUIRED")
            ),
            "fill_assumption": (
                "decision_time_executable"
                if mode is PairExecutionMode.TAKER_TAKER
                else "hypothetical_full_fill_requires_shadow_validation"
            ),
        }

    curves: list[dict[str, Any]] = []
    negative_count = 0
    observation_count = 0
    midpoint_observation_count = 0
    for expiry_id, observations in sorted(observations_by_expiry.items()):
        points: list[dict[str, Any]] = []
        for context, books, _maker_fill_caps, _fill_cap_source in sorted(
            observations,
            key=lambda item: item[0].decision_tau_seconds,
            reverse=True,
        ):
            diagnostic = _implicit_split_probability_diagnostic(context, books)
            observation_count += 1
            split_probability = diagnostic["b"]
            if isinstance(split_probability, Decimal):
                midpoint_observation_count += 1
            if isinstance(split_probability, Decimal) and split_probability < ZERO:
                negative_count += 1
            points.append(
                {
                    "ttc_seconds": context.decision_tau_seconds,
                    "b": (
                        None
                        if split_probability is None
                        else str(split_probability)
                    ),
                    "bid_side_sum_minus_one": (
                        None
                        if diagnostic["bid_side_sum_minus_one"] is None
                        else str(diagnostic["bid_side_sum_minus_one"])
                    ),
                    "ask_side_sum_minus_one": (
                        None
                        if diagnostic["ask_side_sum_minus_one"] is None
                        else str(diagnostic["ask_side_sum_minus_one"])
                    ),
                    "unavailable_reason": diagnostic["unavailable_reason"],
                }
            )
        curves.append(
            {
                "attempt_id": expiry_id,
                "points": points,
                "negative_b_observation_count": sum(
                    point["b"] is not None and Decimal(point["b"]) < ZERO
                    for point in points
                ),
                "unavailable_midpoint_observation_count": sum(
                    point["b"] is None for point in points
                ),
            }
        )
    split_diagnostics = {
        "definition": "sum_of_structural_leg_midprices_minus_one",
        "observation_count": observation_count,
        "midpoint_observation_count": midpoint_observation_count,
        "unavailable_midpoint_observation_count": (
            observation_count - midpoint_observation_count
        ),
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
    gate_0_contract = _load_gate_0_contract(V08_PREREGISTRATION_PATH)
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

    def _maker_fill_caps_for_context(
        context: Any,
        books: Mapping[str, Any],
    ) -> tuple[Mapping[str, Decimal] | None, str | None]:
        explicit_caps = getattr(context, "future_public_trade_caps_by_token_id", None)
        if isinstance(explicit_caps, Mapping):
            return (
                {
                    str(token_id): Decimal(str(quantity))
                    for token_id, quantity in explicit_caps.items()
                },
                "diagnostic.precomputed_fill_cap_summary",
            )
        future_trades = getattr(context, "future_public_trades_by_token_id", None)
        if isinstance(future_trades, Mapping):
            expected_token_ids = set(books)
            if {str(token_id) for token_id in future_trades} != expected_token_ids:
                return None, "context.future_public_trades_by_token_id"
            caps: dict[str, Decimal] = {}
            seen_trade_ids: set[str] = set()
            for token_id in expected_token_ids:
                trades = future_trades.get(token_id)
                if not isinstance(trades, Sequence):
                    return None, "context.future_public_trades_by_token_id"
                book = books.get(token_id)
                if book is None or book.best_bid is None:
                    return None, "context.future_public_trades_by_token_id"
                total = ZERO
                for trade in trades:
                    if hasattr(trade, "price") and hasattr(trade, "quantity"):
                        trade_token_id = getattr(trade, "token_id", token_id)
                        aggressor = getattr(trade, "aggressor_side", None)
                        aggressor_value = getattr(aggressor, "value", aggressor)
                        price = Decimal(str(trade.price))
                        quantity = Decimal(str(trade.quantity))
                        timestamp_ms = getattr(trade, "timestamp_ms", None)
                        source_event_id = getattr(trade, "source_event_id", None)
                    elif isinstance(trade, Mapping):
                        trade_token_id = trade.get("token_id", token_id)
                        aggressor_value = trade.get("aggressor_side")
                        price = Decimal(str(trade.get("price")))
                        quantity = Decimal(str(trade.get("quantity")))
                        timestamp_ms = trade.get("timestamp_ms")
                        source_event_id = trade.get("source_event_id")
                    else:
                        return None, "context.future_public_trades_by_token_id"
                    if (
                        trade_token_id != token_id
                        or not isinstance(source_event_id, str)
                        or not source_event_id
                        or source_event_id in seen_trade_ids
                        or isinstance(timestamp_ms, bool)
                        or not isinstance(timestamp_ms, int)
                        or not context.decision_at_ms < timestamp_ms < context.expiry_ms
                        or not price.is_finite()
                        or not quantity.is_finite()
                        or quantity < ZERO
                    ):
                        return None, "context.future_public_trades_by_token_id"
                    seen_trade_ids.add(source_event_id)
                    if str(aggressor_value).upper() == "SELL" and price <= book.best_bid:
                        total += quantity
                    elif str(aggressor_value).upper() != "BUY":
                        return None, "context.future_public_trades_by_token_id"
                caps[token_id] = total
            return caps, "context.future_public_trades_by_token_id"
        return None, None

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
        books = _books_for_context(context, required=False) or {}
        maker_fill_caps, _fill_cap_source = _maker_fill_caps_for_context(context, books)
        return PerfectInformationAttempt(
            attempt_id=unique_attempt_id,
            pair=context.pair,
            settlement_state=context.settlement_state,
            books=books,
            actual_5_up=context.actual_5_up,
            actual_15_up=context.actual_15_up,
            pricing_policy=PairPricingPolicy(
                maximum_spread_each_leg=pricing_policy.maximum_spread_each_leg,
                minimum_market_price=pricing_policy.minimum_market_price,
                maximum_market_price=pricing_policy.maximum_market_price,
                pair_risk_usdc=pricing_policy.pair_risk_usdc,
                structural_only=pricing_policy.structural_only,
                maker_fill_cap_by_token_id=maker_fill_caps,
            ),
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
        str,
        dict[
            int,
            tuple[
                Any,
                Mapping[str, Any],
                Mapping[str, Decimal] | None,
                str | None,
            ],
        ],
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
                    maker_fill_caps, fill_cap_source = _maker_fill_caps_for_context(
                        scanned_context,
                        books,
                    )
                    observation_slots[expiry_id][ttc_seconds] = (
                        scanned_context,
                        books,
                        maker_fill_caps,
                        fill_cap_source,
                    )
            # Preserve the exact preregistered contexts at their timestamps.
            # Real contexts share one replay; this also prevents a synthetic
            # fixture from hiding a distinct captured book at the same tau.
            for context in expiry_contexts:
                books = _books_for_context(context, required=False)
                if books is not None:
                    maker_fill_caps, fill_cap_source = _maker_fill_caps_for_context(
                        context,
                        books,
                    )
                    observation_slots[expiry_id][context.decision_tau_seconds] = (
                        context,
                        books,
                        maker_fill_caps,
                        fill_cap_source,
                    )
    else:
        for context in selected:
            books = _books_for_context(context)
            assert books is not None
            maker_fill_caps, fill_cap_source = _maker_fill_caps_for_context(
                context,
                books,
            )
            observation_slots[context.expiry_cluster_id][
                context.decision_tau_seconds
            ] = (context, books, maker_fill_caps, fill_cap_source)
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
            required_observation_count_per_expiry=(
                301 if decision_tau_seconds is None else 1
            ),
        )
    )
    maker_maker_gate = execution_modes[PairExecutionMode.MAKER_MAKER.value]
    strict_fill_volume_bound_complete = all(
        attempt.get("fill_volume_bound_source") == "context.future_public_trades_by_token_id"
        for attempt in maker_maker_gate["attempts"]
        if attempt.get("selected_level") is not None
    ) and not maker_maker_gate["missing_fill_volume_bound_expiry_ids"]
    strict_parameters_match = (
        decision_tau_seconds is None
        and expected_clean_attempts == gate_0_contract["expected_clean_attempts"]
        and strict_fill_volume_bound_complete
    )
    strict_identity_payload = {
        "track_id": gate_0_contract["track_id"],
        "v08_preregistration_sha256": gate_0_contract["preregistration_sha256"],
        "expected_clean_attempts": gate_0_contract["expected_clean_attempts"],
        "scan_ttc_seconds_inclusive": gate_0_contract["scan_ttc_seconds_inclusive"],
        "candidate_ttc_count": gate_0_contract["candidate_ttc_count"],
        "decision_execution_mode": gate_0_contract["decision_execution_mode"],
        "minimum_average_best_total_pnl_per_expiry_usdc": gate_0_contract[
            "minimum_average_best_total_pnl_per_expiry_usdc"
        ],
    }
    authorized_gate_pass = strict_parameters_match and maker_maker_gate["gate_0_passed"]
    rerun_required = (
        not strict_parameters_match or maker_maker_gate["rerun_required"]
    )
    stop_recommended = (
        strict_parameters_match
        and not rerun_required
        and maker_maker_gate["stop_recommended"]
    )
    top_level_decision = (
        "RERUN_REQUIRED"
        if rerun_required
        else ("PASS" if authorized_gate_pass else "STOP")
    )
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "counterfactual_preregistration_path": str(preregistration_path),
        "counterfactual_preregistration_sha256": _sha256(preregistration_path),
        "authority": {
            "track_id": gate_0_contract["track_id"],
            "v08_preregistration_path": gate_0_contract["preregistration_path"],
            "v08_preregistration_sha256": gate_0_contract["preregistration_sha256"],
            "expected_clean_attempts": gate_0_contract["expected_clean_attempts"],
            "scan_ttc_seconds_inclusive": gate_0_contract[
                "scan_ttc_seconds_inclusive"
            ],
            "candidate_ttc_count": gate_0_contract["candidate_ttc_count"],
            "decision_execution_mode": gate_0_contract[
                "decision_execution_mode"
            ],
            "minimum_average_best_total_pnl_per_expiry_usdc": gate_0_contract[
                "minimum_average_best_total_pnl_per_expiry_usdc"
            ],
            "strict_fill_volume_bound_complete": strict_fill_volume_bound_complete,
            "strict_identity_sha256": hashlib.sha256(
                canonical_json_bytes(strict_identity_payload)
            ).hexdigest(),
            "requested_expected_clean_attempts": expected_clean_attempts,
            "requested_decision_tau_seconds": decision_tau_seconds,
            "strict_parameters_match": strict_parameters_match,
            "can_authorize_pass": strict_parameters_match,
        },
        "decision_tau_seconds": decision_tau_seconds,
        "expected_clean_attempts": expected_clean_attempts,
        "observed_unique_common_expiry_attempts": len(selected),
        "selected_decision_tau_seconds_by_expiry": {
            context.expiry_cluster_id: context.decision_tau_seconds
            for context in selected
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
            "observed_point_count_by_expiry": {
                expiry_id: len(items)
                for expiry_id, items in sorted(observations_by_expiry.items())
            },
            "all_expiries_complete": not maker_maker_gate[
                "incomplete_scan_expiry_ids"
            ],
            "sampling": (
                "every_integer_second_in_5m_live_window"
                if decision_tau_seconds is None
                else "diagnostic_fixed_tau_override"
            ),
        },
        "gate_0_passed": authorized_gate_pass,
        "stop_recommended": stop_recommended,
        "rerun_required": rerun_required,
        "decision": top_level_decision,
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
            "formal_pass_requires_strict_v08_authority": True,
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
    parser.add_argument("--expected-clean-attempts", type=int, required=True)
    parser.add_argument("--decision-tau-seconds", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gate_0_contract = _load_gate_0_contract(V08_PREREGISTRATION_PATH)
    if args.decision_tau_seconds is not None:
        raise ValueError(
            "formal Gate-0 reports must scan the full preregistered 300→0 window; "
            "--decision-tau-seconds is diagnostic-only and cannot authorize PASS/STOP"
        )
    if args.expected_clean_attempts != gate_0_contract["expected_clean_attempts"]:
        raise ValueError(
            "formal Gate-0 reports must use the preregistered clean-attempt count: "
            f"expected {gate_0_contract['expected_clean_attempts']}, "
            f"got {args.expected_clean_attempts}"
        )
    report = build_upper_bound_report(
        manifest_path=args.manifest,
        decision_tau_seconds=None,
        expected_clean_attempts=args.expected_clean_attempts,
    )
    _write_report(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
