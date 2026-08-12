"""Contract tests for the evidence-bounded execution tracer bullet."""

from decimal import Decimal

import pytest

from src.edge_lab.execution import (
    BookEvent,
    DepthBook,
    ExecutionFeeSchedule,
    ExecutionStatus,
    InsufficientCashError,
    InventoryError,
    Ledger,
    MakerOrderRequest,
    MakerReplay,
    MakerStatus,
    MarketStatus,
    OperationAssumption,
    OrderSide,
    QueueScenario,
    TimeInForce,
    TraceRef,
    TradeEvent,
    execute_taker,
)


D = Decimal


def ref(event: str, decision: str, timestamp_ms: int = 1) -> TraceRef:
    return TraceRef(
        source_event_id=event,
        decision_id=decision,
        timestamp_ms=timestamp_ms,
    )


def fee_free(source_ref: str = "fixture://fee-free-market") -> ExecutionFeeSchedule:
    return ExecutionFeeSchedule.fee_exempt(
        reason="test fixture explicitly models a fee-free market",
        source_ref=source_ref,
    )


def operation_free(source_ref: str = "fixture://confirmed-operation") -> OperationAssumption:
    return OperationAssumption(
        cost=D("0"),
        latency_ms=0,
        confirmed=True,
        source_ref=source_ref,
    )


def binary_ledger(cash: str = "100") -> Ledger:
    ledger = Ledger(D(cash))
    ledger.register_binary_market(
        "market-1",
        yes_token_id="yes-1",
        no_token_id="no-1",
    )
    return ledger


def test_compact_fee_schedule_and_sub_quantum_rule() -> None:
    with pytest.raises(ValueError, match="fd or feeSchedule"):
        ExecutionFeeSchedule.from_market({})
    with pytest.raises(ValueError, match="missing required fee fields"):
        ExecutionFeeSchedule.from_market({"fd": {"r": "0.05", "e": 1}})
    with pytest.raises(ValueError, match="missing required fee fields"):
        ExecutionFeeSchedule.from_market(
            {"fd": {"r": "0.05", "e": 1, "to": None}}
        )
    with pytest.raises(ValueError, match="taker-only flag"):
        ExecutionFeeSchedule.from_market(
            {"fd": {"r": "0.05", "e": 1, "to": "unknown"}}
        )

    schedule = ExecutionFeeSchedule.from_market(
        {"fd": {"r": "0.05", "e": 1, "to": True}}
    )
    assert schedule.rate == D("0.05")
    assert schedule.exponent == D("1")
    assert schedule.taker_only is True
    assert schedule.fee(D("100"), D("0.50"), maker=False) == D("1.25000")
    assert schedule.fee(D("0.0007"), D("0.50"), maker=False) == D("0")
    assert schedule.fee(D("0"), D("0.50"), maker=False) == D("0")
    assert schedule.fee(D("100"), D("0.50"), maker=True) == D("0")

    exempt = ExecutionFeeSchedule.fee_exempt(
        reason="fixture market is explicitly fee-free",
        source_ref="fixture://market-1",
    )
    assert exempt.rate == D("0")
    assert exempt.exemption_reason == "fixture market is explicitly fee-free"
    assert exempt.exemption_source_ref == "fixture://market-1"
    with pytest.raises(ValueError, match="fee_exempt"):
        ExecutionFeeSchedule(rate=D("0"), exponent=D("1"), taker_only=True)

    compact_zero = ExecutionFeeSchedule.from_market(
        {"c": "condition-compact", "fd": {"r": "0", "e": 1, "to": True}}
    )
    assert compact_zero.exemption_source_ref == (
        "market-payload:condition-compact:fd"
    )


def test_multitoken_ledger_conserves_marked_equity_and_blocks_naked_sell() -> None:
    ledger = binary_ledger()
    before = ledger.equity({"yes-1": D("0.40"), "no-1": D("0.60")})

    ledger.buy(
        "yes-1",
        D("10"),
        D("0.40"),
        fee=D("0"),
        trace=ref("trade-buy", "decision-buy"),
    )
    assert ledger.cash == D("96.00")
    assert ledger.position("yes-1") == D("10")
    assert ledger.position("no-1") == D("0")
    assert ledger.equity({"yes-1": D("0.40"), "no-1": D("0.60")}) == before

    ledger.sell(
        "yes-1",
        D("4"),
        D("0.40"),
        fee=D("0"),
        trace=ref("trade-sell", "decision-sell", 2),
    )
    assert ledger.position("yes-1") == D("6")
    assert ledger.equity({"yes-1": D("0.40"), "no-1": D("0.60")}) == before

    with pytest.raises(InventoryError, match="naked sell"):
        ledger.sell(
            "no-1",
            D("1"),
            D("0.60"),
            fee=D("0"),
            trace=ref("bad-sell", "bad-decision", 3),
        )

    assert {entry.source_event_id for entry in ledger.trace} == {
        "trade-buy",
        "trade-sell",
    }
    assert {entry.decision_id for entry in ledger.trace} == {
        "decision-buy",
        "decision-sell",
    }


