"""Causality tests for the BTC TWAP pilot-report builder."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scripts.build_btc_twap_relative_value_pilot_report import (
    _assert_frozen_decision_tau,
    _book_replay_coverage,
    _capture_runtime_health,
    _combined_series,
    _exact,
    _latest_before,
    _resample_one_second,
    _resolution_event_validation,
)

D = Decimal


def _write_rtds_record(
    root: Path,
    *,
    record_id: str,
    topic: str,
    timestamp_ms: int,
    value: Decimal,
    received_at_ms: int,
    symbol: str = "btcusdt",
) -> None:
    raw_dir = root / "raw" / "rtds_ws"
    raw_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_id": record_id,
        "received_at": (
            datetime.fromtimestamp(received_at_ms / 1_000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "payload": {
            "payload": {
                "topic": topic,
                "payload": {
                    "symbol": symbol,
                    "timestamp": timestamp_ms,
                    "value": str(value),
                },
            }
        },
    }
    with (raw_dir / "records.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def test_capture_runtime_health_preserves_redundancy_evidence(tmp_path: Path) -> None:
    summary = {
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
        "recorder_leg_count": 2,
        "recorder_leg_failures": [{"code": "recorder_leg_failed"}],
        "websocket_redundancy": {"clob_market_ws": 2, "rtds_ws": 2},
        "capture_error": None,
    }
    (tmp_path / "capture-summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )

    assert _capture_runtime_health(tmp_path) == {
        "recorder_leg_count": 2,
        "recorder_leg_failures": [{"code": "recorder_leg_failed"}],
        "websocket_redundancy": {"clob_market_ws": 2, "rtds_ws": 2},
        "capture_error": None,
    }


def test_predictor_window_ends_at_latest_causally_received_complete_second() -> None:
    decision_at_ms = 500_000
    series = tuple(
        (
            source_second * 1_000,
            source_second * 1_000 + 2_000,
            D(source_second),
        )
        for source_second in range(198, 498)
    )

    resampled = _resample_one_second(
        series,
        decision_at_ms=decision_at_ms,
        seconds=300,
    )

    assert len(resampled) == 300
    assert resampled[0].timestamp_ms == 201_000
    assert resampled[-1].timestamp_ms == 500_000


def test_predictor_resampling_uses_causal_grid_not_source_second_labels() -> None:
    series = (
        (1_999, 1_999, D("100")),
        (2_001, 2_001, D("110")),
        (3_001, 3_001, D("120")),
    )

    resampled = _resample_one_second(
        series,
        decision_at_ms=4_000,
        seconds=3,
    )

    assert [(item.timestamp_ms, item.price) for item in resampled] == [
        (2_000, D("100")),
        (3_000, D("110")),
        (4_000, D("120")),
    ]


def test_predictor_window_rejects_gaps_staleness_and_future_delivery() -> None:
    decision_at_ms = 500_000
    future_delivered = tuple(
        (second * 1_000, decision_at_ms + 1, D(second)) for second in range(198, 498)
    )
    stale = tuple(
        (second * 1_000, second * 1_000, D(second)) for second in range(190, 490)
    )
    gap = tuple(
        (second * 1_000, second * 1_000, D(second))
        for second in range(198, 498)
        if not 350 <= second <= 356
    )

    assert not _resample_one_second(future_delivered, decision_at_ms=decision_at_ms)
    assert not _resample_one_second(stale, decision_at_ms=decision_at_ms)
    assert not _resample_one_second(gap, decision_at_ms=decision_at_ms)


def test_predictor_window_allows_late_start_but_not_internal_gaps() -> None:
    decision_at_ms = 500_000
    late_start = tuple(
        (second * 1_000, second * 1_000, D(second)) for second in range(231, 500)
    )

    resampled = _resample_one_second(late_start, decision_at_ms=decision_at_ms)

    assert len(resampled) == 270
    assert resampled[0].timestamp_ms == 231_000
    assert resampled[-1].timestamp_ms == 500_000


def test_predictor_history_root_cannot_bridge_a_rollover_gap(
    tmp_path: Path,
) -> None:
    history_root = tmp_path / "history"
    current_root = tmp_path / "current"
    decision_at_ms = 500_000
    for second in range(201, 469):
        _write_rtds_record(
            history_root,
            record_id=f"history-{second}",
            topic="crypto_prices",
            timestamp_ms=second * 1_000,
            value=D(second),
            received_at_ms=second * 1_000,
        )
    # A real service rollover gap must not be treated as continuous history.
    for second in range(480, 501):
        _write_rtds_record(
            current_root,
            record_id=f"current-{second}",
            topic="crypto_prices",
            timestamp_ms=second * 1_000,
            value=D(second),
            received_at_ms=second * 1_000,
        )

    current_only = _combined_series(
        (current_root,),
        topic="crypto_prices",
        symbol="btcusdt",
    )
    combined = _combined_series(
        (history_root, current_root),
        topic="crypto_prices",
        symbol="btcusdt",
    )

    current_resampled = _resample_one_second(
        current_only,
        decision_at_ms=decision_at_ms,
    )
    combined_resampled = _resample_one_second(
        combined,
        decision_at_ms=decision_at_ms,
    )

    assert len(current_resampled) == 21
    assert combined_resampled == ()


def test_latest_before_requires_both_source_and_delivery_before_decision() -> None:
    series = (
        (100_000, 101_000, D("100")),
        (101_000, 105_000, D("101")),
    )

    assert _latest_before(series, 103_000) == (100_000, 101_000, D("100"))


def test_exact_opening_boundary_must_be_delivered_before_decision() -> None:
    series = ((100_000, 105_000, D("100")),)

    assert _exact(series, 100_000, available_by_ms=104_999) is None
    assert _exact(series, 100_000, available_by_ms=105_000) == D("100")


def test_decision_tau_must_be_one_of_the_frozen_ticks() -> None:
    preregistration = {
        "frozen_strategy": {
            "tau_min_seconds": 45,
            "tau_max_seconds": 240,
            "decision_tau_seconds": [240, 180, 120, 60],
        }
    }

    _assert_frozen_decision_tau(preregistration, 120)

    import pytest

    with pytest.raises(ValueError, match="frozen decision ticks"):
        _assert_frozen_decision_tau(preregistration, 90)


def test_resolution_event_must_match_condition_winning_token_and_outcome() -> None:
    target = {
        "condition_id": "0xabc",
        "up_token_id": "up",
        "down_token_id": "down",
    }

    assert _resolution_event_validation(
        {
            "condition_id": "0xabc",
            "winning_asset_id": "down",
            "winning_outcome": "Down",
        },
        target,
    ) == (True, None)
    assert _resolution_event_validation(
        {
            "condition_id": "0xdef",
            "winning_asset_id": "down",
            "winning_outcome": "Down",
        },
        target,
    ) == (False, "resolution_condition_mismatch")
    assert _resolution_event_validation(
        {
            "condition_id": "0xabc",
            "winning_asset_id": "up",
            "winning_outcome": "Down",
        },
        target,
    ) == (False, "resolution_outcome_token_mismatch")


def test_book_coverage_requires_causal_signal_and_post_delay_snapshot() -> None:
    observations = (
        {
            "token_id": "a",
            "source_at_ms": 99_500,
            "received_at_ms": 99_700,
            "bid_levels": 5,
            "ask_levels": 5,
            "source_event_id": "a-signal",
        },
        {
            "token_id": "a",
            "source_at_ms": 100_300,
            "received_at_ms": 100_350,
            "bid_levels": 5,
            "ask_levels": 5,
            "source_event_id": "a-execution",
        },
        {
            "token_id": "b",
            "source_at_ms": 99_600,
            "received_at_ms": 99_800,
            "bid_levels": 5,
            "ask_levels": 5,
            "source_event_id": "b-signal",
        },
        {
            "token_id": "b",
            "source_at_ms": 100_250,
            "received_at_ms": 100_400,
            "bid_levels": 5,
            "ask_levels": 5,
            "source_event_id": "b-execution",
        },
        {
            "token_id": "b",
            "source_at_ms": 99_900,
            "received_at_ms": 100_100,
            "bid_levels": 5,
            "ask_levels": 5,
            "source_event_id": "future-delivery",
        },
    )

    coverage = _book_replay_coverage(
        observations,
        token_ids=("a", "b"),
        decision_at_ms=100_000,
        taker_delay_ms=250,
        max_book_age_ms=750,
    )

    assert coverage["complete_four_token_signal_surface"] is True
    assert coverage["complete_four_token_delayed_execution_surface"] is True
    assert coverage["tokens"]["b"]["signal_source_event_id"] == "b-signal"
    assert coverage["tokens"]["a"]["execution_source_event_id"] == "a-execution"

    incomplete = _book_replay_coverage(
        observations,
        token_ids=("a", "b", "c"),
        decision_at_ms=100_000,
        taker_delay_ms=250,
        max_book_age_ms=750,
    )
    assert incomplete["complete_four_token_signal_surface"] is False
    assert incomplete["complete_four_token_delayed_execution_surface"] is False
