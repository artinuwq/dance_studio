#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dance_studio}"
BRANCH="${BRANCH:-main}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:3000/health}"
DEFAULT_SERVICES="web bot"

SERVICES_RAW="${SERVICES:-${DEFAULT_SERVICES}}"
SERVICE_NAMES=()
if [[ -n "${SERVICES_RAW// }" ]]; then
  read -r -a SERVICE_NAMES <<< "${SERVICES_RAW}"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      APP_DIR="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --service)
      SERVICE_NAMES+=("$2")
      shift 2
      ;;
    --services)
      read -r -a SERVICE_NAMES <<< "$2"
      shift 2
      ;;
    --healthcheck-url)
      HEALTHCHECK_URL="$2"
      shift 2
      ;;
    *)
      echo "[deploy] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ${#SERVICE_NAMES[@]} -eq 0 ]]; then
  read -r -a SERVICE_NAMES <<< "${DEFAULT_SERVICES}"
fi

echo "==> Stop services: ${SERVICE_NAMES[*]}"
for service in "${SERVICE_NAMES[@]}"; do
  sudo systemctl stop "${service}"
done

echo "==> Update code in ${APP_DIR} (${BRANCH})"
cd "${APP_DIR}"
git fetch origin
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

echo "==> Start services: ${SERVICE_NAMES[*]}"
for service in "${SERVICE_NAMES[@]}"; do
  sudo systemctl start "${service}"
done

echo "==> Wait a bit"
sleep 3

echo "==> Healthcheck: ${HEALTHCHECK_URL}"
if curl -fsS --max-time 5 "${HEALTHCHECK_URL}" >/dev/null; then
  echo "Deploy success"
  for service in "${SERVICE_NAMES[@]}"; do
    sudo systemctl status "${service}" --no-pager -l | sed -n '1,12p'
  done
  exit 0
fi

echo "Healthcheck failed"
for service in "${SERVICE_NAMES[@]}"; do
  sudo journalctl -u "${service}" -n 80 --no-pager || true
done
exit 1
