"""Mechanism-level economics for paired complete-set market making.

No fill probability is invented here.  The functions expose the payoff of a
paired fill, the loss from a one-legged fill followed by an executable hedge,
and a transparent proxy for liquidity-reward competition.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable

from .models import (
    ONE,
    ZERO,
    BookLevel,
    DepthFill,
    FeeSchedule,
    OrderBook,
    PairedAskQuote,
    PairedQuote,
)


FEE_QUANTUM = Decimal("0.00001")
CENT = Decimal("0.01")


def taker_fee(
    shares: Decimal,
    price: Decimal,
    schedule: FeeSchedule,
) -> Decimal:
    """Return the current Polymarket taker fee in pUSD/USDC equivalent.

    Formula: C * feeRate * (p * (1-p)) ** exponent.  The public documentation
    specifies five-decimal precision.
    """
    if shares <= ZERO or schedule.rate <= ZERO:
        return ZERO
    probability_term = price * (ONE - price)
    if probability_term <= ZERO:
        return ZERO
    raw = shares * schedule.rate * (probability_term**schedule.exponent)
    if raw < FEE_QUANTUM:
        return ZERO
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def round_to_tick(price: Decimal, tick: Decimal, *, up: bool = False) -> Decimal:
    if tick <= ZERO:
        raise ValueError("tick must be positive")
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    units = (price / tick).to_integral_value(rounding=rounding)
    rounded = units * tick
    return min(max(rounded, tick), ONE - tick)


def combined_yes_mid(yes_book: OrderBook, no_book: OrderBook) -> Decimal:
    """Blend the two independently observed books into a YES reference price."""
    mids: list[Decimal] = []
    if yes_book.midpoint is not None:
        mids.append(yes_book.midpoint)
    if no_book.midpoint is not None:
        mids.append(ONE - no_book.midpoint)
    if not mids:
        raise ValueError("both books are missing a two-sided midpoint")
    return sum(mids, ZERO) / Decimal(len(mids))


def build_paired_bid_quote(
    yes_book: OrderBook,
    no_book: OrderBook,
    *,
    max_spread_cents: Decimal,
    size: Decimal,
    distance_fraction: Decimal = Decimal("0.5"),
) -> PairedQuote:
    """Build balanced YES/NO bids inside the reward-eligible distance.

    The quotes are passive bids on both outcome tokens.  When both fill, one
    YES plus one NO can be merged into one unit of collateral.  This avoids the
    naked-short assumption in the old simulator.
    """
    if max_spread_cents <= ZERO:
        raise ValueError("max_spread_cents must be positive")
    if not (ZERO < distance_fraction < ONE):
        raise ValueError("distance_fraction must be between 0 and 1")
    if size <= ZERO:
        raise ValueError("size must be positive")

    yes_mid = combined_yes_mid(yes_book, no_book)
    max_distance = max_spread_cents * CENT
    target_distance = max_distance * distance_fraction
    tick = max(yes_book.tick_size, no_book.tick_size)

    yes_bid = round_to_tick(yes_mid - target_distance, tick)
    no_bid = round_to_tick((ONE - yes_mid) - target_distance, tick)

    # Post-only safety: never produce a bid that crosses the currently visible
    # best ask.  One tick below is the nearest passive fallback.
    if yes_book.best_ask is not None and yes_bid >= yes_book.best_ask:
        yes_bid = round_to_tick(yes_book.best_ask - tick, tick)
    if no_book.best_ask is not None and no_bid >= no_book.best_ask:
        no_bid = round_to_tick(no_book.best_ask - tick, tick)

    actual_distance = max(
        abs(yes_mid - yes_bid),
        abs((ONE - yes_mid) - no_bid),
    )
    return PairedQuote(
        yes_bid=yes_bid,
        no_bid=no_bid,
        size=size,
        reference_yes_mid=yes_mid,
        distance_cents=actual_distance / CENT,
    )


def build_paired_ask_quote(
    yes_book: OrderBook,
    no_book: OrderBook,
    *,
    max_spread_cents: Decimal,
    size: Decimal,
    distance_fraction: Decimal = Decimal("0.5"),
) -> PairedAskQuote:
    """Build balanced passive asks backed by a pre-split complete set."""
    if max_spread_cents <= ZERO:
        raise ValueError("max_spread_cents must be positive")
    if not (ZERO < distance_fraction < ONE):
        raise ValueError("distance_fraction must be between 0 and 1")
    if size <= ZERO:
        raise ValueError("size must be positive")

    yes_mid = combined_yes_mid(yes_book, no_book)
    target_distance = max_spread_cents * CENT * distance_fraction
    tick = max(yes_book.tick_size, no_book.tick_size)
    yes_ask = round_to_tick(yes_mid + target_distance, tick, up=True)
    no_ask = round_to_tick((ONE - yes_mid) + target_distance, tick, up=True)

    if yes_book.best_bid is not None and yes_ask <= yes_book.best_bid:
        yes_ask = round_to_tick(yes_book.best_bid + tick, tick, up=True)
    if no_book.best_bid is not None and no_ask <= no_book.best_bid:
        no_ask = round_to_tick(no_book.best_bid + tick, tick, up=True)

    actual_distance = max(
        abs(yes_ask - yes_mid),
        abs(no_ask - (ONE - yes_mid)),
    )
    return PairedAskQuote(
        yes_ask=yes_ask,
        no_ask=no_ask,
        size=size,
        reference_yes_mid=yes_mid,
        distance_cents=actual_distance / CENT,
    )


def liquidity_score(
    max_spread_cents: Decimal,
    distance_cents: Decimal,
    size: Decimal,
    *,
    in_game_multiplier: Decimal = ONE,
) -> Decimal:
    """Official quadratic per-order score, before trader normalization."""
    if (
        max_spread_cents <= ZERO
        or size <= ZERO
        or distance_cents < ZERO
        or distance_cents > max_spread_cents
    ):
        return ZERO
    closeness = (max_spread_cents - distance_cents) / max_spread_cents
    return closeness * closeness * in_game_multiplier * size


def _bid_side_score(
    book: OrderBook,
    *,
    midpoint: Decimal,
    max_spread_cents: Decimal,
    min_size: Decimal,
) -> Decimal:
    """Aggregate visible qualifying bids as a public competition proxy.

    Reward allocation is per maker address, which public L2 books do not expose.
    This aggregate therefore remains a proxy and is deliberately reported as
    such; it is never treated as realized income.
    """
    score = ZERO
    for level in book.bids:
        if level.size < min_size:
            continue
        distance = abs(midpoint - level.price) / CENT
        score += liquidity_score(max_spread_cents, distance, level.size)
    return score


def reward_competition_proxy(
    yes_book: OrderBook,
    no_book: OrderBook,
    *,
    max_spread_cents: Decimal,
    min_size: Decimal,
) -> dict[str, Decimal]:
    yes_mid = combined_yes_mid(yes_book, no_book)
    yes_side = _bid_side_score(
        yes_book,
        midpoint=yes_mid,
        max_spread_cents=max_spread_cents,
        min_size=min_size,
    )
    no_side = _bid_side_score(
        no_book,
        midpoint=ONE - yes_mid,
        max_spread_cents=max_spread_cents,
        min_size=min_size,
    )

    # Two-sided makers score by the smaller side.  Without maker identities,
    # min(aggregate sides) is a useful static-book proxy, not an exact score.
    two_sided = min(yes_side, no_side)
    if Decimal("0.10") <= yes_mid <= Decimal("0.90"):
        single_side = max(yes_side, no_side) / Decimal("3")
        q_min_proxy = max(two_sided, single_side)
    else:
        q_min_proxy = two_sided
    return {
        "yes_side": yes_side,
        "no_side": no_side,
        "q_min_proxy": q_min_proxy,
    }


def walk_asks(
    asks: Iterable[BookLevel],
    size: Decimal,
    *,
    stress_price: Decimal = ZERO,
) -> DepthFill:
    """Buy through visible ask depth; never assume missing liquidity."""
    remaining = size
    filled = ZERO
    notional = ZERO
    for level in sorted(asks, key=lambda item: item.price):
        if remaining <= ZERO:
            break
        quantity = min(remaining, level.size)
        price = min(ONE, level.price + stress_price)
        notional += quantity * price
        filled += quantity
        remaining -= quantity
    average = notional / filled if filled > ZERO else None
    return DepthFill(
        requested_size=size,
        filled_size=filled,
        notional=notional,
        average_price=average,
        complete=remaining <= ZERO,
    )


def walk_bids(
    bids: Iterable[BookLevel],
    size: Decimal,
    *,
    stress_price: Decimal = ZERO,
) -> DepthFill:
    """Sell through visible bid depth; never assume missing liquidity."""
    remaining = size
    filled = ZERO
    notional = ZERO
    for level in sorted(bids, key=lambda item: item.price, reverse=True):
        if remaining <= ZERO:
            break
        quantity = min(remaining, level.size)
        price = max(ZERO, level.price - stress_price)
        notional += quantity * price
        filled += quantity
        remaining -= quantity
    average = notional / filled if filled > ZERO else None
    return DepthFill(
        requested_size=size,
        filled_size=filled,
        notional=notional,
        average_price=average,
        complete=remaining <= ZERO,
    )


def _walk_fee(
    levels: Iterable[BookLevel],
    size: Decimal,
    *,
    schedule: FeeSchedule,
    stress_price: Decimal,
    ascending: bool,
) -> Decimal:
    """Apply the nonlinear fee at each visible depth level."""
    remaining = size
    fee = ZERO
    ordered = sorted(levels, key=lambda item: item.price, reverse=not ascending)
    for level in ordered:
        if remaining <= ZERO:
            break
        quantity = min(remaining, level.size)
        price = (
            min(ONE, level.price + stress_price)
            if ascending
            else max(ZERO, level.price - stress_price)
        )
        fee += taker_fee(quantity, price, schedule)
        remaining -= quantity
    return fee


def _single_leg_hedge(
    maker_bid: Decimal,
    complement_book: OrderBook,
    *,
    size: Decimal,
    fee_schedule: FeeSchedule,
    stress_ticks: int,
) -> dict[str, Any]:
    stress_price = complement_book.tick_size * Decimal(stress_ticks)
    fill = walk_asks(complement_book.asks, size, stress_price=stress_price)
    if not fill.complete or fill.average_price is None:
        return {
            "complete": False,
            "filled_size": fill.filled_size,
            "average_hedge_price": fill.average_price,
            "taker_fee": None,
            "merge_pnl": None,
        }
    fee = _walk_fee(
        complement_book.asks,
        size,
        schedule=fee_schedule,
        stress_price=stress_price,
        ascending=True,
    )
    merge_pnl = size - (maker_bid * size) - fill.notional - fee
    return {
        "complete": True,
        "filled_size": fill.filled_size,
        "average_hedge_price": fill.average_price,
        "taker_fee": fee,
        "merge_pnl": merge_pnl,
    }


def _single_leg_sell_hedge(
    maker_ask: Decimal,
    complement_book: OrderBook,
    *,
    size: Decimal,
    fee_schedule: FeeSchedule,
    stress_ticks: int,
) -> dict[str, Any]:
    """After one maker ask fills, sell the remaining token into visible bids."""
    stress_price = complement_book.tick_size * Decimal(stress_ticks)
    fill = walk_bids(complement_book.bids, size, stress_price=stress_price)
    if not fill.complete or fill.average_price is None:
        return {
            "complete": False,
            "filled_size": fill.filled_size,
            "average_hedge_price": fill.average_price,
            "taker_fee": None,
            "complete_set_pnl": None,
        }
    fee = _walk_fee(
        complement_book.bids,
        size,
        schedule=fee_schedule,
        stress_price=stress_price,
        ascending=False,
    )
    pnl = (maker_ask * size) + fill.notional - size - fee
    return {
        "complete": True,
        "filled_size": fill.filled_size,
        "average_hedge_price": fill.average_price,
        "taker_fee": fee,
        "complete_set_pnl": pnl,
    }


def evaluate_paired_bid_quote(
    quote: PairedQuote,
    yes_book: OrderBook,
    no_book: OrderBook,
    *,
    daily_reward: Decimal,
    reward_max_spread_cents: Decimal,
    reward_min_size: Decimal,
    fee_schedule: FeeSchedule,
    stress_ticks: int = 2,
    competition_multiplier: Decimal = Decimal("3"),
) -> dict[str, Any]:
    """Evaluate paired-fill economics and both one-leg hedge paths."""
    if competition_multiplier < ONE:
        raise ValueError("competition_multiplier must be at least 1")

    own_yes = liquidity_score(
        reward_max_spread_cents,
        abs(quote.reference_yes_mid - quote.yes_bid) / CENT,
        quote.size,
    )
    own_no = liquidity_score(
        reward_max_spread_cents,
        abs((ONE - quote.reference_yes_mid) - quote.no_bid) / CENT,
        quote.size,
    )
    own_score = min(own_yes, own_no)
    competition = reward_competition_proxy(
        yes_book,
        no_book,
        max_spread_cents=reward_max_spread_cents,
        min_size=reward_min_size,
    )
    stressed_competition = competition["q_min_proxy"] * competition_multiplier
    denominator = own_score + stressed_competition
    static_share = own_score / denominator if denominator > ZERO else ZERO
    reward_scenario = daily_reward * static_share

    yes_first = _single_leg_hedge(
        quote.yes_bid,
        no_book,
        size=quote.size,
        fee_schedule=fee_schedule,
        stress_ticks=stress_ticks,
    )
    no_first = _single_leg_hedge(
        quote.no_bid,
        yes_book,
        size=quote.size,
        fee_schedule=fee_schedule,
        stress_ticks=stress_ticks,
    )
    completed_pnls = [
        path["merge_pnl"]
        for path in (yes_first, no_first)
        if path["complete"] and path["merge_pnl"] is not None
    ]
    worst_hedge_pnl = min(completed_pnls) if len(completed_pnls) == 2 else None
    worst_hedge_loss = (
        max(ZERO, -worst_hedge_pnl) if worst_hedge_pnl is not None else None
    )
    break_even_pair_to_single = None
    if worst_hedge_loss is not None and quote.paired_margin > ZERO:
        break_even_pair_to_single = worst_hedge_loss / quote.paired_margin

    return {
        "quote": {
            "yes_bid": quote.yes_bid,
            "no_bid": quote.no_bid,
            "size": quote.size,
            "reference_yes_mid": quote.reference_yes_mid,
            "distance_cents": quote.distance_cents,
            "capital": quote.capital,
        },
        "paired_fill": {
            "margin_per_share": quote.paired_margin_per_share,
            "margin": quote.paired_margin,
        },
        "one_leg_hedges": {
            "yes_fills_first": yes_first,
            "no_fills_first": no_first,
            "stress_ticks": stress_ticks,
            "worst_completed_pnl": worst_hedge_pnl,
            "worst_loss": worst_hedge_loss,
            "required_paired_fills_per_single_fill": break_even_pair_to_single,
        },
        "liquidity_reward_scenario": {
            "own_yes_score": own_yes,
            "own_no_score": own_no,
            "own_q_min": own_score,
            "visible_competition_proxy": competition,
            "competition_multiplier": competition_multiplier,
            "static_share": static_share,
            "daily_reward_if_static": reward_scenario,
            "warning": (
                "Scenario only: public books omit maker identity and future "
                "competition; reward is sampled and not guaranteed."
            ),
        },
    }


def evaluate_paired_ask_quote(
    quote: PairedAskQuote,
    yes_book: OrderBook,
    no_book: OrderBook,
    *,
    daily_reward: Decimal,
    reward_max_spread_cents: Decimal,
    reward_min_size: Decimal,
    fee_schedule: FeeSchedule,
    stress_ticks: int = 2,
    competition_multiplier: Decimal = Decimal("3"),
) -> dict[str, Any]:
    """Evaluate pre-split paired asks and both one-leg liquidation paths."""
    if competition_multiplier < ONE:
        raise ValueError("competition_multiplier must be at least 1")
    own_yes = liquidity_score(
        reward_max_spread_cents,
        abs(quote.yes_ask - quote.reference_yes_mid) / CENT,
        quote.size,
    )
    own_no = liquidity_score(
        reward_max_spread_cents,
        abs(quote.no_ask - (ONE - quote.reference_yes_mid)) / CENT,
        quote.size,
    )
    own_score = min(own_yes, own_no)
    competition = reward_competition_proxy(
        yes_book,
        no_book,
        max_spread_cents=reward_max_spread_cents,
        min_size=reward_min_size,
    )
    stressed_competition = competition["q_min_proxy"] * competition_multiplier
    denominator = own_score + stressed_competition
    static_share = own_score / denominator if denominator > ZERO else ZERO

    yes_first = _single_leg_sell_hedge(
        quote.yes_ask,
        no_book,
        size=quote.size,
        fee_schedule=fee_schedule,
        stress_ticks=stress_ticks,
    )
    no_first = _single_leg_sell_hedge(
        quote.no_ask,
        yes_book,
        size=quote.size,
        fee_schedule=fee_schedule,
        stress_ticks=stress_ticks,
    )
    completed_pnls = [
        path["complete_set_pnl"]
        for path in (yes_first, no_first)
        if path["complete"] and path["complete_set_pnl"] is not None
    ]
    worst_pnl = min(completed_pnls) if len(completed_pnls) == 2 else None
    worst_loss = max(ZERO, -worst_pnl) if worst_pnl is not None else None
    break_even = None
    if worst_loss is not None and quote.paired_margin > ZERO:
        break_even = worst_loss / quote.paired_margin

    return {
        "quote": {
            "yes_ask": quote.yes_ask,
            "no_ask": quote.no_ask,
            "size": quote.size,
            "reference_yes_mid": quote.reference_yes_mid,
            "distance_cents": quote.distance_cents,
            "capital_for_split_inventory": quote.capital,
        },
        "paired_fill": {
            "margin_per_share": quote.paired_margin_per_share,
            "margin": quote.paired_margin,
        },
        "one_leg_hedges": {
            "yes_sells_first": yes_first,
            "no_sells_first": no_first,
            "stress_ticks": stress_ticks,
            "worst_completed_pnl": worst_pnl,
            "worst_loss": worst_loss,
            "required_paired_fills_per_single_fill": break_even,
        },
        "liquidity_reward_scenario": {
            "own_yes_score": own_yes,
            "own_no_score": own_no,
            "own_q_min": own_score,
            "visible_competition_proxy": competition,
            "competition_multiplier": competition_multiplier,
            "static_share": static_share,
            "daily_reward_if_static": daily_reward * static_share,
            "warning": (
                "Scenario only: public books omit maker identity and future "
                "competition; reward is sampled and not guaranteed."
            ),
        },
    }


def evaluate_taker_complete_sets(
    yes_book: OrderBook,
    no_book: OrderBook,
    *,
    sizes: Iterable[Decimal],
    fee_schedule: FeeSchedule,
    execution_buffer_per_share: Decimal = Decimal("0.002"),
) -> list[dict[str, Any]]:
    """Depth-aware read-only scan of buy-both and sell-both complete sets."""
    results: list[dict[str, Any]] = []
    for size in sizes:
        yes_buy = walk_asks(yes_book.asks, size)
        no_buy = walk_asks(no_book.asks, size)
        yes_sell = walk_bids(yes_book.bids, size)
        no_sell = walk_bids(no_book.bids, size)
        buffer = execution_buffer_per_share * size

        buy_edge = None
        buy_fees = None
        if yes_buy.complete and no_buy.complete:
            buy_fees = _walk_fee(
                yes_book.asks,
                size,
                schedule=fee_schedule,
                stress_price=ZERO,
                ascending=True,
            ) + _walk_fee(
                no_book.asks,
                size,
                schedule=fee_schedule,
                stress_price=ZERO,
                ascending=True,
            )
            buy_edge = size - yes_buy.notional - no_buy.notional - buy_fees - buffer

        sell_edge = None
        sell_fees = None
        if yes_sell.complete and no_sell.complete:
            sell_fees = _walk_fee(
                yes_book.bids,
                size,
                schedule=fee_schedule,
                stress_price=ZERO,
                ascending=False,
            ) + _walk_fee(
                no_book.bids,
                size,
                schedule=fee_schedule,
                stress_price=ZERO,
                ascending=False,
            )
            sell_edge = (
                yes_sell.notional
                + no_sell.notional
                - size
                - sell_fees
                - buffer
            )

        results.append(
            {
                "size": size,
                "buy_both": {
                    "executable": yes_buy.complete and no_buy.complete,
                    "yes_cost": yes_buy.notional,
                    "no_cost": no_buy.notional,
                    "fees": buy_fees,
                    "buffer": buffer,
                    "net_edge": buy_edge,
                },
                "sell_both_from_split_inventory": {
                    "executable": yes_sell.complete and no_sell.complete,
                    "yes_proceeds": yes_sell.notional,
                    "no_proceeds": no_sell.notional,
                    "fees": sell_fees,
                    "buffer": buffer,
                    "net_edge": sell_edge,
                },
            }
        )
    return results


def json_safe(value: Any) -> Any:
    """Convert nested Decimal/dataclass structures to JSON-compatible values."""
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "__dataclass_fields__"):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
