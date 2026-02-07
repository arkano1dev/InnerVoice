#!/bin/bash
# InnerVoice deployment script
set -e

case "${1:-up}" in
  up)
    docker compose up -d --build
    ;;
  down)
    docker compose down
    ;;
  up-bot)
    docker compose up -d --build bot
    ;;
  up-whisper)
    docker compose up -d --build whisper
    ;;
  logs)
    docker compose logs -f "${2:-}"
    ;;
  *)
    echo "Usage: $0 {up|down|up-bot|up-whisper|logs [service]}"
    exit 1
    ;;
esac
