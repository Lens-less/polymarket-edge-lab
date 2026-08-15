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
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


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


@dataclass(frozen=True)
class PreLabelLockProvenance:
    """Auditable manifest, forecast, and decision lock receipts."""

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
        return cls(status=PreLabelLockStatus.COUNTERFACTUAL, reason=reason)

    @property
    def verified(self) -> bool:
        return self.status is PreLabelLockStatus.VERIFIED

    def to_document(self) -> dict[str, Any]:
        payload = {
            "schema_version": "btc-5m-15m-v07-prelabel-lock-provenance.v1",
            "status": self.status.value,
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


@dataclass(frozen=True)
class LockedOOSForecastRow:
    event_cluster_id: str
    event_cluster_alias: str
    expiry_ms: int
    decision_tau_seconds: int
    decision_at_ms: int
    label_available_at_ms: int
    strike_5: Decimal
    strike_15: Decimal
    forecast_q_5_up: Decimal
    forecast_q_15_up: Decimal
    market_q_5_up: Decimal
    market_q_15_up: Decimal
    actual_5_up: bool
    actual_15_up: bool
    lock_provenance: PreLabelLockProvenance = field(
        default_factory=lambda: PreLabelLockProvenance.counterfactual(
            "prelabel_lock_receipt_not_supplied"
        )
    )
    raw_top_ask_q_5_up: Decimal | None = None
    raw_top_ask_q_15_up: Decimal | None = None

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
        label_at_ms = _non_negative_int(
            self.label_available_at_ms,
            label="label_available_at_ms",
        )
        if decision_at_ms > expiry_ms:
            raise ValueError("decision must not occur after the common expiry")
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
            object.__setattr__(
                self,
                name,
                _probability(getattr(self, name), label=name),
            )
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
            if prediction_locked_at_ms >= label_at_ms:
                raise ValueError("prediction lock must predate label availability")
            if prediction_received_at_ms >= label_at_ms:
                raise ValueError("prediction receipt must predate label availability")
            if universe_locked_at_ms > decision_at_ms:
                raise ValueError("test universe must be locked before the decision")
            if universe_received_at_ms > decision_at_ms:
                raise ValueError("test-universe receipt must predate the decision")

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
    def coherent_market_marginals(self) -> tuple[Decimal, Decimal]:
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
    net_pnl: Decimal
    explainable: bool
    complete_cost_evidence: bool
    complete_execution_evidence: bool
    complete_settlement_evidence: bool
    immutable_public_capture_evidence: bool
    signal_strength: Decimal
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
        object.__setattr__(self, "net_pnl", _decimal(self.net_pnl, label="net_pnl"))
        strength = _decimal(self.signal_strength, label="signal_strength")
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
    positive_net_pnl_user_check_passed: bool
    true_edge_gate_satisfied: bool
    auditable_prelabel_lock_evidence: bool
    bootstrap_cluster_mean_lower_95: Decimal | None
    largest_positive_cluster_to_total_net_pnl: Decimal | None
    largest_positive_cluster_to_total_positive_cluster_pnl: Decimal | None
    maximum_absolute_cluster_contribution_share: Decimal | None
    forecast_metrics: Mapping[str, Mapping[str, Decimal | None]]
    complete_cost_and_execution_evidence: bool
    parameter_neighborhood: ParameterNeighborhoodEvidence | None
    reason_codes: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-5m-15m-v07-locked-oos-evaluation.v3",
            "status": self.status.value,
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
                "minimum_settled_expiry_clusters": MINIMUM_SETTLED_CLUSTERS,
                "minimum_explainable_economic_attempts": (
                    MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
                ),
                "tau_rows_do_not_count_as_independent_clusters": True,
                "pair_hashes_do_not_count_as_independent_clusters": True,
                "no_fill_rows_do_not_count_as_economic_attempts": True,
            },
            "net_pnl": _decimal_text(self.net_pnl),
            "qualified_net_pnl": _decimal_text(self.qualified_net_pnl),
            "positive_net_pnl_user_check_passed": (
                self.positive_net_pnl_user_check_passed
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
                "lower_95_quantile_method": (
                    "sorted_order_statistic_at_ceil_0.05_times_resamples_minus_one"
                ),
                "mean_net_pnl_lower_95": _decimal_text(
                    self.bootstrap_cluster_mean_lower_95
                ),
            },
            "concentration": {
                "binding_largest_positive_cluster_to_total_net_pnl": (
                    _decimal_text(self.largest_positive_cluster_to_total_net_pnl)
                ),
                "binding_numerator": "largest_positive_common_expiry_net_pnl",
                "binding_denominator": "total_net_pnl",
                "binding_unavailable_when_total_net_pnl_nonpositive": True,
                "limit": _decimal_text(CONCENTRATION_LIMIT),
                "diagnostic_largest_positive_cluster_to_total_positive_cluster_pnl": (
                    _decimal_text(
                        self.largest_positive_cluster_to_total_positive_cluster_pnl
                    )
                ),
                "diagnostic_largest_absolute_cluster_to_total_absolute_cluster_pnl": (
                    _decimal_text(self.maximum_absolute_cluster_contribution_share)
                ),
            },
            "forecast_metric_method": {
                "brier_weighting": (
                    "equal_common_expiry_then_equal_tau_within_expiry"
                ),
                "binding_market_baseline": (
                    "fixed_size_depth_walk_fee_inclusive_then_shared_terminal_projection"
                ),
                "raw_top_ask_probabilities_are_diagnostic_only": True,
                "ece_bins": ECE_BINS,
                "ece_bin_edges": "equal_width_on_closed_unit_interval",
                "ece_weighting": (
                    "equal_common_expiry_then_equal_tau_within_expiry"
                ),
                "canonical_pair_hash_role": "market_condition_integrity_only",
            },
            "forecast_metrics": {
                horizon: {key: _decimal_text(value) for key, value in metrics.items()}
                for horizon, metrics in self.forecast_metrics.items()
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


def evaluate_locked_oos_evidence(
    *,
    forecasts: Sequence[LockedOOSForecastRow],
    economic_attempts: Sequence[LockedOOSEconomicAttempt],
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
) -> V07LockedOOSEvaluation:
    """Apply the preregistered common-expiry and true-edge evidence gates."""

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

    forecast_clusters = {row.expiry_cluster_id for row in frozen_forecasts}
    forecast_cluster_count = len(forecast_clusters)
    economic_rows = tuple(row for row in reconciliations if row.economic_attempt)
    explainable_attempts = tuple(row for row in economic_rows if row.explainable)
    cluster_count = len({row.expiry_cluster_id for row in explainable_attempts})
    economic_count = len(economic_rows)
    explainable_count = len(explainable_attempts)
    no_fill_count = sum(row.causal_no_fill for row in reconciliations)
    net_pnl = sum((row.net_pnl for row in reconciliations), ZERO)
    bootstrap_lower = _bootstrap_cluster_mean_lower_95(reconciliations)
    binding_concentration, positive_share, absolute_share = (
        _cluster_concentration_ratios(reconciliations)
    )
    metrics: dict[str, Mapping[str, Decimal | None]] = {}
    for horizon in ("5m", "15m"):
        market_brier = _cluster_equal_brier(
            frozen_forecasts,
            horizon=horizon,
            baseline=True,
        )
        metrics[horizon] = MappingProxyType(
            {
                "model_brier": _cluster_equal_brier(
                    frozen_forecasts,
                    horizon=horizon,
                    baseline=False,
                ),
                "coherent_executable_market_brier": market_brier,
                "executable_market_brier": market_brier,
                "ece": _cluster_equal_ece(
                    frozen_forecasts,
                    horizon=horizon,
                ),
            }
        )
    complete_operational_evidence = bool(reconciliations) and all(
        row.complete_cost_evidence
        and row.complete_execution_evidence
        and row.complete_settlement_evidence
        and (row.explainable if row.economic_attempt else row.causal_no_fill)
        for row in reconciliations
    )
    immutable_capture_evidence = bool(reconciliations) and all(
        row.immutable_public_capture_evidence for row in reconciliations
    )
    complete_evidence = complete_operational_evidence and immutable_capture_evidence
    auditable_lock_evidence = (
        bool(frozen_forecasts)
        and all(row.lock_provenance.verified for row in frozen_forecasts)
        and all(row.lock_provenance.verified for row in reconciliations)
    )

    reasons: list[str] = []
    sample_gate = True
    if cluster_count < MINIMUM_SETTLED_CLUSTERS:
        reasons.append("fewer_than_100_distinct_settled_expiry_clusters")
        sample_gate = False
    if explainable_count < MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS:
        reasons.append("fewer_than_100_explainable_locked_oos_economic_attempts")
        sample_gate = False
    positive_user_check = sample_gate and net_pnl > ZERO
    if sample_gate and net_pnl <= ZERO:
        reasons.append("locked_oos_net_pnl_not_positive")

    true_edge = positive_user_check
    if not auditable_lock_evidence:
        reasons.append("auditable_prelabel_manifest_prediction_decision_lock_missing")
        true_edge = False
    if bootstrap_lower is None or bootstrap_lower <= ZERO:
        reasons.append("expiry_cluster_bootstrap_lower_95_not_above_zero")
        true_edge = False
    if binding_concentration is None or binding_concentration > CONCENTRATION_LIMIT:
        reasons.append("largest_positive_expiry_cluster_exceeds_20_percent_of_net_pnl")
        true_edge = False
    if not complete_operational_evidence:
        reasons.append("cost_execution_or_settlement_evidence_incomplete")
        true_edge = False
    if not immutable_capture_evidence:
        reasons.append("immutable_public_capture_evidence_missing")
        true_edge = False
    for horizon in ("5m", "15m"):
        model_brier = metrics[horizon]["model_brier"]
        market_brier = metrics[horizon]["coherent_executable_market_brier"]
        ece = metrics[horizon]["ece"]
        if model_brier is None or market_brier is None or model_brier >= market_brier:
            reasons.append(f"{horizon}_brier_does_not_beat_coherent_executable_market")
            true_edge = False
        if ece is None or ece > ECE_LIMIT:
            reasons.append(f"{horizon}_ece_exceeds_0_05")
            true_edge = False
    if parameter_neighborhood is None or not parameter_neighborhood.stable_positive:
        reasons.append("neighboring_preregistered_settings_not_stably_positive")
        true_edge = False

    if not auditable_lock_evidence:
        status = V07EvidenceStatus.COUNTERFACTUAL_INSUFFICIENT
    elif not sample_gate:
        status = V07EvidenceStatus.INSUFFICIENT_DATA
    elif net_pnl <= ZERO:
        status = V07EvidenceStatus.NOT_PROFITABLE
    elif true_edge:
        status = V07EvidenceStatus.TRUE_EDGE_GATE_SATISFIED
    else:
        status = V07EvidenceStatus.POSITIVE_BUT_NOT_TRUE_EDGE
    return V07LockedOOSEvaluation(
        status=status,
        settled_expiry_cluster_count=cluster_count,
        settled_forecast_cluster_count=forecast_cluster_count,
        reconciled_actionable_decision_count=len(reconciliations),
        causal_no_fill_count=no_fill_count,
        economic_attempt_count=economic_count,
        explainable_economic_attempt_count=explainable_count,
        net_pnl=net_pnl,
        qualified_net_pnl=net_pnl if true_edge else None,
        positive_net_pnl_user_check_passed=positive_user_check,
        true_edge_gate_satisfied=true_edge,
        auditable_prelabel_lock_evidence=auditable_lock_evidence,
        bootstrap_cluster_mean_lower_95=bootstrap_lower,
        largest_positive_cluster_to_total_net_pnl=binding_concentration,
        largest_positive_cluster_to_total_positive_cluster_pnl=positive_share,
        maximum_absolute_cluster_contribution_share=absolute_share,
        forecast_metrics=MappingProxyType(metrics),
        complete_cost_and_execution_evidence=complete_evidence,
        parameter_neighborhood=parameter_neighborhood,
        reason_codes=tuple(dict.fromkeys(reasons)),
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
    "V07LockedOOSEvaluation",
    "evaluate_locked_oos_evidence",
]
