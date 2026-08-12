"""Leakage-resistant weather-market parsing and probability primitives.

The module is deliberately independent of credentials, network clients, and
order submission.  It turns immutable Gamma payloads and forecast vintages
into auditable event specifications that an experiment runner can consume.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .experiments import EventSplits, chronological_event_split, city_event_split


ZERO = Decimal("0")
ONE = Decimal("1")
HALF = Decimal("0.5")


class WeatherDataError(ValueError):
    """Raised when public weather data cannot be interpreted safely."""


class FutureDataError(WeatherDataError):
    """Raised when a backtest attempts to use a future forecast vintage."""


def _json_array(value: Any, *, field: str) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WeatherDataError(f"{field} is not valid JSON") from exc
    if not isinstance(value, list):
        raise WeatherDataError(f"{field} must be an array or JSON array string")
    return value


@dataclass(frozen=True)
class WeatherBin:
    """One exhaustive temperature outcome represented on a continuous scale."""

    market_id: str
    condition_id: str
    label: str
    yes_token_id: str
    no_token_id: str
    lower_bound: Optional[Decimal]
    upper_bound: Optional[Decimal]
    yes_outcome_price: Optional[Decimal] = None
    no_outcome_price: Optional[Decimal] = None

    @property
    def tail_censored(self) -> bool:
        return self.lower_bound is None or self.upper_bound is None

    def contains(self, value: Decimal) -> bool:
        value = Decimal(str(value))
        return (
            (self.lower_bound is None or value >= self.lower_bound)
            and (self.upper_bound is None or value < self.upper_bound)
        )


@dataclass(frozen=True)
class WeatherEventSpec:
    """Canonical rules and mutually-exclusive bins for one Gamma event."""

    event_id: str
    slug: str
    city: str
    station_name: str
    station_code: str
    local_date: date
    timezone: str
    metric: str
    unit: str
    reporting_semantics: str
    resolution_source: str
    rules: str
    rules_hash: str
    revision_cutoff: str
    bins: tuple[WeatherBin, ...]
    quarantine_reasons: tuple[str, ...] = ()

    @property
    def quarantined(self) -> bool:
        return bool(self.quarantine_reasons)

    def map_observation(self, value: Decimal) -> WeatherBin:
        matches = [weather_bin for weather_bin in self.bins if weather_bin.contains(value)]
        if len(matches) != 1:
            raise WeatherDataError(
                f"observation {value} maps to {len(matches)} bins, expected exactly one"
            )
        return matches[0]


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WeatherDataError(f"{name} must be a timezone-aware datetime")
    if value.utcoffset() is None:
        raise WeatherDataError(f"{name} must have a valid UTC offset")


@dataclass(frozen=True)
class ForecastVintage:
    """One immutable model run with its actual data-availability provenance."""

    provider: str
    model: str
    run_initialized_at: datetime
    available_at: datetime
    captured_at: datetime

    def __post_init__(self) -> None:
        if not self.provider or not self.model:
            raise WeatherDataError("forecast provider and model must be non-empty")
        for name in ("run_initialized_at", "available_at", "captured_at"):
            _require_aware(name, getattr(self, name))
        if self.available_at < self.run_initialized_at:
            raise WeatherDataError("forecast cannot be available before initialization")
        if self.captured_at < self.available_at:
            raise WeatherDataError("forecast cannot be captured before it is available")

    @property
    def known_at(self) -> datetime:
        """Earliest decision time allowed by availability and local capture."""

        return max(self.available_at, self.captured_at)

    def is_usable_at(self, decision_at: datetime) -> bool:
        _require_aware("decision_at", decision_at)
        return self.known_at <= decision_at

    def require_usable_at(self, decision_at: datetime) -> "ForecastVintage":
        if not self.is_usable_at(decision_at):
            raise FutureDataError(
                "forecast was not available and captured at the decision time"
            )
        return self


def select_latest_vintage(
    vintages: Sequence[ForecastVintage],
    *,
    decision_at: datetime,
    provider: str | None = None,
    model: str | None = None,
) -> ForecastVintage:
    """Return the latest run actually known at ``decision_at``."""

    _require_aware("decision_at", decision_at)
    usable = [
        vintage
        for vintage in vintages
        if vintage.is_usable_at(decision_at)
        and (provider is None or vintage.provider == provider)
        and (model is None or vintage.model == model)
    ]
    if not usable:
        raise FutureDataError("no forecast vintage was available at decision time")
    return max(
        usable,
        key=lambda item: (
            item.run_initialized_at,
            item.available_at,
            item.captured_at,
        ),
    )


@dataclass(frozen=True)
class HourlyTemperature:
    """One timezone-aware hourly forecast or observation."""

    valid_at: datetime
    temperature: Decimal

    def __post_init__(self) -> None:
        _require_aware("valid_at", self.valid_at)
        value = Decimal(str(self.temperature))
        if not value.is_finite():
            raise WeatherDataError("hourly temperature must be finite")
        object.__setattr__(self, "temperature", value)


def local_day_extreme(
    points: Iterable[HourlyTemperature],
    *,
    local_date: date,
    timezone_name: str,
    metric: str,
    minimum_points: int = 18,
) -> Decimal:
    """Aggregate hourly values over the named city's actual local day."""

    if metric not in {"high", "low"}:
        raise WeatherDataError("weather metric must be 'high' or 'low'")
    if (
        not isinstance(minimum_points, int)
        or isinstance(minimum_points, bool)
        or minimum_points <= 0
    ):
        raise WeatherDataError("minimum_points must be a positive integer")
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise WeatherDataError(f"unknown IANA timezone: {timezone_name}") from exc
    values = [
        point.temperature
        for point in points
        if point.valid_at.astimezone(local_zone).date() == local_date
    ]
    if len(values) < minimum_points:
        raise WeatherDataError(
            "insufficient local-day coverage: "
            f"got {len(values)} points, require {minimum_points}"
        )
    return max(values) if metric == "high" else min(values)


