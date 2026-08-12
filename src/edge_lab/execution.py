"""Decimal, inventory-safe execution replay for the Edge Discovery Lab.

This module is deliberately independent from the legacy backtest engine.  It
models only mechanisms that can be tied to captured source events:

* a multi-token collateral/inventory ledger;
* binary complete-set split, merge, resolution, dispute, and invalid payout;
* mutable shared L2 depth for taker FAK/FOK replays; and
* trade-driven maker fills with explicit queue-evidence scenarios.

Book touches never create maker fills.  A maker fill needs a qualifying trade
event, an active order, remaining capacity, and exhausted modeled queue ahead.
The queue scenarios are deterministic evidence bounds, not fill probabilities.
Nothing in this file signs or submits an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional


ZERO = Decimal("0")
ONE = Decimal("1")
FEE_QUANTUM = Decimal("0.00001")
BASE_UNIT = Decimal("0.000001")


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except Exception as exc:  # pragma: no cover - Decimal error types vary
            raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value: Any, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= ZERO:
        raise ValueError(f"{name} must be positive")
    return result


def _base_amount(value: Any, *, name: str, positive: bool = True) -> Decimal:
    """Validate a pUSD/CTF quantity against six-decimal base units."""

    result = _positive(value, name=name) if positive else _decimal(value, name=name)
    if result < ZERO or result % BASE_UNIT != ZERO:
        raise ValueError(f"{name} must use non-negative 6-decimal base units")
    return result


def _cash_aligned_fill_quantity(
    maximum_quantity: Decimal, price: Decimal
) -> Decimal:
    """Floor share dust so ``quantity * price`` is a six-decimal cash amount."""

    _numerator, denominator = price.as_integer_ratio()
    quantum = BASE_UNIT * Decimal(denominator)
    return (maximum_quantity // quantum) * quantum


def _probability(value: Any, *, name: str = "price") -> Decimal:
    result = _decimal(value, name=name)
    if not ZERO <= result <= ONE:
        raise ValueError(f"{name} must be between 0 and 1")
    return result


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError("taker-only flag must be an explicit boolean")


class InventoryError(ValueError):
    """Raised when an action would create unbacked token inventory."""


class InsufficientCashError(ValueError):
    """Raised when collateral is insufficient for an atomic action."""


class MarketStatus(str, Enum):
    OPEN = "open"
    DISPUTED = "disputed"
    RESOLVED = "resolved"
    INVALID = "invalid"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TimeInForce(str, Enum):
    FAK = "FAK"
    FOK = "FOK"


class ExecutionStatus(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    KILLED = "killed"


class LiquidityRole(str, Enum):
    TAKER = "taker"
    MAKER = "maker"


@dataclass(frozen=True)
class TraceRef:
    """Immutable provenance required for every economic state transition."""

    source_event_id: str
    decision_id: str
    timestamp_ms: int

    def __post_init__(self) -> None:
        if not self.source_event_id.strip():
            raise ValueError("source_event_id must be non-empty")
        if not self.decision_id.strip():
            raise ValueError("decision_id must be non-empty")
        if self.timestamp_ms < 0:
            raise ValueError("timestamp_ms must be non-negative")


@dataclass(frozen=True)
class LedgerTrace:
    sequence: int
    action: str
    source_event_id: str
    decision_id: str
    timestamp_ms: int
    market_id: str
    token_id: Optional[str]
    quantity: Decimal
    price: Optional[Decimal]
    cash_delta: Decimal
    fee: Decimal
    operation_cost: Decimal
    details: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountingSnapshot:
    """Auditable point-in-time accounting without cost-basis assumptions.

    ``realized_cashflow`` is net collateral movement since initialization,
    equal to both ``cash - initial_cash`` and the sum of trace cash deltas.  It
    is deliberately not labeled realized P&L while outcome inventory remains.
    """

    cash: Decimal
    positions: Mapping[str, Decimal]
    fees_paid: Decimal
    operation_costs_paid: Decimal
    marked_equity: Decimal
    realized_cashflow: Decimal
    trace_entries: int

    @property
    def fees(self) -> Decimal:
        return self.fees_paid


@dataclass
class _BinaryMarket:
    market_id: str
    yes_token_id: str
    no_token_id: str
    status: MarketStatus = MarketStatus.OPEN
    payouts: dict[str, Decimal] = field(default_factory=dict)

    @property
    def token_ids(self) -> tuple[str, str]:
        return (self.yes_token_id, self.no_token_id)


@dataclass(frozen=True)
class OperationAssumption:
    """Explicit cost and confirmation evidence for simulated chain actions."""

    cost: Decimal
    latency_ms: int
    confirmed: bool
    source_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cost",
            _base_amount(self.cost, name="operation cost", positive=False),
        )
        if self.latency_ms < 0:
            raise ValueError("operation latency cannot be negative")
        if not isinstance(self.confirmed, bool):
            raise TypeError("operation confirmed must be bool")
        if not self.source_ref.strip():
            raise ValueError("operation source_ref must be non-empty")

    def require_confirmed(self) -> "OperationAssumption":
        if not self.confirmed:
            raise ValueError("chain operation is not confirmed")
        return self


@dataclass(frozen=True)
class ExecutionFeeSchedule:
    """Per-market V2 fee data, including compact ``fd={r,e,to}`` fields."""

    rate: Decimal
    exponent: Decimal
    taker_only: bool
    exemption_reason: Optional[str] = None
    exemption_source_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "rate", _decimal(self.rate, name="fee rate")
        )
        object.__setattr__(
            self, "exponent", _decimal(self.exponent, name="fee exponent")
        )
        object.__setattr__(
            self,
            "taker_only",
            _bool(self.taker_only),
        )
        if self.rate < ZERO:
            raise ValueError("fee rate cannot be negative")
        if self.exponent < ZERO:
            raise ValueError("fee exponent cannot be negative")
        if self.rate == ZERO:
            if not self.exemption_reason or not self.exemption_source_ref:
                raise ValueError(
                    "zero-fee schedules must use fee_exempt(reason, source_ref)"
                )
        elif self.exemption_reason is not None or self.exemption_source_ref is not None:
            raise ValueError("nonzero fee schedules cannot carry an exemption")

    @classmethod
    def fee_exempt(
        cls,
        *,
        reason: str,
        source_ref: str,
    ) -> "ExecutionFeeSchedule":
        """Create an auditable, explicitly sourced zero-fee schedule."""

        if not reason.strip():
            raise ValueError("fee exemption reason must be non-empty")
        if not source_ref.strip():
            raise ValueError("fee exemption source_ref must be non-empty")
        return cls(
            rate=ZERO,
            exponent=ONE,
            taker_only=True,
            exemption_reason=reason,
            exemption_source_ref=source_ref,
        )

    @classmethod
    def from_market(cls, market: Optional[Mapping[str, Any]]) -> "ExecutionFeeSchedule":
        """Parse a complete current compact or verbose market fee payload.

        Missing fee data is unknown, not zero.  Callers with an independently
        verified fee-free market must use :meth:`fee_exempt` and retain the
        reason/source reference in the result.
        """

        if not isinstance(market, Mapping):
            raise ValueError(
                "market fee payload must be a mapping with fd or feeSchedule"
            )
        if isinstance(market.get("fd"), Mapping):
            raw = market["fd"]
            keys = ("r", "e", "to")
            source_name = "fd"
        elif isinstance(market.get("feeSchedule"), Mapping):
            raw = market["feeSchedule"]
            keys = ("rate", "exponent", "takerOnly")
            source_name = "feeSchedule"
        else:
            raise ValueError("market payload is missing fd or feeSchedule")
        missing = [
            key for key in keys if key not in raw or raw[key] is None
        ]
        if missing:
            raise ValueError(
                "missing required fee fields "
                f"in {source_name}: {', '.join(missing)}"
            )
        rate = _decimal(raw[keys[0]], name="fee rate")
        exponent = _decimal(raw[keys[1]], name="fee exponent")
        taker_only = _bool(raw[keys[2]])
        if rate == ZERO:
            market_ref = str(
                market.get("condition_id")
                or market.get("conditionId")
                or market.get("c")
                or market.get("id")
                or "unknown-market"
            )
            return cls(
                rate=rate,
                exponent=exponent,
                taker_only=taker_only,
                exemption_reason=(
                    f"{source_name} explicitly reports a zero fee rate"
                ),
                exemption_source_ref=f"market-payload:{market_ref}:{source_name}",
            )
        return cls(rate=rate, exponent=exponent, taker_only=taker_only)

    def fee(
        self,
        shares: Decimal,
        price: Decimal,
        *,
        maker: bool,
    ) -> Decimal:
        """Calculate a per-fill fee with the documented sub-quantum rule.

        The raw amount is ``shares * rate * (p * (1-p)) ** exponent``.
        A raw amount strictly below 0.00001 is zero; otherwise it is rounded to
        five decimal places.  Rounding is intentionally done per price level.
        """

        raw = self.raw_fee(shares, price, maker=maker)
        return self.quantize_fee(raw)

    def raw_fee(
        self,
        shares: Decimal,
        price: Decimal,
        *,
        maker: bool,
    ) -> Decimal:
        """Return the unrounded fee for one modeled match."""

        quantity = _decimal(shares, name="shares")
        if quantity < ZERO:
            raise ValueError("shares cannot be negative")
        probability = _probability(price)
        if quantity == ZERO or self.rate == ZERO or (maker and self.taker_only):
            return ZERO
        probability_term = probability * (ONE - probability)
        if probability_term <= ZERO:
            return ZERO
        return quantity * self.rate * (probability_term**self.exponent)

    @staticmethod
    def quantize_fee(raw: Decimal) -> Decimal:
        """Apply the documented minimum and a conservative half-up boundary."""

        raw = _decimal(raw, name="raw fee")
        if raw < ZERO:
            raise ValueError("raw fee cannot be negative")
        if raw < FEE_QUANTUM:
            return ZERO
        return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


class Ledger:
    """Collateral and token balances with no implicit short inventory."""

    def __init__(self, initial_cash: Decimal) -> None:
        cash = _base_amount(
            initial_cash, name="initial_cash", positive=False
        )
        self.initial_cash = cash
        self.cash = cash
        self.fees_paid = ZERO
        self.operation_costs_paid = ZERO
        self._balances: dict[str, Decimal] = {}
        self._markets: dict[str, _BinaryMarket] = {}
        self._token_market: dict[str, str] = {}
        self._trace: list[LedgerTrace] = []

    @property
    def trace(self) -> tuple[LedgerTrace, ...]:
        return tuple(self._trace)

    def register_binary_market(
        self,
        market_id: str,
        *,
        yes_token_id: str,
        no_token_id: str,
    ) -> None:
        if not market_id or not yes_token_id or not no_token_id:
            raise ValueError("market and token ids must be non-empty")
        if yes_token_id == no_token_id:
            raise ValueError("binary outcome token ids must differ")
        if market_id in self._markets:
            raise ValueError(f"market already registered: {market_id}")
        for token_id in (yes_token_id, no_token_id):
            if token_id in self._token_market:
                raise ValueError(f"token already registered: {token_id}")
        self._markets[market_id] = _BinaryMarket(
            market_id=market_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )
        for token_id in (yes_token_id, no_token_id):
            self._token_market[token_id] = market_id
            self._balances[token_id] = ZERO

    def position(self, token_id: str) -> Decimal:
        self._require_token(token_id)
        return self._balances[token_id]

    def market_status(self, market_id: str) -> MarketStatus:
        return self._require_market(market_id).status

    def market_id_for_token(self, token_id: str) -> str:
        """Return the registered market, validating the token in the process."""

        self._require_token(token_id)
        return self._token_market[token_id]

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Return cash plus marked token inventory, never cost-basis P&L."""

        inventory_value = ZERO
        for token_id, quantity in self._balances.items():
            if quantity == ZERO:
                continue
            if token_id not in marks:
                raise ValueError(f"missing mark for token {token_id}")
            inventory_value += quantity * _probability(
                marks[token_id], name=f"mark[{token_id}]"
            )
        return self.cash + inventory_value

    def accounting_snapshot(
        self,
        marks: Mapping[str, Decimal],
    ) -> AccountingSnapshot:
        """Return state plus independently reconcilable trace aggregates."""

        traced_cashflow = sum(
            (entry.cash_delta for entry in self._trace), ZERO
        )
        state_cashflow = self.cash - self.initial_cash
        if traced_cashflow != state_cashflow:
            raise RuntimeError(
                "ledger cash does not reconcile to traced cashflow: "
                f"state={state_cashflow}, trace={traced_cashflow}"
            )
        traced_fees = sum((entry.fee for entry in self._trace), ZERO)
        if traced_fees != self.fees_paid:
            raise RuntimeError(
                "ledger fees do not reconcile to trace: "
                f"state={self.fees_paid}, trace={traced_fees}"
            )
        traced_operation_costs = sum(
            (entry.operation_cost for entry in self._trace), ZERO
        )
        if traced_operation_costs != self.operation_costs_paid:
            raise RuntimeError(
                "ledger operation costs do not reconcile to trace: "
                f"state={self.operation_costs_paid}, "
                f"trace={traced_operation_costs}"
            )
        if any(quantity < ZERO for quantity in self._balances.values()):
            raise RuntimeError("ledger contains negative token inventory")
        return AccountingSnapshot(
            cash=self.cash,
            positions=MappingProxyType(dict(self._balances)),
            fees_paid=self.fees_paid,
            operation_costs_paid=self.operation_costs_paid,
            marked_equity=self.equity(marks),
            realized_cashflow=traced_cashflow,
            trace_entries=len(self._trace),
        )

    def buy(
        self,
        token_id: str,
        quantity: Decimal,
        price: Decimal,
        *,
        fee: Decimal,
        trace: TraceRef,
    ) -> None:
        market = self._open_market_for_token(token_id)
        size = _base_amount(quantity, name="quantity")
        fill_price = _probability(price)
        fill_fee = _decimal(fee, name="fee")
        if fill_fee < ZERO:
            raise ValueError("fee cannot be negative")
        debit = size * fill_price + fill_fee
        _base_amount(debit, name="cash debit", positive=False)
        if debit > self.cash:
            raise InsufficientCashError(
                f"insufficient cash: need {debit}, have {self.cash}"
            )
        self.cash -= debit
        self.fees_paid += fill_fee
        self._balances[token_id] += size
        self._record(
            "buy",
            market.market_id,
            token_id,
            size,
            fill_price,
            -debit,
            fill_fee,
            trace,
        )

    def sell(
        self,
        token_id: str,
        quantity: Decimal,
        price: Decimal,
        *,
        fee: Decimal,
        trace: TraceRef,
    ) -> None:
        market = self._open_market_for_token(token_id)
        size = _base_amount(quantity, name="quantity")
        fill_price = _probability(price)
        fill_fee = _decimal(fee, name="fee")
        if fill_fee < ZERO:
            raise ValueError("fee cannot be negative")
        if size > self._balances[token_id]:
            raise InventoryError(
                f"naked sell blocked for {token_id}: "
                f"requested {size}, available {self._balances[token_id]}"
            )
        credit = size * fill_price - fill_fee
        _base_amount(credit, name="cash credit", positive=False)
        self.cash += credit
        self.fees_paid += fill_fee
        self._balances[token_id] -= size
        self._record(
            "sell",
            market.market_id,
            token_id,
            size,
            fill_price,
            credit,
            fill_fee,
            trace,
        )

    def split(
        self,
        market_id: str,
        quantity: Decimal,
        *,
        operation: OperationAssumption,
        trace: TraceRef,
    ) -> None:
        market = self._require_market(market_id)
        self._require_open(market)
        size = _base_amount(quantity, name="quantity")
        confirmed = operation.require_confirmed()
        debit = size + confirmed.cost
        if debit > self.cash:
            raise InsufficientCashError(
                f"insufficient cash to split: need {debit}, have {self.cash}"
            )
        self.cash -= debit
        self.operation_costs_paid += confirmed.cost
        for token_id in market.token_ids:
            self._balances[token_id] += size
        self._record(
            "split",
            market_id,
            None,
            size,
            ONE,
            -debit,
            ZERO,
            trace,
            operation_cost=confirmed.cost,
            details={
                "yes_token_id": market.yes_token_id,
                "no_token_id": market.no_token_id,
                "operation_source_ref": confirmed.source_ref,
                "operation_latency_ms": str(confirmed.latency_ms),
            },
        )

    def merge(
        self,
        market_id: str,
        quantity: Decimal,
        *,
        operation: OperationAssumption,
        trace: TraceRef,
    ) -> None:
        """Merge an explicit quantity; less than the pair balance is partial."""

        market = self._require_market(market_id)
        self._require_open(market)
        size = _base_amount(quantity, name="quantity")
        available = min(self._balances[token] for token in market.token_ids)
        if size > available:
            raise InventoryError(
                f"merge needs both outcomes: requested {size}, available {available}"
            )
        confirmed = operation.require_confirmed()
        if confirmed.cost > self.cash + size:
            raise InsufficientCashError(
                "insufficient cash to pay merge operation cost"
            )
        for token_id in market.token_ids:
            self._balances[token_id] -= size
        net_credit = size - confirmed.cost
        self.cash += net_credit
        self.operation_costs_paid += confirmed.cost
        self._record(
            "merge",
            market_id,
            None,
            size,
            ONE,
            net_credit,
            ZERO,
            trace,
            operation_cost=confirmed.cost,
            details={
                "partial": str(
                    any(self._balances[t] > ZERO for t in market.token_ids)
                ),
                "operation_source_ref": confirmed.source_ref,
                "operation_latency_ms": str(confirmed.latency_ms),
            },
        )

    def dispute(self, market_id: str, *, trace: TraceRef) -> None:
        market = self._require_market(market_id)
        if market.status is not MarketStatus.OPEN:
            raise ValueError(f"cannot dispute market in state {market.status.value}")
        market.status = MarketStatus.DISPUTED
        self._record(
            "dispute",
            market_id,
            None,
            ZERO,
            None,
            ZERO,
            ZERO,
            trace,
        )

    def resolve(
        self,
        market_id: str,
        *,
        trace: TraceRef,
        winning_token_id: Optional[str] = None,
        invalid: bool = False,
    ) -> None:
        market = self._require_market(market_id)
        if market.status not in {MarketStatus.OPEN, MarketStatus.DISPUTED}:
            raise ValueError(f"market already final: {market.status.value}")
        if invalid and winning_token_id is not None:
            raise ValueError("invalid resolution cannot also specify a winner")
        if invalid:
            market.status = MarketStatus.INVALID
            half = ONE / Decimal("2")
            market.payouts = {token_id: half for token_id in market.token_ids}
        else:
            if winning_token_id not in market.token_ids:
                raise ValueError("winning_token_id must be a market outcome token")
            market.status = MarketStatus.RESOLVED
            market.payouts = {
                token_id: ONE if token_id == winning_token_id else ZERO
                for token_id in market.token_ids
            }
        self._record(
            "resolve_invalid" if invalid else "resolve",
            market_id,
            winning_token_id,
            ZERO,
            None,
            ZERO,
            ZERO,
            trace,
            details={
                "status": market.status.value,
                "payouts": ",".join(
                    f"{token}:{payout}" for token, payout in market.payouts.items()
                ),
            },
        )

    def redeem(
        self,
        market_id: str,
        *,
        operation: OperationAssumption,
        trace: TraceRef,
    ) -> Decimal:
        market = self._require_market(market_id)
        if market.status is MarketStatus.DISPUTED:
            raise ValueError("cannot redeem a disputed market")
        if market.status not in {MarketStatus.RESOLVED, MarketStatus.INVALID}:
            raise ValueError("market has no final payout")
        payout = sum(
            (
                self._balances[token_id] * market.payouts[token_id]
                for token_id in market.token_ids
            ),
            ZERO,
        )
        redeemed_shares = sum(
            (self._balances[token_id] for token_id in market.token_ids), ZERO
        )
        confirmed = operation.require_confirmed()
        if confirmed.cost > self.cash + payout:
            raise InsufficientCashError(
                "insufficient cash to pay redeem operation cost"
            )
        for token_id in market.token_ids:
            self._balances[token_id] = ZERO
        net_payout = payout - confirmed.cost
        self.cash += net_payout
        self.operation_costs_paid += confirmed.cost
        self._record(
            "redeem",
            market_id,
            None,
            redeemed_shares,
            None,
            net_payout,
            ZERO,
            trace,
            operation_cost=confirmed.cost,
            details={
                "market_status": market.status.value,
                "gross_payout": str(payout),
                "operation_source_ref": confirmed.source_ref,
                "operation_latency_ms": str(confirmed.latency_ms),
            },
        )
        return net_payout

    def _require_token(self, token_id: str) -> str:
        if token_id not in self._token_market:
            raise ValueError(f"unknown token: {token_id}")
        return token_id

    def _require_market(self, market_id: str) -> _BinaryMarket:
        if market_id not in self._markets:
            raise ValueError(f"unknown market: {market_id}")
        return self._markets[market_id]

    def _open_market_for_token(self, token_id: str) -> _BinaryMarket:
        self._require_token(token_id)
        market = self._markets[self._token_market[token_id]]
        self._require_open(market)
        return market

    @staticmethod
    def _require_open(market: _BinaryMarket) -> None:
        if market.status is not MarketStatus.OPEN:
            raise ValueError(f"market is not open: {market.status.value}")

    def _record(
        self,
        action: str,
        market_id: str,
        token_id: Optional[str],
        quantity: Decimal,
        price: Optional[Decimal],
        cash_delta: Decimal,
        fee: Decimal,
        trace: TraceRef,
        *,
        operation_cost: Decimal = ZERO,
        details: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._trace.append(
            LedgerTrace(
                sequence=len(self._trace) + 1,
                action=action,
                source_event_id=trace.source_event_id,
                decision_id=trace.decision_id,
                timestamp_ms=trace.timestamp_ms,
                market_id=market_id,
                token_id=token_id,
                quantity=quantity,
                price=price,
                cash_delta=cash_delta,
                fee=fee,
                operation_cost=operation_cost,
                details=dict(details or {}),
            )
        )


@dataclass
class DepthLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        self.price = _probability(self.price)
        self.size = _base_amount(self.size, name="level size")


@dataclass
class DepthBook:
    """A mutable L2 book whose depth is shared across replayed taker orders."""

    token_id: str
    bids: list[DepthLevel]
    asks: list[DepthLevel]
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Optional[Decimal] = None
    timestamp_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.token_id:
            raise ValueError("token_id must be non-empty")
        self.tick_size = _positive(self.tick_size, name="tick_size")
        if self.min_order_size is not None:
            self.min_order_size = _base_amount(
                self.min_order_size, name="min_order_size"
            )
        if self.timestamp_ms is not None and self.timestamp_ms < 0:
            raise ValueError("book timestamp must be non-negative")
        self.bids.sort(key=lambda level: level.price, reverse=True)
        self.asks.sort(key=lambda level: level.price)

    @classmethod
    def from_tuples(
        cls,
        token_id: str,
        *,
        bids: Iterable[tuple[Any, Any]],
        asks: Iterable[tuple[Any, Any]],
        tick_size: Decimal = Decimal("0.01"),
        min_order_size: Optional[Decimal] = None,
        timestamp_ms: Optional[int] = None,
    ) -> "DepthBook":
        return cls(
            token_id=token_id,
            bids=[
                DepthLevel(
                    _decimal(price, name="bid price"),
                    _decimal(size, name="bid size"),
                )
                for price, size in bids
            ],
            asks=[
                DepthLevel(
                    _decimal(price, name="ask price"),
                    _decimal(size, name="ask size"),
                )
                for price, size in asks
            ],
            tick_size=tick_size,
            min_order_size=min_order_size,
            timestamp_ms=timestamp_ms,
        )


@dataclass(frozen=True)
class Fill:
    token_id: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    liquidity_role: LiquidityRole
    source_event_id: str
    decision_id: str
    timestamp_ms: int
    order_id: Optional[str] = None

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    requested_quantity: Decimal
    fills: tuple[Fill, ...]
    source_event_id: str
    decision_id: str
    reason: Optional[str] = None

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), ZERO)

    @property
    def notional(self) -> Decimal:
        return sum((fill.notional for fill in self.fills), ZERO)

    @property
    def fee(self) -> Decimal:
        return sum((fill.fee for fill in self.fills), ZERO)

    @property
    def average_price(self) -> Optional[Decimal]:
        if self.filled_quantity == ZERO:
            return None
        return self.notional / self.filled_quantity