def test_split_partial_merge_resolution_dispute_and_invalid_payout() -> None:
    ledger = binary_ledger()
    ledger.split(
        "market-1",
        D("10"),
        operation=operation_free(),
        trace=ref("split", "inventory-plan"),
    )
    assert ledger.cash == D("90")
    assert ledger.position("yes-1") == D("10")
    assert ledger.position("no-1") == D("10")

    ledger.merge(
        "market-1",
        D("4"),
        operation=operation_free(),
        trace=ref("merge", "inventory-plan", 2),
    )
    assert ledger.cash == D("94")
    assert ledger.position("yes-1") == D("6")
    assert ledger.position("no-1") == D("6")
    assert ledger.equity({"yes-1": D("0.42"), "no-1": D("0.58")}) == D(
        "100"
    )

    ledger.dispute("market-1", trace=ref("oracle-dispute", "settlement", 3))
    assert ledger.market_status("market-1") is MarketStatus.DISPUTED
    with pytest.raises(ValueError, match="disputed"):
        ledger.redeem(
            "market-1",
            operation=operation_free(),
            trace=ref("too-soon", "settlement", 4),
        )

    ledger.resolve(
        "market-1",
        winning_token_id="yes-1",
        trace=ref("oracle-final", "settlement", 5),
    )
    paid = ledger.redeem(
        "market-1",
        operation=operation_free(),
        trace=ref("redeem", "settlement", 6),
    )
    assert paid == D("6")
    assert ledger.cash == D("100")
    assert ledger.position("yes-1") == D("0")
    assert ledger.position("no-1") == D("0")

    invalid = binary_ledger()
    invalid.split(
        "market-1",
        D("8"),
        operation=operation_free(),
        trace=ref("split-2", "invalid-case"),
    )
    invalid.resolve(
        "market-1",
        invalid=True,
        trace=ref("oracle-invalid", "invalid-case", 2),
    )
    assert invalid.market_status("market-1") is MarketStatus.INVALID
    assert invalid.redeem(
        "market-1",
        operation=operation_free(),
        trace=ref("invalid-redeem", "invalid-case", 3),
    ) == D("8")
    assert invalid.cash == D("100")


def test_accounting_snapshots_audit_complete_set_conservation() -> None:
    ledger = binary_ledger()
    opening = ledger.accounting_snapshot(
        {"yes-1": D("0.40"), "no-1": D("0.60")}
    )
    assert opening.cash == D("100")
    assert opening.positions == {"yes-1": D("0"), "no-1": D("0")}
    assert opening.fees_paid == D("0")
    assert opening.marked_equity == D("100")
    assert opening.realized_cashflow == D("0")

    ledger.split(
        "market-1",
        D("10"),
        operation=operation_free(),
        trace=ref("snap-split", "snapshots"),
    )
    after_split = ledger.accounting_snapshot(
        {"yes-1": D("0.40"), "no-1": D("0.60")}
    )
    assert after_split.cash == D("90")
    assert after_split.positions == {"yes-1": D("10"), "no-1": D("10")}
    assert after_split.marked_equity == opening.marked_equity
    assert after_split.realized_cashflow == D("-10")

    ledger.merge(
        "market-1",
        D("4"),
        operation=operation_free(),
        trace=ref("snap-merge", "snapshots", 2),
    )
    after_merge = ledger.accounting_snapshot(
        {"yes-1": D("0.40"), "no-1": D("0.60")}
    )
    assert after_merge.cash == D("94")
    assert after_merge.positions == {"yes-1": D("6"), "no-1": D("6")}
    assert after_merge.marked_equity == opening.marked_equity
    assert after_merge.realized_cashflow == D("-6")

    ledger.resolve(
        "market-1",
        winning_token_id="yes-1",
        trace=ref("snap-resolve", "snapshots", 3),
    )
    after_resolution = ledger.accounting_snapshot(
        {"yes-1": D("1"), "no-1": D("0")}
    )
    assert after_resolution.marked_equity == opening.marked_equity
    assert after_resolution.realized_cashflow == D("-6")

    ledger.redeem(
        "market-1",
        operation=operation_free(),
        trace=ref("snap-redeem", "snapshots", 4),
    )
    after_redeem = ledger.accounting_snapshot({})
    assert after_redeem.cash == D("100")
    assert after_redeem.positions == {"yes-1": D("0"), "no-1": D("0")}
    assert after_redeem.marked_equity == opening.marked_equity
    assert after_redeem.realized_cashflow == D("0")


