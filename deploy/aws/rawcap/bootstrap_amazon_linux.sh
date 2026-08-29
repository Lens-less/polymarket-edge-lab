#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Lens-less/polymarket-edge-lab.git}"
DEPLOY_REF="${DEPLOY_REF:?DEPLOY_REF must be the immutable commit to deploy}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/poly-mm-rawcap}"
DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-rawcap}"
SERVICE_USER="${SERVICE_USER:-polybotraw}"
SERVICE_GROUP="${SERVICE_GROUP:-polybotraw}"
DEPLOYMENT_REVISION_PATH="${INSTALL_ROOT}/.deployment-revision"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if ! grep -q '^ID="\?amzn"\?$' /etc/os-release; then
  echo "This bootstrap is frozen for Amazon Linux only." >&2
  exit 1
fi

dnf install -y git python3.11 python3.11-pip zstd
if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${SERVICE_GROUP}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${SERVICE_GROUP}" --create-home \
    --home-dir "/home/${SERVICE_USER}" --shell /sbin/nologin "${SERVICE_USER}"
fi

if [[ ! -e "${INSTALL_ROOT}" ]]; then
  git clone "${REPO_URL}" "${INSTALL_ROOT}"
fi
if [[ -d "${INSTALL_ROOT}/.git" ]]; then
  git -C "${INSTALL_ROOT}" fetch --tags --prune origin
  git -C "${INSTALL_ROOT}" checkout --detach "${DEPLOY_REF}"
  test "$(git -C "${INSTALL_ROOT}" rev-parse HEAD)" = "${DEPLOY_REF}"
  printf '%s\n' "${DEPLOY_REF}" >"${DEPLOYMENT_REVISION_PATH}"
elif [[ -f "${DEPLOYMENT_REVISION_PATH}" ]]; then
  test "$(tr -d '\r\n' <"${DEPLOYMENT_REVISION_PATH}")" = "${DEPLOY_REF}"
else
  echo "${INSTALL_ROOT} is neither a Git checkout nor a verified source archive." >&2
  exit 1
fi

python3.11 -m venv "${INSTALL_ROOT}/.venv"
"${INSTALL_ROOT}/.venv/bin/pip" install --upgrade pip
"${INSTALL_ROOT}/.venv/bin/pip" install -e "${INSTALL_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0755 \
  "${DATA_ROOT}" "${DATA_ROOT}/data" "${DATA_ROOT}/status" \
  "${DATA_ROOT}/monitor" "${DATA_ROOT}/monitor/history"
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/rawcap/polymm-btc-rawcap.service" \
  /etc/systemd/system/polymm-btc-rawcap.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/rawcap/polymm-btc-rawcap-health.service" \
  /etc/systemd/system/polymm-btc-rawcap-health.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/rawcap/polymm-btc-rawcap-health.timer" \
  /etc/systemd/system/polymm-btc-rawcap-health.timer
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/rawcap/polymm-btc-rawcap-maintenance.service" \
  /etc/systemd/system/polymm-btc-rawcap-maintenance.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/rawcap/polymm-btc-rawcap-maintenance.timer" \
  /etc/systemd/system/polymm-btc-rawcap-maintenance.timer
install -o root -g root -m 0755 \
  "${INSTALL_ROOT}/deploy/aws/rawcap/polymm-btc-rawcap-healthcheck.sh" \
  /usr/local/bin/polymm-btc-rawcap-healthcheck

chown -R root:root "${INSTALL_ROOT}"
chmod -R a+rX "${INSTALL_ROOT}"
"${INSTALL_ROOT}/.venv/bin/python" \
  "${INSTALL_ROOT}/scripts/run_btc_regime_agnostic_collector.py" \
  --data-root "${DATA_ROOT}/data" \
  --status-path "${DATA_ROOT}/status/status.json" \
  --registry "${INSTALL_ROOT}/research/settlement_regime_break_2026-08-14/REGIME_REGISTRY.json" \
  --validate-only
"${INSTALL_ROOT}/.venv/bin/python" \
  "${INSTALL_ROOT}/scripts/maintain_btc_rawcap.py" \
  --data-root "${DATA_ROOT}/data" \
  --compress-after-seconds 600 \
  --retention-days 30 \
  --status-path "${DATA_ROOT}/monitor/maintenance-latest.json" \
  --validate-only
systemctl daemon-reload

printf '%s\n' \
  "Validated only. Start manually when ready:" \
  "  systemctl enable polymm-btc-rawcap.service" \
  "  systemctl start polymm-btc-rawcap.service" \
  "  systemctl enable polymm-btc-rawcap-health.timer" \
  "  systemctl start polymm-btc-rawcap-health.timer" \
  "  systemctl enable polymm-btc-rawcap-maintenance.timer" \
  "  systemctl start polymm-btc-rawcap-maintenance.timer"
