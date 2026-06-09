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
# official NVIDIA CUDA devel image — a consistent toolkit that needs none of the
# wheel workarounds — and install it to /usr/local/bin. Cookbook's serve gate is
# `! command -v llama-server`, so a binary on PATH makes every Launch skip the
# build entirely: serving is instant and survives rebuilds (Ollama-style).
#
# ubuntu22.04 (glibc 2.35) is older than the python:3.14-slim runtime's glibc,
# so the binary stays forward-compatible there. CMAKE_CUDA_ARCHITECTURES is set
# explicitly because no GPU is visible at build time for autodetection (86 =
# Ampere / RTX 30-series; override LLAMA_CUDA_ARCH for other cards).
# ---------------------------------------------------------------------------
ARG LLAMA_CUDA=off

FROM nvidia/cuda:12.6.3-devel-ubuntu22.04 AS llama-builder
ARG LLAMA_CUDA_ARCH=86
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# ggml-cuda uses the CUDA driver API (cuMemMap, cuGetErrorString, ...) from
# libcuda. A devel image has no GPU/driver, only a stub at lib64/stubs. Symlink
# it into the standard lib dir (both libcuda.so for the linker and libcuda.so.1
# for the runtime loader) so CMake's FindCUDAToolkit resolves CUDA::cuda_driver,
# and force-link -lcuda (Ubuntu's default --as-needed would otherwise drop it,
# since the flag precedes the objects on the link line). Putting the stub dir on
# LD_LIBRARY_PATH lets build-time host tools (llama-ui-embed) that pick up the
# forced -lcuda still load — they never actually call CUDA. The stub's SONAME is
# libcuda.so.1, satisfied at runtime by the real driver the NVIDIA Container
# Toolkit injects; the stub itself is never copied into the runtime image.
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH}
RUN ln -sf stubs/libcuda.so /usr/local/cuda/lib64/libcuda.so \
    && ln -sf stubs/libcuda.so /usr/local/cuda/lib64/libcuda.so.1 \
    && git clone --depth 1 https://github.com/ggml-org/llama.cpp /opt/src/llama.cpp \
    && cmake -S /opt/src/llama.cpp -B /opt/src/llama.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DLLAMA_CURL=OFF \
        -DCMAKE_CUDA_ARCHITECTURES="${LLAMA_CUDA_ARCH}" \
        -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,--no-as-needed -lcuda -Wl,--as-needed" \
        -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,--no-as-needed -lcuda -Wl,--as-needed" \
    && cmake --build /opt/src/llama.cpp/build -j"$(nproc)" --target llama-server \
    && mkdir -p /opt/llama/bin /opt/llama/lib \
    && cp /opt/src/llama.cpp/build/bin/llama-server /opt/llama/bin/ \
    && cp -a /opt/src/llama.cpp/build/bin/*.so* /opt/llama/lib/ \
    && for _l in libcudart libcublas libcublasLt; do \
         cp -a /usr/local/cuda/lib64/$_l.so* /opt/llama/lib/ ; \
       done

# ggml-cuda links libnccl (NEEDED in the binary even on a single GPU, so it must
# load or llama-server won't start). NCCL ships in the distro libdir, not
# /usr/local/cuda/lib64, so the loop above misses it. Kept as its own layer so
# the expensive clone+compile RUN above stays cache-valid.
RUN cp -a /usr/lib/x86_64-linux-gnu/libnccl.so* /opt/llama/lib/

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
