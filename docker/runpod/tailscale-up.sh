#!/bin/sh
set -eu
set -f

socket=${TS_SOCKET:-/run/tailscale/tailscaled.sock}

attempt=0
while [ ! -S "${socket}" ]; do
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge 60 ]; then
        printf 'Timed out waiting for tailscaled socket: %s\n' "${socket}" >&2
        exit 1
    fi
    sleep 1
done

set -- \
    --hostname="${TS_HOSTNAME:-jarvis-runpod}" \
    --accept-dns="${TS_ACCEPT_DNS:-false}"

if [ -n "${TS_EXTRA_ARGS:-}" ]; then
    # Intentional whitespace splitting only; no shell evaluation.
    # shellcheck disable=SC2086
    set -- "$@" ${TS_EXTRA_ARGS}
fi

if tailscale --socket="${socket}" status >/dev/null 2>&1; then
    tailscale --socket="${socket}" set "$@"
    printf 'Tailscale state restored for %s\n' "${TS_HOSTNAME:-jarvis-runpod}"
    exit 0
fi

if [ -z "${TS_AUTHKEY_FILE:-}" ] || [ ! -f "${TS_AUTHKEY_FILE}" ]; then
    if [ "${TAILSCALE_REQUIRED:-false}" = "true" ]; then
        printf 'Persisted Tailscale state could not be restored and no auth key is available.\n' >&2
        exit 1
    fi
    printf 'Tailscale auth key is not set; Tailnet access is disabled.\n' >&2
    exit 0
fi

tailscale --socket="${socket}" up \
    --auth-key="file:${TS_AUTHKEY_FILE}" "$@"
printf 'Tailscale connected as %s\n' "${TS_HOSTNAME:-jarvis-runpod}"
