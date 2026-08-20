#!/usr/bin/env python3
"""Build the legacy/old-Edge-Lab evidence baseline without network access.

The pack deliberately distinguishes authenticated fills from synthetic,
touch, shadow, and public-flow records.  It is an audit/reproduction tool, not
an execution entry point.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.run_backtest import generate_mock_data, simple_mm_strategy
from src.backtest.engine import BacktestEngine
from src.strategy.backtest_wrapper import run_backtest_with_spread


SCHEMA = "polymarket-edge-lab/baseline-evidence-pack-v1"
PACK_RELATIVE_PATH = Path(
    "research/edge_discovery_2026-07-24/BASELINE_EVIDENCE_PACK.json"
)
REBUILT_PACK_RELATIVE_PATH = Path(
    "research/edge_discovery_2026-07-24/BASELINE_EVIDENCE_PACK_REBUILT.json"
)
OLD_RESEARCH_RELATIVE_PATH = Path("research/profit_redesign_2026-07-24")
REQUIRED_METRICS = (
    "initial_capital",
    "pnl",
    "return",
    "fees",
    "rewards",
    "max_drawdown",
    "confidence_interval",
    "pnl_concentration",
)


def _decimal_text(value: Decimal | int | str | float) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _metric(
    value: Decimal | int | str | float | None,
    *,
    unit: str | None,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "value": None if value is None else _decimal_text(value),
        "unit": unit,
        "reason": reason,
    }


def _retained_complete_set_edge(
    *,
    size: Decimal,
    direction: str,
    result: Mapping[str, Any],
) -> Decimal:
    fees_and_buffer = Decimal(str(result["fees"])) + Decimal(
        str(result["buffer"])
    )
    if direction == "buy_both":
        return (
            size
            - Decimal(str(result["yes_cost"]))
            - Decimal(str(result["no_cost"]))
            - fees_and_buffer
        )
    if direction == "sell_both_from_split_inventory":
        return (
            Decimal(str(result["yes_proceeds"]))
            + Decimal(str(result["no_proceeds"]))
            - size
            - fees_and_buffer
        )
    raise ValueError(f"unknown complete-set direction: {direction}")


def _unavailable_metrics(
    reasons: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    return {
        name: _metric(
            None,
            unit=(
                "pUSD"
                if name in {"initial_capital", "pnl", "fees", "rewards"}
                else "ratio"
            ),
            reason=reasons[name],
        )
        for name in REQUIRED_METRICS
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(value)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_manifest(
    repo_root: Path,
    paths: Iterable[Path],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(repo_root).as_posix(),
            "kind": kind,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(set(paths))
    ]


def _latest_source_timestamp(documents: Iterable[Any]) -> str:
    values: list[str] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        for key in ("generated_at", "observed_at"):
            value = document.get(key)
            if isinstance(value, str):
                values.append(value)
    return max(values) if values else "2026-07-24"


def _legacy_mock_evidence(repo_root: Path) -> dict[str, Any]:
    del repo_root  # The reproduction is intentionally generated, not loaded.
    initial_capital = Decimal("1000")
    data = generate_mock_data(num_snapshots=500)
    engine = BacktestEngine(initial_capital=initial_capital)
    result = engine.run(data, strategy="custom", strategy_fn=simple_mm_strategy)
    final_capital = result.final_capital
    legacy_reported_pnl = final_capital - initial_capital
    trades_by_timestamp: dict[int, list[dict[str, Any]]] = {}
    for trade in engine._trades:  # noqa: SLF001 - audit seam
        trades_by_timestamp.setdefault(int(trade["timestamp"]), []).append(trade)
    cash = initial_capital
    inventory = Decimal("0")
    minimum_inventory = Decimal("0")
    maximum_inventory = Decimal("0")
    reconciled_equity_curve = [initial_capital]
    for snapshot in data.iterate():
        for trade in trades_by_timestamp.get(snapshot.timestamp, []):
            notional = trade["price"] * trade["size"]
            if trade["side"] == "BUY":
                cash -= notional + trade["fee"]
                inventory += trade["size"]
            else:
                cash += notional - trade["fee"]
                inventory -= trade["size"]
            minimum_inventory = min(minimum_inventory, inventory)
            maximum_inventory = max(maximum_inventory, inventory)
        midpoint = (snapshot.best_bid + snapshot.best_ask) / 2
        reconciled_equity_curve.append(cash + inventory * midpoint)
    reconciled_final_equity = reconciled_equity_curve[-1]
    reconciled_pnl = reconciled_final_equity - initial_capital
    reconciled_return = reconciled_pnl / initial_capital
    reconciled_peak = reconciled_equity_curve[0]
    reconciled_max_drawdown = Decimal("0")
    for equity in reconciled_equity_curve:
        reconciled_peak = max(reconciled_peak, equity)
        reconciled_max_drawdown = max(
            reconciled_max_drawdown,
            (reconciled_peak - equity) / reconciled_peak,
        )
    legacy_reported_peak = engine._equity_curve[0]  # noqa: SLF001
    legacy_reported_max_drawdown = Decimal("0")
    for equity in engine._equity_curve:  # noqa: SLF001 - audit seam
        legacy_reported_peak = max(legacy_reported_peak, equity)
        legacy_reported_max_drawdown = max(
            legacy_reported_max_drawdown,
            (legacy_reported_peak - equity) / legacy_reported_peak,
        )
    fees = sum(
        (trade["fee"] for trade in engine._trades),  # noqa: SLF001 - audit seam
        Decimal("0"),
    )

    metrics = {
        "initial_capital": _metric(
            initial_capital,
            unit="pUSD",
            reason="Configured by the deterministic legacy mock runner.",
        ),
        "pnl": _metric(
            reconciled_pnl,
            unit="pUSD",
            reason=(
                "Independently reconciled synthetic cash plus signed inventory "
                "marked at the last midpoint; not real or historical PnL."
            ),
        ),
        "return": _metric(
            reconciled_return,
            unit="ratio",
            reason=(
                "Independently reconciled synthetic return; not profitability "
                "evidence."
            ),
        ),
        "fees": _metric(
            fees,
            unit="pUSD",
            reason=(
                "Modeled zero under the runner's default FEE_FREE scenario; "
                "not observed exchange fees."
            ),
        ),
        "rewards": _metric(
            Decimal("0"),
            unit="pUSD",
            reason="The legacy mock runner has no reward model.",
        ),
        "max_drawdown": _metric(
            reconciled_max_drawdown,
            unit="ratio",
            reason=(
                "Computed on an independently reconstructed cash plus signed-"
                "inventory synthetic equity curve."
            ),
        ),
        "confidence_interval": _metric(
            None,
            unit="ratio",
            reason=(
                "One deterministic synthetic path has no independent event "
                "sample from which to estimate a defensible confidence interval."
            ),
        ),
        "pnl_concentration": _metric(
            None,
            unit="ratio",
            reason=(
                "The run has one synthetic token and trade PnL does not form "
                "independent event-level observations."
            ),
        ),
    }
    return {
        "evidence_id": "legacy_mock_crossing_backtest",
        "data_level": "L0",
        "classification": "rejected",
        "source_paths": [
            "scripts/run_backtest.py",
            "src/backtest/engine.py",
            "src/backtest/data.py",
            "src/backtest/fee_model.py",
        ],
        "counts": {
            "market_count": 1,
            "event_count": 0,
            "snapshot_count": len(data),
            "synthetic_fill_count": result.total_trades,
            "authenticated_fill_count": 0,
        },
        "count_reasons": {
            "market_count": "One generated token named mock-token-1.",
            "event_count": "No event exists; the token and path are synthetic.",
            "authenticated_fill_count": (
                "Every fill is generated by the legacy crossing fill model."
            ),
        },
        "metrics": metrics,
        "details": {
            "reconciled_final_equity": _decimal_text(reconciled_final_equity),
            "legacy_engine_reported_final_capital": _decimal_text(final_capital),
            "legacy_engine_reported_pnl": _decimal_text(legacy_reported_pnl),
            "legacy_engine_reported_return": _decimal_text(result.total_return),
            "legacy_engine_reported_max_drawdown": _decimal_text(
                legacy_reported_max_drawdown
            ),
            "legacy_engine_equity_reconciliation_gap": _decimal_text(
                final_capital - reconciled_final_equity
            ),
            "winning_trade_count": result.winning_trades,
            "losing_trade_count": result.losing_trades,
            "win_rate": _decimal_text(result.win_rate),
            "sharpe_ratio": _decimal_text(result.sharpe_ratio),
            "profit_factor": _decimal_text(result.profit_factor),
            "ending_position_shares": _decimal_text(engine.position),
            "minimum_position_shares": _decimal_text(minimum_inventory),
            "maximum_position_shares": _decimal_text(maximum_inventory),
            "fill_semantics": (
                "The example strategy deliberately crosses the spread and the "
                "engine permits inventory behavior that is not authenticated "
                "Polymarket execution."
            ),
            "accounting_warning": (
                "The legacy engine's equity curve adds only unrealized profit "
                "to cash rather than signed inventory market value. Its reported "
                "final capital, return, and drawdown therefore do not reconcile."
            ),
        },
        "disqualifying_reasons": [
            "Synthetic trigonometric market path.",
            "No historical market or event.",
            "No authenticated order, fill, settlement, or payout evidence.",
            "The example strategy crosses the spread to force simulated fills.",
            "The legacy equity calculation does not reconcile cash and inventory.",
            "The reconstructed path reaches a naked short position of 330 shares.",
        ],
    }


def _legacy_dry_run_evidence(
    repo_root: Path,
    log_paths: Sequence[Path],
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for path in log_paths:
        records.extend(_read_jsonl(path))

    trade_records = [row for row in records if row.get("type") == "trade"]
    quote_records = [row for row in records if row.get("type") == "quote"]
    token_ids = {
        str(row["market_id"])
        for row in records
        if row.get("market_id") not in (None, "")
    }
    simulated_records = [
        row
        for row in trade_records
        if str(row.get("order_id", "")).startswith("sim_")
    ]
    side_counts = Counter(str(row.get("side")) for row in simulated_records)
    side_sizes: dict[str, Decimal] = {}
    side_notionals: dict[str, Decimal] = {}
    for row in simulated_records:
        side = str(row.get("side"))
        size = Decimal(str(row["size"]))
        price = Decimal(str(row["price"]))
        side_sizes[side] = side_sizes.get(side, Decimal("0")) + size
        side_notionals[side] = (
            side_notionals.get(side, Decimal("0")) + size * price
        )

    shared_reason = (
        "Dry-run log records are simulated output, not authenticated fills; "
        "the logs omit a reconcilable starting ledger, settlement, and payouts."
    )
    reasons = {
        "initial_capital": (
            "The JSONL files do not persist a session starting-capital ledger."
        ),
        "pnl": shared_reason,
        "return": "PnL and starting capital are unavailable.",
        "fees": "TradeLogger omits the simulator fee field from these JSONL rows.",
        "rewards": "No order-scoring or payout records are present.",
        "max_drawdown": "No reconciled equity time series is present.",
        "confidence_interval": (
            "There are no valid trade-level returns or independent event groups."
        ),
        "pnl_concentration": (
            "There is no reconciled PnL to allocate across markets or events."
        ),
    }
    return {
        "evidence_id": "legacy_dry_run_jsonl",
        "data_level": "L0",
        "classification": "rejected",
        "source_paths": [
            path.relative_to(repo_root).as_posix() for path in log_paths
        ],
        "counts": {
            "file_count": len(log_paths),
            "token_id_count": len(token_ids),
            "market_count": None,
            "event_count": None,
            "record_count": len(records),
            "quote_record_count": len(quote_records),
            "simulated_trade_record_count": len(simulated_records),
            "non_simulated_trade_record_count": (
                len(trade_records) - len(simulated_records)
            ),
            "authenticated_fill_count": 0,
        },
        "count_reasons": {
            "token_id_count": (
                "Distinct outcome-token identifiers stored in market_id fields."
            ),
            "market_count": (
                "Outcome-token IDs cannot be mapped to distinct conditions "
                "without condition/token metadata."
            ),
            "event_count": (
                "The logs contain token/market identifiers but no Gamma event IDs."
            ),
            "authenticated_fill_count": (
                "All trade order IDs use the sim_ prefix and originate from "
                "OrderSimulator.check_fills."
            ),
        },
        "metrics": _unavailable_metrics(reasons),
        "details": {
            "simulated_side_counts": dict(sorted(side_counts.items())),
            "simulated_side_sizes": {
                side: _decimal_text(value)
                for side, value in sorted(side_sizes.items())
            },
            "simulated_side_notionals": {
                side: _decimal_text(value)
                for side, value in sorted(side_notionals.items())
            },
            "fill_semantics": (
                "The source calls simulator fills 'maker' in the log, but the "
                "matching rule is local price crossing and no exchange maker "
                "identity or queue position is present."
            ),
        },
        "disqualifying_reasons": [
            "Local simulator order IDs, not exchange order IDs.",
            "Naked simulated SELL records are not backed by persisted inventory.",
            "No cash, fee, settlement, reward, or equity reconciliation.",
        ],
    }


def _legacy_autoresearch_evidence(
    repo_root: Path,
    results_path: Path,
) -> dict[str, Any]:
    lines = [
        line
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    rows = list(csv.DictReader(lines, delimiter="\t"))
    retained_rows = [
        row
        for row in rows
        if row.get("status") == "keep" and row.get("metric")
    ]
    spread_by_iteration = {
        "1": Decimal("0.02"),
        "2": Decimal("0.01"),
        "3": Decimal("0.005"),
        "4": Decimal("0.001"),
        "5": Decimal("0.0001"),
    }
    tolerance = Decimal("0.00005")
    diagnostic_results: list[dict[str, Any]] = []
    roundtrip_passes = 0
    max_difference = Decimal("0")
    synthetic_fill_count = 0
    for row in retained_rows:
        iteration = str(row["iteration"])
        spread = spread_by_iteration[iteration]
        reproduced = run_backtest_with_spread(
            spread=spread,
            snapshots=500,
            capital=1000.0,
        )
        recorded_sharpe = Decimal(str(row["metric"]))
        reproduced_sharpe = Decimal(str(reproduced["sharpe_ratio"]))
        difference = abs(recorded_sharpe - reproduced_sharpe)
        roundtrip_passes += int(difference <= tolerance)
        max_difference = max(max_difference, difference)
        synthetic_fill_count += int(reproduced["total_trades"])
        diagnostic_results.append(
            {
                "iteration": int(iteration),
                "spread": _decimal_text(spread),
                "recorded_sharpe": _decimal_text(recorded_sharpe),
                "current_code_reproduced_sharpe": _decimal_text(
                    reproduced_sharpe
                ),
                "absolute_difference": _decimal_text(difference),
                "legacy_engine_reported_return": _decimal_text(
                    reproduced["total_return"]
                ),
                "legacy_engine_reported_max_drawdown": _decimal_text(
                    reproduced["max_drawdown"]
                ),
                "synthetic_fill_count": int(reproduced["total_trades"]),
            }
        )

    reasons = {
        "initial_capital": (
            "The TSV does not persist capital; 1,000 is only a current-code "
            "diagnostic reproduction parameter."
        ),
        "pnl": (
            "The TSV stores Sharpe only, and the underlying synthetic engine's "
            "equity accounting does not reconcile cash plus inventory."
        ),
        "return": (
            "Historical return is absent; current-code diagnostic returns use "
            "the invalid legacy equity curve."
        ),
        "fees": "The TSV stores no fee inputs or paid-fee ledger.",
        "rewards": "The TSV stores no reward model, scoring, or payouts.",
        "max_drawdown": (
            "Historical drawdown is absent; current-code diagnostic drawdown "
            "uses the invalid legacy equity curve."
        ),
        "confidence_interval": (
            "Five parameter points reuse one deterministic synthetic path."
        ),
        "pnl_concentration": (
            "There is no valid PnL and only one synthetic token/path."
        ),
    }
    return {
        "evidence_id": "legacy_autoresearch_parameter_search",
        "data_level": "L0",
        "classification": "rejected",
        "source_paths": [
            results_path.relative_to(repo_root).as_posix(),
            "scripts/autoresearch_verify.py",
            "src/strategy/real_data_backtest.py",
            "src/strategy/backtest_wrapper.py",
        ],
        "counts": {
            "tsv_row_count": len(rows),
            "retained_iteration_count": len(retained_rows),
            "market_count": 1,
            "event_count": 0,
            "diagnostic_synthetic_fill_count": synthetic_fill_count,
            "authenticated_fill_count": 0,
        },
        "count_reasons": {
            "market_count": "Each diagnostic uses one generated synthetic token.",
            "event_count": "No historical event is replayed.",
            "authenticated_fill_count": (
                "The verification path generates a trigonometric price series "
                "and local simulated fills."
            ),
        },
        "metrics": _unavailable_metrics(reasons),
        "recomputation_audit": {
            "mode": "offline_current_code_diagnostic",
            "network_accessed": False,
            "recorded_sharpe_rounding_tolerance": _decimal_text(tolerance),
            "recorded_sharpe_roundtrip_check_count": len(retained_rows),
            "recorded_sharpe_roundtrip_pass_count": roundtrip_passes,
            "recorded_sharpe_max_absolute_difference": _decimal_text(
                max_difference
            ),
            "diagnostic_results": diagnostic_results,
            "warning": (
                "The rounded Sharpe values reproduce, but the path is synthetic "
                "and all current-code return/drawdown fields use the invalid "
                "legacy equity calculation."
            ),
        },
        "details": {
            "recorded_sharpe_min": _decimal_text(
                min(Decimal(str(row["metric"])) for row in retained_rows)
            ),
            "recorded_sharpe_max": _decimal_text(
                max(Decimal(str(row["metric"])) for row in retained_rows)
            ),
            "real_data_name_warning": (
                "real_data_backtest.py fetches one current market as a seed and "
                "then generates a deterministic trigonometric path; it is not "
                "historical real-data replay."
            ),
        },
        "disqualifying_reasons": [
            "Synthetic path reused across parameter points.",
            "Negative recorded Sharpe at every retained parameter point.",
            "No historical fills, events, settlement, or payout evidence.",
            "Underlying engine equity accounting does not reconcile.",
        ],
    }


def _replay_priority(path: Path) -> tuple[int, str]:
    name = path.name
    if "robust_" in name:
        rank = 0
    elif "final_replay_12h_v2" in name:
        rank = 5
    elif "final_replay_12h" in name:
        rank = 4
    elif "replay_12h_v2" in name:
        rank = 3
    elif "replay_12h" in name:
        rank = 2
    else:
        rank = 1
    return rank, name


def _recompute_replay_arithmetic(
    replay_documents: Sequence[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    tolerance = Decimal("1e-12")
    distribution_blocks = 0
    distribution_exact_passes = 0
    distribution_tolerance_passes = 0
    distribution_max_difference = Decimal("0")
    distribution_failures: list[dict[str, Any]] = []
    integrated_artifacts = 0
    integrated_exact_passes = 0
    integrated_tolerance_passes = 0
    integrated_max_difference = Decimal("0")
    integrated_failures: list[dict[str, Any]] = []

    for path, document in replay_documents:
        samples = document["samples"]
        distributions = document.get("economics_distribution", {})
        for field_name, observed in distributions.items():
            if not (
                isinstance(observed, dict)
                and {"count", "min", "median", "p90", "max"} <= set(observed)
                and any(field_name in sample for sample in samples)
            ):
                continue
            values = sorted(
                Decimal(str(sample[field_name]))
                for sample in samples
                if sample.get(field_name) is not None
            )
            if values:
                middle = len(values) // 2
                median = (
                    values[middle]
                    if len(values) % 2
                    else (values[middle - 1] + values[middle]) / Decimal("2")
                )
                p90_index = min(
                    len(values) - 1,
                    int((len(values) - 1) * 0.9),
                )
                expected: dict[str, Any] = {
                    "count": len(values),
                    "min": values[0],
                    "median": median,
                    "p90": values[p90_index],
                    "max": values[-1],
                }
            else:
                expected = {
                    "count": 0,
                    "min": None,
                    "median": None,
                    "p90": None,
                    "max": None,
                }

            exact = int(observed["count"]) == expected["count"]
            within_tolerance = exact
            field_differences: dict[str, str] = {}
            for statistic_name in ("min", "median", "p90", "max"):
                expected_value = expected[statistic_name]
                observed_value = observed[statistic_name]
                if expected_value is None or observed_value is None:
                    same = expected_value is None and observed_value is None
                    exact = exact and same
                    within_tolerance = within_tolerance and same
                    continue
                difference = abs(
                    Decimal(str(observed_value)) - Decimal(expected_value)
                )
                distribution_max_difference = max(
                    distribution_max_difference, difference
                )
                field_differences[statistic_name] = _decimal_text(difference)
                exact = exact and difference == 0
                within_tolerance = (
                    within_tolerance and difference <= tolerance
                )
            distribution_blocks += 1
            distribution_exact_passes += int(exact)
            distribution_tolerance_passes += int(within_tolerance)
            if not within_tolerance:
                distribution_failures.append(
                    {
                        "path": path.name,
                        "field": field_name,
                        "differences": field_differences,
                    }
                )

        integrated = document.get("integrated_reward_scenario")
        if not isinstance(integrated, dict):
            continue
        covered_ms = 0
        reward = Decimal("0")
        for current, following in zip(samples, samples[1:]):
            interval_ms = max(
                0,
                min(
                    60_000,
                    int(following["timestamp_ms"])
                    - int(current["timestamp_ms"]),
                ),
            )
            covered_ms += interval_ms
            reward += (
                Decimal(str(current["daily_reward_if_static"]))
                * Decimal(interval_ms)
                / Decimal("86400000")
            )
        expected_values = {
            "covered_hours": Decimal(covered_ms) / Decimal("3600000"),
            "static_competition_reward": reward,
        }
        exact = True
        within_tolerance = True
        differences: dict[str, str] = {}
        for field_name, expected_value in expected_values.items():
            difference = abs(
                Decimal(str(integrated[field_name])) - expected_value
            )
            integrated_max_difference = max(
                integrated_max_difference, difference
            )
            differences[field_name] = _decimal_text(difference)
            exact = exact and difference == 0
            within_tolerance = within_tolerance and difference <= tolerance
        integrated_artifacts += 1
        integrated_exact_passes += int(exact)
        integrated_tolerance_passes += int(within_tolerance)
        if not within_tolerance:
            integrated_failures.append(
                {"path": path.name, "differences": differences}
            )

    return {
        "arithmetic_tolerance": _decimal_text(tolerance),
        "distribution_block_count": distribution_blocks,
        "distribution_block_exact_pass_count": distribution_exact_passes,
        "distribution_block_tolerance_pass_count": (
            distribution_tolerance_passes
        ),
        "distribution_max_absolute_difference": _decimal_text(
            distribution_max_difference
        ),
        "distribution_tolerance_failures": distribution_failures,
        "integrated_reward_artifact_count": integrated_artifacts,
        "integrated_reward_exact_pass_count": integrated_exact_passes,
        "integrated_reward_tolerance_pass_count": integrated_tolerance_passes,
        "integrated_reward_max_absolute_difference": _decimal_text(
            integrated_max_difference
        ),
        "integrated_reward_tolerance_failures": integrated_failures,
        "raw_l2_recomputable": False,
        "raw_l2_recomputable_reason": (
            "The replay artifacts retain derived samples but not the complete "
            "raw order-book responses and immutable raw manifests."
        ),
    }


def _public_replay_evidence(
    repo_root: Path,
    replay_documents: Sequence[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    by_condition: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    all_condition_timestamps: set[tuple[str, int]] = set()
    all_sample_rows = 0
    all_flow_records = 0
    all_shadow_events = 0
    for path, document in replay_documents:
        condition_id = str(document["condition_id"])
        by_condition.setdefault(condition_id, []).append((path, document))
        samples = document["samples"]
        all_sample_rows += len(samples)
        all_flow_records += int(document.get("trade_flow", {}).get("records", 0))
        all_shadow_events += int(
            document.get("shadow_fill_proxy", {}).get("events", 0)
        )
        for sample in samples:
            all_condition_timestamps.add(
                (condition_id, int(sample["timestamp_ms"]))
            )

    canonical: list[tuple[Path, dict[str, Any]]] = []
    for variants in by_condition.values():
        canonical.append(max(variants, key=lambda item: _replay_priority(item[0])))
    canonical.sort(key=lambda item: str(item[1]["condition_id"]))
    canonical_sample_rows = sum(len(document["samples"]) for _, document in canonical)
    canonical_flow_records = sum(
        int(document.get("trade_flow", {}).get("records", 0))
        for _, document in canonical
    )
    canonical_shadow_events = sum(
        int(document.get("shadow_fill_proxy", {}).get("events", 0))
        for _, document in canonical
    )

    reasons = {
        "initial_capital": (
            "Replay reports define quote sizes but no funded portfolio ledger."
        ),
        "pnl": (
            "Public trades/touches do not reveal our queue position or prove "
            "that a hypothetical order filled; shadow stress PnL is only a proxy."
        ),
        "return": "No valid initial capital or realized PnL exists.",
        "fees": (
            "Reports model hypothetical hedge fees but contain no executed fills."
        ),
        "rewards": (
            "Static reward-share scenarios are not order scoring or actual payouts."
        ),
        "max_drawdown": "There is no fill-backed equity curve.",
        "confidence_interval": (
            "There are no valid trade returns, and replay variants duplicate the "
            "same seven conditions."
        ),
        "pnl_concentration": "There is no fill-backed PnL to allocate.",
    }
    return {
        "evidence_id": "public_replay_corpus",
        "data_level": "L1",
        "classification": "insufficient_data",
        "source_paths": [
            path.relative_to(repo_root).as_posix()
            for path, _ in replay_documents
        ],
        "canonical_source_paths": [
            path.relative_to(repo_root).as_posix() for path, _ in canonical
        ],
        "counts": {
            "artifact_count": len(replay_documents),
            "market_count": len(by_condition),
            "event_count": None,
            "sample_row_count_including_variants": all_sample_rows,
            "unique_condition_timestamp_count": len(all_condition_timestamps),
            "canonical_sample_row_count": canonical_sample_rows,
            "public_flow_record_count_including_variants": all_flow_records,
            "canonical_public_flow_record_count": canonical_flow_records,
            "shadow_touch_event_count_including_variants": all_shadow_events,
            "canonical_shadow_touch_event_count": canonical_shadow_events,
            "authenticated_fill_count": 0,
        },
        "count_reasons": {
            "event_count": (
                "Reports persist condition IDs but not parent Gamma event IDs; "
                "conditions cannot be safely collapsed into events."
            ),
            "authenticated_fill_count": (
                "Every report explicitly says no passive fill is inferred from "
                "a book touch; public flow and shadow events remain proxies."
            ),
        },
        "metrics": _unavailable_metrics(reasons),
        "recomputation_audit": _recompute_replay_arithmetic(replay_documents),
        "details": {
            "condition_ids": sorted(by_condition),
            "variant_duplication_count": (
                all_sample_rows - len(all_condition_timestamps)
            ),
            "source_semantics": (
                "Unsupported public orderbook-history snapshots plus public "
                "trade-flow proxies. These are L1 under DATA_DICTIONARY.md."
            ),
        },
        "disqualifying_reasons": [
            "Unsupported historical endpoint and no immutable raw response manifest.",
            "No own authenticated orders or maker queue position.",
            "No actual fills, settlement, scoring, or payouts.",
            "Multiple parameter variants reuse the same condition-timestamps.",
        ],
    }


def _observe_evidence(
    repo_root: Path,
    path: Path,
    records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    conditions = {
        str(row["report"]["candidate"]["condition_id"])
        for row in records
        if row.get("ok")
        and isinstance(row.get("report"), dict)
        and isinstance(row["report"].get("candidate"), dict)
        and row["report"]["candidate"].get("condition_id")
    }
    success_count = sum(bool(row.get("ok")) for row in records)
    reasons = {
        "initial_capital": "Observation rows contain no funded portfolio ledger.",
        "pnl": "Quote snapshots and economic scenarios are not fills.",
        "return": "No valid initial capital or PnL exists.",
        "fees": "Only hypothetical hedge fees are present.",
        "rewards": "Displayed/static reward scenarios are not actual payouts.",
        "max_drawdown": "No fill-backed equity curve exists.",
        "confidence_interval": (
            "Twenty-one snapshots of one condition are not independent return samples."
        ),
        "pnl_concentration": "No realized PnL exists.",
    }
    return {
        "evidence_id": "public_observation_jsonl",
        "data_level": "L1",
        "classification": "insufficient_data",
        "source_paths": [path.relative_to(repo_root).as_posix()],
        "counts": {
            "file_count": 1,
            "market_count": len(conditions),
            "event_count": None,
            "observation_row_count": len(records),
            "successful_snapshot_count": success_count,
            "authenticated_fill_count": 0,
        },
        "count_reasons": {
            "event_count": "Observation rows contain condition IDs, not event IDs.",
            "authenticated_fill_count": (
                "Rows contain books/quotes/scenarios only; no order or trade IDs."
            ),
        },
        "metrics": _unavailable_metrics(reasons),
        "details": {
            "condition_ids": sorted(conditions),
            "first_observed_at": (
                min(str(row["observed_at"]) for row in records) if records else None
            ),
            "last_observed_at": (
                max(str(row["observed_at"]) for row in records) if records else None
            ),
        },
        "disqualifying_reasons": [
            "One market and approximately five minutes of polling.",
            "No authenticated fills, queue position, scoring, or payouts.",
        ],
    }


def _complete_set_scan_evidence(
    repo_root: Path,
    scans: Sequence[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    canonical_path, canonical = max(
        scans,
        key=lambda item: (
            int(item[1].get("gamma_rows_seen", 0)),
            float(item[1].get("execution_buffer_per_share", 0)),
            item[0].name,
        ),
    )
    reasons = {
        "initial_capital": "The scanner evaluates independent order-book snapshots.",
        "pnl": "No orders were submitted or filled.",
        "return": "No capital ledger or realized PnL exists.",
        "fees": "Fees are modeled inside candidate edge, not paid fees.",
        "rewards": "The complete-set scan does not observe reward payouts.",
        "max_drawdown": "No equity path exists.",
        "confidence_interval": (
            "This is a cross-sectional snapshot, not an independent return sample."
        ),
        "pnl_concentration": "There is no realized PnL to allocate.",
    }
    tolerance = Decimal("1e-12")
    market_size_results = 0
    directional_checks = 0
    directional_passes = 0
    retained_positive_directions = 0
    retained_zero_directions = 0
    retained_negative_directions = 0
    max_difference = Decimal("0")
    failures: list[dict[str, Any]] = []
    for path, document in scans:
        for market in document.get("top_near_edges", []):
            for size_result in market.get("results", []):
                market_size_results += 1
                size = Decimal(str(size_result["size"]))
                for direction, result in (
                    ("buy_both", size_result.get("buy_both")),
                    (
                        "sell_both_from_split_inventory",
                        size_result.get("sell_both_from_split_inventory"),
                    ),
                ):
                    if not isinstance(result, dict) or not result.get("executable"):
                        continue
                    expected = _retained_complete_set_edge(
                        size=size,
                        direction=direction,
                        result=result,
                    )
                    difference = abs(
                        Decimal(str(result["net_edge"])) - expected
                    )
                    observed_edge = Decimal(str(result["net_edge"]))
                    if observed_edge > 0:
                        retained_positive_directions += 1
                    elif observed_edge == 0:
                        retained_zero_directions += 1
                    else:
                        retained_negative_directions += 1
                    directional_checks += 1
                    directional_passes += int(difference <= tolerance)
                    max_difference = max(max_difference, difference)
                    if difference > tolerance:
                        failures.append(
                            {
                                "path": path.name,
                                "condition_id": market.get("condition_id"),
                                "size": _decimal_text(size),
                                "direction": direction,
                                "difference": _decimal_text(difference),
                            }
                        )
    return {
        "evidence_id": "complete_set_snapshot_scans",
        "data_level": "L1",
        "classification": "insufficient_data",
        "source_paths": [
            path.relative_to(repo_root).as_posix() for path, _ in scans
        ],
        "canonical_source_path": canonical_path.relative_to(repo_root).as_posix(),
        "counts": {
            "artifact_count": len(scans),
            "market_count": None,
            "event_count": None,
            "gamma_row_count": int(canonical["gamma_rows_seen"]),
            "source_reported_market_evaluation_count": int(
                canonical["markets_evaluated"]
            ),
            "canonical_retained_market_record_count": len(
                canonical.get("top_near_edges", [])
            ),
            "retained_market_record_count_including_variants": sum(
                len(document.get("top_near_edges", []))
                for _, document in scans
            ),
            "binary_market_prepared_count": int(
                canonical["binary_markets_prepared"]
            ),
            "market_skipped_for_book_count": int(
                canonical["markets_skipped_for_books"]
            ),
            "source_reported_positive_market_count": int(
                canonical["positive_market_count"]
            ),
            "retained_positive_direction_count": retained_positive_directions,
            "retained_zero_direction_count": retained_zero_directions,
            "retained_negative_direction_count": retained_negative_directions,
            "authenticated_fill_count": 0,
        },
        "count_reasons": {
            "market_count": (
                "Only the top 50 market identities are retained; the source-reported "
                "1,821 evaluated rows cannot be independently de-duplicated."
            ),
            "event_count": (
                "Full condition-to-event identities are not retained in the scan."
            ),
            "authenticated_fill_count": "Scanner is read-only and has no order path.",
        },
        "metrics": _unavailable_metrics(reasons),
        "recomputation_audit": {
            "arithmetic_tolerance": _decimal_text(tolerance),
            "market_size_result_count": market_size_results,
            "directional_net_edge_check_count": directional_checks,
            "directional_net_edge_tolerance_pass_count": directional_passes,
            "directional_net_edge_max_absolute_difference": _decimal_text(
                max_difference
            ),
            "directional_net_edge_tolerance_failures": failures,
            "full_scan_zero_positive_claim_recomputable": False,
            "full_scan_zero_positive_claim_recomputable_reason": (
                "Only the top 50 near-edge markets are retained per artifact; "
                "the books/results for all evaluated markets are absent."
            ),
        },
        "details": {
            "retained_snapshot_classification": "rejected",
            "execution_buffer_per_share": _decimal_text(
                canonical["execution_buffer_per_share"]
            ),
            "tested_sizes": [
                _decimal_text(value) for value in canonical.get("sizes", [])
            ],
            "source_reported_zero_buffer_positive_market_count": next(
                (
                    int(document["positive_market_count"])
                    for _, document in scans
                    if Decimal(
                        str(document.get("execution_buffer_per_share", "NaN"))
                    )
                    == 0
                ),
                None,
            ),
        },
        "disqualifying_reasons": [
            "Snapshot opportunity scan, not a trading backtest.",
            "Every retained directional edge is negative.",
            "The source-reported full-scan zero-positive count is not independently "
            "recomputable from the top-50-only artifacts.",
            "No order, fill, latency, settlement, or capital path.",
        ],
    }


def _reward_scan_evidence(
    repo_root: Path,
    scans: Sequence[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    canonical_path, canonical = max(
        scans,
        key=lambda item: (str(item[1].get("generated_at", "")), item[0].name),
    )
    production_eligible = sum(
        bool(row.get("preflight", {}).get("production_quote_eligible"))
        for row in canonical.get("results", [])
    )
    conditions = {
        str(row["candidate"]["condition_id"])
        for row in canonical.get("results", [])
        if row.get("candidate", {}).get("condition_id")
    }
    reasons = {
        "initial_capital": "The scanner has no account or funded portfolio ledger.",
        "pnl": "No orders were submitted or filled.",
        "return": "No valid initial capital or PnL exists.",
        "fees": "Hedge fees are scenarios, not paid fees.",
        "rewards": (
            "Displayed daily rates and static share scenarios are not order-scoring "
            "evidence or actual payouts."
        ),
        "max_drawdown": "No equity path exists.",
        "confidence_interval": (
            "A single configuration/book snapshot has no independent reward outcomes."
        ),
        "pnl_concentration": "No realized PnL or payout exists.",
    }
    tolerance = Decimal("1e-12")
    direction_count = 0
    paired_margin_passes = 0
    static_reward_passes = 0
    hedge_summary_passes = 0
    paired_margin_max_difference = Decimal("0")
    static_reward_max_difference = Decimal("0")
    hedge_summary_max_difference = Decimal("0")
    taker_direction_checks = 0
    taker_direction_passes = 0
    taker_max_difference = Decimal("0")
    audit_failures: list[dict[str, Any]] = []
    for path, scan in scans:
        reward_multiplier = Decimal(
            str(scan.get("config", {}).get("reward_multiplier", 1))
        )
        for row in scan.get("results", []):
            candidate = row["candidate"]
            economics = row["economics"]
            daily_reward = (
                Decimal(str(candidate["daily_reward"])) * reward_multiplier
            )
            directions = [("paired_bids", economics)]
            if isinstance(economics.get("paired_asks"), dict):
                directions.append(("paired_asks", economics["paired_asks"]))
            for direction, direction_economics in directions:
                direction_count += 1
                quote = direction_economics["quote"]
                size = Decimal(str(quote["size"]))
                if direction == "paired_bids":
                    margin_per_share = (
                        Decimal("1")
                        - Decimal(str(quote["yes_bid"]))
                        - Decimal(str(quote["no_bid"]))
                    )
                else:
                    margin_per_share = (
                        Decimal(str(quote["yes_ask"]))
                        + Decimal(str(quote["no_ask"]))
                        - Decimal("1")
                    )
                expected_margin = margin_per_share * size
                paired_fill = direction_economics["paired_fill"]
                margin_differences = [
                    abs(
                        Decimal(str(paired_fill["margin_per_share"]))
                        - margin_per_share
                    ),
                    abs(
                        Decimal(str(paired_fill["margin"])) - expected_margin
                    ),
                ]
                margin_max = max(margin_differences)
                paired_margin_max_difference = max(
                    paired_margin_max_difference, margin_max
                )
                margin_passed = margin_max <= tolerance
                paired_margin_passes += int(margin_passed)

                scenario = direction_economics[
                    "liquidity_reward_scenario"
                ]
                own_score = min(
                    Decimal(str(scenario["own_yes_score"])),
                    Decimal(str(scenario["own_no_score"])),
                )
                stressed_competition = (
                    Decimal(
                        str(
                            scenario["visible_competition_proxy"][
                                "q_min_proxy"
                            ]
                        )
                    )
                    * Decimal(str(scenario["competition_multiplier"]))
                )
                denominator = own_score + stressed_competition
                static_share = (
                    own_score / denominator
                    if denominator > 0
                    else Decimal("0")
                )
                static_reward = daily_reward * static_share
                reward_differences = [
                    abs(
                        Decimal(str(scenario["own_q_min"])) - own_score
                    ),
                    abs(
                        Decimal(str(scenario["static_share"])) - static_share
                    ),
                    abs(
                        Decimal(str(scenario["daily_reward_if_static"]))
                        - static_reward
                    ),
                ]
                reward_max = max(reward_differences)
                static_reward_max_difference = max(
                    static_reward_max_difference, reward_max
                )
                reward_passed = reward_max <= tolerance
                static_reward_passes += int(reward_passed)

                hedges = direction_economics["one_leg_hedges"]
                pnl_key = (
                    "merge_pnl"
                    if direction == "paired_bids"
                    else "complete_set_pnl"
                )
                path_keys = (
                    ("yes_fills_first", "no_fills_first")
                    if direction == "paired_bids"
                    else ("yes_sells_first", "no_sells_first")
                )
                completed_pnls = [
                    Decimal(str(hedges[key][pnl_key]))
                    for key in path_keys
                    if hedges[key].get("complete")
                    and hedges[key].get(pnl_key) is not None
                ]
                hedge_differences: list[Decimal] = []
                if len(completed_pnls) == 2:
                    worst_pnl = min(completed_pnls)
                    worst_loss = max(Decimal("0"), -worst_pnl)
                    break_even = (
                        worst_loss / expected_margin
                        if expected_margin > 0
                        else None
                    )
                    hedge_differences.extend(
                        [
                            abs(
                                Decimal(str(hedges["worst_completed_pnl"]))
                                - worst_pnl
                            ),
                            abs(
                                Decimal(str(hedges["worst_loss"]))
                                - worst_loss
                            ),
                        ]
                    )
                    if (
                        break_even is not None
                        and hedges.get(
                            "required_paired_fills_per_single_fill"
                        )
                        is not None
                    ):
                        hedge_differences.append(
                            abs(
                                Decimal(
                                    str(
                                        hedges[
                                            "required_paired_fills_per_single_fill"
                                        ]
                                    )
                                )
                                - break_even
                            )
                        )
                hedge_max = (
                    max(hedge_differences)
                    if hedge_differences
                    else Decimal("0")
                )
                hedge_summary_max_difference = max(
                    hedge_summary_max_difference, hedge_max
                )
                hedge_passed = hedge_max <= tolerance
                hedge_summary_passes += int(hedge_passed)

                if not (margin_passed and reward_passed and hedge_passed):
                    audit_failures.append(
                        {
                            "path": path.name,
                            "condition_id": candidate.get("condition_id"),
                            "direction": direction,
                            "margin_max_difference": _decimal_text(
                                margin_max
                            ),
                            "reward_max_difference": _decimal_text(
                                reward_max
                            ),
                            "hedge_max_difference": _decimal_text(hedge_max),
                        }
                    )

            for size_result in economics.get("taker_complete_sets", []):
                size = Decimal(str(size_result["size"]))
                for direction, result in (
                    ("buy_both", size_result.get("buy_both")),
                    (
                        "sell_both_from_split_inventory",
                        size_result.get("sell_both_from_split_inventory"),
                    ),
                ):
                    if not isinstance(result, dict) or not result.get("executable"):
                        continue
                    expected = _retained_complete_set_edge(
                        size=size,
                        direction=direction,
                        result=result,
                    )
                    difference = abs(
                        Decimal(str(result["net_edge"])) - expected
                    )
                    taker_direction_checks += 1
                    taker_direction_passes += int(difference <= tolerance)
                    taker_max_difference = max(
                        taker_max_difference, difference
                    )
    return {
        "evidence_id": "reward_market_snapshot_scans",
        "data_level": "L1",
        "classification": "insufficient_data",
        "source_paths": [
            path.relative_to(repo_root).as_posix() for path, _ in scans
        ],
        "canonical_source_path": canonical_path.relative_to(repo_root).as_posix(),
        "counts": {
            "artifact_count": len(scans),
            "market_count": len(conditions),
            "event_count": None,
            "reward_config_row_count": int(canonical["reward_rows_seen"]),
            "prefiltered_market_count": int(
                canonical["reward_rows_prefiltered"]
            ),
            "evaluated_market_count": int(canonical["evaluated"]),
            "production_quote_eligible_count": production_eligible,
            "authenticated_fill_count": 0,
            "actual_payout_record_count": 0,
        },
        "count_reasons": {
            "market_count": (
                "Unique condition IDs retained in the canonical evaluated results."
            ),
            "event_count": "Canonical results contain conditions, not event IDs.",
            "authenticated_fill_count": "Scanner is read-only.",
            "actual_payout_record_count": (
                "No maker/account-scoped payout evidence is present."
            ),
        },
        "metrics": _unavailable_metrics(reasons),
        "recomputation_audit": {
            "arithmetic_tolerance": _decimal_text(tolerance),
            "quote_direction_count": direction_count,
            "paired_margin_tolerance_pass_count": paired_margin_passes,
            "paired_margin_max_absolute_difference": _decimal_text(
                paired_margin_max_difference
            ),
            "static_reward_tolerance_pass_count": static_reward_passes,
            "static_reward_max_absolute_difference": _decimal_text(
                static_reward_max_difference
            ),
            "hedge_summary_tolerance_pass_count": hedge_summary_passes,
            "hedge_summary_max_absolute_difference": _decimal_text(
                hedge_summary_max_difference
            ),
            "taker_directional_net_edge_check_count": (
                taker_direction_checks
            ),
            "taker_directional_net_edge_tolerance_pass_count": (
                taker_direction_passes
            ),
            "taker_directional_net_edge_max_absolute_difference": (
                _decimal_text(taker_max_difference)
            ),
            "tolerance_failures": audit_failures,
            "raw_book_recomputable": False,
            "raw_book_recomputable_reason": (
                "The scans retain derived books only as top-of-book summaries; "
                "full raw levels needed to recompute scores, fees, and hedges "
                "are absent."
            ),
        },
        "details": {
            "condition_ids": sorted(conditions),
            "theoretical_reward_scenario_present": True,
            "recognized_reward_pnl": None,
        },
        "disqualifying_reasons": [
            "No production-preflight-eligible market in the canonical scan.",
            "No own authenticated orders, order scoring, or payout records.",
            "Static competition estimates cannot be recognized as rewards.",
        ],
    }


def _current_snapshot_evidence(
    repo_root: Path,
    snapshots: Sequence[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    conditions = {
        str(document["candidate"]["condition_id"])
        for _, document in snapshots
        if document.get("candidate", {}).get("condition_id")
    }
    reasons = {
        "initial_capital": "Snapshots contain hypothetical quote capital only.",
        "pnl": "A current book and quote scenario are not fills.",
        "return": "No valid portfolio capital or PnL exists.",
        "fees": "Only hypothetical hedge fees are present.",
        "rewards": "Static reward scenarios are not payouts.",
        "max_drawdown": "No equity path exists.",
        "confidence_interval": "Two conditions and repeated snapshots are not returns.",
        "pnl_concentration": "No realized PnL exists.",
    }
    return {
        "evidence_id": "current_quote_snapshots",
        "data_level": "L1",
        "classification": "insufficient_data",
        "source_paths": [
            path.relative_to(repo_root).as_posix() for path, _ in snapshots
        ],
        "counts": {
            "artifact_count": len(snapshots),
            "market_count": len(conditions),
            "event_count": None,
            "authenticated_fill_count": 0,
        },
        "count_reasons": {
            "event_count": "Snapshots contain condition IDs, not event IDs.",
            "authenticated_fill_count": "No order or trade evidence is present.",
        },
        "metrics": _unavailable_metrics(reasons),
        "details": {"condition_ids": sorted(conditions)},
        "disqualifying_reasons": [
            "Current snapshot/scenario only.",
            "Repeated versions reuse conditions.",
            "No authenticated orders, fills, scoring, settlement, or payouts.",
        ],
    }


def _frozen_evidence_set(repo_root: Path, evidence_id: str) -> dict[str, Any]:
    """Read one evidence set from the immutable checked-in seed pack."""

    frozen_pack_path = repo_root / PACK_RELATIVE_PATH
    if not frozen_pack_path.is_file():
        raise FileNotFoundError("missing frozen baseline evidence pack")
    frozen_pack = _read_json(frozen_pack_path)
    frozen_sets = frozen_pack.get("evidence_sets")
    if not isinstance(frozen_sets, list):
        raise ValueError("frozen baseline evidence_sets are invalid")
    result = next(
        (
            json.loads(json.dumps(item))
            for item in frozen_sets
            if isinstance(item, Mapping) and item.get("evidence_id") == evidence_id
        ),
        None,
    )
    if result is None:
        raise ValueError(f"frozen baseline lacks {evidence_id}")
    result.setdefault("details", {})["source_artifacts_missing_from_checkout"] = True
    result["details"]["fallback_source"] = PACK_RELATIVE_PATH.as_posix()
    result["details"]["fallback_source_role"] = "immutable_seed_not_builder_output"
    return result


def build_pack(
    repo_root: Path | str,
    *,
    trade_log_paths: Sequence[Path | str] = (),
) -> dict[str, Any]:
    """Return a deterministic evidence pack for explicitly selected inputs."""

    root = Path(repo_root).resolve()
    if root != _REPO_ROOT.resolve():
        raise ValueError(
            "repo_root must be the same checkout that owns the loaded "
            "baseline evidence methods"
        )
    old_research = root / OLD_RESEARCH_RELATIVE_PATH
    if not old_research.is_dir():
        raise FileNotFoundError(f"missing old research directory: {old_research}")

    json_paths = sorted(old_research.glob("*.json"))
    json_documents = [(path, _read_json(path)) for path in json_paths]
    observe_path = old_research / "singapore_28_observe.jsonl"
    observe_records = _read_jsonl(observe_path)
    log_paths: list[Path] = []
    for supplied_path in trade_log_paths:
        path = Path(supplied_path)
        resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("trade_log_paths must stay within repo_root") from error
        if not resolved.is_file():
            raise FileNotFoundError(f"missing explicit trade log: {resolved}")
        log_paths.append(resolved)
    log_paths.sort()
    frozen_pack_path = root / PACK_RELATIVE_PATH
    frozen_dry_run_evidence: dict[str, Any] | None = None
    if not log_paths:
        frozen_dry_run_evidence = _frozen_evidence_set(
            root,
            "legacy_dry_run_jsonl",
        )
    autoresearch_results_path = root / "autoresearch-results.tsv"
    frozen_autoresearch_evidence = (
        None
        if autoresearch_results_path.is_file()
        else _frozen_evidence_set(
            root,
            "legacy_autoresearch_parameter_search",
        )
    )

    replay_documents = [
        (path, document)
        for path, document in json_documents
        if isinstance(document, dict)
        and isinstance(document.get("samples"), list)
        and document.get("condition_id")
    ]
    complete_set_scans = [
        (path, document)
        for path, document in json_documents
        if path.name.startswith("complete_set_scan_")
    ]
    reward_scans = [
        (path, document)
        for path, document in json_documents
        if path.name.startswith("reward_scan_")
    ]
    current_snapshots = [
        (path, document)
        for path, document in json_documents
        if "_current" in path.stem and path.name != "reward_scan_current.json"
    ]

    evidence_sets = [
        _legacy_mock_evidence(root),
        (
            _legacy_dry_run_evidence(root, log_paths)
            if frozen_dry_run_evidence is None
            else frozen_dry_run_evidence
        ),
        (
            _legacy_autoresearch_evidence(root, autoresearch_results_path)
            if frozen_autoresearch_evidence is None
            else frozen_autoresearch_evidence
        ),
        _public_replay_evidence(root, replay_documents),
        _observe_evidence(root, observe_path, observe_records),
        _complete_set_scan_evidence(root, complete_set_scans),
        _reward_scan_evidence(root, reward_scans),
        _current_snapshot_evidence(root, current_snapshots),
    ]
    source_documents: list[Any] = [document for _, document in json_documents]
    source_documents.extend(observe_records)

    data_paths = [path for path, _ in json_documents]
    data_paths.extend(sorted(old_research.glob("*.md")))
    data_paths.extend([observe_path, *log_paths])
    if autoresearch_results_path.is_file():
        data_paths.append(autoresearch_results_path)
    if (
        frozen_dry_run_evidence is not None
        or frozen_autoresearch_evidence is not None
    ):
        data_paths.append(frozen_pack_path)
    method_paths = [
        root / "scripts/build_baseline_evidence_pack.py",
        root / "scripts/run_backtest.py",
        root / "scripts/autoresearch_verify.py",
        root / "src/backtest/engine.py",
        root / "src/backtest/data.py",
        root / "src/backtest/fee_model.py",
        root / "src/simulator.py",
        root / "src/strategy/backtest_wrapper.py",
        root / "src/strategy/market_maker.py",
        root / "src/strategy/real_data_backtest.py",
        root / "src/telemetry/trade_logger.py",
        root / "research/edge_discovery_2026-07-24/DATA_DICTIONARY.md",
    ]

    return {
        "schema": SCHEMA,
        "pack_version": 1,
        "source_as_of": _latest_source_timestamp(source_documents),
        "generation": {
            "mode": "offline_only",
            "network_access_required": False,
            "live_order_access_required": False,
            "deterministic": True,
            "command": (
                "./.venv/bin/python scripts/build_baseline_evidence_pack.py"
            ),
        },
        "evidence_policy": {
            "data_levels": {
                "L0": "synthetic, fixture, simulator, or diagnostic",
                "L1": "public snapshots, price/history, touch, shadow, or flow proxy",
                "L2": "forward raw L2/WS/RTDS with complete manifests and queue bounds",
                "L3": "authorized small live order/scoring probe",
                "L4": "real low-risk multi-event trading performance",
            },
            "authenticated_fill_definition": (
                "An own order/fill linked to authenticated exchange evidence and "
                "reconcilable through settlement. touch, shadow, public-flow, "
                "and sim_ records do not qualify."
            ),
            "financial_metric_rule": (
                "PnL/return/fees/rewards/drawdown/CI/concentration are null unless "
                "their required cash, fill, settlement, payout, and grouping "
                "evidence is present. Every null includes a reason."
            ),
        },
        "summary": {
            "highest_data_level": "L1",
            "authenticated_fill_count": 0,
            "validated_profitable_evidence_set_count": 0,
            "combined_market_count": None,
            "combined_market_count_reason": (
                "Snapshot scans omit most identities and overlap the seven replay "
                "conditions, so a cross-artifact distinct-market union is impossible."
            ),
            "combined_event_count": None,
            "combined_event_count_reason": (
                "Parent event IDs are absent from the replay, observe, and current "
                "snapshot artifacts."
            ),
            "conclusion": (
                "No old artifact contains fill-backed, settlement-aware, "
                "sample-out-of-sample profitability evidence."
            ),
        },
        "evidence_sets": evidence_sets,
        "source_artifact_manifest": _relative_manifest(
            root, data_paths, kind="source_artifact"
        ),
        "method_source_manifest": _relative_manifest(
            root, method_paths, kind="method_source"
        ),
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.fchmod(handle.fileno(), 0o644)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except PermissionError:
            if os.name != "nt":
                raise
        else:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the offline legacy baseline evidence pack."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--trade-log",
        action="append",
        default=[],
        type=Path,
        help=(
            "Explicit runtime trade JSONL to include; omitted logs are never "
            "discovered from the ambient logs directory"
        ),
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    output = (
        args.output.resolve()
        if args.output is not None
        else repo_root / REBUILT_PACK_RELATIVE_PATH
    )
    seed_path = (repo_root / PACK_RELATIVE_PATH).resolve()
    if output == seed_path:
        raise ValueError(
            "refusing to overwrite the immutable baseline seed pack; choose "
            f"{REBUILT_PACK_RELATIVE_PATH.as_posix()} or --output"
        )
    _write_json_atomic(
        output,
        build_pack(repo_root, trade_log_paths=args.trade_log),
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
