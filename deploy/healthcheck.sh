#!/usr/bin/env bash
set -euo pipefail

HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:3000/health}"
HEALTHCHECK_TIMEOUT="${HEALTHCHECK_TIMEOUT:-5}"
HEALTHCHECK_RETRIES="${HEALTHCHECK_RETRIES:-3}"
HEALTHCHECK_RETRY_DELAY="${HEALTHCHECK_RETRY_DELAY:-2}"

if ! command -v curl >/dev/null 2>&1; then
  echo "[healthcheck] curl is required" >&2
  exit 2
fi

attempt=1
while [[ "${attempt}" -le "${HEALTHCHECK_RETRIES}" ]]; do
  echo "[healthcheck] attempt ${attempt}/${HEALTHCHECK_RETRIES}: ${HEALTHCHECK_URL}"
  if curl -fsS --max-time "${HEALTHCHECK_TIMEOUT}" "${HEALTHCHECK_URL}" >/dev/null; then
    echo "[healthcheck] ok"
    exit 0
  fi

  if [[ "${attempt}" -lt "${HEALTHCHECK_RETRIES}" ]]; then
    sleep "${HEALTHCHECK_RETRY_DELAY}"
  fi
  attempt=$((attempt + 1))
done

echo "[healthcheck] failed: ${HEALTHCHECK_URL}" >&2
exit 1
