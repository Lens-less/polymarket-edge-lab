from __future__ import annotations

from decimal import Decimal

import pytest

from src.edge_lab.btc_twap_relative_value_oos_metrics import (
    OOSForecastRow,
    OOSTradeRow,
    bootstrap_cluster_mean_lower_95,
    brier_score,
    direction_exposure_below_single_leg,
    expected_calibration_error,
    maximum_absolute_event_contribution_share,
    signal_strength_net_pnl_monotonic,
)


def _forecast(
    horizon: str,
    event_cluster_id: str,
    *,
    model_probability_up: str,
    market_probability_up: str,
    actual_up: bool,
    decision_tau_seconds: int | None = None,
) -> OOSForecastRow:
    return OOSForecastRow(
        horizon=horizon,
        event_cluster_id=event_cluster_id,
        model_probability_up=Decimal(model_probability_up),
        market_probability_up=Decimal(market_probability_up),
        actual_up=actual_up,
        decision_tau_seconds=decision_tau_seconds,
    )


def _trade(
    event_cluster_id: str,
    *,
    net_pnl: str,
    transient_naked_exposure_peak_usdc: str,
    planned_single_leg_max_loss_usdc: str = "25",
    signal_strength: str,
) -> OOSTradeRow:
    return OOSTradeRow(
        event_cluster_id=event_cluster_id,
        net_pnl=Decimal(net_pnl),
        transient_naked_exposure_peak_usdc=Decimal(transient_naked_exposure_peak_usdc),
        planned_single_leg_max_loss_usdc=Decimal(planned_single_leg_max_loss_usdc),
        signal_strength=Decimal(signal_strength),
    )


def test_brier_score_uses_requested_horizon_and_baseline() -> None:
    rows = (
        _forecast(
            "5m",
            "evt-1",
            model_probability_up="0.9",
            market_probability_up="0.6",
            actual_up=True,
        ),
        _forecast(
            "5m",
            "evt-2",
            model_probability_up="0.1",
            market_probability_up="0.4",
            actual_up=False,
        ),
        _forecast(
            "15m",
            "evt-3",
            model_probability_up="0.8",
            market_probability_up="0.8",
            actual_up=True,
        ),
    )

    assert brier_score(rows, horizon="5m", baseline=False) == Decimal("0.01")
    assert brier_score(rows, horizon="5m", baseline=True) == Decimal("0.16")
    assert brier_score(rows, horizon="15m", baseline=False) == Decimal("0.04")
    assert brier_score(rows, horizon="30m", baseline=False) is None


def test_forecast_metrics_reject_duplicate_identity_but_allow_distinct_tau() -> None:
    duplicate_rows = (
        _forecast(
            "5m",
            "evt-1",
            model_probability_up="0.9",
            market_probability_up="0.6",
            actual_up=True,
        ),
        _forecast(
            "5m",
            "evt-1",
            model_probability_up="0.1",
            market_probability_up="0.4",
            actual_up=False,
        ),
    )
    tau_partitioned_rows = (
        _forecast(
            "5m",
            "evt-1",
            model_probability_up="0.9",
            market_probability_up="0.6",
            actual_up=True,
            decision_tau_seconds=60,
        ),
        _forecast(
            "5m",
            "evt-1",
            model_probability_up="0.1",
            market_probability_up="0.4",
            actual_up=False,
            decision_tau_seconds=120,
        ),
    )

    with pytest.raises(ValueError, match="duplicate forecast identity"):
        brier_score(duplicate_rows, horizon="5m", baseline=False)
    with pytest.raises(ValueError, match="duplicate forecast identity"):
        expected_calibration_error(duplicate_rows, horizon="5m")

    assert brier_score(tau_partitioned_rows, horizon="5m", baseline=False) == Decimal(
        "0.01"
    )
    assert expected_calibration_error(tau_partitioned_rows, horizon="5m") == Decimal(
        "0.1"
    )


def test_expected_calibration_error_uses_ten_equal_width_bins() -> None:
    rows = (
        _forecast(
            "5m",
            "evt-1",
            model_probability_up="0.05",
            market_probability_up="0.5",
            actual_up=False,
        ),
        _forecast(
            "5m",
            "evt-2",
            model_probability_up="0.15",
            market_probability_up="0.5",
            actual_up=True,
        ),
        _forecast(
            "5m",
            "evt-3",
            model_probability_up="0.95",
            market_probability_up="0.5",
            actual_up=False,
        ),
        _forecast(
            "5m",
            "evt-4",
            model_probability_up="1.0",
            market_probability_up="0.5",
            actual_up=True,
        ),
    )

    assert expected_calibration_error(rows, horizon="5m") == Decimal("0.4625")
    assert expected_calibration_error((), horizon="5m") is None


