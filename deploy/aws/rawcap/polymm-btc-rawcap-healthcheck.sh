#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="/var/lib/poly-mm-rawcap/monitor"
HISTORY_DIR="${OUTPUT_DIR}/history"
OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"
SNAPSHOT_AT="$(date -u +%Y%m%dT%H%M%S.%NZ)"
install -d -m 0755 "${OUTPUT_DIR}" "${HISTORY_DIR}"
TMP_PATH="$(mktemp "${OUTPUT_DIR}/health-latest.json.tmp.XXXXXX")"
HISTORY_TMP="$(mktemp "${HISTORY_DIR}/health-${SNAPSHOT_AT}-XXXXXX.json.tmp")"

/opt/poly-mm-rawcap/.venv/bin/python - <<'PY' >"${TMP_PATH}"
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

status_path = Path("/var/lib/poly-mm-rawcap/status/status.json")
try:
    status = json.loads(status_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    status = {}
now = datetime.now(timezone.utc)
heartbeat = status.get("heartbeat_at")
try:
    parsed = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
    heartbeat_age = (now - parsed.astimezone(timezone.utc)).total_seconds()
except (TypeError, ValueError):
    heartbeat_age = None
free_disk = shutil.disk_usage("/var/lib/poly-mm-rawcap").free
failures = []
if status.get("phase") in {None, "error_wait", "stopped"}:
    failures.append("collector_phase_unhealthy")
if heartbeat_age is None or heartbeat_age > 90:
    failures.append("heartbeat_stale")
if free_disk < 12 * 1024**3:
    failures.append("disk_reserve_breached")
for field, expected in {
    "paper_only": True,
    "public_only": True,
    "new_orders_disabled": True,
    "authenticated_endpoints_used": 0,
    "orders_submitted": 0,
}.items():
    if status.get(field) != expected:
        failures.append(f"guard_failed:{field}")
payload = {
    "schema_version": "btc-regime-agnostic-collector-health.v1",
    "checked_at": now.isoformat().replace("+00:00", "Z"),
    "healthy": not failures,
    "failures": failures,
    "phase": status.get("phase"),
    "heartbeat_at": heartbeat,
    "heartbeat_age_seconds": heartbeat_age,
    "completed_capture_count": status.get("completed_capture_count"),
    "latest_classification": status.get("latest_classification"),
    "free_disk_bytes": free_disk,
    "minimum_free_disk_bytes": 12 * 1024**3,
}
print(json.dumps(payload, sort_keys=True))
PY

cp "${TMP_PATH}" "${HISTORY_TMP}"
chmod 0640 "${TMP_PATH}" "${HISTORY_TMP}"
mv -f "${TMP_PATH}" "${OUTPUT_PATH}"
mv -f "${HISTORY_TMP}" "${HISTORY_TMP%.tmp}"
