# syntax=docker/dockerfile:1.7

ARG CUDA_VERSION=12.8.1

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS builder

ARG LLAMA_CPP_REF=b12316

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        libcurl4-openssl-dev \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --filter=blob:none https://github.com/ggml-org/llama.cpp.git \
    && cd llama.cpp \
    && git checkout "${LLAMA_CPP_REF}" \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_CUDA=ON \
        -DGGML_NATIVE=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_TOOLS=ON \
        -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_BUILD_APP=OFF \
    && cmake --build build --config Release --target llama-server --parallel "$(nproc)" \
    && strip build/bin/llama-server

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

COPY --from=builder /src/llama.cpp/build/bin/llama-server /usr/local/bin/llama-server
COPY --chmod=0755 docker/llama/download-model.sh /usr/local/bin/download-model.sh
COPY --chmod=0755 docker/llama/entrypoint.sh /usr/local/bin/llama-entrypoint.sh
RUN for script in \
        /usr/local/bin/download-model.sh \
        /usr/local/bin/llama-entrypoint.sh; \
    do sh -n "${script}"; done

ENV HF_HOME=/models/.cache/huggingface \
    PATH=/opt/huggingface/bin:${PATH}

USER llama
WORKDIR /models
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/llama-entrypoint.sh"]
