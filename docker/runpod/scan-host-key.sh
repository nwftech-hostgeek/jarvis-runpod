#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf 'Usage: %s TARGET_ALIAS\n' "$0" >&2
    exit 2
fi

config_root=${RUNPOD_CONFIG_ROOT:-/workspace/jarvis-config}
export SSH_TARGETS_FILE="${config_root}/ssh-targets.yaml"
export SSH_KNOWN_HOSTS_FILE="${config_root}/ssh_known_hosts"
export SSH_SOCKS5_HOST=127.0.0.1
export SSH_SOCKS5_PORT=1055

cd /opt/jarvis-tools-app
exec /opt/jarvis-tools/bin/python scan_host_key.py "$1"
