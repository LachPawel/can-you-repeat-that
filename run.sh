#!/bin/bash
# ./run.sh arm | arm-fake | bridge | tunnel | vapi | record NAME [SECONDS] | play NAME
set -euo pipefail
cd "$(dirname "$0")"
BOOTH_PY="$HOME/code/reachy-booth/.booth-venv/bin/python"
case "${1:-help}" in
  arm)      exec "$BOOTH_PY" arm_server.py serve --port "${ARM_PORT:-/dev/tty.usbmodem5B415331661}" ;;
  arm-fake) exec "$BOOTH_PY" arm_server.py serve --fake ;;
  record)   exec "$BOOTH_PY" arm_server.py record "$2" --seconds "${3:-6}" ;;
  play)     exec "$BOOTH_PY" arm_server.py play "$2" ;;
  bridge)   exec .venv/bin/python bridge.py ;;
  tunnel)   exec cloudflared tunnel --url http://127.0.0.1:8787 2>&1 | tee tunnel.log ;;
  vapi)     exec .venv/bin/python vapi/setup.py ;;
  *)        sed -n 2p "$0" ;;
esac
