"""Core immutable types for the V0.2 profit-system scaffold."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ProfitSystemValidationError(ValueError):
    """Raised when a core profit-system contract is violated."""


def parse_decimal(
    value: Decimal | str | int,
    *,
    label: str,
    minimum: Decimal | None = None,
) -> Decimal:
    """Parse a Decimal while rejecting binary floats and empty values."""

    if isinstance(value, bool):
        raise ProfitSystemValidationError(f"{label} must not be bool")
    if isinstance(value, float):
        raise ProfitSystemValidationError(f"{label} must be Decimal-compatible, not binary float")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProfitSystemValidationError(f"{label} must be a Decimal") from exc
    if not decimal_value.is_finite():
        raise ProfitSystemValidationError(f"{label} must be finite")
    if minimum is not None and decimal_value < minimum:
        raise ProfitSystemValidationError(f"{label} must be >= {minimum}, got {decimal_value}")
    return decimal_value


def parse_decimal_or_none(
    value: Decimal | str | int | None,
    *,
    label: str,
    minimum: Decimal | None = None,
) -> Decimal | None:
    if value is None:
        return None
    return parse_decimal(value, label=label, minimum=minimum)


def parse_utc_datetime(value: datetime | str, *, label: str) -> datetime:
    """Parse a timezone-aware timestamp and normalize it to UTC."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ProfitSystemValidationError(f"{label} cannot be empty")
        try:
            parsed = datetime.fromisoformat(
                f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
            )
        except ValueError as exc:
            raise ProfitSystemValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    else:
        raise ProfitSystemValidationError(f"{label} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProfitSystemValidationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def validate_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ProfitSystemValidationError(f"{label} must match {_IDENTIFIER_RE.pattern!r}")
    return value


def validate_version_hash(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _VERSION_HASH_RE.fullmatch(value):
        raise ProfitSystemValidationError(f"{label} must be a 64-character lowercase hex hash")
    return value


def normalize_string_sequence(
    values: tuple[str, ...] | list[str],
    *,
    label: str,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized:
        raise ProfitSystemValidationError(f"{label} cannot be empty")
    for index, value in enumerate(normalized):
        validate_identifier(value, label=f"{label}[{index}]")
    return normalized


class OperatingMode(StrEnum):
    RESEARCH = "RESEARCH"
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE_CANARY = "LIVE_CANARY"
    LIVE_LIMITED = "LIVE_LIMITED"
    KILLED = "KILLED"


class StrategyQualification(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    SHADOW_ONLY = "SHADOW_ONLY"
    PROBE_ALLOWED = "PROBE_ALLOWED"
    LIVE_ALLOWED = "LIVE_ALLOWED"
    STOPPED = "STOPPED"


class OpportunityStatus(StrEnum):
    REJECTED = "REJECTED"
    WATCH = "WATCH"
    REVIEW = "REVIEW"
    EXECUTABLE = "EXECUTABLE"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    ref: str
    label: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.kind, label="evidence_ref.kind")
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ProfitSystemValidationError("evidence_ref.ref cannot be empty")
        if self.label is not None and not self.label.strip():
            raise ProfitSystemValidationError("evidence_ref.label cannot be blank")


@dataclass(frozen=True, slots=True)
class RecommendedLeg:
    market_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    order_type: OrderType = OrderType.LIMIT
    post_only: bool = True

    def __post_init__(self) -> None:
        validate_identifier(self.market_id, label="recommended_leg.market_id")
        validate_identifier(self.token_id, label="recommended_leg.token_id")
        object.__setattr__(
            self,
            "price",
            parse_decimal(self.price, label="recommended_leg.price", minimum=ZERO),
        )
        object.__setattr__(
            self,
            "size",
            parse_decimal(self.size, label="recommended_leg.size", minimum=ZERO),
        )
        if self.size <= ZERO:
            raise ProfitSystemValidationError("recommended_leg.size must be > 0")


@dataclass(frozen=True, slots=True)
class OpportunityCostEstimate:
    expected_taker_fee: Decimal | None
    expected_builder_fee: Decimal | None
    expected_slippage: Decimal | None
    expected_adverse_selection: Decimal | None
    expected_inventory_cost: Decimal | None
    expected_unwind_cost: Decimal | None
    uncertainty_buffer: Decimal | None = ZERO

    def __post_init__(self) -> None:
        for field_name in (
            "expected_taker_fee",
            "expected_builder_fee",
            "expected_slippage",
            "expected_adverse_selection",
            "expected_inventory_cost",
            "expected_unwind_cost",
            "uncertainty_buffer",
        ):
            object.__setattr__(
                self,
                field_name,
                parse_decimal_or_none(
                    getattr(self, field_name),
                    label=f"opportunity_costs.{field_name}",
                    minimum=ZERO,
                ),
            )

    @property
    def unknown_fields(self) -> tuple[str, ...]:
        missing = []
        for field_name in (
            "expected_taker_fee",
            "expected_builder_fee",
            "expected_slippage",
            "expected_adverse_selection",
            "expected_inventory_cost",
            "expected_unwind_cost",
            "uncertainty_buffer",
        ):
            if getattr(self, field_name) is None:
                missing.append(field_name)
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class ProjectedRisk:
    order_notional: Decimal
    projected_market_exposure: Decimal
    projected_event_exposure: Decimal
    projected_strategy_exposure: Decimal
    projected_total_exposure: Decimal
    projected_daily_loss: Decimal
    projected_drawdown: Decimal
    projected_open_orders: int
    projected_unmatched_leg_seconds: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "order_notional",
            "projected_market_exposure",
            "projected_event_exposure",
            "projected_strategy_exposure",
            "projected_total_exposure",
            "projected_daily_loss",
            "projected_drawdown",
            "projected_unmatched_leg_seconds",
        ):
            object.__setattr__(
                self,
                field_name,
                parse_decimal(
                    getattr(self, field_name),
                    label=f"projected_risk.{field_name}",
                    minimum=ZERO,
                ),
            )
        if isinstance(self.projected_open_orders, bool) or self.projected_open_orders < 0:
            raise ProfitSystemValidationError("projected_risk.projected_open_orders must be >= 0")


@dataclass(frozen=True, slots=True)
class OpportunitySignal:
    strategy_id: str
    detected_at: datetime
    expires_at: datetime
    market_ids: tuple[str, ...]
    token_ids: tuple[str, ...]
    event_id: str
    recommended_legs: tuple[RecommendedLeg, ...]
    expected_gross_edge: Decimal
    expected_incentive: Decimal | None
    costs: OpportunityCostEstimate
    estimated_capacity: Decimal
    max_loss: Decimal
    confidence: Decimal
    lower_confidence_margin: Decimal
    projected_risk: ProjectedRisk
    evidence_refs: tuple[EvidenceRef, ...]
    data_available: bool = True
    market_available: bool = True
    account_available: bool = True
    execution_available: bool = True

    def __post_init__(self) -> None:
        validate_identifier(self.strategy_id, label="opportunity_signal.strategy_id")
        validate_identifier(self.event_id, label="opportunity_signal.event_id")
        object.__setattr__(
            self,
            "detected_at",
            parse_utc_datetime(self.detected_at, label="opportunity_signal.detected_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            parse_utc_datetime(self.expires_at, label="opportunity_signal.expires_at"),
        )
        if self.expires_at <= self.detected_at:
            raise ProfitSystemValidationError(
                "opportunity_signal.expires_at must be after detected_at"
            )
        object.__setattr__(
            self,
            "market_ids",
            normalize_string_sequence(self.market_ids, label="opportunity_signal.market_ids"),
        )
        object.__setattr__(
            self,
            "token_ids",
            normalize_string_sequence(self.token_ids, label="opportunity_signal.token_ids"),
        )
        if not self.recommended_legs:
            raise ProfitSystemValidationError("opportunity_signal.recommended_legs cannot be empty")
        if not self.evidence_refs:
            raise ProfitSystemValidationError("opportunity_signal.evidence_refs cannot be empty")
        object.__setattr__(
            self,
            "expected_gross_edge",
            parse_decimal(
                self.expected_gross_edge,
                label="opportunity_signal.expected_gross_edge",
            ),
        )
        object.__setattr__(
            self,
            "expected_incentive",
            parse_decimal_or_none(
                self.expected_incentive,
                label="opportunity_signal.expected_incentive",
                minimum=ZERO,
            ),
        )
        object.__setattr__(
            self,
            "estimated_capacity",
            parse_decimal(
                self.estimated_capacity,
                label="opportunity_signal.estimated_capacity",
                minimum=ZERO,
            ),
        )
        object.__setattr__(
            self,
            "max_loss",
            parse_decimal(
                self.max_loss,
                label="opportunity_signal.max_loss",
                minimum=ZERO,
            ),
        )
        object.__setattr__(
            self,
            "confidence",
            parse_decimal(
                self.confidence,
                label="opportunity_signal.confidence",
                minimum=ZERO,
            ),
        )
        if self.confidence > ONE:
            raise ProfitSystemValidationError("opportunity_signal.confidence must be <= 1")
        object.__setattr__(
            self,
            "lower_confidence_margin",
            parse_decimal(
                self.lower_confidence_margin,
                label="opportunity_signal.lower_confidence_margin",
                minimum=ZERO,
            ),
        )


@dataclass(frozen=True, slots=True)
class OpportunitySnapshot:
    captured_at: datetime
    signals: tuple[OpportunitySignal, ...]
    market_data_age_ms: int
    user_stream_gap_seconds: Decimal
    open_order_count: int
    current_order_rate: Decimal
    market_constraints_known: bool = True
    geoblock_allows_trading: bool = True
    persistent_kill: bool = False
    snapshot_hash: str = field(init=False)

    def __post_init__(self) -> None:
        from .serialization import canonical_sha256, to_canonical_jsonable

        object.__setattr__(
            self,
            "captured_at",
            parse_utc_datetime(self.captured_at, label="opportunity_snapshot.captured_at"),
        )
        if not self.signals:
            raise ProfitSystemValidationError("opportunity_snapshot.signals cannot be empty")
        if isinstance(self.market_data_age_ms, bool) or self.market_data_age_ms < 0:
            raise ProfitSystemValidationError(
                "opportunity_snapshot.market_data_age_ms must be >= 0"
            )
        if isinstance(self.open_order_count, bool) or self.open_order_count < 0:
            raise ProfitSystemValidationError("opportunity_snapshot.open_order_count must be >= 0")
        object.__setattr__(
            self,
            "user_stream_gap_seconds",
            parse_decimal(
                self.user_stream_gap_seconds,
                label="opportunity_snapshot.user_stream_gap_seconds",
                minimum=ZERO,
            ),
        )
        object.__setattr__(
            self,
            "current_order_rate",
            parse_decimal(
                self.current_order_rate,
                label="opportunity_snapshot.current_order_rate",
                minimum=ZERO,
            ),
        )
        unsigned = {
            "captured_at": self.captured_at,
            "signals": self.signals,
            "market_data_age_ms": self.market_data_age_ms,
            "user_stream_gap_seconds": self.user_stream_gap_seconds,
            "open_order_count": self.open_order_count,
            "current_order_rate": self.current_order_rate,
            "market_constraints_known": self.market_constraints_known,
            "geoblock_allows_trading": self.geoblock_allows_trading,
            "persistent_kill": self.persistent_kill,
        }
        object.__setattr__(self, "snapshot_hash", canonical_sha256(to_canonical_jsonable(unsigned)))


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    opportunity_id: str
    strategy_id: str
    detected_at: datetime
    expires_at: datetime
    market_ids: tuple[str, ...]
    token_ids: tuple[str, ...]
    event_id: str
    recommended_legs: tuple[RecommendedLeg, ...]
    expected_gross_edge: Decimal
    expected_fees: Decimal | None
    expected_slippage: Decimal | None
    expected_adverse_selection: Decimal | None
    expected_inventory_cost: Decimal | None
    expected_incentive: Decimal | None
    expected_net_edge: Decimal | None
    tradable_edge: Decimal | None
    max_loss: Decimal
    estimated_capacity: Decimal
    confidence: Decimal
    evidence_refs: tuple[EvidenceRef, ...]
    rejection_reasons: tuple[str, ...]
    status: OpportunityStatus
    executable: bool
    expected_taker_fee: Decimal | None
    expected_builder_fee: Decimal | None
    expected_unwind_cost: Decimal | None
    uncertainty_buffer: Decimal | None
    lower_confidence_margin: Decimal
    risk_adjusted_expected_net_profit: Decimal | None
    snapshot_hash: str
    config_hash: str
    code_version_hash: str

    def __post_init__(self) -> None:
        validate_identifier(self.opportunity_id, label="opportunity_candidate.opportunity_id")
        validate_identifier(self.strategy_id, label="opportunity_candidate.strategy_id")
        validate_identifier(self.event_id, label="opportunity_candidate.event_id")
        object.__setattr__(
            self,
            "detected_at",
            parse_utc_datetime(self.detected_at, label="opportunity_candidate.detected_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            parse_utc_datetime(self.expires_at, label="opportunity_candidate.expires_at"),
        )
        object.__setattr__(
            self,
            "market_ids",
            normalize_string_sequence(self.market_ids, label="opportunity_candidate.market_ids"),
        )
        object.__setattr__(
            self,
            "token_ids",
            normalize_string_sequence(self.token_ids, label="opportunity_candidate.token_ids"),
        )
        for field_name in (
            "expected_gross_edge",
            "expected_fees",
            "expected_slippage",
            "expected_adverse_selection",
            "expected_inventory_cost",
            "expected_incentive",
            "expected_net_edge",
            "tradable_edge",
            "max_loss",
            "estimated_capacity",
            "confidence",
            "expected_taker_fee",
            "expected_builder_fee",
            "expected_unwind_cost",
            "uncertainty_buffer",
            "lower_confidence_margin",
            "risk_adjusted_expected_net_profit",
        ):
            value = getattr(self, field_name)
            parser = (
                parse_decimal
                if field_name
                in {
                    "expected_gross_edge",
                    "max_loss",
                    "estimated_capacity",
                    "confidence",
                    "lower_confidence_margin",
                }
                else parse_decimal_or_none
            )
            object.__setattr__(
                self,
                field_name,
                parser(value, label=f"opportunity_candidate.{field_name}"),
            )
        validate_version_hash(self.snapshot_hash, label="opportunity_candidate.snapshot_hash")
        validate_version_hash(self.config_hash, label="opportunity_candidate.config_hash")
        validate_version_hash(
            self.code_version_hash,
            label="opportunity_candidate.code_version_hash",
        )

    @property
    def remaining_ttl_seconds(self) -> Decimal:
        return Decimal(str((self.expires_at - self.detected_at).total_seconds()))


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        validate_identifier(self.name, label="gate_check.name")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ProfitSystemValidationError("gate_check.detail cannot be empty")


@dataclass(frozen=True, slots=True)
class OpportunityExplanation:
    opportunity_id: str
    candidate: OpportunityCandidate
    gate_checks: tuple[GateCheck, ...]
    scenarios: Mapping[str, Decimal | None]
    recomputation_inputs: Mapping[str, Any]
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.opportunity_id, label="opportunity_explanation.opportunity_id")
        if self.opportunity_id != self.candidate.opportunity_id:
            raise ProfitSystemValidationError(
                "opportunity_explanation.opportunity_id must match candidate"
            )
        if not self.gate_checks:
            raise ProfitSystemValidationError("opportunity_explanation.gate_checks cannot be empty")
        if not self.evidence_refs:
            raise ProfitSystemValidationError(
                "opportunity_explanation.evidence_refs cannot be empty"
            )


@dataclass(frozen=True, slots=True)
class RankedOpportunities:
    scanned_at: datetime
    snapshot_hash: str
    opportunities: tuple[OpportunityCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scanned_at", parse_utc_datetime(self.scanned_at, label="ranked.scanned_at")
        )
        validate_version_hash(self.snapshot_hash, label="ranked.snapshot_hash")
