from __future__ import annotations

import importlib
from decimal import Decimal

import pytest

from src.edge_lab.btc_twap_relative_value import CalibrationPoint


def _runtime():
    return importlib.import_module(
        "src.edge_lab.btc_twap_relative_value_walk_forward_runtime"
    )


def _point(
    event_id: str,
    prediction: str,
    *,
    outcome: bool,
    label_available_at_ms: int,
) -> CalibrationPoint:
    return CalibrationPoint(
        event_id=event_id,
        prediction=Decimal(prediction),
        outcome=outcome,
        split="train",
        label_available_at_ms=label_available_at_ms,
    )


def _qualified_report(
    *,
    day: str,
    event_id: str,
    qualified_sample: bool,
    net_pnl: str | None,
) -> dict[str, object]:
    return {
        "capture_cycle_id": f"cycle-{day}",
        "capture_started_at": f"{day}T12:00:00Z",
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "paper_decision": {
            "evaluated": True,
            "track": "qualified" if qualified_sample else "development_shadow",
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        },
        "qualified_cycle": {
            "available": qualified_sample,
            "track": "qualified",
            "decision": {
                "action": "long_15_up_long_5_down" if qualified_sample else "no_trade",
                "reason_codes": [] if qualified_sample else ["outside_locked_test_fold"],
            },
            "settlement": (
                {
                    "event_id": event_id,
                    "qualified_sample": True,
                    "explainable": True,
                    "net_pnl": net_pnl,
                }
                if qualified_sample
                else None
            ),
        },
        "development_shadow_cycle": {
            "available": True,
            "track": "development_shadow",
            "decision": {
                "action": "long_15_up_long_5_down",
                "reason_codes": ["uncalibrated_shadow_only"],
            },
            "settlement": {
                "event_id": event_id,
                "qualified_sample": False,
                "explainable": True,
                "net_pnl": net_pnl,
            },
        },
    }


def test_build_daily_folds_returns_disjoint_5_1_1_windows() -> None:
    runtime = _runtime()

    folds = runtime.build_daily_folds(
        (
            "2026-08-01",
            "2026-08-02",
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-08",
        )
    )

    assert len(folds) == 2
    assert folds[0].train_days == (
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
    )
    assert folds[0].validation_day == "2026-08-06"
    assert folds[0].test_day == "2026-08-07"
    assert folds[1].train_days == (
        "2026-08-02",
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
    )
    assert folds[1].validation_day == "2026-08-07"
    assert folds[1].test_day == "2026-08-08"
    assert set(folds[0].train_days).isdisjoint({folds[0].validation_day, folds[0].test_day})


def test_fit_past_only_calibrators_rejects_future_and_non_train_labels() -> None:
    runtime = _runtime()

    rows = (
        runtime.HorizonCalibrationPoint(
            horizon="5m",
            point=_point("train-1", "0.20", outcome=False, label_available_at_ms=100),
            utc_day="2026-08-01",
        ),
        runtime.HorizonCalibrationPoint(
            horizon="5m",
            point=_point("train-2", "0.80", outcome=True, label_available_at_ms=200),
            utc_day="2026-08-02",
        ),
        runtime.HorizonCalibrationPoint(
            horizon="5m",
            point=_point("future-leak", "0.60", outcome=True, label_available_at_ms=600),
            utc_day="2026-08-07",
        ),
        runtime.HorizonCalibrationPoint(
            horizon="15m",
            point=CalibrationPoint(
                event_id="validation-leak",
                prediction=Decimal("0.55"),
                outcome=True,
                split="validation",
                label_available_at_ms=250,
            ),
            utc_day="2026-08-03",
        ),
    )

    with pytest.raises(Exception, match="future|leak|train"):
        runtime.fit_past_only_calibrators(
            rows,
            fold=runtime.WalkForwardFold(
                index=0,
                train_days=(
                    "2026-08-01",
                    "2026-08-02",
                    "2026-08-03",
                    "2026-08-04",
                    "2026-08-05",
                ),
                validation_day="2026-08-06",
                test_day="2026-08-07",
            ),
            fit_at_ms=500,
            minimum_points_per_horizon=20,
        )


