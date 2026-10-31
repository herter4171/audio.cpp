#!/usr/bin/env bash
# Start the MiniMax Music 3 hot model server + web UI.
#
#   1) audiocpp_server  : keeps the model loaded between requests (hot).
#                        Config: music3_webui/server_config.json
#   2) web UI (python)  : simple lyrics + description page, 0.0.0.0:8321
#
# Usage:
#   ./run.sh                     # foreground; Ctrl-C stops what it started
#   nohup ./run.sh >run.log 2>&1 &   # keep it running in the background
#
# Re-running while things are already up is a no-op (ports are checked).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_BIN="$ROOT/build/bin/audiocpp_server"
CONFIG="$ROOT/music3_webui/server_config.json"
LOG_DIR="$ROOT/music3_webui/logs"
mkdir -p "$LOG_DIR"

HOT_PORT=8080   # must match music3_webui/server_config.json
UI_PORT="${UI_PORT:-8321}"
HOT_PID=""
UI_PID=""

[[ -x "$SERVER_BIN" ]] || { echo "error: $SERVER_BIN missing - run ./build.sh first" >&2; exit 1; }
[[ -f "$CONFIG" ]]     || { echo "error: $CONFIG missing" >&2; exit 1; }

cleanup() {
  local p
  for p in $HOT_PID $UI_PID; do kill "$p" 2>/dev/null || true; done
}
trap cleanup INT TERM

# --- 1) hot model server -------------------------------------------------------
if curl -sf "http://127.0.0.1:$HOT_PORT/health" >/dev/null 2>&1; then
  echo "hot server already running on :$HOT_PORT (leaving it alone)"
else
  echo "starting audiocpp_server (model loads at startup, ~30-60s)..."
  "$SERVER_BIN" --config "$CONFIG" --host 0.0.0.0 --port "$HOT_PORT" \
    --log --log-file "$LOG_DIR/server.log" &
  HOT_PID=$!
  ok=0
  for _ in $(seq 1 180); do
    if curl -sf "http://127.0.0.1:$HOT_PORT/health" 2>/dev/null | grep -q '"status":"ok"'; then
      ok=1; break
    fi
    if ! kill -0 "$HOT_PID" 2>/dev/null; then
      echo "error: server process died - see $LOG_DIR/server.log" >&2
      exit 1
    fi
    sleep 1
  done
  [[ $ok -eq 1 ]] || { echo "error: server did not become healthy in 180s - see $LOG_DIR/server.log" >&2; exit 1; }
  echo "hot server ready: $(curl -s http://127.0.0.1:$HOT_PORT/health)"
fi

# --- 2) web UI ------------------------------------------------------------------
if curl -sf "http://127.0.0.1:$UI_PORT/api/status" >/dev/null 2>&1; then
  echo "web UI already running on :$UI_PORT (leaving it alone)"
else
  HOT_SERVER="http://127.0.0.1:$HOT_PORT" PORT="$UI_PORT" \
    python3 "$ROOT/music3_webui/server.py" >> "$LOG_DIR/ui.log" 2>&1 &
  UI_PID=$!
  for _ in $(seq 1 20); do
    curl -sf "http://127.0.0.1:$UI_PORT/api/status" >/dev/null 2>&1 && break
    sleep 0.5
  done
  echo "web UI ready"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "  Web UI : http://$IP:$UI_PORT"
echo "  API    : http://$IP:$HOT_PORT/health"
echo "  Logs   : $LOG_DIR/server.log  $LOG_DIR/ui.log"
echo "Ctrl-C stops the processes started by this script."

# Keep the script alive while what it started keeps running (foreground mode).
if [[ -n "$HOT_PID" || -n "$UI_PID" ]]; then
  wait $HOT_PID $UI_PID 2>/dev/null || true
else
  echo "nothing was started (both already running); staying idle."
  exec sleep infinity
fi
