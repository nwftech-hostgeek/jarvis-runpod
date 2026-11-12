#!/bin/sh
set -eu

root=${JARVIS_DATA_ROOT:-}
if [ -z "${root}" ] && [ -f .env ]; then
    root=$(awk -F= '$1 == "JARVIS_DATA_ROOT" {sub(/^[^=]*=/, ""); print; exit}' .env)
fi
root=${root:-./data}

case "${root}" in
    ""|"/")
        printf 'Refusing unsafe JARVIS_DATA_ROOT: %s\n' "${root}" >&2
        exit 1
        ;;
esac

mkdir -p \
    "${root}/models" \
    "${root}/open-webui" \
    "${root}/voice-cache" \
    "${root}/tailscale" \
    secrets

chmod 0700 secrets "${root}/tailscale"

if [ "$(id -u)" -eq 0 ]; then
    # IDs match the non-root users in the custom llama and Speaches images.
    chown 10001:10001 "${root}/models"
    chown 1000:1000 "${root}/voice-cache"
else
    printf '%s\n' \
        "Data directories created at ${root}." \
        "If a Linux bind mount reports permission errors, rerun this script with sudo."
fi

printf 'Persistent directories are ready under %s\n' "${root}"
