"""Cluster-level locked-OOS evidence for the v0.7 shared-terminal track."""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from .btc_twap_relative_value_v07 import (
    CANONICAL_PAIR_ID_PREFIX,
    V07EdgeBasis,
    V07ForecastAvailabilityBasis,
    V07QuantitySelectionBasis,
    canonical_expiry_cluster_id,
)
from .data_store import canonical_json_bytes

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
ECE_LIMIT = Decimal("0.05")
CONCENTRATION_LIMIT = Decimal("0.20")
MINIMUM_SETTLED_CLUSTERS = 100
MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS = 100
BOOTSTRAP_RESAMPLES = 5_000
BOOTSTRAP_SEED = 712
ECE_BINS = 10
CAPTURED_TAKER_DELAY_MS = 250
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_LOCK_PROVENANCE_ISSUER = object()
_BUILDER_EVIDENCE_ISSUER = object()
_BUILDER_ID = "scripts.build_btc_twap_relative_value_v07_counterfactual"
_SOURCE_BASELINE = "c557160d0c98e195a988f4353bbe19a3b00b3576"
_CAPTURE_CONFIG_SCHEMA_VERSION = "btc-5m-15m-relative-value-capture.v1"


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{label} must be exact decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _probability(value: Any, *, label: str) -> Decimal:
    parsed = _decimal(value, label=label)
    if not ZERO <= parsed <= ONE:
        raise ValueError(f"{label} must be in [0, 1]")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _non_negative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _sha256(value: str | None, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return value


def _canonical_pair_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith(CANONICAL_PAIR_ID_PREFIX):
        raise ValueError("event_cluster_id must be a canonical expiry/pair key")
    _sha256(value.removeprefix(CANONICAL_PAIR_ID_PREFIX), label="pair digest")
    return value


def _project_pair(
    *,
    strike_5: Decimal,
    strike_15: Decimal,
    q_5_up: Decimal,
    q_15_up: Decimal,
) -> tuple[Decimal, Decimal]:
    q5 = _probability(q_5_up, label="q_5_up")
    q15 = _probability(q_15_up, label="q_15_up")
    if strike_5 < strike_15 and q5 < q15:
        pooled = (q5 + q15) / TWO
        return pooled, pooled
    if strike_5 > strike_15 and q15 < q5:
        pooled = (q5 + q15) / TWO
        return pooled, pooled
    if strike_5 == strike_15:
        pooled = (q5 + q15) / TWO
        return pooled, pooled
    return q5, q15


class PreLabelLockStatus(str, Enum):
    VERIFIED = "verified_pre_label"
    COUNTERFACTUAL = "counterfactual_unlocked"


@dataclass(frozen=True, init=False)
class PreLabelLockProvenance:
    """Lock receipts issued only by the verified builder path."""

    status: PreLabelLockStatus
    test_universe_sha256: str | None = None
    test_universe_locked_at_ms: int | None = None
    test_universe_received_at_ms: int | None = None
    test_universe_receipt_id: str | None = None
    prediction_locked_at_ms: int | None = None
    prediction_received_at_ms: int | None = None
    prediction_receipt_id: str | None = None
    forecast_payload_sha256: str | None = None
    decision_payload_sha256: str | None = None
    reason: str | None = None
    _issuer_token: object | None = field(default=None, repr=False, compare=False)

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError(
            "PreLabelLockProvenance cannot be caller-constructed; use "
            "counterfactual() or the verified builder path"
        )

    @classmethod
    def _create(
        cls,
        *,
        status: PreLabelLockStatus,
        test_universe_sha256: str | None = None,
        test_universe_locked_at_ms: int | None = None,
        test_universe_received_at_ms: int | None = None,
        test_universe_receipt_id: str | None = None,
        prediction_locked_at_ms: int | None = None,
        prediction_received_at_ms: int | None = None,
        prediction_receipt_id: str | None = None,
        forecast_payload_sha256: str | None = None,
        decision_payload_sha256: str | None = None,
        reason: str | None = None,
        issuer_token: object | None = None,
    ) -> PreLabelLockProvenance:
        instance = object.__new__(cls)
        values = {
            "status": status,
            "test_universe_sha256": test_universe_sha256,
            "test_universe_locked_at_ms": test_universe_locked_at_ms,
            "test_universe_received_at_ms": test_universe_received_at_ms,
            "test_universe_receipt_id": test_universe_receipt_id,
            "prediction_locked_at_ms": prediction_locked_at_ms,
            "prediction_received_at_ms": prediction_received_at_ms,
            "prediction_receipt_id": prediction_receipt_id,
            "forecast_payload_sha256": forecast_payload_sha256,
            "decision_payload_sha256": decision_payload_sha256,
            "reason": reason,
            "_issuer_token": issuer_token,
        }
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if not isinstance(self.status, PreLabelLockStatus):
            raise TypeError("status must be PreLabelLockStatus")
        if self.status is PreLabelLockStatus.COUNTERFACTUAL:
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("counterfactual lock provenance requires a reason")
            receipt_fields = (
                self.test_universe_sha256,
                self.test_universe_locked_at_ms,
                self.test_universe_received_at_ms,
                self.test_universe_receipt_id,
                self.prediction_locked_at_ms,
                self.prediction_received_at_ms,
                self.prediction_receipt_id,
                self.forecast_payload_sha256,
                self.decision_payload_sha256,
            )
            if any(value is not None for value in receipt_fields):
                raise ValueError("counterfactual provenance cannot claim lock receipts")
            return

        for name in (
            "test_universe_sha256",
            "forecast_payload_sha256",
            "decision_payload_sha256",
        ):
            _sha256(getattr(self, name), label=name)
        for name in (
            "test_universe_locked_at_ms",
            "test_universe_received_at_ms",
            "prediction_locked_at_ms",
            "prediction_received_at_ms",
        ):
            _non_negative_int(getattr(self, name), label=name)
        for name in ("test_universe_receipt_id", "prediction_receipt_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        assert self.test_universe_locked_at_ms is not None
        assert self.test_universe_received_at_ms is not None
        assert self.prediction_locked_at_ms is not None
        assert self.prediction_received_at_ms is not None
        if self.test_universe_received_at_ms < self.test_universe_locked_at_ms:
            raise ValueError("test-universe receipt predates its lock timestamp")
        if self.prediction_received_at_ms < self.prediction_locked_at_ms:
            raise ValueError("prediction receipt predates its lock timestamp")
        if self.reason is not None:
            raise ValueError("verified lock provenance cannot have a failure reason")

    @classmethod
    def counterfactual(cls, reason: str) -> PreLabelLockProvenance:
        return cls._create(status=PreLabelLockStatus.COUNTERFACTUAL, reason=reason)

    @property
    def verified(self) -> bool:
        return (
            self.status is PreLabelLockStatus.VERIFIED
            and self._issuer_token is _LOCK_PROVENANCE_ISSUER
        )

    def to_document(self) -> dict[str, Any]:
        payload = {
            "schema_version": "btc-5m-15m-v07-prelabel-lock-provenance.v3",
            "status": self.status.value,
            "builder_issued_verified_receipt": self.verified,
            "test_universe_sha256": self.test_universe_sha256,
            "test_universe_locked_at_ms": self.test_universe_locked_at_ms,
            "test_universe_received_at_ms": self.test_universe_received_at_ms,
            "test_universe_receipt_id": self.test_universe_receipt_id,
            "prediction_locked_at_ms": self.prediction_locked_at_ms,
            "prediction_received_at_ms": self.prediction_received_at_ms,
            "prediction_receipt_id": self.prediction_receipt_id,
            "forecast_payload_sha256": self.forecast_payload_sha256,
            "decision_payload_sha256": self.decision_payload_sha256,
            "reason": self.reason,
        }
        return {
            **payload,
            "provenance_sha256": hashlib.sha256(
                canonical_json_bytes(payload)
            ).hexdigest(),
        }


def _issue_verified_prelabel_lock_provenance(
    *,
    test_universe_sha256: str,
    test_universe_locked_at_ms: int,
    test_universe_received_at_ms: int,
    test_universe_receipt_id: str,
    prediction_locked_at_ms: int,
    prediction_received_at_ms: int,
    prediction_receipt_id: str,
    forecast_payload_sha256: str,
    decision_payload_sha256: str,
) -> PreLabelLockProvenance:
    """Issue an opaque verified receipt after builder journal verification."""

    return PreLabelLockProvenance._create(
        status=PreLabelLockStatus.VERIFIED,
        test_universe_sha256=test_universe_sha256,
        test_universe_locked_at_ms=test_universe_locked_at_ms,
        test_universe_received_at_ms=test_universe_received_at_ms,
        test_universe_receipt_id=test_universe_receipt_id,
        prediction_locked_at_ms=prediction_locked_at_ms,
        prediction_received_at_ms=prediction_received_at_ms,
        prediction_receipt_id=prediction_receipt_id,
        forecast_payload_sha256=forecast_payload_sha256,
        decision_payload_sha256=decision_payload_sha256,
        issuer_token=_LOCK_PROVENANCE_ISSUER,
    )


@dataclass(frozen=True)
class LockedOOSForecastRow:
    event_cluster_id: str
    event_cluster_alias: str
    expiry_ms: int
    decision_tau_seconds: int
    decision_at_ms: int
    forecast_available_at_ms: int
    forecast_availability_basis: V07ForecastAvailabilityBasis
    label_available_at_ms: int
    strike_5: Decimal
    strike_15: Decimal
    forecast_q_5_up: Decimal | None
    forecast_q_15_up: Decimal | None
    market_q_5_up: Decimal | None
    market_q_15_up: Decimal | None
    actual_5_up: bool
    actual_15_up: bool
    lock_provenance: PreLabelLockProvenance = field(
        default_factory=lambda: PreLabelLockProvenance.counterfactual(
            "prelabel_lock_receipt_not_supplied"
        )
    )
    raw_top_ask_q_5_up: Decimal | None = None
    raw_top_ask_q_15_up: Decimal | None = None
    edge_basis: V07EdgeBasis = V07EdgeBasis.PREDICTIVE
    edge_evaluation_quantity: Decimal | None = None
    structural_worst_case_payoff_per_pair: Decimal | None = None
    structural_net_floor_per_pair: Decimal | None = None
    structural_quantity_executable: bool = False
    quantity_selection_basis: V07QuantitySelectionBasis = V07QuantitySelectionBasis.NONE
    quantity_candidate_breakpoint_count: int = 0
    selected_guaranteed_total_pnl: Decimal | None = None
    selected_uncertainty_adjusted_total_pnl: Decimal | None = None
    probability_diagnostics_applicable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_cluster_id",
            _canonical_pair_id(self.event_cluster_id),
        )
        if (
            not isinstance(self.event_cluster_alias, str)
            or not self.event_cluster_alias
        ):
            raise ValueError("event_cluster_alias must be non-empty")
        _non_negative_int(self.decision_tau_seconds, label="decision_tau_seconds")
        expiry_ms = _non_negative_int(self.expiry_ms, label="expiry_ms")
        if expiry_ms <= 0:
            raise ValueError("expiry_ms must be positive")
        decision_at_ms = _non_negative_int(self.decision_at_ms, label="decision_at_ms")
        available_at_ms = _non_negative_int(
            self.forecast_available_at_ms,
            label="forecast_available_at_ms",
        )
        label_at_ms = _non_negative_int(
            self.label_available_at_ms,
            label="label_available_at_ms",
        )
        if not isinstance(
            self.forecast_availability_basis,
            V07ForecastAvailabilityBasis,
        ):
            raise TypeError(
                "forecast_availability_basis must be V07ForecastAvailabilityBasis"
            )
        if decision_at_ms > expiry_ms:
            raise ValueError("decision must not occur after the common expiry")
        if available_at_ms < decision_at_ms:
            raise ValueError("forecast cannot become available before the decision")
        if available_at_ms >= expiry_ms:
            raise ValueError("forecast must become available before the common expiry")
        if label_at_ms < expiry_ms:
            raise ValueError("label cannot become available before the common expiry")
        if label_at_ms <= decision_at_ms:
            raise ValueError("label must become available after the decision")
        for name in (
            "forecast_q_5_up",
            "forecast_q_15_up",
            "market_q_5_up",
            "market_q_15_up",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _probability(value, label=name))
        for name in ("raw_top_ask_q_5_up", "raw_top_ask_q_15_up"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _probability(value, label=name))
        for name in ("strike_5", "strike_15"):
            strike = _decimal(getattr(self, name), label=name)
            if strike <= ZERO:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, strike)
        if not isinstance(self.actual_5_up, bool) or not isinstance(
            self.actual_15_up,
            bool,
        ):
            raise TypeError("actual outcomes must be bool")
        if (self.forecast_q_5_up is None) != (self.forecast_q_15_up is None):
            raise ValueError("forecast probability fields must both be null or present")
        if (self.market_q_5_up is None) != (self.market_q_15_up is None):
            raise ValueError("market probability fields must both be null or present")
        if self.forecast_q_5_up is not None and self.forecast_q_15_up is not None:
            projected = _project_pair(
                strike_5=self.strike_5,
                strike_15=self.strike_15,
                q_5_up=self.forecast_q_5_up,
                q_15_up=self.forecast_q_15_up,
            )
            if projected != (self.forecast_q_5_up, self.forecast_q_15_up):
                raise ValueError("forecast probabilities violate strike ordering")
        if (
            self.strike_5 < self.strike_15
            and self.actual_15_up
            and not self.actual_5_up
        ):
            raise ValueError("actual outcomes violate strike ordering")
        if (
            self.strike_5 > self.strike_15
            and self.actual_5_up
            and not self.actual_15_up
        ):
            raise ValueError("actual outcomes violate strike ordering")
        if self.strike_5 == self.strike_15 and self.actual_5_up != self.actual_15_up:
            raise ValueError("equal strikes require equal actual outcomes")
        if not isinstance(self.lock_provenance, PreLabelLockProvenance):
            raise TypeError("lock_provenance must be PreLabelLockProvenance")
        if not isinstance(self.edge_basis, V07EdgeBasis):
            raise TypeError("edge_basis must be V07EdgeBasis")
        if not isinstance(self.structural_quantity_executable, bool):
            raise TypeError("structural_quantity_executable must be bool")
        if not isinstance(self.quantity_selection_basis, V07QuantitySelectionBasis):
            raise TypeError(
                "quantity_selection_basis must be V07QuantitySelectionBasis"
            )
        _non_negative_int(
            self.quantity_candidate_breakpoint_count,
            label="quantity_candidate_breakpoint_count",
        )
        if not isinstance(self.probability_diagnostics_applicable, bool):
            raise TypeError("probability_diagnostics_applicable must be bool")
        for name in (
            "edge_evaluation_quantity",
            "structural_worst_case_payoff_per_pair",
            "structural_net_floor_per_pair",
            "selected_guaranteed_total_pnl",
            "selected_uncertainty_adjusted_total_pnl",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, label=name))
        if (
            self.edge_evaluation_quantity is not None
            and self.edge_evaluation_quantity <= ZERO
        ):
            raise ValueError("edge_evaluation_quantity must be positive")
        if self.edge_basis is V07EdgeBasis.STRUCTURAL:
            if (
                not self.structural_quantity_executable
                or self.structural_net_floor_per_pair is None
                or self.structural_net_floor_per_pair <= ZERO
                or self.quantity_selection_basis
                is not (V07QuantitySelectionBasis.STRUCTURAL_MAX_GUARANTEED_TOTAL_PNL)
                or self.selected_guaranteed_total_pnl is None
                or self.selected_guaranteed_total_pnl <= ZERO
            ):
                raise ValueError(
                    "structural forecast basis requires executable positive floor"
                )
            if self.edge_evaluation_quantity is None:
                raise ValueError("structural forecast requires evaluated quantity")
            if self.selected_guaranteed_total_pnl != (
                self.edge_evaluation_quantity * self.structural_net_floor_per_pair
            ):
                raise ValueError(
                    "structural forecast guaranteed total does not match quantity/floor"
                )
            if self.probability_diagnostics_applicable:
                raise ValueError(
                    "structural forecast cannot claim predictive "
                    "probability diagnostics"
                )
            if any(
                value is not None
                for value in (
                    self.forecast_q_5_up,
                    self.forecast_q_15_up,
                    self.market_q_5_up,
                    self.market_q_15_up,
                    self.raw_top_ask_q_5_up,
                    self.raw_top_ask_q_15_up,
                    self.selected_uncertainty_adjusted_total_pnl,
                )
            ):
                raise ValueError(
                    "structural forecast probability and predictive objective fields "
                    "must be null"
                )
        elif self.edge_basis is V07EdgeBasis.PREDICTIVE:
            if self.edge_evaluation_quantity is None:
                raise ValueError("predictive forecast requires evaluated quantity")
            if any(
                value is None
                for value in (
                    self.forecast_q_5_up,
                    self.forecast_q_15_up,
                    self.market_q_5_up,
                    self.market_q_15_up,
                    self.selected_uncertainty_adjusted_total_pnl,
                )
            ):
                raise ValueError("predictive forecasts require probability diagnostics")
            if not self.probability_diagnostics_applicable:
                raise ValueError(
                    "predictive probability diagnostics must be applicable"
                )
            if self.quantity_selection_basis is not (
                V07QuantitySelectionBasis.PREDICTIVE_MAX_UNCERTAINTY_ADJUSTED_TOTAL_PNL
            ):
                raise ValueError("predictive forecast has the wrong sizing objective")
        if self.lock_provenance.verified:
            prediction_locked_at_ms = self.lock_provenance.prediction_locked_at_ms
            prediction_received_at_ms = self.lock_provenance.prediction_received_at_ms
            universe_locked_at_ms = self.lock_provenance.test_universe_locked_at_ms
            universe_received_at_ms = self.lock_provenance.test_universe_received_at_ms
            assert prediction_locked_at_ms is not None
            assert prediction_received_at_ms is not None
            assert universe_locked_at_ms is not None
            assert universe_received_at_ms is not None
            if prediction_locked_at_ms != decision_at_ms:
                raise ValueError("prediction lock must equal decision_at_ms")
            if prediction_received_at_ms != available_at_ms:
                raise ValueError(
                    "verified forecast availability must equal prediction receipt time"
                )
            if self.forecast_availability_basis is not (
                V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
            ):
                raise ValueError(
                    "verified forecast requires immutable-receipt availability basis"
                )
            if prediction_received_at_ms >= expiry_ms:
                raise ValueError("prediction receipt must predate the common expiry")
            if universe_locked_at_ms > decision_at_ms:
                raise ValueError("test universe must be locked before the decision")
            if universe_received_at_ms > decision_at_ms:
                raise ValueError("test-universe receipt must predate the decision")
        elif self.forecast_availability_basis is not (
            V07ForecastAvailabilityBasis.PREREGISTERED_COUNTERFACTUAL_DELAY
        ):
            raise ValueError(
                "counterfactual forecast requires preregistered-delay "
                "availability basis"
            )

    @property
    def identity(self) -> tuple[str, int]:
        return self.expiry_cluster_id, self.decision_tau_seconds

    @property
    def canonical_pair_id(self) -> str:
        return self.event_cluster_id

    @property
    def expiry_cluster_id(self) -> str:
        return canonical_expiry_cluster_id(self.expiry_ms)

    @property
    def coherent_market_marginals(
        self,
    ) -> tuple[Decimal | None, Decimal | None]:
        if self.market_q_5_up is None or self.market_q_15_up is None:
            return None, None
        return _project_pair(
            strike_5=self.strike_5,
            strike_15=self.strike_15,
            q_5_up=self.market_q_5_up,
            q_15_up=self.market_q_15_up,
        )

    def to_document(self) -> dict[str, Any]:
        coherent_5, coherent_15 = self.coherent_market_marginals
        return {
            "canonical_pair_id": self.canonical_pair_id,
            "expiry_ms": self.expiry_ms,
            "expiry_cluster_id": self.expiry_cluster_id,
            "event_cluster_alias": self.event_cluster_alias,
            "decision_tau_seconds": self.decision_tau_seconds,
            "decision_at_ms": self.decision_at_ms,
            "forecast_available_at_ms": self.forecast_available_at_ms,
            "forecast_availability_basis": self.forecast_availability_basis.value,
            "label_available_at_ms": self.label_available_at_ms,
            "strike_5": _decimal_text(self.strike_5),
            "strike_15": _decimal_text(self.strike_15),
            "forecast_q_5_up": _decimal_text(self.forecast_q_5_up),
            "forecast_q_15_up": _decimal_text(self.forecast_q_15_up),
            "binding_coherent_executable_market_q_5_up": _decimal_text(coherent_5),
            "binding_coherent_executable_market_q_15_up": _decimal_text(coherent_15),
            "pre_projection_executable_market_q_5_up": _decimal_text(
                self.market_q_5_up
            ),
            "pre_projection_executable_market_q_15_up": _decimal_text(
                self.market_q_15_up
            ),
            "raw_top_ask_q_5_up_diagnostic": _decimal_text(self.raw_top_ask_q_5_up),
            "raw_top_ask_q_15_up_diagnostic": _decimal_text(self.raw_top_ask_q_15_up),
            "actual_5_up": self.actual_5_up,
            "actual_15_up": self.actual_15_up,
            "edge_basis": self.edge_basis.value,
            "edge_evaluation_quantity": _decimal_text(self.edge_evaluation_quantity),
            "structural_worst_case_payoff_per_pair": _decimal_text(
                self.structural_worst_case_payoff_per_pair
            ),
            "structural_net_floor_per_pair": _decimal_text(
                self.structural_net_floor_per_pair
            ),
            "structural_quantity_executable": self.structural_quantity_executable,
            "quantity_selection_basis": self.quantity_selection_basis.value,
            "quantity_candidate_breakpoint_count": (
                self.quantity_candidate_breakpoint_count
            ),
            "selected_guaranteed_total_pnl": _decimal_text(
                self.selected_guaranteed_total_pnl
            ),
            "selected_uncertainty_adjusted_total_pnl": _decimal_text(
                self.selected_uncertainty_adjusted_total_pnl
            ),
            "probability_diagnostics_applicable": (
                self.probability_diagnostics_applicable
            ),
            "lock_provenance": self.lock_provenance.to_document(),
        }


