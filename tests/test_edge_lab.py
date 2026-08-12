"""Tests for the read-only paired-outcome edge research lab."""

from decimal import Decimal

import pytest

from src.edge_lab.economics import (
    build_paired_ask_quote,
    build_paired_bid_quote,
    evaluate_paired_ask_quote,
    evaluate_paired_bid_quote,
    evaluate_taker_complete_sets,
    liquidity_score,
    taker_fee,
    walk_asks,
)
from src.edge_lab.compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from src.edge_lab.models import BookLevel, FeeSchedule, OrderBook
from src.edge_lab.replay import _matched_volume_within, align_books
from src.backtest.fee_model import FeeConfig, FeeModel, FeeScenario
from src.edge_lab.state_machine import (
    CycleState,
    PairCycle,
    PairMode,
    PairRiskLimits,
)


def book(
    token_id: str,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    *,
    timestamp_ms: int = 1,
) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in bids),
        asks=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in asks),
        timestamp_ms=timestamp_ms,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
    )


def sample_books() -> tuple[OrderBook, OrderBook]:
    yes = book(
        "yes",
        [("0.22", "100")],
        [("0.38", "6"), ("0.41", "30"), ("0.70", "100")],
    )
    no = book(
        "no",
        [("0.62", "6")],
        [("0.78", "100")],
    )
    return yes, no


def test_current_fee_formula_matches_weather_table() -> None:
    weather = FeeSchedule(rate=Decimal("0.05"), exponent=Decimal("1"))
    assert taker_fee(Decimal("100"), Decimal("0.50"), weather) == Decimal(
        "1.25000"
    )
    assert taker_fee(Decimal("100"), Decimal("0.10"), weather) == Decimal(
        "0.45000"
    )
    assert taker_fee(Decimal("100"), Decimal("0.90"), weather) == Decimal(
        "0.45000"
    )

    legacy_engine_model = FeeModel(
        FeeConfig(
            scenario=FeeScenario.TAKER_FEE,
            taker_fee_rate=0.05,
            taker_fee_exponent=1,
        )
    )
    assert legacy_engine_model.calculate_taker_fee(
        Decimal("50"),
        shares=Decimal("100"),
        price=Decimal("0.50"),
    ) == Decimal("1.25000")
    assert legacy_engine_model.calculate_maker_rebate(Decimal("100")) == Decimal(
        "0"
    )


def test_taker_fee_rounds_sub_quantum_amount_down_to_zero() -> None:
    weather = FeeSchedule(rate=Decimal("0.05"), exponent=Decimal("1"))
    assert taker_fee(Decimal("0.0007"), Decimal("0.50"), weather) == Decimal("0")


def test_legacy_fee_model_drops_sub_quantum_amount_instead_of_rounding_up() -> None:
    model = FeeModel(
        FeeConfig(
            scenario=FeeScenario.TAKER_FEE,
            taker_fee_rate=0.05,
            taker_fee_exponent=1,
        )
    )

    assert model.calculate_taker_fee(
        shares=Decimal("0.001"),
        price=Decimal("0.25"),
    ) == Decimal("0")


def test_liquidity_score_rewards_tighter_quotes() -> None:
    tight = liquidity_score(
        Decimal("3"), Decimal("1"), Decimal("100")
    )
    loose = liquidity_score(
        Decimal("3"), Decimal("2"), Decimal("100")
    )
    assert tight.quantize(Decimal("0.000001")) == Decimal("44.444444")
    assert loose.quantize(Decimal("0.000001")) == Decimal("11.111111")
    assert tight > loose
    assert (
        liquidity_score(Decimal("3"), Decimal("3.01"), Decimal("100"))
        == Decimal("0")
    )


def test_paired_bid_quote_is_passive_and_complete_set_positive() -> None:
    yes, no = sample_books()
    quote = build_paired_bid_quote(
        yes,
        no,
        max_spread_cents=Decimal("4.5"),
        size=Decimal("20"),
    )
    assert quote.reference_yes_mid == Decimal("0.30")
    assert quote.yes_bid == Decimal("0.27")
    assert quote.no_bid == Decimal("0.67")
    assert quote.yes_bid < yes.best_ask
    assert quote.no_bid < no.best_ask
    assert quote.paired_margin == Decimal("1.20")


def test_paired_ask_quote_is_passive_and_backed_by_complete_set() -> None:
    yes, no = sample_books()
    quote = build_paired_ask_quote(
        yes,
        no,
        max_spread_cents=Decimal("4.5"),
        size=Decimal("20"),
    )
    assert quote.yes_ask == Decimal("0.33")
    assert quote.no_ask == Decimal("0.73")
    assert quote.yes_ask > yes.best_bid
    assert quote.no_ask > no.best_bid
    assert quote.capital == Decimal("20")
    assert quote.paired_margin == Decimal("1.20")


def test_depth_walk_refuses_to_invent_liquidity() -> None:
    fill = walk_asks(
        [
            BookLevel(Decimal("0.40"), Decimal("4")),
            BookLevel(Decimal("0.50"), Decimal("3")),
        ],
        Decimal("10"),
    )
    assert not fill.complete
    assert fill.filled_size == Decimal("7")
    assert fill.notional == Decimal("3.10")