def test_chain_operations_require_confirmation_and_charge_explicit_costs() -> None:
    ledger = binary_ledger()
    unconfirmed = OperationAssumption(
        cost=D("0.10"),
        latency_ms=500,
        confirmed=False,
        source_ref="scenario://unconfirmed-split",
    )
    with pytest.raises(ValueError, match="not confirmed"):
        ledger.split(
            "market-1",
            D("10"),
            operation=unconfirmed,
            trace=ref("unconfirmed-split", "chain-cost"),
        )
    assert ledger.cash == D("100")
    assert ledger.position("yes-1") == D("0")

    ledger.split(
        "market-1",
        D("10"),
        operation=OperationAssumption(
            cost=D("0.10"),
            latency_ms=500,
            confirmed=True,
            source_ref="scenario://confirmed-split",
        ),
        trace=ref("confirmed-split", "chain-cost", 2),
    )
    ledger.merge(
        "market-1",
        D("4"),
        operation=OperationAssumption(
            cost=D("0.20"),
            latency_ms=700,
            confirmed=True,
            source_ref="scenario://confirmed-merge",
        ),
        trace=ref("confirmed-merge", "chain-cost", 3),
    )
    snapshot = ledger.accounting_snapshot(
        {"yes-1": D("0.40"), "no-1": D("0.60")}
    )
    assert snapshot.cash == D("93.70")
    assert snapshot.operation_costs_paid == D("0.30")
    assert snapshot.marked_equity == D("99.70")
    assert [entry.operation_cost for entry in ledger.trace] == [
        D("0.10"),
        D("0.20"),
    ]


def test_accounting_snapshot_reconciles_fees_and_trade_cashflows() -> None:
    ledger = binary_ledger()
    ledger.buy(
        "yes-1",
        D("10"),
        D("0.40"),
        fee=D("0.10"),
        trace=ref("accounting-buy", "accounting-trades"),
    )
    after_buy = ledger.accounting_snapshot(
        {"yes-1": D("0.40"), "no-1": D("0.60")}
    )
    assert after_buy.cash == D("95.90")
    assert after_buy.fees == D("0.10")
    assert after_buy.marked_equity == D("99.90")
    assert after_buy.realized_cashflow == D("-4.10")
    assert after_buy.trace_entries == 1

    ledger.sell(
        "yes-1",
        D("4"),
        D("0.50"),
        fee=D("0.05"),
        trace=ref("accounting-sell", "accounting-trades", 2),
    )
    after_sell = ledger.accounting_snapshot(
        {"yes-1": D("0.50"), "no-1": D("0.50")}
    )
    assert after_sell.cash == D("97.85")
    assert after_sell.positions["yes-1"] == D("6")
    assert after_sell.fees == D("0.15")
    assert after_sell.marked_equity == D("100.85")
    assert after_sell.realized_cashflow == D("-2.15")
    assert after_sell.trace_entries == 2


