# syntax=docker/dockerfile:1.7

ARG CUDA_VERSION=12.8.1
ARG LLAMA_CPP_REF=b12316

FROM ghcr.io/ggml-org/llama.cpp:server-cuda12-${LLAMA_CPP_REF} AS llama

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu24.04 AS runtime

ARG HUGGINGFACE_HUB_VERSION=2.4.0

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        libcurl4 \
        libgomp1 \
        python3 \
        python3-venv \
    && python3 -m venv /opt/huggingface \
    && /opt/huggingface/bin/pip install --no-cache-dir \
        "huggingface-hub==${HUGGINGFACE_HUB_VERSION}" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin llama \
    && mkdir -p /models \
    && chown llama:llama /models \
    && rm -rf /var/lib/apt/lists/*

COPY --from=llama /app/ /opt/llama-cpp/
RUN test -x /opt/llama-cpp/llama-server \
    && ln -sf /opt/llama-cpp/llama-server /usr/local/bin/llama-server

COPY --chmod=0755 docker/llama/download-model.sh /usr/local/bin/download-model.sh
COPY --chmod=0755 docker/llama/entrypoint.sh /usr/local/bin/llama-entrypoint.sh
RUN for script in \
        /usr/local/bin/download-model.sh \
        /usr/local/bin/llama-entrypoint.sh; \
    do sh -n "${script}"; done

ENV HF_HOME=/models/.cache/huggingface \
    PATH=/opt/huggingface/bin:${PATH} \
    LD_LIBRARY_PATH=/opt/llama-cpp:/usr/local/nvidia/lib64:/usr/local/nvidia/lib:${LD_LIBRARY_PATH}

USER llama
WORKDIR /models
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/llama-entrypoint.sh"]