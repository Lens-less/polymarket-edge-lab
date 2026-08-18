"""Version-neutral, exact-decimal pricing helpers for two-leg BTC TWAP pairs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from enum import Enum

from .btc_twap_relative_value import OrderBookSnapshot, TwapMarketContract

ZERO = Decimal(0)
ONE = Decimal(1)
SIZE_QUANTUM = Decimal("0.000001")


class PairExecutionMode(str, Enum):
    """Execution-cost shapes used by structural readiness diagnostics."""

    TAKER_TAKER = "taker_taker"
    MAKER_TAKER = "maker_taker"
    MAKER_MAKER = "maker_maker"


@dataclass(frozen=True)
class PairPricingPolicy:
    """Explicit execution assumptions shared by readiness diagnostics."""

    maximum_spread_each_leg: Decimal = Decimal("0.03")
    minimum_market_price: Decimal = Decimal("0.05")
    maximum_market_price: Decimal = Decimal("0.95")
    pair_risk_usdc: Decimal | None = Decimal(25)
    structural_only: bool = False

    def __post_init__(self) -> None:
        for name in (
            "maximum_spread_each_leg",
            "minimum_market_price",
            "maximum_market_price",
        ):
            value = getattr(self, name)
            if isinstance(value, (bool, float)):
                raise TypeError(f"{name} must be an exact decimal")
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
            if not parsed.is_finite():
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, parsed)
        if self.pair_risk_usdc is not None:
            value = self.pair_risk_usdc
            if isinstance(value, (bool, float)):
                raise TypeError("pair_risk_usdc must be an exact decimal or None")
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
            if not parsed.is_finite():
                raise ValueError("pair_risk_usdc must be finite")
            object.__setattr__(self, "pair_risk_usdc", parsed)
        if self.maximum_spread_each_leg < ZERO:
            raise ValueError("maximum_spread_each_leg cannot be negative")
        if not ZERO <= self.minimum_market_price < self.maximum_market_price <= ONE:
            raise ValueError("market price bounds are invalid")
        if self.pair_risk_usdc is not None and self.pair_risk_usdc <= ZERO:
            raise ValueError("pair_risk_usdc must be positive")
        if not isinstance(self.structural_only, bool):
            raise TypeError("structural_only must be bool")


@dataclass(frozen=True)
class BookBuyQuote:
    notional: Decimal
    fee: Decimal


@dataclass(frozen=True)
class PairBuyQuote:
    quantity: Decimal
    first: BookBuyQuote
    second: BookBuyQuote
    notional_per_pair: Decimal
    fee_per_pair: Decimal
    cost_per_pair: Decimal
    total_cost: Decimal
    execution_mode: PairExecutionMode
    maker_token_ids: tuple[str, ...]


def quote_book_buy(
    book: OrderBookSnapshot,
    *,
    quantity: Decimal,
    contract: TwapMarketContract,
    maker: bool = False,
) -> BookBuyQuote | None:
    """Price a taker buy from asks or a hypothetical resting buy at best bid."""

    if quantity <= ZERO:
        raise ValueError("quantity must be positive")
    if maker:
        if book.best_bid is None:
            return None
        return BookBuyQuote(
            notional=quantity * book.best_bid,
            fee=contract.fee_schedule.fee(quantity, book.best_bid, maker=True),
        )
    remaining = quantity
    notional = ZERO
    fee = ZERO
    for level in book.asks:
        if remaining <= ZERO:
            break
        filled = min(remaining, level.size)
        notional += filled * level.price
        fee += contract.fee_schedule.fee(filled, level.price, maker=False)
        remaining -= filled
    if remaining > ZERO:
        return None
    return BookBuyQuote(notional=notional, fee=fee)


def quote_pair_buy(
    *,
    quantity: Decimal,
    first_book: OrderBookSnapshot,
    second_book: OrderBookSnapshot,
    first_contract: TwapMarketContract,
    second_contract: TwapMarketContract,
    execution_mode: PairExecutionMode = PairExecutionMode.TAKER_TAKER,
) -> PairBuyQuote | None:
    """Return exact two-leg all-in cost at one common executable quantity."""

    mode = (
        execution_mode
        if isinstance(execution_mode, PairExecutionMode)
        else PairExecutionMode(execution_mode)
    )

    def quote_roles(*, first_maker: bool, second_maker: bool) -> PairBuyQuote | None:
        first_quote = quote_book_buy(
            first_book,
            quantity=quantity,
            contract=first_contract,
            maker=first_maker,
        )
        second_quote = quote_book_buy(
            second_book,
            quantity=quantity,
            contract=second_contract,
            maker=second_maker,
        )
        if first_quote is None or second_quote is None:
            return None
        total_cost = (
            first_quote.notional
            + first_quote.fee
            + second_quote.notional
            + second_quote.fee
        )
        maker_token_ids = tuple(
            token_id
            for token_id, maker in (
                (first_book.token_id, first_maker),
                (second_book.token_id, second_maker),
            )
            if maker
        )
        return PairBuyQuote(
            quantity=quantity,
            first=first_quote,
            second=second_quote,
            notional_per_pair=(
                first_quote.notional + second_quote.notional
            )
            / quantity,
            fee_per_pair=(first_quote.fee + second_quote.fee) / quantity,
            cost_per_pair=total_cost / quantity,
            total_cost=total_cost,
            execution_mode=mode,
            maker_token_ids=maker_token_ids,
        )

    if mode is PairExecutionMode.TAKER_TAKER:
        return quote_roles(first_maker=False, second_maker=False)
    if mode is PairExecutionMode.MAKER_MAKER:
        return quote_roles(first_maker=True, second_maker=True)
    alternatives = tuple(
        quote
        for quote in (
            quote_roles(first_maker=True, second_maker=False),
            quote_roles(first_maker=False, second_maker=True),
        )
        if quote is not None
    )
    if not alternatives:
        return None
    return min(
        alternatives,
        key=lambda quote: quote.total_cost,
    )


def select_healthy_pair_books(
    *,
    first_token_id: str,
    second_token_id: str,
    books: Mapping[str, OrderBookSnapshot],
    policy: PairPricingPolicy,
    first_contract: TwapMarketContract | None = None,
    second_contract: TwapMarketContract | None = None,
) -> tuple[OrderBookSnapshot, OrderBookSnapshot] | None:
    """Fail closed unless both books are complete and satisfy the active policy."""

    first_book = books.get(first_token_id)
    second_book = books.get(second_token_id)
    if first_book is None or second_book is None:
        return None
    if first_book.token_id != first_token_id or second_book.token_id != second_token_id:
        return None
    for book, contract in (
        (first_book, first_contract),
        (second_book, second_contract),
    ):
        if contract is None:
            continue
        if (
            book.token_id not in {contract.up_token_id, contract.down_token_id}
            or book.tick_size != contract.tick_size
            or book.minimum_order_size != contract.minimum_order_size
            or any(
                level.price % book.tick_size != ZERO
                for level in (*book.bids, *book.asks)
            )
        ):
            return None
    for book in (first_book, second_book):
        if (
            book.best_bid is None
            or book.best_ask is None
            or book.spread is None
            or book.spread < ZERO
        ):
            return None
        if policy.structural_only:
            continue
        if (
            book.spread > policy.maximum_spread_each_leg
            or not policy.minimum_market_price
            <= book.best_ask
            <= policy.maximum_market_price
        ):
            return None
    return first_book, second_book


def _quantity_units_ceiling(quantity: Decimal) -> int:
    return int((quantity / SIZE_QUANTUM).to_integral_value(rounding=ROUND_CEILING))


def _quantity_units_floor(quantity: Decimal) -> int:
    return int((quantity / SIZE_QUANTUM).to_integral_value(rounding=ROUND_DOWN))


def _maximum_affordable_quantity(
    *,
    minimum_quantity: Decimal,
    maximum_depth_quantity: Decimal,
    first_book: OrderBookSnapshot,
    second_book: OrderBookSnapshot,
    first_contract: TwapMarketContract,
    second_contract: TwapMarketContract,
    pair_risk_usdc: Decimal,
) -> Decimal | None:
    minimum_units = _quantity_units_ceiling(minimum_quantity)
    maximum_units = _quantity_units_floor(maximum_depth_quantity)
    affordable_units: int | None = None
    while minimum_units <= maximum_units:
        middle_units = (minimum_units + maximum_units) // 2
        quantity = Decimal(middle_units) * SIZE_QUANTUM
        quote = quote_pair_buy(
            quantity=quantity,
            first_book=first_book,
            second_book=second_book,
            first_contract=first_contract,
            second_contract=second_contract,
        )
        if quote is not None and quote.total_cost <= pair_risk_usdc:
            affordable_units = middle_units
            minimum_units = middle_units + 1
        else:
            maximum_units = middle_units - 1
    if affordable_units is None:
        return None
    return Decimal(affordable_units) * SIZE_QUANTUM


def joint_quantity_breakpoints(
    *,
    first_book: OrderBookSnapshot,
    second_book: OrderBookSnapshot,
    first_contract: TwapMarketContract,
    second_contract: TwapMarketContract,
    policy: PairPricingPolicy,
) -> tuple[Decimal, ...]:
    """Enumerate joint depth breaks and the exact fee-aware risk boundary."""

    minimum_quantity = max(
        first_book.minimum_order_size,
        second_book.minimum_order_size,
        first_contract.minimum_order_size,
        second_contract.minimum_order_size,
    )
    minimum_quantity = Decimal(_quantity_units_ceiling(minimum_quantity)) * SIZE_QUANTUM
    first_depth = sum((level.size for level in first_book.asks), ZERO)
    second_depth = sum((level.size for level in second_book.asks), ZERO)
    maximum_depth = min(first_depth, second_depth)
    if maximum_depth < minimum_quantity:
        return ()
    maximum_affordable = (
        Decimal(_quantity_units_floor(maximum_depth)) * SIZE_QUANTUM
        if policy.pair_risk_usdc is None
        else _maximum_affordable_quantity(
            minimum_quantity=minimum_quantity,
            maximum_depth_quantity=maximum_depth,
            first_book=first_book,
            second_book=second_book,
            first_contract=first_contract,
            second_contract=second_contract,
            pair_risk_usdc=policy.pair_risk_usdc,
        )
    )
    if maximum_affordable is None:
        return ()
    candidates = {minimum_quantity, maximum_affordable}
    for book in (first_book, second_book):
        cumulative = ZERO
        for level in book.asks:
            cumulative += level.size
            quantity = Decimal(_quantity_units_floor(cumulative)) * SIZE_QUANTUM
            if minimum_quantity <= quantity <= maximum_affordable:
                candidates.add(quantity)
    return tuple(sorted(candidates))


__all__ = [
    "BookBuyQuote",
    "PairBuyQuote",
    "PairExecutionMode",
    "PairPricingPolicy",
    "joint_quantity_breakpoints",
    "quote_book_buy",
    "quote_pair_buy",
    "select_healthy_pair_books",
]
