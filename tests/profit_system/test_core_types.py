from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.profit_system.serialization import (
    SerializationError,
    canonical_decimal_string,
    canonical_json_bytes,
)
from src.profit_system.types import (
    EvidenceRef,
    OpportunityCostEstimate,
    OpportunitySignal,
    OpportunitySnapshot,
    OrderSide,
    OrderType,
    ProjectedRisk,
    RecommendedLeg,
)


def _signal() -> OpportunitySignal:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    return OpportunitySignal(
        strategy_id="strategy.alpha",
        detected_at=now,
        expires_at=now + timedelta(seconds=30),
        market_ids=("market-a",),
        token_ids=("token-a",),
        event_id="event-a",
        recommended_legs=(
            RecommendedLeg(
                market_id="market-a",
                token_id="token-a",
                side=OrderSide.BUY,
                price=Decimal("0.41"),
                size=Decimal("12"),
                order_type=OrderType.LIMIT,
                post_only=True,
            ),
        ),
        expected_gross_edge=Decimal("1.50"),
        expected_incentive=Decimal("0.20"),
        costs=OpportunityCostEstimate(
            expected_taker_fee=Decimal("0.10"),
            expected_builder_fee=Decimal("0.05"),
            expected_slippage=Decimal("0.02"),
            expected_adverse_selection=Decimal("0.03"),
            expected_inventory_cost=Decimal("0.04"),
            expected_unwind_cost=Decimal("0.01"),
            uncertainty_buffer=Decimal("0.05"),
        ),
        estimated_capacity=Decimal("12"),
        max_loss=Decimal("0.80"),
        confidence=Decimal("0.85"),
        lower_confidence_margin=Decimal("0.10"),
        projected_risk=ProjectedRisk(
            order_notional=Decimal("6"),
            projected_market_exposure=Decimal("6"),
            projected_event_exposure=Decimal("6"),
            projected_strategy_exposure=Decimal("6"),
            projected_total_exposure=Decimal("6"),
            projected_daily_loss=Decimal("0.80"),
            projected_drawdown=Decimal("0.80"),
            projected_open_orders=1,
            projected_unmatched_leg_seconds=Decimal("2"),
        ),
        evidence_refs=(EvidenceRef(kind="snapshot", ref="captures/market-a.json"),),
    )


def test_canonical_decimal_string_normalizes_scale() -> None:
    assert canonical_decimal_string(Decimal("1.2300")) == "1.23"
    assert canonical_decimal_string(Decimal("-0.000")) == "0"


def test_canonical_json_bytes_emits_decimal_strings_and_sorted_keys() -> None:
    encoded = canonical_json_bytes(
        {"b": Decimal("2.5000"), "a": {"ts": datetime(2026, 8, 30, tzinfo=UTC)}}
    )
    assert encoded == b'{"a":{"ts":"2026-08-30T00:00:00.000000Z"},"b":"2.5"}'


def test_canonical_json_bytes_rejects_binary_float() -> None:
    with pytest.raises(SerializationError, match="binary float"):
        canonical_json_bytes({"edge": 0.1})


def test_core_types_are_frozen_and_snapshot_hash_is_stable() -> None:
    signal = _signal()
    snapshot_a = OpportunitySnapshot(
        captured_at=datetime(2026, 8, 30, 12, 0, 5, tzinfo=UTC),
        signals=(signal,),
        market_data_age_ms=500,
        user_stream_gap_seconds=Decimal("1"),
        open_order_count=0,
        current_order_rate=Decimal("0.5"),
    )
    snapshot_b = OpportunitySnapshot(
        captured_at=datetime(2026, 8, 30, 12, 0, 5, tzinfo=UTC),
        signals=(signal,),
        market_data_age_ms=500,
        user_stream_gap_seconds=Decimal("1"),
        open_order_count=0,
        current_order_rate=Decimal("0.5"),
    )

    assert snapshot_a.snapshot_hash == snapshot_b.snapshot_hash

    with pytest.raises(FrozenInstanceError):
        signal.strategy_id = "mutated"
