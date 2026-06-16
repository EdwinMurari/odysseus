@C:\Users\murar\.codex\RTK.md

## Mac ↔ Windows workflow (read first)

Development happens on a MacBook Air, but the app **only runs on the Windows 11
PC** (`winpc`, RTX 3080). There is exactly one copy of the code, and it lives on
the PC.

- **Files / single source of truth.** The Windows repo is SMB-mounted on the Mac
  at `/Volumes/Projects/Ai/odysseus` and symlinked to
  `/Users/edwin/Documents/Projects/odysseus`. Editing either path writes the same
  Windows files — no second clone, no sync step.
- **Never build, run, or deploy on the Mac.** Docker and the GPU exist only on
  Windows. An agent running on the Mac must execute every build/run/deploy over
  SSH on `winpc`, e.g.:
  ```bash
  ssh winpc "cd D:/Projects/Ai/odysseus; .\dev-stack.ps1 rebuild"
  ```
  (`winpc` = the PC's Tailscale SSH alias; adjust the path if your share root
  differs.)
- **Canonical deploy is `dev-stack.ps1` (Docker)** — see "Odysseus runtime"
  below. Do **not** use `launch-windows.ps1` or a bare `docker compose` command
  for the GPU deployment.
- **Access the running app from the Mac** at the tailnet HTTPS hostname:
  `https://edwin-work.taila70345.ts.net`. Tailscale Serve on the PC terminates
  HTTPS and reverse-proxies to the loopback Docker app (`127.0.0.1:7000`). Keep
  `APP_BIND=127.0.0.1`; do not bind the container to `0.0.0.0` or the LAN.
- **Network.** SSH (22) and SMB (445) are firewalled to the tailnet
  (`100.64.0.0/10`) only; nothing is exposed to the LAN or the internet.

Daily loop from the Mac:

```bash
ssh winpc "cd D:/Projects/Ai/odysseus; .\dev-stack.ps1 up"        # start (existing images)
ssh winpc "cd D:/Projects/Ai/odysseus; .\dev-stack.ps1 rebuild"   # after app code changes
ssh winpc "cd D:/Projects/Ai/odysseus; .\dev-stack.ps1 logs"      # snapshot logs
```

## Odysseus runtime

Use `dev-stack.ps1` as the canonical Windows development entry point. Do not
invent a second Compose command or run the native launcher when validating the
Docker deployment.

The local machine has an NVIDIA RTX 3080. The default `-Gpu Auto` mode selects
`docker/gpu.nvidia.yml`, which builds the CUDA `llama-server` for architecture
86 and verifies GPU passthrough after startup.

Commands:

```powershell
# First start or ordinary start with existing images
.\dev-stack.ps1 up

# After changing Odysseus application code
.\dev-stack.ps1 rebuild

# After changing Dockerfiles, Compose files, dependencies, or capability repos
.\dev-stack.ps1 rebuild -Scope All

# Configuration-only restart; does not rebuild code copied into an image
.\dev-stack.ps1 restart

.\dev-stack.ps1 status
# Bounded snapshot; exits after printing the latest 200 lines
.\dev-stack.ps1 logs
# Explicit live stream; runs until Ctrl+C
.\dev-stack.ps1 logs -Follow
.\dev-stack.ps1 down
```

Capability workers are enabled automatically only when all of these exist:

- `data/capabilities.yaml`
- sibling repository `../pain-miner`
- sibling repository `../stock-research`

Override detection with `-Capabilities On` or `-Capabilities Off`. Never use
`docker compose down -v`; the named volumes and bind-mounted `data/`,
`logs/`, Hugging Face cache, and installed Cookbook engines must survive
rebuilds.

After startup, require all of the following:

1. `docker compose config --quiet` succeeds.
2. `GET http://127.0.0.1:<APP_PORT>/api/ready` returns `ready: true`.
3. For NVIDIA, `nvidia-smi -L` succeeds inside the `odysseus` container.
4. If startup fails, inspect `.\dev-stack.ps1 logs` before changing code.

## Hardware profile (this machine)

- GPU: RTX 3080 **10GB** (Ampere, `LLAMA_CUDA_ARCH=86`). It also drives the
  Windows desktop, so treat ~9GB as the usable VRAM budget, not 10.
- RAM: 32GB. WSL2 must be capped at 20GB via `%USERPROFILE%\.wslconfig`
  (template: `docker/wslconfig.example`; `dev-stack.ps1` warns when the file
  is missing). Without the cap, WSL2 balloons and starves Windows.
- Per-container memory ceilings live in `docker/limits.yml` and
  `docker/limits.capabilities.yml`. `dev-stack.ps1` applies them
  automatically (`-Limits Off` only for OOM debugging). They are safety
  nets, not tuning knobs — do not lower the `odysseus` ceiling below 12g;
  llama-server's mmapped GGUF pages count against the cgroup.

## Model serving plan (RTX 3080 10GB — do not change without measuring)

Exactly two models share the GPU. Never serve a third model, a second chat
model, or a draft model alongside them.

| Role | Model | VRAM |
|------|-------|------|
| Chat | Gemma 3 12B IT, GGUF **Q4_K_M** (e.g. `ggml-org/gemma-3-12b-it-GGUF`) | ~7.3GB |
| Embeddings | EmbeddingGemma-300m, GGUF QAT Q8_0 (`ggml-org/embeddinggemma-300m-qat-q8_0-GGUF`) | ~0.4GB |

Chat llama-server settings: full offload (`-ngl 999`), context **8192**,
quantized KV cache (`--cache-type-k q8_0 --cache-type-v q8_0`; requires
flash attention — recent llama.cpp enables it automatically, otherwise pass
`-fa`), `--parallel 1`. Embedding server: a separate llama-server with
`--embeddings`, context 2048, on its own port.

Budget at these settings: 7.3 weights + ~0.5 KV + ~0.5 compute buffers +
~0.4 embeddings ≈ **8.7GB**, leaving only a few hundred MB of headroom.
After every (re)serve, run `nvidia-smi` inside the container and confirm
total VRAM use stays under ~9.2GB. If it does not, apply this ladder in
order and re-measure after each step:

1. Drop chat context 8192 → 4096.
2. Move EmbeddingGemma to CPU (`-ngl 0`; a 300m embedder is fast on CPU).
3. Switch chat weights Q4_K_M → Q4_K_S.

Never resolve VRAM pressure by partially offloading the 12B model
(`-ngl` below full): CPU/GPU split inference is drastically slower than any
step above. Do not use `google/gemma-3-12b-it-qat-q4_0-gguf` on this card:
its Q4_0 file keeps fp16 token embeddings (8.07GB on disk) and leaves no
room for the KV cache in 10GB.
