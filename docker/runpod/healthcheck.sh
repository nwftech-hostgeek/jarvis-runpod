#!/bin/sh
set -eu

curl --fail --silent --show-error \
    "http://127.0.0.1:${RUNPOD_UI_PORT:-3000}/health" >/dev/null
curl --fail --silent --show-error \
    "http://127.0.0.1:8080/health" >/dev/null
curl --fail --silent --show-error \
    "http://127.0.0.1:8000/health" >/dev/null
curl --fail --silent --show-error \
    "http://127.0.0.1:8090/health" >/dev/null

if [ "${TAILSCALE_REQUIRED:-false}" = "true" ]; then
    tailscale --socket="${TS_SOCKET:-/run/tailscale/tailscaled.sock}" \
        status >/dev/null
fi