def execute_taker(
    ledger: Ledger,
    book: DepthBook,
    *,
    side: OrderSide,
    quantity: Decimal,
    limit_price: Decimal,
    time_in_force: TimeInForce,
    fee_schedule: ExecutionFeeSchedule,
    trace: TraceRef,
    execution_latency_ms: int,
    max_book_age_ms: int,
) -> ExecutionResult:
    """Walk eligible levels, preserving FOK atomicity and shared depth.

    The execution price is each visible level, never the submitted limit.  FAK
    consumes available eligible levels and kills the remainder.  FOK first
    proves complete executable depth and ledger capacity, so failure mutates
    neither the book nor the ledger.
    """

    if execution_latency_ms < 0:
        raise ValueError("execution_latency_ms cannot be negative")
    if max_book_age_ms < 0:
        raise ValueError("max_book_age_ms cannot be negative")
    requested = _base_amount(quantity, name="quantity")
    limit = _probability(limit_price, name="limit_price")
    try:
        order_side = side if isinstance(side, OrderSide) else OrderSide(side)
        tif = (
            time_in_force
            if isinstance(time_in_force, TimeInForce)
            else TimeInForce(time_in_force)
        )
    except ValueError as exc:
        raise ValueError("unsupported side or time_in_force") from exc

    execution_timestamp_ms = trace.timestamp_ms + execution_latency_ms

    def killed(reason: str) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionStatus.KILLED,
            requested_quantity=requested,
            fills=(),
            source_event_id=trace.source_event_id,
            decision_id=trace.decision_id,
            reason=reason,
        )

    if book.timestamp_ms is None:
        return killed("missing_book_timestamp")
    if book.min_order_size is None:
        return killed("missing_min_order_size")
    if requested < book.min_order_size:
        return killed("below_min_order_size")
    if not _aligned_to_tick(limit, book.tick_size):
        return killed("limit_price_not_aligned_to_tick")
    if book.timestamp_ms > execution_timestamp_ms:
        return killed("book_timestamp_after_execution")
    if execution_timestamp_ms - book.timestamp_ms > max_book_age_ms:
        return killed("book_too_old_at_execution")

    # No order path is allowed to request more sell inventory than exists.
    if order_side is OrderSide.SELL and requested > ledger.position(book.token_id):
        raise InventoryError(
            f"naked sell blocked for {book.token_id}: requested {requested}, "
            f"available {ledger.position(book.token_id)}"
        )

    levels = book.asks if order_side is OrderSide.BUY else book.bids
    if any(not _aligned_to_tick(level.price, book.tick_size) for level in levels):
        return killed("book_level_not_aligned_to_tick")

    def eligible(level: DepthLevel) -> bool:
        if order_side is OrderSide.BUY:
            return level.price <= limit
        return level.price >= limit

    planned: list[tuple[DepthLevel, Decimal, Decimal]] = []
    remaining = requested
    for level in levels:
        if remaining == ZERO or not eligible(level):
            break
        size = _cash_aligned_fill_quantity(
            min(remaining, level.size), level.price
        )
        if size == ZERO:
            continue
        fee = fee_schedule.fee(size, level.price, maker=False)
        planned.append((level, size, fee))
        remaining -= size

    # Public documentation does not settle whether rounding/minimums apply per
    # maker match or after aggregating all matches in one taker action.  For
    # pessimistic evidence, charge the larger result and retain per-level fills.
    if planned:
        aggregate_fee = fee_schedule.quantize_fee(
            sum(
                (
                    fee_schedule.raw_fee(size, level.price, maker=False)
                    for level, size, _ in planned
                ),
                ZERO,
            )
        )
        per_level_fee = sum((fee for _, _, fee in planned), ZERO)
        if aggregate_fee > per_level_fee:
            level, size, fee = planned[-1]
            planned[-1] = (
                level,
                size,
                fee + aggregate_fee - per_level_fee,
            )

    if tif is TimeInForce.FOK and remaining > ZERO:
        return killed("insufficient_eligible_depth")
    if not planned:
        return killed("no_eligible_depth")

    # Preflight the whole executable slice before mutating shared depth.
    total_notional = sum((size * level.price for level, size, _ in planned), ZERO)
    total_fee = sum((fee for _, _, fee in planned), ZERO)
    if order_side is OrderSide.BUY and total_notional + total_fee > ledger.cash:
        raise InsufficientCashError(
            f"insufficient cash: need {total_notional + total_fee}, "
            f"have {ledger.cash}"
        )

    fills: list[Fill] = []
    for level, size, fee in planned:
        fill = Fill(
            token_id=book.token_id,
            side=order_side,
            quantity=size,
            price=level.price,
            fee=fee,
            liquidity_role=LiquidityRole.TAKER,
            source_event_id=trace.source_event_id,
            decision_id=trace.decision_id,
            timestamp_ms=execution_timestamp_ms,
        )
        fill_trace = TraceRef(
            source_event_id=fill.source_event_id,
            decision_id=fill.decision_id,
            timestamp_ms=fill.timestamp_ms,
        )
        if order_side is OrderSide.BUY:
            ledger.buy(
                fill.token_id,
                fill.quantity,
                fill.price,
                fee=fill.fee,
                trace=fill_trace,
            )
        else:
            ledger.sell(
                fill.token_id,
                fill.quantity,
                fill.price,
                fee=fill.fee,
                trace=fill_trace,
            )
        level.size -= size
        fills.append(fill)

    # Remove exhausted depth only after successful ledger mutations.
    if order_side is OrderSide.BUY:
        book.asks = [level for level in book.asks if level.size > ZERO]
    else:
        book.bids = [level for level in book.bids if level.size > ZERO]
    filled = sum((fill.quantity for fill in fills), ZERO)
    status = (
        ExecutionStatus.FILLED
        if filled == requested
        else ExecutionStatus.PARTIAL
    )
    return ExecutionResult(
        status=status,
        requested_quantity=requested,
        fills=tuple(fills),
        source_event_id=trace.source_event_id,
        decision_id=trace.decision_id,
        reason=None if status is ExecutionStatus.FILLED else "fak_remainder_killed",
    )


