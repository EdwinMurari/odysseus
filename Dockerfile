# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Optional CUDA llama.cpp build stage.
#
# Cookbook normally builds llama.cpp from source on the first llama.cpp serve,
# inside the container's ephemeral filesystem. That build is slow, fragile (pip
# CUDA-wheel version skew), and — critically — thrown away on every container
# recreate, so a GPU model has to re-earn its binary after each rebuild.
#
# When built with LLAMA_CUDA=on (the docker/gpu.nvidia.yml overlay sets this),
# we instead compile a CUDA llama-server here, at image-build time, using the
# official NVIDIA CUDA devel image — a consistent toolkit — and install it to
# /usr/local/bin. Cookbook's serve gate is `! command -v llama-server`, so a
# binary on PATH makes every Launch skip the build entirely: serving is instant
# and survives rebuilds (Ollama-style).
#
# Built on ubuntu22.04 (glibc 2.35), older than the python:3.14-slim runtime's
# glibc, so the binary stays forward-compatible there. No GPU is visible at
# build time, so the CUDA architecture is given explicitly (LLAMA_CUDA_ARCH=86
# = Ampere / RTX 30-series; override for other cards).
# ---------------------------------------------------------------------------
ARG LLAMA_CUDA=off

FROM nvidia/cuda:12.6.3-devel-ubuntu22.04 AS llama-builder
ARG LLAMA_CUDA_ARCH=86
# A current CMake from pip, not the distro's 3.22: only newer FindCUDAToolkit
# resolves the CUDA::cuda_driver target to the toolkit's bundled driver stub
# (lib64/stubs/libcuda.so) on a GPU-less builder. With 3.22 that target stays
# empty and ggml-cuda's VMM code fails to link (undefined cuMemMap, ...), which
# is what forced the old -lcuda/symlink workarounds.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential ca-certificates python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir cmake ninja

# Build a CUDA llama-server.
#   GGML_CUDA_NCCL=OFF — NCCL is multi-GPU collective comms; a single card never
#     uses it. The option defaults ON, which would link a libnccl.so.2 runtime
#     dependency for nothing, so we drop it at the source.
#   CUDA::cuda_driver — ggml-cuda's VMM memory pool uses the CUDA driver API
#     (cuMemMap, cuGetErrorString, ...). CMake links the toolkit's driver stub
#     (SONAME libcuda.so.1); the real driver is injected at runtime by the
#     NVIDIA Container Toolkit, so the stub is never shipped.
RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp /opt/src/llama.cpp \
    && cmake -S /opt/src/llama.cpp -B /opt/src/llama.cpp/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DGGML_CUDA_NCCL=OFF \
        -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DCMAKE_CUDA_ARCHITECTURES="${LLAMA_CUDA_ARCH}" \
    && cmake --build /opt/src/llama.cpp/build -j"$(nproc)" --target llama-server

# Assemble a self-contained bundle: the binary, ggml's own shared libs, and the
# exact CUDA toolkit libraries the binary transitively needs — discovered via
# ldd and copied with their full SONAME symlink chains, so this can never go
# stale the way a hand-maintained library list does. libcuda.so.1 (the GPU
# driver) resolves to "not found" on the builder and is therefore skipped; it is
# injected at runtime, not shipped.
RUN mkdir -p /opt/llama/bin /opt/llama/lib \
    && cp /opt/src/llama.cpp/build/bin/llama-server /opt/llama/bin/ \
    && find /opt/src/llama.cpp/build -name '*.so*' -exec cp -P -t /opt/llama/lib {} + \
    && for base in $(ldd /opt/llama/bin/llama-server /opt/llama/lib/*.so* \
                      | awk '/=> \/usr\/local\/cuda/ {print $3}' \
                      | xargs -r -n1 basename | sed 's/\.so\..*/.so/' | sort -u); do \
         cp -a /usr/local/cuda/lib64/${base}* /opt/llama/lib/ ; \
       done

# ---------------------------------------------------------------------------
# Base application image (the MIT-core slim image).
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS base

# System deps. tmux is required by Cookbook for background downloads/serves.
# openssh-client is required for Cookbook remote server tests, setup, probes,
# downloads, and serves from Docker installs.
# git/cmake are required when Cookbook builds llama.cpp on first llama.cpp
# launch inside Docker.
# nodejs/npm provide npx for the optional built-in Browser MCP server.
# gosu lets the entrypoint drop privileges cleanly so signals still reach
# uvicorn directly (no extra shell layer like `su`/`sudo` would add).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    git \
    nodejs \
    npm \
    tmux \
    openssh-client \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache). Optional extras (PyMuPDF AGPL, etc.)
# are opt-in so the default image stays MIT-core; see requirements-optional.txt.
ARG INSTALL_OPTIONAL=false
COPY requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then pip install --no-cache-dir -r requirements-optional.txt; fi

# Copy app code
COPY . .

# Create data directory (mount a volume here for persistence)
RUN mkdir -p data logs services/cache/search

# Entrypoint that drops to PUID/PGID (default 1000:1000) and repairs
# ownership on the bind-mounted /app/data and /app/logs. Without this,
# the container runs as root and writes root-owned files into host
# bind mounts — any later non-root run (or a host user trying to
# update them) silently fails on EPERM, breaking skill extraction,
# prefs persistence, mail attachments, etc.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 7000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7000"]

# ---------------------------------------------------------------------------
# Final image selector. `FROM llama-${LLAMA_CUDA}` resolves to llama-off
# (plain base, default) or llama-on (base + prebuilt CUDA llama-server). When
# off, BuildKit never builds llama-builder, so the default image neither pulls
# the NVIDIA toolkit nor bundles CUDA — keeping the slim/MIT promise intact.
# ---------------------------------------------------------------------------
FROM base AS llama-off

FROM base AS llama-on
# Prebuilt CUDA llama-server + its shared libs (libggml*, libcudart/cublas).
# LD_LIBRARY_PATH points the loader at them; the GPU driver's libcuda.so is
# injected at runtime by the NVIDIA Container Toolkit. command -v finds the
# binary on PATH, so Cookbook skips its from-source build.
COPY --from=llama-builder /opt/llama/bin/llama-server /usr/local/bin/llama-server
COPY --from=llama-builder /opt/llama/lib/ /opt/llama/lib/
ENV LD_LIBRARY_PATH=/opt/llama/lib:${LD_LIBRARY_PATH}

FROM llama-${LLAMA_CUDA} AS final
