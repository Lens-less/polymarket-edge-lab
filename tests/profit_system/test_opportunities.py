from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.profit_system.config import ProfitSystemConfig, RiskLimits, StrategyConfig
from src.profit_system.opportunities import OpportunityEngine
from src.profit_system.types import (
    EvidenceRef,
    OperatingMode,
    OpportunityCostEstimate,
    OpportunitySignal,
    OpportunitySnapshot,
    OpportunityStatus,
    OrderSide,
    OrderType,
    ProjectedRisk,
    RecommendedLeg,
    StrategyQualification,
)


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("50"),
        max_market_exposure=Decimal("100"),
        max_event_exposure=Decimal("120"),
        max_strategy_exposure=Decimal("150"),
        max_total_exposure=Decimal("200"),
        max_daily_loss=Decimal("25"),
        max_drawdown=Decimal("35"),
        max_open_orders=10,
        max_unmatched_leg_seconds=Decimal("8"),
        max_market_data_age_ms=1_500,
        max_user_stream_gap_seconds=Decimal("5"),
        max_order_rate=Decimal("3"),
    )


def _strategy(
    *,
    strategy_id: str = "strategy.alpha",
    operating_mode: OperatingMode = OperatingMode.SHADOW,
    qualification: StrategyQualification = StrategyQualification.SHADOW_ONLY,
    allowed_modes: tuple[OperatingMode, ...] = (
        OperatingMode.RESEARCH,
        OperatingMode.REPLAY,
        OperatingMode.PAPER,
        OperatingMode.SHADOW,
    ),
    code_version_hash: str = "d" * 64,
) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        strategy_version="v0.2.0",
        hypothesis_summary="Opportunity scanning core test strategy.",
        operating_mode=operating_mode,
        qualification=qualification,
        allowed_modes=allowed_modes,
        min_net_edge=Decimal("0.30"),
        min_trade_size=Decimal("5"),
        risk_penalty_multiplier=Decimal("1"),
        max_candidate_ttl_seconds=Decimal("60"),
        risk_limits=_risk_limits(),
        code_version_hash=code_version_hash,
    )


def _signal(
    *,
    strategy_id: str = "strategy.alpha",
    market_id: str = "market-a",
    token_id: str = "token-a",
    detected_at: datetime | None = None,
    expires_at: datetime | None = None,
    gross: str = "2.00",
    incentive: str | None = "0.40",
    taker_fee: str | None = "0.20",
    builder_fee: str | None = "0.10",
    slippage: str | None = "0.05",
    adverse: str | None = "0.05",
    inventory: str | None = "0.10",
    unwind: str | None = "0.10",
    uncertainty: str | None = "0.10",
    lower_confidence_margin: str = "0.20",
    capacity: str = "10",
    max_loss: str = "1.00",
    order_notional: str = "8.00",
    market_exposure: str = "20.00",
    event_exposure: str = "20.00",
    strategy_exposure: str = "25.00",
    total_exposure: str = "30.00",
    daily_loss: str = "1.00",
    drawdown: str = "2.00",
    open_orders: int = 1,
    unmatched_leg_seconds: str = "2.00",
    confidence: str = "0.90",
) -> OpportunitySignal:
    base_time = detected_at or datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    expiry_time = expires_at or (base_time + timedelta(seconds=30))
    return OpportunitySignal(
        strategy_id=strategy_id,
        detected_at=base_time,
        expires_at=expiry_time,
        market_ids=(market_id,),
        token_ids=(token_id,),
        event_id="event-a",
        recommended_legs=(
            RecommendedLeg(
                market_id=market_id,
                token_id=token_id,
                side=OrderSide.BUY,
                price=Decimal("0.40"),
                size=Decimal("10"),
                order_type=OrderType.LIMIT,
                post_only=True,
            ),
        ),
        expected_gross_edge=Decimal(gross),
        expected_incentive=None if incentive is None else Decimal(incentive),
        costs=OpportunityCostEstimate(
            expected_taker_fee=None if taker_fee is None else Decimal(taker_fee),
            expected_builder_fee=None if builder_fee is None else Decimal(builder_fee),
            expected_slippage=None if slippage is None else Decimal(slippage),
            expected_adverse_selection=None if adverse is None else Decimal(adverse),
            expected_inventory_cost=None if inventory is None else Decimal(inventory),
            expected_unwind_cost=None if unwind is None else Decimal(unwind),
            uncertainty_buffer=None if uncertainty is None else Decimal(uncertainty),
        ),
        estimated_capacity=Decimal(capacity),
        max_loss=Decimal(max_loss),
        confidence=Decimal(confidence),
        lower_confidence_margin=Decimal(lower_confidence_margin),
        projected_risk=ProjectedRisk(
            order_notional=Decimal(order_notional),
            projected_market_exposure=Decimal(market_exposure),
            projected_event_exposure=Decimal(event_exposure),
            projected_strategy_exposure=Decimal(strategy_exposure),
            projected_total_exposure=Decimal(total_exposure),
            projected_daily_loss=Decimal(daily_loss),
            projected_drawdown=Decimal(drawdown),
            projected_open_orders=open_orders,
            projected_unmatched_leg_seconds=Decimal(unmatched_leg_seconds),
        ),
        evidence_refs=(EvidenceRef(kind="snapshot", ref=f"captures/{market_id}.json"),),
    )


