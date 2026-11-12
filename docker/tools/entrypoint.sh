#!/bin/sh
set -eu

source_dir=${SSH_PRIVATE_KEY_SOURCE_DIR:-/run/secrets-src/ssh}
runtime_dir=/run/secrets/ssh

install -d -m 0700 -o jarvis-tools -g jarvis-tools "${runtime_dir}"

if [ -d "${source_dir}" ]; then
    for source in "${source_dir}"/*; do
        [ -f "${source}" ] || continue
        name=$(basename "${source}")
        case "${name}" in
            ""|"."|".."|*[!A-Za-z0-9_.-]*)
                printf 'Ignoring SSH key with unsafe filename: %s\n' "${name}" >&2
                continue
                ;;
        esac
        install -m 0400 -o jarvis-tools -g jarvis-tools \
            "${source}" "${runtime_dir}/${name}"
    done
fi

exec setpriv \
    --reuid=jarvis-tools \
    --regid=jarvis-tools \
    --init-groups \
    "$@"
