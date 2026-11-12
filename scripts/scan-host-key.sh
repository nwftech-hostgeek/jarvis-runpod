#!/bin/sh
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    printf 'Usage: %s TARGET_ALIAS [--yes]\n' "$0" >&2
    exit 2
fi

target=$1
assume_yes=${2:-}
tmp=$(mktemp)
trap 'rm -f "${tmp}"' EXIT HUP INT TERM

docker compose exec -T --user jarvis-tools jarvis-tools \
    python /app/scan_host_key.py "${target}" >"${tmp}"

printf '\nCandidate known_hosts entry:\n'
cat "${tmp}"

if [ "${assume_yes}" != "--yes" ]; then
    printf '\nVerify the SHA256 fingerprint out-of-band. Append this key? [y/N] '
    read -r answer
    case "${answer}" in
        y|Y|yes|YES) ;;
        *)
            printf 'Host key was not changed.\n'
            exit 1
            ;;
    esac
fi

if grep -Fqx -f "${tmp}" config/ssh_known_hosts; then
    printf 'The exact host key is already present.\n'
else
    cat "${tmp}" >> config/ssh_known_hosts
    printf 'Host key appended to config/ssh_known_hosts.\n'
fi