def test_evaluation_exposes_one_leg_loss_and_no_fill_probability() -> None:
    yes, no = sample_books()
    quote = build_paired_bid_quote(
        yes,
        no,
        max_spread_cents=Decimal("4.5"),
        size=Decimal("20"),
    )
    evaluation = evaluate_paired_bid_quote(
        quote,
        yes,
        no,
        daily_reward=Decimal("34"),
        reward_max_spread_cents=Decimal("4.5"),
        reward_min_size=Decimal("20"),
        fee_schedule=FeeSchedule(
            rate=Decimal("0.05"), exponent=Decimal("1")
        ),
        stress_ticks=2,
    )
    assert evaluation["paired_fill"]["margin"] == Decimal("1.20")
    assert evaluation["one_leg_hedges"]["yes_fills_first"]["complete"]
    assert evaluation["one_leg_hedges"]["worst_loss"] > Decimal("0")
    assert "fill_probability" not in evaluation
    assert "not guaranteed" in evaluation["liquidity_reward_scenario"]["warning"]


def test_paired_ask_and_taker_scans_preserve_inventory_constraints() -> None:
    yes, no = sample_books()
    ask_quote = build_paired_ask_quote(
        yes,
        no,
        max_spread_cents=Decimal("4.5"),
        size=Decimal("5"),
    )
    ask_evaluation = evaluate_paired_ask_quote(
        ask_quote,
        yes,
        no,
        daily_reward=Decimal("34"),
        reward_max_spread_cents=Decimal("4.5"),
        reward_min_size=Decimal("5"),
        fee_schedule=FeeSchedule(
            rate=Decimal("0.05"), exponent=Decimal("1")
        ),
    )
    assert ask_evaluation["paired_fill"]["margin"] > Decimal("0")
    assert ask_evaluation["one_leg_hedges"]["yes_sells_first"]["complete"]

    taker = evaluate_taker_complete_sets(
        yes,
        no,
        sizes=[Decimal("5"), Decimal("20")],
        fee_schedule=FeeSchedule(
            rate=Decimal("0.05"), exponent=Decimal("1")
        ),
    )
    assert taker[0]["buy_both"]["net_edge"] < Decimal("0")
    assert taker[0]["sell_both_from_split_inventory"]["net_edge"] < Decimal("0")
    assert not taker[1]["sell_both_from_split_inventory"]["executable"]


def test_history_alignment_does_not_reuse_snapshots() -> None:
    yes = [
        book("yes", [("0.4", "1")], [("0.6", "1")], timestamp_ms=1_000),
        book("yes", [("0.4", "1")], [("0.6", "1")], timestamp_ms=2_000),
    ]
    no = [
        book("no", [("0.4", "1")], [("0.6", "1")], timestamp_ms=1_050),
        book("no", [("0.4", "1")], [("0.6", "1")], timestamp_ms=2_050),
    ]
    aligned = align_books(yes, no, tolerance_ms=100)
    assert len(aligned) == 2
    assert aligned[0][1].timestamp_ms == 1_050
    assert aligned[1][1].timestamp_ms == 2_050


def test_shadow_pairing_proxy_preserves_one_leg_imbalance() -> None:
    first = [(1_000, Decimal("10")), (20_000, Decimal("5"))]
    second = [(1_500, Decimal("4")), (50_000, Decimal("20"))]
    assert _matched_volume_within(first, second, horizon_ms=1_000) == Decimal(
        "4"
    )
    assert _matched_volume_within(first, second, horizon_ms=60_000) == Decimal(
        "15"
    )


def test_legacy_live_order_adapter_is_fail_closed() -> None:
    with pytest.raises(LiveExecutionBlocked, match="CLOB V2"):
        assert_new_orders_disabled()


def risk_limits() -> PairRiskLimits:
    return PairRiskLimits(
        max_collateral=Decimal("25"),
        max_single_leg_shares=Decimal("20"),
        max_single_leg_seconds=Decimal("5"),
        max_emergency_hedge_loss=Decimal("2"),
    )


def test_pair_cycle_fails_closed_on_unknown_reward_preflight() -> None:
    cycle = PairCycle(
        "condition",
        PairMode.PAIRED_BIDS,
        Decimal("20"),
        Decimal("19"),
        risk_limits(),
    )
    cycle.activate(
        preflight={"production_quote_eligible": False},
        split_inventory_ready=False,
        now_ms=0,
    )
    assert cycle.state == CycleState.SAFE_STOP
    assert cycle.stop_reason == "preflight_failed"


def test_pair_cycle_tracks_one_leg_timeout_and_hedge_budget() -> None:
    cycle = PairCycle(
        "condition",
        PairMode.PAIRED_ASKS_FROM_SPLIT,
        Decimal("20"),
        Decimal("20"),
        risk_limits(),
    )
    cycle.activate(
        preflight={"production_quote_eligible": True},
        split_inventory_ready=True,
        now_ms=0,
    )
    cycle.record_fill("first", Decimal("10"), now_ms=1_000)
    assert cycle.state == CycleState.ONE_LEG_FILLED
    assert cycle.directional_exposure == Decimal("10")
    cycle.check_timeout(now_ms=6_000)
    assert cycle.state == CycleState.HEDGE_REQUIRED
    assert cycle.approve_emergency_hedge(Decimal("1.50"))
    assert cycle.state == CycleState.RECONCILING


def test_pair_cycle_requires_confirmed_merge_before_flat() -> None:
    cycle = PairCycle(
        "condition",
        PairMode.PAIRED_BIDS,
        Decimal("20"),
        Decimal("19"),
        risk_limits(),
    )
    cycle.activate(
        preflight={"production_quote_eligible": True},
        split_inventory_ready=False,
        now_ms=0,
    )
    cycle.record_fill("first", Decimal("20"), now_ms=1_000)
    cycle.record_fill("second", Decimal("20"), now_ms=2_000)
    assert cycle.state == CycleState.PAIR_COMPLETE
    cycle.mark_pair_settled()
    assert cycle.state == CycleState.MERGE_PENDING
    cycle.mark_merge_confirmed()
    assert cycle.state == CycleState.FLAT
