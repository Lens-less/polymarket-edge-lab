#!/usr/bin/env python3
"""Read-only health check for the BTC TWAP paper-validation service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_relative_value_service import (  # noqa: E402
    evaluate_service_health,
    load_service_config,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return value


def _process_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _free_disk_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


_GUARD_FIELD_ORDER = (
    "paper_only",
    "public_only",
    "new_orders_disabled",
    "orders_submitted",
    "authenticated_endpoints_used",
)


def _guard_value_safe(key: str, value: Any) -> bool:
    if key in {"orders_submitted", "authenticated_endpoints_used"}:
        return type(value) is int and value == 0
    return value is True


def _guard_snapshot(
    document: Mapping[str, Any] | None,
    *,
    require_full_guard: bool,
) -> dict[str, Any] | None:
    if document is None:
        return None
    required_fields = ["paper_only", "new_orders_disabled"]
    if require_full_guard:
        required_fields.extend(
            ["public_only", "orders_submitted", "authenticated_endpoints_used"]
        )
    missing_fields: list[str] = []
    unsafe_fields: list[str] = []
    snapshot: dict[str, Any] = {
        "full_guard_required": require_full_guard,
        "required_fields": required_fields,
    }
    for key in _GUARD_FIELD_ORDER:
        present = key in document
        value = document.get(key) if present else None
        snapshot[key] = value
        if not present:
            if key in required_fields:
                missing_fields.append(key)
            continue
        if not _guard_value_safe(key, value):
            unsafe_fields.append(key)
    snapshot["missing_fields"] = missing_fields
    snapshot["unsafe_fields"] = unsafe_fields
    snapshot["all_required_safe"] = not missing_fields and not unsafe_fields
    return snapshot


def _summary_requires_full_guard(summary: Mapping[str, Any]) -> bool:
    schema_version = summary.get("schema_version")
    return isinstance(schema_version, str) and schema_version.endswith(".v2")


def _summary_snapshot(path_value: Any) -> Mapping[str, Any] | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value).expanduser().resolve()
    summary = _read_object(path)
    unsigned = dict(summary)
    claimed = unsigned.pop("summary_sha256", None)
    actual = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    if claimed != actual:
        raise ValueError("validation summary hash mismatch")
    guards = _guard_snapshot(
        summary,
        require_full_guard=_summary_requires_full_guard(summary),
    )
    evidence = summary.get("evidence")
    gaps = summary.get("promotion_gaps")
    return {
        "path": str(path),
        "schema_version": summary.get("schema_version"),
        "summary_sha256": claimed,
        "paper_only": summary.get("paper_only"),
        "public_only": summary.get("public_only"),
        "new_orders_disabled": summary.get("new_orders_disabled"),
        "orders_submitted": summary.get("orders_submitted"),
        "authenticated_endpoints_used": summary.get("authenticated_endpoints_used"),
        "guards": guards,
        "classification": summary.get("classification"),
        "qualified_net_pnl": summary.get("qualified_net_pnl"),
        "development_shadow": (
            summary.get("development_shadow")
            if isinstance(summary.get("development_shadow"), Mapping)
            else None
        ),
        "evidence": evidence if isinstance(evidence, Mapping) else None,
        "promotion_gaps": gaps if isinstance(gaps, Mapping) else None,
    }


def _integer_field(document: Mapping[str, Any] | None, key: str) -> int | None:
    if document is None:
        return None
    value = document.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _scalar_field(document: Mapping[str, Any] | None, key: str) -> Any:
    if document is None:
        return None
    return document.get(key)


def _clock_policy_limits(preregistration_path: Path) -> tuple[int, int]:
    preregistration = _read_object(preregistration_path)
    frozen_strategy = preregistration.get("frozen_strategy")
    clock_sync = (
        frozen_strategy.get("clock_sync")
        if isinstance(frozen_strategy, Mapping)
        else None
    )
    if not isinstance(clock_sync, Mapping):
        runtime = preregistration.get("runtime_capture_contract")
        if (
            preregistration.get("schema_version")
            == "btc-5m-15m-edge-readiness-preregistration.v08-draft"
            and isinstance(runtime, Mapping)
            and isinstance(runtime.get("clock_sync_source"), str)
            and runtime.get("clock_sync_source").strip()
        ):
            # v0.8 froze the clock source, not the v0.6 measurement envelope.
            # Keep the same numeric envelope so capture health stays fail-closed.
            return 100, 1200 * 1_000
        raise ValueError("preregistration has no frozen clock-sync policy")
    uncertainty_ms = _integer_field(clock_sync, "maximum_measurement_uncertainty_ms")
    maximum_age_seconds = _integer_field(clock_sync, "maximum_measurement_age_seconds")
    if uncertainty_ms is None or uncertainty_ms < 0:
        raise ValueError("preregistration clock uncertainty limit is invalid")
    if maximum_age_seconds is None or maximum_age_seconds < 0:
        raise ValueError("preregistration clock age limit is invalid")
    return uncertainty_ms, maximum_age_seconds * 1_000


def _monitoring_snapshot(
    health: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    summary: Mapping[str, Any] | None,
    *,
    clock_policy_limits: tuple[int, int] | None = None,
) -> dict[str, Any]:
    service_guards = _guard_snapshot(status, require_full_guard=True)
    summary_guards = (
        summary.get("guards")
        if isinstance(summary, Mapping) and isinstance(summary.get("guards"), Mapping)
        else None
    )
    evidence = summary.get("evidence") if isinstance(summary, Mapping) else None
    evidence = evidence if isinstance(evidence, Mapping) else None
    shadow = summary.get("development_shadow") if isinstance(summary, Mapping) else None
    shadow = shadow if isinstance(shadow, Mapping) else None
    clock = (
        evidence.get("clock_sync")
        if isinstance(evidence, Mapping)
        and isinstance(evidence.get("clock_sync"), Mapping)
        else None
    )
    required_decisions = _integer_field(clock, "required_decisions")
    valid_decisions = _integer_field(clock, "valid_decisions")
    invalid_decisions = _integer_field(clock, "invalid_decisions")
    all_required_valid = None
    if required_decisions is not None and valid_decisions is not None:
        all_required_valid = required_decisions == valid_decisions
    maximum_measurement_age_ms = _integer_field(clock, "maximum_measurement_age_ms")
    maximum_uncertainty_ms = _integer_field(clock, "maximum_uncertainty_ms")
    clock_policy_violations: list[str] = []
    clock_policy_passed: bool | None = None
    uncertainty_limit_ms: int | None = None
    measurement_age_limit_ms: int | None = None
    if clock is not None and clock_policy_limits is not None:
        uncertainty_limit_ms, measurement_age_limit_ms = clock_policy_limits
        if any(
            value is None
            for value in (required_decisions, valid_decisions, invalid_decisions)
        ):
            clock_policy_violations.append("clock_evidence_incomplete")
        elif required_decisions != valid_decisions + invalid_decisions:
            clock_policy_violations.append("clock_decision_counts_inconsistent")
        if invalid_decisions not in (None, 0):
            clock_policy_violations.append("invalid_clock_decisions_observed")
        if required_decisions not in (None, 0) and (
            maximum_uncertainty_ms is None or maximum_measurement_age_ms is None
        ):
            clock_policy_violations.append("clock_measurement_limits_unverifiable")
        if (
            maximum_uncertainty_ms is not None
            and maximum_uncertainty_ms > uncertainty_limit_ms
        ):
            clock_policy_violations.append("clock_uncertainty_limit_exceeded")
        if (
            maximum_measurement_age_ms is not None
            and maximum_measurement_age_ms > measurement_age_limit_ms
        ):
            clock_policy_violations.append("clock_measurement_age_limit_exceeded")
        clock_policy_passed = not clock_policy_violations
    return {
        "service": {
            "healthy": health.get("healthy"),
            "phase": health.get("phase"),
            "failures": health.get("failures"),
            "heartbeat_age_seconds": health.get("heartbeat_age_seconds"),
            "pid": health.get("pid"),
            "process_alive": health.get("process_alive"),
            "current_expiry_seconds": health.get("current_expiry_seconds"),
            "completed_report_count": health.get("completed_report_count"),
            "latest_classification": health.get("latest_classification"),
            "last_error": health.get("last_error"),
            "status_hash_valid": health.get("status_hash_valid"),
            "paper_only": (
                service_guards.get("paper_only")
                if isinstance(service_guards, Mapping)
                else None
            ),
            "public_only": (
                service_guards.get("public_only")
                if isinstance(service_guards, Mapping)
                else None
            ),
            "new_orders_disabled": (
                service_guards.get("new_orders_disabled")
                if isinstance(service_guards, Mapping)
                else None
            ),
            "orders_submitted": (
                service_guards.get("orders_submitted")
                if isinstance(service_guards, Mapping)
                else None
            ),
            "authenticated_endpoints_used": (
                service_guards.get("authenticated_endpoints_used")
                if isinstance(service_guards, Mapping)
                else None
            ),
            "paper_only_guard_valid": (
                service_guards.get("all_required_safe")
                if isinstance(service_guards, Mapping)
                else health.get("paper_only_guard_valid")
            ),
            "guard_missing_fields": (
                service_guards.get("missing_fields")
                if isinstance(service_guards, Mapping)
                else None
            ),
            "guard_unsafe_fields": (
                service_guards.get("unsafe_fields")
                if isinstance(service_guards, Mapping)
                else None
            ),
        },
        "summary_guards": summary_guards,
        "connections": (
            {
                "clob": {
                    "errors": _integer_field(evidence, "clob_websocket_errors"),
                    "reconnects": _integer_field(evidence, "clob_reconnects"),
                    "disconnects": _integer_field(evidence, "clob_disconnects"),
                },
                "rtds": {
                    "errors": _integer_field(evidence, "rtds_websocket_errors"),
                    "reconnects": _integer_field(evidence, "rtds_reconnects"),
                    "disconnects": _integer_field(evidence, "rtds_disconnects"),
                },
                "redundancy": {
                    "recorder_leg_failures": _integer_field(
                        evidence,
                        "recorder_leg_failures",
                    ),
                    "degraded_capture_cycles": _integer_field(
                        evidence,
                        "degraded_capture_cycles",
                    ),
                    "minimum_clob_legs": _integer_field(
                        evidence,
                        "minimum_clob_recorder_legs",
                    ),
                    "minimum_rtds_legs": _integer_field(
                        evidence,
                        "minimum_rtds_recorder_legs",
                    ),
                },
            }
            if evidence is not None
            else None
        ),
        "clock": (
            {
                "required_decisions": required_decisions,
                "valid_decisions": valid_decisions,
                "invalid_decisions": invalid_decisions,
                "all_required_decisions_valid": all_required_valid,
                "maximum_measurement_age_ms": maximum_measurement_age_ms,
                "maximum_uncertainty_ms": maximum_uncertainty_ms,
                "latest_offset_seconds": _scalar_field(
                    clock,
                    "latest_offset_seconds",
                ),
                "frozen_maximum_measurement_age_ms": measurement_age_limit_ms,
                "frozen_maximum_uncertainty_ms": uncertainty_limit_ms,
                "policy_passed": clock_policy_passed,
                "policy_violations": clock_policy_violations,
            }
            if clock is not None
            else None
        ),
        "paper": (
            {
                "decision_evaluations": _integer_field(
                    evidence,
                    "paper_decision_evaluations",
                ),
                "no_trade_decisions": _integer_field(
                    evidence,
                    "paper_no_trade_decisions",
                ),
                "explainable_simulated_trades": _integer_field(
                    evidence,
                    "explainable_simulated_trades",
                ),
                "explainable_fills": _integer_field(evidence, "explainable_fills"),
                "observed_explainable_net_pnl": _scalar_field(
                    evidence,
                    "observed_explainable_net_pnl",
                ),
                "qualified_net_pnl": _scalar_field(summary, "qualified_net_pnl"),
            }
            if evidence is not None
            else None
        ),
        "shadow": (
            {
                "decisions": _integer_field(shadow, "decisions"),
                "trades": _integer_field(shadow, "trades"),
                "fills": _integer_field(shadow, "fills"),
                "settled_trades": _integer_field(shadow, "settled_trades"),
                "pending_trades": _integer_field(shadow, "pending_trades"),
                "net_pnl": _scalar_field(shadow, "net_pnl"),
                "average_net_pnl": _scalar_field(shadow, "average_net_pnl"),
                "winning_trades": _integer_field(shadow, "winning_trades"),
                "losing_trades": _integer_field(shadow, "losing_trades"),
                "breakeven_trades": _integer_field(shadow, "breakeven_trades"),
                "win_rate": _scalar_field(shadow, "win_rate"),
                "profit_factor": _scalar_field(shadow, "profit_factor"),
                "execution_diagnostics": (
                    shadow.get("execution_diagnostics")
                    if isinstance(shadow.get("execution_diagnostics"), Mapping)
                    else None
                ),
            }
            if shadow is not None
            else None
        ),
        "promotion_gaps": (
            summary.get("promotion_gaps") if isinstance(summary, Mapping) else None
        ),
        "evidence_healthy": clock_policy_passed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--maximum-heartbeat-age-seconds", type=int, default=90)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        config = load_service_config(args.config)
        status_path = config.data_root / "service" / "status.json"
        status = _read_object(status_path)
        health = evaluate_service_health(
            status,
            now=now,
            process_alive=_process_alive(status.get("pid")),
            free_disk_bytes=_free_disk_bytes(config.data_root),
            minimum_free_disk_bytes=config.minimum_free_disk_bytes,
            maximum_heartbeat_age_seconds=args.maximum_heartbeat_age_seconds,
        )
        clock_policy_limits = _clock_policy_limits(config.preregistration_path)
        try:
            summary = _summary_snapshot(status.get("latest_summary_path"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            summary = None
            health["healthy"] = False
            health["failures"].append("validation_summary_invalid")
            health["summary_error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        health["summary"] = summary
        runtime_healthy = health["healthy"]
        monitoring = _monitoring_snapshot(
            health,
            status,
            summary,
            clock_policy_limits=clock_policy_limits,
        )
        if monitoring["service"]["paper_only_guard_valid"] is False:
            health["healthy"] = False
            if "paper_only_guard_invalid" not in health["failures"]:
                health["failures"].append("paper_only_guard_invalid")
        if isinstance(monitoring.get("summary_guards"), Mapping) and (
            monitoring["summary_guards"].get("all_required_safe") is False
        ):
            health["healthy"] = False
            if "validation_summary_guard_invalid" not in health["failures"]:
                health["failures"].append("validation_summary_guard_invalid")
        if monitoring["evidence_healthy"] is False:
            health["healthy"] = False
            if "clock_evidence_policy_violated" not in health["failures"]:
                health["failures"].append("clock_evidence_policy_violated")
        monitoring["service"]["runtime_healthy"] = runtime_healthy
        monitoring["service"]["healthy"] = health["healthy"]
        health["monitoring"] = monitoring
        health["checked_at"] = now.isoformat().replace("+00:00", "Z")
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        health = {
            "schema_version": "btc-twap-relative-value-service-health.v1",
            "healthy": False,
            "failures": ["health_check_input_invalid"],
            "checked_at": now.isoformat().replace("+00:00", "Z"),
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "monitoring": _monitoring_snapshot({}, None, None),
        }
    print(json.dumps(health, indent=2, sort_keys=True))
    return 0 if health["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
