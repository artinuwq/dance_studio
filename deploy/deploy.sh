#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "[deploy] run as root" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/dance_studio}"
REPO_DIR="${REPO_DIR:-${DEPLOY_ROOT}/repo}"
RELEASES_DIR="${RELEASES_DIR:-${DEPLOY_ROOT}/releases}"
CURRENT_LINK="${CURRENT_LINK:-${DEPLOY_ROOT}/current}"
PREVIOUS_LINK="${PREVIOUS_LINK:-${DEPLOY_ROOT}/previous}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-run_all}"
APP_USER="${APP_USER:-dance}"
APP_GROUP="${APP_GROUP:-dance}"
ENV_FILE="${ENV_FILE:-${DEPLOY_ROOT}/.env}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REPO_URL="${REPO_URL:-}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --service)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --deploy-root)
      DEPLOY_ROOT="$2"
      REPO_DIR="${DEPLOY_ROOT}/repo"
      RELEASES_DIR="${DEPLOY_ROOT}/releases"
      CURRENT_LINK="${DEPLOY_ROOT}/current"
      PREVIOUS_LINK="${DEPLOY_ROOT}/previous"
      shift 2
      ;;
    --repo-url)
      REPO_URL="$2"
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
      echo "[deploy] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

for cmd in git rsync systemctl "${PYTHON_BIN}"; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[deploy] required command not found: ${cmd}" >&2
    exit 3
  fi
done

mkdir -p "${DEPLOY_ROOT}" "${RELEASES_DIR}"

if [[ ! -d "${REPO_DIR}/.git" ]]; then
  if [[ -z "${REPO_URL}" ]]; then
    echo "[deploy] repo is missing and REPO_URL is not set (${REPO_DIR})" >&2
    exit 4
  fi
  git clone "${REPO_URL}" "${REPO_DIR}"
fi

echo "[deploy] updating repo: ${REPO_DIR}"
git -C "${REPO_DIR}" fetch --all --prune
git -C "${REPO_DIR}" checkout "${BRANCH}"
git -C "${REPO_DIR}" pull --ff-only origin "${BRANCH}"

COMMIT_SHA="$(git -C "${REPO_DIR}" rev-parse --short HEAD)"
TS="$(date +%Y%m%d%H%M%S)"
NEW_RELEASE_DIR="${RELEASES_DIR}/${TS}-${COMMIT_SHA}"

echo "[deploy] creating release: ${NEW_RELEASE_DIR}"
mkdir -p "${NEW_RELEASE_DIR}"
rsync -a --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  --exclude ".pytest_cache" \
  "${REPO_DIR}/" "${NEW_RELEASE_DIR}/"

echo "[deploy] installing dependencies"
"${PYTHON_BIN}" -m venv "${NEW_RELEASE_DIR}/.venv"
"${NEW_RELEASE_DIR}/.venv/bin/pip" install --upgrade pip
"${NEW_RELEASE_DIR}/.venv/bin/pip" install -r "${NEW_RELEASE_DIR}/requirements.txt"

echo "[deploy] build step"
"${NEW_RELEASE_DIR}/.venv/bin/python" -m compileall "${NEW_RELEASE_DIR}/src"

if [[ -f "${NEW_RELEASE_DIR}/alembic.ini" ]]; then
  echo "[deploy] alembic upgrade head"
  (
    cd "${NEW_RELEASE_DIR}"
    "${NEW_RELEASE_DIR}/.venv/bin/alembic" upgrade head
  )
fi

CURRENT_TARGET=""
if [[ -L "${CURRENT_LINK}" ]]; then
  CURRENT_TARGET="$(readlink -f "${CURRENT_LINK}")"
fi

ln -sfn "${NEW_RELEASE_DIR}" "${CURRENT_LINK}"
if [[ -n "${CURRENT_TARGET}" && -d "${CURRENT_TARGET}" && "${CURRENT_TARGET}" != "${NEW_RELEASE_DIR}" ]]; then
  ln -sfn "${CURRENT_TARGET}" "${PREVIOUS_LINK}"
fi

chown -R "${APP_USER}:${APP_GROUP}" "${NEW_RELEASE_DIR}"
chown -h "${APP_USER}:${APP_GROUP}" "${CURRENT_LINK}" || true
if [[ -L "${PREVIOUS_LINK}" ]]; then
  chown -h "${APP_USER}:${APP_GROUP}" "${PREVIOUS_LINK}" || true
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "[deploy] warning: env file not found (${ENV_FILE})"
fi

echo "[deploy] installing service unit"
"${SCRIPT_DIR}/install-service.sh" "${SERVICE_NAME}" \
  --app-dir "${CURRENT_LINK}" \
  --app-user "${APP_USER}" \
  --app-group "${APP_GROUP}" \
  --env-file "${ENV_FILE}"

echo "[deploy] restarting ${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

echo "[deploy] running healthcheck"
if ! "${SCRIPT_DIR}/healthcheck.sh"; then
  echo "[deploy] healthcheck failed, starting rollback" >&2
  "${SCRIPT_DIR}/rollback.sh" \
    --service "${SERVICE_NAME}" \
    --deploy-root "${DEPLOY_ROOT}" \
    --app-user "${APP_USER}" \
    --app-group "${APP_GROUP}" \
    --env-file "${ENV_FILE}" || true
  exit 5
fi

echo "[deploy] cleanup old releases"
if [[ "${KEEP_RELEASES}" =~ ^[0-9]+$ ]] && [[ "${KEEP_RELEASES}" -gt 0 ]]; then
  mapfile -t RELEASE_DIRS < <(find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d | sort)
  if [[ "${#RELEASE_DIRS[@]}" -gt "${KEEP_RELEASES}" ]]; then
    REMOVE_COUNT=$(( ${#RELEASE_DIRS[@]} - KEEP_RELEASES ))
    for ((i=0; i<REMOVE_COUNT; i++)); do
      candidate="${RELEASE_DIRS[$i]}"
      if [[ -L "${CURRENT_LINK}" && "$(readlink -f "${CURRENT_LINK}")" == "${candidate}" ]]; then
        continue
      fi
      if [[ -L "${PREVIOUS_LINK}" && "$(readlink -f "${PREVIOUS_LINK}")" == "${candidate}" ]]; then
        continue
      fi
      rm -rf "${candidate}"
    done
  fi
fi

echo "[deploy] SUCCESS branch=${BRANCH} commit=${COMMIT_SHA}"
echo "[deploy] current -> $(readlink -f "${CURRENT_LINK}")"
