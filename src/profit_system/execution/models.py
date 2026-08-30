from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from .errors import OrderValidationError

ZERO = Decimal("0")
ONE = Decimal("1")


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def to_decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise OrderValidationError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise OrderValidationError(f"{name} must be finite")
    return result


def to_positive_decimal(value: Any, *, name: str) -> Decimal:
    result = to_decimal(value, name=name)
    if result <= ZERO:
        raise OrderValidationError(f"{name} must be positive")
    return result


def to_probability(value: Any, *, name: str) -> Decimal:
    result = to_decimal(value, name=name)
    if not ZERO < result <= ONE:
        raise OrderValidationError(f"{name} must be in (0, 1]")
    return result


def non_empty(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrderValidationError(f"{name} must be a non-empty string")
    return value


def to_canonical_json(payload: Mapping[str, Any]) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, Decimal):
            return decimal_text(value)
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in sorted(value.items())}
        if isinstance(value, tuple | list):
            return [normalize(item) for item in value]
        if isinstance(value, set | frozenset):
            return [normalize(item) for item in sorted(value)]
        if isinstance(value, StrEnum):
            return value.value
        return value

    return json.dumps(normalize(payload), separators=(",", ":"), sort_keys=True)


def sha256_hex(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(to_canonical_json(payload).encode("utf-8")).hexdigest()


def hmac_sha256_hex(secret: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(
        secret,
        to_canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class ExecutionMode(StrEnum):
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE_CANARY = "LIVE_CANARY"
    LIVE_LIMITED = "LIVE_LIMITED"
    KILLED = "KILLED"


class QualificationLevel(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    SHADOW_ONLY = "SHADOW_ONLY"
    PROBE_ALLOWED = "PROBE_ALLOWED"
    LIVE_ALLOWED = "LIVE_ALLOWED"
    STOPPED = "STOPPED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(StrEnum):
    GTC = "GTC"
    GTD = "GTD"
    FAK = "FAK"
    FOK = "FOK"


class OrderLifecycleStatus(StrEnum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    MATCHED = "MATCHED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILING = "RECONCILING"
    REJECTED = "REJECTED"
    ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    KILLED = "KILLED"


class VenueEventKind(StrEnum):
    ORDER = "ORDER"
    TRADE = "TRADE"
    HEARTBEAT = "HEARTBEAT"
    GAP = "GAP"
    BACKFILL = "BACKFILL"
    STATUS = "STATUS"


class InterventionKind(StrEnum):
    CANCEL = "CANCEL"
    CANCEL_ALL = "CANCEL_ALL"
    REPLACE = "REPLACE"
    RECONCILE = "RECONCILE"
    KILL = "KILL"


ALLOWED_TRANSITIONS: dict[OrderLifecycleStatus, frozenset[OrderLifecycleStatus]] = {
    OrderLifecycleStatus.DRAFT: frozenset(
        {OrderLifecycleStatus.REVIEWED, OrderLifecycleStatus.KILLED}
    ),
    OrderLifecycleStatus.REVIEWED: frozenset(
        {OrderLifecycleStatus.APPROVED, OrderLifecycleStatus.KILLED}
    ),
    OrderLifecycleStatus.APPROVED: frozenset(
        {OrderLifecycleStatus.SUBMITTING, OrderLifecycleStatus.KILLED}
    ),
    OrderLifecycleStatus.SUBMITTING: frozenset(
        {
            OrderLifecycleStatus.LIVE,
            OrderLifecycleStatus.DELAYED,
            OrderLifecycleStatus.MATCHED,
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.REJECTED,
            OrderLifecycleStatus.AMBIGUOUS,
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.ATTENTION_REQUIRED,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.LIVE: frozenset(
        {
            OrderLifecycleStatus.MATCHED,
            OrderLifecycleStatus.PARTIALLY_FILLED,
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.CANCEL_REQUESTED,
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.DELAYED: frozenset(
        {
            OrderLifecycleStatus.LIVE,
            OrderLifecycleStatus.MATCHED,
            OrderLifecycleStatus.PARTIALLY_FILLED,
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.CANCEL_REQUESTED,
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.MATCHED: frozenset(
        {
            OrderLifecycleStatus.PARTIALLY_FILLED,
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.CANCEL_REQUESTED,
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.FILLED: frozenset({OrderLifecycleStatus.KILLED}),
    OrderLifecycleStatus.AMBIGUOUS: frozenset(
        {
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.LIVE,
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.REJECTED,
            OrderLifecycleStatus.ATTENTION_REQUIRED,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.RECONCILING: frozenset(
        {
            OrderLifecycleStatus.LIVE,
            OrderLifecycleStatus.PARTIALLY_FILLED,
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.CANCELED,
            OrderLifecycleStatus.REJECTED,
            OrderLifecycleStatus.ATTENTION_REQUIRED,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.REJECTED: frozenset({OrderLifecycleStatus.KILLED}),
    OrderLifecycleStatus.ATTENTION_REQUIRED: frozenset(
        {
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.CANCEL_REQUESTED,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.CANCEL_REQUESTED: frozenset(
        {
            OrderLifecycleStatus.CANCELED,
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.CANCELED: frozenset(
        {
            OrderLifecycleStatus.PARTIALLY_FILLED,
            OrderLifecycleStatus.FILLED,
            OrderLifecycleStatus.ATTENTION_REQUIRED,
            OrderLifecycleStatus.KILLED,
        }
    ),
    OrderLifecycleStatus.KILLED: frozenset(),
}


LIVEISH_STATUSES = frozenset(
    {
        OrderLifecycleStatus.LIVE,
        OrderLifecycleStatus.DELAYED,
        OrderLifecycleStatus.MATCHED,
        OrderLifecycleStatus.PARTIALLY_FILLED,
        OrderLifecycleStatus.CANCEL_REQUESTED,
        OrderLifecycleStatus.RECONCILING,
        OrderLifecycleStatus.ATTENTION_REQUIRED,
        OrderLifecycleStatus.AMBIGUOUS,
        OrderLifecycleStatus.SUBMITTING,
        OrderLifecycleStatus.APPROVED,
    }
)


def ensure_transition(
    current: OrderLifecycleStatus,
    target: OrderLifecycleStatus,
) -> None:
    if current == target:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise OrderValidationError(
            f"invalid order lifecycle transition: {current.value} -> {target.value}"
        )


def qualification_rank(level: QualificationLevel) -> int:
    return {
        QualificationLevel.RESEARCH_ONLY: 0,
        QualificationLevel.SHADOW_ONLY: 1,
        QualificationLevel.PROBE_ALLOWED: 2,
        QualificationLevel.LIVE_ALLOWED: 3,
        QualificationLevel.STOPPED: -1,
    }[level]


def qualification_allows_mode(
    qualification: QualificationLevel,
    mode: ExecutionMode,
) -> bool:
    if qualification is QualificationLevel.STOPPED:
        return False
    if mode in {ExecutionMode.REPLAY, ExecutionMode.PAPER}:
        return True
    if mode is ExecutionMode.SHADOW:
        return qualification_rank(qualification) >= qualification_rank(
            QualificationLevel.SHADOW_ONLY
        )
    if mode is ExecutionMode.LIVE_CANARY:
        return qualification_rank(qualification) >= qualification_rank(
            QualificationLevel.PROBE_ALLOWED
        )
    if mode is ExecutionMode.LIVE_LIMITED:
        return qualification_rank(qualification) >= qualification_rank(
            QualificationLevel.LIVE_ALLOWED
        )
    return False


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_market_exposure: Decimal
    max_event_exposure: Decimal
    max_strategy_exposure: Decimal
    max_total_exposure: Decimal
    max_daily_loss: Decimal
    max_drawdown: Decimal
    max_open_orders: int
    max_unmatched_leg_seconds: int
    max_market_data_age_ms: int
    max_user_stream_gap_seconds: int
    max_order_rate: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_order_notional",
            "max_market_exposure",
            "max_event_exposure",
            "max_strategy_exposure",
            "max_total_exposure",
            "max_daily_loss",
            "max_drawdown",
        ):
            object.__setattr__(
                self,
                field_name,
                to_positive_decimal(getattr(self, field_name), name=field_name),
            )
        for field_name in (
            "max_open_orders",
            "max_unmatched_leg_seconds",
            "max_market_data_age_ms",
            "max_user_stream_gap_seconds",
            "max_order_rate",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OrderValidationError(f"{field_name} must be a positive integer")


@dataclass(frozen=True)
class QualificationRecord:
    strategy_id: str
    qualification: QualificationLevel
    allowed_modes: tuple[ExecutionMode, ...]
    market_whitelist: tuple[str, ...]
    risk_limits: RiskLimits
    evidence_digest: str
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", non_empty(self.strategy_id, name="strategy_id"))
        if not self.allowed_modes:
            raise OrderValidationError("allowed_modes must be non-empty")
        object.__setattr__(
            self,
            "market_whitelist",
            tuple(
                non_empty(value, name="market_whitelist value") for value in self.market_whitelist
            ),
        )
        object.__setattr__(
            self, "evidence_digest", non_empty(self.evidence_digest, name="evidence_digest")
        )


@dataclass(frozen=True)
class RiskContext:
    market_exposure: Mapping[str, Decimal] = field(default_factory=dict)
    event_exposure: Mapping[str, Decimal] = field(default_factory=dict)
    strategy_exposure: Mapping[str, Decimal] = field(default_factory=dict)
    total_exposure: Decimal = ZERO
    realized_daily_loss: Decimal = ZERO
    drawdown: Decimal = ZERO
    open_order_count: int = 0
    market_data_age_ms: int = 0

    def __post_init__(self) -> None:
        for field_name in ("total_exposure", "realized_daily_loss", "drawdown"):
            object.__setattr__(
                self,
                field_name,
                to_decimal(getattr(self, field_name), name=field_name),
            )
        for field_name in ("open_order_count", "market_data_age_ms"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OrderValidationError(f"{field_name} must be a non-negative integer")
        for mapping_name in ("market_exposure", "event_exposure", "strategy_exposure"):
            mapping = getattr(self, mapping_name)
            object.__setattr__(
                self,
                mapping_name,
                {
                    str(key): to_decimal(value, name=f"{mapping_name}.{key}")
                    for key, value in mapping.items()
                },
            )


@dataclass(frozen=True)
class VenueOrderSpec:
    client_order_id: str
    strategy_id: str
    market_id: str
    token_id: str
    event_id: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    time_in_force: TimeInForce
    post_only: bool = False
    expires_at: datetime | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "client_order_id", non_empty(self.client_order_id, name="client_order_id")
        )
        object.__setattr__(self, "strategy_id", non_empty(self.strategy_id, name="strategy_id"))
        object.__setattr__(self, "market_id", non_empty(self.market_id, name="market_id"))
        object.__setattr__(self, "token_id", non_empty(self.token_id, name="token_id"))
        object.__setattr__(self, "event_id", non_empty(self.event_id, name="event_id"))
        object.__setattr__(self, "quantity", to_positive_decimal(self.quantity, name="quantity"))
        object.__setattr__(
            self, "limit_price", to_probability(self.limit_price, name="limit_price")
        )
        if self.time_in_force in {TimeInForce.FAK, TimeInForce.FOK} and self.post_only:
            raise OrderValidationError("FAK/FOK orders cannot be post_only")
        if self.time_in_force is TimeInForce.GTD and self.expires_at is None:
            raise OrderValidationError("GTD orders require expires_at")
        if self.time_in_force is not TimeInForce.GTD and self.expires_at is not None:
            raise OrderValidationError("only GTD orders may set expires_at")
        if not isinstance(self.post_only, bool):
            raise OrderValidationError("post_only must be bool")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.limit_price

    def to_document(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "strategy_id": self.strategy_id,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "event_id": self.event_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "time_in_force": self.time_in_force.value,
            "post_only": self.post_only,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ExecutionIntent:
    strategy_id: str
    mode: ExecutionMode
    orders: tuple[VenueOrderSpec, ...]
    risk_context: RiskContext = field(default_factory=RiskContext)
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", non_empty(self.strategy_id, name="strategy_id"))
        if not self.orders:
            raise OrderValidationError("execution intent requires at least one order")
        strategy_ids = {order.strategy_id for order in self.orders}
        if strategy_ids != {self.strategy_id}:
            raise OrderValidationError("all orders must share the intent strategy_id")

    def to_document(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "mode": self.mode.value,
            "orders": [order.to_document() for order in self.orders],
            "note": self.note,
        }


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass(frozen=True)
class VenuePreflightRequest:
    mode: ExecutionMode
    qualification: QualificationRecord
    orders: tuple[VenueOrderSpec, ...]
    risk_context: RiskContext
    idempotency_key: str
    now: datetime


@dataclass(frozen=True)
class VenuePreflightResult:
    ok: bool
    checks: tuple[PreflightCheck, ...]
    as_of: datetime
    close_only: bool = False
    geoblocked: bool = False
    allowance_available: Decimal = ZERO
    cash_available: Decimal = ZERO
    retry_after_seconds: int | None = None

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if check.blocking and not check.ok)


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    venue_order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    fee: Decimal
    occurred_at: datetime
    raw_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", non_empty(self.fill_id, name="fill_id"))
        object.__setattr__(
            self, "venue_order_id", non_empty(self.venue_order_id, name="venue_order_id")
        )
        object.__setattr__(self, "market_id", non_empty(self.market_id, name="market_id"))
        object.__setattr__(self, "token_id", non_empty(self.token_id, name="token_id"))
        object.__setattr__(self, "price", to_probability(self.price, name="price"))
        object.__setattr__(self, "quantity", to_positive_decimal(self.quantity, name="quantity"))
        object.__setattr__(self, "fee", to_decimal(self.fee, name="fee"))
        object.__setattr__(self, "raw_status", non_empty(self.raw_status, name="raw_status"))


@dataclass(frozen=True)
class VenueOrderState:
    venue_order_id: str
    client_order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    filled_quantity: Decimal
    status: OrderLifecycleStatus
    time_in_force: TimeInForce
    post_only: bool = False
    expires_at: datetime | None = None
    reject_reason: str | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "venue_order_id", non_empty(self.venue_order_id, name="venue_order_id")
        )
        object.__setattr__(
            self, "client_order_id", non_empty(self.client_order_id, name="client_order_id")
        )
        object.__setattr__(self, "market_id", non_empty(self.market_id, name="market_id"))
        object.__setattr__(self, "token_id", non_empty(self.token_id, name="token_id"))
        object.__setattr__(self, "quantity", to_positive_decimal(self.quantity, name="quantity"))
        object.__setattr__(
            self, "limit_price", to_probability(self.limit_price, name="limit_price")
        )
        object.__setattr__(
            self, "filled_quantity", to_decimal(self.filled_quantity, name="filled_quantity")
        )
        if self.filled_quantity < ZERO or self.filled_quantity > self.quantity:
            raise OrderValidationError("filled_quantity must be within [0, quantity]")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    def to_document(self) -> dict[str, Any]:
        return {
            "venue_order_id": self.venue_order_id,
            "client_order_id": self.client_order_id,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "filled_quantity": self.filled_quantity,
            "status": self.status.value,
            "time_in_force": self.time_in_force.value,
            "post_only": self.post_only,
            "expires_at": self.expires_at,
            "reject_reason": self.reject_reason,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class VenueSubmitResult:
    client_order_id: str
    status: OrderLifecycleStatus
    venue_order_id: str | None = None
    filled_quantity: Decimal = ZERO
    reject_reason: str | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "client_order_id", non_empty(self.client_order_id, name="client_order_id")
        )
        if self.venue_order_id is not None:
            object.__setattr__(
                self, "venue_order_id", non_empty(self.venue_order_id, name="venue_order_id")
            )
        object.__setattr__(
            self, "filled_quantity", to_decimal(self.filled_quantity, name="filled_quantity")
        )


@dataclass(frozen=True)
class VenueSubmitBatch:
    results: tuple[VenueSubmitResult, ...]
    submitted_at: datetime


@dataclass(frozen=True)
class VenueCancelResult:
    canceled_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    canceled_at: datetime
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class VenueReconciliation:
    open_orders: tuple[VenueOrderState, ...]
    fills: tuple[FillRecord, ...]
    as_of: datetime
    cash_balance: Decimal = ZERO
    allowance_available: Decimal = ZERO
    close_only: bool = False
    geoblocked: bool = False
    backfill_complete: bool = True
    discrepancies: tuple[str, ...] = ()


@dataclass(frozen=True)
class VenueUserEvent:
    kind: VenueEventKind
    occurred_at: datetime
    cursor: str
    order: VenueOrderState | None = None
    fill: FillRecord | None = None
    gap_seconds: int | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cursor", non_empty(self.cursor, name="cursor"))
        if self.gap_seconds is not None and (
            isinstance(self.gap_seconds, bool)
            or not isinstance(self.gap_seconds, int)
            or self.gap_seconds < 0
        ):
            raise OrderValidationError("gap_seconds must be a non-negative integer")


@dataclass(frozen=True)
class ReleasePermit:
    version: str
    strategy_id: str
    qualification: QualificationLevel
    mode: ExecutionMode
    plan_id: str
    plan_hash: str
    confirmation_phrase: str
    canary_checks: Mapping[str, bool]
    evidence_digest: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", non_empty(self.version, name="version"))
        object.__setattr__(self, "strategy_id", non_empty(self.strategy_id, name="strategy_id"))
        object.__setattr__(self, "plan_id", non_empty(self.plan_id, name="plan_id"))
        object.__setattr__(self, "plan_hash", non_empty(self.plan_hash, name="plan_hash"))
        object.__setattr__(
            self,
            "confirmation_phrase",
            non_empty(self.confirmation_phrase, name="confirmation_phrase"),
        )
        object.__setattr__(
            self, "evidence_digest", non_empty(self.evidence_digest, name="evidence_digest")
        )
        object.__setattr__(self, "nonce", non_empty(self.nonce, name="nonce"))
        object.__setattr__(self, "signature", non_empty(self.signature, name="signature"))
        normalized_checks: dict[str, bool] = {}
        for key, value in self.canary_checks.items():
            if not isinstance(value, bool):
                raise OrderValidationError("canary_checks values must be explicit booleans")
            normalized_checks[str(key)] = value
        object.__setattr__(self, "canary_checks", normalized_checks)

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "strategy_id": self.strategy_id,
            "qualification": self.qualification.value,
            "mode": self.mode.value,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "confirmation_phrase": self.confirmation_phrase,
            "canary_checks": dict(self.canary_checks),
            "evidence_digest": self.evidence_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }


@dataclass(frozen=True)
class ExecutionApproval:
    actor: str
    idempotency_key: str
    plan_hash: str
    confirmation_phrase: str
    release_permit: ReleasePermit | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor", non_empty(self.actor, name="actor"))
        object.__setattr__(
            self, "idempotency_key", non_empty(self.idempotency_key, name="idempotency_key")
        )
        object.__setattr__(self, "plan_hash", non_empty(self.plan_hash, name="plan_hash"))
        object.__setattr__(
            self,
            "confirmation_phrase",
            non_empty(self.confirmation_phrase, name="confirmation_phrase"),
        )


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    plan_hash: str
    created_at: datetime
    actor: str
    intent: ExecutionIntent
    qualification: QualificationRecord
    preflight: VenuePreflightResult
    confirmation_phrase: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", non_empty(self.plan_id, name="plan_id"))
        object.__setattr__(self, "plan_hash", non_empty(self.plan_hash, name="plan_hash"))
        object.__setattr__(self, "actor", non_empty(self.actor, name="actor"))
        object.__setattr__(
            self,
            "confirmation_phrase",
            non_empty(self.confirmation_phrase, name="confirmation_phrase"),
        )

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        actor: str,
        intent: ExecutionIntent,
        qualification: QualificationRecord,
        preflight: VenuePreflightResult,
        created_at: datetime,
    ) -> ExecutionPlan:
        payload = {
            "plan_id": plan_id,
            "actor": actor,
            "intent": intent.to_document(),
            "qualification": qualification.strategy_id,
            "preflight_ok": preflight.ok,
            "created_at": created_at,
        }
        plan_hash = sha256_hex(payload)
        return cls(
            plan_id=plan_id,
            plan_hash=plan_hash,
            created_at=created_at,
            actor=actor,
            intent=intent,
            qualification=qualification,
            preflight=preflight,
            confirmation_phrase=f"CONFIRM {plan_id} {plan_hash}",
        )


@dataclass(frozen=True)
class OrderRecord:
    client_order_id: str
    spec: VenueOrderSpec
    status: OrderLifecycleStatus
    plan_id: str
    venue_order_id: str | None = None
    parent_order_id: str | None = None
    fills: tuple[FillRecord, ...] = ()
    reject_reason: str | None = None
    submitted_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def transition(self, status: OrderLifecycleStatus, *, updated_at: datetime) -> OrderRecord:
        ensure_transition(self.status, status)
        return replace(self, status=status, updated_at=updated_at)

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), ZERO)

    @property
    def notional(self) -> Decimal:
        return self.spec.notional

    def with_fill(self, fill: FillRecord) -> OrderRecord:
        if fill.fill_id in {existing.fill_id for existing in self.fills}:
            return self
        fills = self.fills + (fill,)
        filled_quantity = sum((item.quantity for item in fills), ZERO)
        if filled_quantity >= self.spec.quantity:
            status = OrderLifecycleStatus.FILLED
        elif filled_quantity > ZERO:
            status = OrderLifecycleStatus.PARTIALLY_FILLED
        else:
            status = self.status
        if status is not self.status and self.status not in {
            OrderLifecycleStatus.CANCELED,
            OrderLifecycleStatus.CANCEL_REQUESTED,
            OrderLifecycleStatus.RECONCILING,
            OrderLifecycleStatus.ATTENTION_REQUIRED,
            OrderLifecycleStatus.AMBIGUOUS,
        }:
            ensure_transition(self.status, status)
        return replace(
            self,
            fills=fills,
            status=status,
            updated_at=max(self.updated_at, fill.occurred_at),
        )

    def with_venue_state(self, state: VenueOrderState) -> OrderRecord:
        if self.venue_order_id is None:
            venue_order_id = state.venue_order_id
        else:
            venue_order_id = self.venue_order_id
        current = self.status
        target = state.status
        if current != target:
            ensure_transition(current, target)
        return replace(
            self,
            venue_order_id=venue_order_id,
            status=target,
            reject_reason=state.reject_reason,
            updated_at=state.updated_at,
        )


@dataclass(frozen=True)
class ExecutionTicket:
    plan_id: str
    plan_hash: str
    approved_at: datetime
    orders: tuple[OrderRecord, ...]
    results: tuple[VenueSubmitResult, ...]


@dataclass(frozen=True)
class InterventionAction:
    kind: InterventionKind
    order_ids: tuple[str, ...] = ()
    replacement_orders: tuple[VenueOrderSpec, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class InterventionResult:
    action: InterventionKind
    completed_at: datetime
    orders: tuple[OrderRecord, ...]
    canceled_ids: tuple[str, ...] = ()
    remaining_ids: tuple[str, ...] = ()
    reconciliation: VenueReconciliation | None = None


@dataclass(frozen=True)
class ExecutionSnapshot:
    mode: ExecutionMode
    killed: bool
    blocked_reasons: tuple[str, ...]
    last_reconcile_at: datetime | None
    last_user_event_at: datetime | None
    last_heartbeat_at: datetime | None
    user_stream_backfill_complete: bool
    plans: tuple[ExecutionPlan, ...]
    orders: tuple[OrderRecord, ...]
    unresolved_order_ids: tuple[str, ...]