@dataclass(frozen=True)
class CalibrationRow:
    """A forecast/observation pair explicitly assigned to one data split."""

    forecast: Decimal
    observed: Decimal
    split: str

    def __post_init__(self) -> None:
        forecast = Decimal(str(self.forecast))
        observed = Decimal(str(self.observed))
        if not forecast.is_finite() or not observed.is_finite():
            raise WeatherDataError("calibration temperatures must be finite")
        if not self.split:
            raise WeatherDataError("calibration split must be non-empty")
        object.__setattr__(self, "forecast", forecast)
        object.__setattr__(self, "observed", observed)


@dataclass(frozen=True)
class GaussianResidualCalibration:
    """Train-only additive bias and sample residual standard deviation."""

    bias: Decimal
    standard_deviation: Decimal
    n_train: int

    def __post_init__(self) -> None:
        bias = Decimal(str(self.bias))
        standard_deviation = Decimal(str(self.standard_deviation))
        if not bias.is_finite():
            raise WeatherDataError("calibration bias must be finite")
        if (
            not standard_deviation.is_finite()
            or standard_deviation <= ZERO
        ):
            raise WeatherDataError(
                "calibration standard_deviation must be positive and finite"
            )
        if (
            not isinstance(self.n_train, int)
            or isinstance(self.n_train, bool)
            or self.n_train < 2
        ):
            raise WeatherDataError("calibration n_train must be at least two")
        object.__setattr__(self, "bias", bias)
        object.__setattr__(self, "standard_deviation", standard_deviation)

    @classmethod
    def fit(
        cls,
        rows: Sequence[CalibrationRow],
    ) -> "GaussianResidualCalibration":
        if len(rows) < 2:
            raise WeatherDataError("at least two train rows are required")
        if any(row.split != "train" for row in rows):
            raise WeatherDataError(
                "residual calibration accepts train rows only"
            )
        residuals = tuple(row.observed - row.forecast for row in rows)
        count = len(residuals)
        bias = sum(residuals, ZERO) / Decimal(count)
        variance = sum(
            ((residual - bias) ** 2 for residual in residuals),
            ZERO,
        ) / Decimal(count - 1)
        standard_deviation = variance.sqrt()
        if standard_deviation <= ZERO:
            raise WeatherDataError(
                "train residual standard deviation must be positive"
            )
        return cls(
            bias=bias,
            standard_deviation=standard_deviation,
            n_train=count,
        )


