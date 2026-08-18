from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from src.edge_lab.btc_twap_structural_shadow import (
    SingleLegDisposition,
    StructuralDoubleMakerPlan,
    StructuralMakerLeg,
    StructuralShadowResult,
    evaluate_shadow_robustness,
    replay_structural_double_maker,
)
from src.edge_lab.execution import (
    DepthBook,
    OrderSide,
    QueueScenario,
    TradeEvent,
)
from tests.test_btc_twap_relative_value_v07_replay import _v07_pair

D = Decimal


def _plan() -> StructuralDoubleMakerPlan:
    pair = _v07_pair()
    return StructuralDoubleMakerPlan(
        attempt_id="expiry-1",
        expiry_ms=pair.expires_at_ms,
        direction="five_above_fifteen",
        first=StructuralMakerLeg(
            contract=pair.market_15,
            token_id=pair.market_15.up_token_id,
            price=D("0.40"),
            quantity=D("5"),
            visible_queue_ahead=D("5"),
        ),
        second=StructuralMakerLeg(
            contract=pair.market_5,
            token_id=pair.market_5.down_token_id,
            price=D("0.58"),
            quantity=D("5"),
            visible_queue_ahead=D("5"),
        ),
        submitted_at_ms=0,
        book_timestamp_ms=0,
        terminal_payouts={
            pair.market_15.up_token_id: D("1"),
            pair.market_15.down_token_id: D("0"),
            pair.market_5.up_token_id: D("1"),
            pair.market_5.down_token_id: D("0"),
        },
        initial_cash=D("20"),
        max_book_age_ms=1_000,
    )


def _trade(token_id: str, *, source: str, timestamp_ms: int = 100) -> TradeEvent:
    return TradeEvent(
        token_id=token_id,
        timestamp_ms=timestamp_ms,
        price=D("0.40") if "15" in source else D("0.58"),
        quantity=D("10"),
        aggressor_side=OrderSide.SELL,
        source_event_id=source,
    )


def test_double_maker_shadow_reuses_all_three_queue_scenarios() -> None:
    plan = _plan()
    events = (
        _trade(plan.first.token_id, source="trade-15"),
        _trade(plan.second.token_id, source="trade-5", timestamp_ms=101),
    )

    optimistic = replay_structural_double_maker(
        plan,
        public_events=events,
        scenario=QueueScenario.OPTIMISTIC,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )
    neutral = replay_structural_double_maker(
        plan,
        public_events=events,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )
    pessimistic = replay_structural_double_maker(
        plan,
        public_events=events,
        scenario=QueueScenario.PESSIMISTIC,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )

    assert optimistic.paired_quantity == D("5")
    assert neutral.paired_quantity == D("5")
    assert neutral.net_pnl == D("0.10")
    assert pessimistic.paired_quantity == D("0")
    assert pessimistic.net_pnl == D("0")


def test_single_leg_dispositions_are_accounted_separately() -> None:
    plan = _plan()
    first_only = (_trade(plan.first.token_id, source="trade-15"),)
    hedge_book = DepthBook.from_tuples(
        plan.second.token_id,
        bids=((D("0.58"), D("20")),),
        asks=((D("0.60"), D("20")),),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        timestamp_ms=100,
    )
    unwind_book = DepthBook.from_tuples(
        plan.first.token_id,
        bids=((D("0.39"), D("20")),),
        asks=((D("0.41"), D("20")),),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        timestamp_ms=100,
    )

    passive = replay_structural_double_maker(
        plan,
        public_events=first_only,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )
    hedged = replay_structural_double_maker(
        plan,
        public_events=first_only,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.TAKER_HEDGE,
        hedge_books={plan.second.token_id: hedge_book},
    )
    unwound = replay_structural_double_maker(
        plan,
        public_events=first_only,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.FAK_UNWIND,
        unwind_books={plan.first.token_id: unwind_book},
    )

    assert passive.unpaired_quantity == D("5")
    assert passive.taker_hedge_quantity == D("0")
    assert passive.fak_unwind_quantity == D("0")
    assert hedged.unpaired_quantity == D("0")
    assert hedged.taker_hedge_quantity == D("5")
    assert hedged.fees_paid > D("0")
    assert unwound.unpaired_quantity == D("0")
    assert unwound.fak_unwind_quantity == D("5")
    assert unwound.net_pnl < D("0")


def test_shadow_robustness_removes_best_expiry_direction_and_rolling_window() -> None:
    records = tuple(
        StructuralShadowResult.for_robustness(
            attempt_id=f"expiry-{index}",
            expiry_ms=index,
            direction="five_above_fifteen" if index % 2 else "five_below_fifteen",
            net_pnl=pnl,
        )
        for index, pnl in enumerate(
            (D("1"), D("1"), D("1"), D("1"), D("1")),
            start=1,
        )
    )

    verdict = evaluate_shadow_robustness(
        records,
        rolling_window_size=2,
        minimum_expiries=5,
    )

    assert verdict.total_net_pnl == D("5")
    assert verdict.without_best_expiry_net_pnl == D("4")
    assert verdict.without_best_direction_net_pnl == D("2")
    assert verdict.minimum_rolling_window_net_pnl == D("2")
    assert verdict.max_single_expiry_concentration == D("0.2")
    assert verdict.passed is True


def test_double_maker_plan_rejects_a_direction_label_that_conflicts_with_legs() -> None:
    with pytest.raises(ValueError, match="direction conflicts"):
        replace(_plan(), direction="five_below_fifteen")


def test_taker_disposition_does_not_mutate_captured_depth_evidence() -> None:
    plan = _plan()
    first_only = (_trade(plan.first.token_id, source="trade-15"),)
    hedge_book = DepthBook.from_tuples(
        plan.second.token_id,
        bids=((D("0.58"), D("20")),),
        asks=((D("0.60"), D("20")),),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        timestamp_ms=100,
    )

    first = replay_structural_double_maker(
        plan,
        public_events=first_only,
        disposition=SingleLegDisposition.TAKER_HEDGE,
        hedge_books={plan.second.token_id: hedge_book},
    )
    second = replay_structural_double_maker(
        plan,
        public_events=first_only,
        disposition=SingleLegDisposition.TAKER_HEDGE,
        hedge_books={plan.second.token_id: hedge_book},
    )

    assert first == second
    assert hedge_book.asks[0].size == D("20")


def test_robustness_requires_unique_common_expiries_and_typed_results() -> None:
    duplicate_expiry = (
        StructuralShadowResult.for_robustness(
            attempt_id="a",
            expiry_ms=1,
            direction="five_above_fifteen",
            net_pnl=D("1"),
        ),
        StructuralShadowResult.for_robustness(
            attempt_id="b",
            expiry_ms=1,
            direction="five_below_fifteen",
            net_pnl=D("1"),
        ),
    )
    with pytest.raises(ValueError, match="unique common expiries"):
        evaluate_shadow_robustness(
            duplicate_expiry,
            rolling_window_size=1,
            minimum_expiries=2,
        )
    with pytest.raises(TypeError, match="StructuralShadowResult"):
        evaluate_shadow_robustness(
            (object(),),  # type: ignore[arg-type]
            rolling_window_size=1,
            minimum_expiries=1,
        )
