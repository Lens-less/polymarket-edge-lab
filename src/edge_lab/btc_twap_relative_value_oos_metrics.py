from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FIVE_PERCENT = Decimal("0.05")
_ECE_BIN_COUNT = 10
_BOOTSTRAP_RESAMPLES = 5_000
_BOOTSTRAP_SEED = 712
_SIGNAL_BINS = 4
_MIN_SIGNAL_BIN_COUNT = 5


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{label} must be decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _probability(value: Any, *, label: str) -> Decimal:
    parsed = _decimal(value, label=label)
    if not _ZERO <= parsed <= _ONE:
        raise ValueError(f"{label} must be in [0, 1]")
    return parsed


def _non_negative_decimal(value: Any, *, label: str) -> Decimal:
    parsed = _decimal(value, label=label)
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative")
    return parsed


def _optional_tau_seconds(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("decision_tau_seconds must be a non-negative integer or None")
    return value


def _validated_forecasts(
    rows: tuple[OOSForecastRow, ...], *, horizon: str
) -> tuple[OOSForecastRow, ...]:
    selected = tuple(row for row in rows if row.horizon == horizon)
    if not selected:
        return ()
    seen: set[tuple[str, str, int | None]] = set()
    for row in selected:
        identity = (row.event_cluster_id, row.horizon, row.decision_tau_seconds)
        if identity in seen:
            raise ValueError("duplicate forecast identity")
        seen.add(identity)
    return selected


@dataclass(frozen=True)
class OOSForecastRow:
    horizon: str
    event_cluster_id: str
    model_probability_up: Decimal
    market_probability_up: Decimal
    actual_up: bool
    decision_tau_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.horizon not in {"5m", "15m"}:
            raise ValueError("horizon must be 5m or 15m")
        if not isinstance(self.event_cluster_id, str) or not self.event_cluster_id:
            raise ValueError("event_cluster_id must be non-empty")
        object.__setattr__(
            self,
            "model_probability_up",
            _probability(self.model_probability_up, label="model_probability_up"),
        )
        object.__setattr__(
            self,
            "market_probability_up",
            _probability(self.market_probability_up, label="market_probability_up"),
        )
        object.__setattr__(
            self,
            "decision_tau_seconds",
            _optional_tau_seconds(self.decision_tau_seconds),
        )
        if not isinstance(self.actual_up, bool):
            raise TypeError("actual_up must be bool")


@dataclass(frozen=True)
class OOSTradeRow:
    event_cluster_id: str
    net_pnl: Decimal
    transient_naked_exposure_peak_usdc: Decimal
    planned_single_leg_max_loss_usdc: Decimal
    signal_strength: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.event_cluster_id, str) or not self.event_cluster_id:
            raise ValueError("event_cluster_id must be non-empty")
        object.__setattr__(self, "net_pnl", _decimal(self.net_pnl, label="net_pnl"))
        object.__setattr__(
            self,
            "transient_naked_exposure_peak_usdc",
            _non_negative_decimal(
                self.transient_naked_exposure_peak_usdc,
                label="transient_naked_exposure_peak_usdc",
            ),
        )
        planned = _non_negative_decimal(
            self.planned_single_leg_max_loss_usdc,
            label="planned_single_leg_max_loss_usdc",
        )
        if planned <= 0:
            raise ValueError("planned_single_leg_max_loss_usdc must be positive")
        object.__setattr__(self, "planned_single_leg_max_loss_usdc", planned)
        object.__setattr__(
            self,
            "signal_strength",
            _non_negative_decimal(self.signal_strength, label="signal_strength"),
        )


def brier_score(
    rows: tuple[OOSForecastRow, ...], *, horizon: str, baseline: bool
) -> Decimal | None:
    selected = _validated_forecasts(rows, horizon=horizon)
    if not selected:
        return None
    total = _ZERO
    for row in selected:
        probability = (
            row.market_probability_up if baseline else row.model_probability_up
        )
        actual = _ONE if row.actual_up else _ZERO
        diff = probability - actual
        total += diff * diff
    return total / Decimal(len(selected))


