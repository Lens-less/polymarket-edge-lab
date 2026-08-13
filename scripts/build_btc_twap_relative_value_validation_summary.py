#!/usr/bin/env python3
"""Aggregate finalized BTC TWAP pilot reports without inventing evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_relative_value import (  # noqa: E402
    ValidationEvidence,
    evaluate_validation,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402


def _load_verified_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"report must be an object: {path}")
    expected_hash = report.get("report_sha256")
    unhashed = dict(report)
    unhashed.pop("report_sha256", None)
    actual_hash = hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    if expected_hash != actual_hash:
        raise ValueError(f"report hash mismatch: {path}")
    if (
        report.get("paper_only") is not True
        or report.get("new_orders_disabled") is not True
    ):
        raise ValueError(f"report is not fail-closed paper evidence: {path}")
    integrity = report.get("integrity")
    if not isinstance(integrity, dict):
        raise ValueError(f"report has no integrity evidence: {path}")
    for value in integrity.values():
        checks: Iterable[dict[str, Any]]
        if isinstance(value, list):
            checks = (
                item.get("integrity", {})
                for item in value
                if isinstance(item, dict)
            )
        elif isinstance(value, dict):
            checks = (value,)
        else:
            raise ValueError(f"invalid integrity evidence: {path}")
        for check in checks:
            if any(check.get(key) for key in check):
                raise ValueError(f"capture integrity is not clean: {path}")
    return report


def build_summary(report_paths: tuple[Path, ...]) -> dict[str, Any]:
    if not report_paths:
        raise ValueError("at least one pilot report is required")
    resolved_market_ids: set[str] = set()
    market_ids: set[str] = set()
    mechanically_labelable_market_ids: set[str] = set()
    resolution_conflicts = 0
    explainable_trades = 0
    explainable_fills = 0
    raw_shadow_available = 0
    raw_reason_counts: Counter[str] = Counter()
    decision_reason_counts: Counter[str] = Counter()
    decision_evaluations = 0
    no_trade_decisions = 0
    shadow_decisions = 0
    shadow_trades = 0
    shadow_fills = 0
    shadow_complete_cost_trades = 0
    shadow_net_pnls: list[Decimal] = []
    shadow_residual_unhedged_usdc_total = Decimal("0")
    shadow_residual_unhedged_usdc_material_total = Decimal("0")
    shadow_transient_peak_usdc = Decimal("0")
    shadow_transient_duration_ms_total = 0
    shadow_transient_nonzero_trades = 0
    shadow_dust_failed_unhedged_trades = 0
    shadow_material_failed_unhedged_trades = 0
    shadow_action_counts: Counter[str] = Counter()
    shadow_decision_reason_counts: Counter[str] = Counter()
    shadow_cycle_reason_counts: Counter[str] = Counter()
    capture_roots: set[str] = set()
    clob_websocket_errors = 0
    clob_reconnects = 0
    clob_disconnects = 0
    rtds_websocket_errors = 0
    rtds_reconnects = 0
    rtds_disconnects = 0
    recorder_leg_failures = 0
    degraded_capture_cycles = 0
    clob_recorder_legs: list[int] = []
    rtds_recorder_legs: list[int] = []
    clock_required_decisions = 0
    clock_valid_decisions = 0
    clock_measurement_ages_ms: list[int] = []
    clock_uncertainties_ms: list[int] = []
    latest_clock_measurement_raw_ms = -1
    latest_clock_offset_seconds: str | None = None
    input_rows: list[dict[str, str]] = []
    complete_cost_model = True
    decision_keys: set[tuple[str, int]] = set()
    preregistration_sha256: str | None = None

    for path in report_paths:
        resolved_path = path.resolve()
        report = _load_verified_report(resolved_path)
        report_inputs = report.get("inputs")
        capture_root = (
            report_inputs.get("capture_root")
            if isinstance(report_inputs, dict)
            else None
        )
        if not isinstance(capture_root, str) or not capture_root:
            raise ValueError(f"report has no capture-root identity: {resolved_path}")
        report_preregistration_sha256 = report_inputs.get(
            "preregistration_sha256"
        )
        if (
            not isinstance(report_preregistration_sha256, str)
            or len(report_preregistration_sha256) != 64
        ):
            raise ValueError(
                f"report has no preregistration identity: {resolved_path}"
            )
        if preregistration_sha256 is None:
            preregistration_sha256 = report_preregistration_sha256
        elif preregistration_sha256 != report_preregistration_sha256:
            raise ValueError("mixed preregistration reports are not comparable")
        decision_tau_seconds = report_inputs.get("decision_tau_seconds")
        if isinstance(decision_tau_seconds, bool) or not isinstance(
            decision_tau_seconds, int
        ):
            raise ValueError(f"report has no decision-tick identity: {resolved_path}")
        decision_key = (capture_root, decision_tau_seconds)
        if decision_key in decision_keys:
            raise ValueError(f"duplicate decision report: {decision_key}")
        decision_keys.add(decision_key)
        first_report_for_capture = capture_root not in capture_roots
        capture_roots.add(capture_root)
        input_rows.append(
            {
                "path": str(resolved_path),
                "report_sha256": str(report["report_sha256"]),
            }
        )
        observed = report["observed"]
        clock_sync = observed.get("clock_sync")
        if isinstance(clock_sync, dict) and clock_sync.get("required") is True:
            clock_required_decisions += 1
            clock_valid_decisions += int(
                clock_sync.get("valid_for_decision") is True
            )
            age = clock_sync.get("measurement_age_ms")
            if isinstance(age, int) and not isinstance(age, bool) and age >= 0:
                clock_measurement_ages_ms.append(age)
            clock_evidence = clock_sync.get("evidence")
            if isinstance(clock_evidence, dict):
                uncertainty = clock_evidence.get("uncertainty_ms")
                if (
                    isinstance(uncertainty, int)
                    and not isinstance(uncertainty, bool)
                    and uncertainty >= 0
                ):
                    clock_uncertainties_ms.append(uncertainty)
                measured_at = clock_evidence.get("measured_at_raw_ms", 0)
                if (
                    isinstance(measured_at, int)
                    and not isinstance(measured_at, bool)
                    and measured_at >= latest_clock_measurement_raw_ms
                ):
                    latest_clock_measurement_raw_ms = measured_at
                    offset = clock_evidence.get("offset_seconds")
                    latest_clock_offset_seconds = (
                        str(offset) if offset is not None else None
                    )
        if first_report_for_capture:
            clob_counts = observed.get("clob_event_counts", {})
            if not isinstance(clob_counts, dict):
                raise ValueError(
                    f"report has invalid CLOB event counts: {resolved_path}"
                )
            clob_websocket_errors += int(clob_counts.get("error", 0))
            clob_reconnects += int(clob_counts.get("reconnect_scheduled", 0))
            clob_disconnects += int(clob_counts.get("disconnected", 0))
            rtds_counts = observed.get("rtds_event_counts", {})
            if not isinstance(rtds_counts, dict):
                raise ValueError(
                    f"report has invalid RTDS event counts: {resolved_path}"
                )
            rtds_websocket_errors += int(rtds_counts.get("error", 0))
            rtds_reconnects += int(rtds_counts.get("reconnect_scheduled", 0))
            rtds_disconnects += int(rtds_counts.get("disconnected", 0))
            capture_runtime = observed.get("capture_runtime")
            if capture_runtime is not None:
                if not isinstance(capture_runtime, dict):
                    raise ValueError(
                        f"report has invalid capture runtime: {resolved_path}"
                    )
                failures = capture_runtime.get("recorder_leg_failures")
                redundancy = capture_runtime.get("websocket_redundancy")
                if not isinstance(failures, list) or not isinstance(redundancy, dict):
                    raise ValueError(
                        f"report has incomplete capture runtime: {resolved_path}"
                    )
                recorder_leg_failures += len(failures)
                degraded_capture_cycles += int(bool(failures))
                clob_legs = redundancy.get("clob_market_ws")
                rtds_legs = redundancy.get("rtds_ws")
                if (
                    isinstance(clob_legs, bool)
                    or not isinstance(clob_legs, int)
                    or clob_legs < 1
                    or isinstance(rtds_legs, bool)
                    or not isinstance(rtds_legs, int)
                    or rtds_legs < 1
                ):
                    raise ValueError(
                        f"report has invalid websocket redundancy: {resolved_path}"
                    )
                clob_recorder_legs.append(clob_legs)
                rtds_recorder_legs.append(rtds_legs)
        rules = observed["latest_rules"]
        for market_id, rule in rules.items():
            market_ids.add(str(market_id))
            if rule.get("resolution_event_valid") is True:
                resolved_market_ids.add(str(market_id))
            fee = rule.get("fee_schedule")
            if (
                rule.get("present") is not True
                or not isinstance(fee, dict)
                or Decimal(str(fee.get("rate"))) != Decimal("0.07")
                or Decimal(str(fee.get("exponent"))) != Decimal("1")
                or fee.get("takerOnly") is not True
            ):
                complete_cost_model = False
        boundaries = observed.get("boundaries")
        if not isinstance(boundaries, dict):
            raise ValueError(f"report has no boundary evidence: {resolved_path}")
        for boundary in boundaries.values():
            if not isinstance(boundary, dict):
                raise ValueError(f"report boundary is malformed: {resolved_path}")
            market_id = boundary.get("market_id")
            if not isinstance(market_id, str) or market_id not in rules:
                raise ValueError(
                    f"report boundary lacks an exact market mapping: {resolved_path}"
                )
            if (
                boundary.get("mechanical_outcome") in {"Up", "Down"}
                and rules[market_id].get("rules_match_capture") is True
                and int(observed.get("predictor_one_second_samples", 0)) >= 60
                and isinstance(observed.get("book_replay_coverage"), dict)
                and observed["book_replay_coverage"].get(
                    "complete_four_token_signal_surface"
                )
                is True
                and observed["book_replay_coverage"].get(
                    "complete_four_token_delayed_execution_surface"
                )
                is True
            ):
                mechanically_labelable_market_ids.add(market_id)
        resolution_conflicts += len(observed["resolution_conflicts"])
        economic = report["economic_evidence"]
        explainable_trades += int(economic["explainable_simulated_trades"])
        explainable_fills += int(economic["explainable_fills"])
        if economic.get("observed_explainable_net_pnl") is not None:
            raise ValueError(
                "pilot summary cannot aggregate PnL without per-trade explainable rows"
            )
        if economic.get("development_shadow_is_qualified_evidence") is True:
            raise ValueError("development shadow cannot be qualified evidence")
        shadow_decisions += int(economic.get("development_shadow_decisions", 0))
        shadow_trades += int(economic.get("development_shadow_trades", 0))
        shadow_fills += int(economic.get("development_shadow_fills", 0))
        shadow_complete_cost_trades += int(
            economic.get("development_shadow_complete_taker_cost_model") is True
        )
        diagnostics = economic.get("development_shadow_execution_diagnostics")
        if isinstance(diagnostics, dict):
            residual_unhedged_usdc = Decimal(
                str(diagnostics.get("residual_unhedged_usdc", "0"))
            )
            if not residual_unhedged_usdc.is_finite():
                raise ValueError("development shadow residual_unhedged_usdc must be finite")
            shadow_residual_unhedged_usdc_total += residual_unhedged_usdc
            if diagnostics.get("material_failed_unhedged") is True:
                shadow_residual_unhedged_usdc_material_total += residual_unhedged_usdc
                shadow_material_failed_unhedged_trades += 1
            if diagnostics.get("dust_failed_unhedged") is True:
                shadow_dust_failed_unhedged_trades += 1
            transient_peak_usdc = Decimal(
                str(diagnostics.get("transient_naked_exposure_peak_usdc", "0"))
            )
            if not transient_peak_usdc.is_finite():
                raise ValueError(
                    "development shadow transient_naked_exposure_peak_usdc must be finite"
                )
            shadow_transient_peak_usdc = max(
                shadow_transient_peak_usdc,
                transient_peak_usdc,
            )
            transient_duration_ms = diagnostics.get(
                "transient_naked_exposure_duration_ms",
                0,
            )
            if (
                isinstance(transient_duration_ms, bool)
                or not isinstance(transient_duration_ms, int)
                or transient_duration_ms < 0
            ):
                raise ValueError(
                    "development shadow transient_naked_exposure_duration_ms is invalid"
                )
            shadow_transient_duration_ms_total += transient_duration_ms
            shadow_transient_nonzero_trades += int(transient_peak_usdc > 0)
        shadow_net = economic.get("development_shadow_net_pnl")
        if shadow_net is not None:
            parsed_shadow_net = Decimal(str(shadow_net))
            if not parsed_shadow_net.is_finite():
                raise ValueError("development shadow PnL must be finite")
            shadow_net_pnls.append(parsed_shadow_net)
        shadow_cycle = report.get("development_shadow_cycle")
        if isinstance(shadow_cycle, dict):
            shadow_decision = shadow_cycle.get("decision")
            if isinstance(shadow_decision, dict):
                action = str(shadow_decision.get("action") or "unknown")
                shadow_action_counts[action] += 1
                for reason in shadow_decision.get("reason_codes", ()):
                    shadow_decision_reason_counts[str(reason)] += 1
            for reason in shadow_cycle.get("reason_codes", ()):
                shadow_cycle_reason_counts[str(reason)] += 1
        raw_shadow = report["raw_shadow_model"]
        raw_shadow_available += int(raw_shadow.get("available") is True)
        for reason in raw_shadow.get("reason_codes", ()):  # old reports may omit it
            raw_reason_counts[str(reason)] += 1
        paper_decision = report.get("paper_decision")
        if isinstance(paper_decision, dict) and paper_decision.get("evaluated") is True:
            decision_evaluations += 1
            no_trade_decisions += int(paper_decision.get("action") == "no_trade")
            for reason in paper_decision.get("reason_codes", ()):
                decision_reason_counts[str(reason)] += 1

    expected_markets = len(market_ids)
    mechanically_labelable = len(mechanically_labelable_market_ids)
    if mechanically_labelable > expected_markets:
        raise ValueError("mechanically labelable market count exceeds unique markets")
    evidence = ValidationEvidence(
        resolved_current_regime_markets=len(resolved_market_ids),
        expected_current_regime_markets=expected_markets,
        markets_with_complete_capture=mechanically_labelable,
        unknown_resolution_mapping_count=(
            expected_markets - mechanically_labelable + resolution_conflicts
        ),
        explainable_simulated_trades=explainable_trades,
        explainable_fills=explainable_fills,
        explainable_net_pnls=(),
        chronological_oos_complete=False,
        # Metadata proves the current fee parameters, not that costs were
        # applied to every delayed/depth-walked execution row.
        complete_taker_cost_model=False,
        delay_depth_and_legging_replay_complete=False,
        bootstrap_net_pnl_lower_95=None,
        oos_brier_5=None,
        oos_brier_15=None,
        market_brier_5=None,
        market_brier_15=None,
        oos_expected_calibration_error_5=None,
        oos_expected_calibration_error_15=None,
        maximum_single_event_pnl_share=None,
        direction_exposure_below_single_leg=None,
        signal_strength_net_ev_monotonic=None,
    )
    validation = evaluate_validation(evidence)
    shadow_winners = sum(value > 0 for value in shadow_net_pnls)
    shadow_losers = sum(value < 0 for value in shadow_net_pnls)
    shadow_breakeven = sum(value == 0 for value in shadow_net_pnls)
    shadow_positive = sum(
        (value for value in shadow_net_pnls if value > 0), Decimal("0")
    )
    shadow_negative = sum(
        (-value for value in shadow_net_pnls if value < 0), Decimal("0")
    )
    shadow_net_total = (
        sum(shadow_net_pnls, Decimal("0")) if shadow_net_pnls else None
    )
    summary: dict[str, Any] = {
        "schema_version": "btc-5m-15m-relative-value-validation-summary.v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "paper_only": True,
        "new_orders_disabled": True,
        "classification": validation.status.value,
        "qualified_net_pnl": (
            str(validation.qualified_net_pnl)
            if validation.qualified_net_pnl is not None
            else None
        ),
        "development_shadow": {
            "qualified_evidence": False,
            "decisions": shadow_decisions,
            "no_trade_decisions": shadow_action_counts["no_trade"],
            "action_counts": dict(sorted(shadow_action_counts.items())),
            "decision_reason_counts": dict(
                sorted(shadow_decision_reason_counts.items())
            ),
            "cycle_reason_counts": dict(sorted(shadow_cycle_reason_counts.items())),
            "trades": shadow_trades,
            "fills": shadow_fills,
            "settled_trades": len(shadow_net_pnls),
            "pending_trades": max(0, shadow_trades - len(shadow_net_pnls)),
            "complete_taker_cost_model_trades": shadow_complete_cost_trades,
            "net_pnl": (
                str(shadow_net_total) if shadow_net_total is not None else None
            ),
            "average_net_pnl": (
                str(shadow_net_total / Decimal(len(shadow_net_pnls)))
                if shadow_net_total is not None
                else None
            ),
            "winning_trades": shadow_winners,
            "losing_trades": shadow_losers,
            "breakeven_trades": shadow_breakeven,
            "win_rate": (
                str(Decimal(shadow_winners) / Decimal(len(shadow_net_pnls)))
                if shadow_net_pnls
                else None
            ),
            "profit_factor": (
                str(shadow_positive / shadow_negative)
                if shadow_negative > 0
                else None
            ),
            "execution_diagnostics": {
                "dust_failed_unhedged_trades": shadow_dust_failed_unhedged_trades,
                "material_failed_unhedged_trades": (
                    shadow_material_failed_unhedged_trades
                ),
                "residual_unhedged_usdc_total": str(
                    shadow_residual_unhedged_usdc_total
                ),
                "residual_unhedged_usdc_material_total": str(
                    shadow_residual_unhedged_usdc_material_total
                ),
                "transient_naked_exposure_peak_usdc": str(
                    shadow_transient_peak_usdc
                ),
                "transient_naked_exposure_total_duration_ms": (
                    shadow_transient_duration_ms_total
                ),
                "transient_naked_exposure_trades": shadow_transient_nonzero_trades,
            },
        },
        "reason_codes": list(validation.reason_codes),
        "inputs": input_rows,
        "preregistration_sha256": preregistration_sha256,
        "evidence": {
            "capture_cycles": len(capture_roots),
            "decision_reports": len(report_paths),
            "clob_websocket_errors": clob_websocket_errors,
            "clob_reconnects": clob_reconnects,
            "clob_disconnects": clob_disconnects,
            "rtds_websocket_errors": rtds_websocket_errors,
            "rtds_reconnects": rtds_reconnects,
            "rtds_disconnects": rtds_disconnects,
            "recorder_leg_failures": recorder_leg_failures,
            "degraded_capture_cycles": degraded_capture_cycles,
            "minimum_clob_recorder_legs": (
                min(clob_recorder_legs) if clob_recorder_legs else None
            ),
            "minimum_rtds_recorder_legs": (
                min(rtds_recorder_legs) if rtds_recorder_legs else None
            ),
            "clock_sync": {
                "required_decisions": clock_required_decisions,
                "valid_decisions": clock_valid_decisions,
                "invalid_decisions": (
                    clock_required_decisions - clock_valid_decisions
                ),
                "maximum_measurement_age_ms": (
                    max(clock_measurement_ages_ms)
                    if clock_measurement_ages_ms
                    else None
                ),
                "maximum_uncertainty_ms": (
                    max(clock_uncertainties_ms)
                    if clock_uncertainties_ms
                    else None
                ),
                "latest_offset_seconds": latest_clock_offset_seconds,
            },
            "unique_markets": expected_markets,
            "officially_resolved_markets": len(resolved_market_ids),
            "mechanically_labelable_markets": mechanically_labelable,
            "market_coverage": (
                str(validation.market_coverage)
                if validation.market_coverage is not None
                else None
            ),
            "resolution_conflicts": resolution_conflicts,
            "raw_shadow_models_available": raw_shadow_available,
            "raw_shadow_blocker_counts": dict(sorted(raw_reason_counts.items())),
            "paper_decision_evaluations": decision_evaluations,
            "paper_no_trade_decisions": no_trade_decisions,
            "paper_decision_reason_counts": dict(
                sorted(decision_reason_counts.items())
            ),
            "explainable_simulated_trades": explainable_trades,
            "explainable_fills": explainable_fills,
            "observed_explainable_net_pnl": None,
            "complete_current_taker_fee_metadata": complete_cost_model,
        },
        "promotion_gaps": {
            "resolved_markets_remaining": max(
                0, validation.minimum_resolved_markets - len(resolved_market_ids)
            ),
            "simulated_trades_remaining": max(
                0, validation.minimum_simulated_trades - explainable_trades
            ),
            "explainable_fills_remaining": max(
                0, validation.minimum_explainable_fills - explainable_fills
            ),
        },
        "conclusion": (
            "No profitability claim is permitted; preliminary development-shadow "
            f"net PnL is {shadow_net_total}, but qualified/OOS PnL remains null."
            if shadow_net_total is not None
            else "No profitability claim is permitted: economic evidence is missing, "
            "so PnL remains null rather than zero."
        ),
    }
    summary["summary_sha256"] = hashlib.sha256(
        canonical_json_bytes(summary)
    ).hexdigest()
    return summary


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(payload) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summary = build_summary(tuple(args.report))
    _atomic_json(args.output.resolve(), summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
