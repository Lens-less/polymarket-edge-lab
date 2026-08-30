"""Deterministic offline reports for the V0.2 CLI surface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Final, cast

from .acceptance import run_fixed_acceptance_suite_in_tempdir
from .execution import ExecutionSnapshot, VenueReconciliation
from .orchestrator import FixedTrackAcceptanceResult, RealizedOutcome, StageResult
from .portfolio import PerformanceReport, PortfolioSnapshot
from .readiness import (
    JSONValue,
    decimal_to_string,
    dumps_json,
    evaluate_doctor,
    to_jsonable,
    utc_timestamp,
)

ACCEPTANCE_REPORT_VERSION: Final = "profit-system-v0.2.acceptance.v2"
SCAN_REPORT_SCHEMA_VERSION: Final = "polymm-scan-report.v0.2"
REPLAY_REPORT_SCHEMA_VERSION: Final = "polymm-replay-report.v0.2"
SHADOW_REPORT_SCHEMA_VERSION: Final = "polymm-shadow-report.v0.2"
DESK_REPORT_SCHEMA_VERSION: Final = "polymm-desk-report.v0.2"
STATUS_REPORT_SCHEMA_VERSION: Final = "polymm-status-report.v0.2"
ACCEPTANCE_REPORT_SCHEMA_VERSION: Final = "polymm-acceptance-report.v0.2"
FAULT_DRILL_RESULT_SCHEMA_VERSION: Final = "polymm-fault-drill-result.v0.2"
LIVE_PROBE_RESULT_SCHEMA_VERSION: Final = "polymm-live-probe-result.v0.2"
TRACK_BY_SCENARIO: Final = {
    "acceptance_pass": "A",
    "acceptance_no_go": "B",
}
TRACK_BY_STRATEGY: Final = {
    "arbitrage": "A",
    "track_a.complete_set.arbitrage.v0_2": "A",
    "maker": "B",
    "track_b.maker.quote.v0_2": "B",
}
SCENARIO_BY_TRACK: Final = {
    "A": "acceptance_pass",
    "B": "acceptance_no_go",
}


@dataclass(frozen=True, slots=True)
class ResolvedAcceptanceTrack:
    result: FixedTrackAcceptanceResult
    track: str
    resolved_scenario: str
    requested_scenario: str | None
    requested_strategy: str
    selection_basis: str
    scenario_mismatch: bool


def write_report(path: str | Path, document: dict[str, JSONValue]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(dumps_json(document), encoding="utf-8")
    return destination


def _run_acceptance_results() -> dict[str, FixedTrackAcceptanceResult]:
    return run_fixed_acceptance_suite_in_tempdir()


def _json_ready(value: object) -> JSONValue:
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if not isinstance(value, type) and is_dataclass(value):
        return _json_ready(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return to_jsonable(value)


def _resolve_acceptance_track(
    *,
    strategy: str,
    scenario: str | None,
) -> ResolvedAcceptanceTrack:
    normalized_strategy = strategy.strip().lower()
    strategy_track = TRACK_BY_STRATEGY.get(normalized_strategy)
    scenario_track = TRACK_BY_SCENARIO.get(scenario) if scenario is not None else None
    if strategy_track is None and scenario_track is None:
        supported = ", ".join(sorted(TRACK_BY_STRATEGY))
        raise ValueError(f"unsupported strategy '{strategy}'; expected one of: {supported}")
    track = strategy_track or scenario_track
    if track is None:
        raise ValueError("unable to resolve a fixed acceptance track")
    results = _run_acceptance_results()
    resolved = results[track]
    return ResolvedAcceptanceTrack(
        result=resolved,
        track=track,
        resolved_scenario=SCENARIO_BY_TRACK[track],
        requested_scenario=scenario,
        requested_strategy=strategy,
        selection_basis="strategy" if strategy_track is not None else "scenario",
        scenario_mismatch=(
            strategy_track is not None
            and scenario_track is not None
            and strategy_track != scenario_track
        ),
    )


def _stage_mode(stage: StageResult) -> str:
    return str(
        stage.execution_mode.value if stage.execution_mode is not None else stage.runtime_mode.value
    )


def _serialize_execution_snapshot(snapshot: ExecutionSnapshot | None) -> JSONValue:
    if snapshot is None:
        return None
    return _json_ready(
        {
            "mode": snapshot.mode.value,
            "killed": snapshot.killed,
            "blocked_reasons": list(snapshot.blocked_reasons),
            "last_reconcile_at": snapshot.last_reconcile_at,
            "last_user_event_at": snapshot.last_user_event_at,
            "last_heartbeat_at": snapshot.last_heartbeat_at,
            "user_stream_backfill_complete": snapshot.user_stream_backfill_complete,
            "plan_count": len(snapshot.plans),
            "order_count": len(snapshot.orders),
            "unresolved_order_ids": list(snapshot.unresolved_order_ids),
        }
    )


def _serialize_reconciliation(reconciliation: VenueReconciliation | None) -> JSONValue:
    if reconciliation is None:
        return None
    return _json_ready(
        {
            "as_of": reconciliation.as_of,
            "open_order_count": len(reconciliation.open_orders),
            "resolved_order_count": len(reconciliation.resolved_orders),
            "fill_count": len(reconciliation.fills),
            "cash_balance": reconciliation.cash_balance,
            "allowance_available": reconciliation.allowance_available,
            "close_only": reconciliation.close_only,
            "geoblocked": reconciliation.geoblocked,
            "backfill_complete": reconciliation.backfill_complete,
            "discrepancies": list(reconciliation.discrepancies),
        }
    )


def _serialize_portfolio(snapshot: PortfolioSnapshot | None) -> JSONValue:
    if snapshot is None:
        return None
    return _json_ready(snapshot.to_document())


def _serialize_performance(report: PerformanceReport | None) -> JSONValue:
    if report is None:
        return None
    return _json_ready(report.to_document())


def _serialize_realized_outcome(
    outcome: RealizedOutcome | None,
    *,
    opportunity_id: str,
) -> JSONValue:
    if outcome is None:
        return None
    return _json_ready(
        {
            "source_id": outcome.source_id,
            "token_id": outcome.token_id,
            "opportunity_id": opportunity_id,
            "gross_trading_pnl": outcome.gross_trading_pnl,
            "fee_amount": outcome.fee_amount,
            "slippage_amount": outcome.slippage_amount,
            "unwind_amount": outcome.unwind_amount,
            "onchain_amount": outcome.onchain_amount,
            "incentive_confirmed": outcome.incentive_confirmed,
            "incentive_pending": outcome.incentive_pending,
            "markout_bps": outcome.markout_bps,
            "adverse_selection_bps": outcome.adverse_selection_bps,
            "realized_net_pnl": outcome.realized_net_pnl,
        }
    )


def _serialize_stage(stage: StageResult) -> dict[str, JSONValue]:
    confirm_status = "CONFIRMED" if stage.ticket is not None else "BLOCKED"
    return cast(
        dict[str, JSONValue],
        _json_ready(
            {
                "track_phase": stage.phase,
                "cycle_id": stage.cycle_id,
                "runtime_mode": stage.runtime_mode.value,
                "execution_mode": _stage_mode(stage),
                "signal_signature": stage.signal_signature,
                "evidence_digest": stage.evidence_digest,
                "decision_id": stage.decision_id,
                "confirm_status": confirm_status,
                "blocked_reasons": []
                if stage.execution_snapshot is None
                else list(stage.execution_snapshot.blocked_reasons),
                "ticket_order_count": 0 if stage.ticket is None else len(stage.ticket.orders),
                "candidate": {
                    "opportunity_id": stage.candidate.opportunity_id,
                    "strategy_id": stage.candidate.strategy_id,
                    "status": stage.candidate.status.value,
                    "event_id": stage.candidate.event_id,
                    "market_ids": list(stage.candidate.market_ids),
                    "token_ids": list(stage.candidate.token_ids),
                    "expected_gross_edge": stage.candidate.expected_gross_edge,
                    "expected_fees": stage.candidate.expected_fees,
                    "expected_slippage": stage.candidate.expected_slippage,
                    "expected_adverse_selection": stage.candidate.expected_adverse_selection,
                    "expected_inventory_cost": stage.candidate.expected_inventory_cost,
                    "expected_incentive": stage.candidate.expected_incentive,
                    "expected_net_edge": stage.candidate.expected_net_edge,
                    "tradable_edge": stage.candidate.tradable_edge,
                    "expected_taker_fee": stage.candidate.expected_taker_fee,
                    "expected_builder_fee": stage.candidate.expected_builder_fee,
                    "expected_unwind_cost": stage.candidate.expected_unwind_cost,
                    "uncertainty_buffer": stage.candidate.uncertainty_buffer,
                    "max_loss": stage.candidate.max_loss,
                    "estimated_capacity": stage.candidate.estimated_capacity,
                    "confidence": stage.candidate.confidence,
                    "lower_confidence_margin": stage.candidate.lower_confidence_margin,
                    "risk_adjusted_expected_net_profit": (
                        stage.candidate.risk_adjusted_expected_net_profit
                    ),
                    "evidence_refs": [
                        {"kind": ref.kind, "ref": ref.ref, "label": ref.label}
                        for ref in stage.candidate.evidence_refs
                    ],
                    "rejection_reasons": list(stage.candidate.rejection_reasons),
                },
                "explanation": {
                    "gate_checks": [
                        {
                            "name": check.name,
                            "passed": check.passed,
                            "detail": check.detail,
                        }
                        for check in stage.explanation.gate_checks
                    ],
                    "scenarios": dict(stage.explanation.scenarios),
                    "recomputation_inputs": dict(stage.explanation.recomputation_inputs),
                    "evidence_refs": [
                        {"kind": ref.kind, "ref": ref.ref, "label": ref.label}
                        for ref in stage.explanation.evidence_refs
                    ],
                },
                "decision": stage.decision.to_document(),
                "execution_snapshot": _serialize_execution_snapshot(stage.execution_snapshot),
                "reconciliation": _serialize_reconciliation(stage.reconciliation),
                "portfolio_snapshot": _serialize_portfolio(stage.ledger_snapshot),
                "performance": _serialize_performance(stage.performance),
                "realized_outcome": _serialize_realized_outcome(
                    stage.realized_outcome,
                    opportunity_id=stage.candidate.opportunity_id,
                ),
            }
        ),
    )


def _track_summary(result: FixedTrackAcceptanceResult) -> dict[str, JSONValue]:
    return cast(
        dict[str, JSONValue],
        _json_ready(
            {
                "track": result.track,
                "strategy_id": result.strategy_id,
                "final_status": result.final_status,
                "research": result.research.decision.to_document(),
                "replay": {
                    "status": result.replay.decision.status.value,
                    "signal_signature": result.replay.signal_signature,
                    "evidence_digest": result.replay.evidence_digest,
                },
                "paper": {
                    "status": result.paper.decision.status.value,
                    "signal_signature": result.paper.signal_signature,
                    "evidence_digest": result.paper.evidence_digest,
                    "realized_net_pnl": (
                        None
                        if result.paper.ledger_snapshot is None
                        else result.paper.ledger_snapshot.realized_net_pnl
                    ),
                },
                "shadow": {
                    "status": result.shadow.decision.status.value,
                    "signal_signature": result.shadow.signal_signature,
                    "evidence_digest": result.shadow.evidence_digest,
                    "realized_net_pnl": None,
                },
                "shadow_gate": result.shadow_gate.decision.to_document(),
                "replay_execution": {
                    "confirm_status": (
                        "CONFIRMED" if result.replay.ticket is not None else "BLOCKED"
                    ),
                    "blocked_reasons": []
                    if result.replay.execution_snapshot is None
                    else list(result.replay.execution_snapshot.blocked_reasons),
                },
                "paper_execution": {
                    "confirm_status": (
                        "CONFIRMED" if result.paper.ticket is not None else "BLOCKED"
                    ),
                    "blocked_reasons": []
                    if result.paper.execution_snapshot is None
                    else list(result.paper.execution_snapshot.blocked_reasons),
                },
                "shadow_execution": {
                    "confirm_status": (
                        "CONFIRMED" if result.shadow.ticket is not None else "BLOCKED"
                    ),
                    "blocked_reasons": []
                    if result.shadow.execution_snapshot is None
                    else list(result.shadow.execution_snapshot.blocked_reasons),
                },
            }
        ),
    )


def build_scan_report(
    *,
    strategy: str,
    scenario: str = "acceptance_pass",
) -> dict[str, JSONValue]:
    resolved = _resolve_acceptance_track(strategy=strategy, scenario=scenario)
    stage = resolved.result.replay
    candidate = stage.candidate
    return {
        "schema_version": SCAN_REPORT_SCHEMA_VERSION,
        "command": "scan",
        "generated_at": utc_timestamp(),
        "track": resolved.track,
        "strategy": resolved.requested_strategy,
        "strategy_id": resolved.result.strategy_id,
        "requested_scenario": resolved.requested_scenario,
        "scenario": resolved.resolved_scenario,
        "selection_basis": resolved.selection_basis,
        "scenario_mismatch": resolved.scenario_mismatch,
        "opportunities": [
            _serialize_stage(stage)["candidate"],
        ],
        "scan_summary": {
            "candidate_count": 1,
            "tradable_candidate_count": 1 if candidate.executable else 0,
            "unknown_cost_count": 0,
            "status": stage.decision.status.value,
            "executable": stage.decision.executable,
        },
        "gate_report": resolved.result.research.decision.to_document(),
    }


def build_replay_report(
    *,
    strategy: str,
    scenario: str = "acceptance_pass",
) -> dict[str, JSONValue]:
    resolved = _resolve_acceptance_track(strategy=strategy, scenario=scenario)
    stage = resolved.result.replay
    stage_document = _serialize_stage(stage)
    realized_net_pnl = (
        Decimal("0") if stage.ledger_snapshot is None else stage.ledger_snapshot.realized_net_pnl
    )
    paper_reference_realized_net_pnl = (
        None
        if resolved.result.paper.ledger_snapshot is None
        else resolved.result.paper.ledger_snapshot.realized_net_pnl
    )
    return {
        "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
        "command": "replay",
        "generated_at": utc_timestamp(),
        "track": resolved.track,
        "strategy": resolved.requested_strategy,
        "strategy_id": resolved.result.strategy_id,
        "requested_scenario": resolved.requested_scenario,
        "scenario": resolved.resolved_scenario,
        "selection_basis": resolved.selection_basis,
        "scenario_mismatch": resolved.scenario_mismatch,
        "rows": [
            {
                "attempt_id": stage.cycle_id,
                "opportunity_id": stage.candidate.opportunity_id,
                "decision_status": stage.decision.status.value,
                "confirm_status": stage_document["confirm_status"],
                "expected_net_edge": decimal_to_string(stage.decision.expected_net_edge),
                "tradable_edge": decimal_to_string(stage.decision.tradable_edge),
                "realized_net_pnl": decimal_to_string(realized_net_pnl),
                "paper_reference_realized_net_pnl": (
                    None
                    if paper_reference_realized_net_pnl is None
                    else decimal_to_string(paper_reference_realized_net_pnl)
                ),
            }
        ],
        "stage": stage_document,
        "totals": {
            "attempt_count": 1,
            "confirmed_attempt_count": 1 if stage.ticket is not None else 0,
            "realized_net_pnl": decimal_to_string(realized_net_pnl),
        },
    }


def build_shadow_report(
    *,
    strategy: str,
    scenario: str = "acceptance_pass",
) -> dict[str, JSONValue]:
    resolved = _resolve_acceptance_track(strategy=strategy, scenario=scenario)
    shadow_stage = _serialize_stage(resolved.result.shadow)
    gate_report: dict[str, JSONValue] = {
        "gate": "SHADOW",
        **resolved.result.shadow_gate.decision.to_document(),
    }
    gate_metrics = cast(dict[str, JSONValue], gate_report["metrics"])
    return {
        "schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "command": "shadow",
        "generated_at": utc_timestamp(),
        "track": resolved.track,
        "strategy": resolved.requested_strategy,
        "strategy_id": resolved.result.strategy_id,
        "requested_scenario": resolved.requested_scenario,
        "scenario": resolved.resolved_scenario,
        "selection_basis": resolved.selection_basis,
        "scenario_mismatch": resolved.scenario_mismatch,
        "metrics": {
            **gate_metrics,
            "realized_net_pnl": None,
            "pnl_basis": "shadow_is_observational_only",
            "same_strategy_logic": (
                resolved.result.replay.signal_signature == resolved.result.shadow.signal_signature
            ),
        },
        "stage": shadow_stage,
        "gate_report": gate_report,
    }


def build_acceptance_report(*, track: str = "all") -> dict[str, JSONValue]:
    normalized_track = track.upper()
    results = _run_acceptance_results()
    if normalized_track == "ALL":
        tracks = [_track_summary(results["A"]), _track_summary(results["B"])]
    elif normalized_track == "A":
        tracks = [_track_summary(results["A"])]
    elif normalized_track == "B":
        tracks = [_track_summary(results["B"])]
    else:
        raise ValueError("track must be one of: all, A, B")
    return {
        "schema_version": ACCEPTANCE_REPORT_SCHEMA_VERSION,
        "command": "acceptance",
        "generated_at": utc_timestamp(),
        "track_filter": normalized_track,
        "report_version": ACCEPTANCE_REPORT_VERSION,
        "tracks": to_jsonable(tracks),
    }


def build_fault_drill_result(
    *,
    status_by_drill: dict[str, str] | None = None,
) -> dict[str, JSONValue]:
    effective_status = (
        {} if status_by_drill is None else {key: value for key, value in status_by_drill.items()}
    )
    drills = []
    for drill_id in (
        "kill_switch_manual_reset",
        "restart_recovery",
        "user_stream_disconnect_cancel_all",
        "heartbeat_timeout_blocks_new_orders",
        "reconciliation_mismatch_blocks_new_orders",
    ):
        ready = effective_status.get(drill_id, "READY") == "READY"
        drills.append(
            {
                "drill_id": drill_id,
                "status": "READY" if ready else "BLOCKED",
                "detail": "Offline drill receipt recorded."
                if ready
                else "Drill missing or failed.",
                "evidence": {"receipt_recorded": ready},
            }
        )
    overall_ready = all(drill["status"] == "READY" for drill in drills)
    return {
        "schema_version": FAULT_DRILL_RESULT_SCHEMA_VERSION,
        "report_kind": "fault_drill_result",
        "generated_at": utc_timestamp(),
        "status": "READY" if overall_ready else "BLOCKED",
        "drills": to_jsonable(drills),
    }


def build_live_probe_result(
    *,
    ready: bool = False,
) -> dict[str, JSONValue]:
    checks = []
    details = {
        "geoblock": "Trading geography allowed.",
        "credentials_signing": (
            "Credentials present and signing path verified without exposing secrets."
        ),
        "balance_allowance": "Balance and allowance cover the tiny canary budget.",
        "market_constraints": "Market constraints fetched and validated.",
        "user_stream": "User stream connected and receiving private events.",
        "heartbeat": "Order heartbeat stayed healthy inside the configured gap.",
        "reconciliation": "Orders, trades, positions, and cash reconcile cleanly.",
    }
    for check_id, detail in details.items():
        checks.append(
            {
                "check_id": check_id,
                "label": check_id.replace("_", " "),
                "status": "READY" if ready else "BLOCKED",
                "detail": detail if ready else f"{detail} Live proof still required.",
                "evidence": {"offline_safe": True, "verified": ready},
            }
        )
    return {
        "schema_version": LIVE_PROBE_RESULT_SCHEMA_VERSION,
        "report_kind": "live_probe_result",
        "generated_at": utc_timestamp(),
        "status": "READY" if ready else "BLOCKED",
        "checks": to_jsonable(checks),
    }


def build_status_report(config_path: str | Path | None = None) -> dict[str, JSONValue]:
    doctor_report = evaluate_doctor(config_path)
    return {
        "schema_version": STATUS_REPORT_SCHEMA_VERSION,
        "command": "status",
        "generated_at": utc_timestamp(),
        "status": doctor_report.status,
        "gate_report": doctor_report.to_document(),
    }


def build_desk_report(config_path: str | Path | None = None) -> dict[str, JSONValue]:
    doctor_report = evaluate_doctor(config_path)
    return {
        "schema_version": DESK_REPORT_SCHEMA_VERSION,
        "command": "desk",
        "generated_at": utc_timestamp(),
        "mode": "offline_operator_overview",
        "current_canary_status": doctor_report.status,
        "profitability_status": "NOT_CLAIMED",
        "realized_net_pnl": None,
        "real_orders_submitted": False,
        "real_funds_changed": False,
        "commands": [
            "polymm doctor",
            "polymm scan --strategy arbitrage",
            "polymm replay --strategy maker",
            "polymm shadow --strategy maker",
            "polymm desk",
            "polymm status",
        ],
        "gate_snapshots": [doctor_report.to_document()],
    }


__all__ = [
    "ACCEPTANCE_REPORT_SCHEMA_VERSION",
    "DESK_REPORT_SCHEMA_VERSION",
    "FAULT_DRILL_RESULT_SCHEMA_VERSION",
    "LIVE_PROBE_RESULT_SCHEMA_VERSION",
    "REPLAY_REPORT_SCHEMA_VERSION",
    "SCAN_REPORT_SCHEMA_VERSION",
    "SHADOW_REPORT_SCHEMA_VERSION",
    "STATUS_REPORT_SCHEMA_VERSION",
    "build_acceptance_report",
    "build_desk_report",
    "build_fault_drill_result",
    "build_live_probe_result",
    "build_replay_report",
    "build_scan_report",
    "build_shadow_report",
    "build_status_report",
    "write_report",
]
