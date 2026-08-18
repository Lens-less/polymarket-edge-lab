"""Reproducible experiment records and conservative validation statistics.

This module deliberately has no market-data, credential, or execution
dependencies.  It provides a small audit seam between an experiment runner and
the evidence it claims:

* immutable, append-only JSONL experiment records;
* event-level bootstrap intervals (fills within an event are never sampled as
  if they were independent);
* leakage-resistant train/validation/test split helpers;
* reward and profit-concentration stress tests; and
* explicit profitability validation gates.

Financial ``Decimal`` values are persisted as strings so registry artifacts do
not acquire binary floating-point drift.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence

from .portable_fcntl import LOCK_EX, LOCK_SH, LOCK_UN, flock


ZERO = Decimal("0")
ONE = Decimal("1")
DEFAULT_REWARD_MULTIPLIERS = (
    Decimal("1"),
    Decimal("0.5"),
    Decimal("0.3"),
    Decimal("0"),
)
VALIDATION_LABELS = frozenset(
    {
        "validated_profitable",
        "promising_not_validated",
        "insufficient_data",
        "rejected",
    }
)


class ExperimentRegistryError(ValueError):
    """Base class for registry validation and corruption errors."""


class ExperimentConflictError(ExperimentRegistryError):
    """Raised when one experiment ID is associated with different content."""


class CorruptExperimentRegistryError(ExperimentRegistryError):
    """Raised when an existing registry row cannot be validated."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _deep_freeze(value: Any) -> Any:
    """Take an immutable recursive snapshot without changing scalar types."""

    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            canonical_key = str(key)
            if canonical_key in frozen:
                raise ValueError(f"duplicate canonical mapping key: {canonical_key}")
            frozen[canonical_key] = _deep_freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        frozen_items = tuple(_deep_freeze(item) for item in value)
        return tuple(sorted(frozen_items, key=canonical_json))
    if isinstance(value, (Decimal, Path, datetime)):
        _canonicalize(value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("float values must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported registry value type: {type(value).__name__}")


def _canonicalize(value: Any) -> Any:
    """Convert a value into deterministic, JSON-compatible data.

    Decimal is intentionally converted with ``str`` rather than ``float``.
    Trailing zeroes are retained because they can encode experiment precision.
    """

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal values must be finite")
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime values must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        canonical_mapping: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            canonical_key = str(key)
            if canonical_key in canonical_mapping:
                raise ValueError(
                    f"duplicate canonical mapping key: {canonical_key}"
                )
            canonical_mapping[canonical_key] = _canonicalize(item)
        return canonical_mapping
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        canonical_items = [_canonicalize(item) for item in value]
        return sorted(
            canonical_items,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("float values must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported registry value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON suitable for hashing and JSONL."""

    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    """Return a namespaced SHA-256 digest of canonical JSON content."""

    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def make_experiment_id(
    *,
    strategy: str,
    canonical_params: Mapping[str, Any],
    data_manifest_hash: str,
    git_snapshot: str,
    splits: Mapping[str, Sequence[str]],
    random_seed: int,
) -> str:
    """Derive an experiment ID from every reproducibility-defining input."""

    digest = canonical_hash(
        {
            "strategy": strategy,
            "canonical_params": canonical_params,
            "data_manifest_hash": data_manifest_hash,
            "git_snapshot": git_snapshot,
            "splits": splits,
            "random_seed": random_seed,
        }
    ).removeprefix("sha256:")
    return f"exp-{digest[:24]}"


def _normalise_splits(
    splits: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    if not isinstance(splits, Mapping):
        raise TypeError("splits must be a mapping")
    required = ("train", "validation", "test")
    missing = [name for name in required if name not in splits]
    if missing:
        raise ValueError(f"missing experiment splits: {', '.join(missing)}")

    normalised: dict[str, tuple[str, ...]] = {}
    owner: dict[str, str] = {}
    for name in required:
        if isinstance(splits[name], (str, bytes)):
            raise TypeError(f"{name} split must be a sequence of event IDs")
        event_ids = tuple(str(event_id) for event_id in splits[name])
        if len(event_ids) != len(set(event_ids)):
            raise ValueError(f"duplicate event ID within {name} split")
        for event_id in event_ids:
            if not event_id:
                raise ValueError("event IDs must be non-empty")
            previous = owner.get(event_id)
            if previous is not None:
                raise ValueError(
                    f"event {event_id} appears in both {previous} and {name}"
                )
            owner[event_id] = name
        normalised[name] = event_ids
    return normalised


@dataclass(frozen=True)
class ExperimentRecord:
    """One immutable registry row describing a reproducible experiment."""

    experiment_id: str
    canonical_params: Mapping[str, Any]
    data_manifest_hash: str
    git_snapshot: str
    status: str
    strategy: str
    data_level: str
    splits: Mapping[str, Sequence[str]]
    random_seed: int
    started_at: str
    finished_at: Optional[str]
    metrics: Mapping[str, Any]
    artifacts: Sequence[str]
    failed_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.experiment_id:
            raise ValueError("experiment_id must be non-empty")
        if not self.strategy:
            raise ValueError("strategy must be non-empty")
        if not self.data_manifest_hash:
            raise ValueError("data_manifest_hash must be non-empty")
        if not self.git_snapshot:
            raise ValueError("git_snapshot must be non-empty")
        if not self.data_level:
            raise ValueError("data_level must be non-empty")
        if not self.started_at:
            raise ValueError("started_at must be non-empty")
        if not isinstance(self.canonical_params, Mapping):
            raise TypeError("canonical_params must be a mapping")
        if not isinstance(self.metrics, Mapping):
            raise TypeError("metrics must be a mapping")
        if isinstance(self.artifacts, (str, bytes)):
            raise TypeError("artifacts must be a sequence of paths")
        if self.status not in {
            "planned",
            "running",
            "completed",
            "failed",
            "rejected",
        }:
            raise ValueError(f"unsupported experiment status: {self.status}")
        if self.status in {"completed", "failed", "rejected"} and not self.finished_at:
            raise ValueError(f"{self.status} experiments require finished_at")
        if self.status == "failed" and not self.failed_reason:
            raise ValueError("failed experiments require failed_reason")
        if self.status != "failed" and self.failed_reason:
            raise ValueError("failed_reason is only valid for failed experiments")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise TypeError("random_seed must be an integer")

        normalised_splits = _normalise_splits(self.splits)
        object.__setattr__(
            self,
            "canonical_params",
            _deep_freeze(_canonicalize(self.canonical_params)),
        )
        object.__setattr__(
            self, "metrics", _deep_freeze(_canonicalize(self.metrics))
        )
        object.__setattr__(self, "splits", _deep_freeze(normalised_splits))
        object.__setattr__(
            self, "artifacts", tuple(str(artifact) for artifact in self.artifacts)
        )

        # Validate nested content at construction time rather than during the
        # eventual append, when an error could otherwise leave partial work.
        _canonicalize(self.canonical_params)
        _canonicalize(self.metrics)

    def to_mapping(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible registry representation."""

        return _canonicalize(
            {
                "experiment_id": self.experiment_id,
                "canonical_params": self.canonical_params,
                "data_manifest_hash": self.data_manifest_hash,
                "git_snapshot": self.git_snapshot,
                "status": self.status,
                "strategy": self.strategy,
                "data_level": self.data_level,
                "splits": self.splits,
                "random_seed": self.random_seed,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "metrics": self.metrics,
                "artifacts": self.artifacts,
                "failed_reason": self.failed_reason,
            }
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ExperimentRecord":
        try:
            return cls(
                experiment_id=str(raw["experiment_id"]),
                canonical_params=dict(raw["canonical_params"]),
                data_manifest_hash=str(raw["data_manifest_hash"]),
                git_snapshot=str(raw["git_snapshot"]),
                status=str(raw["status"]),
                strategy=str(raw["strategy"]),
                data_level=str(raw["data_level"]),
                splits=dict(raw["splits"]),
                random_seed=int(raw["random_seed"]),
                started_at=str(raw["started_at"]),
                finished_at=(
                    str(raw["finished_at"])
                    if raw.get("finished_at") is not None
                    else None
                ),
                metrics=dict(raw["metrics"]),
                artifacts=tuple(str(item) for item in raw["artifacts"]),
                failed_reason=(
                    str(raw["failed_reason"])
                    if raw.get("failed_reason") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptExperimentRegistryError(
                f"invalid experiment registry row: {exc}"
            ) from exc


class ExperimentRegistry:
    """File-locked append-only JSONL registry.

    Re-appending byte-equivalent canonical content is idempotent.  Reusing an
    existing ``experiment_id`` for different content fails closed.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @staticmethod
    def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError(f"duplicate JSON key: {key}")
            decoded[key] = value
        return decoded

    @staticmethod
    def _read_handle(handle: Any) -> tuple[list[ExperimentRecord], dict[str, str]]:
        handle.seek(0)
        records: list[ExperimentRecord] = []
        rows_by_id: dict[str, str] = {}
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                raise CorruptExperimentRegistryError(
                    f"blank registry row at line {line_number}"
                )
            try:
                decoded = json.loads(
                    line,
                    object_pairs_hook=ExperimentRegistry._strict_json_object,
                    parse_constant=_reject_json_constant,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise CorruptExperimentRegistryError(
                    f"invalid JSON at line {line_number}: {exc}"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise CorruptExperimentRegistryError(
                    f"registry line {line_number} is not an object"
                )
            record = ExperimentRecord.from_mapping(decoded)
            canonical_line = canonical_json(record.to_mapping())
            previous = rows_by_id.get(record.experiment_id)
            if previous is not None and previous != canonical_line:
                raise ExperimentConflictError(
                    f"conflicting rows for experiment_id {record.experiment_id}"
                )
            if previous is None:
                records.append(record)
                rows_by_id[record.experiment_id] = canonical_line
        return records, rows_by_id

    def append(self, record: ExperimentRecord) -> bool:
        """Append one record; return ``False`` for an idempotent duplicate."""

        if not isinstance(record, ExperimentRecord):
            raise TypeError("record must be an ExperimentRecord")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        canonical_line = canonical_json(record.to_mapping())
        with self.path.open("a+", encoding="utf-8") as handle:
            flock(handle.fileno(), LOCK_EX)
            try:
                _, rows_by_id = self._read_handle(handle)
                previous = rows_by_id.get(record.experiment_id)
                if previous is not None:
                    if previous != canonical_line:
                        raise ExperimentConflictError(
                            "experiment_id "
                            f"{record.experiment_id} already has different content"
                        )
                    return False
                handle.seek(0, os.SEEK_END)
                handle.write(canonical_line)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                return True
            finally:
                flock(handle.fileno(), LOCK_UN)

    def read_all(self) -> tuple[ExperimentRecord, ...]:
        if not self.path.exists():
            return ()
        with self.path.open("r", encoding="utf-8") as handle:
            flock(handle.fileno(), LOCK_SH)
            try:
                records, _ = self._read_handle(handle)
                return tuple(records)
            finally:
                flock(handle.fileno(), LOCK_UN)

    def get(self, experiment_id: str) -> Optional[ExperimentRecord]:
        return next(
            (
                record
                for record in self.read_all()
                if record.experiment_id == experiment_id
            ),
            None,
        )


def _aggregate_event_profit(
    records: Iterable[Mapping[str, Any]],
    *,
    event_key: str,
    profit_key: str,
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row in records:
        if event_key not in row:
            raise ValueError(f"record missing {event_key}")
        if profit_key not in row:
            raise ValueError(f"record missing {profit_key}")
        event_id = str(row[event_key])
        if not event_id:
            raise ValueError("event IDs must be non-empty")
        profit = row[profit_key]
        try:
            decimal_profit = (
                profit if isinstance(profit, Decimal) else Decimal(str(profit))
            )
        except Exception as exc:
            raise ValueError(f"invalid profit for event {event_id}: {profit}") from exc
        if not decimal_profit.is_finite():
            raise ValueError(f"profit for event {event_id} must be finite")
        totals[event_id] = totals.get(event_id, ZERO) + decimal_profit
    if not totals:
        raise ValueError("at least one event is required")
    return totals


def _percentile(values: Sequence[Decimal], probability: Decimal) -> Decimal:
    if not values:
        raise ValueError("cannot calculate a percentile of no values")
    if not (ZERO <= probability <= ONE):
        raise ValueError("probability must be between zero and one")
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * probability
    lower_index = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - Decimal(lower_index)
    return ordered[lower_index] + (
        ordered[upper_index] - ordered[lower_index]
    ) * weight


@dataclass(frozen=True)
class BootstrapInterval:
    lower: Decimal
    estimate: Decimal
    upper: Decimal
    confidence: Decimal
    n_events: int
    n_resamples: int
    seed: int
    unit: str = "mean_profit_per_event"


def bootstrap_event_profit_ci(
    records: Iterable[Mapping[str, Any]],
    *,
    event_key: str = "event_id",
    profit_key: str = "profit",
    confidence: Decimal = Decimal("0.95"),
    n_resamples: int = 10_000,
    seed: int = 0,
) -> BootstrapInterval:
    """Percentile bootstrap CI over independently resampled events.

    Fill-level profits are aggregated first.  Each bootstrap draw then samples
    the same number of whole events with replacement and reports the mean event
    profit, preserving arbitrary dependence among fills from one event.
    """

    confidence = Decimal(str(confidence))
    if not confidence.is_finite() or not (ZERO < confidence < ONE):
        raise ValueError("confidence must be between zero and one")
    if n_resamples <= 0:
        raise ValueError("n_resamples must be positive")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an integer")

    event_totals = _aggregate_event_profit(
        records, event_key=event_key, profit_key=profit_key
    )
    profits = tuple(event_totals[event_id] for event_id in sorted(event_totals))
    event_count = len(profits)
    denominator = Decimal(event_count)
    estimate = sum(profits, ZERO) / denominator
    rng = random.Random(seed)
    bootstrap_means: list[Decimal] = []
    for _ in range(n_resamples):
        sampled_total = sum(
            (profits[rng.randrange(event_count)] for _ in range(event_count)),
            ZERO,
        )
        bootstrap_means.append(sampled_total / denominator)

    tail = (ONE - confidence) / Decimal("2")
    return BootstrapInterval(
        lower=_percentile(bootstrap_means, tail),
        estimate=estimate,
        upper=_percentile(bootstrap_means, ONE - tail),
        confidence=confidence,
        n_events=event_count,
        n_resamples=n_resamples,
        seed=seed,
    )


@dataclass(frozen=True)
class ProfitConcentration:
    max_event_share: Decimal
    leading_event_id: Optional[str]
    positive_profit: Decimal
    total_profit: Decimal
    event_count: int


def profit_concentration(
    records: Iterable[Mapping[str, Any]],
    *,
    event_key: str = "event_id",
    profit_key: str = "profit",
) -> ProfitConcentration:
    """Return the largest event's share of gross positive event profit.

    Gross positive profit is the conservative denominator: a large winner is
    not made to look diversified merely because losing events reduce net PnL.
    """

    totals = _aggregate_event_profit(
        records, event_key=event_key, profit_key=profit_key
    )
    positive = {
        event_id: profit for event_id, profit in totals.items() if profit > ZERO
    }
    positive_profit = sum(positive.values(), ZERO)
    if not positive:
        leading_event_id = None
        max_share = ZERO
    else:
        leading_event_id = min(
            positive,
            key=lambda event_id: (-positive[event_id], event_id),
        )
        max_share = positive[leading_event_id] / positive_profit
    return ProfitConcentration(
        max_event_share=max_share,
        leading_event_id=leading_event_id,
        positive_profit=positive_profit,
        total_profit=sum(totals.values(), ZERO),
        event_count=len(totals),
    )


@dataclass(frozen=True)
class EventSplits:
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]

    def __post_init__(self) -> None:
        _normalise_splits(self.as_dict())

    def as_dict(self) -> dict[str, tuple[str, ...]]:
        return {
            "train": self.train,
            "validation": self.validation,
            "test": self.test,
        }


def _normalise_split_fractions(
    train_fraction: Decimal, validation_fraction: Decimal
) -> tuple[Decimal, Decimal]:
    train_fraction = Decimal(str(train_fraction))
    validation_fraction = Decimal(str(validation_fraction))
    if not train_fraction.is_finite() or not validation_fraction.is_finite():
        raise ValueError("split fractions must be finite")
    if train_fraction <= ZERO or validation_fraction <= ZERO:
        raise ValueError("train and validation fractions must be positive")
    if train_fraction + validation_fraction >= ONE:
        raise ValueError("train plus validation fraction must be below one")
    return train_fraction, validation_fraction


def _partition_counts(
    count: int,
    *,
    train_fraction: Decimal,
    validation_fraction: Decimal,
) -> tuple[int, int, int]:
    if count < 0:
        raise ValueError("count must be non-negative")
    fractions = (
        train_fraction,
        validation_fraction,
        ONE - train_fraction - validation_fraction,
    )
    raw = [Decimal(count) * fraction for fraction in fractions]
    counts = [
        int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in raw
    ]
    remainder = count - sum(counts)
    priority = sorted(
        range(3),
        key=lambda index: (-(raw[index] - Decimal(counts[index])), index),
    )
    for index in priority[:remainder]:
        counts[index] += 1
    return counts[0], counts[1], counts[2]


def _partition_ordered(
    ordered_event_ids: Sequence[str],
    *,
    train_fraction: Decimal,
    validation_fraction: Decimal,
) -> EventSplits:
    train_fraction, validation_fraction = _normalise_split_fractions(
        train_fraction, validation_fraction
    )
    train_count, validation_count, _ = _partition_counts(
        len(ordered_event_ids),
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    validation_end = train_count + validation_count
    return EventSplits(
        train=tuple(ordered_event_ids[:train_count]),
        validation=tuple(ordered_event_ids[train_count:validation_end]),
        test=tuple(ordered_event_ids[validation_end:]),
    )


def _sortable_time(value: Any) -> tuple[int, Decimal | str]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)):
        numeric = Decimal(str(value))
        if not numeric.is_finite():
            raise ValueError("event timestamp must be finite")
        return (0, numeric)
    else:
        text = str(value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return (1, text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = Decimal(str(parsed.astimezone(timezone.utc).timestamp()))
    return (0, epoch)


def _event_rows(
    records: Iterable[Mapping[str, Any]],
    *,
    event_key: str,
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in records:
        if event_key not in row:
            raise ValueError(f"record missing {event_key}")
        event_id = str(row[event_key])
        if not event_id:
            raise ValueError("event IDs must be non-empty")
        grouped.setdefault(event_id, []).append(row)
    if not grouped:
        raise ValueError("at least one event is required")
    return grouped


def chronological_event_split(
    records: Iterable[Mapping[str, Any]],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
    event_key: str = "event_id",
    time_key: str = "settled_at",
) -> EventSplits:
    """Split whole events in chronological order of their latest record."""

    grouped = _event_rows(records, event_key=event_key)
    event_times: dict[str, tuple[int, Decimal | str]] = {}
    for event_id, rows in grouped.items():
        missing = [row for row in rows if time_key not in row]
        if missing:
            raise ValueError(f"event {event_id} has a record missing {time_key}")
        event_times[event_id] = max(_sortable_time(row[time_key]) for row in rows)
    ordered = sorted(grouped, key=lambda event_id: (event_times[event_id], event_id))
    return _partition_ordered(
        ordered,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )


def seeded_event_split(
    records: Iterable[Mapping[str, Any]],
    *,
    seed: int,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
    event_key: str = "event_id",
) -> EventSplits:
    """Randomize at event granularity with a fixed seed."""

    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an integer")
    grouped = _event_rows(records, event_key=event_key)
    ordered = sorted(grouped)
    random.Random(seed).shuffle(ordered)
    return _partition_ordered(
        ordered,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )


def city_event_split(
    records: Iterable[Mapping[str, Any]],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
    event_key: str = "event_id",
    city_key: str = "city",
    time_key: str = "settled_at",
) -> EventSplits:
    """Split by whole cities while also keeping every event in one split."""

    train_fraction, validation_fraction = _normalise_split_fractions(
        train_fraction, validation_fraction
    )
    grouped = _event_rows(records, event_key=event_key)
    city_events: dict[str, list[str]] = {}
    city_times: dict[str, tuple[int, Decimal | str]] = {}
    for event_id, rows in grouped.items():
        cities = {
            str(row[city_key])
            for row in rows
            if city_key in row and str(row[city_key])
        }
        if len(cities) != 1:
            raise ValueError(
                f"event {event_id} must have exactly one consistent {city_key}"
            )
        city = next(iter(cities))
        if any(time_key not in row for row in rows):
            raise ValueError(f"event {event_id} has a record missing {time_key}")
        latest = max(_sortable_time(row[time_key]) for row in rows)
        city_events.setdefault(city, []).append(event_id)
        if city not in city_times or latest < city_times[city]:
            city_times[city] = latest

    ordered_cities = sorted(
        city_events, key=lambda city: (city_times[city], city)
    )
    train_count, validation_count, _ = _partition_counts(
        len(ordered_cities),
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    city_slices = {
        "train": ordered_cities[:train_count],
        "validation": ordered_cities[
            train_count : train_count + validation_count
        ],
        "test": ordered_cities[train_count + validation_count :],
    }

    def event_ids(split_name: str) -> tuple[str, ...]:
        return tuple(
            event_id
            for city in city_slices[split_name]
            for event_id in sorted(city_events[city])
        )

    return EventSplits(
        train=event_ids("train"),
        validation=event_ids("validation"),
        test=event_ids("test"),
    )


@dataclass(frozen=True)
class RewardHaircut:
    multiplier: Decimal
    rewards: Decimal
    non_reward_profit: Decimal
    net_profit: Decimal


def apply_reward_haircuts(
    *,
    non_reward_profit: Decimal,
    theoretical_rewards: Decimal,
    multipliers: Sequence[Decimal] = DEFAULT_REWARD_MULTIPLIERS,
) -> tuple[RewardHaircut, ...]:
    """Apply the required 100%, 50%, 30%, and zero-reward stresses."""

    base = Decimal(str(non_reward_profit))
    rewards = Decimal(str(theoretical_rewards))
    if not base.is_finite() or not rewards.is_finite():
        raise ValueError("profit and reward values must be finite")
    if rewards < ZERO:
        raise ValueError("theoretical_rewards cannot be negative")
    normalised = tuple(Decimal(str(multiplier)) for multiplier in multipliers)
    if any(not (ZERO <= multiplier <= ONE) for multiplier in normalised):
        raise ValueError("reward multipliers must be between zero and one")
    return tuple(
        RewardHaircut(
            multiplier=multiplier,
            rewards=rewards * multiplier,
            non_reward_profit=base,
            net_profit=base + rewards * multiplier,
        )
        for multiplier in normalised
    )


@dataclass(frozen=True)
class ValidationEvidence:
    """Evidence consumed by the strict profitability classification gates."""

    data_level: str
    independent_settled_events: int
    oos_fills: int
    pessimistic_profit: Optional[Decimal]
    reward_zero_profit: Optional[Decimal]
    bootstrap_ci_lower: Optional[Decimal]
    max_event_profit_share: Optional[Decimal]
    stable_neighbors: Optional[bool]
    tail_acceptable: Optional[bool]
    recomputable: Optional[bool]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.independent_settled_events, int)
            or isinstance(self.independent_settled_events, bool)
            or not isinstance(self.oos_fills, int)
            or isinstance(self.oos_fills, bool)
        ):
            raise TypeError("event and fill counts must be integers")
        if self.independent_settled_events < 0 or self.oos_fills < 0:
            raise ValueError("event and fill counts cannot be negative")
        for name in (
            "pessimistic_profit",
            "reward_zero_profit",
            "bootstrap_ci_lower",
            "max_event_profit_share",
        ):
            value = getattr(self, name)
            if value is not None:
                decimal_value = Decimal(str(value))
                if not decimal_value.is_finite():
                    raise ValueError(f"{name} must be finite")
                object.__setattr__(self, name, decimal_value)
        if (
            self.max_event_profit_share is not None
            and not ZERO <= self.max_event_profit_share <= ONE
        ):
            raise ValueError("max_event_profit_share must be between zero and one")
        for name in ("stable_neighbors", "tail_acceptable", "recomputable"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be bool or None")


@dataclass(frozen=True)
class ValidationClassification:
    classification: str
    passed_gates: tuple[str, ...]
    failed_gates: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.classification not in VALIDATION_LABELS:
            raise ValueError(f"unsupported classification: {self.classification}")


def _data_level_rank(data_level: str) -> int:
    compact = str(data_level).upper().replace(" ", "")
    match = re.search(r"L([0-9])", compact)
    return int(match.group(1)) if match else -1


def classify_validation(
    evidence: ValidationEvidence,
    *,
    minimum_data_level: str = "L2",
    minimum_independent_settled_events: int = 100,
    minimum_oos_fills: int = 100,
    maximum_event_profit_share: Decimal = Decimal("0.20"),
) -> ValidationClassification:
    """Classify evidence with conservative, explicit profitability gates.

    ``insufficient_data`` takes precedence whenever market-data depth, event
    count, fill count, or a required metric is absent.  With sufficient data,
    a demonstrated economic or tail-risk failure is ``rejected``.  Positive
    economics missing only reproducibility or neighboring-parameter stability
    are ``promising_not_validated``.  Every gate must pass for
    ``validated_profitable``.
    """

    if minimum_independent_settled_events <= 0 or minimum_oos_fills <= 0:
        raise ValueError("minimum event and fill counts must be positive")
    if _data_level_rank(minimum_data_level) < 0:
        raise ValueError("minimum_data_level must contain an L-level")
    maximum_share = Decimal(str(maximum_event_profit_share))
    if not (ZERO < maximum_share <= ONE):
        raise ValueError("maximum_event_profit_share must be in (0, 1]")

    checks: tuple[tuple[str, bool], ...] = (
        (
            f"data_level>={minimum_data_level}",
            _data_level_rank(evidence.data_level)
            >= _data_level_rank(minimum_data_level),
        ),
        (
            f"independent_settled_events>={minimum_independent_settled_events}",
            evidence.independent_settled_events
            >= minimum_independent_settled_events,
        ),
        (
            f"oos_fills>={minimum_oos_fills}",
            evidence.oos_fills >= minimum_oos_fills,
        ),
        (
            "pessimistic_profit>0",
            evidence.pessimistic_profit is not None
            and evidence.pessimistic_profit > ZERO,
        ),
        (
            "reward_zero_profit>0",
            evidence.reward_zero_profit is not None
            and evidence.reward_zero_profit > ZERO,
        ),
        (
            "bootstrap_ci_lower>0",
            evidence.bootstrap_ci_lower is not None
            and evidence.bootstrap_ci_lower > ZERO,
        ),
        (
            f"max_event_profit_share<={maximum_share}",
            evidence.max_event_profit_share is not None
            and evidence.max_event_profit_share <= maximum_share,
        ),
        ("stable_neighbors", evidence.stable_neighbors is True),
        ("tail_acceptable", evidence.tail_acceptable is True),
        ("recomputable", evidence.recomputable is True),
    )
    failed = tuple(name for name, passed in checks if not passed)
    passed = tuple(name for name, passed in checks if passed)
    if not failed:
        classification = "validated_profitable"
    else:
        unavailable_metric = any(
            value is None
            for value in (
                evidence.pessimistic_profit,
                evidence.reward_zero_profit,
                evidence.bootstrap_ci_lower,
                evidence.max_event_profit_share,
                evidence.stable_neighbors,
                evidence.tail_acceptable,
                evidence.recomputable,
            )
        )
        insufficient = unavailable_metric or any(
            name.startswith(("data_level", "independent_settled_events", "oos_fills"))
            for name in failed
        )
        economic_failure = any(
            name.startswith(
                (
                    "pessimistic_profit",
                    "reward_zero_profit",
                    "bootstrap_ci_lower",
                    "max_event_profit_share",
                    "tail_acceptable",
                )
            )
            for name in failed
        )
        if insufficient:
            classification = "insufficient_data"
        elif economic_failure:
            classification = "rejected"
        else:
            classification = "promising_not_validated"
    return ValidationClassification(
        classification=classification,
        passed_gates=passed,
        failed_gates=failed,
    )


__all__ = [
    "BootstrapInterval",
    "CorruptExperimentRegistryError",
    "EventSplits",
    "ExperimentConflictError",
    "ExperimentRecord",
    "ExperimentRegistry",
    "ExperimentRegistryError",
    "ProfitConcentration",
    "RewardHaircut",
    "ValidationClassification",
    "ValidationEvidence",
    "apply_reward_haircuts",
    "bootstrap_event_profit_ci",
    "canonical_hash",
    "canonical_json",
    "chronological_event_split",
    "city_event_split",
    "classify_validation",
    "make_experiment_id",
    "profit_concentration",
    "seeded_event_split",
]