@dataclass(frozen=True)
class LockedOOSEconomicAttempt:
    """One reconciled actionable decision; no-fill rows remain explicit."""

    attempt_id: str
    event_cluster_id: str
    event_cluster_alias: str
    expiry_ms: int
    decision_tau_seconds: int
    decision_at_ms: int
    forecast_available_at_ms: int
    forecast_availability_basis: V07ForecastAvailabilityBasis
    first_execution_not_before_ms: int
    first_execution_observed_at_ms: int | None
    effective_signal_to_execution_latency_ms: int
    net_pnl: Decimal
    explainable: bool
    complete_cost_evidence: bool
    complete_execution_evidence: bool
    complete_settlement_evidence: bool
    immutable_public_capture_evidence: bool
    signal_strength: Decimal | None
    execution_status: str
    economic_attempt: bool = True
    causal_no_fill: bool = False
    reconciliation_status: str = "settled_execution"
    decision_action: str = "actionable"
    lock_provenance: PreLabelLockProvenance = field(
        default_factory=lambda: PreLabelLockProvenance.counterfactual(
            "prelabel_lock_receipt_not_supplied"
        )
    )
    edge_basis: V07EdgeBasis = V07EdgeBasis.PREDICTIVE
    edge_evaluation_quantity: Decimal | None = None
    structural_worst_case_payoff_per_pair: Decimal | None = None
    structural_net_floor_per_pair: Decimal | None = None
    structural_quantity_executable: bool = False
    quantity_selection_basis: V07QuantitySelectionBasis = V07QuantitySelectionBasis.NONE
    quantity_candidate_breakpoint_count: int = 0
    selected_guaranteed_total_pnl: Decimal | None = None
    selected_uncertainty_adjusted_total_pnl: Decimal | None = None
    probability_diagnostics_applicable: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        object.__setattr__(
            self,
            "event_cluster_id",
            _canonical_pair_id(self.event_cluster_id),
        )
        if (
            not isinstance(self.event_cluster_alias, str)
            or not self.event_cluster_alias
        ):
            raise ValueError("event_cluster_alias must be non-empty")
        for name in ("execution_status", "reconciliation_status", "decision_action"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        _non_negative_int(self.decision_tau_seconds, label="decision_tau_seconds")
        expiry_ms = _non_negative_int(self.expiry_ms, label="expiry_ms")
        if expiry_ms <= 0:
            raise ValueError("expiry_ms must be positive")
        decision_at_ms = _non_negative_int(self.decision_at_ms, label="decision_at_ms")
        available_at_ms = _non_negative_int(
            self.forecast_available_at_ms,
            label="forecast_available_at_ms",
        )
        first_not_before_ms = _non_negative_int(
            self.first_execution_not_before_ms,
            label="first_execution_not_before_ms",
        )
        observed_at_ms = self.first_execution_observed_at_ms
        if observed_at_ms is not None:
            observed_at_ms = _non_negative_int(
                observed_at_ms,
                label="first_execution_observed_at_ms",
            )
        latency_ms = _non_negative_int(
            self.effective_signal_to_execution_latency_ms,
            label="effective_signal_to_execution_latency_ms",
        )
        if not isinstance(
            self.forecast_availability_basis,
            V07ForecastAvailabilityBasis,
        ):
            raise TypeError(
                "forecast_availability_basis must be V07ForecastAvailabilityBasis"
            )
        if available_at_ms < decision_at_ms:
            raise ValueError("forecast cannot become available before decision")
        if available_at_ms >= expiry_ms:
            raise ValueError("forecast must become available before common expiry")
        expected_first_not_before_ms = available_at_ms + CAPTURED_TAKER_DELAY_MS
        if first_not_before_ms != expected_first_not_before_ms:
            raise ValueError(
                "execution eligibility must equal forecast availability plus "
                "the captured taker delay"
            )
        if observed_at_ms is not None and observed_at_ms < first_not_before_ms:
            raise ValueError("first execution surface predates execution eligibility")
        effective_at_ms = (
            first_not_before_ms if observed_at_ms is None else observed_at_ms
        )
        if latency_ms != effective_at_ms - decision_at_ms:
            raise ValueError("effective signal-to-execution latency is inconsistent")
        object.__setattr__(self, "net_pnl", _decimal(self.net_pnl, label="net_pnl"))
        strength = self.signal_strength
        if strength is not None:
            strength = _decimal(strength, label="signal_strength")
            if strength < ZERO:
                raise ValueError("signal_strength must be non-negative")
            object.__setattr__(self, "signal_strength", strength)
        for name in (
            "explainable",
            "complete_cost_evidence",
            "complete_execution_evidence",
            "complete_settlement_evidence",
            "immutable_public_capture_evidence",
            "economic_attempt",
            "causal_no_fill",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if not isinstance(self.lock_provenance, PreLabelLockProvenance):
            raise TypeError("lock_provenance must be PreLabelLockProvenance")
        if self.lock_provenance.verified:
            receipt_at_ms = self.lock_provenance.prediction_received_at_ms
            assert receipt_at_ms is not None
            if self.forecast_availability_basis is not (
                V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
            ):
                raise ValueError(
                    "verified attempt requires immutable-receipt availability basis"
                )
            if available_at_ms != receipt_at_ms:
                raise ValueError(
                    "verified attempt availability must equal prediction receipt time"
                )
            if receipt_at_ms >= expiry_ms:
                raise ValueError("prediction receipt must predate the common expiry")
        elif self.forecast_availability_basis is not (
            V07ForecastAvailabilityBasis.PREREGISTERED_COUNTERFACTUAL_DELAY
        ):
            raise ValueError(
                "counterfactual attempt requires preregistered-delay availability basis"
            )
        if not isinstance(self.edge_basis, V07EdgeBasis):
            raise TypeError("edge_basis must be V07EdgeBasis")
        if not isinstance(self.structural_quantity_executable, bool):
            raise TypeError("structural_quantity_executable must be bool")
        if not isinstance(self.quantity_selection_basis, V07QuantitySelectionBasis):
            raise TypeError(
                "quantity_selection_basis must be V07QuantitySelectionBasis"
            )
        _non_negative_int(
            self.quantity_candidate_breakpoint_count,
            label="quantity_candidate_breakpoint_count",
        )
        if not isinstance(self.probability_diagnostics_applicable, bool):
            raise TypeError("probability_diagnostics_applicable must be bool")
        for name in (
            "edge_evaluation_quantity",
            "structural_worst_case_payoff_per_pair",
            "structural_net_floor_per_pair",
            "selected_guaranteed_total_pnl",
            "selected_uncertainty_adjusted_total_pnl",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, label=name))
        if (
            self.edge_evaluation_quantity is not None
            and self.edge_evaluation_quantity <= ZERO
        ):
            raise ValueError("edge_evaluation_quantity must be positive")
        if self.edge_basis is V07EdgeBasis.STRUCTURAL:
            if (
                not self.structural_quantity_executable
                or self.structural_net_floor_per_pair is None
                or self.structural_net_floor_per_pair <= ZERO
                or self.quantity_selection_basis
                is not (V07QuantitySelectionBasis.STRUCTURAL_MAX_GUARANTEED_TOTAL_PNL)
                or self.selected_guaranteed_total_pnl is None
                or self.selected_guaranteed_total_pnl <= ZERO
            ):
                raise ValueError(
                    "structural attempt basis requires executable positive floor"
                )
            if self.edge_evaluation_quantity is None:
                raise ValueError("structural attempt requires evaluated quantity")
            if self.selected_guaranteed_total_pnl != (
                self.edge_evaluation_quantity * self.structural_net_floor_per_pair
            ):
                raise ValueError(
                    "structural attempt guaranteed total does not match quantity/floor"
                )
            if strength is not None:
                raise ValueError("structural attempt signal_strength must be null")
            if self.probability_diagnostics_applicable:
                raise ValueError(
                    "structural attempt cannot claim predictive probability diagnostics"
                )
            if self.selected_uncertainty_adjusted_total_pnl is not None:
                raise ValueError(
                    "structural attempt predictive sizing objective must be null"
                )
        elif self.edge_basis is V07EdgeBasis.PREDICTIVE:
            if self.edge_evaluation_quantity is None:
                raise ValueError("predictive attempt requires evaluated quantity")
            if strength is None:
                raise ValueError("predictive attempt requires signal_strength")
            if self.quantity_selection_basis is not (
                V07QuantitySelectionBasis.PREDICTIVE_MAX_UNCERTAINTY_ADJUSTED_TOTAL_PNL
            ):
                raise ValueError("predictive attempt has the wrong sizing objective")
            if self.selected_uncertainty_adjusted_total_pnl is None:
                raise ValueError("predictive attempt requires adjusted total PnL")
            if not self.probability_diagnostics_applicable:
                raise ValueError("predictive attempt requires probability diagnostics")
        if self.causal_no_fill:
            if self.economic_attempt:
                raise ValueError("causal no-fill cannot count as an economic attempt")
            if self.net_pnl != ZERO:
                raise ValueError("causal no-fill must reconcile to zero PnL")
            if self.execution_status != "no_fill":
                raise ValueError("causal no-fill must have no_fill execution status")
            if self.reconciliation_status != "causal_no_fill_zero_pnl":
                raise ValueError("causal no-fill reconciliation status is invalid")
            if self.explainable:
                raise ValueError("causal no-fill is not an economic fill attempt")
        elif not self.economic_attempt:
            raise ValueError("non-economic reconciliation must be a causal no-fill")

    @property
    def identity(self) -> tuple[str, int]:
        return self.expiry_cluster_id, self.decision_tau_seconds

    @property
    def canonical_pair_id(self) -> str:
        return self.event_cluster_id

    @property
    def expiry_cluster_id(self) -> str:
        return canonical_expiry_cluster_id(self.expiry_ms)

    def to_document(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "canonical_pair_id": self.canonical_pair_id,
            "expiry_ms": self.expiry_ms,
            "expiry_cluster_id": self.expiry_cluster_id,
            "event_cluster_alias": self.event_cluster_alias,
            "decision_tau_seconds": self.decision_tau_seconds,
            "decision_at_ms": self.decision_at_ms,
            "forecast_available_at_ms": self.forecast_available_at_ms,
            "forecast_availability_basis": self.forecast_availability_basis.value,
            "first_execution_not_before_ms": self.first_execution_not_before_ms,
            "first_execution_observed_at_ms": self.first_execution_observed_at_ms,
            "effective_signal_to_execution_latency_ms": (
                self.effective_signal_to_execution_latency_ms
            ),
            "decision_action": self.decision_action,
            "net_pnl": _decimal_text(self.net_pnl),
            "economic_attempt": self.economic_attempt,
            "causal_no_fill": self.causal_no_fill,
            "reconciliation_status": self.reconciliation_status,
            "explainable": self.explainable,
            "complete_cost_evidence": self.complete_cost_evidence,
            "complete_execution_evidence": self.complete_execution_evidence,
            "complete_settlement_evidence": self.complete_settlement_evidence,
            "immutable_public_capture_evidence": (
                self.immutable_public_capture_evidence
            ),
            "signal_strength": _decimal_text(self.signal_strength),
            "execution_status": self.execution_status,
            "edge_basis": self.edge_basis.value,
            "edge_evaluation_quantity": _decimal_text(self.edge_evaluation_quantity),
            "structural_worst_case_payoff_per_pair": _decimal_text(
                self.structural_worst_case_payoff_per_pair
            ),
            "structural_net_floor_per_pair": _decimal_text(
                self.structural_net_floor_per_pair
            ),
            "structural_quantity_executable": self.structural_quantity_executable,
            "quantity_selection_basis": self.quantity_selection_basis.value,
            "quantity_candidate_breakpoint_count": (
                self.quantity_candidate_breakpoint_count
            ),
            "selected_guaranteed_total_pnl": _decimal_text(
                self.selected_guaranteed_total_pnl
            ),
            "selected_uncertainty_adjusted_total_pnl": _decimal_text(
                self.selected_uncertainty_adjusted_total_pnl
            ),
            "probability_diagnostics_applicable": (
                self.probability_diagnostics_applicable
            ),
            "lock_provenance": self.lock_provenance.to_document(),
        }


@dataclass(frozen=True)
class ParameterNeighborhoodEvidence:
    """Diagnostic-only neighboring settings; never changes recorded actions."""

    net_pnl_by_setting: Mapping[str, Decimal | None]
    required_setting_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        required = tuple(self.required_setting_ids)
        if not required or any(
            not isinstance(item, str) or not item for item in required
        ):
            raise ValueError("required_setting_ids must be non-empty strings")
        if len(set(required)) != len(required):
            raise ValueError("required_setting_ids must be unique")
        parsed: dict[str, Decimal | None] = {}
        for key, value in self.net_pnl_by_setting.items():
            if not isinstance(key, str) or not key:
                raise ValueError("neighborhood setting id must be non-empty")
            parsed[key] = None if value is None else _decimal(value, label=key)
        object.__setattr__(self, "required_setting_ids", required)
        object.__setattr__(self, "net_pnl_by_setting", MappingProxyType(parsed))

    @property
    def complete(self) -> bool:
        return all(
            setting in self.net_pnl_by_setting
            and self.net_pnl_by_setting[setting] is not None
            for setting in self.required_setting_ids
        )

    @property
    def stable_positive(self) -> bool:
        return self.complete and all(
            self.net_pnl_by_setting[setting] is not None
            and self.net_pnl_by_setting[setting] > ZERO
            for setting in self.required_setting_ids
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-5m-15m-v07-parameter-neighborhood.v1",
            "diagnostic_only": True,
            "action_changing": False,
            "required_setting_ids": list(self.required_setting_ids),
            "net_pnl_by_setting": {
                key: _decimal_text(value)
                for key, value in sorted(self.net_pnl_by_setting.items())
            },
            "complete": self.complete,
            "stable_positive": self.stable_positive,
        }


def _hash_documents(documents: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(documents))).hexdigest()


