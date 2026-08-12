#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="/opt/poly-mm/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/SERVICE_CONFIG.json"
OUTPUT_DIR="/var/lib/poly-mm/monitor"
HISTORY_DIR="${OUTPUT_DIR}/history"
OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"
TMP_PATH="${OUTPUT_PATH}.tmp.$$"
SNAPSHOT_AT="$(date -u +%Y%m%dT%H%M%SZ)"
HISTORY_TMP="${HISTORY_DIR}/health-${SNAPSHOT_AT}.json.tmp.$$"
HISTORY_PATH="${HISTORY_DIR}/health-${SNAPSHOT_AT}.json"

install -d -m 0755 "${OUTPUT_DIR}" "${HISTORY_DIR}"

set +e
/opt/poly-mm/.venv/bin/python /opt/poly-mm/scripts/check_btc_twap_relative_value_service.py \
  --config "${CONFIG_PATH}" \
  --maximum-heartbeat-age-seconds 90 >"${TMP_PATH}"
status=$?
set -e

mv -f "${TMP_PATH}" "${OUTPUT_PATH}"
cp "${OUTPUT_PATH}" "${HISTORY_TMP}"
mv -f "${HISTORY_TMP}" "${HISTORY_PATH}"
if [[ "${status}" -ne 0 ]]; then
  printf '%s\n' "health snapshot is unhealthy; details retained at ${HISTORY_PATH}" >&2
fi
exit 0
