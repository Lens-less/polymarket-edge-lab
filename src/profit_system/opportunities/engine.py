"""Opportunity scanning and explanation for the V0.2 profit-system core."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from ..config import ProfitSystemConfig, StrategyConfig
from ..serialization import canonical_sha256
from ..types import (
    GateCheck,
    OpportunityCandidate,
    OpportunityExplanation,
    OpportunitySignal,
    OpportunitySnapshot,
    OpportunityStatus,
    RankedOpportunities,
    StrategyQualification,
)


@dataclass(frozen=True, slots=True)
class ScanContext:
    signal: OpportunitySignal
    config: StrategyConfig
    snapshot: OpportunitySnapshot
    scanned_at: datetime


class OpportunityEngine:
    """Compute, filter, rank, and explain immutable opportunity candidates."""

    def __init__(self) -> None:
        self._last_ranked: RankedOpportunities | None = None
        self._explanations: dict[str, OpportunityExplanation] = {}

    def scan(
        self,
        snapshot: OpportunitySnapshot,
        strategy_configs: ProfitSystemConfig | Iterable[StrategyConfig],
    ) -> RankedOpportunities:
        scanned_at = datetime.now(UTC)
        config_map = _strategy_map(strategy_configs)
        candidates = []
        explanations: dict[str, OpportunityExplanation] = {}
        for signal in snapshot.signals:
            config = config_map[signal.strategy_id]
            context = ScanContext(
                signal=signal,
                config=config,
                snapshot=snapshot,
                scanned_at=scanned_at,
            )
            candidate, explanation = _evaluate_signal(context)
            candidates.append(candidate)
            explanations[candidate.opportunity_id] = explanation
        ranked = tuple(sorted(candidates, key=_rank_sort_key))
        result = RankedOpportunities(
            scanned_at=scanned_at,
            snapshot_hash=snapshot.snapshot_hash,
            opportunities=ranked,
        )
        self._last_ranked = result
        self._explanations = explanations
        return result

    def explain(self, opportunity_id: str) -> OpportunityExplanation:
        try:
            return self._explanations[opportunity_id]
        except KeyError as exc:
            raise KeyError(f"unknown opportunity_id: {opportunity_id}") from exc


def _strategy_map(
    strategy_configs: ProfitSystemConfig | Iterable[StrategyConfig],
) -> dict[str, StrategyConfig]:
    if isinstance(strategy_configs, ProfitSystemConfig):
        strategies = strategy_configs.strategies
    else:
        strategies = tuple(strategy_configs)
    return {strategy.strategy_id: strategy for strategy in strategies}


def _rank_sort_key(candidate: OpportunityCandidate) -> tuple[int, Decimal, Decimal, str]:
    status_priority = {
        OpportunityStatus.EXECUTABLE: 3,
        OpportunityStatus.REVIEW: 2,
        OpportunityStatus.WATCH: 1,
        OpportunityStatus.REJECTED: 0,
    }[candidate.status]
    risk_adjusted = (
        candidate.risk_adjusted_expected_net_profit
        if candidate.risk_adjusted_expected_net_profit is not None
        else Decimal("-999999999")
    )
    tradable_edge = (
        candidate.tradable_edge if candidate.tradable_edge is not None else Decimal("-999999999")
    )
    return (-status_priority, -risk_adjusted, -tradable_edge, candidate.opportunity_id)


def _evaluate_signal(
    context: ScanContext,
) -> tuple[OpportunityCandidate, OpportunityExplanation]:
    signal = context.signal
    config = context.config
    snapshot = context.snapshot
    rejection_reasons: list[str] = []
    gate_checks: list[GateCheck] = []

    if signal.strategy_id != config.strategy_id:
        raise ValueError("signal/config strategy_id mismatch")

    gate_checks.append(
        GateCheck(
            name="qualification_mode_allowed",
            passed=config.allows_mode(config.operating_mode),
            detail=f"{config.qualification.value} allows {config.operating_mode.value}",
        )
    )
    if not config.allows_mode(config.operating_mode):
        rejection_reasons.append("strategy_mode_not_allowed")
    if config.qualification is StrategyQualification.STOPPED:
        rejection_reasons.append("strategy_stopped")

    if config.market_whitelist and any(
        market_id not in config.market_whitelist for market_id in signal.market_ids
    ):
        rejection_reasons.append("market_not_whitelisted")

    expected_net_edge, tradable_edge, expected_fees, risk_adjusted = _economic_values(
        signal,
        config,
        rejection_reasons,
    )

    ttl_seconds = Decimal(str((signal.expires_at - snapshot.captured_at).total_seconds()))
    gate_checks.append(
        GateCheck(
            name="ttl_positive",
            passed=ttl_seconds > Decimal("0"),
            detail=f"ttl_seconds={ttl_seconds}",
        )
    )
    if ttl_seconds <= Decimal("0"):
        rejection_reasons.append("expired")
    gate_checks.append(
        GateCheck(
            name="ttl_within_strategy_cap",
            passed=ttl_seconds <= config.max_candidate_ttl_seconds,
            detail=f"ttl_seconds={ttl_seconds}, cap={config.max_candidate_ttl_seconds}",
        )
    )
    if ttl_seconds > config.max_candidate_ttl_seconds:
        rejection_reasons.append("ttl_exceeds_strategy_cap")

    gate_checks.append(
        GateCheck(
            name="capacity_min_trade_size",
            passed=signal.estimated_capacity >= config.min_trade_size,
            detail=(
                f"estimated_capacity={signal.estimated_capacity}, "
                f"min_trade_size={config.min_trade_size}"
            ),
        )
    )
    if signal.estimated_capacity < config.min_trade_size:
        rejection_reasons.append("insufficient_capacity")

    if tradable_edge is not None:
        passed = tradable_edge > config.min_net_edge
        gate_checks.append(
            GateCheck(
                name="tradable_edge_threshold",
                passed=passed,
                detail=f"tradable_edge={tradable_edge}, min_net_edge={config.min_net_edge}",
            )
        )
        if not passed:
            rejection_reasons.append("tradable_edge_below_minimum")

    if risk_adjusted is not None:
        passed = risk_adjusted > Decimal("0")
        gate_checks.append(
            GateCheck(
                name="risk_adjusted_profit_positive",
                passed=passed,
                detail=f"risk_adjusted_expected_net_profit={risk_adjusted}",
            )
        )
        if not passed:
            rejection_reasons.append("risk_adjusted_profit_non_positive")

    _append_availability_checks(
        signal=signal,
        snapshot=snapshot,
        config=config,
        rejection_reasons=rejection_reasons,
        gate_checks=gate_checks,
    )
    _append_risk_checks(
        signal=signal,
        snapshot=snapshot,
        config=config,
        rejection_reasons=rejection_reasons,
        gate_checks=gate_checks,
    )

    status = _status_for(rejection_reasons=rejection_reasons)
    executable = status is OpportunityStatus.EXECUTABLE
    opportunity_id = canonical_sha256(
        {
            "schema_version": "profit-system.opportunity.v0.2",
            "strategy_id": signal.strategy_id,
            "detected_at": signal.detected_at,
            "expires_at": signal.expires_at,
            "market_ids": signal.market_ids,
            "token_ids": signal.token_ids,
            "recommended_legs": signal.recommended_legs,
            "snapshot_hash": snapshot.snapshot_hash,
            "config_hash": config.config_hash,
            "code_version_hash": config.code_version_hash,
        }
    )
    candidate = OpportunityCandidate(
        opportunity_id=opportunity_id,
        strategy_id=signal.strategy_id,
        detected_at=signal.detected_at,
        expires_at=signal.expires_at,
        market_ids=signal.market_ids,
        token_ids=signal.token_ids,
        event_id=signal.event_id,
        recommended_legs=signal.recommended_legs,
        expected_gross_edge=signal.expected_gross_edge,
        expected_fees=expected_fees,
        expected_slippage=signal.costs.expected_slippage,
        expected_adverse_selection=signal.costs.expected_adverse_selection,
        expected_inventory_cost=signal.costs.expected_inventory_cost,
        expected_incentive=signal.expected_incentive,
        expected_net_edge=expected_net_edge,
        tradable_edge=tradable_edge,
        max_loss=signal.max_loss,
        estimated_capacity=signal.estimated_capacity,
        confidence=signal.confidence,
        evidence_refs=signal.evidence_refs,
        rejection_reasons=tuple(dict.fromkeys(rejection_reasons)),
        status=status,
        executable=executable,
        expected_taker_fee=signal.costs.expected_taker_fee,
        expected_builder_fee=signal.costs.expected_builder_fee,
        expected_unwind_cost=signal.costs.expected_unwind_cost,
        uncertainty_buffer=signal.costs.uncertainty_buffer,
        lower_confidence_margin=signal.lower_confidence_margin,
        risk_adjusted_expected_net_profit=risk_adjusted,
        snapshot_hash=snapshot.snapshot_hash,
        config_hash=config.config_hash,
        code_version_hash=config.code_version_hash,
    )
    explanation = OpportunityExplanation(
        opportunity_id=opportunity_id,
        candidate=candidate,
        gate_checks=tuple(gate_checks),
        scenarios={
            "optimistic_net_edge": (
                expected_net_edge + signal.lower_confidence_margin
                if expected_net_edge is not None
                else None
            ),
            "base_expected_net_edge": expected_net_edge,
            "lower_confidence_bound": tradable_edge,
        },
        recomputation_inputs={
            "snapshot_hash": snapshot.snapshot_hash,
            "config_hash": config.config_hash,
            "code_version_hash": config.code_version_hash,
            "signal": signal,
            "risk_limits": config.risk_limits,
            "operating_mode": config.operating_mode,
            "qualification": config.qualification,
            "scanned_at": context.scanned_at,
        },
        evidence_refs=signal.evidence_refs,
    )
    return candidate, explanation


def _economic_values(
    signal: OpportunitySignal,
    config: StrategyConfig,
    rejection_reasons: list[str],
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    costs = signal.costs
    unknown_costs = list(costs.unknown_fields)
    if signal.expected_incentive is None:
        unknown_costs.append("expected_incentive")
    if unknown_costs:
        for name in unknown_costs:
            rejection_reasons.append(f"unknown_{name}")
        return None, None, None, None

    expected_incentive = cast(Decimal, signal.expected_incentive)
    expected_taker_fee = cast(Decimal, costs.expected_taker_fee)
    expected_builder_fee = cast(Decimal, costs.expected_builder_fee)
    expected_slippage = cast(Decimal, costs.expected_slippage)
    expected_adverse_selection = cast(Decimal, costs.expected_adverse_selection)
    expected_inventory_cost = cast(Decimal, costs.expected_inventory_cost)
    expected_unwind_cost = cast(Decimal, costs.expected_unwind_cost)
    uncertainty_buffer = cast(Decimal, costs.uncertainty_buffer)
    expected_fees = expected_taker_fee + expected_builder_fee
    expected_net_edge = (
        signal.expected_gross_edge
        + expected_incentive
        - expected_taker_fee
        - expected_builder_fee
        - expected_slippage
        - expected_adverse_selection
        - expected_inventory_cost
        - expected_unwind_cost
        - uncertainty_buffer
    )
    tradable_edge = expected_net_edge - signal.lower_confidence_margin
    risk_adjusted = tradable_edge * signal.estimated_capacity - (
        signal.max_loss * config.risk_penalty_multiplier
    )
    return expected_net_edge, tradable_edge, expected_fees, risk_adjusted


def _append_availability_checks(
    *,
    signal: OpportunitySignal,
    snapshot: OpportunitySnapshot,
    config: StrategyConfig,
    rejection_reasons: list[str],
    gate_checks: list[GateCheck],
) -> None:
    del config
    availability_checks = (
        ("data_available", signal.data_available, "data availability"),
        ("market_available", signal.market_available, "market availability"),
        ("account_available", signal.account_available, "account availability"),
        ("execution_available", signal.execution_available, "execution availability"),
        (
            "market_constraints_known",
            snapshot.market_constraints_known,
            "market constraints known",
        ),
        (
            "geoblock_allows_trading",
            snapshot.geoblock_allows_trading,
            "geoblock permits trading",
        ),
        ("persistent_kill_clear", not snapshot.persistent_kill, "persistent kill clear"),
    )
    for name, passed, detail in availability_checks:
        gate_checks.append(GateCheck(name=name, passed=passed, detail=detail))
        if not passed:
            rejection_reasons.append(name)


def _append_risk_checks(
    *,
    signal: OpportunitySignal,
    snapshot: OpportunitySnapshot,
    config: StrategyConfig,
    rejection_reasons: list[str],
    gate_checks: list[GateCheck],
) -> None:
    risk = signal.projected_risk
    checks = (
        (
            "max_order_notional",
            risk.order_notional <= config.risk_limits.max_order_notional,
            f"{risk.order_notional} <= {config.risk_limits.max_order_notional}",
        ),
        (
            "max_market_exposure",
            risk.projected_market_exposure <= config.risk_limits.max_market_exposure,
            (f"{risk.projected_market_exposure} <= {config.risk_limits.max_market_exposure}"),
        ),
        (
            "max_event_exposure",
            risk.projected_event_exposure <= config.risk_limits.max_event_exposure,
            f"{risk.projected_event_exposure} <= {config.risk_limits.max_event_exposure}",
        ),
        (
            "max_strategy_exposure",
            risk.projected_strategy_exposure <= config.risk_limits.max_strategy_exposure,
            (f"{risk.projected_strategy_exposure} <= {config.risk_limits.max_strategy_exposure}"),
        ),
        (
            "max_total_exposure",
            risk.projected_total_exposure <= config.risk_limits.max_total_exposure,
            f"{risk.projected_total_exposure} <= {config.risk_limits.max_total_exposure}",
        ),
        (
            "max_daily_loss",
            risk.projected_daily_loss <= config.risk_limits.max_daily_loss,
            f"{risk.projected_daily_loss} <= {config.risk_limits.max_daily_loss}",
        ),
        (
            "max_drawdown",
            risk.projected_drawdown <= config.risk_limits.max_drawdown,
            f"{risk.projected_drawdown} <= {config.risk_limits.max_drawdown}",
        ),
        (
            "max_open_orders",
            risk.projected_open_orders + snapshot.open_order_count
            <= config.risk_limits.max_open_orders,
            (
                f"{risk.projected_open_orders + snapshot.open_order_count} <= "
                f"{config.risk_limits.max_open_orders}"
            ),
        ),
        (
            "max_unmatched_leg_seconds",
            risk.projected_unmatched_leg_seconds <= config.risk_limits.max_unmatched_leg_seconds,
            (
                f"{risk.projected_unmatched_leg_seconds} <= "
                f"{config.risk_limits.max_unmatched_leg_seconds}"
            ),
        ),
        (
            "max_market_data_age_ms",
            snapshot.market_data_age_ms <= config.risk_limits.max_market_data_age_ms,
            f"{snapshot.market_data_age_ms} <= {config.risk_limits.max_market_data_age_ms}",
        ),
        (
            "max_user_stream_gap_seconds",
            snapshot.user_stream_gap_seconds <= config.risk_limits.max_user_stream_gap_seconds,
            (
                f"{snapshot.user_stream_gap_seconds} <= "
                f"{config.risk_limits.max_user_stream_gap_seconds}"
            ),
        ),
        (
            "max_order_rate",
            snapshot.current_order_rate <= config.risk_limits.max_order_rate,
            f"{snapshot.current_order_rate} <= {config.risk_limits.max_order_rate}",
        ),
    )
    for name, passed, detail in checks:
        gate_checks.append(GateCheck(name=name, passed=passed, detail=detail))
        if not passed:
            rejection_reasons.append(name)


def _status_for(
    *,
    rejection_reasons: list[str],
) -> OpportunityStatus:
    reasons = set(rejection_reasons)
    hard_reject_prefixes = (
        "unknown_",
        "data_available",
        "market_available",
        "account_available",
        "execution_available",
        "market_constraints_known",
        "geoblock_allows_trading",
        "persistent_kill_clear",
        "strategy_",
        "max_",
    )
    if any(
        reason == "expired"
        or reason == "market_not_whitelisted"
        or reason.startswith(hard_reject_prefixes)
        for reason in reasons
    ):
        return OpportunityStatus.REJECTED
    if reasons:
        return OpportunityStatus.WATCH
    return OpportunityStatus.EXECUTABLE
