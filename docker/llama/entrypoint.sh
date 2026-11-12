#!/bin/sh
set -eu
set -f

: "${MODEL_PATH:?MODEL_PATH is required}"
: "${LLAMA_API_KEY:?LLAMA_API_KEY is required}"

case "${LLAMA_API_KEY}" in
    change-me*|"")
        printf 'LLAMA_API_KEY still contains a placeholder value\n' >&2
        exit 1
        ;;
esac
if [ "${#LLAMA_API_KEY}" -lt 20 ]; then
    printf 'LLAMA_API_KEY must contain at least 20 characters\n' >&2
    exit 1
fi

/usr/local/bin/download-model.sh

ctx_size=${LLAMA_CTX_SIZE:-16384}
parallel=${LLAMA_PARALLEL:-1}
gpu_layers=${LLAMA_N_GPU_LAYERS:-99}
model_alias=${MODEL_ALIAS:-jarvis-model}

# llama-server reads LLAMA_API_KEY through its native environment binding,
# keeping the bearer token out of the process command line.
set -- /usr/local/bin/llama-server \
    --host 0.0.0.0 \
    --port 8080 \
    --model "${MODEL_PATH}" \
    --alias "${model_alias}" \
    --ctx-size "${ctx_size}" \
    --parallel "${parallel}" \
    --n-gpu-layers "${gpu_layers}" \
    --batch-size "${LLAMA_BATCH_SIZE:-2048}" \
    --ubatch-size "${LLAMA_UBATCH_SIZE:-512}" \
    --cache-type-k "${LLAMA_CACHE_TYPE_K:-q8_0}" \
    --cache-type-v "${LLAMA_CACHE_TYPE_V:-q8_0}" \
    --flash-attn "${LLAMA_FLASH_ATTN:-on}" \
    --jinja \
    --cont-batching \
    --metrics \
    --no-webui

if [ -n "${LLAMA_THREADS:-}" ]; then
    set -- "$@" --threads "${LLAMA_THREADS}"
fi

# This intentionally performs only whitespace splitting, not shell evaluation.
# Advanced arguments containing whitespace within one value are not supported.
if [ -n "${LLAMA_EXTRA_ARGS:-}" ]; then
    # shellcheck disable=SC2086
    set -- "$@" ${LLAMA_EXTRA_ARGS}
fi

printf 'Starting llama-server: model=%s alias=%s context=%s slots=%s gpu_layers=%s\n' \
    "${MODEL_PATH}" "${model_alias}" "${ctx_size}" "${parallel}" "${gpu_layers}"

exec "$@"