def _snapshot(
    *signals: OpportunitySignal, captured_at: datetime | None = None
) -> OpportunitySnapshot:
    return OpportunitySnapshot(
        captured_at=captured_at or datetime(2026, 8, 30, 12, 0, 5, tzinfo=UTC),
        signals=signals,
        market_data_age_ms=500,
        user_stream_gap_seconds=Decimal("1"),
        open_order_count=0,
        current_order_rate=Decimal("0.5"),
    )


def test_scan_computes_expected_net_edge_and_tradable_edge() -> None:
    engine = OpportunityEngine()
    strategy = _strategy()
    ranked = engine.scan(
        _snapshot(_signal()),
        ProfitSystemConfig(
            strategies=(strategy,),
            generated_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        ),
    )

    candidate = ranked.opportunities[0]
    assert candidate.status is OpportunityStatus.EXECUTABLE
    assert candidate.expected_fees == Decimal("0.30")
    assert candidate.expected_net_edge == Decimal("1.70")
    assert candidate.tradable_edge == Decimal("1.50")
    assert candidate.risk_adjusted_expected_net_profit == Decimal("14.00")


def test_scan_rejects_unknown_costs_fail_closed() -> None:
    engine = OpportunityEngine()
    strategy = _strategy()
    ranked = engine.scan(
        _snapshot(_signal(slippage=None)),
        (strategy,),
    )

    candidate = ranked.opportunities[0]
    assert candidate.status is OpportunityStatus.REJECTED
    assert "unknown_expected_slippage" in candidate.rejection_reasons
    assert candidate.expected_net_edge is None
    assert candidate.tradable_edge is None


def test_scan_applies_ttl_capacity_and_risk_checks() -> None:
    engine = OpportunityEngine()
    strategy = _strategy()
    signal = _signal(
        expires_at=datetime(2026, 8, 30, 12, 0, 4, tzinfo=UTC),
        capacity="4",
        order_notional="60",
    )
    ranked = engine.scan(
        _snapshot(signal),
        (strategy,),
    )

    candidate = ranked.opportunities[0]
    assert candidate.status is OpportunityStatus.REJECTED
    assert "expired" in candidate.rejection_reasons
    assert "insufficient_capacity" in candidate.rejection_reasons
    assert "max_order_notional" in candidate.rejection_reasons


def test_scan_ranks_deterministically_by_status_profit_edge_and_id() -> None:
    engine = OpportunityEngine()
    strategy = _strategy()
    profitable = _signal(market_id="market-a", token_id="token-a", gross="2.20")
    weaker = _signal(market_id="market-b", token_id="token-b", gross="1.50")
    watch = _signal(
        market_id="market-c",
        token_id="token-c",
        gross="0.90",
        lower_confidence_margin="0.40",
    )
    ranked = engine.scan(_snapshot(profitable, weaker, watch), (strategy,))

    assert [candidate.status for candidate in ranked.opportunities] == [
        OpportunityStatus.EXECUTABLE,
        OpportunityStatus.EXECUTABLE,
        OpportunityStatus.WATCH,
    ]
    assert (
        ranked.opportunities[0].risk_adjusted_expected_net_profit
        > ranked.opportunities[1].risk_adjusted_expected_net_profit
    )

    tied = engine.scan(
        _snapshot(
            _signal(market_id="market-d", token_id="token-d", gross="1.50"),
            _signal(market_id="market-e", token_id="token-e", gross="1.50"),
        ),
        (strategy,),
    )
    tied_ids = [candidate.opportunity_id for candidate in tied.opportunities]
    assert tied_ids == sorted(tied_ids)


def test_explain_returns_evidence_and_recomputable_hash_inputs() -> None:
    engine = OpportunityEngine()
    strategy = _strategy()
    ranked = engine.scan(_snapshot(_signal()), (strategy,))

    candidate = ranked.opportunities[0]
    explanation = engine.explain(candidate.opportunity_id)

    assert explanation.candidate.opportunity_id == candidate.opportunity_id
    assert explanation.evidence_refs == candidate.evidence_refs
    assert explanation.recomputation_inputs["snapshot_hash"] == candidate.snapshot_hash
    assert explanation.recomputation_inputs["config_hash"] == candidate.config_hash
    assert explanation.recomputation_inputs["code_version_hash"] == candidate.code_version_hash
    assert explanation.scenarios["lower_confidence_bound"] == candidate.tradable_edge


def test_v0_2_live_candidate_uses_new_qualification_and_risk_gates() -> None:
    engine = OpportunityEngine()
    live_strategy = _strategy(
        strategy_id="strategy.live",
        operating_mode=OperatingMode.LIVE_CANARY,
        qualification=StrategyQualification.PROBE_ALLOWED,
        allowed_modes=(
            OperatingMode.RESEARCH,
            OperatingMode.REPLAY,
            OperatingMode.PAPER,
            OperatingMode.SHADOW,
            OperatingMode.LIVE_CANARY,
        ),
        code_version_hash="e" * 64,
    )
    ranked = engine.scan(
        _snapshot(_signal(strategy_id="strategy.live")),
        (live_strategy,),
    )

    candidate = ranked.opportunities[0]
    explanation = engine.explain(candidate.opportunity_id)
    assert candidate.status is OpportunityStatus.EXECUTABLE
    assert candidate.executable is True
    assert candidate.rejection_reasons == ()
    assert all(check.name != "legacy_live_gate" for check in explanation.gate_checks)
