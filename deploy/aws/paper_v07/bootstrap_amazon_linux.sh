#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Lens-less/poly-mm.git}"
DEPLOY_REF="${DEPLOY_REF:?DEPLOY_REF must be the immutable commit to deploy}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/poly-mm-v07}"
DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-v07}"
SERVICE_USER="${SERVICE_USER:-polybotv07}"
SERVICE_GROUP="${SERVICE_GROUP:-polybotv07}"
RESEARCH_DIR="btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16"
CONFIG_PATH="${INSTALL_ROOT}/research/${RESEARCH_DIR}/SERVICE_CONFIG.json"
PREREG_PATH="${INSTALL_ROOT}/research/btc_5m_15m_relative_value_counterfactual_v07_2026-08-15/PREREGISTRATION.json"
DEPLOYMENT_REVISION_PATH="${INSTALL_ROOT}/.deployment-revision"
IMPLEMENTATION_REVISION_PATH="${INSTALL_ROOT}/.implementation-revision"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if ! grep -q '^ID="\?amzn"\?$' /etc/os-release; then
  echo "This bootstrap is frozen for Amazon Linux only." >&2
  exit 1
fi
if [[ ! "${DEPLOY_REF}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "DEPLOY_REF must be a 40-character lowercase commit." >&2
  exit 1
fi

dnf install -y acl git python3.11 python3.11-pip sudo
if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${SERVICE_GROUP}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${SERVICE_GROUP}" --create-home \
    --home-dir "/home/${SERVICE_USER}" --shell /sbin/nologin "${SERVICE_USER}"
fi
getent group polybotv06 >/dev/null 2>&1 || {
  echo "Required source group polybotv06 is missing." >&2
  exit 1
}

if [[ ! -e "${INSTALL_ROOT}" ]]; then
  git clone "${REPO_URL}" "${INSTALL_ROOT}"
fi
if [[ ! -d "${INSTALL_ROOT}/.git" ]]; then
  echo "${INSTALL_ROOT} must be a Git checkout; release-marker-only archives are forbidden." >&2
  exit 1
fi
for release_marker in "/.deployment-revision" "/.implementation-revision"; do
  grep -qxF "${release_marker}" "${INSTALL_ROOT}/.git/info/exclude" ||
    printf '%s\n' "${release_marker}" >>"${INSTALL_ROOT}/.git/info/exclude"
done
git -C "${INSTALL_ROOT}" fetch --tags --prune origin
git -C "${INSTALL_ROOT}" checkout -f --detach "${DEPLOY_REF}"
git -C "${INSTALL_ROOT}" clean -ffdx
test "$(git -C "${INSTALL_ROOT}" rev-parse HEAD)" = "${DEPLOY_REF}"
test -z "$(git -C "${INSTALL_ROOT}" status --porcelain=v1 --untracked-files=all)"
printf '%s\n' "${DEPLOY_REF}" >"${DEPLOYMENT_REVISION_PATH}"

FROZEN_IMPLEMENTATION_REVISION="$(python3.11 - <<PY
import json
from pathlib import Path

document = json.loads(Path("${PREREG_PATH}").read_text(encoding="utf-8"))
revision = document.get("source_baseline")
if not isinstance(revision, str) or len(revision) != 40:
    raise SystemExit("preregistration source_baseline must be a 40-character commit")
print(revision)
PY
)"
git -C "${INSTALL_ROOT}" merge-base --is-ancestor \
  "${FROZEN_IMPLEMENTATION_REVISION}" "${DEPLOY_REF}"
printf '%s\n' "${FROZEN_IMPLEMENTATION_REVISION}" \
  >"${IMPLEMENTATION_REVISION_PATH}"
test -z "$(git -C "${INSTALL_ROOT}" status --porcelain=v1 --untracked-files=all)"

python3.11 -m venv "${INSTALL_ROOT}/.venv"
"${INSTALL_ROOT}/.venv/bin/pip" install --upgrade pip
"${INSTALL_ROOT}/.venv/bin/pip" install -e "${INSTALL_ROOT}"

install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0755 \
  "${DATA_ROOT}" \
  "${DATA_ROOT}/data" \
  "${DATA_ROOT}/research" \
  "${DATA_ROOT}/status" \
  "${DATA_ROOT}/monitor" \
  "${DATA_ROOT}/monitor/history"

SOURCE_RUNS_ROOT="$(
  "${INSTALL_ROOT}/.venv/bin/python" - "${CONFIG_PATH}" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
source_runs_root = document.get("source_runs_root")
if not isinstance(source_runs_root, str) or not source_runs_root:
    raise SystemExit("source_runs_root is missing or invalid")
path = Path(source_runs_root).expanduser()
if not path.is_absolute():
    raise SystemExit("source_runs_root must be absolute")
resolved = path.resolve(strict=True)
allowed_root = Path("/var/lib/poly-mm-v06").resolve(strict=True)
if not resolved.is_relative_to(allowed_root):
    raise SystemExit("source_runs_root must remain under /var/lib/poly-mm-v06")
print(resolved)
PY
)"
SOURCE_STATUS_PATH="$(
  "${INSTALL_ROOT}/.venv/bin/python" - "${CONFIG_PATH}" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
source_status_path = document.get("source_status_path")
if not isinstance(source_status_path, str) or not source_status_path:
    raise SystemExit("source_status_path is missing or invalid")
path = Path(source_status_path).expanduser()
if not path.is_absolute():
    raise SystemExit("source_status_path must be absolute")
resolved = path.resolve(strict=True)
allowed_root = Path("/var/lib/poly-mm-v06").resolve(strict=True)
if not resolved.is_relative_to(allowed_root):
    raise SystemExit("source_status_path must remain under /var/lib/poly-mm-v06")
