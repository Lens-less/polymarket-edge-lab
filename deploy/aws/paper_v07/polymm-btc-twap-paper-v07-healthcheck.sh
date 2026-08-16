#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="/opt/poly-mm-v07/research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/SERVICE_CONFIG.json"
OUTPUT_DIR="/var/lib/poly-mm-v07/monitor"
HISTORY_DIR="${OUTPUT_DIR}/history"
STATUS_PATH="/var/lib/poly-mm-v07/data/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/service/status.json"
OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"
SNAPSHOT_AT="$(date -u +%Y%m%dT%H%M%S.%NZ)"

install -d -m 0755 "${OUTPUT_DIR}" "${HISTORY_DIR}"
TMP_PATH="$(mktemp "${OUTPUT_DIR}/health-latest.json.tmp.XXXXXX")"
HISTORY_TMP="$(mktemp "${HISTORY_DIR}/health-${SNAPSHOT_AT}-XXXXXX.json.tmp")"
HISTORY_PATH="${HISTORY_TMP%.tmp}"

/opt/poly-mm-v07/.venv/bin/python - <<'PY' >"${TMP_PATH}"
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

config = json.loads(Path("/opt/poly-mm-v07/research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/SERVICE_CONFIG.json").read_text(encoding="utf-8"))
status_path = Path("/var/lib/poly-mm-v07/data/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/service/status.json")
now = datetime.now(timezone.utc)

def read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None