def test_fit_past_only_calibrators_requires_20_past_points_per_horizon() -> None:
    runtime = _runtime()

    rows = tuple(
        runtime.HorizonCalibrationPoint(
            horizon="5m" if index % 2 == 0 else "15m",
            point=_point(
                f"train-{index}",
                "0.10" if index % 3 == 0 else "0.90",
                outcome=bool(index % 2),
                label_available_at_ms=100 + index,
            ),
            utc_day="2026-08-01" if index < 10 else "2026-08-02",
        )
        for index in range(18)
    )

    with pytest.raises(Exception, match="20|insufficient"):
        runtime.fit_past_only_calibrators(
            rows,
            fold=runtime.WalkForwardFold(
                index=0,
                train_days=(
                    "2026-08-01",
                    "2026-08-02",
                    "2026-08-03",
                    "2026-08-04",
                    "2026-08-05",
                ),
                validation_day="2026-08-06",
                test_day="2026-08-07",
            ),
            fit_at_ms=1_000,
            minimum_points_per_horizon=20,
        )


def test_fit_past_only_calibrators_returns_provenance_for_each_horizon() -> None:
    runtime = _runtime()

    rows = tuple(
        runtime.HorizonCalibrationPoint(
            horizon=horizon,
            point=_point(
                f"{horizon}-event-{index:02d}",
                "0.15" if index % 2 == 0 else "0.85",
                outcome=bool(index % 3),
                label_available_at_ms=100 + index,
            ),
            utc_day="2026-08-01" if index < 10 else "2026-08-02",
        )
        for horizon in ("5m", "15m")
        for index in range(20)
    )

    fitted = runtime.fit_past_only_calibrators(
        rows,
        fold=runtime.WalkForwardFold(
            index=3,
            train_days=(
                "2026-08-01",
                "2026-08-02",
                "2026-08-03",
                "2026-08-04",
                "2026-08-05",
            ),
            validation_day="2026-08-06",
            test_day="2026-08-07",
        ),
        fit_at_ms=2_000,
        minimum_points_per_horizon=20,
    )

    assert set(fitted.artifacts) == {"5m", "15m"}
    assert fitted.provenance.fold_index == 3
    assert fitted.provenance.split == "train"
    assert fitted.provenance.fit_at_ms == 2_000
    assert fitted.provenance.maximum_label_available_at_ms == 119
    assert len(fitted.provenance.training_event_ids_by_horizon["5m"]) == 20
    assert len(fitted.provenance.training_event_ids_by_horizon["15m"]) == 20


def test_qualified_decision_is_allowed_only_inside_locked_test_day() -> None:
    runtime = _runtime()
    fold = runtime.WalkForwardFold(
        index=0,
        train_days=(
            "2026-08-01",
            "2026-08-02",
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
        ),
        validation_day="2026-08-06",
        test_day="2026-08-07",
    )

    assert runtime.classify_day("2026-08-04", fold) == "train"
    assert runtime.classify_day("2026-08-06", fold) == "validation"
    assert runtime.classify_day("2026-08-07", fold) == "test"
    assert runtime.allow_qualified_decision("2026-08-07", fold) is True
    assert runtime.allow_qualified_decision("2026-08-06", fold) is False
    assert runtime.allow_qualified_decision("2026-08-04", fold) is False


def test_summarize_locked_oos_reports_excludes_shadow_and_unlocked_rows() -> None:
    runtime = _runtime()

    summary = runtime.summarize_locked_oos_reports(
        (
            _qualified_report(
                day="2026-08-07",
                event_id="evt-1",
                qualified_sample=True,
                net_pnl="1.25",
            ),
            _qualified_report(
                day="2026-08-06",
                event_id="evt-2",
                qualified_sample=False,
                net_pnl="-0.50",
            ),
        ),
        locked_test_days=("2026-08-07",),
    )

    assert summary["qualified_net_pnl"] == "1.25"
    assert summary["qualified_trade_count"] == 1
    assert summary["qualified_event_ids"] == ["evt-1"]
    assert summary["shadow_reports_excluded"] == 2
    assert summary["unlocked_reports_excluded"] == 1
