#!/bin/sh
set -eu

: "${MODEL_PATH:?MODEL_PATH must point to the GGUF file llama-server should load}"

if [ -s "${MODEL_PATH}" ]; then
    printf 'Model already present: %s\n' "${MODEL_PATH}"
    exit 0
fi

if [ -z "${HF_REPO_ID:-}" ]; then
    printf 'Model is missing and HF_REPO_ID is not set: %s\n' "${MODEL_PATH}" >&2
    exit 1
fi

model_dir=$(dirname "${MODEL_PATH}")
model_name=$(basename "${MODEL_PATH}")
mkdir -p "${model_dir}"

printf 'Downloading model from Hugging Face repository %s\n' "${HF_REPO_ID}"

if [ -n "${HF_MODEL_PATTERN:-}" ]; then
    hf download "${HF_REPO_ID}" \
        --include "${HF_MODEL_PATTERN}" \
        --local-dir "${model_dir}"
else
    filename=${HF_FILENAME:-${model_name}}
    hf download "${HF_REPO_ID}" "${filename}" \
        --local-dir "${model_dir}"
fi

if [ ! -s "${MODEL_PATH}" ]; then
    printf 'Download completed but expected model was not found: %s\n' "${MODEL_PATH}" >&2
    printf 'Check MODEL_PATH, HF_FILENAME, and HF_MODEL_PATTERN.\n' >&2
    exit 1
fi

printf 'Model ready: %s\n' "${MODEL_PATH}"
