#!/usr/bin/env bash
# dev-stack.sh — Odysseus deploy on the WSL Docker engine (Linux-native).
# Mirrors dev-stack.ps1 against Docker Engine in WSL. No Docker Desktop, no session.
#   cd /mnt/d/Projects/Ai/odysseus && ./dev-stack.sh dev
# Actions: up|dev|rebuild|restart|status|logs|down  Options: --scope App|All --gpu nvidia|amd|none --dev --no-capabilities --no-limits --tail N
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

ACTION="${1:-up}"; shift || true
SCOPE="App"; GPU="nvidia"; CAPS=1; LIMITS=1; DEV=0; TAIL=120
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) SCOPE="$2"; shift 2;;
    --gpu) GPU="$2"; shift 2;;
    --dev) DEV=1; shift;;
    --no-capabilities) CAPS=0; shift;;
    --no-limits) LIMITS=0; shift;;
    --tail) TAIL="$2"; shift 2;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

[[ "$ACTION" == "dev" ]] && DEV=1

FILES=(-f docker-compose.yml)
case "$GPU" in
  nvidia) FILES+=(-f docker/gpu.nvidia.yml);;
  amd)    FILES+=(-f docker/gpu.amd.yml);;
  none)   ;;
  *) echo "bad --gpu: $GPU" >&2; exit 2;;
esac
[[ "$CAPS" -eq 1 ]] && FILES+=(-f docker-compose.capabilities.example.yml)
[[ "$DEV" -eq 1 ]] && FILES+=(-f docker-compose.dev.yml)
if [[ "$LIMITS" -eq 1 ]]; then
  FILES+=(-f docker/limits.yml)
  [[ "$CAPS" -eq 1 ]] && FILES+=(-f docker/limits.capabilities.yml)
fi

# dev-stack owns the compose file list; mask .env COMPOSE_FILE so Windows/WSL
# separators or stale overlay choices cannot change this script's runtime shape.
dc() { COMPOSE_FILE= docker compose "${FILES[@]}" "$@"; }
on_off() { [[ "$1" -eq 1 ]] && echo On || echo Off; }
echo "==> GPU: $GPU | Capabilities: $(on_off $CAPS) | Dev: $(on_off $DEV) | Limits: $(on_off $LIMITS) | Action: $ACTION | Scope: $SCOPE"
echo "==> Validating effective compose config"
dc config --quiet

case "$ACTION" in
  up) dc up -d --remove-orphans;;
  dev) dc up -d --force-recreate --remove-orphans;;
  rebuild)
    if [[ "$SCOPE" == "All" ]]; then
      dc up -d --build --force-recreate --remove-orphans
    else
      dc up -d --remove-orphans
      dc build odysseus
      dc up -d --force-recreate --no-deps odysseus
    fi;;
  restart) dc restart;;
  status)  dc ps;;
  logs)    dc logs --tail "$TAIL" -f;;
  down)    dc down;;
  *) echo "unknown action: $ACTION" >&2; exit 2;;
esac
echo "==> Done."