def test_taker_fak_walks_levels_at_improved_prices_and_depletes_shared_depth() -> None:
    ledger = binary_ledger()
    book = DepthBook.from_tuples(
        "yes-1",
        bids=[("0.38", "9")],
        asks=[("0.40", "2"), ("0.41", "3"), ("0.43", "20")],
        tick_size=D("0.01"),
        min_order_size=D("0.000001"),
        timestamp_ms=10,
    )
    fee = fee_free()

    first = execute_taker(
        ledger,
        book,
        side=OrderSide.BUY,
        quantity=D("6"),
        limit_price=D("0.41"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee,
        trace=ref("book-10", "take-1", 10),
        execution_latency_ms=0,
        max_book_age_ms=10,
    )
    assert first.filled_quantity == D("5")
    assert first.status.value == "partial"
    assert [fill.price for fill in first.fills] == [D("0.40"), D("0.41")]
    assert first.notional == D("2.03")
    assert book.asks[0].price == D("0.43")

    second = execute_taker(
        ledger,
        book,
        side=OrderSide.BUY,
        quantity=D("2"),
        limit_price=D("0.43"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee,
        trace=ref("book-11", "take-2", 11),
        execution_latency_ms=0,
        max_book_age_ms=10,
    )
    assert second.filled_quantity == D("2")
    assert book.asks[0].size == D("18")
    assert ledger.position("yes-1") == D("7")


def test_taker_fee_uses_conservative_max_of_per_fill_and_aggregate_rounding() -> None:
    ledger = binary_ledger()
    book = DepthBook.from_tuples(
        "yes-1",
        bids=[],
        asks=[("0.50", "0.0007"), ("0.51", "0.0007")],
        min_order_size=D("0.000001"),
        timestamp_ms=1,
    )
    result = execute_taker(
        ledger,
        book,
        side=OrderSide.BUY,
        quantity=D("0.0014"),
        limit_price=D("0.51"),
        time_in_force=TimeInForce.FOK,
        fee_schedule=ExecutionFeeSchedule(
            rate=D("0.05"), exponent=D("1"), taker_only=True
        ),
        trace=ref("tiny-levels", "fee-rounding"),
        execution_latency_ms=0,
        max_book_age_ms=10,
    )
    assert result.filled_quantity == D("0.0014")
    assert [fill.fee for fill in result.fills] == [D("0"), D("0.00002")]
    assert result.fee == D("0.00002")


def test_taker_floors_fill_dust_to_an_executable_six_decimal_notional() -> None:
    ledger = binary_ledger()
    book = DepthBook.from_tuples(
        "yes-1",
        bids=[("0.536", "10")],
        asks=[("0.537", "10")],
        tick_size=D("0.001"),
        min_order_size=D("5"),
        timestamp_ms=1,
    )
    schedule = ExecutionFeeSchedule(
        rate=D("0.07"), exponent=D("1"), taker_only=True
    )

    result = execute_taker(
        ledger,
        book,
        side=OrderSide.BUY,
        quantity=D("5.123456"),
        limit_price=D("1"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=schedule,
        trace=ref("real-shape-book", "real-shape-decision"),
        execution_latency_ms=0,
        max_book_age_ms=10,
    )

    assert result.status is ExecutionStatus.PARTIAL
    assert result.filled_quantity == D("5.123")
    assert result.notional == D("2.751051")
    assert ledger.cash == ledger.cash.quantize(D("0.000001"))


def test_taker_fok_is_atomic_for_depth_and_ledger() -> None:
    ledger = binary_ledger()
    book = DepthBook.from_tuples(
        "yes-1",
        bids=[("0.39", "100")],
        asks=[("0.40", "2"), ("0.41", "3")],
        min_order_size=D("0.000001"),
        timestamp_ms=1,
    )
    original_cash = ledger.cash

    killed = execute_taker(
        ledger,
        book,
        side=OrderSide.BUY,
        quantity=D("6"),
        limit_price=D("0.41"),
        time_in_force=TimeInForce.FOK,
        fee_schedule=fee_free(),
        trace=ref("book-fok", "fok"),
        execution_latency_ms=0,
        max_book_age_ms=10,
    )
    assert killed.status.value == "killed"
    assert killed.filled_quantity == D("0")
    assert ledger.cash == original_cash
    assert book.asks[0].size == D("2")

    with pytest.raises(InventoryError, match="naked sell"):
        execute_taker(
            ledger,
            book,
            side=OrderSide.SELL,
            quantity=D("1"),
            limit_price=D("0.39"),
            time_in_force=TimeInForce.FAK,
            fee_schedule=fee_free(),
            trace=ref("book-sell", "sell"),
            execution_latency_ms=0,
            max_book_age_ms=10,
        )


def test_taker_fok_insufficient_cash_does_not_mutate_shared_depth() -> None:
    ledger = binary_ledger(cash="1")
    book = DepthBook.from_tuples(
        "yes-1",
        bids=[],
        asks=[("0.40", "2"), ("0.41", "2")],
        min_order_size=D("0.000001"),
        timestamp_ms=1,
    )
    before_depth = [(level.price, level.size) for level in book.asks]

    with pytest.raises(InsufficientCashError, match="insufficient cash"):
        execute_taker(
            ledger,
            book,
            side=OrderSide.BUY,
            quantity=D("3"),
            limit_price=D("0.41"),
            time_in_force=TimeInForce.FOK,
            fee_schedule=fee_free(),
            trace=ref("fok-no-cash", "fok-no-cash"),
            execution_latency_ms=0,
            max_book_age_ms=10,
        )

    assert [(level.price, level.size) for level in book.asks] == before_depth
    assert ledger.cash == D("1")
    assert ledger.position("yes-1") == D("0")
    assert ledger.trace == ()


def test_taker_sell_uses_best_bid_first_and_never_reuses_consumed_depth() -> None:
    ledger = binary_ledger()
    ledger.split(
        "market-1",
        D("6"),
        operation=operation_free(),
        trace=ref("split-sell", "sell-plan"),
    )
    book = DepthBook.from_tuples(
        "yes-1",
        bids=[("0.44", "2"), ("0.43", "3"), ("0.41", "20")],
        asks=[("0.45", "100")],
        min_order_size=D("0.000001"),
        timestamp_ms=2,
    )
    sold = execute_taker(
        ledger,
        book,
        side=OrderSide.SELL,
        quantity=D("4"),
        limit_price=D("0.43"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee_free(),
        trace=ref("sell-book", "sell-plan", 2),
        execution_latency_ms=0,
        max_book_age_ms=10,
    )
    assert [fill.price for fill in sold.fills] == [D("0.44"), D("0.43")]
    assert sold.notional == D("1.74")
    assert book.bids[0].price == D("0.43")
    assert book.bids[0].size == D("1")
    assert ledger.position("yes-1") == D("2")


def test_taker_rejects_missing_future_or_stale_book_and_traces_latency() -> None:
    missing = DepthBook.from_tuples(
        "yes-1",
        bids=[],
        asks=[("0.40", "2")],
        min_order_size=D("0.000001"),
    )
    ledger = binary_ledger()
    missing_result = execute_taker(
        ledger,
        missing,
        side=OrderSide.BUY,
        quantity=D("1"),
        limit_price=D("0.40"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee_free(),
        trace=ref("missing-book-time", "latency-check", 100),
        execution_latency_ms=5,
        max_book_age_ms=20,
    )
    assert missing_result.status is ExecutionStatus.KILLED
    assert missing_result.reason == "missing_book_timestamp"

    future = DepthBook.from_tuples(
        "yes-1",
        bids=[],
        asks=[("0.40", "2")],
        min_order_size=D("0.000001"),
        timestamp_ms=106,
    )
    future_result = execute_taker(
        ledger,
        future,
        side=OrderSide.BUY,
        quantity=D("1"),
        limit_price=D("0.40"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee_free(),
        trace=ref("future-book", "latency-check", 100),
        execution_latency_ms=5,
        max_book_age_ms=20,
    )
    assert future_result.reason == "book_timestamp_after_execution"

    stale = DepthBook.from_tuples(
        "yes-1",
        bids=[],
        asks=[("0.40", "2")],
        min_order_size=D("0.000001"),
        timestamp_ms=80,
    )
    stale_result = execute_taker(
        ledger,
        stale,
        side=OrderSide.BUY,
        quantity=D("1"),
        limit_price=D("0.40"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee_free(),
        trace=ref("stale-book", "latency-check", 100),
        execution_latency_ms=5,
        max_book_age_ms=20,
    )
    assert stale_result.reason == "book_too_old_at_execution"
    assert ledger.position("yes-1") == D("0")

    fresh = DepthBook.from_tuples(
        "yes-1",
        bids=[],
        asks=[("0.40", "2")],
        min_order_size=D("0.000001"),
        timestamp_ms=104,
    )
    filled = execute_taker(
        ledger,
        fresh,
        side=OrderSide.BUY,
        quantity=D("1"),
        limit_price=D("0.40"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee_free(),
        trace=ref("fresh-book", "latency-check", 100),
        execution_latency_ms=5,
        max_book_age_ms=20,
    )
    assert filled.status is ExecutionStatus.FILLED
    assert filled.fills[0].timestamp_ms == 105


def test_taker_enforces_minimum_size_tick_and_six_decimal_units() -> None:
    ledger = binary_ledger()
    book = DepthBook.from_tuples(
        "yes-1",
        bids=[],
        asks=[("0.40", "10")],
        min_order_size=D("5"),
        timestamp_ms=1,
    )
    below_minimum = execute_taker(
        ledger,
        book,
        side=OrderSide.BUY,
        quantity=D("1"),
        limit_price=D("0.40"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee_free(),
        trace=ref("min-order-book", "minimum-order", 1),
        execution_latency_ms=0,
        max_book_age_ms=0,
    )
    assert below_minimum.reason == "below_min_order_size"

    off_tick = execute_taker(
        ledger,
        book,
        side=OrderSide.BUY,
        quantity=D("5"),
        limit_price=D("0.405"),
        time_in_force=TimeInForce.FAK,
        fee_schedule=fee_free(),
        trace=ref("off-tick-book", "tick-check", 1),
        execution_latency_ms=0,
        max_book_age_ms=0,
    )
    assert off_tick.reason == "limit_price_not_aligned_to_tick"

    with pytest.raises(ValueError, match="6-decimal base units"):
        execute_taker(
            ledger,
            book,
            side=OrderSide.BUY,
            quantity=D("0.0000001"),
            limit_price=D("0.40"),
            time_in_force=TimeInForce.FAK,
            fee_schedule=fee_free(),
            trace=ref("sub-base-unit", "precision-check", 1),
            execution_latency_ms=0,
            max_book_age_ms=0,
        )


def maker_request(
    *,
    side: OrderSide = OrderSide.BUY,
    quantity: str = "8",
    capacity: str | None = None,
) -> MakerOrderRequest:
    return MakerOrderRequest(
        order_id="maker-1",
        market_id="market-1",
        token_id="yes-1",
        side=side,
        price=D("0.40"),
        quantity=D(quantity),
        capacity=D(capacity) if capacity is not None else None,
        tick_size=D("0.01"),
        fee_schedule=fee_free(),
        submit_latency_ms=100,
        cancel_latency_ms=50,
        minimum_live_ms=200,
        max_book_age_ms=500,
    )


def test_maker_touch_never_fills_then_trade_consumes_queue_and_capacity() -> None:
    ledger = binary_ledger()
    replay = MakerReplay(ledger, scenario=QueueScenario.NEUTRAL)
    order = replay.submit(
        maker_request(capacity="4"),
        visible_queue_ahead=D("3"),
        now_ms=1_000,
        book_timestamp_ms=1_000,
        trace=ref("book-submit", "quote-1", 1_000),
    )
    assert order.status is MakerStatus.PENDING_SUBMIT

    replay.on_book(
        BookEvent(
            token_id="yes-1",
            timestamp_ms=1_100,
            source_timestamp_ms=1_100,
            best_bid=D("0.40"),
            best_ask=D("0.41"),
            tick_size=D("0.01"),
            source_event_id="book-touch",
        )
    )
    assert order.status is MakerStatus.LIVE
    assert order.filled_quantity == D("0")

    fills = replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=1_150,
            price=D("0.40"),
            quantity=D("10"),
            aggressor_side=OrderSide.SELL,
            source_event_id="trade-1",
        )
    )
    assert len(fills) == 1
    assert fills[0].quantity == D("4")
    assert fills[0].source_event_id == "trade-1"
    assert fills[0].decision_id == "quote-1"
    assert order.filled_quantity == D("4")
    assert order.status is MakerStatus.CAPACITY_EXHAUSTED
    assert ledger.position("yes-1") == D("4")


def test_maker_submit_cancel_latency_minimum_live_and_late_fill() -> None:
    ledger = binary_ledger()
    replay = MakerReplay(ledger, scenario=QueueScenario.OPTIMISTIC)
    order = replay.submit(
        maker_request(quantity="5"),
        visible_queue_ahead=D("99"),
        now_ms=1_000,
        book_timestamp_ms=1_000,
        trace=ref("submit", "quote-late", 1_000),
    )
    replay.request_cancel(
        order.order_id,
        now_ms=1_120,
        trace=ref("cancel-request", "quote-late", 1_120),
    )
    # Live at 1100; minimum-live rule delays cancellation until 1300 even
    # though transport cancellation latency alone would finish at 1170.
    assert order.cancel_effective_ms == 1_300
    assert order.status is MakerStatus.CANCEL_PENDING

    late = replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=1_250,
            price=D("0.39"),
            quantity=D("2"),
            aggressor_side=OrderSide.SELL,
            source_event_id="trade-after-cancel-request",
        )
    )
    assert sum((fill.quantity for fill in late), D("0")) == D("2")
    assert order.status is MakerStatus.CANCEL_PENDING
    replay.advance_time(1_300, source_event_id="clock-1300")
    assert order.status is MakerStatus.CANCELLED
    assert order.filled_quantity == D("2")


def test_maker_stale_book_and_tick_change_fail_closed() -> None:
    stale_ledger = binary_ledger()
    stale = MakerReplay(stale_ledger).submit(
        maker_request(),
        visible_queue_ahead=D("0"),
        now_ms=2_000,
        book_timestamp_ms=1_000,
        trace=ref("stale-book", "stale-quote", 2_000),
    )
    assert stale.status is MakerStatus.STALE

    tick_ledger = binary_ledger()
    tick_replay = MakerReplay(tick_ledger)
    tick_order = tick_replay.submit(
        maker_request(),
        visible_queue_ahead=D("0"),
        now_ms=1_000,
        book_timestamp_ms=1_000,
        trace=ref("fresh-book", "tick-quote", 1_000),
    )
    tick_replay.on_book(
        BookEvent(
            token_id="yes-1",
            timestamp_ms=1_100,
            source_timestamp_ms=1_100,
            best_bid=D("0.40"),
            best_ask=D("0.42"),
            tick_size=D("0.03"),
            source_event_id="tick-change",
        )
    )
    assert tick_order.status is MakerStatus.STALE
    assert tick_order.stale_reason == "price_not_aligned_to_new_tick"


def test_maker_requires_fresh_book_context_at_trade_time() -> None:
    ledger = binary_ledger()
    replay = MakerReplay(ledger, scenario=QueueScenario.OPTIMISTIC)
    order = replay.submit(
        maker_request(),
        visible_queue_ahead=D("0"),
        now_ms=1_000,
        book_timestamp_ms=1_000,
        trace=ref("fresh-at-submit", "freshness", 1_000),
    )
    assert replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=1_600,
            price=D("0.40"),
            quantity=D("8"),
            aggressor_side=OrderSide.SELL,
            source_event_id="trade-with-expired-book",
        )
    ) == ()
    assert order.status is MakerStatus.STALE
    assert order.stale_reason == "book_context_expired_before_trade"


def test_maker_fill_fails_closed_if_reserved_cash_was_spent_elsewhere() -> None:
    ledger = binary_ledger(cash="10")
    replay = MakerReplay(ledger, scenario=QueueScenario.OPTIMISTIC)
    order = replay.submit(
        maker_request(quantity="20"),
        visible_queue_ahead=D("3"),
        now_ms=1,
        book_timestamp_ms=1,
        trace=ref("reservation-book", "reserved-maker", 1),
    )
    assert replay.reserved_cash == D("8.00")

    # This direct ledger action represents an out-of-band balance mutation.
    # The replay must invalidate the quote rather than consume queue and later
    # fill it after cash happens to return.
    ledger.split(
        "market-1",
        D("5"),
        operation=operation_free(),
        trace=ref("external-split", "other", 2),
    )
    fills = replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=3,
            price=D("0.40"),
            quantity=D("10"),
            aggressor_side=OrderSide.SELL,
            source_event_id="reservation-breach-trade",
        )
    )
    assert fills == ()
    assert order.status is MakerStatus.STALE
    assert order.stale_reason == "cash_reservation_breached"
    assert order.queue_ahead == D("0")

    ledger.merge(
        "market-1",
        D("5"),
        operation=operation_free(),
        trace=ref("external-merge", "other", 4),
    )
    assert replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=5,
            price=D("0.40"),
            quantity=D("10"),
            aggressor_side=OrderSide.SELL,
            source_event_id="post-breach-trade",
        )
    ) == ()
    assert ledger.position("yes-1") == D("0")


