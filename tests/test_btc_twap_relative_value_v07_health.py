from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.edge_lab.btc_twap_relative_value_v07_health import evaluate_shadow_health


def _status(now: datetime, *, phase: str = "ok", rejected: int = 0) -> dict[str, Any]:
    return {
        "schema_version": "btc-twap-relative-value-v07-shadow-status.v1",
        "heartbeat_at": now.isoformat().replace("+00:00", "Z"),
        "mode": "prospective_actual_market_counterfactual_shadow",
        "phase": phase,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "live": False,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
        "source_post_cutoff_attempt_count": 4 + rejected,
        "source_finalized_clean_count": 4,
        "source_rejected_count": rejected,
        "source_rejected_capture_error_count": rejected,
        "source_rejected_recorder_leg_failure_count": 0,
        "selection_denominator_count": 4,
        "cohort_admission_count": 4,
        "case_count": 4,
        "data_quality_complete": rejected == 0,
        "latest_rejected_attempts": [],
        "qualified_net_pnl": None,
        "true_edge": False,
        "true_edge_gate": False,
        "positive_100_trade_check": False,
        "positive_100_trade_pnl_check": False,
        "prelabel_lock_journal": False,
        "prelabel": False,
    }


def _evaluate(
    now: datetime,
    *,
    status: dict[str, Any] | None = None,
    shadow_config_returncode: int = 0,
    source_runtime_returncode: int = 0,
    performance_service: dict[str, str] | None = None,
) -> dict[str, Any]:
    successful_unit = {
        "ActiveState": "active",
        "Result": "success",
        "ExecMainStatus": "0",
    }
    return evaluate_shadow_health(
        config={
            "mode": "prospective_actual_market_counterfactual_shadow",
            "maximum_status_age_seconds": 2700,
            "minimum_free_bytes": 1024,
        },
        status=_status(now) if status is None else status,
        now=now,
        shadow_config_returncode=shadow_config_returncode,
        source_runtime_returncode=source_runtime_returncode,
        performance_timer=successful_unit,
        performance_service=(
            successful_unit if performance_service is None else performance_service
        ),
        health_timer=successful_unit,
        source_acl_service=successful_unit,
        source_active=True,
        free_bytes=2048,
    )


def test_refreshing_status_is_healthy_when_heartbeat_is_fresh() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    result = _evaluate(now, status=_status(now, phase="refreshing"))

    assert result["healthy"] is True
    assert result["failures"] == []
    assert result["status_freshness_seconds"] == 0


def test_static_config_and_source_runtime_failures_are_distinct() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    config_failure = _evaluate(now, shadow_config_returncode=1)
    source_failure = _evaluate(now, source_runtime_returncode=1)

    assert config_failure["failures"] == ["shadow_config_invalid"]
    assert source_failure["failures"] == ["source_runtime_invalid"]
    assert "shadow_validate_failed" not in config_failure["failures"]
    assert "shadow_validate_failed" not in source_failure["failures"]


def test_last_performance_service_failure_is_not_hidden_by_active_timer() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    result = _evaluate(
        now,
        performance_service={
            "ActiveState": "failed",
            "Result": "exit-code",
            "ExecMainStatus": "1",
        },
    )

    assert result["healthy"] is False
    assert "performance_service_failed" in result["failures"]


def test_stale_status_and_capture_errors_remain_visible() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    status = _status(now - timedelta(seconds=2701), rejected=2)

    result = _evaluate(now, status=status)

    assert result["healthy"] is False
    assert "status_stale" in result["failures"]
    assert "source_attempt_capture_error_present" in result["warnings"]
