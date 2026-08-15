#!/usr/bin/env python3
"""Aggregate non-canonical candidate evidence without changing paper actions."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.edge_lab.data_store import canonical_json_bytes

ZERO = Decimal(0)
EXTREME_BOUND = Decimal("0.05")
LOSS_PROBABILITY_MAX = Decimal("0.45")
SHRINKAGE_LAMBDAS = (
    Decimal(0),
    Decimal("0.25"),
    Decimal("0.5"),
    Decimal("0.75"),
    Decimal(1),
)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return parsed


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _load_report(path: Path) -> Mapping[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, Mapping):
        raise TypeError(f"report is not an object: {path}")
    unsigned = dict(report)
    claimed = unsigned.pop("report_sha256", None)
    if claimed != hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest():
        raise ValueError(f"report hash mismatch: {path}")
    return report


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _row(path: Path) -> dict[str, Any] | None:
    report = _load_report(path)
    cycle = _mapping(report.get("development_shadow_cycle"))
    decision = _mapping(cycle.get("decision"))
    if not decision:
        return None
    action = decision.get("action")
    trade = action not in {None, "no_trade"}
    execution = _mapping(cycle.get("execution"))
    diagnostics = _mapping(execution.get("diagnostics"))
    settlement = _mapping(cycle.get("settlement"))
    pnl = (
        _decimal(settlement.get("net_pnl"))
        if settlement.get("explainable") is True
        else None
    )
    candidate = _mapping(report.get("candidate_hypotheses"))
    probability = _mapping(candidate.get("probability"))
    depth = _mapping(candidate.get("depth_buffer"))
    controls = _mapping(candidate.get("existing_canonical_controls"))
    return {
        "path": str(path),
        "trade": trade,
        "settled": pnl is not None,
        "net_pnl": pnl,
        "q_5_raw": _decimal(decision.get("q_5_raw")),
        "q_15_raw": _decimal(decision.get("q_15_raw")),
        "loss_probability": _decimal(decision.get("loss_probability")),
        "depth_passes": dict(_mapping(depth.get("passes"))),
        "candidate_available": candidate.get("available") is True,
        "model_probability_up": dict(
            _mapping(probability.get("model_probability_up"))
        ),
        "market_probability_up": dict(
            _mapping(probability.get("market_probability_up"))
        ),
        "actual_up": dict(_mapping(probability.get("actual_up"))),
        "controls": dict(controls),
        "material_second_leg_failure": diagnostics.get(
            "material_second_leg_failure"
        )
        is True,
        "residual_unhedged_max_loss_usdc": _decimal(
            diagnostics.get("residual_unhedged_max_loss_usdc")
        ),
        "transient_peak_usdc": _decimal(
            diagnostics.get("transient_naked_exposure_peak_usdc")
        ),
        "transient_duration_ms": diagnostics.get(
            "transient_naked_exposure_duration_ms"
        ),
    }


def _candidate_keep(candidate_id: str, row: Mapping[str, Any]) -> bool | None:
    if not row.get("trade"):
        return False
    if candidate_id == "baseline_shadow":
        return True
    if candidate_id == "a1_extreme_probability_veto":
        probabilities = (row.get("q_5_raw"), row.get("q_15_raw"))
        return all(
            isinstance(probability, Decimal)
            and EXTREME_BOUND <= probability <= Decimal(1) - EXTREME_BOUND
            for probability in probabilities
        )
    if candidate_id == "a4_loss_probability_veto":
        probability = row.get("loss_probability")
        return (
            isinstance(probability, Decimal)
            and probability <= LOSS_PROBABILITY_MAX
        )
    if candidate_id.startswith("b3_signal_depth_buffer_"):
        threshold = candidate_id.removeprefix("b3_signal_depth_buffer_").replace(
            "_", "."
        )
        value = _mapping(row.get("depth_passes")).get(threshold)
        return value if isinstance(value, bool) else None
    raise ValueError(f"unknown candidate: {candidate_id}")


def _candidate_summary(
    candidate_id: str, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    kept_trades = filtered_trades = evaluable = unavailable = kept_settled = 0
    kept_net = filtered_positive = filtered_negative = ZERO
    for row in rows:
        if not row.get("trade"):
            continue
        keep = _candidate_keep(candidate_id, row)
        if keep is None:
            unavailable += 1
            continue
        evaluable += 1
        pnl = row.get("net_pnl")
        if keep:
            kept_trades += 1
            if isinstance(pnl, Decimal):
                kept_settled += 1
                kept_net += pnl
        else:
            filtered_trades += 1
            if isinstance(pnl, Decimal):
                if pnl >= ZERO:
                    filtered_positive += pnl
                else:
                    filtered_negative += pnl
    return {
        "evaluable_trades": evaluable,
        "unavailable_trades": unavailable,
        "kept_trades": kept_trades,
        "filtered_trades": filtered_trades,
        "kept_settled_trades": kept_settled,
        "kept_net_pnl": _text(kept_net),
        "filtered_positive_pnl": _text(filtered_positive),
        "filtered_negative_pnl": _text(filtered_negative),
    }


def _probability_shrinkage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for shrinkage in SHRINKAGE_LAMBDAS:
        by_horizon: dict[str, Any] = {}
        for horizon in ("5m", "15m"):
            errors: list[Decimal] = []
            for row in rows:
                model = _decimal(_mapping(row.get("model_probability_up")).get(horizon))
                market = _decimal(_mapping(row.get("market_probability_up")).get(horizon))
                outcome = _mapping(row.get("actual_up")).get(horizon)
                if model is None or market is None or not isinstance(outcome, bool):
                    continue
                probability = shrinkage * model + (Decimal(1) - shrinkage) * market
                errors.append((probability - Decimal(outcome)) ** 2)
            by_horizon[horizon] = {
                "sample_count": len(errors),
                "brier": _text(sum(errors, ZERO) / Decimal(len(errors))) if errors else None,
            }
        result[_text(shrinkage)] = by_horizon
    return {
        "selection_policy": "train_only_then_freeze_lambda_before_oos_use",
        "lambda_definition": "q_prime=lambda*q_model+(1-lambda)*q_market",
        "grid": result,
    }


def _execution_control_audit(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    metadata_rows = [row for row in rows if row.get("candidate_available")]
    failures = [row for row in rows if row.get("material_second_leg_failure")]
    material_residuals = [
        row
        for row in failures
        if isinstance(row.get("residual_unhedged_max_loss_usdc"), Decimal)
        and row["residual_unhedged_max_loss_usdc"] >= Decimal("0.01")
    ]
    return {
        "reports_with_candidate_metadata": len(metadata_rows),
        "canonical_signal_walks_both_legs_to_full_target": all(
            _mapping(row.get("controls")).get(
                "signal_walks_both_legs_to_full_target"
            )
            is True
            for row in metadata_rows
        )
        if metadata_rows
        else None,
        "canonical_timeout_then_immediate_unwind": all(
            _mapping(row.get("controls")).get("timeout_then_immediate_unwind")
            is True
            for row in metadata_rows
        )
        if metadata_rows
        else None,
        "material_second_leg_failures": len(failures),
        "failures_with_residual_max_loss_at_least_0_01_usdc": len(
            material_residuals
        ),
        "interpretation": (
            "B1 dual-depth walking and B2 timeout/unwind are existing canonical "
            "controls; evaluate depth buffers and economically material residuals "
            "instead of treating any dust duration as full naked exposure."
        ),
    }


def build_candidate_scoreboard(
    report_paths: Iterable[Path],
    *,
    candidate_ids: Sequence[str] = (
        "baseline_shadow",
        "a1_extreme_probability_veto",
        "a4_loss_probability_veto",
        "b3_signal_depth_buffer_1_25",
        "b3_signal_depth_buffer_1_5",
        "b3_signal_depth_buffer_2",
    ),
) -> dict[str, Any]:
    rows = [
        row
        for path in sorted(report_paths)
        if (row := _row(path)) is not None
    ]
    scoreboard: dict[str, Any] = {
        "schema_version": "btc-regime-candidate-scoreboard.v2",
        "non_canonical": True,
        "reports_considered": len(rows),
        "candidates": {
            candidate_id: _candidate_summary(candidate_id, rows)
            for candidate_id in candidate_ids
        },
        "probability_shrinkage": _probability_shrinkage(rows),
        "execution_control_audit": _execution_control_audit(rows),
        "limitations": [
            "Candidate outputs never alter the canonical v0.6 action.",
            "Depth buffers are unavailable for reports created before candidate metadata v1.",
            "B2 unwind PnL cannot be reconstructed from outcome-only reports without lookahead.",
        ],
    }
    scoreboard["scoreboard_sha256"] = hashlib.sha256(
        canonical_json_bytes(scoreboard)
    ).hexdigest()
    return scoreboard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scoreboard = build_candidate_scoreboard(args.reports_dir.glob("*.json"))
    encoded = json.dumps(scoreboard, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