def systemctl_show(unit: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "--property=ActiveState,SubState,UnitFileState,NextElapseUSecRealtime,Result,ExecMainStatus",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = {}
    for raw in completed.stdout.splitlines():
        if "=" in raw:
            key, value = raw.split("=", 1)
            lines[key] = value
    lines["returncode"] = str(completed.returncode)
    return lines

status = read_json(status_path)
heartbeat_age_seconds = None
if isinstance(status, dict) and isinstance(status.get("heartbeat_at"), str):
    try:
        heartbeat = datetime.fromisoformat(str(status["heartbeat_at"]).replace("Z", "+00:00"))
        heartbeat_age_seconds = (now - heartbeat).total_seconds()
    except ValueError:
        heartbeat_age_seconds = None

source_active = subprocess.run(
    ["systemctl", "is-active", "polymm-btc-twap-paper-v06.service"],
    check=False,
    capture_output=True,
    text=True,
).stdout.strip() == "active"

performance_timer = systemctl_show("polymm-btc-twap-paper-v07-performance.timer")
health_timer = systemctl_show("polymm-btc-twap-paper-v07-health.timer")
source_acl_service = systemctl_show("polymm-btc-twap-paper-v07-source-acl.service")
free_bytes = shutil.disk_usage(Path(config["data_root"])).free
shadow_validation = subprocess.run(
    [
        "/opt/poly-mm-v07/.venv/bin/python",
        "/opt/poly-mm-v07/scripts/run_btc_twap_relative_value_v07_shadow.py",
        "--config",
        "/opt/poly-mm-v07/research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/SERVICE_CONFIG.json",
        "--validate-only",
    ],
    check=False,
    capture_output=True,
    text=True,
)
failures: list[str] = []
warnings: list[str] = []

if shadow_validation.returncode != 0:
    failures.append("shadow_validate_failed")
if performance_timer.get("ActiveState") != "active":
    failures.append("performance_timer_inactive")
if health_timer.get("ActiveState") != "active":
    failures.append("health_timer_inactive")
if (
    source_acl_service.get("Result") != "success"
    or source_acl_service.get("ExecMainStatus") != "0"
):
    failures.append("source_acl_service_failed")
if status is None:
    failures.append("status_missing_or_invalid")
else:
    if status.get("mode") != config["mode"]:
        failures.append("mode_mismatch")
    if status.get("paper_only") is not True:
        failures.append("paper_only_guard_invalid")
    if status.get("public_only") is not True:
        failures.append("public_only_guard_invalid")
    if status.get("new_orders_disabled") is not True:
        failures.append("new_orders_disabled_guard_invalid")
    if status.get("live") is not False:
        failures.append("live_flag_invalid")
    if status.get("orders_submitted") != 0:
        failures.append("orders_submitted_nonzero")
    if status.get("authenticated_endpoints_used") != 0:
        failures.append("authenticated_endpoints_used_nonzero")
    if status.get("phase") not in {"warming_up", "ok"}:
        failures.append("status_phase_unhealthy")
    source_accounting = {
        key: status.get(key)
        for key in (
            "source_post_cutoff_attempt_count",
            "source_finalized_clean_count",
            "source_rejected_count",
            "source_rejected_capture_error_count",
            "selection_denominator_count",
            "cohort_admission_count",
            "case_count",
        )
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in source_accounting.values()
    ):
        failures.append("source_accounting_invalid")
    elif (
        source_accounting["source_post_cutoff_attempt_count"]
        != source_accounting["source_finalized_clean_count"]
        + source_accounting["source_rejected_count"]
        or source_accounting["source_rejected_count"]
        != source_accounting["source_rejected_capture_error_count"]
        or source_accounting["selection_denominator_count"]
        != source_accounting["source_finalized_clean_count"]
        or source_accounting["cohort_admission_count"]
        != source_accounting["case_count"]
        or status.get("data_quality_complete")
        is not (source_accounting["source_rejected_count"] == 0)
        or not isinstance(status.get("latest_rejected_attempts"), list)
    ):
        failures.append("source_accounting_inconsistent")
    else:
        if source_accounting["source_rejected_capture_error_count"] > 0:
            warnings.append("source_attempt_capture_error_present")
        if (
            source_accounting["source_post_cutoff_attempt_count"] > 0
            and source_accounting["source_finalized_clean_count"] == 0
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
    if (
        heartbeat_age_seconds is None
        or heartbeat_age_seconds < -60
        or heartbeat_age_seconds > int(config["maximum_status_age_seconds"])
    ):
        failures.append("status_stale")
if not source_active:
    failures.append("source_v06_inactive")
if free_bytes < int(config["minimum_free_bytes"]):
    failures.append("disk_below_minimum")

payload = {
    "schema_version": "btc-twap-relative-value-v07-shadow-health.v1",
    "checked_at": now.isoformat().replace("+00:00", "Z"),
    "healthy": not failures,
    "failures": failures,
    "warnings": warnings,
    "config_path": str(Path("/opt/poly-mm-v07/research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/SERVICE_CONFIG.json")),
    "status_path": str(status_path),
    "mode": config["mode"],
    "status": status,
    "status_freshness_seconds": heartbeat_age_seconds,
    "performance_timer": performance_timer,
    "health_timer": health_timer,
    "source_acl_service": source_acl_service,
    "source_v06_active": source_active,
    "free_bytes": free_bytes,
    "minimum_free_bytes": config["minimum_free_bytes"],
    "shadow_validation_returncode": shadow_validation.returncode,
    "source_post_cutoff_attempt_count": (
        None if status is None else status.get("source_post_cutoff_attempt_count")
    ),
    "source_finalized_clean_count": (
        None if status is None else status.get("source_finalized_clean_count")
    ),
    "source_rejected_capture_error_count": (
        None
        if status is None
        else status.get("source_rejected_capture_error_count")
    ),
    "selection_denominator_count": (
        None if status is None else status.get("selection_denominator_count")
    ),
    "cohort_admission_count": (
        None if status is None else status.get("cohort_admission_count")
    ),
}
print(json.dumps(payload, indent=2, sort_keys=True))
PY

chmod 0640 "${TMP_PATH}"
mv -f "${TMP_PATH}" "${OUTPUT_PATH}"
cp "${OUTPUT_PATH}" "${HISTORY_TMP}"
chmod 0640 "${HISTORY_TMP}"
mv -f "${HISTORY_TMP}" "${HISTORY_PATH}"
