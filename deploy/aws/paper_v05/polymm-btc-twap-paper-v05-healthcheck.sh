#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="/opt/poly-mm-v05/research/btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/SERVICE_CONFIG.json"
OUTPUT_DIR="/var/lib/poly-mm-v05/monitor"
HISTORY_DIR="${OUTPUT_DIR}/history"
OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"
SNAPSHOT_AT="$(date -u +%Y%m%dT%H%M%S.%NZ)"

install -d -m 0755 "${OUTPUT_DIR}" "${HISTORY_DIR}"
TMP_PATH="$(mktemp "${OUTPUT_DIR}/health-latest.json.tmp.XXXXXX")"
HISTORY_TMP="$(mktemp "${HISTORY_DIR}/health-${SNAPSHOT_AT}-XXXXXX.json.tmp")"
HISTORY_PATH="${HISTORY_TMP%.tmp}"

set +e
/opt/poly-mm-v05/.venv/bin/python /opt/poly-mm-v05/scripts/check_btc_twap_relative_value_service.py \
  --config "${CONFIG_PATH}" \
  --maximum-heartbeat-age-seconds 90 >"${TMP_PATH}"
status=$?
set -e

chmod 0640 "${TMP_PATH}"
mv -f "${TMP_PATH}" "${OUTPUT_PATH}"
cp "${OUTPUT_PATH}" "${HISTORY_TMP}"
chmod 0640 "${HISTORY_TMP}"
mv -f "${HISTORY_TMP}" "${HISTORY_PATH}"
if [[ "${status}" -ne 0 ]]; then
  printf '%s\n' "health snapshot is unhealthy; details retained at ${HISTORY_PATH}" >&2
fi
exit 0
