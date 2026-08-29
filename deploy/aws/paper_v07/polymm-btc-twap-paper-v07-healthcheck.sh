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

from src.edge_lab.btc_twap_relative_value_v07_health import evaluate_shadow_health

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
performance_service = systemctl_show("polymm-btc-twap-paper-v07-performance.service")
health_timer = systemctl_show("polymm-btc-twap-paper-v07-health.timer")
source_acl_service = systemctl_show("polymm-btc-twap-paper-v07-source-acl.service")
free_bytes = shutil.disk_usage(Path(config["data_root"])).free
shadow_config_validation = subprocess.run(
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
source_runtime_validation = subprocess.run(
    [
        "/opt/poly-mm-v07/.venv/bin/python",
        "/opt/poly-mm-v07/scripts/run_btc_twap_relative_value_v07_shadow.py",
        "--config",
        "/opt/poly-mm-v07/research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/SERVICE_CONFIG.json",
        "--validate-only",
        "--check-source",
    ],
    check=False,
    capture_output=True,
    text=True,
)

payload = evaluate_shadow_health(
    config=config,
    status=status,
    now=now,
    shadow_config_returncode=shadow_config_validation.returncode,
    source_runtime_returncode=source_runtime_validation.returncode,
    performance_timer=performance_timer,
    performance_service=performance_service,
    health_timer=health_timer,
    source_acl_service=source_acl_service,
    source_active=source_active,
    free_bytes=free_bytes,
)
payload.update({
    "config_path": str(Path("/opt/poly-mm-v07/research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/SERVICE_CONFIG.json")),
    "status_path": str(status_path),
    "shadow_config_stderr": shadow_config_validation.stderr[-1000:],
    "source_runtime_stderr": source_runtime_validation.stderr[-1000:],
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
    "source_rejected_recorder_leg_failure_count": (
        None
        if status is None
        else status.get("source_rejected_recorder_leg_failure_count")
    ),
    "selection_denominator_count": (
        None if status is None else status.get("selection_denominator_count")
    ),
    "cohort_admission_count": (
        None if status is None else status.get("cohort_admission_count")
    ),
})
print(json.dumps(payload, indent=2, sort_keys=True))
PY

chmod 0640 "${TMP_PATH}"
mv -f "${TMP_PATH}" "${OUTPUT_PATH}"
cp "${OUTPUT_PATH}" "${HISTORY_TMP}"
chmod 0640 "${HISTORY_TMP}"
mv -f "${HISTORY_TMP}" "${HISTORY_PATH}"

/opt/poly-mm-v07/.venv/bin/python - "${OUTPUT_PATH}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if payload.get("healthy") is True else 1)
PY
