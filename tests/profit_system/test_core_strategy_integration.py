from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.profit_system.config import ProfitSystemConfig, RiskLimits, StrategyConfig
from src.profit_system.opportunities import OpportunityEngine
from src.profit_system.runtime import StrategyRuntime
from src.profit_system.strategies.arbitrage import ArbitrageStrategy
from src.profit_system.strategies.maker import MakerStrategy
from src.profit_system.strategies.models import (
    NormalizedOpportunity,
    PortfolioSnapshot,
    RuntimeMode,
)
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
        max_order_notional=Decimal("100"),
        max_market_exposure=Decimal("200"),
        max_event_exposure=Decimal("200"),
        max_strategy_exposure=Decimal("250"),
        max_total_exposure=Decimal("300"),
        max_daily_loss=Decimal("50"),
        max_drawdown=Decimal("60"),
        max_open_orders=20,
        max_unmatched_leg_seconds=Decimal("10"),
        max_market_data_age_ms=1_000,
        max_user_stream_gap_seconds=Decimal("5"),
        max_order_rate=Decimal("3"),
    )


def _strategy_config(
    strategy_id: str,
    *,
    qualification: StrategyQualification = StrategyQualification.SHADOW_ONLY,
) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        strategy_version="0.2.0",
        hypothesis_summary=f"Integration fixture for {strategy_id}.",
        operating_mode=OperatingMode.SHADOW,
        qualification=qualification,
        allowed_modes=(
            OperatingMode.RESEARCH,
            OperatingMode.REPLAY,
            OperatingMode.PAPER,
            OperatingMode.SHADOW,
        ),
        min_net_edge=Decimal("0.01"),
        min_trade_size=Decimal("5"),
        risk_penalty_multiplier=Decimal("1"),
        max_candidate_ttl_seconds=Decimal("120"),
        risk_limits=_risk_limits(),
        code_version_hash="a" * 64 if "track_a" in strategy_id else "b" * 64,
    )


def _projected_risk(*, order_notional: str, open_orders: int = 1) -> ProjectedRisk:
    return ProjectedRisk(
        order_notional=Decimal(order_notional),
        projected_market_exposure=Decimal("25"),
        projected_event_exposure=Decimal("30"),
        projected_strategy_exposure=Decimal("35"),
        projected_total_exposure=Decimal("45"),
        projected_daily_loss=Decimal("2"),
        projected_drawdown=Decimal("3"),
        projected_open_orders=open_orders,
        projected_unmatched_leg_seconds=Decimal("2"),
    )


def _track_a_signal() -> OpportunitySignal:
    detected_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    return OpportunitySignal(
        strategy_id="track_a.complete_set.arbitrage.v0_2",
        detected_at=detected_at,
        expires_at=detected_at + timedelta(seconds=45),
        market_ids=("track-a-m1", "track-a-m2"),
        token_ids=("track-a-y1", "track-a-y2"),
        event_id="track-a-event",
        recommended_legs=(
            RecommendedLeg(
                market_id="track-a-m1",
                token_id="track-a-y1",
                side=OrderSide.BUY,
                price=Decimal("0.41"),
                size=Decimal("10"),
                order_type=OrderType.LIMIT,
                post_only=True,
            ),
            RecommendedLeg(
                market_id="track-a-m2",
                token_id="track-a-y2",
                side=OrderSide.BUY,
                price=Decimal("0.47"),
                size=Decimal("10"),
                order_type=OrderType.LIMIT,
                post_only=True,
            ),
        ),
        expected_gross_edge=Decimal("1.90"),
        expected_incentive=Decimal("0.20"),
        costs=OpportunityCostEstimate(
            expected_taker_fee=Decimal("0.10"),
            expected_builder_fee=Decimal("0.05"),
            expected_slippage=Decimal("0.03"),
            expected_adverse_selection=Decimal("0.02"),
            expected_inventory_cost=Decimal("0.01"),
            expected_unwind_cost=Decimal("0.04"),
            uncertainty_buffer=Decimal("0.05"),
        ),
        estimated_capacity=Decimal("10"),
        max_loss=Decimal("0.60"),
        confidence=Decimal("0.93"),
        lower_confidence_margin=Decimal("0.10"),
        projected_risk=_projected_risk(order_notional="8.80"),
        evidence_refs=(
            EvidenceRef(kind="research", ref="track-a/baseline"),
            EvidenceRef(kind="shadow", ref="track-a/day17"),
        ),
    )


