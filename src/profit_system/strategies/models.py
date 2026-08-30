from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Any, Protocol, cast

from src.edge_lab.data_store import canonical_json_bytes
from src.profit_system.types import OpportunityCandidate

ZERO = Decimal("0")
ONE = Decimal("1")
_MISSING = object()


def _require_decimal(value: Any, *, label: str, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise TypeError(f"{label} must be a Decimal-compatible value")
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{label} must be Decimal-compatible") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return result


def _require_int(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return int(value)


def _require_non_empty(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return value


def _require_timestamp_ms(value: Any, *, label: str) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must include a timezone")
        utc_value = value.astimezone(UTC)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        delta = utc_value - epoch
        return ((delta.days * 86_400) + delta.seconds) * 1000 + (delta.microseconds // 1000)
    return _require_int(value, label=label, minimum=0)


def _read_field(candidate: Any, *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if isinstance(candidate, Mapping) and name in candidate:
            return candidate[name]
        if hasattr(candidate, name):
            return getattr(candidate, name)
    if default is not _MISSING:
        return default
    joined = ", ".join(names)
    raise ValueError(f"missing required opportunity field: {joined}")


def _read_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (_require_non_empty(value, label=label),)
    if not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence")
    return tuple(_require_non_empty(item, label=label) for item in value)


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _read_decimal_or_default(
    candidate: Any,
    *names: str,
    label: str,
    minimum: Decimal | None = None,
    default: Any = _MISSING,
    none_is_missing: bool = False,
) -> Decimal:
    value = _read_field(candidate, *names, default=default)
    if value is None and none_is_missing:
        value = _MISSING
    if value is _MISSING:
        raise ValueError(f"{label} is required to normalize opportunity economics")
    return _require_decimal(value, label=label, minimum=minimum)


def _infer_opportunity_kind(strategy_id: str) -> str:
    normalized = strategy_id.lower()
    if "maker" in normalized:
        return "maker"
    if "complete_set" in normalized:
        return "complete_set"
    return "arbitrage"


def _normalize_evidence_ref(value: Any, *, index: int) -> str:
    if isinstance(value, str):
        return _require_non_empty(value, label=f"evidence_refs[{index}]")
    kind = _read_field(value, "kind")
    ref = _read_field(value, "ref")
    return (
        f"{_require_non_empty(kind, label=f'evidence_refs[{index}].kind')}://"
        f"{_require_non_empty(ref, label=f'evidence_refs[{index}].ref')}"
    )


def _normalize_evidence_refs(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError("evidence_refs must be a sequence")
    if not isinstance(value, Sequence):
        raise TypeError("evidence_refs must be a sequence")
    return tuple(_normalize_evidence_ref(item, index=index) for index, item in enumerate(value))


def _read_metadata(candidate: Any) -> dict[str, Any]:
    metadata = dict(_read_field(candidate, "metadata", default={}))
    if isinstance(candidate, OpportunityCandidate):
        metadata.setdefault("source_status", candidate.status.value)
        metadata.setdefault("source_executable", candidate.executable)
        metadata.setdefault("source_snapshot_hash", candidate.snapshot_hash)
        metadata.setdefault("source_config_hash", candidate.config_hash)
        metadata.setdefault("source_code_version_hash", candidate.code_version_hash)
        metadata.setdefault("source_lower_confidence_margin", candidate.lower_confidence_margin)
        metadata.setdefault("source_rejection_reasons", tuple(candidate.rejection_reasons))
    return metadata


class RuntimeMode(StrEnum):
    RESEARCH = "RESEARCH"
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE_CANARY = "LIVE_CANARY"
    LIVE_LIMITED = "LIVE_LIMITED"
    KILLED = "KILLED"


class DecisionStatus(StrEnum):
    EXECUTABLE = "EXECUTABLE"
    REVIEW = "REVIEW"
    NO_GO = "NO_GO"


@dataclass(frozen=True)
class OrderLeg:
    leg_id: str
    market_id: str
    token_id: str
    event_id: str
    side: str
    price: Decimal
    quantity: Decimal
    order_type: str = "limit"
    post_only: bool = False
    time_in_force: str = "GTD"
    expires_at_ms: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "leg_id", _require_non_empty(self.leg_id, label="leg_id"))
        object.__setattr__(self, "market_id", _require_non_empty(self.market_id, label="market_id"))
        object.__setattr__(self, "token_id", _require_non_empty(self.token_id, label="token_id"))
        object.__setattr__(self, "event_id", _require_non_empty(self.event_id, label="event_id"))
        side = _require_non_empty(self.side, label="side").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "price", _require_decimal(self.price, label="price", minimum=ZERO))
        object.__setattr__(
            self, "quantity", _require_decimal(self.quantity, label="quantity", minimum=ZERO)
        )
        object.__setattr__(
            self, "order_type", _require_non_empty(self.order_type, label="order_type")
        )
        object.__setattr__(
            self,
            "time_in_force",
            _require_non_empty(self.time_in_force, label="time_in_force").upper(),
        )
        if self.expires_at_ms is not None:
            object.__setattr__(
                self,
                "expires_at_ms",
                _require_int(self.expires_at_ms, label="expires_at_ms", minimum=0),
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    def to_document(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _serialize(
                {
                    "leg_id": self.leg_id,
                    "market_id": self.market_id,
                    "token_id": self.token_id,
                    "event_id": self.event_id,
                    "side": self.side,
                    "price": self.price,
                    "quantity": self.quantity,
                    "order_type": self.order_type,
                    "post_only": self.post_only,
                    "time_in_force": self.time_in_force,
                    "expires_at_ms": self.expires_at_ms,
                    "metadata": dict(self.metadata),
                }
            ),
        )


@dataclass(frozen=True)
class NormalizedOpportunity:
    opportunity_id: str
    strategy_id: str
    opportunity_kind: str
    detected_at_ms: int
    expires_at_ms: int
    market_ids: tuple[str, ...]
    token_ids: tuple[str, ...]
    event_id: str
    recommended_legs: tuple[OrderLeg, ...]
    expected_gross_edge: Decimal
    expected_fees: Decimal
    expected_slippage: Decimal
    expected_adverse_selection: Decimal
    expected_inventory_cost: Decimal
    expected_incentive: Decimal
    expected_unwind_cost: Decimal
    uncertainty_buffer: Decimal
    expected_net_edge: Decimal
    tradable_edge: Decimal
    max_loss: Decimal
    estimated_capacity: Decimal
    confidence: Decimal
    evidence_refs: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "opportunity_id", _require_non_empty(self.opportunity_id, label="opportunity_id")
        )
        object.__setattr__(
            self, "strategy_id", _require_non_empty(self.strategy_id, label="strategy_id")
        )
        object.__setattr__(
            self,
            "opportunity_kind",
            _require_non_empty(self.opportunity_kind, label="opportunity_kind"),
        )
        object.__setattr__(
            self,
            "detected_at_ms",
            _require_int(self.detected_at_ms, label="detected_at_ms", minimum=0),
        )
        object.__setattr__(
            self,
            "expires_at_ms",
            _require_int(self.expires_at_ms, label="expires_at_ms", minimum=0),
        )
        if self.expires_at_ms < self.detected_at_ms:
            raise ValueError("expires_at_ms must be >= detected_at_ms")
        object.__setattr__(self, "market_ids", tuple(self.market_ids))
        object.__setattr__(self, "token_ids", tuple(self.token_ids))
        object.__setattr__(self, "event_id", _require_non_empty(self.event_id, label="event_id"))
        object.__setattr__(self, "recommended_legs", tuple(self.recommended_legs))
        for field_name in (
            "expected_gross_edge",
            "expected_fees",
            "expected_slippage",
            "expected_adverse_selection",
            "expected_inventory_cost",
            "expected_incentive",
            "expected_unwind_cost",
            "uncertainty_buffer",
            "expected_net_edge",
            "tradable_edge",
            "max_loss",
            "estimated_capacity",
            "confidence",
        ):
            value = getattr(self, field_name)
            minimum = (
                ZERO
                if field_name != "expected_net_edge" and field_name != "tradable_edge"
                else None
            )
            object.__setattr__(
                self, field_name, _require_decimal(value, label=field_name, minimum=minimum)
            )
        if not ZERO <= self.confidence <= ONE:
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "rejection_reasons", tuple(self.rejection_reasons))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def ttl_ms(self) -> int:
        return self.expires_at_ms - self.detected_at_ms

    def remaining_ttl_ms(self, *, as_of_ms: int | None = None) -> int:
        reference = (
            self.detected_at_ms
            if as_of_ms is None
            else _require_int(as_of_ms, label="as_of_ms", minimum=0)
        )
        return max(self.expires_at_ms - reference, 0)

    def to_document(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _serialize(
                {
                    "opportunity_id": self.opportunity_id,
                    "strategy_id": self.strategy_id,
                    "opportunity_kind": self.opportunity_kind,
                    "detected_at_ms": self.detected_at_ms,
                    "expires_at_ms": self.expires_at_ms,
                    "market_ids": list(self.market_ids),
                    "token_ids": list(self.token_ids),
                    "event_id": self.event_id,
                    "recommended_legs": [leg.to_document() for leg in self.recommended_legs],
                    "expected_gross_edge": self.expected_gross_edge,
                    "expected_fees": self.expected_fees,
                    "expected_slippage": self.expected_slippage,
                    "expected_adverse_selection": self.expected_adverse_selection,
                    "expected_inventory_cost": self.expected_inventory_cost,
                    "expected_incentive": self.expected_incentive,
                    "expected_unwind_cost": self.expected_unwind_cost,
                    "uncertainty_buffer": self.uncertainty_buffer,
                    "expected_net_edge": self.expected_net_edge,
                    "tradable_edge": self.tradable_edge,
                    "max_loss": self.max_loss,
                    "estimated_capacity": self.estimated_capacity,
                    "confidence": self.confidence,
                    "evidence_refs": list(self.evidence_refs),
                    "rejection_reasons": list(self.rejection_reasons),
                    "metadata": dict(self.metadata),
                }
            ),
        )

    @classmethod
    def from_candidate(
        cls, candidate: NormalizedOpportunity | OpportunityCandidate | Mapping[str, Any]
    ) -> NormalizedOpportunity:
        if isinstance(candidate, cls):
            return candidate
        expected_net_edge = _read_field(candidate, "expected_net_edge", default=_MISSING)
        expected_gross_edge = _require_decimal(
            _read_field(candidate, "expected_gross_edge"), label="expected_gross_edge"
        )
        expected_taker_fee = _read_field(candidate, "expected_taker_fee", default=None)
        expected_builder_fee = _read_field(candidate, "expected_builder_fee", default=None)
        expected_fees_raw = _read_field(candidate, "expected_fees", default=_MISSING)
        if expected_fees_raw is _MISSING or expected_fees_raw is None:
            if expected_taker_fee in {_MISSING, None} or expected_builder_fee in {_MISSING, None}:
                raise ValueError("expected_fees is required to normalize opportunity economics")
            expected_fees = _require_decimal(
                expected_taker_fee,
                label="expected_taker_fee",
                minimum=ZERO,
            ) + _require_decimal(
                expected_builder_fee,
                label="expected_builder_fee",
                minimum=ZERO,
            )
        else:
            expected_fees = _require_decimal(
                expected_fees_raw,
                label="expected_fees",
                minimum=ZERO,
            )
        expected_slippage = _read_decimal_or_default(
            candidate,
            "expected_slippage",
            label="expected_slippage",
            minimum=ZERO,
            none_is_missing=True,
        )
        expected_adverse_selection = _read_decimal_or_default(
            candidate,
            "expected_adverse_selection",
            label="expected_adverse_selection",
            minimum=ZERO,
            none_is_missing=True,
        )
        expected_inventory_cost = _read_decimal_or_default(
            candidate,
            "expected_inventory_cost",
            label="expected_inventory_cost",
            minimum=ZERO,
            none_is_missing=True,
        )
        expected_incentive = _read_decimal_or_default(
            candidate,
            "expected_incentive",
            label="expected_incentive",
            minimum=ZERO,
            default=ZERO,
            none_is_missing=isinstance(candidate, OpportunityCandidate),
        )
        expected_unwind_cost = _read_decimal_or_default(
            candidate,
            "expected_unwind_cost",
            label="expected_unwind_cost",
            minimum=ZERO,
            default=ZERO,
            none_is_missing=isinstance(candidate, OpportunityCandidate),
        )
        uncertainty_buffer = _read_decimal_or_default(
            candidate,
            "uncertainty_buffer",
            label="uncertainty_buffer",
            minimum=ZERO,
            default=ZERO,
            none_is_missing=isinstance(candidate, OpportunityCandidate),
        )
        if expected_net_edge is _MISSING or expected_net_edge is None:
            expected_net = (
                expected_gross_edge
                + expected_incentive
                - expected_fees
                - expected_slippage
                - expected_adverse_selection
                - expected_inventory_cost
                - expected_unwind_cost
                - uncertainty_buffer
            )
        else:
            expected_net = _require_decimal(expected_net_edge, label="expected_net_edge")
        lower_confidence_margin = _require_decimal(
            _read_field(candidate, "lower_confidence_margin", default=ZERO),
            label="lower_confidence_margin",
            minimum=ZERO,
        )
        tradable_edge = _require_decimal(
            _read_field(
                candidate,
                "tradable_edge",
                default=expected_net - lower_confidence_margin,
            ),
            label="tradable_edge",
        )
        raw_legs = _read_field(candidate, "recommended_legs")
        if not isinstance(raw_legs, Sequence) or isinstance(raw_legs, (str, bytes)):
            raise TypeError("recommended_legs must be a sequence")
        event_id = _read_field(
            candidate,
            "event_id",
            default=_read_field(candidate, "opportunity_id"),
        )
        legs = tuple(
            leg
            if isinstance(leg, OrderLeg)
            else OrderLeg(
                leg_id=_read_field(leg, "leg_id", default=f"leg-{index + 1}"),
                market_id=_read_field(leg, "market_id"),
                token_id=_read_field(leg, "token_id"),
                event_id=_read_field(leg, "event_id", default=event_id),
                side=_enum_value(_read_field(leg, "side")),
                price=_require_decimal(
                    _read_field(leg, "price"),
                    label=f"recommended_legs[{index}].price",
                    minimum=ZERO,
                ),
                quantity=_require_decimal(
                    _read_field(leg, "quantity", "size"),
                    label=f"recommended_legs[{index}].quantity",
                    minimum=ZERO,
                ),
                order_type=_require_non_empty(
                    str(_enum_value(_read_field(leg, "order_type", default="limit"))),
                    label=f"recommended_legs[{index}].order_type",
                ).lower(),
                post_only=bool(_read_field(leg, "post_only", default=False)),
                time_in_force=_read_field(leg, "time_in_force", default="GTD"),
                expires_at_ms=_read_field(leg, "expires_at_ms", default=None),
                metadata=dict(_read_field(leg, "metadata", default={})),
            )
            for index, leg in enumerate(raw_legs)
        )
        market_ids = _read_tuple(
            _read_field(candidate, "market_ids", default=()), label="market_ids"
        )
        token_ids = _read_tuple(_read_field(candidate, "token_ids", default=()), label="token_ids")
        if not market_ids:
            market_ids = tuple(dict.fromkeys(leg.market_id for leg in legs))
        if not token_ids:
            token_ids = tuple(dict.fromkeys(leg.token_id for leg in legs))
        return cls(
            opportunity_id=_read_field(candidate, "opportunity_id"),
            strategy_id=_read_field(candidate, "strategy_id"),
            opportunity_kind=_read_field(
                candidate,
                "opportunity_kind",
                default=_infer_opportunity_kind(_read_field(candidate, "strategy_id")),
            ),
            detected_at_ms=_require_timestamp_ms(
                _read_field(candidate, "detected_at_ms", "detected_at"),
                label="detected_at_ms",
            ),
            expires_at_ms=_require_timestamp_ms(
                _read_field(candidate, "expires_at_ms", "expires_at"),
                label="expires_at_ms",
            ),
            market_ids=market_ids,
            token_ids=token_ids,
            event_id=event_id,
            recommended_legs=legs,
            expected_gross_edge=expected_gross_edge,
            expected_fees=expected_fees,
            expected_slippage=expected_slippage,
            expected_adverse_selection=expected_adverse_selection,
            expected_inventory_cost=expected_inventory_cost,
            expected_incentive=expected_incentive,
            expected_unwind_cost=expected_unwind_cost,
            uncertainty_buffer=uncertainty_buffer,
            expected_net_edge=expected_net,
            tradable_edge=tradable_edge,
            max_loss=_read_field(candidate, "max_loss"),
            estimated_capacity=_read_field(candidate, "estimated_capacity"),
            confidence=_read_field(candidate, "confidence"),
            evidence_refs=_normalize_evidence_refs(
                _read_field(candidate, "evidence_refs", default=())
            ),
            rejection_reasons=_read_tuple(
                _read_field(candidate, "rejection_reasons", default=()), label="rejection_reasons"
            ),
            metadata=_read_metadata(candidate),
        )


@dataclass(frozen=True)
class PortfolioSnapshot:
    available_capital: Decimal
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    open_orders: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_capital",
            _require_decimal(self.available_capital, label="available_capital", minimum=ZERO),
        )
        object.__setattr__(
            self,
            "positions",
            {
                str(key): _require_decimal(value, label=f"positions[{key}]")
                for key, value in self.positions.items()
            },
        )
        object.__setattr__(
            self, "open_orders", _require_int(self.open_orders, label="open_orders", minimum=0)
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def capped(self, capital_limit: Decimal) -> PortfolioSnapshot:
        limit = _require_decimal(capital_limit, label="capital_limit", minimum=ZERO)
        return replace(self, available_capital=min(self.available_capital, limit))

    def position_for(self, token_id: str) -> Decimal:
        return self.positions.get(token_id, ZERO)


@dataclass(frozen=True)
class StrategyDecision:
    strategy_id: str
    strategy_version: str
    opportunity_id: str
    status: DecisionStatus
    executable: bool
    rationale: str
    expected_net_edge: Decimal
    tradable_edge: Decimal
    max_loss: Decimal
    capital_required: Decimal
    legs: tuple[OrderLeg, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    attribution: Mapping[str, Any] = field(default_factory=dict)
    failure_plan: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    risk_tags: tuple[str, ...] = ()
    mode: RuntimeMode | None = None
    mutation_allowed: bool = False
    capital_limit: Decimal | None = None
    runtime_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "strategy_id", _require_non_empty(self.strategy_id, label="strategy_id")
        )
        object.__setattr__(
            self,
            "strategy_version",
            _require_non_empty(self.strategy_version, label="strategy_version"),
        )
        object.__setattr__(
            self, "opportunity_id", _require_non_empty(self.opportunity_id, label="opportunity_id")
        )
        object.__setattr__(
            self,
            "rationale",
            _require_non_empty(self.rationale, label="rationale"),
        )
        object.__setattr__(
            self,
            "expected_net_edge",
            _require_decimal(self.expected_net_edge, label="expected_net_edge"),
        )
        object.__setattr__(
            self,
            "tradable_edge",
            _require_decimal(self.tradable_edge, label="tradable_edge"),
        )
        object.__setattr__(
            self, "max_loss", _require_decimal(self.max_loss, label="max_loss", minimum=ZERO)
        )
        object.__setattr__(
            self,
            "capital_required",
            _require_decimal(self.capital_required, label="capital_required", minimum=ZERO),
        )
        if self.capital_limit is not None:
            object.__setattr__(
                self,
                "capital_limit",
                _require_decimal(self.capital_limit, label="capital_limit", minimum=ZERO),
            )
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "rejection_reasons", tuple(self.rejection_reasons))
        object.__setattr__(self, "attribution", dict(self.attribution))
        object.__setattr__(self, "failure_plan", dict(self.failure_plan))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "risk_tags", tuple(self.risk_tags))
        object.__setattr__(self, "runtime_context", dict(self.runtime_context))

    def to_document(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _serialize(
                {
                    "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version,
                    "opportunity_id": self.opportunity_id,
                    "status": self.status,
                    "executable": self.executable,
                    "rationale": self.rationale,
                    "expected_net_edge": self.expected_net_edge,
                    "tradable_edge": self.tradable_edge,
                    "max_loss": self.max_loss,
                    "capital_required": self.capital_required,
                    "legs": [leg.to_document() for leg in self.legs],
                    "rejection_reasons": list(self.rejection_reasons),
                    "attribution": dict(self.attribution),
                    "failure_plan": dict(self.failure_plan),
                    "metadata": dict(self.metadata),
                    "risk_tags": list(self.risk_tags),
                    "mode": self.mode,
                    "mutation_allowed": self.mutation_allowed,
                    "capital_limit": self.capital_limit,
                    "runtime_context": dict(self.runtime_context),
                }
            ),
        )

    def signal_signature(self) -> str:
        payload = self.to_document()
        payload.pop("mode", None)
        payload.pop("mutation_allowed", None)
        payload.pop("capital_limit", None)
        payload.pop("runtime_context", None)
        payload.pop("status", None)
        payload.pop("executable", None)
        payload.pop("rejection_reasons", None)
        payload.pop("rationale", None)
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class StrategyFeedback:
    strategy_id: str
    opportunity_id: str
    realized_net_edge: Decimal
    realized_pnl: Decimal
    markout_bps: Decimal = ZERO
    adverse_selection_bps: Decimal = ZERO
    attribution: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "strategy_id", _require_non_empty(self.strategy_id, label="strategy_id")
        )
        object.__setattr__(
            self, "opportunity_id", _require_non_empty(self.opportunity_id, label="opportunity_id")
        )
        object.__setattr__(
            self,
            "realized_net_edge",
            _require_decimal(self.realized_net_edge, label="realized_net_edge"),
        )
        object.__setattr__(
            self, "realized_pnl", _require_decimal(self.realized_pnl, label="realized_pnl")
        )
        object.__setattr__(
            self, "markout_bps", _require_decimal(self.markout_bps, label="markout_bps")
        )
        object.__setattr__(
            self,
            "adverse_selection_bps",
            _require_decimal(self.adverse_selection_bps, label="adverse_selection_bps"),
        )
        object.__setattr__(self, "attribution", dict(self.attribution))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_document(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _serialize(
                {
                    "strategy_id": self.strategy_id,
                    "opportunity_id": self.opportunity_id,
                    "realized_net_edge": self.realized_net_edge,
                    "realized_pnl": self.realized_pnl,
                    "markout_bps": self.markout_bps,
                    "adverse_selection_bps": self.adverse_selection_bps,
                    "attribution": dict(self.attribution),
                    "metadata": dict(self.metadata),
                }
            ),
        )


class Strategy(Protocol):
    strategy_id: str
    strategy_version: str

    def evaluate(
        self,
        opportunity: NormalizedOpportunity,
        portfolio: PortfolioSnapshot,
    ) -> StrategyDecision: ...

    def observe(self, outcome: Mapping[str, Any]) -> StrategyFeedback: ...
