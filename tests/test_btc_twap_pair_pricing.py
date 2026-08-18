from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from src.edge_lab.btc_twap_pair_pricing import (
    PairExecutionMode,
    quote_pair_buy,
)
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