def _normal_cdf(value: Decimal, *, mean: Decimal, std: Decimal) -> float:
    z_score = float((value - mean) / std)
    return 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))


def gaussian_bin_probabilities(
    spec: WeatherEventSpec,
    *,
    forecast: Decimal,
    calibration: GaussianResidualCalibration,
    model_uncertainty: Decimal = ZERO,
) -> dict[str, Decimal]:
    """Map a calibrated Gaussian temperature forecast to event-bin fair values."""

    if spec.quarantined:
        raise WeatherDataError("cannot price a quarantined weather event")
    forecast_value = Decimal(str(forecast))
    uncertainty = Decimal(str(model_uncertainty))
    if not forecast_value.is_finite():
        raise WeatherDataError("forecast must be finite")
    if not uncertainty.is_finite() or uncertainty < ZERO:
        raise WeatherDataError("model_uncertainty must be finite and non-negative")
    residual_std = calibration.standard_deviation
    combined_std = (residual_std * residual_std + uncertainty * uncertainty).sqrt()
    if combined_std <= ZERO:
        raise WeatherDataError("combined forecast uncertainty must be positive")
    mean = forecast_value + calibration.bias

    raw: list[float] = []
    for weather_bin in spec.bins:
        lower_cdf = (
            0.0
            if weather_bin.lower_bound is None
            else _normal_cdf(weather_bin.lower_bound, mean=mean, std=combined_std)
        )
        upper_cdf = (
            1.0
            if weather_bin.upper_bound is None
            else _normal_cdf(weather_bin.upper_bound, mean=mean, std=combined_std)
        )
        raw.append(max(0.0, upper_cdf - lower_cdf))
    total = sum(raw)
    if not math.isfinite(total) or total <= 0.0:
        raise WeatherDataError("Gaussian bin probabilities are not normalizable")

    quantum = Decimal("0.000000000000000001")
    normalized = [
        (Decimal(str(value / total))).quantize(
            quantum,
            rounding=ROUND_HALF_EVEN,
        )
        for value in raw
    ]
    correction_index = max(range(len(normalized)), key=normalized.__getitem__)
    others = sum(
        (
            probability
            for index, probability in enumerate(normalized)
            if index != correction_index
        ),
        ZERO,
    )
    normalized[correction_index] = ONE - others
    if any(probability < ZERO for probability in normalized):
        raise WeatherDataError("Gaussian normalization produced a negative probability")
    return {
        weather_bin.market_id: probability
        for weather_bin, probability in zip(spec.bins, normalized)
    }