def test_maker_sell_reserves_inventory_and_fills_without_naked_short() -> None:
    ledger = binary_ledger()
    ledger.split(
        "market-1",
        D("3"),
        operation=operation_free(),
        trace=ref("maker-split", "maker-sell"),
    )
    replay = MakerReplay(ledger, scenario=QueueScenario.OPTIMISTIC)
    sell_request = MakerOrderRequest(
        order_id="sell-maker",
        market_id="market-1",
        token_id="yes-1",
        side=OrderSide.SELL,
        price=D("0.40"),
        quantity=D("3"),
        tick_size=D("0.01"),
        fee_schedule=fee_free(),
        max_book_age_ms=500,
    )
    order = replay.submit(
        sell_request,
        visible_queue_ahead=D("0"),
        now_ms=10,
        book_timestamp_ms=10,
        trace=ref("sell-submit", "maker-sell", 10),
    )
    with pytest.raises(InventoryError, match="naked sell reservation"):
        replay.submit(
            MakerOrderRequest(
                order_id="oversubscribed-sell",
                market_id="market-1",
                token_id="yes-1",
                side=OrderSide.SELL,
                price=D("0.41"),
                quantity=D("1"),
                tick_size=D("0.01"),
                fee_schedule=fee_free(),
            ),
            visible_queue_ahead=D("0"),
            now_ms=10,
            book_timestamp_ms=10,
            trace=ref("sell-submit-2", "maker-sell-2", 10),
        )
    fills = replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=20,
            price=D("0.41"),
            quantity=D("3"),
            aggressor_side=OrderSide.BUY,
            source_event_id="aggressive-buy",
        )
    )
    assert sum((fill.quantity for fill in fills), D("0")) == D("3")
    assert order.status is MakerStatus.FILLED
    assert ledger.position("yes-1") == D("0")
    assert ledger.position("no-1") == D("3")