def _forecast_rows_sha256(rows: Sequence[LockedOOSForecastRow]) -> str:
    ordered = sorted(
        (row.to_document() for row in rows),
        key=lambda item: (
            item["expiry_ms"],
            item["decision_tau_seconds"],
            item["canonical_pair_id"],
        ),
    )
    return _hash_documents(ordered)


def _attempt_rows_sha256(rows: Sequence[LockedOOSEconomicAttempt]) -> str:
    ordered = sorted(
        (row.to_document() for row in rows),
        key=lambda item: item["attempt_id"],
    )
    return _hash_documents(ordered)


def _neighborhood_sha256(
    evidence: ParameterNeighborhoodEvidence | None,
) -> str:
    document: Mapping[str, Any] = (
        {"parameter_neighborhood": None} if evidence is None else evidence.to_document()
    )
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


@dataclass(frozen=True, init=False)
class _BuilderVerifiedEvidence:
    """Opaque capability binding evaluated rows to a verified builder chain."""

    preregistration_sha256: str
    test_universe_sha256: str
    verification_chain_sha256: str
    forecast_rows_sha256: str
    attempt_rows_sha256: str
    parameter_neighborhood_sha256: str
    _issuer_token: object = field(repr=False, compare=False)

    @property
    def verified(self) -> bool:
        return self._issuer_token is _BUILDER_EVIDENCE_ISSUER

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-5m-15m-v07-builder-verified-evidence.v4",
            "builder_id": _BUILDER_ID,
            "source_baseline": _SOURCE_BASELINE,
            "preregistration_sha256": self.preregistration_sha256,
            "test_universe_sha256": self.test_universe_sha256,
            "verification_chain_sha256": self.verification_chain_sha256,
            "forecast_rows_sha256": self.forecast_rows_sha256,
            "attempt_rows_sha256": self.attempt_rows_sha256,
            "parameter_neighborhood_sha256": (self.parameter_neighborhood_sha256),
            "opaque_builder_capability_verified": self.verified,
        }