def _validated_probabilities(
    probabilities: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    if not probabilities:
        raise WeatherDataError("probability distribution must be non-empty")
    normalized: dict[str, Decimal] = {}
    for outcome, raw_probability in probabilities.items():
        probability = Decimal(str(raw_probability))
        if not probability.is_finite() or not ZERO <= probability <= ONE:
            raise WeatherDataError("probabilities must be finite and between zero and one")
        canonical_outcome = str(outcome)
        if canonical_outcome in normalized:
            raise WeatherDataError("outcome identifiers must be unique as strings")
        normalized[canonical_outcome] = probability
    total = sum(normalized.values(), ZERO)
    if abs(total - ONE) > Decimal("0.000000000001"):
        raise WeatherDataError("probabilities must sum to one")
    return normalized


def brier_score(
    probabilities: Mapping[str, Decimal],
    *,
    observed: str,
) -> Decimal:
    """Return the multiclass Brier score for one mutually-exclusive event."""

    values = _validated_probabilities(probabilities)
    if observed not in values:
        raise WeatherDataError("observed outcome is absent from the distribution")
    return sum(
        (
            probability - (ONE if outcome == observed else ZERO)
        )
        ** 2
        for outcome, probability in values.items()
    )


multiclass_brier_score = brier_score


def log_loss(
    probabilities: Mapping[str, Decimal],
    *,
    observed: str,
    epsilon: Decimal = Decimal("0.000000000000001"),
) -> Decimal:
    """Return clipped multiclass logarithmic loss for one event."""

    values = _validated_probabilities(probabilities)
    if observed not in values:
        raise WeatherDataError("observed outcome is absent from the distribution")
    floor = Decimal(str(epsilon))
    if not floor.is_finite() or not ZERO < floor < ONE:
        raise WeatherDataError("epsilon must be strictly between zero and one")
    probability = max(values[observed], floor)
    return Decimal(str(-math.log(float(probability))))


multiclass_log_loss = log_loss


@dataclass(frozen=True)
class ReliabilityBucket:
    """One non-empty one-vs-all calibration bucket."""

    lower_bound: Decimal
    upper_bound: Decimal
    count: int
    mean_probability: Decimal
    empirical_frequency: Decimal


def reliability_table(
    predictions: Sequence[Mapping[str, Decimal]],
    *,
    observed: Sequence[str],
    n_bins: int = 10,
) -> tuple[ReliabilityBucket, ...]:
    """Build one-vs-all reliability buckets without treating outcomes as events."""

    if len(predictions) != len(observed) or not predictions:
        raise WeatherDataError(
            "predictions and observed outcomes must have equal non-zero length"
        )
    if not isinstance(n_bins, int) or isinstance(n_bins, bool) or n_bins <= 0:
        raise WeatherDataError("n_bins must be a positive integer")
    buckets: list[list[tuple[Decimal, Decimal]]] = [
        [] for _ in range(n_bins)
    ]
    for raw_distribution, actual in zip(predictions, observed):
        distribution = _validated_probabilities(raw_distribution)
        if actual not in distribution:
            raise WeatherDataError("observed outcome is absent from a prediction")
        for outcome, probability in distribution.items():
            bucket_index = min(int(probability * n_bins), n_bins - 1)
            buckets[bucket_index].append(
                (probability, ONE if outcome == actual else ZERO)
            )

    result: list[ReliabilityBucket] = []
    denominator = Decimal(n_bins)
    for index, rows in enumerate(buckets):
        if not rows:
            continue
        count = len(rows)
        result.append(
            ReliabilityBucket(
                lower_bound=Decimal(index) / denominator,
                upper_bound=Decimal(index + 1) / denominator,
                count=count,
                mean_probability=sum(
                    (probability for probability, _ in rows),
                    ZERO,
                )
                / Decimal(count),
                empirical_frequency=sum(
                    (outcome for _, outcome in rows),
                    ZERO,
                )
                / Decimal(count),
            )
        )
    return tuple(result)


def _weather_split_rows(
    specs: Iterable[WeatherEventSpec],
) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for spec in specs:
        if spec.quarantined:
            raise WeatherDataError(
                f"quarantined event {spec.event_id} cannot enter experiment splits"
            )
        rows.append(
            {
                "event_id": spec.event_id,
                "city": spec.city,
                # Local event date is the leakage-resistant ordering key.  The
                # exchange's UTC endDate is not a local observation boundary.
                "settled_at": spec.local_date.isoformat(),
            }
        )
    if not rows:
        raise WeatherDataError("at least one weather event is required")
    return tuple(rows)


def chronological_weather_split(
    specs: Iterable[WeatherEventSpec],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
) -> EventSplits:
    """Split whole local dates so correlated city events cannot cross folds."""

    rows = _weather_split_rows(specs)
    events_by_date: dict[str, list[str]] = {}
    for row in rows:
        events_by_date.setdefault(row["settled_at"], []).append(row["event_id"])
    date_splits = chronological_event_split(
        (
            {"event_id": event_date, "settled_at": event_date}
            for event_date in events_by_date
        ),
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )

    def event_ids(split_dates: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            event_id
            for event_date in split_dates
            for event_id in sorted(events_by_date[event_date])
        )

    return EventSplits(
        train=event_ids(date_splits.train),
        validation=event_ids(date_splits.validation),
        test=event_ids(date_splits.test),
    )


def city_weather_split(
    specs: Iterable[WeatherEventSpec],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
) -> EventSplits:
    """Bridge weather specs to the experiment registry's whole-city holdout."""

    return city_event_split(
        _weather_split_rows(specs),
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )


@dataclass(frozen=True)
class WeatherQuotePolicy:
    """Conservative probability-edge gates for one weather outcome quote."""

    minimum_net_edge: Decimal
    transaction_cost: Decimal
    model_uncertainty: Decimal
    reprice_probability_delta: Decimal
    cancel_before_settlement: timedelta

    def __post_init__(self) -> None:
        for name in (
            "minimum_net_edge",
            "transaction_cost",
            "model_uncertainty",
            "reprice_probability_delta",
        ):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value < ZERO:
                raise WeatherDataError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if not isinstance(self.cancel_before_settlement, timedelta):
            raise WeatherDataError("cancel_before_settlement must be a timedelta")
        if self.cancel_before_settlement < timedelta(0):
            raise WeatherDataError("cancel_before_settlement cannot be negative")


@dataclass(frozen=True)
class WeatherQuoteDecision:
    action: str
    reason: str
    fair_probability: Decimal
    market_price: Decimal
    net_edge: Decimal
    reward_assumption: Decimal = ZERO


def weather_quote_decision(
    *,
    fair_probability: Decimal,
    market_price: Decimal,
    policy: WeatherQuotePolicy,
    now: datetime,
    settlement_at: datetime,
    existing_quote: bool = False,
    previous_fair_probability: Decimal | None = None,
    reward_assumption: Decimal = ZERO,
) -> WeatherQuoteDecision:
    """Quote selectively and fail closed on updates or settlement proximity."""

    fair = Decimal(str(fair_probability))
    market = Decimal(str(market_price))
    reward = Decimal(str(reward_assumption))
    if (
        not fair.is_finite()
        or not market.is_finite()
        or not ZERO <= fair <= ONE
        or not ZERO <= market <= ONE
    ):
        raise WeatherDataError("fair_probability and market_price must be in [0, 1]")
    if reward != ZERO:
        raise WeatherDataError("weather quote reward assumption must remain zero")
    _require_aware("now", now)
    _require_aware("settlement_at", settlement_at)

    net_edge = (
        fair
        - market
        - policy.transaction_cost
        - policy.model_uncertainty
    )

    def decision(action: str, reason: str) -> WeatherQuoteDecision:
        return WeatherQuoteDecision(
            action=action,
            reason=reason,
            fair_probability=fair,
            market_price=market,
            net_edge=net_edge,
            reward_assumption=ZERO,
        )

    if settlement_at - now <= policy.cancel_before_settlement:
        return decision("cancel" if existing_quote else "skip", "near_settlement")

    if existing_quote and previous_fair_probability is None:
        return decision("cancel", "previous_fair_unknown")

    if existing_quote and previous_fair_probability is not None:
        previous = Decimal(str(previous_fair_probability))
        if not previous.is_finite() or not ZERO <= previous <= ONE:
            raise WeatherDataError("previous_fair_probability must be in [0, 1]")
        if abs(fair - previous) >= policy.reprice_probability_delta:
            return decision("cancel", "forecast_update")

    if net_edge < policy.minimum_net_edge:
        return decision(
            "cancel" if existing_quote else "skip",
            "edge_below_threshold",
        )
    if existing_quote:
        return decision("keep", "edge_still_valid")
    return decision("quote", "edge_above_threshold")


_CITY_TIMEZONES = {
    "Atlanta": "America/New_York",
    "Austin": "America/Chicago",
    "Auckland": "Pacific/Auckland",
    "Amsterdam": "Europe/Amsterdam",
    "Bangkok": "Asia/Bangkok",
    "Beijing": "Asia/Shanghai",
    "Berlin": "Europe/Berlin",
    "Boston": "America/New_York",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Cairo": "Africa/Cairo",
    "Chicago": "America/Chicago",
    "Dallas": "America/Chicago",
    "Delhi": "Asia/Kolkata",
    "Denver": "America/Denver",
    "Detroit": "America/Detroit",
    "Dubai": "Asia/Dubai",
    "Houston": "America/Chicago",
    "Jakarta": "Asia/Jakarta",
    "Jinan": "Asia/Shanghai",
    "Lagos": "Africa/Lagos",
    "Las Vegas": "America/Los_Angeles",
    "Los Angeles": "America/Los_Angeles",
    "Madrid": "Europe/Madrid",
    "Melbourne": "Australia/Melbourne",
    "Mexico City": "America/Mexico_City",
    "Miami": "America/New_York",
    "Minneapolis": "America/Chicago",
    "Moscow": "Europe/Moscow",
    "Mumbai": "Asia/Kolkata",
    "New Delhi": "Asia/Kolkata",
    "New Orleans": "America/Chicago",
    "NYC": "America/New_York",
    "New York": "America/New_York",
    "New York City": "America/New_York",
    "Paris": "Europe/Paris",
    "Panama City": "America/Panama",
    "Philadelphia": "America/New_York",
    "Phoenix": "America/Phoenix",
    "Portland": "America/Los_Angeles",
    "Riyadh": "Asia/Riyadh",
    "Rome": "Europe/Rome",
    "San Diego": "America/Los_Angeles",
    "San Francisco": "America/Los_Angeles",
    "Sao Paulo": "America/Sao_Paulo",
    "São Paulo": "America/Sao_Paulo",
    "Seattle": "America/Los_Angeles",
    "Seoul": "Asia/Seoul",
    "Shanghai": "Asia/Shanghai",
    "Singapore": "Asia/Singapore",
    "Sydney": "Australia/Sydney",
    "Taipei": "Asia/Taipei",
    "Tokyo": "Asia/Tokyo",
    "Toronto": "America/Toronto",
    "Vienna": "Europe/Vienna",
    "Warsaw": "Europe/Warsaw",
    "Washington DC": "America/New_York",
    "Washington, D.C.": "America/New_York",
    "Zhengzhou": "Asia/Shanghai",
    "Hong Kong": "Asia/Hong_Kong",
    "Karachi": "Asia/Karachi",
    "London": "Europe/London",
    "Istanbul": "Europe/Istanbul",
    "Tel Aviv": "Asia/Jerusalem",
}

_STATION_NAME_CODES = {
    "masroor airbase": "OPMR",
}


def _temperature_interval(
    label: str,
    *,
    one_degree_floor: bool = False,
) -> tuple[str, Decimal | None, Decimal | None]:
    compact = re.sub(r"\s+", " ", label.strip())
    unit_match = re.search(r"°\s*([CF])", compact, flags=re.IGNORECASE)
    if unit_match is None:
        raise WeatherDataError(f"temperature unit missing from bin label: {label}")
    unit = unit_match.group(1).upper()
    numbers = [
        Decimal(item)
        for item in re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", compact)
    ]
    lowered = compact.casefold()
    if len(numbers) == 1 and any(
        phrase in lowered for phrase in ("or below", "or lower", "or less")
    ):
        upper = numbers[0] + (Decimal("1") if one_degree_floor else HALF)
        return unit, None, upper
    if len(numbers) == 1 and any(
        phrase in lowered for phrase in ("or higher", "or above", "or more")
    ):
        lower = numbers[0] if one_degree_floor else numbers[0] - HALF
        return unit, lower, None
    if len(numbers) == 1:
        if one_degree_floor:
            return unit, numbers[0], numbers[0] + Decimal("1")
        return unit, numbers[0] - HALF, numbers[0] + HALF
    if len(numbers) == 2 and numbers[0] <= numbers[1]:
        if one_degree_floor:
            return unit, numbers[0], numbers[1] + Decimal("1")
        return unit, numbers[0] - HALF, numbers[1] + HALF
    raise WeatherDataError(f"unsupported temperature bin label: {label}")


def _parse_city_and_metric(title: str) -> tuple[str, str]:
    match = re.search(
        r"\b(highest|lowest)\s+temperature\s+in\s+(.+?)\s+on\b",
        title,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise WeatherDataError("event title does not identify metric, city, and date")
    metric = "high" if match.group(1).casefold() == "highest" else "low"
    return match.group(2).strip(), metric


def _revision_cutoff(rules: str) -> str:
    lowered = rules.casefold()
    if "first data point" in lowered and any(
        phrase in lowered
        for phrase in (
            "following day",
            "following date",
            "subsequent day",
            "next day",
            "day after",
        )
    ):
        return "first_next_day_publication"
    if "initial publication" in lowered:
        return "initial_publication"
    if "finalized" in lowered or "finalised" in lowered:
        return "source_finalized"
    return "unspecified"


def _station_code_from_source(source: str) -> str:
    code_match = re.search(
        r"/([A-Z0-9]{3,5})(?=/(?:date|history)(?:/|$)|/?(?:[?#]|$))",
        source,
    )
    if code_match is None:
        code_match = re.search(
            r"[?&](?:sid|site|station)=([A-Z0-9]{3,5})\b",
            source,
            flags=re.IGNORECASE,
        )
    return code_match.group(1).upper() if code_match else ""


def _validate_bins(bins: Sequence[WeatherBin]) -> tuple[WeatherBin, ...]:
    if len(bins) != 11:
        raise WeatherDataError(f"weather event must have exactly 11 bins, got {len(bins)}")
    for field_name in ("market_id", "condition_id"):
        identities = [
            str(getattr(weather_bin, field_name)).strip()
            for weather_bin in bins
        ]
        if any(not identity for identity in identities):
            raise WeatherDataError(f"weather-bin {field_name} values must be non-empty")
        if len(set(identities)) != len(identities):
            raise WeatherDataError(f"weather-bin {field_name} values must be unique")
    token_ids = [
        token_id.strip()
        for weather_bin in bins
        for token_id in (weather_bin.yes_token_id, weather_bin.no_token_id)
    ]
    if any(not token_id for token_id in token_ids):
        raise WeatherDataError("weather-bin token IDs must be non-empty")
    if len(set(token_ids)) != len(token_ids):
        raise WeatherDataError("weather-bin token IDs must be unique")
    ordered = tuple(
        sorted(
            bins,
            key=lambda item: (
                item.lower_bound is not None,
                item.lower_bound if item.lower_bound is not None else ZERO,
            ),
        )
    )
    if ordered[0].lower_bound is not None or ordered[-1].upper_bound is not None:
        raise WeatherDataError("weather bins must include lower and upper tails")
    for current, following in zip(ordered, ordered[1:]):
        if current.upper_bound != following.lower_bound:
            raise WeatherDataError(
                f"weather bins overlap or leave a gap: {current.label}, {following.label}"
            )
    return ordered


def parse_gamma_weather_event(
    raw: Mapping[str, Any],
    *,
    timezone_name: str | None = None,
) -> WeatherEventSpec:
    """Parse one Gamma event, failing closed on ambiguous bin semantics."""

    title = str(raw.get("title") or "")
    city, metric = _parse_city_and_metric(title)
    rules = str(raw.get("description") or "")
    if not rules:
        raise WeatherDataError("weather event rules are missing")
    try:
        local_date = date.fromisoformat(str(raw["eventDate"])[:10])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherDataError("eventDate must contain an ISO local date") from exc

    timezone_value = timezone_name or _CITY_TIMEZONES.get(city)
    if not timezone_value:
        raise WeatherDataError(f"timezone is unknown for city {city}")

    station_match = re.search(
        r"data point for (?:the )?(.+?)(?:\s+Station)?,\s+which can be found",
        rules,
        flags=re.IGNORECASE,
    )
    if station_match is None:
        station_match = re.search(
            r"recorded at (?:the )?(.+?)\s+Station\s+in degrees",
            rules,
            flags=re.IGNORECASE,
        )
    if station_match is None:
        station_match = re.search(
            r"for (?:the )?(.+?)\s+Station,\s+available here",
            rules,
            flags=re.IGNORECASE,
        )
    station_name = station_match.group(1).strip() if station_match else ""
    url_matches = [
        url.rstrip(".,;")
        for url in re.findall(r"https?://[^\s)]+", rules)
    ]
    declared_source = str(raw.get("resolutionSource") or "").strip()
    source_candidates = [
        source
        for source in (declared_source, *url_matches)
        if source
    ]
    source = next(
        (
            candidate
            for candidate in source_candidates
            if _station_code_from_source(candidate)
        ),
        source_candidates[0] if source_candidates else "",
    )
    if not source:
        raise WeatherDataError("resolution source is missing")
    hko_semantics = (
        "weather.gov.hk" in source.casefold()
        or "hong kong observatory" in rules.casefold()
    )
    if hko_semantics:
        station_name = station_name or "Hong Kong Observatory"

    station_code = _station_code_from_source(source)
    if hko_semantics:
        station_code = station_code or "HKO"
    station_name = station_name or station_code
    expected_station_code = _STATION_NAME_CODES.get(station_name.casefold())
    quarantine_reasons: list[str] = []
    revision_cutoff = _revision_cutoff(rules)
    if not station_name:
        quarantine_reasons.append("station_name_missing")
    if not station_code:
        quarantine_reasons.append("station_code_missing")
    if revision_cutoff == "unspecified":
        quarantine_reasons.append("revision_cutoff_unspecified")
    if (
        expected_station_code
        and station_code
        and expected_station_code != station_code
    ):
        quarantine_reasons.append("station_source_mismatch")

    markets = raw.get("markets")
    if not isinstance(markets, list):
        raise WeatherDataError("Gamma event markets must be an array")
    parsed_bins: list[WeatherBin] = []
    units: set[str] = set()
    for index, market in enumerate(markets):
        if not isinstance(market, Mapping):
            raise WeatherDataError(f"market {index} must be an object")
        outcomes = [str(item) for item in _json_array(market.get("outcomes"), field="outcomes")]
        token_ids = [
            str(item)
            for item in _json_array(market.get("clobTokenIds"), field="clobTokenIds")
        ]
        raw_outcome_prices = _json_array(
            market.get("outcomePrices"),
            field="outcomePrices",
        )
        try:
            outcome_prices = [
                Decimal(str(item)) for item in raw_outcome_prices
            ]
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise WeatherDataError(
                f"market {index} outcome prices are not numeric"
            ) from exc
        if (
            len(outcomes) != 2
            or len(outcomes) != len(token_ids)
            or len(outcomes) != len(outcome_prices)
            or set(outcomes) != {"Yes", "No"}
        ):
            raise WeatherDataError(f"market {index} lacks one Yes and one No token")
        if any(
            not price.is_finite() or not ZERO <= price <= ONE
            for price in outcome_prices
        ):
            raise WeatherDataError(f"market {index} has an invalid outcome price")
        tokens = dict(zip(outcomes, token_ids))
        prices = dict(zip(outcomes, outcome_prices))
        label = str(market.get("groupItemTitle") or market.get("question") or "")
        unit, lower, upper = _temperature_interval(
            label,
            one_degree_floor=hko_semantics,
        )
        units.add(unit)
        parsed_bins.append(
            WeatherBin(
                market_id=str(market.get("id") or ""),
                condition_id=str(market.get("conditionId") or ""),
                label=label,
                yes_token_id=tokens["Yes"],
                no_token_id=tokens["No"],
                lower_bound=lower,
                upper_bound=upper,
                yes_outcome_price=prices["Yes"],
                no_outcome_price=prices["No"],
            )
        )
    if len(units) != 1:
        raise WeatherDataError("all weather bins must use the same unit")
    bins = _validate_bins(parsed_bins)

    return WeatherEventSpec(
        event_id=str(raw.get("id") or ""),
        slug=str(raw.get("slug") or ""),
        city=city,
        station_name=station_name,
        station_code=station_code,
        local_date=local_date,
        timezone=timezone_value,
        metric=metric,
        unit=next(iter(units)),
        reporting_semantics=(
            "one_degree_floor_interval" if hko_semantics else "rounded_integer"
        ),
        resolution_source=source,
        rules=rules,
        rules_hash=hashlib.sha256(rules.encode("utf-8")).hexdigest(),
        revision_cutoff=revision_cutoff,
        bins=bins,
        quarantine_reasons=tuple(quarantine_reasons),
    )


__all__ = [
    "CalibrationRow",
    "ForecastVintage",
    "FutureDataError",
    "GaussianResidualCalibration",
    "HourlyTemperature",
    "ReliabilityBucket",
    "WeatherBin",
    "WeatherDataError",
    "WeatherEventSpec",
    "WeatherQuoteDecision",
    "WeatherQuotePolicy",
    "brier_score",
    "city_weather_split",
    "chronological_weather_split",
    "gaussian_bin_probabilities",
    "log_loss",
    "parse_gamma_weather_event",
    "multiclass_brier_score",
    "multiclass_log_loss",
    "reliability_table",
    "local_day_extreme",
    "select_latest_vintage",
    "weather_quote_decision",
]
