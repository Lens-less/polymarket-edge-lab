#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Lens-less/poly-mm.git}"
DEPLOY_REF="${DEPLOY_REF:?DEPLOY_REF must be the immutable commit to deploy}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/poly-mm}"
DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm}"
SERVICE_USER="${SERVICE_USER:-polybot}"
SERVICE_GROUP="${SERVICE_GROUP:-polybot}"
CONFIG_PATH="${INSTALL_ROOT}/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/SERVICE_CONFIG.json"
PREREG_PATH="${INSTALL_ROOT}/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/PREREGISTRATION.json"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

if ! grep -q '^ID="\?amzn"\?$' /etc/os-release; then
  echo "This bootstrap is frozen for Amazon Linux only." >&2
  exit 1
fi

dnf install -y git python3.11 python3.11-pip chrony

if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${SERVICE_GROUP}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${SERVICE_GROUP}" --create-home \
    --home-dir "/home/${SERVICE_USER}" --shell /sbin/nologin "${SERVICE_USER}"
fi

python3.11 - <<'PY'
from pathlib import Path

path = Path("/etc/chrony.conf")
text = path.read_text(encoding="utf-8")
amazon_line = "server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4"
updated: list[str] = []
for line in text.splitlines():
    stripped = line.strip()
    if stripped.startswith("pool ") or stripped.startswith("server "):
        if "169.254.169.123" in stripped:
            updated.append(amazon_line)
        else:
            updated.append(f"# disabled-by-poly-mm-v04 {line}")
    else:
        updated.append(line)
if not any(
    line.strip().startswith("server 169.254.169.123") for line in updated
):
    updated.extend(("", "# poly-mm v0.4 frozen clock source", amazon_line))
path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
PY
systemctl enable --now chronyd.service
systemctl restart chronyd.service

if [[ -e "${INSTALL_ROOT}" && ! -d "${INSTALL_ROOT}/.git" ]]; then
  echo "${INSTALL_ROOT} exists but is not a Git checkout." >&2
  exit 1
fi
if [[ ! -d "${INSTALL_ROOT}/.git" ]]; then
  git clone "${REPO_URL}" "${INSTALL_ROOT}"
fi

git -C "${INSTALL_ROOT}" fetch --tags --prune origin
git -C "${INSTALL_ROOT}" checkout --detach "${DEPLOY_REF}"
test "$(git -C "${INSTALL_ROOT}" rev-parse HEAD)" = "${DEPLOY_REF}"

python3.11 -m venv "${INSTALL_ROOT}/.venv"
"${INSTALL_ROOT}/.venv/bin/pip" install --upgrade pip
"${INSTALL_ROOT}/.venv/bin/pip" install -e "${INSTALL_ROOT}"

install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0755 \
  "${DATA_ROOT}" \
  "${DATA_ROOT}/data" \
  "${DATA_ROOT}/research" \
  "${DATA_ROOT}/monitor"

install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/paper_v04/polymm-btc-twap-paper-v04.service" \
  /etc/systemd/system/polymm-btc-twap-paper-v04.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/paper_v04/polymm-btc-twap-paper-v04-health.service" \
  /etc/systemd/system/polymm-btc-twap-paper-v04-health.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/paper_v04/polymm-btc-twap-paper-v04-health.timer" \
  /etc/systemd/system/polymm-btc-twap-paper-v04-health.timer
install -o root -g root -m 0755 \
  "${INSTALL_ROOT}/deploy/aws/paper_v04/polymm-btc-twap-paper-v04-healthcheck.sh" \
  /usr/local/bin/polymm-btc-twap-paper-v04-healthcheck

chown -R root:root "${INSTALL_ROOT}"
chmod -R a+rX "${INSTALL_ROOT}"

"${INSTALL_ROOT}/.venv/bin/python" \
  "${INSTALL_ROOT}/scripts/run_btc_twap_relative_value_service.py" \
  --config "${CONFIG_PATH}" \
  --validate-only
"${INSTALL_ROOT}/.venv/bin/python" - <<PY
import hashlib
import json
from pathlib import Path

prereg = json.loads(Path("${PREREG_PATH}").read_text(encoding="utf-8"))
assert prereg["scope"]["paper_only"] is True
assert prereg["scope"]["live_orders_disabled"] is True
assert prereg["frozen_strategy"]["clock_sync"]["source"] == "Chrony Amazon Time Sync Service 169.254.169.123"
strategy_path = Path("${INSTALL_ROOT}") / prereg["strategy_spec"]["path"]
assert strategy_path.is_file()
assert hashlib.sha256(strategy_path.read_bytes()).hexdigest() == prereg["strategy_spec"]["sha256"]
PY

chronyc -n tracking
systemctl daemon-reload
systemctl enable --now polymm-btc-twap-paper-v04.service
systemctl enable --now polymm-btc-twap-paper-v04-health.timer

systemctl --no-pager --full status polymm-btc-twap-paper-v04.service || true
systemctl --no-pager --full status polymm-btc-twap-paper-v04-health.timer || true