_VERIFICATION_CHAIN_KEYS = frozenset(
    {
        "schema_version",
        "builder_id",
        "source_baseline",
        "preregistration_sha256",
        "capture_config_schema_version",
        "strict_capture_config_count",
        "generated_fixture_count",
        "immutable_capture_count",
        "test_context_count",
        "forecast_count",
        "reconciliation_count",
        "predictive_forecast_count",
        "structural_forecast_count",
        "non_edge_forecast_count",
        "predictive_reconciliation_count",
        "structural_reconciliation_count",
        "structural_executable_positive_floor_reconciliation_count",
        "preexpiry_prediction_receipt_count",
        "receipt_timed_replay_count",
        "counterfactual_timing_count",
        "test_universe_sha256",
        "prelabel_lock_journal_identity_sha256",
        "capture_config_and_tree_verification_sha256",
        "cycle_reconciliation_sha256",
    }
)


def _issue_builder_verified_evidence(
    *,
    forecasts: Sequence[LockedOOSForecastRow],
    economic_attempts: Sequence[LockedOOSEconomicAttempt],
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
    verification_chain: Mapping[str, Any],
) -> _BuilderVerifiedEvidence:
    """Bind rows to builder-verified captures, journal receipts, and replay."""

    chain = dict(verification_chain)
    if set(chain) != _VERIFICATION_CHAIN_KEYS:
        raise ValueError("builder verification chain has an invalid field set")
    if chain["schema_version"] != ("btc-5m-15m-v07-builder-verification-chain.v4"):
        raise ValueError("builder verification chain schema mismatch")
    if chain["builder_id"] != _BUILDER_ID:
        raise ValueError("builder verification chain issuer mismatch")
    if chain["source_baseline"] != _SOURCE_BASELINE:
        raise ValueError("builder verification chain baseline mismatch")
    if chain["capture_config_schema_version"] != _CAPTURE_CONFIG_SCHEMA_VERSION:
        raise ValueError("builder verification chain capture schema mismatch")
    preregistration_sha256 = _sha256(
        chain["preregistration_sha256"],
        label="preregistration_sha256",
    )
    test_universe_sha256 = _sha256(
        chain["test_universe_sha256"],
        label="test_universe_sha256",
    )
    for name in (
        "prelabel_lock_journal_identity_sha256",
        "capture_config_and_tree_verification_sha256",
        "cycle_reconciliation_sha256",
    ):
        _sha256(chain[name], label=name)
    for name in (
        "strict_capture_config_count",
        "generated_fixture_count",
        "immutable_capture_count",
        "test_context_count",
        "forecast_count",
        "reconciliation_count",
        "predictive_forecast_count",
        "structural_forecast_count",
        "non_edge_forecast_count",
        "predictive_reconciliation_count",
        "structural_reconciliation_count",
        "structural_executable_positive_floor_reconciliation_count",
        "preexpiry_prediction_receipt_count",
        "receipt_timed_replay_count",
        "counterfactual_timing_count",
    ):
        _non_negative_int(chain[name], label=name)
    if chain["generated_fixture_count"] != 0:
        raise ValueError("generated fixtures cannot receive verified evidence")
    if chain["test_context_count"] <= 0:
        raise ValueError("verified builder evidence requires test contexts")
    if chain["strict_capture_config_count"] != chain["test_context_count"]:
        raise ValueError("not every test context has a strict capture config")
    if chain["immutable_capture_count"] != chain["test_context_count"]:
        raise ValueError("not every test context has immutable capture evidence")
    frozen_forecasts = tuple(forecasts)
    frozen_attempts = tuple(economic_attempts)
    if chain["forecast_count"] != len(frozen_forecasts):
        raise ValueError("builder verification forecast count mismatch")
    if chain["reconciliation_count"] != len(frozen_attempts):
        raise ValueError("builder verification reconciliation count mismatch")
    predictive_forecast_count = sum(
        row.edge_basis is V07EdgeBasis.PREDICTIVE for row in frozen_forecasts
    )
    structural_forecast_count = sum(
        row.edge_basis is V07EdgeBasis.STRUCTURAL for row in frozen_forecasts
    )
    non_edge_forecast_count = sum(
        row.edge_basis is V07EdgeBasis.NONE for row in frozen_forecasts
    )
    if chain["predictive_forecast_count"] != predictive_forecast_count:
        raise ValueError("builder predictive forecast count mismatch")
    if chain["structural_forecast_count"] != structural_forecast_count:
        raise ValueError("builder structural forecast count mismatch")
    if chain["non_edge_forecast_count"] != non_edge_forecast_count:
        raise ValueError("builder non-edge forecast count mismatch")
    if (
        predictive_forecast_count + structural_forecast_count + non_edge_forecast_count
        != len(frozen_forecasts)
    ):
        raise ValueError("builder forecast edge-basis partition is incomplete")
    predictive_reconciliation_count = sum(
        row.edge_basis is V07EdgeBasis.PREDICTIVE for row in frozen_attempts
    )
    structural_reconciliation_count = sum(
        row.edge_basis is V07EdgeBasis.STRUCTURAL for row in frozen_attempts
    )
    if chain["predictive_reconciliation_count"] != (predictive_reconciliation_count):
        raise ValueError("builder predictive reconciliation count mismatch")
    if chain["structural_reconciliation_count"] != (structural_reconciliation_count):
        raise ValueError("builder structural reconciliation count mismatch")
    if predictive_reconciliation_count + structural_reconciliation_count != len(
        frozen_attempts
    ):
        raise ValueError("builder reconciliation edge-basis partition is incomplete")
    structural_floor_count = sum(
        row.edge_basis is V07EdgeBasis.STRUCTURAL
        and row.structural_quantity_executable
        and row.structural_net_floor_per_pair is not None
        and row.structural_net_floor_per_pair > ZERO
        and row.selected_guaranteed_total_pnl is not None
        and row.selected_guaranteed_total_pnl > ZERO
        for row in frozen_attempts
    )
    if (
        chain["structural_executable_positive_floor_reconciliation_count"]
        != structural_floor_count
    ):
        raise ValueError("builder structural positive-floor count mismatch")
    if structural_floor_count != structural_reconciliation_count:
        raise ValueError(
            "every structural reconciliation must bind an executable positive floor"
        )
    if chain["preexpiry_prediction_receipt_count"] != len(frozen_forecasts):
        raise ValueError("not every forecast has a pre-expiry prediction receipt")
    if chain["receipt_timed_replay_count"] != len(frozen_attempts):
        raise ValueError("not every reconciliation is receipt-timed")
    if chain["counterfactual_timing_count"] != 0:
        raise ValueError("builder-verified evidence cannot use counterfactual timing")
    if not frozen_forecasts:
        raise ValueError("verified builder evidence requires forecast rows")
    if any(not row.lock_provenance.verified for row in frozen_forecasts):
        raise ValueError("forecast rows lack builder-issued pre-label receipts")
    if any(not row.lock_provenance.verified for row in frozen_attempts):
        raise ValueError("reconciliation rows lack builder-issued pre-label receipts")
    if any(
        row.forecast_availability_basis
        is not V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
        or row.forecast_available_at_ms >= row.expiry_ms
        or row.lock_provenance.prediction_received_at_ms != row.forecast_available_at_ms
        for row in frozen_forecasts
    ):
        raise ValueError("forecast rows are not pre-expiry receipt-timed")
    if any(
        row.forecast_availability_basis
        is not V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
        or row.forecast_available_at_ms >= row.expiry_ms
        or row.first_execution_not_before_ms
        != row.forecast_available_at_ms + CAPTURED_TAKER_DELAY_MS
        for row in frozen_attempts
    ):
        raise ValueError("reconciliation rows are not receipt-timed")
    universe_hashes = {
        row.lock_provenance.test_universe_sha256 for row in frozen_forecasts
    }
    universe_hashes.update(
        row.lock_provenance.test_universe_sha256 for row in frozen_attempts
    )
    if universe_hashes != {test_universe_sha256}:
        raise ValueError("verified rows do not bind to one test universe")
    instance = object.__new__(_BuilderVerifiedEvidence)
    values = {
        "preregistration_sha256": preregistration_sha256,
        "test_universe_sha256": test_universe_sha256,
        "verification_chain_sha256": hashlib.sha256(
            canonical_json_bytes(chain)
        ).hexdigest(),
        "forecast_rows_sha256": _forecast_rows_sha256(frozen_forecasts),
        "attempt_rows_sha256": _attempt_rows_sha256(frozen_attempts),
        "parameter_neighborhood_sha256": _neighborhood_sha256(parameter_neighborhood),
        "_issuer_token": _BUILDER_EVIDENCE_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


def _validate_builder_verified_evidence(
    evidence: _BuilderVerifiedEvidence,
    *,
    forecasts: Sequence[LockedOOSForecastRow],
    economic_attempts: Sequence[LockedOOSEconomicAttempt],
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
) -> None:
    if not isinstance(evidence, _BuilderVerifiedEvidence) or not evidence.verified:
        raise TypeError("builder evidence capability is invalid")
    if evidence.forecast_rows_sha256 != _forecast_rows_sha256(forecasts):
        raise ValueError("builder evidence does not bind the forecast rows")
    if evidence.attempt_rows_sha256 != _attempt_rows_sha256(economic_attempts):
        raise ValueError("builder evidence does not bind the reconciliation rows")
    if evidence.parameter_neighborhood_sha256 != _neighborhood_sha256(
        parameter_neighborhood
    ):
        raise ValueError("builder evidence does not bind the parameter neighborhood")


class V07EvidenceStatus(str, Enum):
    INSUFFICIENT_DATA = "insufficient_data"
    COUNTERFACTUAL_INSUFFICIENT = "counterfactual_insufficient"
    NOT_PROFITABLE = "not_profitable"
    POSITIVE_BUT_NOT_TRUE_EDGE = "positive_but_not_true_edge"
    TRUE_EDGE_GATE_SATISFIED = "true_edge_gate_satisfied"


@dataclass(frozen=True)
class V07LockedOOSEvaluation:
    status: V07EvidenceStatus
    settled_expiry_cluster_count: int
    settled_forecast_cluster_count: int
    reconciled_actionable_decision_count: int
    causal_no_fill_count: int
    economic_attempt_count: int
    explainable_economic_attempt_count: int
    net_pnl: Decimal
    qualified_net_pnl: Decimal | None
    predictive_settled_expiry_cluster_count: int
    predictive_settled_forecast_cluster_count: int
    predictive_economic_attempt_count: int
    predictive_explainable_economic_attempt_count: int
    predictive_net_pnl: Decimal
    predictive_qualified_net_pnl: Decimal | None
    predictive_true_edge_gate_satisfied: bool
    structural_settled_expiry_cluster_count: int
    structural_settled_forecast_cluster_count: int
    structural_economic_attempt_count: int
    structural_explainable_economic_attempt_count: int
    structural_net_pnl: Decimal
    structural_qualified_net_pnl: Decimal | None
    structural_true_edge_gate_satisfied: bool
    diagnostic_positive_sample_pnl: bool
    predictive_diagnostic_positive_sample_pnl: bool
    structural_diagnostic_positive_sample_pnl: bool
    positive_net_pnl_user_check_passed: bool
    true_edge_gate_satisfied: bool
    auditable_prelabel_lock_evidence: bool
    builder_verified_evidence_chain: bool
    builder_verification_sha256: str | None
    bootstrap_cluster_mean_lower_95: Decimal | None
    predictive_bootstrap_cluster_mean_lower_95: Decimal | None
    structural_bootstrap_cluster_mean_lower_95: Decimal | None
    largest_positive_cluster_to_total_net_pnl: Decimal | None
    largest_positive_cluster_to_total_positive_cluster_pnl: Decimal | None
    maximum_absolute_cluster_contribution_share: Decimal | None
    structural_largest_positive_cluster_to_total_net_pnl: Decimal | None
    structural_largest_positive_cluster_to_total_positive_cluster_pnl: Decimal | None
    structural_maximum_absolute_cluster_contribution_share: Decimal | None
    forecast_metrics: Mapping[str, Mapping[str, Decimal | None]]
    structural_forecast_metrics: Mapping[str, Mapping[str, Decimal | None]]
    complete_cost_and_execution_evidence: bool
    predictive_complete_cost_and_execution_evidence: bool
    structural_complete_cost_and_execution_evidence: bool
    parameter_neighborhood: ParameterNeighborhoodEvidence | None
    reason_codes: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        predictive_bootstrap = self.predictive_bootstrap_cluster_mean_lower_95
        predictive_binding = self.largest_positive_cluster_to_total_net_pnl
        return {
            "schema_version": "btc-5m-15m-v07-locked-oos-evaluation.v6",
            "status": self.status.value,
            "qualification_basis": "independent_predictive_or_structural_gate",
            "builder_verified_evidence": {
                "verified_chain_present": self.builder_verified_evidence_chain,
                "verification_sha256": self.builder_verification_sha256,
                "caller_supplied_rows_or_booleans_are_not_qualification_authority": (
                    True
                ),
            },
            "sample_counts": {
                "settled_expiry_clusters": self.settled_expiry_cluster_count,
                "settled_forecast_clusters": self.settled_forecast_cluster_count,
                "settled_expiry_cluster_definition": (
                    "distinct capture-derived common expiry keys with at least one "
                    "explainable economic attempt"
                ),
                "reconciled_actionable_decisions": (
                    self.reconciled_actionable_decision_count
                ),
                "causally_demonstrated_no_fills": self.causal_no_fill_count,
                "economic_attempts": self.economic_attempt_count,
                "explainable_economic_attempts": (
                    self.explainable_economic_attempt_count
                ),
                "minimum_settled_expiry_clusters_per_edge_basis": (
                    MINIMUM_SETTLED_CLUSTERS
                ),
                "minimum_explainable_economic_attempts_per_edge_basis": (
                    MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
                ),
                "mixed_predictive_and_structural_attempts_cannot_be_combined": True,
                "tau_rows_do_not_count_as_independent_clusters": True,
                "pair_hashes_do_not_count_as_independent_clusters": True,
                "no_fill_rows_do_not_count_as_economic_attempts": True,
            },
            "net_pnl": _decimal_text(self.net_pnl),
            "qualified_net_pnl": _decimal_text(self.qualified_net_pnl),
            "predictive_qualified_net_pnl": _decimal_text(
                self.predictive_qualified_net_pnl
            ),
            "structural_qualified_net_pnl": _decimal_text(
                self.structural_qualified_net_pnl
            ),
            "edge_basis_breakdown": {
                "predictive": {
                    "settled_expiry_clusters": (
                        self.predictive_settled_expiry_cluster_count
                    ),
                    "settled_forecast_clusters": (
                        self.predictive_settled_forecast_cluster_count
                    ),
                    "economic_attempts": self.predictive_economic_attempt_count,
                    "explainable_economic_attempts": (
                        self.predictive_explainable_economic_attempt_count
                    ),
                    "net_pnl": _decimal_text(self.predictive_net_pnl),
                    "qualified_net_pnl": _decimal_text(
                        self.predictive_qualified_net_pnl
                    ),
                    "diagnostic_positive_sample_pnl": (
                        self.predictive_diagnostic_positive_sample_pnl
                    ),
                    "true_edge_gate_satisfied": (
                        self.predictive_true_edge_gate_satisfied
                    ),
                    "complete_cost_execution_settlement_evidence": (
                        self.predictive_complete_cost_and_execution_evidence
                    ),
                    "bootstrap_mean_lower_95": _decimal_text(predictive_bootstrap),
                    "largest_positive_cluster_to_total_net_pnl": (
                        _decimal_text(predictive_binding)
                    ),
                    "requires_brier_ece_and_neighborhood_gates": True,
                },
                "structural": {
                    "settled_expiry_clusters": (
                        self.structural_settled_expiry_cluster_count
                    ),
                    "settled_forecast_clusters": (
                        self.structural_settled_forecast_cluster_count
                    ),
                    "economic_attempts": self.structural_economic_attempt_count,
                    "explainable_economic_attempts": (
                        self.structural_explainable_economic_attempt_count
                    ),
                    "net_pnl": _decimal_text(self.structural_net_pnl),
                    "qualified_net_pnl": _decimal_text(
                        self.structural_qualified_net_pnl
                    ),
                    "diagnostic_positive_sample_pnl": (
                        self.structural_diagnostic_positive_sample_pnl
                    ),
                    "true_edge_gate_satisfied": (
                        self.structural_true_edge_gate_satisfied
                    ),
                    "complete_cost_execution_settlement_evidence": (
                        self.structural_complete_cost_and_execution_evidence
                    ),
                    "bootstrap_mean_lower_95": _decimal_text(
                        self.structural_bootstrap_cluster_mean_lower_95
                    ),
                    "largest_positive_cluster_to_total_net_pnl": _decimal_text(
                        self.structural_largest_positive_cluster_to_total_net_pnl
                    ),
                    "requires_decision_time_executable_positive_floor_on_every_row": (
                        True
                    ),
                    "requires_brier_ece_or_model_neighborhood": False,
                    "theoretical_floor_does_not_replace_realized_execution_pnl": True,
                },
            },
            "diagnostic_positive_sample_pnl": self.diagnostic_positive_sample_pnl,
            "diagnostic_positive_sample_pnl_is_qualification": False,
            "positive_net_pnl_user_check_passed": (
                self.positive_net_pnl_user_check_passed
            ),
            "positive_net_pnl_user_check_requires_a_true_edge_track": True,
            "predictive_true_edge": self.predictive_true_edge_gate_satisfied,
            "predictive_true_edge_gate_satisfied": (
                self.predictive_true_edge_gate_satisfied
            ),
            "structural_true_edge": self.structural_true_edge_gate_satisfied,
            "structural_true_edge_gate_satisfied": (
                self.structural_true_edge_gate_satisfied
            ),
            "true_edge_gate_satisfied": self.true_edge_gate_satisfied,
            "auditable_prelabel_manifest_prediction_decision_lock": (
                self.auditable_prelabel_lock_evidence
            ),
            "bootstrap": {
                "unit": "capture_derived_common_expiry_cluster",
                "resamples": BOOTSTRAP_RESAMPLES,
                "seed": BOOTSTRAP_SEED,
                "statistic": "mean_cluster_net_pnl",
                "zero_pnl_causal_no_fills_retained": True,
                "predictive_mean_net_pnl_lower_95": _decimal_text(predictive_bootstrap),
                "structural_mean_net_pnl_lower_95": _decimal_text(
                    self.structural_bootstrap_cluster_mean_lower_95
                ),
            },
            "concentration": {
                "limit": _decimal_text(CONCENTRATION_LIMIT),
                "binding_numerator": "largest_positive_common_expiry_net_pnl",
                "binding_denominator": "total_net_pnl_within_edge_basis",
                "predictive": {
                    "binding_largest_positive_cluster_to_total_net_pnl": (
                        _decimal_text(predictive_binding)
                    ),
                    "diagnostic_largest_positive_cluster_to_total_positive": (
                        _decimal_text(
                            self.largest_positive_cluster_to_total_positive_cluster_pnl
                        )
                    ),
                    "diagnostic_largest_absolute_cluster_to_total_absolute": (
                        _decimal_text(self.maximum_absolute_cluster_contribution_share)
                    ),
                },
                "structural": {
                    "binding_largest_positive_cluster_to_total_net_pnl": (
                        _decimal_text(
                            self.structural_largest_positive_cluster_to_total_net_pnl
                        )
                    ),
                    "diagnostic_largest_positive_cluster_to_total_positive": (
                        _decimal_text(
                            self.structural_largest_positive_cluster_to_total_positive_cluster_pnl
                        )
                    ),
                    "diagnostic_largest_absolute_cluster_to_total_absolute": (
                        _decimal_text(
                            self.structural_maximum_absolute_cluster_contribution_share
                        )
                    ),
                },
            },
            "forecast_metric_method": {
                "predictive_only": True,
                "brier_weighting": ("equal_common_expiry_then_equal_tau_within_expiry"),
                "binding_market_baseline": (
                    "fixed_size_depth_walk_fee_inclusive_then_shared_terminal_projection"
                ),
                "raw_top_ask_probabilities_are_diagnostic_only": True,
                "ece_bins": ECE_BINS,
                "canonical_pair_hash_role": "market_condition_integrity_only",
            },
            "forecast_metrics": {
                horizon: {key: _decimal_text(value) for key, value in metrics.items()}
                for horizon, metrics in self.forecast_metrics.items()
            },
            "structural_forecast_metrics_non_applicable": {
                horizon: {key: _decimal_text(value) for key, value in metrics.items()}
                for horizon, metrics in self.structural_forecast_metrics.items()
            },
            "complete_cost_execution_and_settlement_evidence": (
                self.complete_cost_and_execution_evidence
            ),
            "parameter_neighborhood": (
                None
                if self.parameter_neighborhood is None
                else self.parameter_neighborhood.to_document()
            ),
            "reason_codes": list(self.reason_codes),
            "paper_only": True,
            "non_promotional": True,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        }


def _validated_forecasts(
    rows: Sequence[LockedOOSForecastRow],
) -> tuple[LockedOOSForecastRow, ...]:
    frozen = tuple(rows)
    if any(not isinstance(row, LockedOOSForecastRow) for row in frozen):
        raise TypeError("forecast rows must be LockedOOSForecastRow")
    identities = tuple(row.identity for row in frozen)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate locked-OOS forecast common-expiry/tau identity")
    outcomes_by_cluster: dict[str, tuple[bool, bool]] = {}
    aliases_by_cluster: dict[str, str] = {}
    cluster_by_alias: dict[str, str] = {}
    pair_by_expiry: dict[str, str] = {}
    expiry_by_pair: dict[str, str] = {}
    universe_hashes: set[str] = set()
    for row in frozen:
        expiry_cluster_id = row.expiry_cluster_id
        actual = row.actual_5_up, row.actual_15_up
        previous = outcomes_by_cluster.setdefault(expiry_cluster_id, actual)
        if previous != actual:
            raise ValueError("one common expiry has conflicting outcomes")
        prior_pair = pair_by_expiry.setdefault(
            expiry_cluster_id,
            row.canonical_pair_id,
        )
        if prior_pair != row.canonical_pair_id:
            raise ValueError("one common expiry maps to multiple market pairs")
        prior_expiry = expiry_by_pair.setdefault(
            row.canonical_pair_id,
            expiry_cluster_id,
        )
        if prior_expiry != expiry_cluster_id:
            raise ValueError("one market pair maps to multiple common expiries")
        prior_alias = aliases_by_cluster.setdefault(
            expiry_cluster_id,
            row.event_cluster_alias,
        )
        if prior_alias != row.event_cluster_alias:
            raise ValueError("one common expiry has multiple manifest aliases")
        prior_cluster = cluster_by_alias.setdefault(
            row.event_cluster_alias,
            expiry_cluster_id,
        )
        if prior_cluster != expiry_cluster_id:
            raise ValueError("one manifest alias maps to multiple common expiries")
        if row.lock_provenance.test_universe_sha256 is not None:
            universe_hashes.add(row.lock_provenance.test_universe_sha256)
    if len(universe_hashes) > 1:
        raise ValueError("locked forecast rows reference different test universes")
    return frozen


def _cluster_equal_brier(
    rows: Sequence[LockedOOSForecastRow],
    *,
    horizon: str,
    baseline: bool,
) -> Decimal | None:
    if not rows:
        return None
    by_cluster: dict[str, list[Decimal]] = {}
    for row in rows:
        market_5, market_15 = row.coherent_market_marginals
        if horizon == "5m":
            probability = market_5 if baseline else row.forecast_q_5_up
            actual = ONE if row.actual_5_up else ZERO
        else:
            probability = market_15 if baseline else row.forecast_q_15_up
            actual = ONE if row.actual_15_up else ZERO
        if probability is None:
            raise ValueError("forecast metric row lacks predictive probabilities")
        by_cluster.setdefault(row.expiry_cluster_id, []).append(
            (probability - actual) ** 2
        )
    cluster_scores = [
        sum(values, ZERO) / Decimal(len(values)) for values in by_cluster.values()
    ]
    return sum(cluster_scores, ZERO) / Decimal(len(cluster_scores))


def _cluster_equal_ece(
    rows: Sequence[LockedOOSForecastRow],
    *,
    horizon: str,
    bins: int = ECE_BINS,
) -> Decimal | None:
    if not rows:
        return None
    if bins <= 0:
        raise ValueError("bins must be positive")
    counts_by_cluster: dict[str, int] = {}
    for row in rows:
        counts_by_cluster[row.expiry_cluster_id] = (
            counts_by_cluster.get(row.expiry_cluster_id, 0) + 1
        )
    cluster_count = Decimal(len(counts_by_cluster))
    buckets: list[list[tuple[Decimal, Decimal, Decimal]]] = [[] for _ in range(bins)]
    for row in rows:
        probability = row.forecast_q_5_up if horizon == "5m" else row.forecast_q_15_up
        if probability is None:
            raise ValueError("forecast metric row lacks predictive probabilities")
        actual = (
            ONE if (row.actual_5_up if horizon == "5m" else row.actual_15_up) else ZERO
        )
        weight = ONE / (
            cluster_count * Decimal(counts_by_cluster[row.expiry_cluster_id])
        )
        index = min(
            bins - 1,
            int((probability * Decimal(bins)).to_integral_value(rounding=ROUND_FLOOR)),
        )
        buckets[index].append((probability, actual, weight))
    error = ZERO
    for bucket in buckets:
        if not bucket:
            continue
        total_weight = sum((item[2] for item in bucket), ZERO)
        mean_probability = (
            sum(
                (probability * weight for probability, _, weight in bucket),
                ZERO,
            )
            / total_weight
        )
        realized = (
            sum(
                (actual * weight for _, actual, weight in bucket),
                ZERO,
            )
            / total_weight
        )
        error += total_weight * abs(mean_probability - realized)
    return error


def _cluster_pnl(
    reconciliations: Sequence[LockedOOSEconomicAttempt],
) -> Mapping[str, Decimal]:
    by_cluster: dict[str, Decimal] = {}
    for row in reconciliations:
        by_cluster[row.expiry_cluster_id] = (
            by_cluster.get(row.expiry_cluster_id, ZERO) + row.net_pnl
        )
    return MappingProxyType(by_cluster)


def _bootstrap_cluster_mean_lower_95(
    reconciliations: Sequence[LockedOOSEconomicAttempt],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> Decimal | None:
    if not reconciliations:
        return None
    values = tuple(_cluster_pnl(reconciliations).values())
    rng = random.Random(seed)
    means: list[Decimal] = []
    for _ in range(resamples):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample, ZERO) / Decimal(len(sample)))
    means.sort()
    lower_index = max(
        0,
        int(
            (Decimal(resamples) * Decimal("0.05")).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        - 1,
    )
    return means[lower_index]


def _cluster_concentration_ratios(
    reconciliations: Sequence[LockedOOSEconomicAttempt],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if not reconciliations:
        return None, None, None
    values = tuple(_cluster_pnl(reconciliations).values())
    net_pnl = sum(values, ZERO)
    positive = tuple(value for value in values if value > ZERO)
    nonzero_absolute = tuple(abs(value) for value in values if value != ZERO)
    binding = None
    positive_share = None
    absolute_share = None
    if net_pnl > ZERO and positive:
        binding = max(positive) / net_pnl
    if positive:
        positive_share = max(positive) / sum(positive, ZERO)
    if nonzero_absolute:
        absolute_share = max(nonzero_absolute) / sum(nonzero_absolute, ZERO)
    return binding, positive_share, absolute_share


def _forecast_metric_set(
    rows: Sequence[LockedOOSForecastRow],
) -> Mapping[str, Mapping[str, Decimal | None]]:
    metrics: dict[str, Mapping[str, Decimal | None]] = {}
    for horizon in ("5m", "15m"):
        market_brier = _cluster_equal_brier(rows, horizon=horizon, baseline=True)
        metrics[horizon] = MappingProxyType(
            {
                "model_brier": _cluster_equal_brier(
                    rows,
                    horizon=horizon,
                    baseline=False,
                ),
                "coherent_executable_market_brier": market_brier,
                "executable_market_brier": market_brier,
                "ece": _cluster_equal_ece(rows, horizon=horizon),
            }
        )
    return MappingProxyType(metrics)


def _basis_complete_evidence(
    rows: Sequence[LockedOOSEconomicAttempt],
    *,
    builder_verified: bool,
) -> bool:
    return (
        builder_verified
        and bool(rows)
        and all(
            row.complete_cost_evidence
            and row.complete_execution_evidence
            and row.complete_settlement_evidence
            and row.immutable_public_capture_evidence
            and (row.explainable if row.economic_attempt else row.causal_no_fill)
            for row in rows
        )
    )


def _non_applicable_forecast_metric_set() -> Mapping[str, Mapping[str, Decimal | None]]:
    return MappingProxyType(
        {
            horizon: MappingProxyType(
                {
                    "model_brier": None,
                    "coherent_executable_market_brier": None,
                    "executable_market_brier": None,
                    "ece": None,
                }
            )
            for horizon in ("5m", "15m")
        }
    )


def _track_operational_evidence(
    rows: Sequence[LockedOOSEconomicAttempt],
) -> bool:
    return bool(rows) and all(
        row.complete_cost_evidence
        and row.complete_execution_evidence
        and row.complete_settlement_evidence
        and (row.explainable if row.economic_attempt else row.causal_no_fill)
        for row in rows
    )


def _track_immutable_evidence(
    rows: Sequence[LockedOOSEconomicAttempt],
) -> bool:
    return bool(rows) and all(row.immutable_public_capture_evidence for row in rows)


def _structural_floor_rows_valid(
    rows: Sequence[LockedOOSEconomicAttempt],
) -> bool:
    return bool(rows) and all(
        row.edge_basis is V07EdgeBasis.STRUCTURAL
        and row.edge_evaluation_quantity is not None
        and row.edge_evaluation_quantity > ZERO
        and row.structural_quantity_executable
        and row.structural_worst_case_payoff_per_pair is not None
        and row.structural_net_floor_per_pair is not None
        and row.structural_net_floor_per_pair > ZERO
        and row.quantity_selection_basis
        is (V07QuantitySelectionBasis.STRUCTURAL_MAX_GUARANTEED_TOTAL_PNL)
        and row.selected_guaranteed_total_pnl is not None
        and row.selected_guaranteed_total_pnl > ZERO
        and row.selected_guaranteed_total_pnl
        == row.edge_evaluation_quantity * row.structural_net_floor_per_pair
        and row.selected_uncertainty_adjusted_total_pnl is None
        and not row.probability_diagnostics_applicable
        for row in rows
    )


def _evaluate_locked_oos_evidence(
    *,
    forecasts: Sequence[LockedOOSForecastRow],
    economic_attempts: Sequence[LockedOOSEconomicAttempt],
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
    builder_verification: _BuilderVerifiedEvidence | None,
) -> V07LockedOOSEvaluation:
    frozen_forecasts = _validated_forecasts(forecasts)
    reconciliations = tuple(economic_attempts)
    if any(not isinstance(row, LockedOOSEconomicAttempt) for row in reconciliations):
        raise TypeError("action reconciliations must be LockedOOSEconomicAttempt")
    attempt_ids = tuple(row.attempt_id for row in reconciliations)
    if len(set(attempt_ids)) != len(attempt_ids):
        raise ValueError("duplicate locked-OOS reconciliation id")
    identities = tuple(row.identity for row in reconciliations)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate actionable decision reconciliation identity")
    forecasts_by_identity = {row.identity: row for row in frozen_forecasts}
    for row in reconciliations:
        forecast = forecasts_by_identity.get(row.identity)
        if forecast is None:
            raise ValueError("action reconciliation has no matching forecast row")
        if row.expiry_ms != forecast.expiry_ms:
            raise ValueError("reconciliation expiry does not match forecast expiry")
        if row.canonical_pair_id != forecast.canonical_pair_id:
            raise ValueError("reconciliation pair identity does not match forecast")
        if row.event_cluster_alias != forecast.event_cluster_alias:
            raise ValueError("reconciliation alias does not match forecast alias")
        if row.lock_provenance.to_document() != forecast.lock_provenance.to_document():
            raise ValueError("reconciliation lock provenance differs from forecast")
        if row.decision_at_ms != forecast.decision_at_ms:
            raise ValueError("reconciliation decision timestamp differs from forecast")
        if row.forecast_available_at_ms != forecast.forecast_available_at_ms:
            raise ValueError(
                "reconciliation forecast availability differs from forecast"
            )
        if row.forecast_availability_basis is not (
            forecast.forecast_availability_basis
        ):
            raise ValueError("reconciliation availability basis differs from forecast")
        if row.edge_basis is not forecast.edge_basis:
            raise ValueError("reconciliation edge basis differs from forecast")
        for name in (
            "edge_evaluation_quantity",
            "structural_worst_case_payoff_per_pair",
            "structural_net_floor_per_pair",
            "structural_quantity_executable",
            "quantity_selection_basis",
            "quantity_candidate_breakpoint_count",
            "selected_guaranteed_total_pnl",
            "selected_uncertainty_adjusted_total_pnl",
            "probability_diagnostics_applicable",
        ):
            if getattr(row, name) != getattr(forecast, name):
                raise ValueError(f"reconciliation {name} differs from forecast")

    builder_verified = builder_verification is not None
    if builder_verification is not None:
        _validate_builder_verified_evidence(
            builder_verification,
            forecasts=frozen_forecasts,
            economic_attempts=reconciliations,
            parameter_neighborhood=parameter_neighborhood,
        )

    forecast_clusters = {row.expiry_cluster_id for row in frozen_forecasts}
    economic_rows = tuple(row for row in reconciliations if row.economic_attempt)
    explainable_attempts = tuple(row for row in economic_rows if row.explainable)
    no_fill_count = sum(row.causal_no_fill for row in reconciliations)
    net_pnl = sum((row.net_pnl for row in reconciliations), ZERO)

    predictive_forecasts = tuple(
        row for row in frozen_forecasts if row.edge_basis is V07EdgeBasis.PREDICTIVE
    )
    structural_forecasts = tuple(
        row for row in frozen_forecasts if row.edge_basis is V07EdgeBasis.STRUCTURAL
    )
    predictive_rows = tuple(
        row for row in reconciliations if row.edge_basis is V07EdgeBasis.PREDICTIVE
    )
    structural_rows = tuple(
        row for row in reconciliations if row.edge_basis is V07EdgeBasis.STRUCTURAL
    )
    predictive_economic_rows = tuple(
        row for row in predictive_rows if row.economic_attempt
    )
    structural_economic_rows = tuple(
        row for row in structural_rows if row.economic_attempt
    )
    predictive_explainable = tuple(
        row for row in predictive_economic_rows if row.explainable
    )
    structural_explainable = tuple(
        row for row in structural_economic_rows if row.explainable
    )

    predictive_cluster_count = len(
        {row.expiry_cluster_id for row in predictive_explainable}
    )
    structural_cluster_count = len(
        {row.expiry_cluster_id for row in structural_explainable}
    )
    predictive_forecast_cluster_count = len(
        {row.expiry_cluster_id for row in predictive_forecasts}
    )
    structural_forecast_cluster_count = len(
        {row.expiry_cluster_id for row in structural_forecasts}
    )
    predictive_net_pnl = sum((row.net_pnl for row in predictive_rows), ZERO)
    structural_net_pnl = sum((row.net_pnl for row in structural_rows), ZERO)

    predictive_bootstrap = _bootstrap_cluster_mean_lower_95(predictive_rows)
    structural_bootstrap = _bootstrap_cluster_mean_lower_95(structural_rows)
    predictive_binding, predictive_positive_share, predictive_absolute_share = (
        _cluster_concentration_ratios(predictive_rows)
    )
    structural_binding, structural_positive_share, structural_absolute_share = (
        _cluster_concentration_ratios(structural_rows)
    )
    predictive_metrics = _forecast_metric_set(predictive_forecasts)
    structural_metrics = _non_applicable_forecast_metric_set()

    common_auditable_lock = (
        builder_verified
        and bool(frozen_forecasts)
        and all(row.lock_provenance.verified for row in frozen_forecasts)
        and all(row.lock_provenance.verified for row in reconciliations)
    )
    predictive_operational = _track_operational_evidence(predictive_rows)
    structural_operational = _track_operational_evidence(structural_rows)
    predictive_immutable = _track_immutable_evidence(predictive_rows)
    structural_immutable = _track_immutable_evidence(structural_rows)
    predictive_complete = (
        builder_verified and predictive_operational and predictive_immutable
    )
    structural_complete = (
        builder_verified
        and structural_operational
        and structural_immutable
        and _structural_floor_rows_valid(structural_rows)
    )
    all_operational = _track_operational_evidence(reconciliations)
    all_immutable = _track_immutable_evidence(reconciliations)
    complete_evidence = builder_verified and all_operational and all_immutable

    predictive_sample_gate = (
        predictive_cluster_count >= MINIMUM_SETTLED_CLUSTERS
        and len(predictive_explainable) >= MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
    )
    structural_sample_gate = (
        structural_cluster_count >= MINIMUM_SETTLED_CLUSTERS
        and len(structural_explainable) >= MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
    )
    predictive_diagnostic_positive = (
        predictive_sample_gate and predictive_net_pnl > ZERO
    )
    structural_diagnostic_positive = (
        structural_sample_gate and structural_net_pnl > ZERO
    )

    reasons: list[str] = []
    if predictive_cluster_count < MINIMUM_SETTLED_CLUSTERS:
        reasons.append("fewer_than_100_distinct_settled_expiry_clusters")
    if len(predictive_explainable) < MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS:
        reasons.append("fewer_than_100_explainable_locked_oos_economic_attempts")
    if structural_cluster_count < MINIMUM_SETTLED_CLUSTERS:
        reasons.append("structural_fewer_than_100_distinct_settled_expiry_clusters")
    if len(structural_explainable) < MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS:
        reasons.append(
            "structural_fewer_than_100_explainable_locked_oos_economic_attempts"
        )
    if predictive_sample_gate and predictive_net_pnl <= ZERO:
        reasons.append("locked_oos_net_pnl_not_positive")
    if structural_sample_gate and structural_net_pnl <= ZERO:
        reasons.append("structural_locked_oos_net_pnl_not_positive")
    if (
        predictive_diagnostic_positive or structural_diagnostic_positive
    ) and not builder_verified:
        reasons.append("positive_sample_pnl_is_diagnostic_without_builder_verification")
    if not builder_verified:
        reasons.append("builder_verified_evidence_chain_missing")
    if not common_auditable_lock:
        reasons.append("auditable_prelabel_manifest_prediction_decision_lock_missing")

    predictive_true = all(
        (
            builder_verified,
            common_auditable_lock,
            predictive_sample_gate,
            predictive_net_pnl > ZERO,
            predictive_bootstrap is not None and predictive_bootstrap > ZERO,
            predictive_binding is not None
            and predictive_binding <= CONCENTRATION_LIMIT,
            predictive_complete,
            parameter_neighborhood is not None
            and parameter_neighborhood.stable_positive,
        )
    )
    if predictive_bootstrap is None or predictive_bootstrap <= ZERO:
        reasons.append("expiry_cluster_bootstrap_lower_95_not_above_zero")
        predictive_true = False
    if predictive_binding is None or predictive_binding > CONCENTRATION_LIMIT:
        reasons.append("largest_positive_expiry_cluster_exceeds_20_percent_of_net_pnl")
        predictive_true = False
    if not predictive_complete:
        reasons.append("cost_execution_or_settlement_evidence_incomplete")
        predictive_true = False
    for horizon in ("5m", "15m"):
        model_brier = predictive_metrics[horizon]["model_brier"]
        market_brier = predictive_metrics[horizon]["coherent_executable_market_brier"]
        ece = predictive_metrics[horizon]["ece"]
        if model_brier is None or market_brier is None or model_brier >= market_brier:
            reasons.append(f"{horizon}_brier_does_not_beat_coherent_executable_market")
            predictive_true = False
        if ece is None or ece > ECE_LIMIT:
            reasons.append(f"{horizon}_ece_exceeds_0_05")
            predictive_true = False
    if parameter_neighborhood is None or not parameter_neighborhood.stable_positive:
        reasons.append("neighboring_preregistered_settings_not_stably_positive")
        predictive_true = False

    structural_floor_valid = _structural_floor_rows_valid(structural_rows)
    structural_true = all(
        (
            builder_verified,
            common_auditable_lock,
            structural_sample_gate,
            structural_net_pnl > ZERO,
            structural_bootstrap is not None and structural_bootstrap > ZERO,
            structural_binding is not None
            and structural_binding <= CONCENTRATION_LIMIT,
            structural_complete,
            structural_floor_valid,
        )
    )
    if structural_bootstrap is None or structural_bootstrap <= ZERO:
        reasons.append("structural_expiry_cluster_bootstrap_lower_95_not_above_zero")
        structural_true = False
    if structural_binding is None or structural_binding > CONCENTRATION_LIMIT:
        reasons.append(
            "structural_largest_positive_expiry_cluster_exceeds_20_percent_of_net_pnl"
        )
        structural_true = False
    if not structural_floor_valid:
        reasons.append("structural_decision_time_executable_positive_floor_incomplete")
        structural_true = False
    if not structural_complete:
        reasons.append("structural_cost_execution_or_settlement_evidence_incomplete")
        structural_true = False

    true_edge = predictive_true or structural_true
    predictive_qualified = predictive_net_pnl if predictive_true else None
    structural_qualified = structural_net_pnl if structural_true else None
    qualified_parts = tuple(
        value
        for value in (predictive_qualified, structural_qualified)
        if value is not None
    )
    qualified_net_pnl = sum(qualified_parts, ZERO) if qualified_parts else None
    diagnostic_positive = (
        predictive_diagnostic_positive or structural_diagnostic_positive
    )
    positive_user_check = true_edge

    if not builder_verified:
        status = V07EvidenceStatus.COUNTERFACTUAL_INSUFFICIENT
    elif true_edge:
        status = V07EvidenceStatus.TRUE_EDGE_GATE_SATISFIED
    elif not predictive_sample_gate and not structural_sample_gate:
        status = V07EvidenceStatus.INSUFFICIENT_DATA
    elif not (
        (predictive_sample_gate and predictive_net_pnl > ZERO)
        or (structural_sample_gate and structural_net_pnl > ZERO)
    ):
        status = V07EvidenceStatus.NOT_PROFITABLE
    else:
        status = V07EvidenceStatus.POSITIVE_BUT_NOT_TRUE_EDGE

    return V07LockedOOSEvaluation(
        status=status,
        settled_expiry_cluster_count=len(
            {row.expiry_cluster_id for row in explainable_attempts}
        ),
        settled_forecast_cluster_count=len(forecast_clusters),
        reconciled_actionable_decision_count=len(reconciliations),
        causal_no_fill_count=no_fill_count,
        economic_attempt_count=len(economic_rows),
        explainable_economic_attempt_count=len(explainable_attempts),
        net_pnl=net_pnl,
        qualified_net_pnl=qualified_net_pnl,
        predictive_settled_expiry_cluster_count=predictive_cluster_count,
        predictive_settled_forecast_cluster_count=(predictive_forecast_cluster_count),
        predictive_economic_attempt_count=len(predictive_economic_rows),
        predictive_explainable_economic_attempt_count=len(predictive_explainable),
        predictive_net_pnl=predictive_net_pnl,
        predictive_qualified_net_pnl=predictive_qualified,
        predictive_true_edge_gate_satisfied=predictive_true,
        structural_settled_expiry_cluster_count=structural_cluster_count,
        structural_settled_forecast_cluster_count=(structural_forecast_cluster_count),
        structural_economic_attempt_count=len(structural_economic_rows),
        structural_explainable_economic_attempt_count=len(structural_explainable),
        structural_net_pnl=structural_net_pnl,
        structural_qualified_net_pnl=structural_qualified,
        structural_true_edge_gate_satisfied=structural_true,
        diagnostic_positive_sample_pnl=diagnostic_positive,
        predictive_diagnostic_positive_sample_pnl=(predictive_diagnostic_positive),
        structural_diagnostic_positive_sample_pnl=(structural_diagnostic_positive),
        positive_net_pnl_user_check_passed=positive_user_check,
        true_edge_gate_satisfied=true_edge,
        auditable_prelabel_lock_evidence=common_auditable_lock,
        builder_verified_evidence_chain=builder_verified,
        builder_verification_sha256=(
            None
            if builder_verification is None
            else builder_verification.verification_chain_sha256
        ),
        bootstrap_cluster_mean_lower_95=predictive_bootstrap,
        predictive_bootstrap_cluster_mean_lower_95=predictive_bootstrap,
        structural_bootstrap_cluster_mean_lower_95=structural_bootstrap,
        largest_positive_cluster_to_total_net_pnl=predictive_binding,
        largest_positive_cluster_to_total_positive_cluster_pnl=(
            predictive_positive_share
        ),
        maximum_absolute_cluster_contribution_share=predictive_absolute_share,
        structural_largest_positive_cluster_to_total_net_pnl=structural_binding,
        structural_largest_positive_cluster_to_total_positive_cluster_pnl=(
            structural_positive_share
        ),
        structural_maximum_absolute_cluster_contribution_share=(
            structural_absolute_share
        ),
        forecast_metrics=predictive_metrics,
        structural_forecast_metrics=structural_metrics,
        complete_cost_and_execution_evidence=complete_evidence,
        predictive_complete_cost_and_execution_evidence=predictive_complete,
        structural_complete_cost_and_execution_evidence=structural_complete,
        parameter_neighborhood=parameter_neighborhood,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True)
class V07GateMechanismDiagnostic:
    """Pure gate-math diagnostic that can never represent economic qualification."""

    sample_gate_passed: bool
    diagnostic_positive_sample_pnl: bool
    bootstrap_gate_passed: bool
    concentration_gate_passed: bool
    forecast_metric_gates_passed: bool
    neighboring_settings_gate_passed: bool
    mathematical_predictive_gate_conditions_satisfied: bool
    structural_sample_gate_passed: bool
    structural_diagnostic_positive_sample_pnl: bool
    structural_bootstrap_gate_passed: bool
    structural_concentration_gate_passed: bool
    structural_positive_floor_gate_passed: bool
    mathematical_structural_gate_conditions_satisfied: bool
    economic_evidence_status: str = "non_economic_mechanism_diagnostic"
    true_edge_gate_satisfied: bool = False
    qualified_net_pnl: None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-5m-15m-v07-gate-mechanism-diagnostic.v2",
            "economic_evidence_status": self.economic_evidence_status,
            "sample_gate_passed": self.sample_gate_passed,
            "diagnostic_positive_sample_pnl": self.diagnostic_positive_sample_pnl,
            "bootstrap_gate_passed": self.bootstrap_gate_passed,
            "concentration_gate_passed": self.concentration_gate_passed,
            "forecast_metric_gates_passed": self.forecast_metric_gates_passed,
            "neighboring_settings_gate_passed": (self.neighboring_settings_gate_passed),
            "mathematical_predictive_gate_conditions_satisfied": (
                self.mathematical_predictive_gate_conditions_satisfied
            ),
            "structural_sample_gate_passed": self.structural_sample_gate_passed,
            "structural_diagnostic_positive_sample_pnl": (
                self.structural_diagnostic_positive_sample_pnl
            ),
            "structural_bootstrap_gate_passed": (self.structural_bootstrap_gate_passed),
            "structural_concentration_gate_passed": (
                self.structural_concentration_gate_passed
            ),
            "structural_positive_floor_gate_passed": (
                self.structural_positive_floor_gate_passed
            ),
            "mathematical_structural_gate_conditions_satisfied": (
                self.mathematical_structural_gate_conditions_satisfied
            ),
            "true_edge_gate_satisfied": False,
            "predictive_true_edge_gate_satisfied": False,
            "structural_true_edge_gate_satisfied": False,
            "qualified_net_pnl": None,
            "predictive_qualified_net_pnl": None,
            "structural_qualified_net_pnl": None,
            "synthetic_or_generated_inputs_can_never_be_economic_evidence": True,
        }


def evaluate_gate_mechanism_diagnostic(
    *,
    forecasts: Sequence[LockedOOSForecastRow],
    economic_attempts: Sequence[LockedOOSEconomicAttempt],
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
) -> V07GateMechanismDiagnostic:
    """Evaluate only preregistered gate arithmetic, never evidence authority."""

    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=economic_attempts,
        parameter_neighborhood=parameter_neighborhood,
    )
    sample_gate = (
        result.predictive_settled_expiry_cluster_count >= MINIMUM_SETTLED_CLUSTERS
        and result.predictive_explainable_economic_attempt_count
        >= MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
    )
    bootstrap_gate = (
        result.bootstrap_cluster_mean_lower_95 is not None
        and result.bootstrap_cluster_mean_lower_95 > ZERO
    )
    concentration_gate = (
        result.largest_positive_cluster_to_total_net_pnl is not None
        and result.largest_positive_cluster_to_total_net_pnl <= CONCENTRATION_LIMIT
    )
    forecast_metric_gates = all(
        result.forecast_metrics[horizon]["model_brier"] is not None
        and result.forecast_metrics[horizon]["coherent_executable_market_brier"]
        is not None
        and result.forecast_metrics[horizon]["model_brier"]
        < result.forecast_metrics[horizon]["coherent_executable_market_brier"]
        and result.forecast_metrics[horizon]["ece"] is not None
        and result.forecast_metrics[horizon]["ece"] <= ECE_LIMIT
        for horizon in ("5m", "15m")
    )
    neighboring_gate = (
        parameter_neighborhood is not None and parameter_neighborhood.stable_positive
    )
    predictive_mathematical = all(
        (
            sample_gate,
            result.predictive_diagnostic_positive_sample_pnl,
            bootstrap_gate,
            concentration_gate,
            forecast_metric_gates,
            neighboring_gate,
        )
    )
    structural_sample_gate = (
        result.structural_settled_expiry_cluster_count >= MINIMUM_SETTLED_CLUSTERS
        and result.structural_explainable_economic_attempt_count
        >= MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
    )
    structural_bootstrap_gate = (
        result.structural_bootstrap_cluster_mean_lower_95 is not None
        and result.structural_bootstrap_cluster_mean_lower_95 > ZERO
    )
    structural_concentration_gate = (
        result.structural_largest_positive_cluster_to_total_net_pnl is not None
        and result.structural_largest_positive_cluster_to_total_net_pnl
        <= CONCENTRATION_LIMIT
    )
    structural_attempts = tuple(
        row
        for row in economic_attempts
        if row.edge_basis is V07EdgeBasis.STRUCTURAL and row.economic_attempt
    )
    structural_floor_gate = bool(structural_attempts) and all(
        row.structural_quantity_executable
        and row.structural_net_floor_per_pair is not None
        and row.structural_net_floor_per_pair > ZERO
        and row.selected_guaranteed_total_pnl is not None
        and row.selected_guaranteed_total_pnl > ZERO
        for row in structural_attempts
    )
    structural_mathematical = all(
        (
            structural_sample_gate,
            result.structural_diagnostic_positive_sample_pnl,
            structural_bootstrap_gate,
            structural_concentration_gate,
            structural_floor_gate,
        )
    )
    return V07GateMechanismDiagnostic(
        sample_gate_passed=sample_gate,
        diagnostic_positive_sample_pnl=(
            result.predictive_diagnostic_positive_sample_pnl
        ),
        bootstrap_gate_passed=bootstrap_gate,
        concentration_gate_passed=concentration_gate,
        forecast_metric_gates_passed=forecast_metric_gates,
        neighboring_settings_gate_passed=neighboring_gate,
        mathematical_predictive_gate_conditions_satisfied=(predictive_mathematical),
        structural_sample_gate_passed=structural_sample_gate,
        structural_diagnostic_positive_sample_pnl=(
            result.structural_diagnostic_positive_sample_pnl
        ),
        structural_bootstrap_gate_passed=structural_bootstrap_gate,
        structural_concentration_gate_passed=structural_concentration_gate,
        structural_positive_floor_gate_passed=structural_floor_gate,
        mathematical_structural_gate_conditions_satisfied=(structural_mathematical),
    )


