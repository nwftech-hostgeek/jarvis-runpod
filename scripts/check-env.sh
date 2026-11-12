#!/bin/sh
set -eu

if [ ! -f .env ]; then
    printf '.env is missing; copy .env.example to .env first.\n' >&2
    exit 1
fi

failed=0
for name in WEBUI_SECRET_KEY LLAMA_API_KEY VOICE_API_KEY TOOL_API_KEY TS_AUTHKEY; do
    value=$(awk -F= -v key="${name}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' .env)
    case "${value}" in
        ""|change-me*)
            printf '%s must be replaced in .env\n' "${name}" >&2
            failed=1
            ;;
    esac
done

llama_key=$(awk -F= '$1 == "LLAMA_API_KEY" {sub(/^[^=]*=/, ""); print; exit}' .env)
voice_key=$(awk -F= '$1 == "VOICE_API_KEY" {sub(/^[^=]*=/, ""); print; exit}' .env)
tool_key=$(awk -F= '$1 == "TOOL_API_KEY" {sub(/^[^=]*=/, ""); print; exit}' .env)
webui_key=$(awk -F= '$1 == "WEBUI_SECRET_KEY" {sub(/^[^=]*=/, ""); print; exit}' .env)
if [ "${#webui_key}" -lt 32 ] \
    || [ "${#llama_key}" -lt 20 ] \
    || [ "${#voice_key}" -lt 20 ] \
    || [ "${#tool_key}" -lt 20 ]; then
    printf 'WEBUI_SECRET_KEY must be 32+ characters; API keys must be 20+\n' >&2
    failed=1
fi
if [ "${llama_key}" = "${voice_key}" ] \
    || [ "${llama_key}" = "${tool_key}" ] \
    || [ "${voice_key}" = "${tool_key}" ]; then
    printf 'LLAMA_API_KEY, VOICE_API_KEY, and TOOL_API_KEY must be unique\n' >&2
    failed=1
fi

if [ "${failed}" -ne 0 ]; then
    exit 1
fi

docker compose config --quiet
printf '.env and Docker Compose configuration passed validation.\n'
