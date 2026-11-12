#!/bin/sh
set -eu

require_secret() {
    name=$1
    minimum=$2
    value=$(printenv "${name}" || true)
    case "${value}" in
        change-me*)
            printf '%s still contains a placeholder value\n' "${name}" >&2
            exit 1
            ;;
    esac
    if [ "${#value}" -lt "${minimum}" ]; then
        printf '%s must be set and contain at least %s characters\n' \
            "${name}" "${minimum}" >&2
        exit 1
    fi
}

require_secret LLAMA_API_KEY 20
require_secret WEBUI_SECRET_KEY 32
require_secret VOICE_API_KEY 20
require_secret TOOL_API_KEY 20

if [ "${LLAMA_API_KEY}" = "${VOICE_API_KEY}" ] \
    || [ "${LLAMA_API_KEY}" = "${TOOL_API_KEY}" ] \
    || [ "${VOICE_API_KEY}" = "${TOOL_API_KEY}" ]; then
    printf 'LLAMA_API_KEY, VOICE_API_KEY, and TOOL_API_KEY must be unique\n' >&2
    exit 1
fi

# Never forward infrastructure lifecycle credentials to application processes.
unset RUNPOD_API_KEY

data_root=${RUNPOD_DATA_ROOT:-/workspace/jarvis-data}
config_root=${RUNPOD_CONFIG_ROOT:-/workspace/jarvis-config}

install -d -o jarvis -g jarvis \
    "${data_root}/models" \
    "${data_root}/open-webui" \
    "${data_root}/voice-cache"
install -d -o root -g root "${data_root}/tailscale"
install -d -o jarvis -g jarvis "${config_root}"
install -d -o jarvis -g jarvis /run/jarvis-config /run/secrets/ssh
install -d -o root -g root /run/secrets/tailscale
install -d -o root -g root /run/tailscale

if [ ! -e /models ]; then
    ln -s "${data_root}/models" /models
fi

export MODEL_PATH=${MODEL_PATH:-${data_root}/models/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf}
export MODEL_ALIAS=${MODEL_ALIAS:-qwen3-coder-30b-a3b}
export HF_REPO_ID=${HF_REPO_ID:-unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF}
export HF_FILENAME=${HF_FILENAME:-Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf}
export LLAMA_CTX_SIZE=${LLAMA_CTX_SIZE:-16384}
export LLAMA_PARALLEL=${LLAMA_PARALLEL:-1}
export LLAMA_N_GPU_LAYERS=${LLAMA_N_GPU_LAYERS:-99}
export LLAMA_BATCH_SIZE=${LLAMA_BATCH_SIZE:-2048}
export LLAMA_UBATCH_SIZE=${LLAMA_UBATCH_SIZE:-512}
export LLAMA_CACHE_TYPE_K=${LLAMA_CACHE_TYPE_K:-q8_0}
export LLAMA_CACHE_TYPE_V=${LLAMA_CACHE_TYPE_V:-q8_0}
export LLAMA_FLASH_ATTN=${LLAMA_FLASH_ATTN:-on}

