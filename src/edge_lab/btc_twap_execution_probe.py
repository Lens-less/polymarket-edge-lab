"""Fail-closed BTC TWAP execution probe harness.

This module models the smallest allowed live-execution probe surface without
binding to any production venue adapter or credential source.  It enforces the
current policy:

* structure-floor routes only;
* exactly one common-expiry pair;
* isolated balance <= 12 USDC;
* cumulative buy notional <= 10 USDC;
* all-in route cost <= 0.99;
* one maker submission, one FOK hedge, and at most one emergency FAK unwind;
* plan-hash-bound risk acceptance plus a separate final confirmation; and
* persistent kill-switch semantics with canonical hash-chained receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .data_store import canonical_json_bytes

ZERO = Decimal(0)
ONE = Decimal(1)
MAX_ISOLATED_BALANCE = Decimal(12)
MAX_BUY_NOTIONAL = Decimal(10)
MAX_ALL_IN_COST = Decimal("0.99")
MAX_EXECUTION_QUOTE_AGE_MS = 750
SNAPSHOT_SCHEMA = "edge-lab-btc-twap-execution-probe-state.v1"
RECEIPT_SCHEMA = "edge-lab-btc-twap-execution-probe-receipt.v1"
PERSISTENCE_SCHEMA = "edge-lab-btc-twap-execution-probe-persistence.v1"
FINAL_CONFIRMATION_MAX_LIFETIME_MS = 300_000
ACCOUNT_LOSS_ACKNOWLEDGEMENT = "I ACCEPT ACCOUNT MAX LOSS 12 USDC"
_FLOOR_ACTIONS_BY_STRIKE_ORDERING = {
    "strike_5_below_strike_15": frozenset({"long_5_up_long_15_down"}),
    "strike_5_above_strike_15": frozenset({"long_15_up_long_5_down"}),
    "strikes_equal": frozenset(
        {"long_5_up_long_15_down", "long_15_up_long_5_down"}
    ),
}
_LEGS_BY_FLOOR_ACTION = {
    "long_5_up_long_15_down": frozenset({("5m", "up"), ("15m", "down")}),
    "long_15_up_long_5_down": frozenset({("15m", "up"), ("5m", "down")}),
}


def _sha256_text(value: str, *, name: str) -> str:
    result = _nonempty(value, name=name).lower()
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return result


def _integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except Exception as exc:  # pragma: no cover - Decimal exceptions vary
            raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive_decimal(value: Any, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= ZERO:
        raise ValueError(f"{name} must be positive")
    return result


def _probability(value: Any, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if not ZERO < result <= ONE:
        raise ValueError(f"{name} must be in (0, 1]")
    return result


def _nonempty(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _side(value: str) -> str:
    normalized = _nonempty(value, name="side").lower()
    if normalized not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    return normalized


def _outcome(value: str) -> str:
    normalized = _nonempty(value, name="outcome").lower()
    if normalized not in {"up", "down"}:
        raise ValueError("outcome must be 'up' or 'down'")
    return normalized


class ProbeKillSwitchError(RuntimeError):
    """Raised once the probe enters a durable killed state."""


class ProbeSubmitStatus(str, Enum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


class ProbeEventKind(str, Enum):
    FILL = "fill"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    DISCONNECT = "disconnect"


@dataclass(frozen=True)
class ProbeOrder:
    leg_id: str
    horizon: str
    outcome: str
    market_id: str
    token_id: str
    fee_rule_sha256: str
    side: str
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "leg_id", _nonempty(self.leg_id, name="leg_id"))
        object.__setattr__(self, "horizon", _nonempty(self.horizon, name="horizon"))
        if self.horizon not in {"5m", "15m"}:
            raise ValueError("horizon must be '5m' or '15m'")
        object.__setattr__(self, "outcome", _outcome(self.outcome))
        object.__setattr__(
            self, "market_id", _nonempty(self.market_id, name="market_id")
        )
        object.__setattr__(self, "token_id", _nonempty(self.token_id, name="token_id"))
        object.__setattr__(
            self,
            "fee_rule_sha256",
            _sha256_text(self.fee_rule_sha256, name="fee_rule_sha256"),
        )
        object.__setattr__(self, "side", _side(self.side))
        object.__setattr__(self, "price", _probability(self.price, name="price"))
        object.__setattr__(
            self, "quantity", _positive_decimal(self.quantity, name="quantity")
        )

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price

    def with_quantity(self, quantity: Decimal) -> ProbeOrder:
        return ProbeOrder(
            leg_id=self.leg_id,
            horizon=self.horizon,
            outcome=self.outcome,
            market_id=self.market_id,
            token_id=self.token_id,
            fee_rule_sha256=self.fee_rule_sha256,
            side=self.side,
            price=self.price,
            quantity=quantity,
        )

    def with_execution(
        self,
        *,
        side: str | None = None,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> ProbeOrder:
        return ProbeOrder(
            leg_id=self.leg_id,
            horizon=self.horizon,
            outcome=self.outcome,
            market_id=self.market_id,
            token_id=self.token_id,
            fee_rule_sha256=self.fee_rule_sha256,
            side=self.side if side is None else side,
            price=self.price if price is None else price,
            quantity=self.quantity if quantity is None else quantity,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "leg_id": self.leg_id,
            "horizon": self.horizon,
            "outcome": self.outcome,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "fee_rule_sha256": self.fee_rule_sha256,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
        }


@dataclass(frozen=True)
class ProbeRouteCandidate:
    candidate_id: str
    common_expiry_id: str
    maker_order: ProbeOrder
    hedge_order: ProbeOrder
    total_cost: Decimal
    hedge_depth_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _nonempty(self.candidate_id, name="candidate_id"),
        )
        object.__setattr__(
            self,
            "common_expiry_id",
            _nonempty(self.common_expiry_id, name="common_expiry_id"),
        )
        object.__setattr__(
            self,
            "total_cost",
            _positive_decimal(self.total_cost, name="total_cost"),
        )
        if self.maker_order.leg_id == self.hedge_order.leg_id:
            raise ValueError("maker and hedge legs must differ")
        if self.maker_order.market_id == self.hedge_order.market_id:
            raise ValueError("maker and hedge markets must differ")
        if self.maker_order.side != "buy" or self.hedge_order.side != "buy":
            raise ValueError("structure-floor probe legs must both be buys")
        if self.maker_order.quantity != self.hedge_order.quantity:
            raise ValueError("maker and hedge quantities must match")
        if not isinstance(self.hedge_depth_available, bool):
            raise TypeError("hedge_depth_available must be bool")

    @property
    def buy_notional(self) -> Decimal:
        return sum(
            (
                order.notional
                for order in (self.maker_order, self.hedge_order)
                if order.side == "buy"
            ),
            ZERO,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "common_expiry_id": self.common_expiry_id,
            "maker_order": self.maker_order.to_document(),
            "hedge_order": self.hedge_order.to_document(),
            "total_cost": self.total_cost,
            "hedge_depth_available": self.hedge_depth_available,
        }


@dataclass(frozen=True)
class ProbePlan:
    plan_id: str
    strategy_kind: str
    strike_ordering: str
    floor_action: str
    co_terminal_validator_sha256: str
    common_expiry_id: str
    isolated_balance: Decimal
    route_candidates: tuple[ProbeRouteCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _nonempty(self.plan_id, name="plan_id"))
        object.__setattr__(
            self,
            "strategy_kind",
            _nonempty(self.strategy_kind, name="strategy_kind"),
        )
        object.__setattr__(
            self,
            "strike_ordering",
            _nonempty(self.strike_ordering, name="strike_ordering"),
        )
        object.__setattr__(
            self,
            "floor_action",
            _nonempty(self.floor_action, name="floor_action"),
        )
        allowed_floor_actions = _FLOOR_ACTIONS_BY_STRIKE_ORDERING.get(
            self.strike_ordering
        )
        if allowed_floor_actions is None:
            raise ValueError("unsupported structural strike ordering")
        if self.floor_action not in allowed_floor_actions:
            raise ValueError("floor action conflicts with strike ordering")
        object.__setattr__(
            self,
            "co_terminal_validator_sha256",
            _sha256_text(
                self.co_terminal_validator_sha256,
                name="co_terminal_validator_sha256",
            ),
        )
        object.__setattr__(
            self,
            "common_expiry_id",
            _nonempty(self.common_expiry_id, name="common_expiry_id"),
        )
        object.__setattr__(
            self,
            "isolated_balance",
            _positive_decimal(self.isolated_balance, name="isolated_balance"),
        )
        if len(self.route_candidates) != 2:
            raise ValueError("probe plan must compare exactly two maker directions")
        if {candidate.common_expiry_id for candidate in self.route_candidates} != {
            self.common_expiry_id
        }:
            raise ValueError("all route candidates must share one common expiry")
        maker_legs = {
            candidate.maker_order.leg_id for candidate in self.route_candidates
        }
        if len(maker_legs) != len(self.route_candidates):
            raise ValueError("maker candidates must point to distinct legs")
        leg_pairs = {
            frozenset(
                (
                    candidate.maker_order.leg_id,
                    candidate.hedge_order.leg_id,
                )
            )
            for candidate in self.route_candidates
        }
        if len(leg_pairs) != 1:
            raise ValueError(
                "maker directions must reverse the same two structural legs"
            )
        expected_legs = _LEGS_BY_FLOOR_ACTION[self.floor_action]
        for candidate in self.route_candidates:
            actual_legs = frozenset(
                {
                    (candidate.maker_order.horizon, candidate.maker_order.outcome),
                    (candidate.hedge_order.horizon, candidate.hedge_order.outcome),
                }
            )
            if actual_legs != expected_legs:
                raise ValueError(
                    "route candidate legs conflict with the structural floor action"
                )
        for first, second in zip(
            self.route_candidates,
            reversed(self.route_candidates),
        ):
            if (
                first.maker_order.token_id != second.hedge_order.token_id
                or first.hedge_order.token_id != second.maker_order.token_id
            ):
                raise ValueError("maker directions must reverse the same two tokens")

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_document())).hexdigest()

    def to_document(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "strategy_kind": self.strategy_kind,
            "strike_ordering": self.strike_ordering,
            "floor_action": self.floor_action,
            "co_terminal_validator_sha256": self.co_terminal_validator_sha256,
            "common_expiry_id": self.common_expiry_id,
            "isolated_balance": self.isolated_balance,
            "route_candidates": [
                candidate.to_document() for candidate in self.route_candidates
            ],
        }


@dataclass(frozen=True)
class ProbeSubmitAck:
    status: ProbeSubmitStatus
    order_id: str | None = None
    reason: str | None = None

    @classmethod
    def accepted(cls, order_id: str) -> ProbeSubmitAck:
        return cls(
            status=ProbeSubmitStatus.ACCEPTED,
            order_id=_nonempty(order_id, name="order_id"),
        )

    @classmethod
    def rejected(cls, *, reason: str) -> ProbeSubmitAck:
        return cls(
            status=ProbeSubmitStatus.REJECTED, reason=_nonempty(reason, name="reason")
        )

    @classmethod
    def ambiguous(cls, *, reason: str) -> ProbeSubmitAck:
        return cls(
            status=ProbeSubmitStatus.AMBIGUOUS, reason=_nonempty(reason, name="reason")
        )


@dataclass(frozen=True)
class ProbeSubmitResult:
    status: ProbeSubmitStatus
    order_id: str | None = None
    reason: str | None = None
    filled_quantity: Decimal = ZERO
    executed_notional: Decimal = ZERO
    fee: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "filled_quantity",
            _decimal(self.filled_quantity, name="filled_quantity"),
        )
        object.__setattr__(
            self,
            "executed_notional",
            _decimal(self.executed_notional, name="executed_notional"),
        )
        object.__setattr__(self, "fee", _decimal(self.fee, name="fee"))
        if (
            self.filled_quantity < ZERO
            or self.executed_notional < ZERO
            or self.fee < ZERO
        ):
            raise ValueError("execution result quantities and costs cannot be negative")
        if self.status is ProbeSubmitStatus.FILLED:
            if self.order_id is None or self.filled_quantity <= ZERO:
                raise ValueError(
                    "filled result requires order_id and positive quantity"
                )
        elif any(
            value != ZERO
            for value in (self.filled_quantity, self.executed_notional, self.fee)
        ):
            raise ValueError("non-filled result cannot claim execution")

    @classmethod
    def filled(
        cls,
        order_id: str,
        *,
        filled_quantity: Decimal,
        executed_notional: Decimal,
        fee: Decimal,
    ) -> ProbeSubmitResult:
        return cls(
            status=ProbeSubmitStatus.FILLED,
            order_id=_nonempty(order_id, name="order_id"),
            filled_quantity=filled_quantity,
            executed_notional=executed_notional,
            fee=fee,
        )

    @classmethod
    def rejected(cls, *, order_id: str | None = None, reason: str) -> ProbeSubmitResult:
        return cls(
            status=ProbeSubmitStatus.REJECTED,
            order_id=order_id,
            reason=_nonempty(reason, name="reason"),
        )

    @classmethod
    def ambiguous(
        cls, *, order_id: str | None = None, reason: str
    ) -> ProbeSubmitResult:
        return cls(
            status=ProbeSubmitStatus.AMBIGUOUS,
            order_id=order_id,
            reason=_nonempty(reason, name="reason"),
        )


@dataclass(frozen=True)
class ProbeEvent:
    kind: ProbeEventKind
    order_id: str | None = None
    fill_id: str | None = None
    quantity: Decimal = ZERO
    price: Decimal | None = None
    fee: Decimal = ZERO
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.order_id is not None:
            object.__setattr__(
                self, "order_id", _nonempty(self.order_id, name="order_id")
            )
        if self.fill_id is not None:
            object.__setattr__(self, "fill_id", _nonempty(self.fill_id, name="fill_id"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, name="quantity"))
        object.__setattr__(self, "fee", _decimal(self.fee, name="fee"))
        if self.quantity < ZERO:
            raise ValueError("quantity cannot be negative")
        if self.fee < ZERO:
            raise ValueError("fee cannot be negative")
        if self.kind is ProbeEventKind.FILL:
            if self.order_id is None or self.fill_id is None:
                raise ValueError("fill events require order_id and fill_id")
            if self.quantity <= ZERO:
                raise ValueError("fill quantity must be positive")
            if self.price is None:
                raise ValueError("fill events require an execution price")
            object.__setattr__(self, "price", _probability(self.price, name="price"))
        elif self.price is not None or self.fee != ZERO:
            raise ValueError("non-fill events cannot claim price or fee")

    @classmethod
    def fill(
        cls,
        *,
        order_id: str,
        fill_id: str,
        quantity: str | Decimal,
        price: str | Decimal,
        fee: str | Decimal,
    ) -> ProbeEvent:
        return cls(
            kind=ProbeEventKind.FILL,
            order_id=order_id,
            fill_id=fill_id,
            quantity=_positive_decimal(quantity, name="quantity"),
            price=_probability(price, name="price"),
            fee=_decimal(fee, name="fee"),
        )

    @classmethod
    def cancelled(cls, *, order_id: str) -> ProbeEvent:
        return cls(kind=ProbeEventKind.CANCELLED, order_id=order_id)

    def to_document(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "order_id": self.order_id,
            "fill_id": self.fill_id,
            "quantity": self.quantity,
            "price": self.price,
            "fee": self.fee,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProbeExecutionQuote:
    order: ProbeOrder
    full_depth_available: bool
    quoted_notional: Decimal
    quoted_fee: Decimal
    source_timestamp_ms: int
    receipt_timestamp_ms: int
    book_sha256: str
    fee_rule_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.full_depth_available, bool):
            raise TypeError("full_depth_available must be bool")
        object.__setattr__(
            self,
            "quoted_notional",
            _decimal(self.quoted_notional, name="quoted_notional"),
        )
        object.__setattr__(
            self,
            "quoted_fee",
            _decimal(self.quoted_fee, name="quoted_fee"),
        )
        if self.quoted_notional < ZERO or self.quoted_fee < ZERO:
            raise ValueError("quoted costs cannot be negative")
        if self.full_depth_available and self.quoted_notional <= ZERO:
            raise ValueError("available quote requires positive notional")
        object.__setattr__(
            self,
            "source_timestamp_ms",
            _integer(self.source_timestamp_ms, name="source_timestamp_ms"),
        )
        object.__setattr__(
            self,
            "receipt_timestamp_ms",
            _integer(self.receipt_timestamp_ms, name="receipt_timestamp_ms"),
        )
        if self.receipt_timestamp_ms < self.source_timestamp_ms:
            raise ValueError("quote receipt cannot precede its source timestamp")
        object.__setattr__(
            self,
            "book_sha256",
            _sha256_text(self.book_sha256, name="book_sha256"),
        )
        object.__setattr__(
            self,
            "fee_rule_sha256",
            _sha256_text(self.fee_rule_sha256, name="fee_rule_sha256"),
        )
        if self.fee_rule_sha256 != self.order.fee_rule_sha256:
            raise ValueError("execution quote fee rule does not match its bound order")

    def with_quantity(self, quantity: Decimal) -> ProbeExecutionQuote:
        quantity = _positive_decimal(quantity, name="quantity")
        if quantity == self.order.quantity:
            return self
        ratio = quantity / self.order.quantity
        return ProbeExecutionQuote(
            order=self.order.with_quantity(quantity),
            full_depth_available=self.full_depth_available,
            quoted_notional=self.quoted_notional * ratio,
            quoted_fee=self.quoted_fee * ratio,
            source_timestamp_ms=self.source_timestamp_ms,
            receipt_timestamp_ms=self.receipt_timestamp_ms,
            book_sha256=self.book_sha256,
            fee_rule_sha256=self.fee_rule_sha256,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "order": self.order.to_document(),
            "full_depth_available": self.full_depth_available,
            "quoted_notional": self.quoted_notional,
            "quoted_fee": self.quoted_fee,
            "source_timestamp_ms": self.source_timestamp_ms,
            "receipt_timestamp_ms": self.receipt_timestamp_ms,
            "book_sha256": self.book_sha256,
            "fee_rule_sha256": self.fee_rule_sha256,
        }


@dataclass(frozen=True)
class ProbeCancelReconciliation:
    maker_order_id: str
    open_order_ids: tuple[str, ...]
    fills: tuple[ProbeEvent, ...]
    user_stream_verified: bool
    captured_at_ms: int
    state_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maker_order_id",
            _nonempty(self.maker_order_id, name="maker_order_id"),
        )
        if any(not isinstance(item, str) or not item for item in self.open_order_ids):
            raise ValueError("open_order_ids must contain non-empty strings")
        if any(event.kind is not ProbeEventKind.FILL for event in self.fills):
            raise ValueError("reconciliation fills must be fill events")
        if not isinstance(self.user_stream_verified, bool):
            raise TypeError("user_stream_verified must be bool")
        object.__setattr__(
            self,
            "captured_at_ms",
            _integer(self.captured_at_ms, name="captured_at_ms"),
        )
        object.__setattr__(
            self,
            "state_sha256",
            _sha256_text(self.state_sha256, name="state_sha256"),
        )


@dataclass(frozen=True)
class ProbeFinalConfirmation:
    plan_hash: str
    accepted_max_loss: Decimal
    nonce: str
    issued_at_ms: int
    expires_at_ms: int
    acknowledgement: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_hash",
            _sha256_text(self.plan_hash, name="plan_hash"),
        )
        object.__setattr__(
            self,
            "accepted_max_loss",
            _decimal(self.accepted_max_loss, name="accepted_max_loss"),
        )
        object.__setattr__(self, "nonce", _nonempty(self.nonce, name="nonce"))
        if len(self.nonce) < 16:
            raise ValueError("final confirmation nonce is too short")
        object.__setattr__(
            self,
            "issued_at_ms",
            _integer(self.issued_at_ms, name="issued_at_ms"),
        )
        object.__setattr__(
            self,
            "expires_at_ms",
            _integer(self.expires_at_ms, name="expires_at_ms"),
        )
        object.__setattr__(
            self,
            "acknowledgement",
            _nonempty(self.acknowledgement, name="acknowledgement"),
        )

    @property
    def confirmation_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "plan_hash": self.plan_hash,
                    "accepted_max_loss": self.accepted_max_loss,
                    "nonce": self.nonce,
                    "issued_at_ms": self.issued_at_ms,
                    "expires_at_ms": self.expires_at_ms,
                    "acknowledgement": self.acknowledgement,
                }
            )
        ).hexdigest()


class ProbePersistence(Protocol):
    def load(self) -> Mapping[str, Any] | None: ...

    def save(self, snapshot: Mapping[str, Any]) -> None: ...


@dataclass
class InMemoryProbePersistence:
    snapshot: dict[str, Any] | None = None

    def load(self) -> Mapping[str, Any] | None:
        return deepcopy(self.snapshot)

    def save(self, snapshot: Mapping[str, Any]) -> None:
        self.snapshot = deepcopy(dict(snapshot))


@dataclass(frozen=True)
class FileProbePersistence:
    """Atomic local persistence with an integrity envelope and restrictive mode."""

    path: Path

    def __post_init__(self) -> None:
        expanded = self.path.expanduser()
        object.__setattr__(self, "path", expanded.absolute())

    def load(self) -> Mapping[str, Any] | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("execution probe persistence path must be a regular file")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("execution probe persistence is unreadable") from exc
        if not isinstance(document, Mapping):
            raise TypeError("execution probe persistence must be a JSON object")
        if document.get("schema_version") != PERSISTENCE_SCHEMA:
            raise ValueError("execution probe persistence schema mismatch")
        snapshot = document.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise TypeError("execution probe persistence snapshot is missing")
        expected = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        if document.get("snapshot_sha256") != expected:
            raise ValueError("execution probe snapshot hash mismatch")
        return deepcopy(dict(snapshot))

    def save(self, snapshot: Mapping[str, Any]) -> None:
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("execution probe persistence path cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        snapshot_document = deepcopy(dict(snapshot))
        envelope = {
            "schema_version": PERSISTENCE_SCHEMA,
            "snapshot": snapshot_document,
            "snapshot_sha256": hashlib.sha256(
                canonical_json_bytes(snapshot_document)
            ).hexdigest(),
        }
        content = canonical_json_bytes(envelope) + b"\n"
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            try:
                parent_descriptor = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                parent_descriptor = None
            if parent_descriptor is not None:
                try:
                    os.fsync(parent_descriptor)
                except OSError:
                    pass
                finally:
                    os.close(parent_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class ProbeReceipt:
    sequence: int
    recorded_at_ms: int
    event_type: str
    body: Mapping[str, Any]
    previous_receipt_sha256: str | None
    receipt_sha256: str

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        recorded_at_ms: int,
        event_type: str,
        body: Mapping[str, Any],
        previous_receipt_sha256: str | None,
    ) -> ProbeReceipt:
        envelope = {
            "schema_version": RECEIPT_SCHEMA,
            "sequence": sequence,
            "recorded_at_ms": _integer(recorded_at_ms, name="recorded_at_ms"),
            "event_type": _nonempty(event_type, name="event_type"),
            "body": dict(body),
            "previous_receipt_sha256": previous_receipt_sha256,
        }
        digest = hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()
        return cls(
            sequence=sequence,
            recorded_at_ms=envelope["recorded_at_ms"],
            event_type=envelope["event_type"],
            body=envelope["body"],
            previous_receipt_sha256=previous_receipt_sha256,
            receipt_sha256=digest,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA,
            "sequence": self.sequence,
            "recorded_at_ms": self.recorded_at_ms,
            "event_type": self.event_type,
            "body": dict(self.body),
            "previous_receipt_sha256": self.previous_receipt_sha256,
            "receipt_sha256": self.receipt_sha256,
        }


@dataclass(frozen=True)
class ProbeRunResult:
    selected_candidate_id: str | None
    maker_filled_quantity: Decimal
    hedge_submitted: bool
    unwind_submitted: bool
    completed_matched: bool
    completed_flat: bool
    kill_switch_engaged: bool


class ProbeVenue(Protocol):
    def assert_authenticated_readiness(self) -> None: ...

    def open_user_event_stream(self, *, plan_id: str) -> tuple[ProbeEvent, ...]: ...

    def submit_post_only_maker(self, order: ProbeOrder) -> ProbeSubmitAck: ...

    def quote_fok_hedge(self, order: ProbeOrder) -> ProbeExecutionQuote: ...

    def submit_fok_hedge(self, order: ProbeOrder) -> ProbeSubmitResult: ...

    def quote_fak_unwind(self, order: ProbeOrder) -> ProbeExecutionQuote: ...

    def submit_fak_unwind(self, order: ProbeOrder) -> ProbeSubmitResult: ...

    def cancel_all(self, *, reason: str) -> bool: ...

    def reconcile_after_cancel(
        self, *, plan_id: str, maker_order_id: str
    ) -> ProbeCancelReconciliation: ...


def _default_snapshot() -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "kill_switch_engaged": False,
        "kill_reason": None,
        "bound_plan_hash": None,
        "bound_max_loss": None,
        "final_confirmation_sha256": None,
        "submission_started": False,
        "selected_candidate_id": None,
        "maker_order_id": None,
        "maker_filled_quantity": ZERO,
        "maker_fill_notional": ZERO,
        "maker_fill_fees": ZERO,
        "hedge_submitted": False,
        "unwind_submitted": False,
        "completed_matched": False,
        "completed_flat": False,
        "seen_fill_ids": {},
        "receipts": [],
    }


@dataclass
class ExecutionProbeHarness:
    venue: ProbeVenue
    persistence: ProbePersistence
    now_ms: Callable[[], int] = field(
        default=lambda: time.time_ns() // 1_000_000,
        repr=False,
    )
    _snapshot: dict[str, Any] = field(init=False, repr=False)
    _receipts: list[ProbeReceipt] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        loaded = self.persistence.load()
        self._snapshot = _default_snapshot() if loaded is None else dict(loaded)
        if self._snapshot.get("schema_version") != SNAPSHOT_SCHEMA:
            raise ValueError("execution probe snapshot schema mismatch")
        self._receipts = self._load_receipts(self._snapshot.get("receipts", ()))
        self._snapshot["receipts"] = [
            receipt.to_document() for receipt in self._receipts
        ]
        self._validate_snapshot_state()
        if (
            loaded is not None
            and self._snapshot.get("submission_started")
            and not self._snapshot.get("completed_matched")
            and not self._snapshot.get("completed_flat")
            and not self._snapshot.get("kill_switch_engaged")
        ):
            self._engage_kill("restart_with_unreconciled_submission")
        self._persist()

    @property
    def kill_switch_engaged(self) -> bool:
        return bool(self._snapshot["kill_switch_engaged"])

    def bind_risk_acceptance(
        self,
        plan: ProbePlan,
        *,
        accepted_plan_hash: str,
        accepted_max_loss: Decimal,
    ) -> ProbeReceipt:
        self._ensure_not_killed()
        if self._snapshot["submission_started"]:
            raise ValueError("probe already submitted")
        self._validate_plan_policy(plan)
        if accepted_plan_hash != plan.plan_hash:
            raise ValueError("risk acceptance must bind the exact plan hash")
        if (
            _decimal(accepted_max_loss, name="accepted_max_loss")
            != MAX_ISOLATED_BALANCE
        ):
            raise ValueError("risk acceptance must bind the 12 USDC account loss cap")
        self._snapshot["bound_plan_hash"] = plan.plan_hash
        self._snapshot["bound_max_loss"] = MAX_ISOLATED_BALANCE
        receipt = self._append_receipt(
            "risk_acceptance_bound",
            {
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "accepted_max_loss": MAX_ISOLATED_BALANCE,
            },
        )
        return receipt

    def execute(
        self,
        plan: ProbePlan,
        *,
        final_confirmation: ProbeFinalConfirmation,
    ) -> ProbeRunResult:
        self._ensure_not_killed()
        if self._snapshot["bound_plan_hash"] is None:
            raise ValueError("risk acceptance must be bound before execution")
        if self._snapshot["bound_plan_hash"] != plan.plan_hash:
            raise ValueError("bound risk acceptance does not match this plan hash")
        if self._snapshot["submission_started"]:
            raise ValueError("probe already submitted")
        self._validate_plan_policy(plan)
        self._validate_final_confirmation(plan, final_confirmation)
        self._snapshot["submission_started"] = True
        self._snapshot["final_confirmation_sha256"] = (
            final_confirmation.confirmation_sha256
        )
        self._append_receipt(
            "final_confirmation_bound",
            {
                "plan_hash": plan.plan_hash,
                "confirmation_sha256": final_confirmation.confirmation_sha256,
                "accepted_max_loss": final_confirmation.accepted_max_loss,
                "issued_at_ms": final_confirmation.issued_at_ms,
                "expires_at_ms": final_confirmation.expires_at_ms,
            },
        )
        try:
            return self._execute_bound_plan(plan)
        except ProbeKillSwitchError:
            raise
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            self._fail_closed(f"unexpected_exception:{type(exc).__name__}")
            raise

    def _execute_bound_plan(self, plan: ProbePlan) -> ProbeRunResult:
        self.venue.assert_authenticated_readiness()
        self._append_receipt(
            "authenticated_readiness_verified", {"plan_id": plan.plan_id}
        )
        candidate = self._select_candidate(plan)
        self._snapshot["selected_candidate_id"] = candidate.candidate_id
        self._append_receipt(
            "route_selected",
            {
                "candidate_id": candidate.candidate_id,
                "maker_leg_id": candidate.maker_order.leg_id,
                "hedge_leg_id": candidate.hedge_order.leg_id,
                "total_cost": candidate.total_cost,
            },
        )
        maker_ack = self.venue.submit_post_only_maker(candidate.maker_order)
        if (
            maker_ack.status is not ProbeSubmitStatus.ACCEPTED
            or maker_ack.order_id is None
        ):
            reason = maker_ack.reason or maker_ack.status.value
            self._fail_closed(f"maker_submit_{reason}")
        maker_order_id = maker_ack.order_id
        assert maker_order_id is not None
        self._snapshot["maker_order_id"] = maker_order_id
        self._append_receipt(
            "maker_submitted",
            {"candidate_id": candidate.candidate_id, "order_id": maker_order_id},
        )

        cancel_requested = False
        for event in self.venue.open_user_event_stream(plan_id=plan.plan_id):
            if event.kind is ProbeEventKind.DISCONNECT:
                reason = event.reason or "stream_closed"
                self._fail_closed(f"disconnect:{reason}")
            if event.kind is ProbeEventKind.REJECTED:
                self._fail_closed(event.reason or "order_rejected")
            if event.order_id != maker_order_id:
                self._fail_closed("unexpected_foreign_order_event")
            if event.kind is ProbeEventKind.CANCELLED:
                self._append_receipt("maker_cancelled", {"order_id": maker_order_id})
                continue
            if event.kind is not ProbeEventKind.FILL:
                continue
            self._observe_maker_fill(
                event,
                maker_order=candidate.maker_order,
                maker_order_id=maker_order_id,
            )
            if not cancel_requested:
                if not self.venue.cancel_all(reason="maker_fill_detected"):
                    self._fail_closed("cancel_all_failed")
                cancel_requested = True
                self._append_receipt(
                    "cancel_all_requested",
                    {"reason": "maker_fill_detected", "order_id": maker_order_id},
                )

        if not cancel_requested:
            if not self.venue.cancel_all(reason="stream_complete_cleanup"):
                self._fail_closed("cancel_all_failed")
            cancel_requested = True
            self._append_receipt(
                "cancel_all_requested",
                {"reason": "stream_complete_cleanup", "order_id": maker_order_id},
            )
        reconciliation = self.venue.reconcile_after_cancel(
            plan_id=plan.plan_id,
            maker_order_id=maker_order_id,
        )
        self._apply_reconciliation(
            reconciliation,
            maker_order=candidate.maker_order,
            maker_order_id=maker_order_id,
        )
        maker_filled_quantity = _decimal(
            self._snapshot["maker_filled_quantity"], name="maker_filled_quantity"
        )
        if maker_filled_quantity == ZERO:
            self._snapshot["completed_flat"] = True
            self._append_receipt("probe_complete", {"path": "maker_no_fill"})
            return self._result()

        hedge_template = candidate.hedge_order.with_quantity(maker_filled_quantity)
        try:
            hedge_quote = self.venue.quote_fok_hedge(hedge_template)
        except Exception as exc:  # noqa: BLE001 - adapter failure still unwinds
            hedge_quote = None
            hedge_rejection = f"fok_quote_failed:{type(exc).__name__}"
            self._append_receipt(
                "hedge_quote_failed",
                {
                    "candidate_id": candidate.candidate_id,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            hedge_rejection = self._hedge_quote_rejection(
                candidate=candidate,
                maker_filled_quantity=maker_filled_quantity,
                quote=hedge_quote,
                isolated_balance=plan.isolated_balance,
            )
            self._append_receipt(
                "hedge_quote_observed",
                {
                    "candidate_id": candidate.candidate_id,
                    "quote": hedge_quote.to_document(),
                    "rejection_reason": hedge_rejection,
                },
            )
        if hedge_rejection is None and hedge_quote is not None:
            fok_result = self.venue.submit_fok_hedge(hedge_quote.order)
            self._snapshot["hedge_submitted"] = True
            self._append_receipt(
                "hedge_submitted",
                {
                    "order_id": fok_result.order_id,
                    "quantity": hedge_quote.order.quantity,
                    "candidate_id": candidate.candidate_id,
                },
            )
            if fok_result.status is ProbeSubmitStatus.FILLED:
                self._validate_complete_execution(
                    result=fok_result,
                    quote=hedge_quote,
                    expected_quantity=maker_filled_quantity,
                    label="fok",
                )
                self._snapshot["completed_flat"] = True
                self._snapshot["completed_matched"] = True
                self._snapshot["completed_flat"] = False
                self._append_receipt(
                    "probe_complete",
                    {
                        "path": "maker_plus_fok",
                        "fok_executed_notional": fok_result.executed_notional,
                        "fok_fee": fok_result.fee,
                    },
                )
                return self._result()
            if fok_result.status is ProbeSubmitStatus.AMBIGUOUS:
                self._fail_closed(fok_result.reason or "fok_ambiguous")
            unwind_reason = fok_result.reason or fok_result.status.value
        else:
            unwind_reason = hedge_rejection

        unwind_template = candidate.maker_order.with_execution(
            side="sell",
            quantity=maker_filled_quantity,
        )
        unwind_quote = self.venue.quote_fak_unwind(unwind_template)
        self._validate_unwind_quote(
            template=unwind_template,
            quote=unwind_quote,
        )
        self._append_receipt(
            "emergency_unwind_quote_observed",
            {"reason": unwind_reason, "quote": unwind_quote.to_document()},
        )
        unwind_result = self.venue.submit_fak_unwind(unwind_quote.order)
        self._snapshot["unwind_submitted"] = True
        self._append_receipt(
            "emergency_unwind_submitted",
            {
                "order_id": unwind_result.order_id,
                "quantity": unwind_quote.order.quantity,
                "reason": unwind_reason,
            },
        )
        if unwind_result.status is not ProbeSubmitStatus.FILLED:
            self._fail_closed(unwind_result.reason or "unwind_failed")
        self._validate_complete_execution(
            result=unwind_result,
            quote=unwind_quote,
            expected_quantity=maker_filled_quantity,
            label="unwind",
        )
        self._snapshot["completed_flat"] = True
        self._append_receipt("probe_complete", {"path": "maker_plus_unwind"})
        return self._result()

    def _result(self) -> ProbeRunResult:
        self._persist()
        return ProbeRunResult(
            selected_candidate_id=self._snapshot["selected_candidate_id"],
            maker_filled_quantity=_decimal(
                self._snapshot["maker_filled_quantity"], name="maker_filled_quantity"
            ),
            hedge_submitted=bool(self._snapshot["hedge_submitted"]),
            unwind_submitted=bool(self._snapshot["unwind_submitted"]),
            completed_matched=bool(self._snapshot["completed_matched"]),
            completed_flat=bool(self._snapshot["completed_flat"]),
            kill_switch_engaged=bool(self._snapshot["kill_switch_engaged"]),
        )

    def _select_candidate(self, plan: ProbePlan) -> ProbeRouteCandidate:
        eligible = [
            candidate
            for candidate in plan.route_candidates
            if (
                candidate.hedge_depth_available
                and candidate.total_cost <= MAX_ALL_IN_COST
                and candidate.buy_notional <= MAX_BUY_NOTIONAL
                and candidate.buy_notional <= plan.isolated_balance
            )
        ]
        if not eligible:
            raise ValueError("no executable structure-floor route candidate")
        eligible.sort(
            key=lambda candidate: (candidate.total_cost, candidate.candidate_id)
        )
        return eligible[0]

    def _observe_maker_fill(
        self,
        event: ProbeEvent,
        *,
        maker_order: ProbeOrder,
        maker_order_id: str,
    ) -> None:
        if event.kind is not ProbeEventKind.FILL or event.order_id != maker_order_id:
            self._fail_closed("invalid_maker_fill_event")
        assert event.fill_id is not None
        assert event.price is not None
        seen_fill_ids = dict(self._snapshot["seen_fill_ids"])
        observed = {
            "quantity": event.quantity,
            "price": event.price,
            "fee": event.fee,
        }
        prior = seen_fill_ids.get(event.fill_id)
        if prior is not None:
            if not isinstance(prior, Mapping) or dict(prior) != observed:
                self._fail_closed("conflicting_duplicate_fill")
            self._append_receipt(
                "duplicate_fill_ignored",
                {"fill_id": event.fill_id, **observed},
            )
            return
        if event.price > maker_order.price:
            self._fail_closed("maker_fill_above_limit")
        new_quantity = (
            _decimal(
                self._snapshot["maker_filled_quantity"],
                name="maker_filled_quantity",
            )
            + event.quantity
        )
        if new_quantity > maker_order.quantity:
            self._fail_closed("maker_overfill")
        seen_fill_ids[event.fill_id] = observed
        self._snapshot["seen_fill_ids"] = seen_fill_ids
        self._snapshot["maker_filled_quantity"] = new_quantity
        self._snapshot["maker_fill_notional"] = (
            _decimal(
                self._snapshot["maker_fill_notional"],
                name="maker_fill_notional",
            )
            + event.quantity * event.price
        )
        self._snapshot["maker_fill_fees"] = (
            _decimal(
                self._snapshot["maker_fill_fees"],
                name="maker_fill_fees",
            )
            + event.fee
        )
        self._append_receipt(
            "maker_fill_observed",
            {
                "order_id": maker_order_id,
                "fill_id": event.fill_id,
                **observed,
                "maker_filled_quantity": new_quantity,
                "maker_fill_notional": self._snapshot["maker_fill_notional"],
                "maker_fill_fees": self._snapshot["maker_fill_fees"],
            },
        )

    def _apply_reconciliation(
        self,
        reconciliation: ProbeCancelReconciliation,
        *,
        maker_order: ProbeOrder,
        maker_order_id: str,
    ) -> None:
        if reconciliation.maker_order_id != maker_order_id:
            self._fail_closed("reconciliation_order_mismatch")
        if not reconciliation.user_stream_verified:
            self._fail_closed("user_fill_stream_unverified")
        if reconciliation.open_order_ids:
            self._fail_closed("maker_order_still_open")
        authoritative_ids: set[str] = set()
        for event in reconciliation.fills:
            if event.order_id != maker_order_id or event.fill_id is None:
                self._fail_closed("reconciliation_foreign_fill")
            authoritative_ids.add(event.fill_id)
            self._observe_maker_fill(
                event,
                maker_order=maker_order,
                maker_order_id=maker_order_id,
            )
        if authoritative_ids != set(self._snapshot["seen_fill_ids"]):
            self._fail_closed("fill_reconciliation_mismatch")
        self._append_receipt(
            "cancel_reconciliation_verified",
            {
                "maker_order_id": maker_order_id,
                "fill_ids": sorted(authoritative_ids),
                "captured_at_ms": reconciliation.captured_at_ms,
                "state_sha256": reconciliation.state_sha256,
            },
        )

    @staticmethod
    def _quote_matches_template(
        *,
        template: ProbeOrder,
        quote: ProbeExecutionQuote,
    ) -> bool:
        order = quote.order
        return (
            order.leg_id == template.leg_id
            and order.horizon == template.horizon
            and order.outcome == template.outcome
            and order.market_id == template.market_id
            and order.token_id == template.token_id
            and order.side == template.side
            and order.quantity == template.quantity
        )

    def _hedge_quote_rejection(
        self,
        *,
        candidate: ProbeRouteCandidate,
        maker_filled_quantity: Decimal,
        quote: ProbeExecutionQuote,
        isolated_balance: Decimal,
    ) -> str | None:
        template = candidate.hedge_order.with_quantity(maker_filled_quantity)
        if not self._quote_matches_template(template=template, quote=quote):
            return "fok_quote_identity_mismatch"
        if not quote.full_depth_available:
            return "fok_full_depth_unavailable"
        if not self._quote_is_current(quote):
            return "fok_quote_stale"
        if quote.quoted_notional > quote.order.notional:
            return "fok_quote_exceeds_buy_limit"
        maker_cost = _decimal(
            self._snapshot["maker_fill_notional"],
            name="maker_fill_notional",
        ) + _decimal(self._snapshot["maker_fill_fees"], name="maker_fill_fees")
        all_in_per_pair = (
            maker_cost + quote.quoted_notional + quote.quoted_fee
        ) / maker_filled_quantity
        if all_in_per_pair > MAX_ALL_IN_COST:
            return "actual_all_in_cost_exceeds_0_99"
        cumulative_buy_notional = candidate.maker_order.notional + quote.order.notional
        if cumulative_buy_notional > MAX_BUY_NOTIONAL:
            return "cumulative_buy_notional_exceeds_10"
        if cumulative_buy_notional > isolated_balance:
            return "cumulative_buy_notional_exceeds_isolated_balance"
        return None

    def _validate_unwind_quote(
        self,
        *,
        template: ProbeOrder,
        quote: ProbeExecutionQuote,
    ) -> None:
        if not self._quote_matches_template(template=template, quote=quote):
            self._fail_closed("unwind_quote_identity_mismatch")
        if quote.order.side != "sell":
            self._fail_closed("unwind_must_sell_maker_leg")
        if not quote.full_depth_available:
            self._fail_closed("unwind_full_depth_unavailable")
        if not self._quote_is_current(quote):
            self._fail_closed("unwind_quote_stale")

    def _quote_is_current(self, quote: ProbeExecutionQuote) -> bool:
        now_ms = _integer(self.now_ms(), name="now_ms")
        return (
            quote.source_timestamp_ms <= now_ms
            and quote.receipt_timestamp_ms <= now_ms
            and now_ms - quote.source_timestamp_ms <= MAX_EXECUTION_QUOTE_AGE_MS
            and now_ms - quote.receipt_timestamp_ms <= MAX_EXECUTION_QUOTE_AGE_MS
        )

    def _validate_complete_execution(
        self,
        *,
        result: ProbeSubmitResult,
        quote: ProbeExecutionQuote,
        expected_quantity: Decimal,
        label: str,
    ) -> None:
        if result.filled_quantity != expected_quantity:
            self._fail_closed(f"{label}_fill_quantity_mismatch")
        if (
            quote.order.side == "buy"
            and result.executed_notional > quote.quoted_notional
        ):
            self._fail_closed(f"{label}_execution_worse_than_quote")
        if (
            quote.order.side == "sell"
            and result.executed_notional < quote.quoted_notional
        ):
            self._fail_closed(f"{label}_execution_worse_than_quote")
        if result.fee > quote.quoted_fee:
            self._fail_closed(f"{label}_fee_exceeds_quote")

    def _validate_final_confirmation(
        self,
        plan: ProbePlan,
        confirmation: ProbeFinalConfirmation,
    ) -> None:
        if not isinstance(confirmation, ProbeFinalConfirmation):
            raise TypeError("final_confirmation must be a ProbeFinalConfirmation")
        now_ms = _integer(self.now_ms(), name="now_ms")
        if confirmation.plan_hash != plan.plan_hash:
            raise ValueError("final confirmation does not bind this plan hash")
        if confirmation.accepted_max_loss != MAX_ISOLATED_BALANCE:
            raise ValueError("final confirmation must accept the 12 USDC loss cap")
        if confirmation.acknowledgement != ACCOUNT_LOSS_ACKNOWLEDGEMENT:
            raise ValueError("final confirmation acknowledgement is invalid")
        lifetime = confirmation.expires_at_ms - confirmation.issued_at_ms
        if (
            lifetime <= 0
            or lifetime > FINAL_CONFIRMATION_MAX_LIFETIME_MS
            or confirmation.issued_at_ms > now_ms
            or now_ms >= confirmation.expires_at_ms
        ):
            raise ValueError("final confirmation is stale or not yet valid")
        if self._snapshot["final_confirmation_sha256"] is not None:
            raise ValueError("final confirmation was already consumed")

    def _validate_plan_policy(self, plan: ProbePlan) -> None:
        if plan.strategy_kind != "structure_floor":
            raise ValueError("execution probe only allows structure_floor routes")
        if plan.isolated_balance > MAX_ISOLATED_BALANCE:
            raise ValueError("isolated balance exceeds 12 USDC")
        for candidate in plan.route_candidates:
            if candidate.common_expiry_id != plan.common_expiry_id:
                raise ValueError("probe plan must stay on one common-expiry pair")
        if not any(
            candidate.hedge_depth_available
            and candidate.buy_notional <= MAX_BUY_NOTIONAL
            and candidate.buy_notional <= plan.isolated_balance
            and candidate.total_cost <= MAX_ALL_IN_COST
            for candidate in plan.route_candidates
        ):
            raise ValueError(
                "no route candidate satisfies probe cost and notional caps"
            )

    def _validate_snapshot_state(self) -> None:
        if self._snapshot.get("bound_plan_hash") is not None:
            _sha256_text(
                self._snapshot["bound_plan_hash"],
                name="bound_plan_hash",
            )
        if self._snapshot.get("final_confirmation_sha256") is not None:
            _sha256_text(
                self._snapshot["final_confirmation_sha256"],
                name="final_confirmation_sha256",
            )
        if self._snapshot.get("submission_started") and (
            self._snapshot.get("bound_plan_hash") is None
            or self._snapshot.get("final_confirmation_sha256") is None
        ):
            raise ValueError("submitted snapshot is missing bound confirmations")
        if self._snapshot.get("completed_matched") and self._snapshot.get(
            "completed_flat"
        ):
            raise ValueError("probe snapshot cannot be both matched and flat")
        kill_receipts = [
            receipt
            for receipt in self._receipts
            if receipt.event_type == "kill_switch_engaged"
        ]
        if bool(kill_receipts) is not bool(self._snapshot.get("kill_switch_engaged")):
            raise ValueError("kill-switch snapshot disagrees with its receipt chain")

    def _load_receipts(self, raw_receipts: Any) -> list[ProbeReceipt]:
        receipts: list[ProbeReceipt] = []
        expected_previous: str | None = None
        for index, raw in enumerate(raw_receipts, start=1):
            if not isinstance(raw, Mapping):
                raise TypeError("probe receipt must be a mapping")
            receipt = ProbeReceipt.create(
                sequence=int(raw["sequence"]),
                recorded_at_ms=_integer(
                    raw.get("recorded_at_ms"),
                    name="recorded_at_ms",
                ),
                event_type=str(raw["event_type"]),
                body=dict(raw["body"]),
                previous_receipt_sha256=raw.get("previous_receipt_sha256"),
            )
            if receipt.sequence != index:
                raise ValueError("probe receipt sequence is not contiguous")
            if receipt.previous_receipt_sha256 != expected_previous:
                raise ValueError("probe receipt chain anchor mismatch")
            if raw.get("receipt_sha256") != receipt.receipt_sha256:
                raise ValueError("probe receipt hash mismatch")
            receipts.append(receipt)
            expected_previous = receipt.receipt_sha256
        return receipts

    def _append_receipt(self, event_type: str, body: Mapping[str, Any]) -> ProbeReceipt:
        previous = self._receipts[-1].receipt_sha256 if self._receipts else None
        receipt = ProbeReceipt.create(
            sequence=len(self._receipts) + 1,
            recorded_at_ms=_integer(self.now_ms(), name="now_ms"),
            event_type=event_type,
            body=body,
            previous_receipt_sha256=previous,
        )
        self._receipts.append(receipt)
        self._snapshot["receipts"] = [entry.to_document() for entry in self._receipts]
        self._persist()
        return receipt

    def _persist(self) -> None:
        self.persistence.save(self._snapshot)

    def _ensure_not_killed(self) -> None:
        if self.kill_switch_engaged:
            raise ProbeKillSwitchError(
                f"persistent kill switch engaged: {self._snapshot['kill_reason'] or 'killed'}"
            )

    def _fail_closed(self, reason: str) -> None:
        self._engage_kill(reason)
        raise ProbeKillSwitchError(reason)

    def _engage_kill(self, reason: str) -> None:
        cancel_all_ok = False
        try:
            cancel_all_ok = bool(self.venue.cancel_all(reason=f"kill:{reason}"))
        except Exception:  # noqa: BLE001 - kill must persist even if cancel fails
            cancel_all_ok = False
        self._snapshot["kill_switch_engaged"] = True
        self._snapshot["kill_reason"] = reason
        self._append_receipt(
            "kill_switch_engaged",
            {"reason": reason, "cancel_all_ok": cancel_all_ok},
        )