class QueueScenario(str, Enum):
    """Deterministic queue assumptions; none is a fill probability."""

    OPTIMISTIC = "optimistic"
    NEUTRAL = "neutral"
    PESSIMISTIC = "pessimistic"

    @property
    def description(self) -> str:
        if self is QueueScenario.OPTIMISTIC:
            return (
                "Public-flow upper bound: zero queue ahead and 100% of each "
                "qualifying reported trade is attributable."
            )
        if self is QueueScenario.NEUTRAL:
            return (
                "Displayed-queue proxy: supplied visible queue ahead is "
                "consumed one-for-one before our order."
            )
        return (
            "Stressed proxy: two times supplied queue ahead and only 50% of "
            "reported trade volume is attributable. This is not a guaranteed "
            "lower bound; the strict public-data lower bound remains zero."
        )

    @property
    def queue_multiplier(self) -> Decimal:
        if self is QueueScenario.OPTIMISTIC:
            return ZERO
        if self is QueueScenario.NEUTRAL:
            return ONE
        return Decimal("2")

    @property
    def trade_volume_haircut(self) -> Decimal:
        if self is QueueScenario.PESSIMISTIC:
            return Decimal("0.5")
        return ONE


class MakerStatus(str, Enum):
    PENDING_SUBMIT = "pending_submit"
    LIVE = "live"
    PARTIAL = "partial"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    FILLED = "filled"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    STALE = "stale"


