from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from src.edge_lab.btc_twap_pair_pricing import (
    PairExecutionMode,
    PairPricingPolicy,
    joint_quantity_breakpoints,
    quote_pair_buy,
    select_healthy_pair_books,
)
from src.edge_lab.btc_twap_relative_value import OrderBookSnapshot
from src.edge_lab.execution import ExecutionFeeSchedule
from tests.test_btc_twap_relative_value_readiness import _book
from tests.test_btc_twap_relative_value_v07_replay import _v07_pair

D = Decimal


def test_pair_buy_quote_reports_all_three_execution_cost_shapes() -> None:
    original = _v07_pair()
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="execution-shape-test",
        source_ref="unit-fixture",
    )
    first_contract = replace(original.market_5, fee_schedule=fee_exempt)
    second_contract = replace(original.market_15, fee_schedule=fee_exempt)
    first_book = _book(first_contract.up_token_id, bid="0.39", ask="0.40")
    second_book = _book(second_contract.down_token_id, bid="0.59", ask="0.60")

    taker_taker = quote_pair_buy(
        quantity=D("1"),
        first_book=first_book,
        second_book=second_book,
        first_contract=first_contract,
        second_contract=second_contract,
        execution_mode=PairExecutionMode.TAKER_TAKER,
    )
    maker_taker = quote_pair_buy(
        quantity=D("1"),
        first_book=first_book,
        second_book=second_book,
        first_contract=first_contract,
        second_contract=second_contract,
        execution_mode=PairExecutionMode.MAKER_TAKER,
    )
    maker_maker = quote_pair_buy(
        quantity=D("1"),
        first_book=first_book,
        second_book=second_book,
        first_contract=first_contract,
        second_contract=second_contract,
        execution_mode=PairExecutionMode.MAKER_MAKER,
    )

    assert taker_taker is not None
    assert taker_taker.cost_per_pair == D("1.00")
    assert taker_taker.maker_token_ids == ()
    assert maker_taker is not None
    assert maker_taker.cost_per_pair == D("0.99")
    assert maker_taker.maker_token_ids == (first_contract.up_token_id,)
    assert maker_maker is not None
    assert maker_maker.cost_per_pair == D("0.98")
    assert maker_maker.maker_token_ids == (
        first_contract.up_token_id,
        second_contract.down_token_id,
    )


def test_maker_taker_cost_includes_the_taker_leg_fee() -> None:
    pair = _v07_pair()
    first_book = _book(pair.market_5.up_token_id, bid="0.40", ask="0.41")
    second_book = _book(pair.market_15.down_token_id, bid="0.60", ask="0.61")

    quote = quote_pair_buy(
        quantity=D("1"),
        first_book=first_book,
        second_book=second_book,
        first_contract=pair.market_5,
        second_contract=pair.market_15,
        execution_mode=PairExecutionMode.MAKER_TAKER,
    )

    assert quote is not None
    assert quote.notional_per_pair == D("1.01")
    assert quote.fee_per_pair > D("0")
    assert quote.cost_per_pair > D("1.01")


def test_structural_maker_taker_accepts_only_the_sides_it_will_use() -> None:
    pair = _v07_pair()
    maker_book = OrderBookSnapshot.from_tuples(
        pair.market_15.up_token_id,
        bids=((D("0.99"), D("8")),),
        asks=(),
        timestamp_ms=1_000,
        tick_size=pair.market_15.tick_size,
        minimum_order_size=pair.market_15.minimum_order_size,
    )
    taker_book = OrderBookSnapshot.from_tuples(
        pair.market_5.down_token_id,
        bids=(),
        asks=((D("0.01"), D("8")),),
        timestamp_ms=1_000,
        tick_size=pair.market_5.tick_size,
        minimum_order_size=pair.market_5.minimum_order_size,
    )
    policy = PairPricingPolicy(structural_only=True, pair_risk_usdc=None)

    selected = select_healthy_pair_books(
        first_token_id=maker_book.token_id,
        second_token_id=taker_book.token_id,
        books={maker_book.token_id: maker_book, taker_book.token_id: taker_book},
        policy=policy,
        first_contract=pair.market_15,
        second_contract=pair.market_5,
        execution_mode=PairExecutionMode.MAKER_TAKER,
    )

    assert selected == (maker_book, taker_book)
    quantities = joint_quantity_breakpoints(
        first_book=maker_book,
        second_book=taker_book,
        first_contract=pair.market_15,
        second_contract=pair.market_5,
        policy=policy,
        execution_mode=PairExecutionMode.MAKER_TAKER,
    )
    assert quantities[-1] == D("8")
    quote = quote_pair_buy(
        quantity=D("8"),
        first_book=maker_book,
        second_book=taker_book,
        first_contract=pair.market_15,
        second_contract=pair.market_5,
        execution_mode=PairExecutionMode.MAKER_TAKER,
    )
    assert quote is not None
    assert quote.maker_token_ids == (maker_book.token_id,)


def test_structural_double_maker_needs_bids_but_not_asks() -> None:
    pair = _v07_pair()
    books = tuple(
        OrderBookSnapshot.from_tuples(
            token_id,
            bids=((price, D("7")),),
            asks=(),
            timestamp_ms=1_000,
            tick_size=contract.tick_size,
            minimum_order_size=contract.minimum_order_size,
        )
        for token_id, price, contract in (
            (pair.market_15.up_token_id, D("0.98"), pair.market_15),
            (pair.market_5.down_token_id, D("0.01"), pair.market_5),
        )
    )
    policy = PairPricingPolicy(structural_only=True, pair_risk_usdc=None)

    assert select_healthy_pair_books(
        first_token_id=books[0].token_id,
        second_token_id=books[1].token_id,
        books={book.token_id: book for book in books},
        policy=policy,
        first_contract=pair.market_15,
        second_contract=pair.market_5,
        execution_mode=PairExecutionMode.MAKER_MAKER,
    ) == books
    assert select_healthy_pair_books(
        first_token_id=books[0].token_id,
        second_token_id=books[1].token_id,
        books={book.token_id: book for book in books},
        policy=policy,
        first_contract=pair.market_15,
        second_contract=pair.market_5,
        execution_mode=PairExecutionMode.TAKER_TAKER,
    ) is None
