# syntax=docker/dockerfile:1.7
#
# RunPod GPU Pods run one container and do not support Docker Compose. This
# image packages the same services under supervisord for that supported model.
# llama-server is taken from the official CUDA image so CI does not compile nvcc.

ARG CUDA_VERSION=12.8.1
ARG LLAMA_CPP_REF=b12316
ARG TAILSCALE_VERSION=v1.104.4

FROM ghcr.io/ggml-org/llama.cpp:server-cuda12-${LLAMA_CPP_REF} AS llama

FROM tailscale/tailscale:${TAILSCALE_VERSION} AS tailscale

FROM nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu24.04 AS runtime

ARG OPEN_WEBUI_VERSION=0.13.3
ARG SPEACHES_REF=v0.9.0-rc.3
ARG HUGGINGFACE_HUB_VERSION=2.4.0

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        gcc \
        git \
        jq \
        libcurl4 \
        libgomp1 \
        libmariadb-dev \
        libsm6 \
        libsndfile1 \
        libxext6 \
        netcat-openbsd \
        pandoc \
        python3 \
        python3-venv \
        supervisor \
        tini \
    && useradd --create-home --uid 10001 --shell /bin/bash jarvis \
    && rm -rf /var/lib/apt/lists/*

COPY --from=llama /app/ /opt/llama-cpp/
RUN test -x /opt/llama-cpp/llama-server \
    && ln -sf /opt/llama-cpp/llama-server /usr/local/bin/llama-server

COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

# Isolated environments prevent the UI, voice, downloader, and tool service
# dependency sets from changing one another.
RUN python3 -m venv /opt/open-webui \
    && /opt/open-webui/bin/pip install --no-cache-dir \
        "torch<=2.9.1" torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/cpu \
    && /opt/open-webui/bin/pip install --no-cache-dir \
        "open-webui==${OPEN_WEBUI_VERSION}" \
    && python3 -m venv /opt/huggingface \
    && /opt/huggingface/bin/pip install --no-cache-dir \
        "huggingface-hub==${HUGGINGFACE_HUB_VERSION}"

RUN git clone --branch "${SPEACHES_REF}" --depth 1 \
        https://github.com/speaches-ai/speaches.git /opt/speaches \
    && cd /opt/speaches \
    && UV_PYTHON=/usr/bin/python3.12 UV_PYTHON_DOWNLOADS=never \
        uv sync --frozen --compile-bytecode --no-dev \
    && rm -rf /root/.cache/uv /opt/speaches/.git

COPY tools/requirements.txt /tmp/tools-requirements.txt
RUN python3 -m venv /opt/jarvis-tools \
    && /opt/jarvis-tools/bin/pip install --no-cache-dir \
        --requirement /tmp/tools-requirements.txt \
    && rm /tmp/tools-requirements.txt

COPY tools/app.py tools/scan_host_key.py /opt/jarvis-tools-app/
COPY docker/runpod/defaults/ssh-targets.yaml docker/runpod/defaults/ssh_known_hosts /opt/jarvis-defaults/
RUN /opt/jarvis-tools/bin/python -m py_compile \
        /opt/jarvis-tools-app/app.py \
        /opt/jarvis-tools-app/scan_host_key.py \
    && rm -rf /opt/jarvis-tools-app/__pycache__
COPY --chmod=0755 docker/llama/download-model.sh /usr/local/bin/download-model.sh
COPY --chmod=0755 docker/llama/entrypoint.sh /usr/local/bin/llama-entrypoint.sh
COPY --chmod=0755 docker/runpod/entrypoint.sh /usr/local/bin/runpod-entrypoint.sh
COPY --chmod=0755 docker/runpod/tailscale-up.sh /usr/local/bin/tailscale-up.sh
COPY --chmod=0755 docker/runpod/healthcheck.sh /usr/local/bin/jarvis-healthcheck.sh
COPY --chmod=0755 docker/runpod/scan-host-key.sh /usr/local/bin/scan-host-key-runpod
COPY docker/runpod/supervisord.conf /etc/supervisor/conf.d/jarvis.conf
RUN for script in \
        /usr/local/bin/download-model.sh \
        /usr/local/bin/llama-entrypoint.sh \
        /usr/local/bin/runpod-entrypoint.sh \
        /usr/local/bin/tailscale-up.sh \
        /usr/local/bin/jarvis-healthcheck.sh \
        /usr/local/bin/scan-host-key-runpod; \
    do sh -n "${script}"; done

RUN mkdir -p \
        /run/jarvis-config \
        /run/secrets/ssh \
        /run/tailscale \
        /workspace/jarvis-data \
        /workspace/jarvis-config \
    && chown -R jarvis:jarvis \
        /opt/jarvis-tools-app \
        /workspace/jarvis-data \
        /workspace/jarvis-config

ENV PATH=/opt/huggingface/bin:${PATH} \
    LD_LIBRARY_PATH=/opt/llama-cpp:/usr/local/nvidia/lib64:/usr/local/nvidia/lib:${LD_LIBRARY_PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RUNPOD_DATA_ROOT=/workspace/jarvis-data \
    RUNPOD_CONFIG_ROOT=/workspace/jarvis-config \
    RUNPOD_UI_PORT=3000

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30m --retries=5 \
    CMD ["/usr/local/bin/jarvis-healthcheck.sh"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/runpod-entrypoint.sh"]