_FILL_ELIGIBLE = {
    MakerStatus.LIVE,
    MakerStatus.PARTIAL,
    MakerStatus.CANCEL_PENDING,
}
_RESERVING = {
    MakerStatus.PENDING_SUBMIT,
    MakerStatus.LIVE,
    MakerStatus.PARTIAL,
    MakerStatus.CANCEL_PENDING,
}


@dataclass(frozen=True)
class MakerOrderRequest:
    order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    tick_size: Decimal
    fee_schedule: ExecutionFeeSchedule
    capacity: Optional[Decimal] = None
    submit_latency_ms: int = 0
    cancel_latency_ms: int = 0
    minimum_live_ms: int = 0
    max_book_age_ms: int = 1_000

    def __post_init__(self) -> None:
        if not self.order_id or not self.market_id or not self.token_id:
            raise ValueError("order, market, and token ids must be non-empty")
        object.__setattr__(
            self,
            "side",
            self.side if isinstance(self.side, OrderSide) else OrderSide(self.side),
        )
        object.__setattr__(self, "price", _probability(self.price))
        object.__setattr__(
            self, "quantity", _base_amount(self.quantity, name="quantity")
        )
        object.__setattr__(
            self, "tick_size", _positive(self.tick_size, name="tick_size")
        )
        if self.capacity is not None:
            object.__setattr__(
                self, "capacity", _positive(self.capacity, name="capacity")
            )
            object.__setattr__(
                self, "capacity", _base_amount(self.capacity, name="capacity")
            )
        for field_name in (
            "submit_latency_ms",
            "cancel_latency_ms",
            "minimum_live_ms",
            "max_book_age_ms",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if not _aligned_to_tick(self.price, self.tick_size):
            raise ValueError("order price must align to tick_size")


@dataclass
class MakerOrder:
    request: MakerOrderRequest
    scenario: QueueScenario
    decision_id: str
    submit_source_event_id: str
    submitted_at_ms: int
    live_at_ms: int
    status: MakerStatus
    queue_ahead: Decimal
    fill_capacity: Decimal
    latest_book_source_ms: int
    filled_quantity: Decimal = ZERO
    fees_paid: Decimal = ZERO
    cancel_requested_ms: Optional[int] = None
    cancel_effective_ms: Optional[int] = None
    stale_reason: Optional[str] = None

    @property
    def order_id(self) -> str:
        return self.request.order_id

    @property
    def remaining_quantity(self) -> Decimal:
        return max(ZERO, self.request.quantity - self.filled_quantity)

    @property
    def remaining_capacity(self) -> Decimal:
        return max(ZERO, self.fill_capacity - self.filled_quantity)

    @property
    def reservable_quantity(self) -> Decimal:
        return min(self.remaining_quantity, self.remaining_capacity)


@dataclass(frozen=True)
class BookEvent:
    token_id: str
    timestamp_ms: int
    source_timestamp_ms: int
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    tick_size: Decimal
    source_event_id: str

    def __post_init__(self) -> None:
        if not self.token_id or not self.source_event_id:
            raise ValueError("book event ids must be non-empty")
        if self.timestamp_ms < 0 or self.source_timestamp_ms < 0:
            raise ValueError("book timestamps must be non-negative")
        object.__setattr__(
            self, "tick_size", _positive(self.tick_size, name="tick_size")
        )
        if self.best_bid is not None:
            object.__setattr__(
                self, "best_bid", _probability(self.best_bid, name="best_bid")
            )
        if self.best_ask is not None:
            object.__setattr__(
                self, "best_ask", _probability(self.best_ask, name="best_ask")
            )


@dataclass(frozen=True)
class TradeEvent:
    token_id: str
    timestamp_ms: int
    price: Decimal
    quantity: Decimal
    aggressor_side: OrderSide
    source_event_id: str

    def __post_init__(self) -> None:
        if not self.token_id or not self.source_event_id:
            raise ValueError("trade event ids must be non-empty")
        if self.timestamp_ms < 0:
            raise ValueError("trade timestamp must be non-negative")
        object.__setattr__(self, "price", _probability(self.price))
        object.__setattr__(
            self, "quantity", _positive(self.quantity, name="trade quantity")
        )
        object.__setattr__(
            self,
            "aggressor_side",
            (
                self.aggressor_side
                if isinstance(self.aggressor_side, OrderSide)
                else OrderSide(self.aggressor_side)
            ),
        )


@dataclass(frozen=True)
class ReplayTrace:
    sequence: int
    action: str
    order_id: str
    status: MakerStatus
    source_event_id: str
    decision_id: str
    timestamp_ms: int
    details: Mapping[str, str] = field(default_factory=dict)


def _aligned_to_tick(price: Decimal, tick: Decimal) -> bool:
    return price % tick == ZERO


class MakerReplay:
    """Trade-driven maker replay with latency, queue, and inventory controls."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        scenario: QueueScenario = QueueScenario.NEUTRAL,
    ) -> None:
        self.ledger = ledger
        self.scenario = (
            scenario
            if isinstance(scenario, QueueScenario)
            else QueueScenario(scenario)
        )
        self._orders: dict[str, MakerOrder] = {}
        self._trace: list[ReplayTrace] = []
        self._now_ms = 0
        self._trade_event_fingerprints: dict[
            str, tuple[str, int, Decimal, Decimal, OrderSide]
        ] = {}

    @property
    def trace(self) -> tuple[ReplayTrace, ...]:
        return tuple(self._trace)

    @property
    def orders(self) -> tuple[MakerOrder, ...]:
        return tuple(self._orders.values())

    @property
    def reserved_cash(self) -> Decimal:
        """Collateral still committed to live or in-flight maker buys."""

        return sum(
            (
                self._cash_commitment(order)
                for order in self._orders.values()
                if order.status in _RESERVING
                and order.request.side is OrderSide.BUY
            ),
            ZERO,
        )

    def reserved_inventory(self, token_id: str) -> Decimal:
        """Shares still committed to live or in-flight maker sells."""

        self.ledger.market_id_for_token(token_id)
        return sum(
            (
                order.reservable_quantity
                for order in self._orders.values()
                if order.status in _RESERVING
                and order.request.side is OrderSide.SELL
                and order.request.token_id == token_id
            ),
            ZERO,
        )

    def get_order(self, order_id: str) -> MakerOrder:
        if order_id not in self._orders:
            raise ValueError(f"unknown maker order: {order_id}")
        return self._orders[order_id]

    def submit(
        self,
        request: MakerOrderRequest,
        *,
        visible_queue_ahead: Decimal,
        now_ms: int,
        book_timestamp_ms: int,
        trace: TraceRef,
    ) -> MakerOrder:
        if request.order_id in self._orders:
            raise ValueError(f"duplicate maker order id: {request.order_id}")
        if trace.timestamp_ms != now_ms:
            raise ValueError("submit trace timestamp must equal now_ms")
        registered_market = self.ledger.market_id_for_token(request.token_id)
        if registered_market != request.market_id:
            raise ValueError(
                f"token {request.token_id} belongs to {registered_market}, "
                f"not {request.market_id}"
            )
        if self.ledger.market_status(request.market_id) is not MarketStatus.OPEN:
            raise ValueError("cannot quote a non-open market")
        queue = _decimal(visible_queue_ahead, name="visible_queue_ahead")
        if queue < ZERO:
            raise ValueError("visible_queue_ahead cannot be negative")
        if book_timestamp_ms > now_ms:
            stale_reason = "book_timestamp_in_future"
        elif now_ms - book_timestamp_ms > request.max_book_age_ms:
            stale_reason = "book_too_old_at_submit"
        else:
            stale_reason = None
        capacity = min(request.quantity, request.capacity or request.quantity)
        modeled_queue = queue * self.scenario.queue_multiplier
        status = (
            MakerStatus.STALE
            if stale_reason is not None
            else MakerStatus.PENDING_SUBMIT
        )
        order = MakerOrder(
            request=request,
            scenario=self.scenario,
            decision_id=trace.decision_id,
            submit_source_event_id=trace.source_event_id,
            submitted_at_ms=now_ms,
            live_at_ms=now_ms + request.submit_latency_ms,
            status=status,
            queue_ahead=modeled_queue,
            fill_capacity=capacity,
            latest_book_source_ms=book_timestamp_ms,
            stale_reason=stale_reason,
        )
        if stale_reason is None:
            self._preflight_reservation(order)
        self._orders[order.order_id] = order
        self._now_ms = max(self._now_ms, now_ms)
        self._record(
            "submit_stale" if stale_reason else "submit",
            order,
            trace.source_event_id,
            now_ms,
            details={
                "scenario": self.scenario.value,
                "scenario_boundary": self.scenario.description,
                "modeled_queue_ahead": str(modeled_queue),
                "capacity": str(capacity),
                "stale_reason": stale_reason or "",
            },
        )
        # Zero-latency orders become live immediately.
        self._advance_orders(now_ms, source_event_id=trace.source_event_id)
        return order

    def request_cancel(
        self,
        order_id: str,
        *,
        now_ms: int,
        trace: TraceRef,
    ) -> None:
        if trace.timestamp_ms != now_ms:
            raise ValueError("cancel trace timestamp must equal now_ms")
        self.advance_time(now_ms, source_event_id=trace.source_event_id)
        order = self.get_order(order_id)
        if order.status not in _RESERVING:
            return
        order.cancel_requested_ms = now_ms
        min_live_end = order.live_at_ms + order.request.minimum_live_ms
        order.cancel_effective_ms = max(
            now_ms + order.request.cancel_latency_ms,
            min_live_end,
        )
        order.status = MakerStatus.CANCEL_PENDING
        self._record("cancel_requested", order, trace.source_event_id, now_ms)
        self._advance_orders(now_ms, source_event_id=trace.source_event_id)

    def advance_time(self, now_ms: int, *, source_event_id: str) -> None:
        if not source_event_id:
            raise ValueError("source_event_id must be non-empty")
        if now_ms < self._now_ms:
            raise ValueError("replay time cannot move backwards")
        self._now_ms = now_ms
        self._advance_orders(now_ms, source_event_id=source_event_id)

    def on_book(self, event: BookEvent) -> None:
        self.advance_time(event.timestamp_ms, source_event_id=event.source_event_id)
        for order in self._orders.values():
            if (
                order.request.token_id != event.token_id
                or order.status not in _FILL_ELIGIBLE
            ):
                continue
            if (
                event.source_timestamp_ms > event.timestamp_ms
                or event.timestamp_ms - event.source_timestamp_ms
                > order.request.max_book_age_ms
            ):
                self._mark_stale(
                    order, "stale_book_event", event.source_event_id, event.timestamp_ms
                )
                continue
            if not _aligned_to_tick(order.request.price, event.tick_size):
                self._mark_stale(
                    order,
                    "price_not_aligned_to_new_tick",
                    event.source_event_id,
                    event.timestamp_ms,
                )
                continue
            order.latest_book_source_ms = event.source_timestamp_ms
            # A post-only order observed crossing the opposite top of book is
            # not silently treated as maker liquidity.
            if (
                order.request.side is OrderSide.BUY
                and event.best_ask is not None
                and order.request.price >= event.best_ask
            ) or (
                order.request.side is OrderSide.SELL
                and event.best_bid is not None
                and order.request.price <= event.best_bid
            ):
                self._mark_stale(
                    order,
                    "would_cross_visible_book",
                    event.source_event_id,
                    event.timestamp_ms,
                )
                continue
            self._record("book_observed_no_fill", order, event.source_event_id, event.timestamp_ms)

    def on_trade(self, event: TradeEvent) -> tuple[Fill, ...]:
        fingerprint = (
            event.token_id,
            event.timestamp_ms,
            event.price,
            event.quantity,
            event.aggressor_side,
        )
        previous = self._trade_event_fingerprints.get(event.source_event_id)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError(
                    "conflicting duplicate trade event for source_event_id "
                    f"{event.source_event_id}"
                )
            return ()
        # Mark the public print consumed before applying economic state.  If an
        # unexpected downstream invariant fails, fail closed instead of
        # replaying an already partially applied print after reconnect.
        self._trade_event_fingerprints[event.source_event_id] = fingerprint
        self.advance_time(event.timestamp_ms, source_event_id=event.source_event_id)
        for order in self._orders.values():
            if (
                order.request.token_id == event.token_id
                and order.status in _FILL_ELIGIBLE
                and event.timestamp_ms - order.latest_book_source_ms
                > order.request.max_book_age_ms
            ):
                self._mark_stale(
                    order,
                    "book_context_expired_before_trade",
                    event.source_event_id,
                    event.timestamp_ms,
                )
        self._invalidate_broken_reservations(
            source_event_id=event.source_event_id,
            timestamp_ms=event.timestamp_ms,
        )
        candidates = [
            order
            for order in self._orders.values()
            if order.request.token_id == event.token_id
            and order.status in _FILL_ELIGIBLE
            and event.timestamp_ms >= order.live_at_ms
            and (
                order.cancel_effective_ms is None
                or event.timestamp_ms < order.cancel_effective_ms
            )
            and self._trade_qualifies(order, event)
        ]
        # A single public print is shared across hypothetical orders.  Better
        # priced orders consume it first, then earlier submissions.
        if event.aggressor_side is OrderSide.SELL:
            candidates.sort(
                key=lambda order: (
                    -order.request.price,
                    order.submitted_at_ms,
                    order.order_id,
                )
            )
        else:
            candidates.sort(
                key=lambda order: (
                    order.request.price,
                    order.submitted_at_ms,
                    order.order_id,
                )
            )
        available = event.quantity * self.scenario.trade_volume_haircut
        fills: list[Fill] = []
        for order in candidates:
            if available <= ZERO:
                break
            queue_consumed = min(order.queue_ahead, available)
            order.queue_ahead -= queue_consumed
            available -= queue_consumed
            if available <= ZERO:
                self._record(
                    "trade_consumed_queue",
                    order,
                    event.source_event_id,
                    event.timestamp_ms,
                    details={"queue_consumed": str(queue_consumed)},
                )
                continue
            size = min(
                available,
                order.remaining_quantity,
                order.remaining_capacity,
            )
            if size <= ZERO:
                continue
            fee = order.request.fee_schedule.fee(
                size, order.request.price, maker=True
            )
            fill = Fill(
                token_id=order.request.token_id,
                side=order.request.side,
                quantity=size,
                price=order.request.price,
                fee=fee,
                liquidity_role=LiquidityRole.MAKER,
                source_event_id=event.source_event_id,
                decision_id=order.decision_id,
                timestamp_ms=event.timestamp_ms,
                order_id=order.order_id,
            )
            fill_trace = TraceRef(
                source_event_id=fill.source_event_id,
                decision_id=fill.decision_id,
                timestamp_ms=fill.timestamp_ms,
            )
            if fill.side is OrderSide.BUY:
                self.ledger.buy(
                    fill.token_id,
                    fill.quantity,
                    fill.price,
                    fee=fill.fee,
                    trace=fill_trace,
                )
            else:
                self.ledger.sell(
                    fill.token_id,
                    fill.quantity,
                    fill.price,
                    fee=fill.fee,
                    trace=fill_trace,
                )
            available -= size
            order.filled_quantity += size
            order.fees_paid += fee
            prior_status = order.status
            if order.remaining_quantity == ZERO:
                order.status = MakerStatus.FILLED
            elif order.remaining_capacity == ZERO:
                order.status = MakerStatus.CAPACITY_EXHAUSTED
            elif prior_status is MakerStatus.CANCEL_PENDING:
                order.status = MakerStatus.CANCEL_PENDING
            else:
                order.status = MakerStatus.PARTIAL
            fills.append(fill)
            self._record(
                "maker_fill",
                order,
                event.source_event_id,
                event.timestamp_ms,
                details={
                    "quantity": str(size),
                    "price": str(fill.price),
                    "fee": str(fee),
                    "queue_consumed": str(queue_consumed),
                    "late_after_cancel_request": str(
                        prior_status is MakerStatus.CANCEL_PENDING
                    ).lower(),
                },
            )
        return tuple(fills)

    def _invalidate_broken_reservations(
        self,
        *,
        source_event_id: str,
        timestamp_ms: int,
    ) -> None:
        """Fail closed if another ledger action spent reserved resources."""

        if self.reserved_cash > self.ledger.cash:
            for order in tuple(self._orders.values()):
                if (
                    order.status in _RESERVING
                    and order.request.side is OrderSide.BUY
                ):
                    self._mark_stale(
                        order,
                        "cash_reservation_breached",
                        source_event_id,
                        timestamp_ms,
                    )

        sell_tokens = {
            order.request.token_id
            for order in self._orders.values()
            if order.status in _RESERVING
            and order.request.side is OrderSide.SELL
        }
        for token_id in sell_tokens:
            if self.reserved_inventory(token_id) <= self.ledger.position(token_id):
                continue
            for order in tuple(self._orders.values()):
                if (
                    order.status in _RESERVING
                    and order.request.side is OrderSide.SELL
                    and order.request.token_id == token_id
                ):
                    self._mark_stale(
                        order,
                        "inventory_reservation_breached",
                        source_event_id,
                        timestamp_ms,
                    )

    def _preflight_reservation(self, new_order: MakerOrder) -> None:
        request = new_order.request
        quantity = new_order.reservable_quantity
        if request.side is OrderSide.SELL:
            already_reserved = self.reserved_inventory(request.token_id)
            available = self.ledger.position(request.token_id) - already_reserved
            if quantity > available:
                raise InventoryError(
                    f"naked sell reservation blocked for {request.token_id}: "
                    f"need {quantity}, available {available}"
                )
        else:
            already_reserved_cash = self.reserved_cash
            needed = quantity * request.price + request.fee_schedule.fee(
                quantity, request.price, maker=True
            )
            available_cash = self.ledger.cash - already_reserved_cash
            if needed > available_cash:
                raise InsufficientCashError(
                    f"insufficient cash reservation: need {needed}, "
                    f"available {available_cash}"
                )

    @staticmethod
    def _cash_commitment(order: MakerOrder) -> Decimal:
        quantity = order.reservable_quantity
        return quantity * order.request.price + order.request.fee_schedule.fee(
            quantity,
            order.request.price,
            maker=True,
        )

    @staticmethod
    def _trade_qualifies(order: MakerOrder, event: TradeEvent) -> bool:
        if order.request.side is OrderSide.BUY:
            return (
                event.aggressor_side is OrderSide.SELL
                and event.price <= order.request.price
            )
        return (
            event.aggressor_side is OrderSide.BUY
            and event.price >= order.request.price
        )

    def _advance_orders(self, now_ms: int, *, source_event_id: str) -> None:
        for order in self._orders.values():
            if (
                order.status is MakerStatus.PENDING_SUBMIT
                and now_ms >= order.live_at_ms
            ):
                order.status = MakerStatus.LIVE
                self._record("live", order, source_event_id, order.live_at_ms)
            if (
                order.status is MakerStatus.CANCEL_PENDING
                and order.cancel_effective_ms is not None
                and now_ms >= order.cancel_effective_ms
            ):
                order.status = MakerStatus.CANCELLED
                self._record(
                    "cancel_effective",
                    order,
                    source_event_id,
                    order.cancel_effective_ms,
                )

    def _mark_stale(
        self,
        order: MakerOrder,
        reason: str,
        source_event_id: str,
        timestamp_ms: int,
    ) -> None:
        order.status = MakerStatus.STALE
        order.stale_reason = reason
        self._record(
            "stale",
            order,
            source_event_id,
            timestamp_ms,
            details={"reason": reason},
        )

    def _record(
        self,
        action: str,
        order: MakerOrder,
        source_event_id: str,
        timestamp_ms: int,
        *,
        details: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._trace.append(
            ReplayTrace(
                sequence=len(self._trace) + 1,
                action=action,
                order_id=order.order_id,
                status=order.status,
                source_event_id=source_event_id,
                decision_id=order.decision_id,
                timestamp_ms=timestamp_ms,
                details=dict(details or {}),
            )
        )
