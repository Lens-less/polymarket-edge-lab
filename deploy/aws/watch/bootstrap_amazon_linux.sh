#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Lens-less/poly-mm.git}"
DEPLOY_REF="${DEPLOY_REF:?DEPLOY_REF must be the immutable commit to deploy}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/poly-mm-watch}"
DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-watch}"
SERVICE_USER="${SERVICE_USER:-polybotwatch}"
SERVICE_GROUP="${SERVICE_GROUP:-polybotwatch}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if ! grep -q '^ID="\?amzn"\?$' /etc/os-release; then
  echo "This bootstrap is frozen for Amazon Linux only." >&2
  exit 1
fi

dnf install -y git python3.11 python3.11-pip
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
fi

python3.11 -m venv "${INSTALL_ROOT}/.venv"
"${INSTALL_ROOT}/.venv/bin/pip" install --upgrade pip
"${INSTALL_ROOT}/.venv/bin/pip" install -e "${INSTALL_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0755 \
  "${DATA_ROOT}" "${DATA_ROOT}/state"
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/watch/polymm-watch.service" \
  /etc/systemd/system/polymm-watch.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/watch/polymm-watch.timer" \
  /etc/systemd/system/polymm-watch.timer

if [[ -n "${POLYMM_SNS_TOPIC_ARN:-}" ]]; then
  command -v aws >/dev/null
  printf 'POLYMM_SNS_TOPIC_ARN=%s\n' "${POLYMM_SNS_TOPIC_ARN}" \
    >/etc/polymm-watch.env
  chmod 0644 /etc/polymm-watch.env
fi

chown -R root:root "${INSTALL_ROOT}"
chmod -R a+rX "${INSTALL_ROOT}"
"${INSTALL_ROOT}/.venv/bin/python" \
  "${INSTALL_ROOT}/scripts/watch_paper_tracks.py" \
  --config "${INSTALL_ROOT}/deploy/aws/watch/watch-config.json" \
  --stdout-only
systemctl daemon-reload

printf '%s\n' \
  "Validated only. Start manually when ready:" \
  "  systemctl enable polymm-watch.timer" \
  "  systemctl start polymm-watch.timer"
