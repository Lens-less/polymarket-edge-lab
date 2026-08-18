#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Lens-less/poly-mm.git}"
DEPLOY_REF="${DEPLOY_REF:?DEPLOY_REF must be the immutable commit to deploy}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/poly-mm-watch}"
DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-watch}"
SERVICE_USER="${SERVICE_USER:-polybotwatch}"
SERVICE_GROUP="${SERVICE_GROUP:-polybotwatch}"
DEPLOYMENT_REVISION_PATH="${INSTALL_ROOT}/.deployment-revision"
RUNTIME_CONFIG_PATH="${RUNTIME_CONFIG_PATH:-/etc/polymm-watch-config.json}"
SNS_TOPIC_ARN="${POLYMM_SNS_TOPIC_ARN:?POLYMM_SNS_TOPIC_ARN is required for paging}"

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
  "${DATA_ROOT}" "${DATA_ROOT}/state"
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/watch/polymm-watch.service" \
  /etc/systemd/system/polymm-watch.service
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/aws/watch/polymm-watch.timer" \
  /etc/systemd/system/polymm-watch.timer

WATCH_REGION_VALUE="${POLYMM_WATCH_AWS_REGION:-}"
WATCH_INSTANCE_ID_VALUE="${POLYMM_WATCH_INSTANCE_ID:-}"
if [[ -z "${WATCH_REGION_VALUE}" || -z "${WATCH_INSTANCE_ID_VALUE}" ]]; then
  IDENTITY_JSON="$(
    python3.11 - <<'PY'
import json
import urllib.request

token_request = urllib.request.Request(
    "http://169.254.169.254/latest/api/token",
    method="PUT",
    headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
)
with urllib.request.urlopen(token_request, timeout=2) as response:
    token = response.read().decode("utf-8")

document_request = urllib.request.Request(
    "http://169.254.169.254/latest/dynamic/instance-identity/document",
    headers={"X-aws-ec2-metadata-token": token},
)
with urllib.request.urlopen(document_request, timeout=2) as response:
    print(response.read().decode("utf-8"))
PY
  )"
  if [[ -z "${WATCH_REGION_VALUE}" ]]; then
    WATCH_REGION_VALUE="$(
      python3.11 -c 'import json,sys; print(json.loads(sys.stdin.read())["region"])' \
        <<<"${IDENTITY_JSON}"
    )"
  fi
  if [[ -z "${WATCH_INSTANCE_ID_VALUE}" ]]; then
    WATCH_INSTANCE_ID_VALUE="$(
      python3.11 -c 'import json,sys; print(json.loads(sys.stdin.read())["instanceId"])' \
        <<<"${IDENTITY_JSON}"
    )"
  fi
fi

python3.11 - "${INSTALL_ROOT}/deploy/aws/watch/watch-config.json" \
  "${RUNTIME_CONFIG_PATH}" "${WATCH_REGION_VALUE}" \
  "${WATCH_INSTANCE_ID_VALUE}" <<'PY'
import json
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
runtime_path = Path(sys.argv[2])
region = sys.argv[3]
instance_id = sys.argv[4]
document = json.loads(source_path.read_text(encoding="utf-8"))
host = document.get("host", {})
host["cpu_credit_region"] = region
host["cpu_credit_instance_id"] = instance_id
runtime_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
PY
chown root:root "${RUNTIME_CONFIG_PATH}"
chmod 0644 "${RUNTIME_CONFIG_PATH}"

command -v aws >/dev/null
SNS_REGION="${SNS_TOPIC_ARN#arn:aws:sns:}"
SNS_REGION="${SNS_REGION%%:*}"
CONFIRMED_SNS_SUBSCRIPTIONS="$(
  aws sns list-subscriptions-by-topic \
    --region "${SNS_REGION}" \
    --topic-arn "${SNS_TOPIC_ARN}" \
    --query "length(Subscriptions[?SubscriptionArn!='PendingConfirmation'])" \
    --output text
)"
if [[ ! "${CONFIRMED_SNS_SUBSCRIPTIONS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "SNS topic must have at least one confirmed SNS subscription." >&2
  exit 1
fi
printf 'POLYMM_SNS_TOPIC_ARN=%s\n' "${SNS_TOPIC_ARN}" \
  >/etc/polymm-watch.env
chmod 0644 /etc/polymm-watch.env

chown -R root:root "${INSTALL_ROOT}"
chmod -R a+rX "${INSTALL_ROOT}"
"${INSTALL_ROOT}/.venv/bin/python" \
  "${INSTALL_ROOT}/scripts/watch_paper_tracks.py" \
  --config "${RUNTIME_CONFIG_PATH}" \
  --stdout-only
systemctl daemon-reload

printf '%s\n' \
  "Validated only. Start manually when ready:" \
  "  systemctl enable polymm-watch.timer" \
  "  systemctl start polymm-watch.timer"