def test_multiple_maker_orders_reserve_cash_and_cancel_releases_it() -> None:
    ledger = binary_ledger(cash="10")
    replay = MakerReplay(ledger, scenario=QueueScenario.NEUTRAL)

    first = MakerOrderRequest(
        order_id="cash-reservation-1",
        market_id="market-1",
        token_id="yes-1",
        side=OrderSide.BUY,
        price=D("0.40"),
        quantity=D("20"),
        tick_size=D("0.01"),
        fee_schedule=fee_free(),
        cancel_latency_ms=50,
        minimum_live_ms=200,
    )
    replay.submit(
        first,
        visible_queue_ahead=D("0"),
        now_ms=0,
        book_timestamp_ms=0,
        trace=ref("reserve-1", "reserve-decision-1", 0),
    )
    assert replay.reserved_cash == D("8.00")

    second = MakerOrderRequest(
        order_id="cash-reservation-2",
        market_id="market-1",
        token_id="yes-1",
        side=OrderSide.BUY,
        price=D("0.40"),
        quantity=D("6"),
        tick_size=D("0.01"),
        fee_schedule=fee_free(),
    )
    with pytest.raises(InsufficientCashError, match="cash reservation"):
        replay.submit(
            second,
            visible_queue_ahead=D("0"),
            now_ms=0,
            book_timestamp_ms=0,
            trace=ref("reserve-2-rejected", "reserve-decision-2", 0),
        )

    replay.request_cancel(
        first.order_id,
        now_ms=1,
        trace=ref("reserve-1-cancel", "reserve-decision-1", 1),
    )
    assert replay.get_order(first.order_id).status is MakerStatus.CANCEL_PENDING
    assert replay.reserved_cash == D("8.00")
    with pytest.raises(InsufficientCashError, match="cash reservation"):
        replay.submit(
            second,
            visible_queue_ahead=D("0"),
            now_ms=1,
            book_timestamp_ms=1,
            trace=ref("reserve-2-still-blocked", "reserve-decision-2", 1),
        )

    replay.advance_time(200, source_event_id="cancel-effective")
    assert replay.reserved_cash == D("0")
    accepted = replay.submit(
        second,
        visible_queue_ahead=D("0"),
        now_ms=200,
        book_timestamp_ms=200,
        trace=ref("reserve-2-accepted", "reserve-decision-2", 200),
    )
    assert accepted.status is MakerStatus.LIVE
    assert replay.reserved_cash == D("2.40")


