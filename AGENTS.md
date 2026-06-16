# AGENTS.md -- Odysseus

Mac = read/search/edit source. Do not run app/tooling on Mac.
Run app/build/test/runtime only on `winpc` via WSL Docker Engine.

## Paths

- Mac source: `odysseus_src/`
- WSL source: `/mnt/d/Projects/Ai/odysseus`
- Host: `winpc`
- WSL shell: `wsl -d Ubuntu -u root`
- URL: `https://edwin-work.taila70345.ts.net`
- Keep `APP_BIND=127.0.0.1`

Local `rg`, `sed`, `find`, `ls`, editors, and patches are OK for source inspection/editing.
Use SSH/WSL for app/build/test/runtime checks.

## Run

Use WSL Docker Engine only. No Docker Desktop. No `dev-stack.ps1`.

```bash
ssh -t winpc 'wsl -d Ubuntu -u root bash -lc "cd /mnt/d/Projects/Ai/odysseus && ./dev-stack.sh dev-session"'
```

Quick recreate, no keepalive:

```bash
ssh winpc 'wsl -d Ubuntu -u root bash -lc "cd /mnt/d/Projects/Ai/odysseus && ./dev-stack.sh dev"'
```

Check:

```bash
ssh winpc 'wsl -d Ubuntu -u root bash -lc "cd /mnt/d/Projects/Ai/odysseus && ./dev-stack.sh status --dev"'
ssh winpc 'wsl -d Ubuntu -u root bash -lc "docker logs --tail 80 odysseus-odysseus-1"'
ssh winpc 'wsl -d Ubuntu -u root bash -lc "curl -i --max-time 10 http://127.0.0.1:7000/ | head -20"'
```

Long rebuild:

```bash
ssh winpc 'wsl -d Ubuntu -u root bash -lc "cd /mnt/d/Projects/Ai/odysseus && nohup ./dev-stack.sh rebuild --scope All > /tmp/odys-deploy.log 2>&1 & echo started"'
ssh winpc 'wsl -d Ubuntu -u root bash -lc "tail -n 40 /tmp/odys-deploy.log"'
```

## Loop

- Hot reload: `./dev-stack.sh dev-session`.
- App source change: no rebuild.
- Capability source change: no rebuild when dev stack active.
- App image/runtime change: `./dev-stack.sh rebuild --scope App`.
- Compose/image/capability container change: `./dev-stack.sh rebuild --scope All`.
- Do not use `--scope All` for normal Odysseus app edits.
- Keep `APP_BIND=127.0.0.1`. If public URL 502, check WSL is running and mirrored networking is active.
- WSL net: `networkingMode=mirrored` in `%UserProfile%\.wslconfig`; apply with `wsl --shutdown`.
- WSL is not a production service host. For dev visibility, use `dev-session`.

Hot reload command:

```bash
uvicorn app:app --host 0.0.0.0 --port 7000 --reload
```

## Capabilities

- `pain-miner` and `stock-research` run only through Odysseus.
- Mac wrappers:
  - `/Users/edwin/Documents/Projects/pain-miner/pain-miner_src`
  - `/Users/edwin/Documents/Projects/stock-research/stock-research_src`
- WSL paths:
  - `/mnt/d/Projects/Ai/pain-miner`
  - `/mnt/d/Projects/Ai/stock-research`
- Shared worker files: `libs/capability_kit`, `integrations/capabilities`, `docker/capability-worker.*`.

## GPU

- RTX 3080 10GB. Budget about 9GB VRAM.
- Models: Gemma 3 12B IT GGUF Q4_K_M + EmbeddingGemma-300m Q8_0.
- If VRAM tight: context 8192 -> 4096, then embeddings CPU, then Q4_K_S. No partial 12B offload.
