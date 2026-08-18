"""Pure health evaluation for the deployed v0.7 read-only shadow track.

The deployment wrapper is responsible for reading files and systemd state.  This
module owns the fail-closed policy so stale/invalid states can be regression
tested without executing a shell heredoc or contacting the host.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_HEALTHY_PHASES = frozenset({"warming_up", "refreshing", "ok"})


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _non_negative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _unit_active(value: Mapping[str, Any]) -> bool:
    return value.get("ActiveState") == "active"


def _unit_succeeded(value: Mapping[str, Any]) -> bool:
    return value.get("Result") == "success" and value.get("ExecMainStatus") == "0"


def evaluate_shadow_health(
    *,
    config: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    now: datetime,
    shadow_config_returncode: int,
    source_runtime_returncode: int,
    performance_timer: Mapping[str, Any],
    health_timer: Mapping[str, Any],
    source_acl_service: Mapping[str, Any],
    source_active: bool,
    free_bytes: int,
) -> dict[str, Any]:
    """Evaluate one v0.7 health snapshot using explicit observations.

    Static shadow configuration and the mutable source runtime are deliberately
    separate inputs.  A stale source can therefore no longer block the shadow
    process before it writes a fresh, specific failure heartbeat.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if isinstance(shadow_config_returncode, bool) or not isinstance(
        shadow_config_returncode, int
    ):
        raise TypeError("shadow_config_returncode must be an integer")
    if isinstance(source_runtime_returncode, bool) or not isinstance(
        source_runtime_returncode, int
    ):
        raise TypeError("source_runtime_returncode must be an integer")
    if isinstance(source_active, bool) is False:
        raise TypeError("source_active must be bool")
    if isinstance(free_bytes, bool) or not isinstance(free_bytes, int):
        raise TypeError("free_bytes must be an integer")

    failures: list[str] = []
    warnings: list[str] = []
    if shadow_config_returncode != 0:
        failures.append("shadow_config_invalid")
    if source_runtime_returncode != 0:
        failures.append("source_runtime_invalid")
    if not _unit_active(performance_timer):
        failures.append("performance_timer_inactive")
    if not _unit_active(health_timer):
        failures.append("health_timer_inactive")
    if not _unit_succeeded(source_acl_service):
        failures.append("source_acl_service_failed")

    heartbeat_age_seconds: float | None = None
    if status is None:
        failures.append("status_missing_or_invalid")
    else:
        heartbeat = _parse_utc(status.get("heartbeat_at"))
        if heartbeat is not None:
            heartbeat_age_seconds = (
                now.astimezone(timezone.utc) - heartbeat
            ).total_seconds()
        if status.get("mode") != config.get("mode"):
            failures.append("mode_mismatch")
        for key, expected, failure in (
            ("paper_only", True, "paper_only_guard_invalid"),
            ("public_only", True, "public_only_guard_invalid"),
            ("new_orders_disabled", True, "new_orders_disabled_guard_invalid"),
            ("live", False, "live_flag_invalid"),
            ("orders_submitted", 0, "orders_submitted_nonzero"),
            (
                "authenticated_endpoints_used",
                0,
                "authenticated_endpoints_used_nonzero",
            ),
        ):
            if status.get(key) != expected:
                failures.append(failure)
        if status.get("phase") not in _HEALTHY_PHASES:
            failures.append("status_phase_unhealthy")

        accounting_keys = (
            "source_post_cutoff_attempt_count",
            "source_finalized_clean_count",
            "source_rejected_count",
            "source_rejected_capture_error_count",
            "source_rejected_recorder_leg_failure_count",
            "selection_denominator_count",
            "cohort_admission_count",
            "case_count",
        )
        accounting = {key: status.get(key) for key in accounting_keys}
        if any(not _non_negative_int(value) for value in accounting.values()):
            failures.append("source_accounting_invalid")
        elif (
            accounting["source_post_cutoff_attempt_count"]
            != accounting["source_finalized_clean_count"]
            + accounting["source_rejected_count"]
            or accounting["source_rejected_count"]
            != accounting["source_rejected_capture_error_count"]
            + accounting["source_rejected_recorder_leg_failure_count"]
            or accounting["selection_denominator_count"]
            != accounting["source_finalized_clean_count"]
            or accounting["cohort_admission_count"] != accounting["case_count"]
            or status.get("data_quality_complete")
            is not (accounting["source_rejected_count"] == 0)
            or not isinstance(status.get("latest_rejected_attempts"), list)
        ):
            failures.append("source_accounting_inconsistent")
        else:
            if accounting["source_rejected_capture_error_count"] > 0:
                warnings.append("source_attempt_capture_error_present")
            if accounting["source_rejected_recorder_leg_failure_count"] > 0:
                warnings.append("source_attempt_recorder_leg_failure_present")
            if (
                accounting["source_post_cutoff_attempt_count"] > 0
                and accounting["source_finalized_clean_count"] == 0
            ):
                warnings.append("no_clean_post_cutoff_source_attempts_yet")

        if status.get("qualified_net_pnl") is not None:
            failures.append("qualified_pnl_must_be_null")
        if (
            status.get("true_edge") is not False
            or status.get("true_edge_gate") is not False
        ):
            failures.append("true_edge_guard_invalid")
        if (
            status.get("positive_100_trade_check") is not False
            or status.get("positive_100_trade_pnl_check") is not False
        ):
            failures.append("positive_100_trade_guard_invalid")
        if (
            status.get("prelabel_lock_journal") is not False
            or status.get("prelabel") is not False
        ):
            failures.append("prelabel_guard_invalid")

        maximum_age = config.get("maximum_status_age_seconds")
        if (
            not _non_negative_int(maximum_age)
            or heartbeat_age_seconds is None
            or heartbeat_age_seconds < -60
            or heartbeat_age_seconds > maximum_age
        ):
            failures.append("status_stale")

    if not source_active:
        failures.append("source_v06_inactive")
    minimum_free = config.get("minimum_free_bytes")
    if not _non_negative_int(minimum_free) or free_bytes < minimum_free:
        failures.append("disk_below_minimum")

    return {
        "schema_version": "btc-twap-relative-value-v07-shadow-health.v2",
        "checked_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "healthy": not failures,
        "failures": failures,
        "warnings": warnings,
        "mode": config.get("mode"),
        "status": None if status is None else dict(status),
        "status_freshness_seconds": heartbeat_age_seconds,
        "performance_timer": dict(performance_timer),
        "health_timer": dict(health_timer),
        "source_acl_service": dict(source_acl_service),
        "source_v06_active": source_active,
        "free_bytes": free_bytes,
        "minimum_free_bytes": minimum_free,
        "shadow_config_returncode": shadow_config_returncode,
        "source_runtime_returncode": source_runtime_returncode,
    }


__all__ = ["evaluate_shadow_health"]