def test_cluster_bootstrap_resamples_by_event_with_seeded_lower_bound() -> None:
    trades = (
        _trade(
            "evt-1",
            net_pnl="5",
            transient_naked_exposure_peak_usdc="1",
            signal_strength="0.01",
        ),
        _trade(
            "evt-1",
            net_pnl="-3",
            transient_naked_exposure_peak_usdc="1",
            signal_strength="0.02",
        ),
        _trade(
            "evt-2",
            net_pnl="1",
            transient_naked_exposure_peak_usdc="1",
            signal_strength="0.03",
        ),
    )

    assert bootstrap_cluster_mean_lower_95(trades) == Decimal("1")
    assert bootstrap_cluster_mean_lower_95(()) is None


def test_absolute_event_concentration_uses_cluster_totals_and_null_without_mass() -> (
    None
):
    trades = (
        _trade(
            "evt-1",
            net_pnl="3",
            transient_naked_exposure_peak_usdc="1",
            signal_strength="0.01",
        ),
        _trade(
            "evt-1",
            net_pnl="2",
            transient_naked_exposure_peak_usdc="1",
            signal_strength="0.02",
        ),
        _trade(
            "evt-2",
            net_pnl="1",
            transient_naked_exposure_peak_usdc="1",
            signal_strength="0.03",
        ),
        _trade(
            "evt-3",
            net_pnl="-4",
            transient_naked_exposure_peak_usdc="1",
            signal_strength="0.04",
        ),
    )

    assert maximum_absolute_event_contribution_share(trades) == Decimal("0.5")
    assert maximum_absolute_event_contribution_share(()) is None
    assert (
        maximum_absolute_event_contribution_share(
            (
                _trade(
                    "evt-1",
                    net_pnl="0",
                    transient_naked_exposure_peak_usdc="1",
                    signal_strength="0.01",
                ),
            )
        )
        is None
    )


def test_direction_exposure_requires_all_trades_below_single_leg() -> None:
    assert (
        direction_exposure_below_single_leg(
            (
                _trade(
                    "evt-1",
                    net_pnl="1",
                    transient_naked_exposure_peak_usdc="5",
                    signal_strength="0.1",
                ),
                _trade(
                    "evt-2",
                    net_pnl="1",
                    transient_naked_exposure_peak_usdc="24.99",
                    signal_strength="0.2",
                ),
            )
        )
        is True
    )
    assert (
        direction_exposure_below_single_leg(
            (
                _trade(
                    "evt-1",
                    net_pnl="1",
                    transient_naked_exposure_peak_usdc="25",
                    signal_strength="0.1",
                ),
            )
        )
        is False
    )
    assert direction_exposure_below_single_leg(()) is None


def test_signal_strength_monotonic_returns_null_when_sample_too_small() -> None:
    trades = tuple(
        _trade(
            f"evt-{index}",
            net_pnl=str(index),
            transient_naked_exposure_peak_usdc="1",
            signal_strength=f"0.{index}",
        )
        for index in range(8)
    )

    assert signal_strength_net_pnl_monotonic(trades) is None


def test_signal_strength_monotonic_checks_equal_count_bins() -> None:
    monotonic = tuple(
        _trade(
            f"evt-{index}",
            net_pnl=str(index // 5),
            transient_naked_exposure_peak_usdc="1",
            signal_strength=f"0.{index:02d}",
        )
        for index in range(20)
    )
    non_monotonic = list(monotonic)
    non_monotonic[-1] = _trade(
        "evt-19",
        net_pnl="-10",
        transient_naked_exposure_peak_usdc="1",
        signal_strength="0.19",
    )

    assert signal_strength_net_pnl_monotonic(monotonic) is True
    assert signal_strength_net_pnl_monotonic(tuple(non_monotonic)) is False


def test_oos_metric_rows_reject_malformed_values() -> None:
    with pytest.raises(ValueError, match="horizon"):
        _forecast(
            "30m",
            "evt-1",
            model_probability_up="0.5",
            market_probability_up="0.5",
            actual_up=True,
        )
    with pytest.raises(ValueError, match="in \\[0, 1\\]"):
        _forecast(
            "5m",
            "evt-1",
            model_probability_up="1.2",
            market_probability_up="0.5",
            actual_up=True,
        )
    with pytest.raises(ValueError, match="non-negative integer or None"):
        OOSForecastRow(
            horizon="5m",
            event_cluster_id="evt-1",
            model_probability_up=Decimal("0.5"),
            market_probability_up=Decimal("0.5"),
            actual_up=True,
            decision_tau_seconds=-1,
        )
    with pytest.raises(TypeError, match="must be decimal"):
        OOSForecastRow(
            horizon="5m",
            event_cluster_id="evt-1",
            model_probability_up=0.5,
            market_probability_up=Decimal("0.5"),
            actual_up=True,
        )
    with pytest.raises(ValueError, match="positive"):
        _trade(
            "evt-1",
            net_pnl="1",
            transient_naked_exposure_peak_usdc="1",
            planned_single_leg_max_loss_usdc="0",
            signal_strength="0.1",
        )