def test_one_trade_print_has_shared_capacity_across_multiple_maker_orders() -> None:
    ledger = binary_ledger()
    replay = MakerReplay(ledger, scenario=QueueScenario.OPTIMISTIC)

    orders = [
        MakerOrderRequest(
            order_id="shared-print-better",
            market_id="market-1",
            token_id="yes-1",
            side=OrderSide.BUY,
            price=D("0.41"),
            quantity=D("4"),
            tick_size=D("0.01"),
            fee_schedule=fee_free("fixture://shared-print-better"),
        ),
        MakerOrderRequest(
            order_id="shared-print-worse",
            market_id="market-1",
            token_id="yes-1",
            side=OrderSide.BUY,
            price=D("0.40"),
            quantity=D("4"),
            tick_size=D("0.01"),
            fee_schedule=fee_free("fixture://shared-print-worse"),
        ),
    ]
    submitted = [
        replay.submit(
            request,
            visible_queue_ahead=D("0"),
            now_ms=0,
            book_timestamp_ms=0,
            trace=ref(
                f"submit-{request.order_id}",
                f"decision-{request.order_id}",
                0,
            ),
        )
        for request in orders
    ]
    fills = replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=100,
            price=D("0.40"),
            quantity=D("5"),
            aggressor_side=OrderSide.SELL,
            source_event_id="one-public-print",
        )
    )

    assert sum((fill.quantity for fill in fills), D("0")) == D("5")
    assert [(fill.order_id, fill.quantity) for fill in fills] == [
        ("shared-print-better", D("4")),
        ("shared-print-worse", D("1")),
    ]
    assert submitted[0].status is MakerStatus.FILLED
    assert submitted[1].status is MakerStatus.PARTIAL
    assert ledger.position("yes-1") == D("5")
    assert all(fill.source_event_id == "one-public-print" for fill in fills)

    duplicate = replay.on_trade(
        TradeEvent(
            token_id="yes-1",
            timestamp_ms=100,
            price=D("0.40"),
            quantity=D("5"),
            aggressor_side=OrderSide.SELL,
            source_event_id="one-public-print",
        )
    )
    assert duplicate == ()
    assert ledger.position("yes-1") == D("5")

    with pytest.raises(ValueError, match="conflicting duplicate trade event"):
        replay.on_trade(
            TradeEvent(
                token_id="yes-1",
                timestamp_ms=100,
                price=D("0.40"),
                quantity=D("6"),
                aggressor_side=OrderSide.SELL,
                source_event_id="one-public-print",
            )
        )


def test_queue_scenarios_are_explicit_deterministic_evidence_bounds() -> None:
    def filled(scenario: QueueScenario) -> Decimal:
        ledger = binary_ledger()
        replay = MakerReplay(ledger, scenario=scenario)
        order = replay.submit(
            maker_request(),
            visible_queue_ahead=D("5"),
            now_ms=0,
            book_timestamp_ms=0,
            trace=ref(f"submit-{scenario.value}", f"decision-{scenario.value}", 0),
        )
        replay.on_trade(
            TradeEvent(
                token_id="yes-1",
                timestamp_ms=100,
                price=D("0.40"),
                quantity=D("10"),
                aggressor_side=OrderSide.SELL,
                source_event_id=f"trade-{scenario.value}",
            )
        )
        return order.filled_quantity

    assert filled(QueueScenario.OPTIMISTIC) == D("8")
    assert filled(QueueScenario.NEUTRAL) == D("5")
    assert filled(QueueScenario.PESSIMISTIC) == D("0")
    assert QueueScenario.OPTIMISTIC.description
    assert QueueScenario.NEUTRAL.description
    assert QueueScenario.PESSIMISTIC.description
