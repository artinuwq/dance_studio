#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "[install-service] run as root" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <service-name> [--app-dir PATH] [--app-user USER] [--app-group GROUP] [--env-file PATH] [--units-src DIR] [--units-dst DIR]" >&2
  exit 2
fi

SERVICE_NAME="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNITS_SRC="${UNITS_SRC:-${SCRIPT_DIR}/systemd}"
UNITS_DST="${UNITS_DST:-/etc/systemd/system}"
APP_DIR="${APP_DIR:-/opt/dance_studio/current}"
APP_USER="${APP_USER:-dance}"
APP_GROUP="${APP_GROUP:-dance}"
ENV_FILE="${ENV_FILE:-/opt/dance_studio/.env}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      APP_DIR="$2"
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
    --units-src)
      UNITS_SRC="$2"
      shift 2
      ;;
    --units-dst)
      UNITS_DST="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    *)
      echo "[install-service] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

SOURCE_UNIT="${UNITS_SRC}/${SERVICE_NAME}.service"
TARGET_UNIT="${UNITS_DST}/${SERVICE_NAME}.service"

if [[ ! -f "${SOURCE_UNIT}" ]]; then
  echo "[install-service] unit file not found: ${SOURCE_UNIT}" >&2
  exit 3
fi

mkdir -p "${UNITS_DST}"
TMP_FILE="$(mktemp)"
trap 'rm -f "${TMP_FILE}"' EXIT

sed \
  -e "s|/opt/dance_studio|${APP_DIR}|g" \
  -e "s|^EnvironmentFile=-.*$|EnvironmentFile=-${ENV_FILE}|g" \
  -e "s|^User=dance$|User=${APP_USER}|g" \
  -e "s|^Group=dance$|Group=${APP_GROUP}|g" \
  "${SOURCE_UNIT}" > "${TMP_FILE}"

install -m 0644 "${TMP_FILE}" "${TARGET_UNIT}"
systemctl daemon-reload

echo "[install-service] installed ${TARGET_UNIT}"
echo "[install-service] app_dir=${APP_DIR} user=${APP_USER} group=${APP_GROUP}"