def expected_calibration_error(
    rows: tuple[OOSForecastRow, ...],
    *,
    horizon: str,
    bins: int = _ECE_BIN_COUNT,
) -> Decimal | None:
    if bins <= 0:
        raise ValueError("bins must be positive")
    selected = _validated_forecasts(rows, horizon=horizon)
    if not selected:
        return None
    bucketed: list[list[OOSForecastRow]] = [[] for _ in range(bins)]
    for row in selected:
        idx = int(
            min(
                bins - 1,
                int(
                    (row.model_probability_up * Decimal(bins)).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                ),
            )
        )
        bucketed[idx].append(row)
    total = Decimal(len(selected))
    error = _ZERO
    for bucket in bucketed:
        if not bucket:
            continue
        mean_probability = sum(
            (row.model_probability_up for row in bucket), _ZERO
        ) / Decimal(len(bucket))
        realized = Decimal(sum(1 for row in bucket if row.actual_up)) / Decimal(
            len(bucket)
        )
        error += (Decimal(len(bucket)) / total) * abs(mean_probability - realized)
    return error


def bootstrap_cluster_mean_lower_95(
    trades: tuple[OOSTradeRow, ...],
    *,
    resamples: int = _BOOTSTRAP_RESAMPLES,
    seed: int = _BOOTSTRAP_SEED,
) -> Decimal | None:
    if not trades:
        return None
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    clusters: dict[str, Decimal] = {}
    for row in trades:
        clusters[row.event_cluster_id] = (
            clusters.get(row.event_cluster_id, _ZERO) + row.net_pnl
        )
    cluster_values = tuple(clusters.values())
    rng = random.Random(seed)
    means: list[Decimal] = []
    cluster_count = len(cluster_values)
    for _ in range(resamples):
        sampled = [
            cluster_values[rng.randrange(cluster_count)] for _ in range(cluster_count)
        ]
        means.append(sum(sampled, _ZERO) / Decimal(cluster_count))
    means.sort()
    lower_index = max(
        0,
        int(
            (Decimal(resamples) * _FIVE_PERCENT).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        - 1,
    )
    return means[lower_index]


def maximum_absolute_event_contribution_share(
    trades: tuple[OOSTradeRow, ...],
) -> Decimal | None:
    if not trades:
        return None
    by_cluster: dict[str, Decimal] = {}
    for row in trades:
        by_cluster[row.event_cluster_id] = (
            by_cluster.get(row.event_cluster_id, _ZERO) + row.net_pnl
        )
    absolute_totals = [abs(value) for value in by_cluster.values() if value != 0]
    if not absolute_totals:
        return None
    total_absolute = sum(absolute_totals, _ZERO)
    return max(absolute_totals) / total_absolute


def direction_exposure_below_single_leg(
    trades: tuple[OOSTradeRow, ...],
) -> bool | None:
    if not trades:
        return None
    return all(
        row.transient_naked_exposure_peak_usdc < row.planned_single_leg_max_loss_usdc
        for row in trades
    )


def signal_strength_net_pnl_monotonic(
    trades: tuple[OOSTradeRow, ...],
    *,
    bin_count: int = _SIGNAL_BINS,
    minimum_bin_count: int = _MIN_SIGNAL_BIN_COUNT,
) -> bool | None:
    if bin_count <= 0:
        raise ValueError("bin_count must be positive")
    if minimum_bin_count <= 0:
        raise ValueError("minimum_bin_count must be positive")
    required = bin_count * minimum_bin_count
    if len(trades) < required:
        return None
    ordered = sorted(
        trades,
        key=lambda row: (
            row.signal_strength,
            row.event_cluster_id,
            row.net_pnl,
            row.transient_naked_exposure_peak_usdc,
            row.planned_single_leg_max_loss_usdc,
        ),
    )
    means: list[Decimal] = []
    for index in range(bin_count):
        start = index * len(ordered) // bin_count
        end = (index + 1) * len(ordered) // bin_count
        bucket = ordered[start:end]
        if len(bucket) < minimum_bin_count:
            return None
        means.append(sum((row.net_pnl for row in bucket), _ZERO) / Decimal(len(bucket)))
    return all(current <= following for current, following in zip(means, means[1:]))


__all__ = [
    "OOSForecastRow",
    "OOSTradeRow",
    "maximum_absolute_event_contribution_share",
    "brier_score",
    "bootstrap_cluster_mean_lower_95",
    "direction_exposure_below_single_leg",
    "expected_calibration_error",
    "signal_strength_net_pnl_monotonic",
]