def evaluate_locked_oos_evidence(
    *,
    forecasts: Sequence[LockedOOSForecastRow],
    economic_attempts: Sequence[LockedOOSEconomicAttempt],
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
) -> V07LockedOOSEvaluation:
    """Public evaluator: validate rows but never grant economic qualification.

    Caller-created dataclasses, receipt-looking strings, and evidence booleans are
    descriptive inputs only.  Only the builder's private, row-bound verification
    capability can authorize a true-edge evaluation.
    """

    return _evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=economic_attempts,
        parameter_neighborhood=parameter_neighborhood,
        builder_verification=None,
    )


def _evaluate_builder_verified_locked_oos_evidence(
    *,
    forecasts: Sequence[LockedOOSForecastRow],
    economic_attempts: Sequence[LockedOOSEconomicAttempt],
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
    builder_verification: _BuilderVerifiedEvidence,
) -> V07LockedOOSEvaluation:
    """Private builder entry point after raw-capture and journal verification."""

    return _evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=economic_attempts,
        parameter_neighborhood=parameter_neighborhood,
        builder_verification=builder_verification,
    )


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "CONCENTRATION_LIMIT",
    "ECE_LIMIT",
    "LockedOOSEconomicAttempt",
    "LockedOOSForecastRow",
    "MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS",
    "MINIMUM_SETTLED_CLUSTERS",
    "ParameterNeighborhoodEvidence",
    "PreLabelLockProvenance",
    "PreLabelLockStatus",
    "V07EvidenceStatus",
    "V07GateMechanismDiagnostic",
    "V07LockedOOSEvaluation",
    "evaluate_gate_mechanism_diagnostic",
    "evaluate_locked_oos_evidence",
]
