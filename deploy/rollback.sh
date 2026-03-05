#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "[rollback] run as root" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/dance_studio}"
CURRENT_LINK="${CURRENT_LINK:-${DEPLOY_ROOT}/current}"
PREVIOUS_LINK="${PREVIOUS_LINK:-${DEPLOY_ROOT}/previous}"
SERVICE_NAME="${SERVICE_NAME:-run_all}"
APP_USER="${APP_USER:-dance}"
APP_GROUP="${APP_GROUP:-dance}"
ENV_FILE="${ENV_FILE:-${DEPLOY_ROOT}/.env}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --deploy-root)
      DEPLOY_ROOT="$2"
      CURRENT_LINK="${DEPLOY_ROOT}/current"
      PREVIOUS_LINK="${DEPLOY_ROOT}/previous"
      shift 2
      ;;
    --app-user)
      APP_USER="$2"
      shift 2
      ;;
    --app-group)
      APP_GROUP="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    *)
      echo "[rollback] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -L "${PREVIOUS_LINK}" ]]; then
  echo "[rollback] previous symlink not found: ${PREVIOUS_LINK}" >&2
  exit 3
fi

PREVIOUS_TARGET="$(readlink -f "${PREVIOUS_LINK}")"
if [[ -z "${PREVIOUS_TARGET}" || ! -d "${PREVIOUS_TARGET}" ]]; then
  echo "[rollback] previous release target invalid: ${PREVIOUS_TARGET}" >&2
  exit 4
fi

CURRENT_TARGET=""
if [[ -L "${CURRENT_LINK}" ]]; then
  CURRENT_TARGET="$(readlink -f "${CURRENT_LINK}")"
fi

ln -sfn "${PREVIOUS_TARGET}" "${CURRENT_LINK}"
if [[ -n "${CURRENT_TARGET}" && "${CURRENT_TARGET}" != "${PREVIOUS_TARGET}" && -d "${CURRENT_TARGET}" ]]; then
  ln -sfn "${CURRENT_TARGET}" "${PREVIOUS_LINK}"
fi

"${SCRIPT_DIR}/install-service.sh" "${SERVICE_NAME}" \
  --app-dir "${CURRENT_LINK}" \
  --app-user "${APP_USER}" \
  --app-group "${APP_GROUP}" \
  --env-file "${ENV_FILE}"

systemctl restart "${SERVICE_NAME}.service"

if "${SCRIPT_DIR}/healthcheck.sh"; then
  echo "[rollback] rollback successful"
  echo "[rollback] current -> $(readlink -f "${CURRENT_LINK}")"
  exit 0
fi

echo "[rollback] healthcheck failed after rollback" >&2
exit 5
