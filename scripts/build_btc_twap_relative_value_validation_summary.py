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
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_relative_value import (  # noqa: E402
    ValidationEvidence,
    evaluate_validation,
)
from src.edge_lab.btc_twap_relative_value_oos_metrics import (  # noqa: E402
    OOSForecastRow,
    OOSTradeRow,
    bootstrap_cluster_mean_lower_95,
    brier_score,
    direction_exposure_below_single_leg,
    expected_calibration_error,
    maximum_absolute_event_contribution_share,
    signal_strength_net_pnl_monotonic,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402

_REPORT_SCHEMA_VERSION = "btc-5m-15m-relative-value-pilot-report.v2"
_VERIFICATION_VERSION = "v2"
_HORIZONS = ("5m", "15m")


def _require_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be bool")
    return value


def _require_non_negative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{label} must be decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


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
    schema_version = report.get("schema_version")
    verification = report.get("verification")
    if (
        schema_version == _REPORT_SCHEMA_VERSION
        or report.get("verified_report_v2") is not None
        or verification is not None
    ):
        if schema_version != _REPORT_SCHEMA_VERSION:
            raise ValueError(
                f"legacy or unsupported report schema {schema_version}: {path}"
            )
        if (
            _require_bool(report.get("verified_report_v2"), label="verified_report_v2")
            is not True
        ):
            raise ValueError(f"report must be verified_report_v2=true: {path}")
        verification_mapping = _require_mapping(
            verification,
            label="verification",
        )
        if (
            _require_bool(
                verification_mapping.get("verified"),
                label="verification.verified",
            )
            is not True
        ):
            raise ValueError(f"report verification must be true: {path}")
        if (
            _require_string(
                verification_mapping.get("verification_version"),
                label="verification.verification_version",
            )
            != _VERIFICATION_VERSION
        ):
            raise ValueError(f"legacy verification version: {path}")
        track = _require_string(
            verification_mapping.get("evidence_track"),
            label="verification.evidence_track",
        )
        inputs = _require_mapping(report.get("inputs"), label="inputs")
        if (
            _require_string(
                inputs.get("evidence_track"),
                label="inputs.evidence_track",
            )
            != track
        ):
            raise ValueError(f"mixed track evidence is not allowed: {path}")
        preregistration_sha256 = _require_string(
            inputs.get("preregistration_sha256"),
            label="inputs.preregistration_sha256",
        )
        if len(preregistration_sha256) != 64:
            raise ValueError(f"inputs.preregistration_sha256 must be sha256: {path}")
        _require_non_negative_int(
            inputs.get("decision_tau_seconds"),
            label="inputs.decision_tau_seconds",
        )
        if (
            report.get("public_only") is not True
            or report.get("orders_submitted") != 0
            or report.get("authenticated_endpoints_used") != 0
        ):
            raise ValueError(f"v2 report violates public paper guards: {path}")
    integrity = report.get("integrity")
    if not isinstance(integrity, dict):
        raise ValueError(f"report has no integrity evidence: {path}")
    for value in integrity.values():
        checks: Iterable[dict[str, Any]]
        if isinstance(value, list):
            checks = (
                item.get("integrity", {}) for item in value if isinstance(item, dict)
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
    decision_keys: set[tuple[int | str, int]] = set()
    preregistration_sha256: str | None = None
    evidence_track: str | None = None
    oos_forecasts: list[OOSForecastRow] = []
    oos_forecast_available_reports = 0
    oos_forecast_unavailable_reason_counts: Counter[str] = Counter()
    qualified_explainable_net_pnls: list[Decimal] = []
    qualified_explainable_fills = 0
    qualified_economic_attempts = 0
    qualified_complete_taker_cost_model = True
    qualified_delay_depth_and_legging_replay_complete = True
    qualified_trade_rows: list[OOSTradeRow] = []
    qualified_trade_metrics_complete = True
    qualified_available_reports = 0
    qualified_oos_missing_reports = 0
    saw_v2_report = False
    saw_legacy_report = False

    for path in report_paths:
        resolved_path = path.resolve()
        report = _load_verified_report(resolved_path)
        is_v2_report = report.get("verified_report_v2") is True
        saw_v2_report = saw_v2_report or is_v2_report
        saw_legacy_report = saw_legacy_report or not is_v2_report
        if saw_v2_report and saw_legacy_report:
            raise ValueError("legacy and v2 reports cannot be mixed in one summary")
        report_inputs = _require_mapping(report.get("inputs"), label="inputs")
        capture_root = report_inputs.get("capture_root")
        if capture_root is not None and (
            not isinstance(capture_root, str) or not capture_root
        ):
            raise ValueError(
                f"report has invalid capture-root identity: {resolved_path}"
            )
        report_preregistration_sha256 = report_inputs.get("preregistration_sha256")
        if (
            not isinstance(report_preregistration_sha256, str)
            or len(report_preregistration_sha256) != 64
        ):
            raise ValueError(f"report has no preregistration identity: {resolved_path}")
        if preregistration_sha256 is None:
            preregistration_sha256 = report_preregistration_sha256
        elif preregistration_sha256 != report_preregistration_sha256:
            raise ValueError("mixed preregistration reports are not comparable")
        report_evidence_track = report_inputs.get("evidence_track")
        if report.get("verified_report_v2") is True:
            if not isinstance(report_evidence_track, str) or not report_evidence_track:
                raise ValueError(
                    f"report has no evidence-track identity: {resolved_path}"
                )
            if evidence_track is None:
                evidence_track = report_evidence_track
            elif evidence_track != report_evidence_track:
                raise ValueError("mixed track evidence is not allowed in summary")
        decision_tau_seconds = report_inputs.get("decision_tau_seconds")
        if isinstance(decision_tau_seconds, bool) or not isinstance(
            decision_tau_seconds, int
        ):
            raise ValueError(f"report has no decision-tick identity: {resolved_path}")
        report_decision_at_ms = report_inputs.get("decision_at_ms")
        if report_decision_at_ms is None:
            if not isinstance(capture_root, str) or not capture_root:
                raise ValueError(
                    f"report has no decision identity fallback: {resolved_path}"
                )
            decision_identity: int | str = capture_root
        else:
            if isinstance(report_decision_at_ms, bool) or not isinstance(
                report_decision_at_ms, int
            ):
                raise ValueError(f"report has invalid decision_at_ms: {resolved_path}")
            decision_identity = report_decision_at_ms
        decision_key = (decision_identity, decision_tau_seconds)
        if decision_key in decision_keys:
            raise ValueError(f"duplicate decision report: {decision_key}")
        decision_keys.add(decision_key)
        first_report_for_capture = (
            isinstance(capture_root, str) and capture_root not in capture_roots
        )
        if isinstance(capture_root, str):
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
            clock_valid_decisions += int(clock_sync.get("valid_for_decision") is True)
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
                raise ValueError(
                    "development shadow residual_unhedged_usdc must be finite"
                )
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
                    "development shadow transient_naked_exposure_peak_usdc "
                    "must be finite"
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

        oos_forecast = report.get("oos_forecast")
        if isinstance(oos_forecast, Mapping):
            if oos_forecast.get("available") is True:
                if oos_forecast.get("split") != "test":
                    raise ValueError(
                        f"available oos_forecast must be split=test: {resolved_path}"
                    )
                oos_event_cluster_id = _require_string(
                    oos_forecast.get("event_cluster_id"),
                    label="oos_forecast.event_cluster_id",
                )
                oos_tau = oos_forecast.get("decision_tau_seconds")
                if oos_tau is not None and oos_tau != decision_tau_seconds:
                    raise ValueError(
                        f"oos_forecast.decision_tau_seconds mismatch: {resolved_path}"
                    )
                model_probability_up = _require_mapping(
                    oos_forecast.get("model_probability_up"),
                    label="oos_forecast.model_probability_up",
                )
                market_probability_up = _require_mapping(
                    oos_forecast.get("market_probability_up"),
                    label="oos_forecast.market_probability_up",
                )
                actual_up = _require_mapping(
                    oos_forecast.get("actual_up"),
                    label="oos_forecast.actual_up",
                )
                local_label_available_at_ms = _require_non_negative_int(
                    oos_forecast.get("local_label_available_at_ms"),
                    label="oos_forecast.local_label_available_at_ms",
                )
                if (
                    isinstance(report_decision_at_ms, int)
                    and local_label_available_at_ms <= report_decision_at_ms
                ):
                    raise ValueError(
                        f"oos_forecast label is not post-decision: {resolved_path}"
                    )
                for horizon in _HORIZONS:
                    oos_forecasts.append(
                        OOSForecastRow(
                            horizon=horizon,
                            event_cluster_id=oos_event_cluster_id,
                            model_probability_up=_require_decimal(
                                model_probability_up.get(horizon),
                                label=f"oos_forecast.model_probability_up.{horizon}",
                            ),
                            market_probability_up=_require_decimal(
                                market_probability_up.get(horizon),
                                label=f"oos_forecast.market_probability_up.{horizon}",
                            ),
                            actual_up=_require_bool(
                                actual_up.get(horizon),
                                label=f"oos_forecast.actual_up.{horizon}",
                            ),
                            decision_tau_seconds=decision_tau_seconds,
                        )
                    )
                oos_forecast_available_reports += 1
            else:
                for reason in oos_forecast.get("reason_codes", ()):
                    oos_forecast_unavailable_reason_counts[str(reason)] += 1

        qualified_evidence = report.get("qualified_evidence")
        qualified_cycle = report.get("qualified_cycle")
        if (
            isinstance(qualified_cycle, Mapping)
            and qualified_cycle.get("available") is True
        ):
            qualified_available_reports += 1
            if not (
                isinstance(oos_forecast, Mapping)
                and oos_forecast.get("available") is True
            ):
                qualified_oos_missing_reports += 1
            if qualified_cycle.get("track") != report_evidence_track:
                raise ValueError(
                    "qualified cycle track does not match report track: "
                    f"{resolved_path}"
                )
        if isinstance(qualified_evidence, Mapping):
            economic_attempt = qualified_evidence.get("economic_attempt") is True
            if economic_attempt:
                qualified_economic_attempts += 1
                fills = qualified_evidence.get("explainable_fills")
                qualified_explainable_fills += _require_non_negative_int(
                    fills,
                    label="qualified_evidence.explainable_fills",
                )
                qualified_complete_taker_cost_model = (
                    qualified_complete_taker_cost_model
                    and qualified_evidence.get("complete_taker_cost_model") is True
                )
                qualified_delay_depth_and_legging_replay_complete = (
                    qualified_delay_depth_and_legging_replay_complete
                    and qualified_evidence.get(
                        "delay_depth_and_legging_replay_complete"
                    )
                    is True
                )
                explainable_net_pnl = qualified_evidence.get("explainable_net_pnl")
                if explainable_net_pnl is not None:
                    parsed_qualified_net_pnl = _require_decimal(
                        explainable_net_pnl,
                        label="qualified_evidence.explainable_net_pnl",
                    )
                    qualified_explainable_net_pnls.append(parsed_qualified_net_pnl)
                    qualified_cycle_mapping = _require_mapping(
                        qualified_cycle,
                        label="qualified_cycle",
                    )
                    decision = _require_mapping(
                        qualified_cycle_mapping.get("decision"),
                        label="qualified_cycle.decision",
                    )
                    execution = _require_mapping(
                        qualified_cycle.get("execution"),
                        label="qualified_cycle.execution",
                    )
                    diagnostics = _require_mapping(
                        execution.get("diagnostics"),
                        label="qualified_cycle.execution.diagnostics",
                    )
                    try:
                        qualified_trade_rows.append(
                            OOSTradeRow(
                                event_cluster_id=_require_string(
                                    qualified_evidence.get("expiry_cluster_id"),
                                    label="qualified_evidence.expiry_cluster_id",
                                ),
                                net_pnl=parsed_qualified_net_pnl,
                                transient_naked_exposure_peak_usdc=_require_decimal(
                                    diagnostics.get(
                                        "transient_naked_exposure_peak_usdc"
                                    ),
                                    label=(
                                        "qualified_cycle.execution.diagnostics."
                                        "transient_naked_exposure_peak_usdc"
                                    ),
                                ),
                                planned_single_leg_max_loss_usdc=_require_decimal(
                                    decision.get("quantity"),
                                    label="qualified_cycle.decision.quantity",
                                ),
                                signal_strength=_require_decimal(
                                    decision.get("uncertainty_adjusted_pnl_per_pair"),
                                    label=(
                                        "qualified_cycle.decision."
                                        "uncertainty_adjusted_pnl_per_pair"
                                    ),
                                ),
                            )
                        )
                    except (TypeError, ValueError):
                        qualified_trade_metrics_complete = False
                else:
                    qualified_trade_metrics_complete = False
        elif report.get("verified_report_v2") is True:
            qualified_trade_metrics_complete = False

    expected_markets = len(market_ids)
    mechanically_labelable = len(mechanically_labelable_market_ids)
    if mechanically_labelable > expected_markets:
        raise ValueError("mechanically labelable market count exceeds unique markets")
    qualified_complete_taker_cost_model = (
        qualified_economic_attempts > 0 and qualified_complete_taker_cost_model
    )
    qualified_delay_depth_and_legging_replay_complete = (
        qualified_economic_attempts > 0
        and qualified_delay_depth_and_legging_replay_complete
    )
    full_oos_rows = tuple(oos_forecasts)
    full_trade_rows = (
        tuple(qualified_trade_rows)
        if qualified_trade_metrics_complete
        and len(qualified_trade_rows) == len(qualified_explainable_net_pnls)
        else ()
    )
    evidence = ValidationEvidence(
        resolved_current_regime_markets=len(resolved_market_ids),
        expected_current_regime_markets=expected_markets,
        markets_with_complete_capture=mechanically_labelable,
        unknown_resolution_mapping_count=(
            expected_markets - mechanically_labelable + resolution_conflicts
        ),
        explainable_simulated_trades=len(qualified_explainable_net_pnls),
        explainable_fills=qualified_explainable_fills,
        explainable_net_pnls=tuple(qualified_explainable_net_pnls),
        chronological_oos_complete=(
            qualified_available_reports > 0 and qualified_oos_missing_reports == 0
        ),
        complete_taker_cost_model=qualified_complete_taker_cost_model,
        delay_depth_and_legging_replay_complete=(
            qualified_delay_depth_and_legging_replay_complete
        ),
        bootstrap_net_pnl_lower_95=bootstrap_cluster_mean_lower_95(full_trade_rows),
        oos_brier_5=brier_score(full_oos_rows, horizon="5m", baseline=False),
        oos_brier_15=brier_score(full_oos_rows, horizon="15m", baseline=False),
        market_brier_5=brier_score(full_oos_rows, horizon="5m", baseline=True),
        market_brier_15=brier_score(full_oos_rows, horizon="15m", baseline=True),
        oos_expected_calibration_error_5=expected_calibration_error(
            full_oos_rows,
            horizon="5m",
        ),
        oos_expected_calibration_error_15=expected_calibration_error(
            full_oos_rows,
            horizon="15m",
        ),
        maximum_single_event_pnl_share=maximum_absolute_event_contribution_share(
            full_trade_rows
        ),
        direction_exposure_below_single_leg=direction_exposure_below_single_leg(
            full_trade_rows
        ),
        signal_strength_net_ev_monotonic=signal_strength_net_pnl_monotonic(
            full_trade_rows
        ),
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
    shadow_net_total = sum(shadow_net_pnls, Decimal("0")) if shadow_net_pnls else None
    if len(shadow_net_pnls) > shadow_trades:
        raise ValueError("development shadow settled trades exceed economic attempts")
    shadow_pending_trades = shadow_trades - len(shadow_net_pnls)
    summary: dict[str, Any] = {
        "schema_version": (
            "btc-5m-15m-relative-value-validation-summary.v2"
            if saw_v2_report
            else "btc-5m-15m-relative-value-validation-summary.v1"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
        "classification": validation.status.value,
        "observed_qualified_attempt_net_pnl": _decimal_text(
            validation.observed_explainable_net_pnl
        ),
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
            "pending_trades": shadow_pending_trades,
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
                str(shadow_positive / shadow_negative) if shadow_negative > 0 else None
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
                "transient_naked_exposure_peak_usdc": str(shadow_transient_peak_usdc),
                "transient_naked_exposure_total_duration_ms": (
                    shadow_transient_duration_ms_total
                ),
                "transient_naked_exposure_trades": shadow_transient_nonzero_trades,
            },
        },
        "reason_codes": list(validation.reason_codes),
        "inputs": input_rows,
        "preregistration_sha256": preregistration_sha256,
        "evidence_track": evidence_track,
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
                "invalid_decisions": (clock_required_decisions - clock_valid_decisions),
                "maximum_measurement_age_ms": (
                    max(clock_measurement_ages_ms)
                    if clock_measurement_ages_ms
                    else None
                ),
                "maximum_uncertainty_ms": (
                    max(clock_uncertainties_ms) if clock_uncertainties_ms else None
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
            "explainable_simulated_trades": len(qualified_explainable_net_pnls),
            "explainable_fills": qualified_explainable_fills,
            "qualified_economic_attempts": qualified_economic_attempts,
            "observed_explainable_net_pnl": _decimal_text(
                validation.observed_explainable_net_pnl
            ),
            "chronological_oos_complete": evidence.chronological_oos_complete,
            "oos_forecast_reports_available": oos_forecast_available_reports,
            "oos_forecast_reports_unavailable": (
                len(report_paths) - oos_forecast_available_reports
            ),
            "qualified_available_reports": qualified_available_reports,
            "qualified_oos_missing_reports": qualified_oos_missing_reports,
            "oos_forecast_unavailable_reason_counts": dict(
                sorted(oos_forecast_unavailable_reason_counts.items())
            ),
            "complete_taker_cost_model": evidence.complete_taker_cost_model,
            "delay_depth_and_legging_replay_complete": (
                evidence.delay_depth_and_legging_replay_complete
            ),
            "bootstrap_net_pnl_lower_95": _decimal_text(
                evidence.bootstrap_net_pnl_lower_95
            ),
            "oos_brier_5": _decimal_text(evidence.oos_brier_5),
            "oos_brier_15": _decimal_text(evidence.oos_brier_15),
            "market_brier_5": _decimal_text(evidence.market_brier_5),
            "market_brier_15": _decimal_text(evidence.market_brier_15),
            "oos_expected_calibration_error_5": _decimal_text(
                evidence.oos_expected_calibration_error_5
            ),
            "oos_expected_calibration_error_15": _decimal_text(
                evidence.oos_expected_calibration_error_15
            ),
            "maximum_single_event_pnl_share": _decimal_text(
                evidence.maximum_single_event_pnl_share
            ),
            "direction_exposure_below_single_leg": (
                evidence.direction_exposure_below_single_leg
            ),
            "signal_strength_net_ev_monotonic": (
                evidence.signal_strength_net_ev_monotonic
            ),
            "complete_current_taker_fee_metadata": complete_cost_model,
        },
        "promotion_gaps": {
            "resolved_markets_remaining": max(
                0, validation.minimum_resolved_markets - len(resolved_market_ids)
            ),
            "simulated_trades_remaining": max(
                0,
                validation.minimum_simulated_trades
                - len(qualified_explainable_net_pnls),
            ),
            "explainable_fills_remaining": max(
                0,
                validation.minimum_explainable_fills - qualified_explainable_fills,
            ),
        },
        "conclusion": (
            "No profitability claim is permitted; observed qualified-attempt "
            f"net PnL is {validation.observed_explainable_net_pnl}, but "
            "qualified/OOS PnL remains null until every validation gate passes."
            if validation.observed_explainable_net_pnl is not None
            else (
                "No profitability claim is permitted; preliminary "
                f"development-shadow net PnL is {shadow_net_total}, but "
                "qualified/OOS PnL remains null."
                if shadow_net_total is not None
                else "No profitability claim is permitted: qualified economic "
                "evidence is unavailable, so PnL remains null rather than zero."
            )
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
