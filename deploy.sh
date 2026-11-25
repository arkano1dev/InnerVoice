#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-up}" in
  up) exec ./bin/innervoice start ;;
  down) exec ./bin/innervoice stop ;;
  up-bot)
    ./bin/innervoice config set deployment.telegram=true
    exec ./bin/innervoice restart ;;
  up-whisper) exec ./bin/innervoice worker-only ;;
  logs) shift; exec ./bin/innervoice logs "$@" ;;
  *) echo "Usage: $0 {up|down|up-bot|up-whisper|logs [service]}" >&2; exit 2 ;;
esac
