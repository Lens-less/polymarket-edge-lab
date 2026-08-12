"""Offline orchestration for leakage-resistant weather edge experiments.

The module consumes already-acquired public evidence through an injected
provider.  It never opens a socket, authenticates, submits an order, or writes
an artifact.  Resolved Gamma winner bins are categorical labels; no exact
temperature is reconstructed from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import random
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from .economics import FeeSchedule, taker_fee as official_taker_fee
from .experiments import EventSplits
from .network_safety import safe_error_details
from .weather import (
    ReliabilityBucket,
    WeatherDataError,
    WeatherEventSpec,
    brier_score,
    chronological_weather_split,
    city_weather_split,
    local_day_extreme,
    parse_gamma_weather_event,
    reliability_table,
    HourlyTemperature,
)
from .weather_sources import (
    AcquiredForecastRun,
    AviationStation,
    CLOBPriceHistory,
    PriceNotAvailableError,
    RawAcquisition,
)


ZERO = Decimal("0")
ONE = Decimal("1")
PROBABILITY_QUANTUM = Decimal("0.000000000000000001")
LOG_EPSILON = Decimal("0.000000000000001")
DECIMAL_TWO_PI = Decimal(
    "6.2831853071795864769252867665590057683943387987502"
)
DECIMAL_WORKING_PRECISION = 50


def _timedelta_seconds(value: timedelta) -> Decimal:
    return (
        Decimal(value.days) * Decimal("86400")
        + Decimal(value.seconds)
        + Decimal(value.microseconds) / Decimal("1000000")
    )


def _validate_acquisition(
    acquisition: RawAcquisition,
    *,
    field: str,
) -> None:
    if not isinstance(acquisition, RawAcquisition):
        raise WeatherExperimentError(f"{field} acquisition is missing")
    expected = hashlib.sha256(
        acquisition.raw_text.encode("utf-8")
    ).hexdigest()
    if acquisition.sha256 != expected:
        raise WeatherExperimentError(f"{field} acquisition checksum mismatch")
    if acquisition.provenance.method != "GET":
        raise WeatherExperimentError(f"{field} acquisition must use GET")
    if not 200 <= acquisition.provenance.status_code < 300:
        raise WeatherExperimentError(
            f"{field} acquisition HTTP status is not successful"
        )


class WeatherExperimentError(ValueError):
    """The experiment cannot be evaluated without weakening its evidence rules."""


class NoEligibleWeatherEventsError(WeatherExperimentError):
    """All input rows were excluded; the exclusions remain machine-readable."""

    def __init__(self, exclusions: Sequence["ExperimentExclusion"]) -> None:
        super().__init__("no eligible weather events remain")
        self.exclusions = tuple(exclusions)

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "status": "invalid_no_eligible_events",
            "classification": "insufficient_data",
            "exclusions": [
                exclusion.artifact_payload()
                for exclusion in self.exclusions
            ],
        }


def _decimal(value: Any, *, field: str, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, (bool, float)) or value is None:
        raise WeatherExperimentError(f"{field} must be a decimal")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise WeatherExperimentError(f"{field} must be a decimal") from exc
    if not result.is_finite():
        raise WeatherExperimentError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise WeatherExperimentError(f"{field} must be at least {minimum}")
    return result


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WeatherExperimentError(f"{field} must be timezone-aware")
    if value.utcoffset() is None:
        raise WeatherExperimentError(f"{field} must have a valid UTC offset")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class WeatherEventEvidence:
    """Already-acquired decision-time evidence for one Gamma event."""

    decision_at: datetime
    forecast: Optional[AcquiredForecastRun]
    station: AviationStation
    price_histories: Mapping[str, CLOBPriceHistory]
    independence_group: str

    def __post_init__(self) -> None:
        decision_at = _aware_utc(self.decision_at, field="decision_at")
        if not isinstance(self.station, AviationStation):
            raise TypeError("station must be an AviationStation")
        if not isinstance(self.station.latitude, Decimal) or not isinstance(
            self.station.longitude, Decimal
        ):
            raise WeatherExperimentError(
                "station coordinates must be Decimal values"
            )
        if (
            not self.station.latitude.is_finite()
            or not self.station.longitude.is_finite()
            or not Decimal("-90")
            <= self.station.latitude
            <= Decimal("90")
            or not Decimal("-180")
            <= self.station.longitude
            <= Decimal("180")
        ):
            raise WeatherExperimentError("station coordinates are invalid")
        _validate_acquisition(
            self.station.acquisition,
            field="station",
        )
        if self.forecast is not None and not isinstance(
            self.forecast, AcquiredForecastRun
        ):
            raise TypeError("forecast must be an AcquiredForecastRun or None")
        if not isinstance(self.price_histories, Mapping):
            raise TypeError("price_histories must be a mapping")
        histories: dict[str, CLOBPriceHistory] = {}
        for raw_token, history in self.price_histories.items():
            token = str(raw_token)
            if not token:
                raise WeatherExperimentError("price-history token IDs cannot be empty")
            if not isinstance(history, CLOBPriceHistory):
                raise TypeError("price histories must be CLOBPriceHistory objects")
            if history.token_id != token:
                raise WeatherExperimentError(
                    f"price-history key/token mismatch for {token}"
                )
            histories[token] = history
        group = str(self.independence_group).strip()
        if not group:
            raise WeatherExperimentError("independence_group cannot be empty")
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(
            self,
            "price_histories",
            MappingProxyType(histories),
        )
        object.__setattr__(self, "independence_group", group)


class WeatherEvidenceProvider(Protocol):
    """Dependency-injection seam; implementations may only return acquired data."""

    def evidence_for(self, spec: WeatherEventSpec) -> Optional[WeatherEventEvidence]:
        ...


class MappingWeatherEvidenceProvider:
    """Deterministic in-memory evidence provider used by offline research."""

    def __init__(self, evidence: Mapping[str, WeatherEventEvidence]) -> None:
        self._evidence = dict(evidence)
        self._requested: list[str] = []

    @property
    def requested_event_ids(self) -> tuple[str, ...]:
        return tuple(self._requested)

    def evidence_for(self, spec: WeatherEventSpec) -> Optional[WeatherEventEvidence]:
        self._requested.append(spec.event_id)
        return self._evidence.get(spec.event_id)


@dataclass(frozen=True)
class WeatherExperimentConfig:
    """Frozen economics, model grid, split, and evidence gates."""

    bias_grid: Sequence[Decimal]
    standard_deviation_grid: Sequence[Decimal]
    edge_threshold_candidates: Sequence[Decimal]
    taker_fee: Decimal
    maker_fee: Decimal
    slippage: Decimal
    model_margin: Decimal
    taker_fee_rate: Decimal = ZERO
    taker_fee_exponent: Decimal = ONE
    taker_fee_source: str = "fixed_per_share_scenario"
    random_seed: int = 0
    train_fraction: Decimal = Decimal("0.6")
    validation_fraction: Decimal = Decimal("0.2")
    minimum_independent_events: int = 100
    minimum_explainable_fills: int = 100
    reliability_bins: int = 10
    minimum_decision_lead: timedelta = timedelta(0)
    maximum_price_age: timedelta = timedelta(hours=6)
    minimum_cohort_train_events: int = 20
    bootstrap_resamples: int = 2000

    def __post_init__(self) -> None:
        bias_grid = tuple(
            sorted({_decimal(item, field="bias_grid") for item in self.bias_grid})
        )
        standard_grid = tuple(
            sorted(
                {
                    _decimal(
                        item,
                        field="standard_deviation_grid",
                        minimum=Decimal("0.000000000001"),
                    )
                    for item in self.standard_deviation_grid
                }
            )
        )
        threshold_grid = tuple(
            sorted(
                {
                    _decimal(
                        item,
                        field="edge_threshold_candidates",
                        minimum=ZERO,
                    )
                    for item in self.edge_threshold_candidates
                }
            )
        )
        if not bias_grid or not standard_grid or not threshold_grid:
            raise WeatherExperimentError("model and threshold grids cannot be empty")
        if ZERO not in bias_grid:
            raise WeatherExperimentError(
                "bias_grid must include zero for the unbiased normal baseline"
            )
        for field in (
            "taker_fee",
            "maker_fee",
            "slippage",
            "model_margin",
            "taker_fee_rate",
        ):
            object.__setattr__(
                self,
                field,
                _decimal(getattr(self, field), field=field, minimum=ZERO),
            )
        object.__setattr__(
            self,
            "taker_fee_exponent",
            _decimal(
                self.taker_fee_exponent,
                field="taker_fee_exponent",
                minimum=ZERO,
            ),
        )
        if not isinstance(self.taker_fee_source, str) or not (
            self.taker_fee_source.strip()
        ):
            raise WeatherExperimentError("taker_fee_source must be non-empty")
        train_fraction = _decimal(
            self.train_fraction, field="train_fraction", minimum=ZERO
        )
        validation_fraction = _decimal(
            self.validation_fraction,
            field="validation_fraction",
            minimum=ZERO,
        )
        if (
            train_fraction <= ZERO
            or validation_fraction <= ZERO
            or train_fraction + validation_fraction >= ONE
        ):
            raise WeatherExperimentError(
                "train and validation fractions must be positive and sum below one"
            )
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise TypeError("random_seed must be an integer")
        for field in ("minimum_independent_events", "minimum_explainable_fills"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise WeatherExperimentError(f"{field} must be a non-negative integer")
        if (
            not isinstance(self.minimum_cohort_train_events, int)
            or isinstance(self.minimum_cohort_train_events, bool)
            or self.minimum_cohort_train_events < 2
        ):
            raise WeatherExperimentError(
                "minimum_cohort_train_events must be at least two"
            )
        if (
            not isinstance(self.bootstrap_resamples, int)
            or isinstance(self.bootstrap_resamples, bool)
            or self.bootstrap_resamples <= 0
        ):
            raise WeatherExperimentError(
                "bootstrap_resamples must be a positive integer"
            )
        if (
            not isinstance(self.reliability_bins, int)
            or isinstance(self.reliability_bins, bool)
            or self.reliability_bins <= 0
        ):
            raise WeatherExperimentError("reliability_bins must be positive")
        if (
            not isinstance(self.minimum_decision_lead, timedelta)
            or self.minimum_decision_lead < timedelta(0)
        ):
            raise WeatherExperimentError(
                "minimum_decision_lead must be a non-negative timedelta"
            )
        if (
            not isinstance(self.maximum_price_age, timedelta)
            or self.maximum_price_age <= timedelta(0)
        ):
            raise WeatherExperimentError(
                "maximum_price_age must be a positive timedelta"
            )
        object.__setattr__(self, "bias_grid", bias_grid)
        object.__setattr__(self, "standard_deviation_grid", standard_grid)
        object.__setattr__(self, "edge_threshold_candidates", threshold_grid)
        object.__setattr__(self, "train_fraction", train_fraction)
        object.__setattr__(self, "validation_fraction", validation_fraction)


@dataclass(frozen=True)
class ExperimentExclusion:
    event_id: str
    reason: str
    detail: str

    def artifact_payload(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class IntervalGridEvaluation:
    bias: Decimal
    standard_deviation: Decimal
    log_likelihood: Decimal

    def artifact_payload(self) -> dict[str, Decimal]:
        return {
            "bias": self.bias,
            "standard_deviation": self.standard_deviation,
            "log_likelihood": self.log_likelihood,
        }


@dataclass(frozen=True)
class UnitIntervalCalibration:
    unit: str
    bias: Decimal
    standard_deviation: Decimal
    log_likelihood: Decimal
    n_train: int
    grid_evaluations: tuple[IntervalGridEvaluation, ...]

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "bias": self.bias,
            "standard_deviation": self.standard_deviation,
            "log_likelihood": self.log_likelihood,
            "n_train": self.n_train,
            "label_likelihood": "interval_censored_resolved_bin",
            "grid_evaluations": [
                evaluation.artifact_payload()
                for evaluation in self.grid_evaluations
            ],
        }


@dataclass(frozen=True)
class CohortCalibrationDiagnostic:
    cohort_key: str
    unit: str
    n_train: int
    status: str
    bias: Optional[Decimal]
    standard_deviation: Optional[Decimal]
    log_likelihood: Optional[Decimal]

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "cohort_key": self.cohort_key,
            "unit": self.unit,
            "n_train": self.n_train,
            "status": self.status,
            "bias": self.bias,
            "standard_deviation": self.standard_deviation,
            "log_likelihood": self.log_likelihood,
            "use": "diagnostic_only_not_main_trading_model",
        }


@dataclass(frozen=True)
class IntervalCalibration:
    by_unit: Mapping[str, UnitIntervalCalibration]
    training_event_ids: tuple[str, ...]
    unbiased_normal_std_by_unit: Mapping[str, Decimal]
    cohort_coverage: Mapping[str, Mapping[str, int]]
    cohort_diagnostics: tuple[CohortCalibrationDiagnostic, ...]

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "training_event_ids": list(self.training_event_ids),
            "by_unit": {
                unit: calibration.artifact_payload()
                for unit, calibration in sorted(self.by_unit.items())
            },
            "unbiased_normal_baseline": {
                "bias": ZERO,
                "standard_deviation_by_unit": dict(
                    sorted(self.unbiased_normal_std_by_unit.items())
                ),
                "fit": "train_only_interval_censored_likelihood",
            },
            "cohort_coverage": {
                dimension: dict(sorted(counts.items()))
                for dimension, counts in sorted(self.cohort_coverage.items())
            },
            "cohort_diagnostics": [
                diagnostic.artifact_payload()
                for diagnostic in self.cohort_diagnostics
            ],
        }


@dataclass(frozen=True)
class WeatherExperimentSplits:
    """Two honest robustness views rather than a false all-at-once split."""

    primary_date: EventSplits
    city_holdout: EventSplits
    primary_date_universe: EventSplits
    city_holdout_universe: EventSplits

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "primary_date": {
                name: list(values)
                for name, values in self.primary_date.as_dict().items()
            },
            "city_holdout": {
                name: list(values)
                for name, values in self.city_holdout.as_dict().items()
            },
            "primary_date_universe": {
                name: list(values)
                for name, values in self.primary_date_universe.as_dict().items()
            },
            "city_holdout_universe": {
                name: list(values)
                for name, values in self.city_holdout_universe.as_dict().items()
            },
            "semantics": {
                "universe": (
                    "folds frozen before inspecting winner bin, tail status, "
                    "forecast availability, or market-price availability"
                ),
                "primary_date": (
                    "eligible rows retained inside frozen whole-local-date folds"
                ),
                "city_holdout": (
                    "eligible rows retained inside frozen whole-city folds"
                ),
            },
        }


@dataclass(frozen=True)
class WeatherEventPrediction:
    event_id: str
    city: str
    local_date: str
    unit: str
    split: str
    independence_group: str
    provider_independence_group: str
    decision_at: datetime
    winner_market_id: str
    winner_bin_label: str
    observed_label_kind: str
    forecast_extreme: Decimal
    forecast_probabilities: Mapping[str, Decimal]
    market_probabilities: Mapping[str, Decimal]
    climatology_probabilities: Mapping[str, Decimal]
    normal_probabilities: Mapping[str, Decimal]
    raw_market_prices: Mapping[str, Decimal]
    price_observed_at: Mapping[str, datetime]
    forecast_provider: str
    forecast_model: str
    forecast_initialized_at: datetime
    forecast_available_at: datetime
    forecast_acquired_at: datetime
    forecast_sha256: str
    price_history_sha256: Mapping[str, str]
    station_code: str
    station_latitude: Decimal
    station_longitude: Decimal
    station_sha256: str

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "city": self.city,
            "local_date": self.local_date,
            "unit": self.unit,
            "split": self.split,
            "independence_group": self.independence_group,
            "provider_independence_group": self.provider_independence_group,
            "decision_at": self.decision_at.isoformat(),
            "winner_market_id": self.winner_market_id,
            "winner_bin_label": self.winner_bin_label,
            "observed_label_kind": self.observed_label_kind,
            "forecast_extreme": self.forecast_extreme,
            "forecast_probabilities": dict(self.forecast_probabilities),
            "market_probabilities": dict(self.market_probabilities),
            "climatology_probabilities": dict(self.climatology_probabilities),
            "normal_probabilities": dict(self.normal_probabilities),
            "raw_market_prices": dict(self.raw_market_prices),
            "price_observed_at": {
                market_id: observed_at.isoformat()
                for market_id, observed_at in self.price_observed_at.items()
            },
            "forecast_provenance": {
                "provider": self.forecast_provider,
                "model": self.forecast_model,
                "initialized_at": self.forecast_initialized_at.isoformat(),
                "available_at": self.forecast_available_at.isoformat(),
                # Acquisition may happen after the historical decision; the
                # exact run's public available_at is the decision-time gate.
                "acquired_at": self.forecast_acquired_at.isoformat(),
                "sha256": self.forecast_sha256,
            },
            "price_history_sha256": dict(self.price_history_sha256),
            "station_provenance": {
                "icao": self.station_code,
                "latitude": self.station_latitude,
                "longitude": self.station_longitude,
                "sha256": self.station_sha256,
            },
            "data_level": "L1",
            "fill_evidence": "none_l1_shadow",
        }


@dataclass(frozen=True)
class ModelMetrics:
    n_events: int
    n_independence_groups: int
    mean_brier: Optional[Decimal]
    mean_log_loss: Optional[Decimal]
    nominal_event_mean_brier: Optional[Decimal]
    nominal_event_mean_log_loss: Optional[Decimal]
    brier_cluster_ci_lower: Optional[Decimal]
    brier_cluster_ci_upper: Optional[Decimal]
    log_loss_cluster_ci_lower: Optional[Decimal]
    log_loss_cluster_ci_upper: Optional[Decimal]
    bootstrap_resamples: int
    bootstrap_seed: int
    reliability: tuple[ReliabilityBucket, ...]

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "n_events": self.n_events,
            "n_independence_groups": self.n_independence_groups,
            "mean_brier": self.mean_brier,
            "mean_log_loss": self.mean_log_loss,
            "mean_weighting": "equal_weight_per_local_date_group",
            "nominal_event_mean_brier": self.nominal_event_mean_brier,
            "nominal_event_mean_log_loss": self.nominal_event_mean_log_loss,
            "brier_cluster_ci_95": {
                "lower": self.brier_cluster_ci_lower,
                "upper": self.brier_cluster_ci_upper,
            },
            "log_loss_cluster_ci_95": {
                "lower": self.log_loss_cluster_ci_lower,
                "upper": self.log_loss_cluster_ci_upper,
            },
            "bootstrap": {
                "unit": "local_date_group",
                "resamples": self.bootstrap_resamples,
                "seed": self.bootstrap_seed,
            },
            "reliability_weighting": (
                "nominal outcome rows; descriptive only, not inference unit"
            ),
            "reliability": [
                {
                    "lower_bound": bucket.lower_bound,
                    "upper_bound": bucket.upper_bound,
                    "count": bucket.count,
                    "mean_probability": bucket.mean_probability,
                    "empirical_frequency": bucket.empirical_frequency,
                }
                for bucket in self.reliability
            ],
        }


@dataclass(frozen=True)
class WeatherTradeRow:
    event_id: str
    independence_group: str
    split: str
    execution_style: str
    action: str
    diagnostic_action: str
    policy_approved: bool
    diagnostic_only: bool
    market_id: str
    winner_market_id: str
    fair_probability: Decimal
    market_price: Decimal
    gross_edge: Decimal
    taker_fee: Decimal
    maker_fee: Decimal
    slippage: Decimal
    model_margin: Decimal
    net_edge: Decimal
    edge_threshold: Decimal
    threshold_origin: str
    reward: Decimal
    resolved_payoff: Decimal
    shadow_profit: Optional[Decimal]
    data_level: str = "L1"
    fill_evidence: str = "none_l1_shadow"
    is_realized: bool = False
    explainable_fill: bool = False
    profit_semantics: str = "diagnostic_counterfactual_l1_not_realized"

    def artifact_payload(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in (
                "event_id",
                "independence_group",
                "split",
                "execution_style",
                "action",
                "diagnostic_action",
                "policy_approved",
                "diagnostic_only",
                "market_id",
                "winner_market_id",
                "fair_probability",
                "market_price",
                "gross_edge",
                "taker_fee",
                "maker_fee",
                "slippage",
                "model_margin",
                "net_edge",
                "edge_threshold",
                "threshold_origin",
                "reward",
                "resolved_payoff",
                "shadow_profit",
                "data_level",
                "fill_evidence",
                "is_realized",
                "explainable_fill",
                "profit_semantics",
            )
        }


def _policy_artifact(
    trade_rows: Sequence[WeatherTradeRow],
    *,
    approved: bool,
    reason_codes: Sequence[str],
    diagnostic_threshold: Decimal,
) -> dict[str, Any]:
    formal_candidates: dict[str, int] = {}
    diagnostic: dict[str, Any] = {}
    for split in ("validation", "test"):
        split_rows = [row for row in trade_rows if row.split == split]
        formal_candidates[split] = len(
            {
                row.event_id
                for row in split_rows
                if row.action == "policy_candidate"
            }
        )
        taker_rows = [
            row
            for row in split_rows
            if row.execution_style == "taker_shadow"
            and row.diagnostic_action == "diagnostic_counterfactual"
        ]
        maker_rows = [
            row
            for row in split_rows
            if row.execution_style == "maker_shadow"
            and row.diagnostic_action == "diagnostic_counterfactual"
        ]
        diagnostic[split] = {
            "taker_counterfactual_events": len(taker_rows),
            "taker_counterfactual_groups": len(
                {row.independence_group for row in taker_rows}
            ),
            "taker_counterfactual_profit_sum": sum(
                (
                    row.shadow_profit
                    for row in taker_rows
                    if row.shadow_profit is not None
                ),
                ZERO,
            ),
            "maker_counterfactual_events": len(maker_rows),
            "maker_counterfactual_profit": None,
            "maker_counterfactual_profit_reason": (
                "L1 has no maker queue or fill evidence"
            ),
            "role": (
                "diagnostic_counterfactual_not_policy_candidate_or_fill"
            ),
        }
    return {
        "status": "shadow_policy_approved" if approved else "abstain",
        "action": "evaluate_shadow_candidates" if approved else "no_trade",
        "approved": approved,
        "reason_codes": list(reason_codes),
        "formal_candidate_events": formal_candidates,
        "diagnostic_counterfactual": diagnostic,
        "diagnostic_threshold": diagnostic_threshold,
        "scope": (
            "formal policy decision; diagnostic counterfactual rows never "
            "authorize orders or imply fills"
        ),
    }


@dataclass(frozen=True)
class ThresholdEvaluation:
    threshold: Decimal
    validation_mean_l1_shadow_profit: Decimal
    diagnostic_counterfactual_events: int
    diagnostic_counterfactual_groups: int
    l1_shadow_cluster_ci_lower: Decimal
    l1_shadow_cluster_ci_upper: Decimal
    bootstrap_seed: int

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "validation_mean_l1_shadow_profit": (
                self.validation_mean_l1_shadow_profit
            ),
            "diagnostic_counterfactual_events": (
                self.diagnostic_counterfactual_events
            ),
            "diagnostic_counterfactual_groups": (
                self.diagnostic_counterfactual_groups
            ),
            "formal_policy_candidate_events": 0,
            "event_role": (
                "diagnostic_counterfactual_not_policy_candidate"
            ),
            "l1_shadow_cluster_ci_95": {
                "lower": self.l1_shadow_cluster_ci_lower,
                "upper": self.l1_shadow_cluster_ci_upper,
            },
            "bootstrap_seed": self.bootstrap_seed,
            "mean_weighting": "equal_weight_per_local_date_group",
            "data_level": "L1",
            "fill_evidence": "none_l1_shadow",
        }


@dataclass(frozen=True)
class WeatherRobustnessView:
    """A separately fitted split view with its own validation-frozen gate."""

    name: str
    splits: EventSplits
    calibration: IntervalCalibration
    selected_threshold: Decimal
    threshold_evaluations: tuple[ThresholdEvaluation, ...]
    threshold_neighbor_stable: bool
    policy_approved: bool
    policy_reason_codes: tuple[str, ...]
    threshold_selection_event_ids: tuple[str, ...]
    metrics: Mapping[str, Mapping[str, ModelMetrics]]
    event_predictions: tuple[WeatherEventPrediction, ...]
    trade_rows: tuple[WeatherTradeRow, ...]

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "splits": {
                split: list(event_ids)
                for split, event_ids in self.splits.as_dict().items()
            },
            "calibration": self.calibration.artifact_payload(),
            "selected_threshold": self.selected_threshold,
            "selected_threshold_role": (
                "diagnostic_best_only"
                if not self.policy_approved
                else "validation_approved_policy_threshold"
            ),
            "threshold_evaluations": [
                evaluation.artifact_payload()
                for evaluation in self.threshold_evaluations
            ],
            "threshold_neighbor_stable": self.threshold_neighbor_stable,
            "threshold_selected_on": "validation",
            "threshold_selection_event_ids": list(
                self.threshold_selection_event_ids
            ),
            "policy": _policy_artifact(
                self.trade_rows,
                approved=self.policy_approved,
                reason_codes=self.policy_reason_codes,
                diagnostic_threshold=self.selected_threshold,
            ),
            "metrics": {
                split: {
                    model: values.artifact_payload()
                    for model, values in sorted(models.items())
                }
                for split, models in self.metrics.items()
            },
            "event_predictions": [
                row.artifact_payload() for row in self.event_predictions
            ],
            "trade_rows": [row.artifact_payload() for row in self.trade_rows],
        }


@dataclass(frozen=True)
class WeatherExperimentResult:
    config: WeatherExperimentConfig
    splits: WeatherExperimentSplits
    calibration: IntervalCalibration
    selected_threshold: Decimal
    threshold_evaluations: tuple[ThresholdEvaluation, ...]
    threshold_neighbor_stable: bool
    policy_approved: bool
    policy_reason_codes: tuple[str, ...]
    threshold_selected_on: str
    threshold_selection_event_ids: tuple[str, ...]
    metrics: Mapping[str, Mapping[str, ModelMetrics]]
    event_predictions: tuple[WeatherEventPrediction, ...]
    trade_rows: tuple[WeatherTradeRow, ...]
    city_holdout_view: WeatherRobustnessView
    exclusions: tuple[ExperimentExclusion, ...]
    classification: str
    gate_reasons: tuple[str, ...]
    independent_settled_events: int
    explainable_fills: int
    data_level: str = "L1"
    evidence_limitation: str = "public price history; not L2 or fill evidence"
    label_semantics: str = "resolved_winner_bin_only"
    reward_assumption: Decimal = ZERO

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "weather-experiment/v2",
            "config": {
                "bias_grid": list(self.config.bias_grid),
                "standard_deviation_grid": list(
                    self.config.standard_deviation_grid
                ),
                "edge_threshold_candidates": list(
                    self.config.edge_threshold_candidates
                ),
                "taker_fee": self.config.taker_fee,
                "taker_fee_rate": self.config.taker_fee_rate,
                "taker_fee_exponent": self.config.taker_fee_exponent,
                "taker_fee_source": self.config.taker_fee_source,
                "maker_fee": self.config.maker_fee,
                "slippage": self.config.slippage,
                "model_margin": self.config.model_margin,
                "random_seed": self.config.random_seed,
                "train_fraction": self.config.train_fraction,
                "validation_fraction": self.config.validation_fraction,
                "minimum_independent_events": (
                    self.config.minimum_independent_events
                ),
                "minimum_explainable_fills": (
                    self.config.minimum_explainable_fills
                ),
                "reliability_bins": self.config.reliability_bins,
                "minimum_cohort_train_events": (
                    self.config.minimum_cohort_train_events
                ),
                "bootstrap_resamples": self.config.bootstrap_resamples,
                "minimum_decision_lead_seconds": _timedelta_seconds(
                    self.config.minimum_decision_lead
                ),
                "maximum_price_age_seconds": _timedelta_seconds(
                    self.config.maximum_price_age
                ),
            },
            "data_level": self.data_level,
            "evidence_limitation": self.evidence_limitation,
            "label_semantics": self.label_semantics,
            "splits": self.splits.artifact_payload(),
            "calibration": self.calibration.artifact_payload(),
            "threshold": {
                "value": self.selected_threshold,
                "value_role": (
                    "diagnostic_best_only"
                    if not self.policy_approved
                    else "validation_approved_policy_threshold"
                ),
                "best_diagnostic_value": self.selected_threshold,
                "policy_value": (
                    self.selected_threshold
                    if self.policy_approved
                    else None
                ),
                "selected_on": self.threshold_selected_on,
                "selection_event_ids": list(self.threshold_selection_event_ids),
                "evaluations": [
                    evaluation.artifact_payload()
                    for evaluation in self.threshold_evaluations
                ],
                "neighbor_stable": self.threshold_neighbor_stable,
            },
            "policy": _policy_artifact(
                self.trade_rows,
                approved=self.policy_approved,
                reason_codes=self.policy_reason_codes,
                diagnostic_threshold=self.selected_threshold,
            ),
            "metrics": {
                split: {
                    name: values.artifact_payload()
                    for name, values in sorted(models.items())
                }
                for split, models in self.metrics.items()
            },
            "event_predictions": [
                row.artifact_payload() for row in self.event_predictions
            ],
            "trade_rows": [row.artifact_payload() for row in self.trade_rows],
            "city_holdout_robustness": (
                self.city_holdout_view.artifact_payload()
            ),
            "exclusions": [row.artifact_payload() for row in self.exclusions],
            "classification": self.classification,
            "gate_reasons": list(self.gate_reasons),
            "evidence_counts": {
                "scope": "frozen_test_split_only",
                "independent_settled_events": self.independent_settled_events,
                "explainable_fills": self.explainable_fills,
            },
            "economics": {
                "taker_fee": self.config.taker_fee,
                "taker_fee_rate": self.config.taker_fee_rate,
                "taker_fee_exponent": self.config.taker_fee_exponent,
                "taker_fee_source": self.config.taker_fee_source,
                "maker_fee": self.config.maker_fee,
                "slippage": self.config.slippage,
                "model_margin": self.config.model_margin,
                "reward_assumption": self.reward_assumption,
            },
            "safety": {
                "network_used": False,
                "orders_submitted": False,
                "authenticated": False,
                "realized_pnl_claimed": False,
            },
            "model_limitations": [
                (
                    "main interval calibration is pooled by temperature unit; "
                    "city, station, season, lead, provider/model and metric "
                    "cohorts are train-only diagnostics and are marked "
                    "insufficient when below their configured sample gate"
                ),
                (
                    "Decimal CDF uses a documented five-term approximation "
                    "with about 7.5e-8 absolute-error bound"
                ),
                (
                    "climatology is a train-only unit-pooled bin-position "
                    "baseline, not a station-season climatology"
                ),
                (
                    "events whose resolved winner is an open-ended tail bin "
                    "are excluded; all scores and shadow diagnostics are "
                    "conditional on the non-tail-winner scope and do not "
                    "represent the full weather market universe"
                ),
                "L1 history cannot explain taker or maker fills",
            ],
        }


@dataclass(frozen=True)
class _PreparedEvent:
    raw: Mapping[str, Any]
    spec: WeatherEventSpec
    evidence: WeatherEventEvidence
    winner_market_id: str
    winner_bin_label: str
    winner_index: int
    forecast_extreme: Decimal
    raw_market_prices: Mapping[str, Decimal]
    market_probabilities: Mapping[str, Decimal]
    price_observed_at: Mapping[str, datetime]
    price_history_sha256: Mapping[str, str]


def _json_array(value: Any, *, field: str) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WeatherExperimentError(f"{field} is invalid JSON") from exc
    if not isinstance(value, list):
        raise WeatherExperimentError(f"{field} must be an array")
    return value


def _resolved_winner(
    raw: Mapping[str, Any],
    spec: WeatherEventSpec,
) -> tuple[str, str, int]:
    if raw.get("closed") is not True:
        raise WeatherExperimentError("event_not_settled")
    markets = raw.get("markets")
    if not isinstance(markets, list):
        raise WeatherExperimentError("markets_missing")
    yes_prices: dict[str, Decimal] = {}
    for index, market in enumerate(markets):
        if not isinstance(market, Mapping):
            raise WeatherExperimentError(f"market_{index}_invalid")
        if market.get("closed") is not True:
            raise WeatherExperimentError(f"market_{index}_not_closed")
        resolution_status = str(
            market.get("umaResolutionStatus") or ""
        ).strip().casefold()
        if resolution_status != "resolved":
            raise WeatherExperimentError(f"market_{index}_not_resolved")
        market_id = str(market.get("id") or "")
        outcomes = [str(item) for item in _json_array(
            market.get("outcomes"), field=f"market_{index}_outcomes"
        )]
        prices = [
            _decimal(item, field=f"market_{index}_outcome_price")
            for item in _json_array(
                market.get("outcomePrices"),
                field=f"market_{index}_outcome_prices",
            )
        ]
        if len(outcomes) != 2 or len(prices) != 2 or set(outcomes) != {"Yes", "No"}:
            raise WeatherExperimentError(f"market_{index}_binary_outcomes_invalid")
        price_by_outcome = dict(zip(outcomes, prices))
        if price_by_outcome["Yes"] not in {ZERO, ONE}:
            raise WeatherExperimentError(f"market_{index}_not_final")
        if price_by_outcome["No"] != ONE - price_by_outcome["Yes"]:
            raise WeatherExperimentError(f"market_{index}_final_prices_inconsistent")
        if market_id in yes_prices:
            raise WeatherExperimentError("duplicate_market_id")
        yes_prices[market_id] = price_by_outcome["Yes"]
    winners = [market_id for market_id, price in yes_prices.items() if price == ONE]
    if len(winners) != 1 or any(
        price != ZERO for market_id, price in yes_prices.items() if market_id not in winners
    ):
        raise WeatherExperimentError("ambiguous_resolved_winner")
    winner_id = winners[0]
    for index, weather_bin in enumerate(spec.bins):
        if weather_bin.market_id == winner_id:
            return winner_id, weather_bin.label, index
    raise WeatherExperimentError("winner_absent_from_parsed_bins")


def _normalize(values: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    if not values:
        raise WeatherExperimentError("probability source is empty")
    total = sum(values.values(), ZERO)
    if total <= ZERO:
        raise WeatherExperimentError("probability source has non-positive total")
    raw = {
        key: (value / total).quantize(
            PROBABILITY_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )
        for key, value in values.items()
    }
    correction_key = min(
        raw,
        key=lambda key: (-raw[key], key),
    )
    raw[correction_key] = ONE - sum(
        (value for key, value in raw.items() if key != correction_key),
        ZERO,
    )
    if any(value < ZERO for value in raw.values()):
        raise WeatherExperimentError("probability normalization became negative")
    return MappingProxyType(raw)


def _standard_normal_cdf(z_score: Decimal) -> Decimal:
    """Deterministic Decimal normal-CDF approximation.

    This is the classic five-term tail approximation (absolute error below
    roughly 7.5e-8).  It avoids binary floats in fitted probabilities and keeps
    the approximation explicit in the artifact limitations.
    """

    z = _decimal(z_score, field="z_score")
    if z <= Decimal("-12"):
        return ZERO
    if z >= Decimal("12"):
        return ONE
    absolute = abs(z)
    t = ONE / (ONE + Decimal("0.2316419") * absolute)
    polynomial = t * (
        Decimal("0.319381530")
        + t
        * (
            Decimal("-0.356563782")
            + t
            * (
                Decimal("1.781477937")
                + t
                * (
                    Decimal("-1.821255978")
                    + t * Decimal("1.330274429")
                )
            )
        )
    )
    density = (
        (-(absolute * absolute) / Decimal("2")).exp()
        / DECIMAL_TWO_PI.sqrt()
    )
    upper = ONE - density * polynomial
    value = upper if z >= ZERO else ONE - upper
    return min(ONE, max(ZERO, value))


def _normal_cdf(
    value: Decimal,
    *,
    mean: Decimal,
    std: Decimal,
) -> Decimal:
    return _standard_normal_cdf((value - mean) / std)


def _interval_gaussian_probabilities(
    spec: WeatherEventSpec,
    *,
    forecast: Decimal,
    bias: Decimal,
    standard_deviation: Decimal,
) -> Mapping[str, Decimal]:
    mean = forecast + bias
    raw: dict[str, Decimal] = {}
    for weather_bin in spec.bins:
        lower = (
            ZERO
            if weather_bin.lower_bound is None
            else _normal_cdf(
                weather_bin.lower_bound,
                mean=mean,
                std=standard_deviation,
            )
        )
        upper = (
            ONE
            if weather_bin.upper_bound is None
            else _normal_cdf(
                weather_bin.upper_bound,
                mean=mean,
                std=standard_deviation,
            )
        )
        raw[weather_bin.market_id] = max(ZERO, upper - lower)
    return _normalize(raw)


def _forecast_unit(run: AcquiredForecastRun) -> str:
    units = run.raw.get("hourly_units") if isinstance(run.raw, Mapping) else None
    if not isinstance(units, Mapping):
        raise WeatherExperimentError("forecast_unit_unknown")
    raw = str(units.get("temperature_2m") or "").strip().casefold()
    if raw in {"°c", "c", "celsius"}:
        return "C"
    if raw in {"°f", "f", "fahrenheit"}:
        return "F"
    raise WeatherExperimentError("forecast_unit_unknown")


def _forecast_extreme(
    spec: WeatherEventSpec,
    run: AcquiredForecastRun,
    *,
    decision_at: datetime,
    station: AviationStation,
) -> Decimal:
    if run.exact_run is not True or run.decision_vintage is not True:
        raise WeatherExperimentError("forecast_not_exact_single_run")
    if run.data_kind != "single_run_vintage":
        raise WeatherExperimentError("forecast_data_kind_invalid")
    normalized_model = run.model.casefold().replace("-", "_").replace(" ", "_")
    if (
        not normalized_model
        or "," in normalized_model
        or "seamless" in normalized_model
        or normalized_model in {"auto", "default", "best_match"}
        or normalized_model.startswith("era5")
    ):
        raise WeatherExperimentError("forecast_model_not_exact_operational")
    _validate_acquisition(run.acquisition, field="forecast")
    initialized_at = _aware_utc(
        run.initialized_at,
        field="forecast initialized_at",
    )
    available_at = _aware_utc(
        run.available_at,
        field="forecast available_at",
    )
    fetched_at = _aware_utc(run.fetched_at, field="forecast fetched_at")
    acquired_at = _aware_utc(
        run.acquisition.provenance.received_at,
        field="forecast acquisition received_at",
    )
    if fetched_at != acquired_at:
        raise WeatherExperimentError("forecast_acquisition_time_mismatch")
    if (
        not isinstance(run.release_delay, timedelta)
        or run.release_delay < timedelta(0)
        or not isinstance(run.safety_delay, timedelta)
        or run.safety_delay < timedelta(0)
    ):
        raise WeatherExperimentError("forecast_release_delay_invalid")
    if available_at != initialized_at + run.release_delay + run.safety_delay:
        raise WeatherExperimentError("forecast_available_at_inconsistent")
    if initialized_at.second or initialized_at.microsecond:
        raise WeatherExperimentError("forecast_initialization_not_minute_exact")
    if initialized_at > decision_at:
        raise WeatherExperimentError("forecast_initialized_after_decision")
    if available_at > decision_at:
        raise WeatherExperimentError("forecast_unavailable_at_decision")
    if run.timezone != spec.timezone:
        raise WeatherExperimentError("forecast_timezone_mismatch")
    if station.icao != spec.station_code:
        raise WeatherExperimentError("forecast_station_code_mismatch")
    if (
        run.requested_latitude != station.latitude
        or run.requested_longitude != station.longitude
    ):
        raise WeatherExperimentError(
            "forecast_requested_coordinates_mismatch_station"
        )
    source_unit = _forecast_unit(run)
    target_zone = ZoneInfo(spec.timezone)
    target_start = datetime.combine(
        spec.local_date,
        datetime.min.time(),
        tzinfo=target_zone,
    ).astimezone(timezone.utc)
    target_end = datetime.combine(
        spec.local_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=target_zone,
    ).astimezone(timezone.utc)
    expected_utc_hours: set[datetime] = set()
    cursor = target_start
    while cursor < target_end:
        expected_utc_hours.add(cursor)
        cursor += timedelta(hours=1)

    target_temperatures: dict[datetime, Decimal] = {}
    for index, hour in enumerate(run.hours):
        _aware_utc(hour.local_at, field=f"forecast hour {index} local_at")
        utc_at = _aware_utc(
            hour.utc_at,
            field=f"forecast hour {index} utc_at",
        )
        if hour.local_at.astimezone(timezone.utc) != utc_at:
            raise WeatherExperimentError("forecast_hour_time_mismatch")
        if not target_start <= utc_at < target_end:
            continue
        if (
            utc_at.minute
            or utc_at.second
            or utc_at.microsecond
        ):
            raise WeatherExperimentError("forecast_hour_not_hour_aligned")
        if hour.temperature_2m is None:
            raise WeatherExperimentError("forecast_target_hour_missing_temperature")
        if not isinstance(hour.temperature_2m, Decimal):
            raise WeatherExperimentError(
                "forecast_temperature_must_be_decimal"
            )
        previous = target_temperatures.get(utc_at)
        if previous is not None:
            raise WeatherExperimentError(
                "forecast_duplicate_target_hour"
            )
        target_temperatures[utc_at] = hour.temperature_2m
    if set(target_temperatures) != expected_utc_hours:
        missing = len(expected_utc_hours - set(target_temperatures))
        unexpected = len(set(target_temperatures) - expected_utc_hours)
        raise WeatherExperimentError(
            "forecast_local_day_coverage_incomplete:"
            f"missing={missing},unexpected={unexpected}"
        )
    points = tuple(
        HourlyTemperature(valid_at=utc_at, temperature=temperature)
        for utc_at, temperature in sorted(target_temperatures.items())
    )
    extreme = local_day_extreme(
        points,
        local_date=spec.local_date,
        timezone_name=spec.timezone,
        metric=spec.metric,
        minimum_points=len(expected_utc_hours),
    )
    if source_unit == spec.unit:
        return extreme
    if source_unit == "C" and spec.unit == "F":
        return extreme * Decimal("9") / Decimal("5") + Decimal("32")
    if source_unit == "F" and spec.unit == "C":
        return (extreme - Decimal("32")) * Decimal("5") / Decimal("9")
    raise WeatherExperimentError("forecast_event_unit_mismatch")


def _market_as_of(
    spec: WeatherEventSpec,
    evidence: WeatherEventEvidence,
    *,
    maximum_price_age: timedelta,
) -> tuple[
    Mapping[str, Decimal],
    Mapping[str, Decimal],
    Mapping[str, datetime],
    Mapping[str, str],
]:
    raw_prices: dict[str, Decimal] = {}
    observed: dict[str, datetime] = {}
    source_hashes: dict[str, str] = {}
    for weather_bin in spec.bins:
        history = evidence.price_histories.get(weather_bin.yes_token_id)
        if history is None:
            raise WeatherExperimentError(
                f"market_asof_missing:{weather_bin.yes_token_id}"
            )
        if history.data_level != "L1":
            raise WeatherExperimentError("price_history_data_level_not_l1")
        _validate_acquisition(
            history.acquisition,
            field="price history",
        )
        if (
            not isinstance(history.fidelity_minutes, int)
            or isinstance(history.fidelity_minutes, bool)
            or history.fidelity_minutes < 1
        ):
            raise WeatherExperimentError("price_history_fidelity_invalid")
        start_at = _aware_utc(
            history.start_at,
            field="price history start_at",
        )
        end_at = _aware_utc(
            history.end_at,
            field="price history end_at",
        )
        if start_at >= end_at:
            raise WeatherExperimentError("price_history_window_invalid")
        observed_prices: dict[datetime, Decimal] = {}
        for history_point in history.points:
            observed_at = _aware_utc(
                history_point.observed_at,
                field="price history observed_at",
            )
            if not start_at <= observed_at <= end_at:
                raise WeatherExperimentError(
                    "price_history_point_outside_window"
                )
            if not isinstance(history_point.price, Decimal):
                raise WeatherExperimentError(
                    "price_history_price_must_be_decimal"
                )
            if (
                not history_point.price.is_finite()
                or not ZERO <= history_point.price <= ONE
            ):
                raise WeatherExperimentError("price_history_price_invalid")
            previous_price = observed_prices.get(observed_at)
            if (
                previous_price is not None
                and previous_price != history_point.price
            ):
                raise WeatherExperimentError(
                    "price_history_conflicting_duplicate_timestamp"
                )
            observed_prices[observed_at] = history_point.price
        try:
            point = history.as_of(evidence.decision_at)
        except PriceNotAvailableError as exc:
            raise WeatherExperimentError(
                f"market_asof_missing:{weather_bin.yes_token_id}"
            ) from exc
        if point.data_level != "L1":
            raise WeatherExperimentError("price_asof_data_level_not_l1")
        if evidence.decision_at - point.observed_at > maximum_price_age:
            raise WeatherExperimentError(
                f"market_asof_stale:{weather_bin.yes_token_id}"
            )
        raw_prices[weather_bin.market_id] = point.price
        observed[weather_bin.market_id] = point.observed_at
        source_hashes[weather_bin.market_id] = history.acquisition.sha256
    return (
        MappingProxyType(raw_prices),
        _normalize(raw_prices),
        MappingProxyType(observed),
        MappingProxyType(source_hashes),
    )


def _exclusion(event_id: str, reason: str, detail: str) -> ExperimentExclusion:
    return ExperimentExclusion(
        event_id=event_id or "<missing>",
        reason=reason,
        detail=detail,
    )


_SAFE_MARKET_EVIDENCE_CODES = frozenset(
    {
        "price_asof_data_level_not_l1",
        "price_history_conflicting_duplicate_timestamp",
        "price_history_data_level_not_l1",
        "price_history_fidelity_invalid",
        "price_history_point_outside_window",
        "price_history_price_invalid",
        "price_history_price_must_be_decimal",
        "price_history_window_invalid",
    }
)
_SAFE_FORECAST_EVIDENCE_CODES = frozenset(
    {
        "forecast_acquisition_time_mismatch",
        "forecast_available_at_inconsistent",
        "forecast_data_kind_invalid",
        "forecast_duplicate_target_hour",
        "forecast_event_unit_mismatch",
        "forecast_hour_not_hour_aligned",
        "forecast_hour_time_mismatch",
        "forecast_initialization_not_minute_exact",
        "forecast_initialized_after_decision",
        "forecast_model_not_exact_operational",
        "forecast_not_exact_single_run",
        "forecast_release_delay_invalid",
        "forecast_requested_coordinates_mismatch_station",
        "forecast_station_code_mismatch",
        "forecast_target_hour_missing_temperature",
        "forecast_temperature_must_be_decimal",
        "forecast_timezone_mismatch",
        "forecast_unavailable_at_decision",
        "forecast_unit_unknown",
    }
)


def _safe_exception_detail(error: BaseException, *, code: str) -> str:
    details = safe_error_details(error, code=code)
    return (
        f"error_type={details['error_type']};"
        f"error_code={details['error_code']}"
    )


def _market_evidence_reason(error: WeatherExperimentError) -> str:
    """Map internal validation failures to value-free persisted reason codes."""

    message = error.args[0] if len(error.args) == 1 else None
    if not isinstance(message, str):
        return "market_evidence_invalid"
    if message.startswith("market_asof_missing:"):
        return "market_asof_missing:token_omitted"
    if message.startswith("market_asof_stale:"):
        return "market_asof_stale:token_omitted"
    if message in _SAFE_MARKET_EVIDENCE_CODES:
        return f"market_evidence_invalid:{message}"
    return "market_evidence_invalid"


def _forecast_evidence_code(error: BaseException) -> str:
    message = error.args[0] if len(error.args) == 1 else None
    if not isinstance(message, str):
        return "forecast_evidence_invalid"
    if message.startswith("forecast_local_day_coverage_incomplete:"):
        return "forecast_local_day_coverage_incomplete"
    if message in _SAFE_FORECAST_EVIDENCE_CODES:
        return message
    return "forecast_evidence_invalid"


def _prepare(
    raw_events: Iterable[Mapping[str, Any]],
    *,
    provider: WeatherEvidenceProvider,
    config: WeatherExperimentConfig,
) -> tuple[
    tuple[_PreparedEvent, ...],
    tuple[ExperimentExclusion, ...],
    tuple[WeatherEventSpec, ...],
]:
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: set[str] = set()
    exclusions: list[ExperimentExclusion] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            exclusions.append(
                _exclusion(
                    "<missing>",
                    "rule_conflict:invalid_event",
                    "event must be an object",
                )
            )
            continue
        event_id = str(raw.get("id") or "")
        if not event_id:
            exclusions.append(
                _exclusion(
                    event_id,
                    "rule_conflict:event_id_missing",
                    "Gamma event ID is missing",
                )
            )
            continue
        if event_id in raw_by_id or event_id in duplicate_ids:
            raw_by_id.pop(event_id, None)
            duplicate_ids.add(event_id)
            continue
        raw_by_id[event_id] = raw
    exclusions.extend(
        _exclusion(
            event_id,
            "rule_conflict:duplicate_event",
            "all Gamma records sharing this event ID were quarantined",
        )
        for event_id in sorted(duplicate_ids)
    )

    prepared: list[_PreparedEvent] = []
    split_universe: list[WeatherEventSpec] = []
    for event_id, raw in sorted(raw_by_id.items()):
        try:
            spec = parse_gamma_weather_event(raw)
        except (WeatherDataError, KeyError, TypeError, ValueError) as exc:
            exclusions.append(
                _exclusion(
                    event_id,
                    "rule_conflict:parse_failed",
                    _safe_exception_detail(
                        exc,
                        code="weather_rule_parse_failed",
                    ),
                )
            )
            continue
        if spec.quarantined:
            reason = ",".join(sorted(spec.quarantine_reasons))
            exclusions.append(
                _exclusion(
                    event_id,
                    f"rule_conflict:{reason}",
                    "parsed rules were quarantined",
                )
            )
            continue
        # Freeze split membership before looking at the resolved winner or any
        # after-the-fact evidence availability.  Otherwise a test-fold tail or
        # missing forecast could move earlier dates into another fold.
        split_universe.append(spec)
        try:
            winner_market_id, winner_bin_label, winner_index = _resolved_winner(
                raw, spec
            )
        except WeatherExperimentError as exc:
            exclusions.append(
                _exclusion(
                    event_id,
                    "rule_conflict:resolved_winner_invalid",
                    _safe_exception_detail(
                        exc,
                        code="resolved_winner_invalid",
                    ),
                )
            )
            continue
        if spec.bins[winner_index].tail_censored:
            exclusions.append(
                _exclusion(
                    event_id,
                    "winner_tail_censored",
                    winner_bin_label,
                )
            )
            continue

        evidence = provider.evidence_for(spec)
        if evidence is None or evidence.forecast is None:
            exclusions.append(
                _exclusion(
                    event_id,
                    "forecast_unavailable",
                    "no exact acquired forecast was supplied",
                )
            )
            continue
        target_start = datetime.combine(
            spec.local_date,
            datetime.min.time(),
            tzinfo=ZoneInfo(spec.timezone),
        ).astimezone(timezone.utc)
        decision_cutoff = target_start - config.minimum_decision_lead
        if evidence.decision_at >= decision_cutoff:
            exclusions.append(
                _exclusion(
                    event_id,
                    "decision_time_leakage",
                    (
                        f"decision_at={evidence.decision_at.isoformat()} "
                        f"must be before {decision_cutoff.isoformat()}"
                    ),
                )
            )
            continue
        try:
            (
                raw_market,
                market_probability,
                price_observed,
                price_source_hashes,
            ) = _market_as_of(
                spec,
                evidence,
                maximum_price_age=config.maximum_price_age,
            )
        except WeatherExperimentError as exc:
            reason = _market_evidence_reason(exc)
            exclusions.append(
                _exclusion(
                    event_id,
                    reason,
                    _safe_exception_detail(
                        exc,
                        code=reason,
                    ),
                )
            )
            continue
        try:
            forecast_extreme = _forecast_extreme(
                spec,
                evidence.forecast,
                decision_at=evidence.decision_at,
                station=evidence.station,
            )
        except (WeatherDataError, WeatherExperimentError) as exc:
            error_code = _forecast_evidence_code(exc)
            exclusions.append(
                _exclusion(
                    event_id,
                    "forecast_unavailable",
                    _safe_exception_detail(
                        exc,
                        code=error_code,
                    ),
                )
            )
            continue
        prepared.append(
            _PreparedEvent(
                raw=raw,
                spec=spec,
                evidence=evidence,
                winner_market_id=winner_market_id,
                winner_bin_label=winner_bin_label,
                winner_index=winner_index,
                forecast_extreme=forecast_extreme,
                raw_market_prices=raw_market,
                market_probabilities=market_probability,
                price_observed_at=price_observed,
                price_history_sha256=price_source_hashes,
            )
        )
    return (
        tuple(prepared),
        tuple(sorted(exclusions, key=lambda item: (item.event_id, item.reason))),
        tuple(split_universe),
    )


def _fit_interval_calibration(
    train: Sequence[_PreparedEvent],
    *,
    config: WeatherExperimentConfig,
    include_cohorts: bool = True,
) -> IntervalCalibration:
    if not train:
        raise WeatherExperimentError("no train events remain after exclusions")
    by_unit_rows: dict[str, list[_PreparedEvent]] = {}
    for row in train:
        by_unit_rows.setdefault(row.spec.unit, []).append(row)
    calibrations: dict[str, UnitIntervalCalibration] = {}
    unbiased_standard_deviations: dict[str, Decimal] = {}
    for unit, rows in sorted(by_unit_rows.items()):
        best: tuple[Decimal, Decimal, Decimal] | None = None
        evaluations: list[IntervalGridEvaluation] = []
        for bias in config.bias_grid:
            for standard_deviation in config.standard_deviation_grid:
                likelihood = ZERO
                for row in rows:
                    probabilities = _interval_gaussian_probabilities(
                        row.spec,
                        forecast=row.forecast_extreme,
                        bias=bias,
                        standard_deviation=standard_deviation,
                    )
                    winner_probability = max(
                        probabilities[row.winner_market_id],
                        LOG_EPSILON,
                    )
                    likelihood += winner_probability.ln()
                evaluations.append(
                    IntervalGridEvaluation(
                        bias=bias,
                        standard_deviation=standard_deviation,
                        log_likelihood=likelihood,
                    )
                )
                candidate = (likelihood, -abs(bias), -standard_deviation)
                if best is None or candidate > (
                    best[0],
                    -abs(best[1]),
                    -best[2],
                ):
                    best = (likelihood, bias, standard_deviation)
        assert best is not None
        calibrations[unit] = UnitIntervalCalibration(
            unit=unit,
            bias=best[1],
            standard_deviation=best[2],
            log_likelihood=best[0],
            n_train=len(rows),
            grid_evaluations=tuple(evaluations),
        )
        zero_bias = [
            evaluation
            for evaluation in evaluations
            if evaluation.bias == ZERO
        ]
        unbiased_standard_deviations[unit] = max(
            zero_bias,
            key=lambda evaluation: (
                evaluation.log_likelihood,
                -evaluation.standard_deviation,
            ),
        ).standard_deviation
    cohort_coverage: dict[str, dict[str, int]] = {}
    cohort_diagnostics: list[CohortCalibrationDiagnostic] = []
    if include_cohorts:
        joint_groups: dict[str, list[_PreparedEvent]] = {}

        def season(month: int) -> str:
            if month in {12, 1, 2}:
                return "DJF"
            if month in {3, 4, 5}:
                return "MAM"
            if month in {6, 7, 8}:
                return "JJA"
            return "SON"

        for row in train:
            run = row.evidence.forecast
            if run is None:
                raise WeatherExperimentError(
                    "prepared cohort row lost its forecast"
                )
            local_start = datetime.combine(
                row.spec.local_date,
                datetime.min.time(),
                tzinfo=ZoneInfo(row.spec.timezone),
            ).astimezone(timezone.utc)
            lead_hours = int(
                _timedelta_seconds(
                    local_start - row.evidence.decision_at
                )
                // Decimal("3600")
            )
            lead_bucket = f"{(lead_hours // 6) * 6}h"
            dimensions = {
                "city": row.spec.city,
                "station": row.spec.station_code,
                "season": season(row.spec.local_date.month),
                "lead_bucket": lead_bucket,
                "provider_model": f"{run.provider}/{run.model}",
                "metric": row.spec.metric,
            }
            for dimension, value in dimensions.items():
                counts = cohort_coverage.setdefault(dimension, {})
                counts[value] = counts.get(value, 0) + 1
            joint_key = "|".join(
                (
                    f"city={dimensions['city']}",
                    f"station={dimensions['station']}",
                    f"season={dimensions['season']}",
                    f"lead={dimensions['lead_bucket']}",
                    f"model={dimensions['provider_model']}",
                    f"metric={dimensions['metric']}",
                    f"unit={row.spec.unit}",
                )
            )
            joint_groups.setdefault(joint_key, []).append(row)

        for cohort_key, rows in sorted(joint_groups.items()):
            unit = rows[0].spec.unit
            if len(rows) < config.minimum_cohort_train_events:
                cohort_diagnostics.append(
                    CohortCalibrationDiagnostic(
                        cohort_key=cohort_key,
                        unit=unit,
                        n_train=len(rows),
                        status="insufficient_data",
                        bias=None,
                        standard_deviation=None,
                        log_likelihood=None,
                    )
                )
                continue
            diagnostic_fit = _fit_interval_calibration(
                rows,
                config=config,
                include_cohorts=False,
            )
            chosen = diagnostic_fit.by_unit[unit]
            cohort_diagnostics.append(
                CohortCalibrationDiagnostic(
                    cohort_key=cohort_key,
                    unit=unit,
                    n_train=len(rows),
                    status="diagnostic_fit",
                    bias=chosen.bias,
                    standard_deviation=chosen.standard_deviation,
                    log_likelihood=chosen.log_likelihood,
                )
            )

    return IntervalCalibration(
        by_unit=MappingProxyType(calibrations),
        training_event_ids=tuple(sorted(row.spec.event_id for row in train)),
        unbiased_normal_std_by_unit=MappingProxyType(
            unbiased_standard_deviations
        ),
        cohort_coverage=MappingProxyType(
            {
                dimension: MappingProxyType(dict(counts))
                for dimension, counts in cohort_coverage.items()
            }
        ),
        cohort_diagnostics=tuple(cohort_diagnostics),
    )


def _climatology_by_unit(
    train: Sequence[_PreparedEvent],
) -> Mapping[str, tuple[Decimal, ...]]:
    grouped: dict[str, list[int]] = {}
    for row in train:
        grouped.setdefault(row.spec.unit, []).append(row.winner_index)
    output: dict[str, tuple[Decimal, ...]] = {}
    for unit, indices in sorted(grouped.items()):
        counts = [ONE] * 11  # fixed Laplace smoothing, train-only
        for index in indices:
            counts[index] += ONE
        total = sum(counts, ZERO)
        output[unit] = tuple(value / total for value in counts)
    return MappingProxyType(output)


def _position_distribution(
    spec: WeatherEventSpec,
    probabilities: Sequence[Decimal],
) -> Mapping[str, Decimal]:
    if len(probabilities) != len(spec.bins):
        raise WeatherExperimentError("rank probability length does not match bins")
    return _normalize(
        {
            weather_bin.market_id: probability
            for weather_bin, probability in zip(spec.bins, probabilities)
        }
    )


def _split_name(event_id: str, splits: EventSplits) -> str:
    for name, values in splits.as_dict().items():
        if event_id in values:
            return name
    raise WeatherExperimentError(f"event {event_id} is absent from primary splits")


def _percentile(
    values: Sequence[Decimal],
    probability: Decimal,
) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise WeatherExperimentError("cannot percentile an empty sample")
    position = Decimal(len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _cluster_bootstrap_mean_ci(
    group_values: Sequence[Decimal],
    *,
    seed: int,
    resamples: int,
) -> tuple[Decimal, Decimal]:
    if not group_values:
        raise WeatherExperimentError("cluster bootstrap needs at least one group")
    count = len(group_values)
    denominator = Decimal(count)
    rng = random.Random(seed)
    means = [
        sum(
            (
                group_values[rng.randrange(count)]
                for _ in range(count)
            ),
            ZERO,
        )
        / denominator
        for _ in range(resamples)
    ]
    return (
        _percentile(means, Decimal("0.025")),
        _percentile(means, Decimal("0.975")),
    )


def _predictions(
    prepared: Sequence[_PreparedEvent],
    *,
    active_splits: EventSplits,
    calibration: IntervalCalibration,
    climatology: Mapping[str, tuple[Decimal, ...]],
) -> tuple[WeatherEventPrediction, ...]:
    result: list[WeatherEventPrediction] = []
    for row in sorted(prepared, key=lambda item: item.spec.event_id):
        run = row.evidence.forecast
        if run is None:
            raise WeatherExperimentError(
                f"event {row.spec.event_id} lost its prepared forecast"
            )
        unit = row.spec.unit
        if unit not in calibration.by_unit:
            raise WeatherExperimentError(
                f"unit {unit} has no train calibration"
            )
        model = calibration.by_unit[unit]
        if (
            unit not in climatology
            or unit not in calibration.unbiased_normal_std_by_unit
        ):
            raise WeatherExperimentError(f"unit {unit} has no train baseline")
        forecast_probabilities = _interval_gaussian_probabilities(
            row.spec,
            forecast=row.forecast_extreme,
            bias=model.bias,
            standard_deviation=model.standard_deviation,
        )
        result.append(
            WeatherEventPrediction(
                event_id=row.spec.event_id,
                city=row.spec.city,
                local_date=row.spec.local_date.isoformat(),
                unit=unit,
                split=_split_name(row.spec.event_id, active_splits),
                # The experiment derives its own conservative dependence key;
                # an injected provider cannot inflate validation counts.
                independence_group=f"date:{row.spec.local_date.isoformat()}",
                provider_independence_group=row.evidence.independence_group,
                decision_at=row.evidence.decision_at,
                winner_market_id=row.winner_market_id,
                winner_bin_label=row.winner_bin_label,
                observed_label_kind="resolved_winner_bin",
                forecast_extreme=row.forecast_extreme,
                forecast_probabilities=forecast_probabilities,
                market_probabilities=row.market_probabilities,
                climatology_probabilities=_position_distribution(
                    row.spec,
                    climatology[unit],
                ),
                normal_probabilities=_interval_gaussian_probabilities(
                    row.spec,
                    forecast=row.forecast_extreme,
                    bias=ZERO,
                    standard_deviation=(
                        calibration.unbiased_normal_std_by_unit[unit]
                    ),
                ),
                raw_market_prices=row.raw_market_prices,
                price_observed_at=row.price_observed_at,
                forecast_provider=run.provider,
                forecast_model=run.model,
                forecast_initialized_at=run.initialized_at,
                forecast_available_at=run.available_at,
                forecast_acquired_at=run.fetched_at,
                forecast_sha256=run.acquisition.sha256,
                price_history_sha256=row.price_history_sha256,
                station_code=row.evidence.station.icao,
                station_latitude=row.evidence.station.latitude,
                station_longitude=row.evidence.station.longitude,
                station_sha256=row.evidence.station.acquisition.sha256,
            )
        )
    return tuple(result)


def _model_metrics(
    rows: Sequence[WeatherEventPrediction],
    *,
    probability_field: str,
    reliability_bins: int,
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> ModelMetrics:
    if not rows:
        return ModelMetrics(
            n_events=0,
            n_independence_groups=0,
            mean_brier=None,
            mean_log_loss=None,
            nominal_event_mean_brier=None,
            nominal_event_mean_log_loss=None,
            brier_cluster_ci_lower=None,
            brier_cluster_ci_upper=None,
            log_loss_cluster_ci_lower=None,
            log_loss_cluster_ci_upper=None,
            bootstrap_resamples=bootstrap_resamples,
            bootstrap_seed=bootstrap_seed,
            reliability=(),
        )
    distributions = [
        getattr(row, probability_field)
        for row in rows
    ]
    observed = [row.winner_market_id for row in rows]
    def decimal_log_loss(
        distribution: Mapping[str, Decimal],
        actual: str,
    ) -> Decimal:
        if actual not in distribution:
            raise WeatherExperimentError(
                "winner is absent from a scored distribution"
            )
        probability = max(distribution[actual], LOG_EPSILON)
        return -probability.ln()

    brier_values = [
        brier_score(distribution, observed=actual)
        for distribution, actual in zip(distributions, observed)
    ]
    log_values = [
        decimal_log_loss(distribution, actual)
        for distribution, actual in zip(distributions, observed)
    ]
    groups: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(row.independence_group, []).append(index)

    def group_means(values: Sequence[Decimal]) -> list[Decimal]:
        return [
            sum((values[index] for index in indices), ZERO)
            / Decimal(len(indices))
            for indices in groups.values()
        ]

    count = Decimal(len(rows))
    brier_group_means = group_means(brier_values)
    log_group_means = group_means(log_values)
    brier_ci = _cluster_bootstrap_mean_ci(
        brier_group_means,
        seed=bootstrap_seed,
        resamples=bootstrap_resamples,
    )
    log_ci = _cluster_bootstrap_mean_ci(
        log_group_means,
        seed=bootstrap_seed + 1,
        resamples=bootstrap_resamples,
    )
    return ModelMetrics(
        n_events=len(rows),
        n_independence_groups=len(groups),
        mean_brier=(
            sum(brier_group_means, ZERO)
            / Decimal(len(brier_group_means))
        ),
        mean_log_loss=(
            sum(log_group_means, ZERO)
            / Decimal(len(log_group_means))
        ),
        nominal_event_mean_brier=sum(brier_values, ZERO) / count,
        nominal_event_mean_log_loss=sum(log_values, ZERO) / count,
        brier_cluster_ci_lower=brier_ci[0],
        brier_cluster_ci_upper=brier_ci[1],
        log_loss_cluster_ci_lower=log_ci[0],
        log_loss_cluster_ci_upper=log_ci[1],
        bootstrap_resamples=bootstrap_resamples,
        bootstrap_seed=bootstrap_seed,
        reliability=reliability_table(
            distributions,
            observed=observed,
            n_bins=reliability_bins,
        ),
    )


def _all_metrics(
    predictions: Sequence[WeatherEventPrediction],
    *,
    reliability_bins: int,
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> Mapping[str, Mapping[str, ModelMetrics]]:
    fields = {
        "forecast": "forecast_probabilities",
        "market": "market_probabilities",
        "climatology": "climatology_probabilities",
        "normal": "normal_probabilities",
    }
    output: dict[str, Mapping[str, ModelMetrics]] = {}
    for split_index, split in enumerate(("train", "validation", "test")):
        rows = [row for row in predictions if row.split == split]
        output[split] = MappingProxyType(
            {
                name: _model_metrics(
                    rows,
                    probability_field=field,
                    reliability_bins=reliability_bins,
                    bootstrap_seed=(
                        bootstrap_seed
                        + split_index * 100
                        + model_index * 10
                    ),
                    bootstrap_resamples=bootstrap_resamples,
                )
                for model_index, (name, field) in enumerate(fields.items())
            }
        )
    return MappingProxyType(output)


def _taker_fee_per_share(
    price: Decimal,
    *,
    config: WeatherExperimentConfig,
) -> Decimal:
    """Official price-dependent fee plus an optional fixed stress buffer."""

    return config.taker_fee + official_taker_fee(
        ONE,
        price,
        FeeSchedule(
            rate=config.taker_fee_rate,
            exponent=config.taker_fee_exponent,
        ),
    )


def _best_outcome(
    row: WeatherEventPrediction,
    *,
    execution_style: str,
    config: WeatherExperimentConfig,
) -> tuple[str, Decimal]:
    def net_edge(market_id: str) -> Decimal:
        fee = (
            _taker_fee_per_share(
                row.raw_market_prices[market_id],
                config=config,
            )
            if execution_style == "taker_shadow"
            else config.maker_fee
        )
        slippage = config.slippage if execution_style == "taker_shadow" else ZERO
        return (
            row.forecast_probabilities[market_id]
            - row.raw_market_prices[market_id]
            - fee
            - slippage
            - config.model_margin
        )

    market_id = min(
        row.forecast_probabilities,
        key=lambda candidate: (-net_edge(candidate), candidate),
    )
    return market_id, net_edge(market_id)


def _threshold_evaluation(
    rows: Sequence[WeatherEventPrediction],
    *,
    threshold: Decimal,
    config: WeatherExperimentConfig,
    bootstrap_seed: int,
) -> ThresholdEvaluation:
    if not rows:
        return ThresholdEvaluation(
            threshold=threshold,
            validation_mean_l1_shadow_profit=ZERO,
            diagnostic_counterfactual_events=0,
            diagnostic_counterfactual_groups=0,
            l1_shadow_cluster_ci_lower=ZERO,
            l1_shadow_cluster_ci_upper=ZERO,
            bootstrap_seed=bootstrap_seed,
        )
    group_profits: dict[str, list[Decimal]] = {}
    candidate_groups: set[str] = set()
    candidate_count = 0
    for row in rows:
        market_id, net_edge = _best_outcome(
            row,
            execution_style="taker_shadow",
            config=config,
        )
        shadow_value = ZERO
        if net_edge >= threshold:
            candidate_count += 1
            candidate_groups.add(row.independence_group)
            payoff = ONE if market_id == row.winner_market_id else ZERO
            shadow_value = (
                payoff
                - row.raw_market_prices[market_id]
                - _taker_fee_per_share(
                    row.raw_market_prices[market_id],
                    config=config,
                )
                - config.slippage
                - config.model_margin
            )
        group_profits.setdefault(row.independence_group, []).append(
            shadow_value
        )
    group_means = [
        sum(values, ZERO) / Decimal(len(values))
        for values in group_profits.values()
    ]
    shadow_ci = _cluster_bootstrap_mean_ci(
        group_means,
        seed=bootstrap_seed,
        resamples=config.bootstrap_resamples,
    )
    return ThresholdEvaluation(
        threshold=threshold,
        validation_mean_l1_shadow_profit=(
            sum(group_means, ZERO) / Decimal(len(group_means))
        ),
        diagnostic_counterfactual_events=candidate_count,
        diagnostic_counterfactual_groups=len(candidate_groups),
        l1_shadow_cluster_ci_lower=shadow_ci[0],
        l1_shadow_cluster_ci_upper=shadow_ci[1],
        bootstrap_seed=bootstrap_seed,
    )


def _select_threshold(
    validation: Sequence[WeatherEventPrediction],
    *,
    config: WeatherExperimentConfig,
) -> tuple[Decimal, tuple[ThresholdEvaluation, ...], bool]:
    # Ties deliberately choose the larger gate.
    evaluations = tuple(
        _threshold_evaluation(
            validation,
            threshold=threshold,
            config=config,
            bootstrap_seed=config.random_seed + 1000 + index,
        )
        for index, threshold in enumerate(config.edge_threshold_candidates)
    )
    selected = max(
        evaluations,
        key=lambda evaluation: (
            evaluation.validation_mean_l1_shadow_profit,
            evaluation.threshold,
        ),
    )
    selected_index = evaluations.index(selected)
    neighbors = tuple(
        evaluations[index]
        for index in (selected_index - 1, selected_index + 1)
        if 0 <= index < len(evaluations)
    )
    neighbor_stable = (
        selected.validation_mean_l1_shadow_profit > ZERO
        and selected.diagnostic_counterfactual_events > 0
        and selected.diagnostic_counterfactual_groups > 0
        and bool(neighbors)
        and all(
            neighbor.validation_mean_l1_shadow_profit > ZERO
            and neighbor.diagnostic_counterfactual_events > 0
            and neighbor.diagnostic_counterfactual_groups > 0
            for neighbor in neighbors
        )
    )
    return selected.threshold, evaluations, neighbor_stable


def _policy_gate(
    evaluations: Sequence[ThresholdEvaluation],
    *,
    selected_threshold: Decimal,
    neighbor_stable: bool,
) -> tuple[bool, tuple[str, ...]]:
    selected = next(
        evaluation
        for evaluation in evaluations
        if evaluation.threshold == selected_threshold
    )
    reasons: list[str] = []
    if selected.validation_mean_l1_shadow_profit <= ZERO:
        reasons.append("no_positive_validation_threshold_objective")
    if not neighbor_stable:
        reasons.append("validation_threshold_not_neighbor_stable")
    return not reasons, tuple(reasons)


def _trade_rows(
    predictions: Sequence[WeatherEventPrediction],
    *,
    selected_threshold: Decimal,
    policy_approved: bool,
    config: WeatherExperimentConfig,
) -> tuple[WeatherTradeRow, ...]:
    result: list[WeatherTradeRow] = []
    for row in predictions:
        if row.split not in {"validation", "test"}:
            continue
        for style in ("taker_shadow", "maker_shadow"):
            market_id, net_edge = _best_outcome(
                row,
                execution_style=style,
                config=config,
            )
            diagnostic_candidate = net_edge >= selected_threshold
            diagnostic_action = (
                "diagnostic_counterfactual"
                if diagnostic_candidate
                else "diagnostic_skip"
            )
            action = (
                "policy_candidate" if diagnostic_candidate else "skip"
            ) if policy_approved else "no_trade"
            payoff = ONE if market_id == row.winner_market_id else ZERO
            taker_fee = (
                _taker_fee_per_share(
                    row.raw_market_prices[market_id],
                    config=config,
                )
                if style == "taker_shadow"
                else ZERO
            )
            maker_fee = config.maker_fee if style == "maker_shadow" else ZERO
            slippage = config.slippage if style == "taker_shadow" else ZERO
            shadow_profit = (
                payoff
                - row.raw_market_prices[market_id]
                - taker_fee
                - maker_fee
                - slippage
                - config.model_margin
                if diagnostic_candidate and style == "taker_shadow"
                else None
            )
            result.append(
                WeatherTradeRow(
                    event_id=row.event_id,
                    independence_group=row.independence_group,
                    split=row.split,
                    execution_style=style,
                    action=action,
                    diagnostic_action=diagnostic_action,
                    policy_approved=policy_approved,
                    diagnostic_only=not policy_approved,
                    market_id=market_id,
                    winner_market_id=row.winner_market_id,
                    fair_probability=row.forecast_probabilities[market_id],
                    market_price=row.raw_market_prices[market_id],
                    gross_edge=(
                        row.forecast_probabilities[market_id]
                        - row.raw_market_prices[market_id]
                    ),
                    taker_fee=taker_fee,
                    maker_fee=maker_fee,
                    slippage=slippage,
                    model_margin=config.model_margin,
                    net_edge=net_edge,
                    edge_threshold=selected_threshold,
                    threshold_origin=(
                        "validation_policy_approved"
                        if policy_approved
                        else "validation_diagnostic_best"
                    ),
                    reward=ZERO,
                    resolved_payoff=payoff,
                    shadow_profit=shadow_profit,
                )
            )
    return tuple(
        sorted(
            result,
            key=lambda item: (item.event_id, item.execution_style),
        )
    )


def _run_robustness_view(
    prepared: Sequence[_PreparedEvent],
    *,
    name: str,
    event_splits: EventSplits,
    config: WeatherExperimentConfig,
    include_cohort_diagnostics: bool,
) -> WeatherRobustnessView:
    if any(
        not event_ids
        for event_ids in event_splits.as_dict().values()
    ):
        raise WeatherExperimentError(
            f"{name} requires non-empty train, validation, and test splits"
        )
    prepared_by_id = {row.spec.event_id: row for row in prepared}
    train = [prepared_by_id[event_id] for event_id in event_splits.train]
    calibration = _fit_interval_calibration(
        train,
        config=config,
        include_cohorts=include_cohort_diagnostics,
    )
    predictions = _predictions(
        prepared,
        active_splits=event_splits,
        calibration=calibration,
        climatology=_climatology_by_unit(train),
    )
    validation = [row for row in predictions if row.split == "validation"]
    (
        selected_threshold,
        threshold_evaluations,
        threshold_neighbor_stable,
    ) = _select_threshold(validation, config=config)
    policy_approved, policy_reason_codes = _policy_gate(
        threshold_evaluations,
        selected_threshold=selected_threshold,
        neighbor_stable=threshold_neighbor_stable,
    )
    return WeatherRobustnessView(
        name=name,
        splits=event_splits,
        calibration=calibration,
        selected_threshold=selected_threshold,
        threshold_evaluations=threshold_evaluations,
        threshold_neighbor_stable=threshold_neighbor_stable,
        policy_approved=policy_approved,
        policy_reason_codes=policy_reason_codes,
        threshold_selection_event_ids=tuple(
            sorted(row.event_id for row in validation)
        ),
        metrics=_all_metrics(
            predictions,
            reliability_bins=config.reliability_bins,
            bootstrap_seed=config.random_seed,
            bootstrap_resamples=config.bootstrap_resamples,
        ),
        event_predictions=predictions,
        trade_rows=_trade_rows(
            predictions,
            selected_threshold=selected_threshold,
            policy_approved=policy_approved,
            config=config,
        ),
    )


def _run_weather_experiment_with_fixed_context(
    raw_events: Iterable[Mapping[str, Any]],
    *,
    evidence_provider: WeatherEvidenceProvider,
    config: WeatherExperimentConfig,
) -> WeatherExperimentResult:
    """Run a fully offline, train/validation/test weather experiment.

    Public price history is always classified as L1.  The resulting taker and
    maker rows are counterfactual shadow decisions, never fills or realized
    profit.
    """

    if not hasattr(evidence_provider, "evidence_for"):
        raise TypeError("evidence_provider must implement evidence_for")
    prepared, exclusions, split_universe = _prepare(
        raw_events,
        provider=evidence_provider,
        config=config,
    )
    if not prepared:
        raise NoEligibleWeatherEventsError(exclusions)
    primary_universe = chronological_weather_split(
        split_universe,
        train_fraction=config.train_fraction,
        validation_fraction=config.validation_fraction,
    )
    city_universe = city_weather_split(
        split_universe,
        train_fraction=config.train_fraction,
        validation_fraction=config.validation_fraction,
    )
    eligible_ids = {row.spec.event_id for row in prepared}

    def eligible_within(frozen: EventSplits) -> EventSplits:
        return EventSplits(
            train=tuple(
                event_id
                for event_id in frozen.train
                if event_id in eligible_ids
            ),
            validation=tuple(
                event_id
                for event_id in frozen.validation
                if event_id in eligible_ids
            ),
            test=tuple(
                event_id
                for event_id in frozen.test
                if event_id in eligible_ids
            ),
        )

    primary = eligible_within(primary_universe)
    city = eligible_within(city_universe)
    splits = WeatherExperimentSplits(
        primary_date=primary,
        city_holdout=city,
        primary_date_universe=primary_universe,
        city_holdout_universe=city_universe,
    )
    primary_view = _run_robustness_view(
        prepared,
        name="chronological_local_date",
        event_splits=primary,
        config=config,
        include_cohort_diagnostics=True,
    )
    city_view = _run_robustness_view(
        prepared,
        name="whole_city_holdout",
        event_splits=city,
        config=config,
        include_cohort_diagnostics=False,
    )
    calibration = primary_view.calibration
    predictions = primary_view.event_predictions
    validation = [row for row in predictions if row.split == "validation"]
    selected_threshold = primary_view.selected_threshold
    trades = primary_view.trade_rows

    # Validation gates are OOS gates: training and threshold-selection rows do
    # not inflate either independent-event or fill counts.
    independent_groups = {
        row.independence_group
        for row in predictions
        if row.split == "test"
    }
    explainable_fills = sum(
        1
        for row in trades
        if row.split == "test" and row.explainable_fill
    )
    gate_reasons: list[str] = []
    if len(independent_groups) < config.minimum_independent_events:
        gate_reasons.append(
            f"fewer_than_{config.minimum_independent_events}_independent_settled_events"
        )
    if explainable_fills < config.minimum_explainable_fills:
        gate_reasons.append(
            f"fewer_than_{config.minimum_explainable_fills}_explainable_fills"
        )
    if not primary_view.policy_approved:
        gate_reasons.append("validation_policy_fail_closed_abstain")
    # L1 can never satisfy a claim of real execution even if count gates are
    # configured to zero.
    gate_reasons.append("public_l1_has_no_execution_or_queue_evidence")

    return WeatherExperimentResult(
        config=config,
        splits=splits,
        calibration=calibration,
        selected_threshold=selected_threshold,
        threshold_evaluations=primary_view.threshold_evaluations,
        threshold_neighbor_stable=(
            primary_view.threshold_neighbor_stable
        ),
        policy_approved=primary_view.policy_approved,
        policy_reason_codes=primary_view.policy_reason_codes,
        threshold_selected_on="validation",
        threshold_selection_event_ids=tuple(
            sorted(row.event_id for row in validation)
        ),
        metrics=primary_view.metrics,
        event_predictions=predictions,
        trade_rows=trades,
        city_holdout_view=city_view,
        exclusions=exclusions,
        classification="insufficient_data",
        gate_reasons=tuple(gate_reasons),
        independent_settled_events=len(independent_groups),
        explainable_fills=explainable_fills,
    )


def run_weather_experiment(
    raw_events: Iterable[Mapping[str, Any]],
    *,
    evidence_provider: WeatherEvidenceProvider,
    config: WeatherExperimentConfig,
) -> WeatherExperimentResult:
    """Run the experiment under a fixed Decimal arithmetic context."""

    with localcontext() as context:
        context.prec = DECIMAL_WORKING_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return _run_weather_experiment_with_fixed_context(
            raw_events,
            evidence_provider=evidence_provider,
            config=config,
        )


__all__ = [
    "ExperimentExclusion",
    "IntervalCalibration",
    "MappingWeatherEvidenceProvider",
    "ModelMetrics",
    "NoEligibleWeatherEventsError",
    "UnitIntervalCalibration",
    "WeatherEventEvidence",
    "WeatherEventPrediction",
    "WeatherEvidenceProvider",
    "WeatherExperimentConfig",
    "WeatherExperimentError",
    "WeatherExperimentResult",
    "WeatherExperimentSplits",
    "WeatherTradeRow",
    "run_weather_experiment",
]