print(resolved)
PY
)"
SOURCE_DATA_ROOT="$(dirname "${SOURCE_RUNS_ROOT}")"
SOURCE_STATUS_DIR="$(dirname "${SOURCE_STATUS_PATH}")"
if [[ "$(dirname "${SOURCE_STATUS_DIR}")" != "${SOURCE_DATA_ROOT}" ]]; then
  echo "V0.6 source runs and status must share the frozen data root." >&2
  exit 1
fi

setfacl -m "u:${SERVICE_USER}:r-x,d:u:${SERVICE_USER}:r-x" \
  "${SOURCE_DATA_ROOT}" "${SOURCE_STATUS_DIR}"
setfacl -m "u:${SERVICE_USER}:r--" "${SOURCE_STATUS_PATH}"
setfacl -m "u:${SERVICE_USER}:r-x,d:u:${SERVICE_USER}:r-x" \
  "${SOURCE_RUNS_ROOT}"
find "${SOURCE_RUNS_ROOT}" -mindepth 1 -type d \
  -exec setfacl -m \
  "u:${SERVICE_USER}:r-x,d:u:${SERVICE_USER}:r-x" {} +
find "${SOURCE_RUNS_ROOT}" -type f \
  -exec setfacl -m "u:${SERVICE_USER}:r--" {} +
if ! sudo -u "${SERVICE_USER}" test -r "${SOURCE_STATUS_PATH}"; then
  echo "V0.7 service user cannot read the V0.6 service status." >&2
  exit 1
fi
if sudo -u "${SERVICE_USER}" test -w "${SOURCE_STATUS_PATH}"; then
  echo "V0.7 service user must not be able to write the V0.6 service status." >&2
  exit 1
fi

for xfs_path in "${DATA_ROOT}" "${SOURCE_RUNS_ROOT}"; do
  if [[ "$(stat -f -c %T "${xfs_path}")" != "xfs" ]]; then
    echo "XFS is mandatory for V0.7 reflink snapshots: ${xfs_path}" >&2
    exit 1
  fi
done
if [[ "$(stat -c %d "${DATA_ROOT}")" != \
  "$(stat -c %d "${SOURCE_RUNS_ROOT}")" ]]; then
  echo "V0.6 source and V0.7 data root must share one filesystem for reflink snapshots." >&2
  exit 1
fi

tmp_probe_dir="$(mktemp -d "${DATA_ROOT}/reflink-probe.XXXXXX")"
dst_probe="${tmp_probe_dir}/dst.txt"
chown "${SERVICE_USER}:${SERVICE_GROUP}" "${tmp_probe_dir}"
chmod 0700 "${tmp_probe_dir}"
cleanup_reflink_probe() {
  rm -f "${dst_probe}"
  rmdir "${tmp_probe_dir}" 2>/dev/null || true
}
trap cleanup_reflink_probe EXIT
src_probe="$(
  find "${SOURCE_RUNS_ROOT}" -type f -name capture-config.json -print -quit
)"
if [[ -z "${src_probe}" ]]; then
  echo "A V0.6 capture-config.json is required for the cross-root reflink probe." >&2
  exit 1
fi
if ! sudo -u "${SERVICE_USER}" test -r "${src_probe}"; then
  echo "V0.7 service user cannot read the V0.6 reflink source." >&2
  exit 1
fi
if sudo -u "${SERVICE_USER}" test -w "${src_probe}"; then
  echo "V0.7 service user must not be able to write the V0.6 source." >&2
  exit 1
fi
sudo -u "${SERVICE_USER}" cp --reflink=always \
  "${src_probe}" "${dst_probe}"
sudo -u "${SERVICE_USER}" python3.11 - \
  "${src_probe}" "${dst_probe}" <<'PY'
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
if src.read_bytes() != dst.read_bytes():
    raise SystemExit("reflink probe payload mismatch")
if src.stat().st_ino == dst.stat().st_ino:
    raise SystemExit("reflink probe must create a distinct inode")
PY
cleanup_reflink_probe
trap - EXIT

install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/paper_v07/polymm-btc-twap-paper-v07-performance.service" \
  /etc/systemd/system/polymm-btc-twap-paper-v07-performance.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/paper_v07/polymm-btc-twap-paper-v07-performance.timer" \
  /etc/systemd/system/polymm-btc-twap-paper-v07-performance.timer
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/paper_v07/polymm-btc-twap-paper-v07-health.service" \
  /etc/systemd/system/polymm-btc-twap-paper-v07-health.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/paper_v07/polymm-btc-twap-paper-v07-health.timer" \
  /etc/systemd/system/polymm-btc-twap-paper-v07-health.timer
install -o root -g root -m 0755 \
  "${INSTALL_ROOT}/deploy/aws/paper_v07/polymm-btc-twap-paper-v07-healthcheck.sh" \
  /usr/local/bin/polymm-btc-twap-paper-v07-healthcheck

chown -R root:root "${INSTALL_ROOT}"
chmod -R a+rX "${INSTALL_ROOT}"
"${INSTALL_ROOT}/.venv/bin/python" \
  "${INSTALL_ROOT}/scripts/run_btc_twap_relative_value_v07_shadow.py" \
  --config "${CONFIG_PATH}" --validate-only
systemctl daemon-reload

printf '%s\n' \
  "Validated only. Start manually when ready:" \
  "  systemctl enable polymm-btc-twap-paper-v07-performance.timer" \
  "  systemctl start polymm-btc-twap-paper-v07-performance.timer" \
  "  systemctl enable polymm-btc-twap-paper-v07-health.timer" \
  "  systemctl start polymm-btc-twap-paper-v07-health.timer"
