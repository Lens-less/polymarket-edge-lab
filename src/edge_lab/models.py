"""Typed models used by the public-data edge research lab."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional


ZERO = Decimal("0")
ONE = Decimal("1")


def decimal_value(value: Any, default: Decimal = ZERO) -> Decimal:
    """Convert API numbers to Decimal without binary floating-point drift."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "BookLevel":
        return cls(
            price=decimal_value(raw.get("price")),
            size=decimal_value(raw.get("size")),
        )


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    timestamp_ms: Optional[int] = None
    condition_id: Optional[str] = None
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Decimal = Decimal("5")
    neg_risk: bool = False
    last_trade_price: Optional[Decimal] = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "OrderBook":
        bids = sorted(
            (BookLevel.from_api(level) for level in raw.get("bids", [])),
            key=lambda level: level.price,
            reverse=True,
        )
        asks = sorted(
            (BookLevel.from_api(level) for level in raw.get("asks", [])),
            key=lambda level: level.price,
        )
        timestamp = raw.get("timestamp")
        last_trade = raw.get("last_trade_price")
        return cls(
            token_id=str(raw.get("asset_id") or raw.get("token_id") or ""),
            bids=tuple(bids),
            asks=tuple(asks),
            timestamp_ms=int(timestamp) if timestamp not in (None, "") else None,
            condition_id=raw.get("market") or raw.get("condition_id"),
            tick_size=decimal_value(raw.get("tick_size"), Decimal("0.01")),
            min_order_size=decimal_value(raw.get("min_order_size"), Decimal("5")),
            neg_risk=bool(raw.get("neg_risk", False)),
            last_trade_price=(
                decimal_value(last_trade) if last_trade not in (None, "") else None
            ),
        )

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def spread(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class FeeSchedule:
    """Current price-dependent taker fee parameters from Gamma."""

    rate: Decimal = ZERO
    exponent: Decimal = ONE
    taker_only: bool = True
    rebate_rate: Decimal = ZERO

    @classmethod
    def from_api(cls, raw: Optional[Mapping[str, Any]]) -> "FeeSchedule":
        raw = raw or {}
        return cls(
            rate=decimal_value(raw.get("rate")),
            exponent=decimal_value(raw.get("exponent"), ONE),
            taker_only=bool(raw.get("takerOnly", True)),
            rebate_rate=decimal_value(raw.get("rebateRate")),
        )


@dataclass(frozen=True)
class Token:
    token_id: str
    outcome: str
    reference_price: Decimal


@dataclass(frozen=True)
class MarketCandidate:
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    daily_reward: Decimal
    reward_min_size: Decimal
    reward_max_spread_cents: Decimal
    reported_competitiveness: Decimal
    minimum_reward_order_age_seconds: Optional[int] = None
    reward_order_age_source: Optional[str] = None
    gamma_reported_daily_reward: Optional[Decimal] = None
    reward_rate_sources_consistent: Optional[bool] = None
    end_date: Optional[str] = None
    seconds_delay: int = 0
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Decimal = Decimal("5")
    neg_risk: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    volume_24h: Decimal = ZERO
    liquidity: Decimal = ZERO
    displayed_spread: Optional[Decimal] = None
    fee_schedule: FeeSchedule = field(default_factory=FeeSchedule)


@dataclass(frozen=True)
class PairedQuote:
    yes_bid: Decimal
    no_bid: Decimal
    size: Decimal
    reference_yes_mid: Decimal
    distance_cents: Decimal

    @property
    def complete_set_cost_per_share(self) -> Decimal:
        return self.yes_bid + self.no_bid

    @property
    def paired_margin_per_share(self) -> Decimal:
        return ONE - self.complete_set_cost_per_share

    @property
    def capital(self) -> Decimal:
        return self.complete_set_cost_per_share * self.size

    @property
    def paired_margin(self) -> Decimal:
        return self.paired_margin_per_share * self.size


@dataclass(frozen=True)
class PairedAskQuote:
    """A complete set split in advance, then offered on both outcome books."""

    yes_ask: Decimal
    no_ask: Decimal
    size: Decimal
    reference_yes_mid: Decimal
    distance_cents: Decimal

    @property
    def complete_set_proceeds_per_share(self) -> Decimal:
        return self.yes_ask + self.no_ask

    @property
    def paired_margin_per_share(self) -> Decimal:
        return self.complete_set_proceeds_per_share - ONE

    @property
    def capital(self) -> Decimal:
        return self.size

    @property
    def paired_margin(self) -> Decimal:
        return self.paired_margin_per_share * self.size


@dataclass(frozen=True)
class DepthFill:
    requested_size: Decimal
    filled_size: Decimal
    notional: Decimal
    average_price: Optional[Decimal]
    complete: bool


def levels_to_json(levels: Iterable[BookLevel]) -> list[dict[str, str]]:
    return [{"price": str(level.price), "size": str(level.size)} for level in levels]
