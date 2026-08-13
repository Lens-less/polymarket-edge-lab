from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from .btc_twap_relative_value import CalibrationPoint, IsotonicProbabilityCalibrator
from .data_store import canonical_json_bytes

_DAY_SPLIT = Literal["train", "validation", "test", "outside"]
_HORIZONS = ("5m", "15m")


def _normalize_utc_day(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("utc_day must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("utc_day must be a valid YYYY-MM-DD string") from exc
    normalized = parsed.isoformat()
    if normalized != value:
        raise ValueError("utc_day must be canonical YYYY-MM-DD")
    return normalized


def _utc_day_from_report(report: dict[str, object]) -> str:
    capture_started_at = report.get("capture_started_at")
    if not isinstance(capture_started_at, str) or len(capture_started_at) < 10:
        raise ValueError("capture_started_at must contain a UTC ISO day")
    return _normalize_utc_day(capture_started_at[:10])


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train_days: tuple[str, ...]
    validation_day: str
    test_day: str

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError("fold index must be a non-negative integer")
        train_days = tuple(_normalize_utc_day(day) for day in self.train_days)
        if len(train_days) != 5:
            raise ValueError("walk-forward folds require exactly five train days")
        if len(set(train_days)) != len(train_days):
            raise ValueError("train_days must be unique")
        validation_day = _normalize_utc_day(self.validation_day)
        test_day = _normalize_utc_day(self.test_day)
        occupied = set(train_days)
        if validation_day in occupied or test_day in occupied or validation_day == test_day:
            raise ValueError("train, validation, and test days must be disjoint")
        ordered = train_days + (validation_day, test_day)
        if tuple(sorted(ordered)) != ordered:
            raise ValueError("fold days must be strictly increasing")
        object.__setattr__(self, "train_days", train_days)
        object.__setattr__(self, "validation_day", validation_day)
        object.__setattr__(self, "test_day", test_day)

    def to_document(self) -> dict[str, object]:
        return {
            "index": self.index,
            "train_days": list(self.train_days),
            "validation_day": self.validation_day,
            "test_day": self.test_day,
        }

    @classmethod
    def from_document(cls, document: dict[str, object]) -> WalkForwardFold:
        return cls(
            index=int(document["index"]),
            train_days=tuple(str(day) for day in document["train_days"]),
            validation_day=str(document["validation_day"]),
            test_day=str(document["test_day"]),
        )


@dataclass(frozen=True)
class HorizonCalibrationPoint:
    horizon: str
    point: CalibrationPoint
    utc_day: str

    def __post_init__(self) -> None:
        if self.horizon not in _HORIZONS:
            raise ValueError("horizon must be 5m or 15m")
        if not isinstance(self.point, CalibrationPoint):
            raise TypeError("point must be a CalibrationPoint")
        object.__setattr__(self, "utc_day", _normalize_utc_day(self.utc_day))


@dataclass(frozen=True)
class WalkForwardCalibrationProvenance:
    fold_index: int
    split: str
    fit_at_ms: int
    maximum_label_available_at_ms: int
    train_days: tuple[str, ...]
    validation_day: str
    test_day: str
    training_event_ids_by_horizon: dict[str, tuple[str, ...]]
    artifact_hashes_by_horizon: dict[str, str]

    def __post_init__(self) -> None:
        if self.split != "train":
            raise ValueError("walk-forward calibration provenance split must be train")
        if isinstance(self.fit_at_ms, bool) or not isinstance(self.fit_at_ms, int) or self.fit_at_ms < 0:
            raise ValueError("fit_at_ms must be non-negative")
        if (
            isinstance(self.maximum_label_available_at_ms, bool)
            or not isinstance(self.maximum_label_available_at_ms, int)
            or self.maximum_label_available_at_ms < 0
        ):
            raise ValueError("maximum_label_available_at_ms must be non-negative")
        if self.maximum_label_available_at_ms >= self.fit_at_ms:
            raise ValueError("maximum_label_available_at_ms must be earlier than fit_at_ms")
        if set(self.training_event_ids_by_horizon) != set(_HORIZONS):
            raise ValueError("provenance must include both horizons")
        if set(self.artifact_hashes_by_horizon) != set(_HORIZONS):
            raise ValueError("artifact hashes must include both horizons")
        object.__setattr__(self, "train_days", tuple(_normalize_utc_day(day) for day in self.train_days))
        object.__setattr__(self, "validation_day", _normalize_utc_day(self.validation_day))
        object.__setattr__(self, "test_day", _normalize_utc_day(self.test_day))
        normalized_ids = {
            horizon: tuple(str(event_id) for event_id in self.training_event_ids_by_horizon[horizon])
            for horizon in _HORIZONS
        }
        normalized_hashes = {
            horizon: str(self.artifact_hashes_by_horizon[horizon]) for horizon in _HORIZONS
        }
        object.__setattr__(self, "training_event_ids_by_horizon", normalized_ids)
        object.__setattr__(self, "artifact_hashes_by_horizon", normalized_hashes)

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": "btc-twap-walk-forward-calibration.v1",
            "fold_index": self.fold_index,
            "split": self.split,
            "fit_at_ms": self.fit_at_ms,
            "maximum_label_available_at_ms": self.maximum_label_available_at_ms,
            "train_days": list(self.train_days),
            "validation_day": self.validation_day,
            "test_day": self.test_day,
            "training_event_ids_by_horizon": {
                horizon: list(self.training_event_ids_by_horizon[horizon]) for horizon in _HORIZONS
            },
            "artifact_hashes_by_horizon": {
                horizon: self.artifact_hashes_by_horizon[horizon] for horizon in _HORIZONS
            },
        }

    @classmethod
    def from_document(
        cls, document: dict[str, object]
    ) -> WalkForwardCalibrationProvenance:
        return cls(
            fold_index=int(document["fold_index"]),
            split=str(document["split"]),
            fit_at_ms=int(document["fit_at_ms"]),
            maximum_label_available_at_ms=int(document["maximum_label_available_at_ms"]),
            train_days=tuple(str(day) for day in document["train_days"]),
            validation_day=str(document["validation_day"]),
            test_day=str(document["test_day"]),
            training_event_ids_by_horizon={
                str(horizon): tuple(str(event_id) for event_id in event_ids)
                for horizon, event_ids in dict(document["training_event_ids_by_horizon"]).items()
            },
            artifact_hashes_by_horizon={
                str(horizon): str(value)
                for horizon, value in dict(document["artifact_hashes_by_horizon"]).items()
            },
        )

    @property
    def artifact_hash(self) -> str:
        import hashlib

        return hashlib.sha256(canonical_json_bytes(self.to_document())).hexdigest()


@dataclass(frozen=True)
class FittedWalkForwardCalibrators:
    artifacts: dict[str, IsotonicProbabilityCalibrator]
    provenance: WalkForwardCalibrationProvenance

    def __post_init__(self) -> None:
        if set(self.artifacts) != set(_HORIZONS):
            raise ValueError("artifacts must include both 5m and 15m calibrators")
        for horizon, calibrator in self.artifacts.items():
            if not isinstance(calibrator, IsotonicProbabilityCalibrator):
                raise TypeError("artifact values must be IsotonicProbabilityCalibrator")
            if calibrator.horizon != horizon:
                raise ValueError("artifact horizon key mismatch")


def build_daily_folds(utc_days: tuple[str, ...]) -> tuple[WalkForwardFold, ...]:
    normalized = tuple(_normalize_utc_day(day) for day in utc_days)
    if tuple(sorted(normalized)) != normalized or len(set(normalized)) != len(normalized):
        raise ValueError("utc_days must be unique and sorted")
    fold_span = 7
    if len(normalized) < fold_span:
        return ()
    return tuple(
        WalkForwardFold(
            index=index,
            train_days=normalized[index : index + 5],
            validation_day=normalized[index + 5],
            test_day=normalized[index + 6],
        )
        for index in range(len(normalized) - fold_span + 1)
    )


def classify_day(utc_day: str, fold: WalkForwardFold) -> _DAY_SPLIT:
    day = _normalize_utc_day(utc_day)
    if day in fold.train_days:
        return "train"
    if day == fold.validation_day:
        return "validation"
    if day == fold.test_day:
        return "test"
    return "outside"


def allow_qualified_decision(utc_day: str, fold: WalkForwardFold) -> bool:
    return classify_day(utc_day, fold) == "test"


def fit_past_only_calibrators(
    rows: tuple[HorizonCalibrationPoint, ...],
    *,
    fold: WalkForwardFold,
    fit_at_ms: int,
    minimum_points_per_horizon: int,
) -> FittedWalkForwardCalibrators:
    if (
        isinstance(fit_at_ms, bool)
        or not isinstance(fit_at_ms, int)
        or fit_at_ms < 0
    ):
        raise ValueError("fit_at_ms must be a non-negative integer")
    if (
        isinstance(minimum_points_per_horizon, bool)
        or not isinstance(minimum_points_per_horizon, int)
        or minimum_points_per_horizon < 2
    ):
        raise ValueError("minimum_points_per_horizon must be at least two")

    filtered: dict[str, list[CalibrationPoint]] = {horizon: [] for horizon in _HORIZONS}
    event_ids_by_horizon: dict[str, list[str]] = {horizon: [] for horizon in _HORIZONS}
    for row in rows:
        split = classify_day(row.utc_day, fold)
        if split != "train":
            raise ValueError(f"future or leaked non-train calibration row: {row.point.event_id}")
        if row.point.split != "train":
            raise ValueError(f"non-train calibration split for event {row.point.event_id}")
        if row.point.label_available_at_ms >= fit_at_ms:
            raise ValueError(f"future label leakage for event {row.point.event_id}")
        filtered[row.horizon].append(row.point)
        event_ids_by_horizon[row.horizon].append(row.point.event_id)

    for horizon in _HORIZONS:
        if len(filtered[horizon]) < minimum_points_per_horizon:
            raise ValueError(
                f"insufficient past-only training rows for {horizon}: "
                f"{len(filtered[horizon])} < {minimum_points_per_horizon}"
            )

    artifacts: dict[str, IsotonicProbabilityCalibrator] = {}
    maximum_label_available_at_ms = -1
    deterministic_event_ids: dict[str, tuple[str, ...]] = {}
    for horizon in _HORIZONS:
        ordered_points = tuple(
            sorted(
                filtered[horizon],
                key=lambda point: (
                    point.label_available_at_ms,
                    point.event_id,
                    point.prediction,
                    int(point.outcome),
                ),
            )
        )
        calibrator = IsotonicProbabilityCalibrator.fit(
            ordered_points,
            fit_at_ms=fit_at_ms,
            horizon=horizon,
        )
        artifacts[horizon] = calibrator
        deterministic_event_ids[horizon] = tuple(calibrator.training_event_ids)
        maximum_label_available_at_ms = max(
            maximum_label_available_at_ms,
            calibrator.maximum_label_available_at_ms,
        )

    provenance = WalkForwardCalibrationProvenance(
        fold_index=fold.index,
        split="train",
        fit_at_ms=fit_at_ms,
        maximum_label_available_at_ms=maximum_label_available_at_ms,
        train_days=fold.train_days,
        validation_day=fold.validation_day,
        test_day=fold.test_day,
        training_event_ids_by_horizon=deterministic_event_ids,
        artifact_hashes_by_horizon={
            horizon: artifacts[horizon].artifact_hash for horizon in _HORIZONS
        },
    )
    return FittedWalkForwardCalibrators(artifacts=artifacts, provenance=provenance)


def summarize_locked_oos_reports(
    reports: tuple[dict[str, object], ...],
    *,
    locked_test_days: tuple[str, ...],
) -> dict[str, object]:
    locked_days = {_normalize_utc_day(day) for day in locked_test_days}
    qualified_event_ids: list[str] = []
    qualified_net_pnl: Decimal | None = None
    qualified_trade_count = 0
    shadow_reports_excluded = 0
    unlocked_reports_excluded = 0

    for report in reports:
        if report.get("paper_only") is not True:
            raise ValueError("paper_only guard malformed")
        if report.get("public_only") is not True:
            raise ValueError("public_only guard malformed")
        if report.get("new_orders_disabled") is not True:
            raise ValueError("new_orders_disabled guard malformed")
        paper_decision = report.get("paper_decision")
        if not isinstance(paper_decision, dict):
            raise TypeError("paper_decision missing")
        if paper_decision.get("orders_submitted") != 0:
            raise ValueError("orders_submitted must stay zero")
        if paper_decision.get("authenticated_endpoints_used") != 0:
            raise ValueError("authenticated_endpoints_used must stay zero")

        if report.get("development_shadow_cycle") is not None:
            shadow_reports_excluded += 1

        utc_day = _utc_day_from_report(report)
        if utc_day not in locked_days:
            unlocked_reports_excluded += 1
            continue

        qualified_cycle = report.get("qualified_cycle")
        if not isinstance(qualified_cycle, dict):
            raise TypeError("qualified_cycle missing")
        if qualified_cycle.get("track") != "qualified":
            raise ValueError("qualified_cycle must stay on the qualified track")
        settlement = qualified_cycle.get("settlement")
        if settlement is None:
            continue
        if not isinstance(settlement, dict):
            raise TypeError("qualified settlement malformed")
        if settlement.get("qualified_sample") is not True:
            continue

        event_id = settlement.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("qualified settlement event_id missing")
        qualified_event_ids.append(event_id)
        qualified_trade_count += 1
        net_pnl_raw = settlement.get("net_pnl")
        if net_pnl_raw is not None:
            qualified_net_pnl = (
                Decimal(str(net_pnl_raw))
                if qualified_net_pnl is None
                else qualified_net_pnl + Decimal(str(net_pnl_raw))
            )

    return {
        "qualified_net_pnl": str(qualified_net_pnl) if qualified_net_pnl is not None else None,
        "qualified_trade_count": qualified_trade_count,
        "qualified_event_ids": qualified_event_ids,
        "shadow_reports_excluded": shadow_reports_excluded,
        "unlocked_reports_excluded": unlocked_reports_excluded,
    }