def _track_b_signal(*, slippage: Decimal | None = Decimal("0.01")) -> OpportunitySignal:
    detected_at = datetime(2026, 8, 30, 12, 0, 5, tzinfo=UTC)
    return OpportunitySignal(
        strategy_id="track_b.maker.quote.v0_2",
        detected_at=detected_at,
        expires_at=detected_at + timedelta(seconds=90),
        market_ids=("track-b-m1",),
        token_ids=("track-b-y1",),
        event_id="track-b-event",
        recommended_legs=(
            RecommendedLeg(
                market_id="track-b-m1",
                token_id="track-b-y1",
                side=OrderSide.BUY,
                price=Decimal("0.48"),
                size=Decimal("12"),
                order_type=OrderType.LIMIT,
                post_only=True,
            ),
        ),
        expected_gross_edge=Decimal("0.90"),
        expected_incentive=Decimal("0.06"),
        costs=OpportunityCostEstimate(
            expected_taker_fee=Decimal("0.04"),
            expected_builder_fee=Decimal("0.02"),
            expected_slippage=slippage,
            expected_adverse_selection=Decimal("0.08"),
            expected_inventory_cost=Decimal("0.03"),
            expected_unwind_cost=Decimal("0.00"),
            uncertainty_buffer=Decimal("0.02"),
        ),
        estimated_capacity=Decimal("12"),
        max_loss=Decimal("0.90"),
        confidence=Decimal("0.88"),
        lower_confidence_margin=Decimal("0.06"),
        projected_risk=_projected_risk(order_notional="5.76"),
        evidence_refs=(
            EvidenceRef(kind="research", ref="track-b/baseline"),
            EvidenceRef(kind="shadow", ref="track-b/day14"),
        ),
    )


def _scan_candidates(*signals: OpportunitySignal):
    config = ProfitSystemConfig(
        strategies=tuple(_strategy_config(signal.strategy_id) for signal in signals),
        generated_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )
    snapshot = OpportunitySnapshot(
        captured_at=datetime(2026, 8, 30, 12, 0, 10, tzinfo=UTC),
        signals=signals,
        market_data_age_ms=250,
        user_stream_gap_seconds=Decimal("1"),
        open_order_count=0,
        current_order_rate=Decimal("0.2"),
    )
    ranked = OpportunityEngine().scan(snapshot, config)
    return {candidate.strategy_id: candidate for candidate in ranked.opportunities}


def test_normalized_opportunity_from_core_candidate_preserves_runtime_inputs() -> None:
    candidate = _scan_candidates(_track_a_signal())["track_a.complete_set.arbitrage.v0_2"]

    normalized = NormalizedOpportunity.from_candidate(candidate)

    assert candidate.status is OpportunityStatus.EXECUTABLE
    assert normalized.detected_at_ms == 1_788_091_200_000
    assert normalized.expires_at_ms == 1_788_091_245_000
    assert normalized.opportunity_kind == "complete_set"
    assert normalized.event_id == "track-a-event"
    assert normalized.recommended_legs[0].leg_id == "leg-1"
    assert normalized.recommended_legs[0].event_id == "track-a-event"
    assert normalized.recommended_legs[0].quantity == Decimal("10")
    assert normalized.recommended_legs[0].side == "buy"
    assert normalized.recommended_legs[0].order_type == "limit"
    assert normalized.evidence_refs == (
        "research://track-a/baseline",
        "shadow://track-a/day17",
    )
    assert normalized.metadata["source_status"] == "EXECUTABLE"
    assert normalized.metadata["source_executable"] is True
    assert normalized.metadata["source_rejection_reasons"] == ()
    assert normalized.metadata["source_snapshot_hash"] == candidate.snapshot_hash


def test_normalized_opportunity_from_core_candidate_fails_closed_on_unknown_costs() -> None:
    candidate = _scan_candidates(_track_b_signal(slippage=None))["track_b.maker.quote.v0_2"]

    assert candidate.status is OpportunityStatus.REJECTED
    assert "unknown_expected_slippage" in candidate.rejection_reasons
    with pytest.raises(ValueError, match="expected_slippage"):
        NormalizedOpportunity.from_candidate(candidate)


@pytest.mark.parametrize(
    ("strategy_id", "strategy"),
    (
        ("track_a.complete_set.arbitrage.v0_2", ArbitrageStrategy()),
        ("track_b.maker.quote.v0_2", MakerStrategy()),
    ),
)
def test_engine_candidates_keep_identical_signal_signatures_across_runtime_modes(
    strategy_id: str,
    strategy: ArbitrageStrategy | MakerStrategy,
) -> None:
    candidates = _scan_candidates(_track_a_signal(), _track_b_signal())
    runtime = StrategyRuntime(strategy)
    portfolio = PortfolioSnapshot(available_capital=Decimal("100"))

    opportunity = candidates[strategy_id]
    replay = runtime.evaluate(opportunity, portfolio, RuntimeMode.REPLAY)
    paper = runtime.evaluate(opportunity, portfolio, RuntimeMode.PAPER)
    shadow = runtime.evaluate(opportunity, portfolio, RuntimeMode.SHADOW)

    assert replay.signal_signature() == paper.signal_signature() == shadow.signal_signature()
    assert all(leg.event_id == opportunity.event_id for leg in replay.legs)
    assert replay.mutation_allowed is False
    assert paper.mutation_allowed is False
    assert shadow.mutation_allowed is False
    assert replay.capital_limit == paper.capital_limit == shadow.capital_limit == Decimal("100")
