"""Operational readiness tests for continuous BTC TWAP paper capture."""

from __future__ import annotations

import pytest

from src.edge_lab.btc_twap_relative_value_schedule import (
    bootstrap_clock_refresh_at_ms,
    next_bootstrap_expiry_seconds,
)


@pytest.mark.parametrize(
    ("now_ms", "expected_expiry_seconds"),
    (
        (1_786_531_810_000, 1_786_533_300),
        (1_786_532_100_000, 1_786_534_200),
        (1_786_532_400_000, 1_786_534_200),
        (1_786_532_405_001, 1_786_534_200),
    ),
)
def test_restart_targets_a_cycle_with_six_minutes_of_subscription_lead(
    now_ms: int,
    expected_expiry_seconds: int,
) -> None:
    assert next_bootstrap_expiry_seconds(now_ms) == expected_expiry_seconds


@pytest.mark.parametrize("value", (-1, True, 1.5))
def test_bootstrap_boundary_rejects_invalid_clock_values(value: object) -> None:
    with pytest.raises(ValueError, match="now_ms"):
        next_bootstrap_expiry_seconds(value)  # type: ignore[arg-type]


def test_bootstrap_clock_refresh_keeps_all_decisions_inside_frozen_max_age() -> None:
    expiry_seconds = 1_786_533_300
    refresh_at_ms = bootstrap_clock_refresh_at_ms(expiry_seconds)
    latest_decision_at_ms = (expiry_seconds - 45) * 1_000

    assert refresh_at_ms == (expiry_seconds - 900 - 60) * 1_000
    assert latest_decision_at_ms - refresh_at_ms == 915_000
    assert latest_decision_at_ms - refresh_at_ms < 1_200_000


@pytest.mark.parametrize("value", (-1, True, 1.5, 1_786_533_301))
def test_bootstrap_clock_refresh_rejects_unaligned_expiry(value: object) -> None:
    with pytest.raises(ValueError, match="expiry_seconds"):
        bootstrap_clock_refresh_at_ms(value)  # type: ignore[arg-type]