export DATA_DIR="${data_root}/open-webui"
export OPENAI_API_BASE_URLS=${OPENAI_API_BASE_URLS:-http://127.0.0.1:8080/v1}
export OPENAI_API_KEYS=${OPENAI_API_KEYS:-${LLAMA_API_KEY}}
export ENABLE_OLLAMA_API=false
export WEBUI_NAME=${WEBUI_NAME:-Jarvis}
export DEFAULT_USER_ROLE=${DEFAULT_USER_ROLE:-pending}
export AUDIO_STT_ENGINE=openai
export AUDIO_STT_OPENAI_API_BASE_URL=http://127.0.0.1:8000/v1
export AUDIO_STT_OPENAI_API_KEY=${VOICE_API_KEY}
export AUDIO_STT_MODEL=${STT_MODEL:-Systran/faster-whisper-small}
export AUDIO_TTS_ENGINE=openai
export AUDIO_TTS_OPENAI_API_BASE_URL=http://127.0.0.1:8000/v1
export AUDIO_TTS_OPENAI_API_KEY=${VOICE_API_KEY}
export AUDIO_TTS_MODEL=${TTS_MODEL:-speaches-ai/Kokoro-82M-v1.0-ONNX}
export AUDIO_TTS_VOICE=${TTS_VOICE:-af_heart}
export RAG_EMBEDDING_MODEL=${RAG_EMBEDDING_MODEL:-sentence-transformers/all-MiniLM-L6-v2}
export DEFAULT_MODELS=${DEFAULT_MODELS:-${MODEL_ALIAS}}
export WEBUI_AUTH=${WEBUI_AUTH:-true}
export ENABLE_SIGNUP=${ENABLE_SIGNUP:-false}
export ENABLE_API_KEYS=true
export WEBSOCKET_EVENT_CALLER_TIMEOUT=${WEBSOCKET_EVENT_CALLER_TIMEOUT:-120}
export SCARF_NO_ANALYTICS=true
export DO_NOT_TRACK=true
export ANONYMIZED_TELEMETRY=false

export API_KEY=${VOICE_API_KEY}
export PRELOAD_MODELS=${VOICE_PRELOAD_MODELS:-'["Systran/faster-whisper-small","speaches-ai/Kokoro-82M-v1.0-ONNX"]'}
export WHISPER__MODEL=${STT_MODEL:-Systran/faster-whisper-small}
export WHISPER__INFERENCE_DEVICE=${WHISPER_DEVICE:-cpu}
export WHISPER__COMPUTE_TYPE=${WHISPER_COMPUTE_TYPE:-int8}
if [ "${WHISPER__INFERENCE_DEVICE}" = "cpu" ]; then
    export UNSTABLE_ORT_OPTS__EXCLUDE_PROVIDERS=${ORT_EXCLUDE_PROVIDERS:-'["TensorrtExecutionProvider","CUDAExecutionProvider"]'}
else
    export UNSTABLE_ORT_OPTS__EXCLUDE_PROVIDERS=${ORT_EXCLUDE_PROVIDERS:-'["TensorrtExecutionProvider"]'}
fi
export STT_MODEL_TTL=${VOICE_MODEL_TTL:--1}
export TTS_MODEL_TTL=${VOICE_MODEL_TTL:--1}
export ENABLE_UI=false
export HF_HOME="${data_root}/voice-cache"

export TS_STATE_DIR="${data_root}/tailscale"
export TS_SOCKET=/run/tailscale/tailscaled.sock
tailscale_default_required=false
if [ -n "${TS_AUTHKEY:-}" ]; then
    umask 077
    printf '%s' "${TS_AUTHKEY}" > /run/secrets/tailscale/authkey
    export TS_AUTHKEY_FILE=/run/secrets/tailscale/authkey
    tailscale_default_required=true
    unset TS_AUTHKEY
elif [ -s "${TS_STATE_DIR}/tailscaled.state" ]; then
    tailscale_default_required=true
fi
export TAILSCALE_REQUIRED=${TAILSCALE_REQUIRED:-${tailscale_default_required}}
case "${TAILSCALE_REQUIRED}" in
    true|false) ;;
    *)
        printf 'TAILSCALE_REQUIRED must be true or false\n' >&2
        exit 1
        ;;
esac
export SSH_SOCKS5_HOST=127.0.0.1
export SSH_SOCKS5_PORT=1055
export SSH_PRIVATE_KEY_PATH=/run/secrets/ssh/id_ed25519
export SSH_TARGETS_FILE=/run/jarvis-config/ssh-targets.yaml
export SSH_KNOWN_HOSTS_FILE=/run/jarvis-config/ssh_known_hosts

if [ ! -f "${config_root}/ssh-targets.yaml" ]; then
    cp /opt/jarvis-defaults/ssh-targets.yaml "${config_root}/ssh-targets.yaml"
    chown jarvis:jarvis "${config_root}/ssh-targets.yaml"
fi
if [ ! -f "${config_root}/ssh_known_hosts" ]; then
    cp /opt/jarvis-defaults/ssh_known_hosts "${config_root}/ssh_known_hosts"
    chown jarvis:jarvis "${config_root}/ssh_known_hosts"
fi

if [ -n "${SSH_TARGETS_YAML_B64:-}" ]; then
    printf '%s' "${SSH_TARGETS_YAML_B64}" | base64 --decode \
        > "${config_root}/ssh-targets.yaml"
    chown jarvis:jarvis "${config_root}/ssh-targets.yaml"
fi
ln -sf "${config_root}/ssh-targets.yaml" /run/jarvis-config/ssh-targets.yaml

if [ -n "${SSH_KNOWN_HOSTS_B64:-}" ]; then
    printf '%s' "${SSH_KNOWN_HOSTS_B64}" | base64 --decode \
        > "${config_root}/ssh_known_hosts"
    chown jarvis:jarvis "${config_root}/ssh_known_hosts"
fi
ln -sf "${config_root}/ssh_known_hosts" /run/jarvis-config/ssh_known_hosts

umask 077
if [ -n "${SSH_PRIVATE_KEY_B64:-}" ]; then
    printf '%s' "${SSH_PRIVATE_KEY_B64}" | base64 --decode \
        > /run/secrets/ssh/id_ed25519
elif [ -f "${config_root}/id_ed25519" ]; then
    cp "${config_root}/id_ed25519" /run/secrets/ssh/id_ed25519
fi

if [ -f /run/secrets/ssh/id_ed25519 ]; then
    chmod 0400 /run/secrets/ssh/id_ed25519
    chown jarvis:jarvis /run/secrets/ssh/id_ed25519
else
    printf 'Warning: no SSH private key configured; SSH tools will remain unavailable.\n' >&2
fi

unset SSH_PRIVATE_KEY_B64 SSH_TARGETS_YAML_B64 SSH_KNOWN_HOSTS_B64

chown -h jarvis:jarvis \
    /run/jarvis-config/ssh-targets.yaml \
    /run/jarvis-config/ssh_known_hosts

exec /usr/bin/supervisord --nodaemon --configuration /etc/supervisor/supervisord.conf
