# AGENTS.md -- Odysseus source

## Runtime

- Mac edits only. Do not build, test, run, or deploy on Mac.
- Runtime host: `winpc`; WSL shell: `wsl -d Ubuntu -u root`.
- WSL repo: `/mnt/d/Projects/Ai/odysseus`.
- Deploy entrypoint: `./dev-stack.sh` in WSL Docker Engine. Do not use Docker Desktop or `dev-stack.ps1` over SSH.
- App URL: `https://edwin-work.taila70345.ts.net`; keep `APP_BIND=127.0.0.1`.

Detached deploy from Mac:

```bash
ssh winpc 'wsl -d Ubuntu -u root bash -lc "cd /mnt/d/Projects/Ai/odysseus && nohup ./dev-stack.sh rebuild --scope All > /tmp/odys-deploy.log 2>&1 & echo started"'
ssh winpc 'wsl -d Ubuntu -u root bash -lc "tail -n 40 /tmp/odys-deploy.log"'
```

`dev-stack.sh`: `up | rebuild | restart | status | logs | down`.
Use `--scope App` for Odysseus-only code; use `--scope All` after Docker, dependency, Compose, or capability-repo changes.

## Capabilities

- `pain-miner` and `stock-research` are Odysseus capabilities, not standalone apps to run independently.
- Mac sibling wrappers point to the same SMB sources:
  - `/Users/edwin/Documents/Projects/pain-miner/pain-miner_src` -> `/Volumes/Projects/Ai/pain-miner`
  - `/Users/edwin/Documents/Projects/stock-research/stock-research_src` -> `/Volumes/Projects/Ai/stock-research`
- WSL paths expected by Odysseus Compose:
  - `/mnt/d/Projects/Ai/pain-miner`
  - `/mnt/d/Projects/Ai/stock-research`
- Capability workers are built and run only through Odysseus `dev-stack.sh` with the capabilities overlay.

## Hardware

- Host GPU: RTX 3080 10GB; target usable VRAM is about 9GB because the desktop shares the card.
- Serve exactly two GPU models unless measured otherwise: chat Gemma 3 12B IT GGUF Q4_K_M plus EmbeddingGemma-300m Q8_0.
- If VRAM is high, prefer: chat context 8192 -> 4096, then move embeddings to CPU, then Q4_K_S chat weights. Do not partially offload the 12B model.

## References

- Capability architecture: `docs/capabilities.md`.
- Runtime wrapper: `dev-stack.sh`.
