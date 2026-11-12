#!/bin/sh
set -eu

compose() {
    docker compose "$@"
}

printf 'Checking container state...\n'
compose ps

printf 'Checking llama.cpp...\n'
compose exec -T llama sh -c \
    'curl -fsS http://127.0.0.1:8080/health >/dev/null &&
     curl -fsS -H "Authorization: Bearer ${LLAMA_API_KEY}" \
       http://127.0.0.1:8080/v1/models >/dev/null'

printf 'Checking voice service...\n'
compose exec -T voice sh -c \
    'curl -fsS http://127.0.0.1:8000/health >/dev/null'

printf 'Checking allowlisted tool service...\n'
compose exec -T jarvis-tools curl -fsS \
    http://127.0.0.1:8090/health
printf '\n'

printf 'Checking Tailscale...\n'
compose exec -T tailscale tailscale \
    --socket=/var/run/tailscale/tailscaled.sock status

webui_port=${OPEN_WEBUI_PORT:-}
if [ -z "${webui_port}" ] && [ -f .env ]; then
    webui_port=$(awk -F= '$1 == "OPEN_WEBUI_PORT" {print $2; exit}' .env)
fi
webui_port=${webui_port:-3000}

printf 'Checking Open WebUI on localhost:%s...\n' "${webui_port}"
curl -fsS "http://127.0.0.1:${webui_port}/health" >/dev/null

printf 'All stack smoke tests passed.\n'
