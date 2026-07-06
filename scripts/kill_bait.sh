#!/usr/bin/env bash
# Kill any running bait_mcp background servers: the launcher and its children
# (MCP frontend, ZMQ worker) and the spawned vendored OAS.
#
# Scoped precisely on purpose. It matches ONLY:
#   - the OAS process:        python -m bait_mcp.ophyd_websocket.server
#   - the console entrypoints: .../bin/bait-mcp[-worker|-server]
# It deliberately does NOT match a bare "bait_mcp", so it never touches unrelated
# processes that merely run from bait_mcp/.venv (e.g. the VS Code mypy language
# server). SIGTERM is used so the launcher shuts its children down cleanly.
set -u

patterns=(
  'bait_mcp\.ophyd_websocket\.server'   # vendored OAS
  '/bin/bait-mcp'                        # launcher + bait-mcp-worker + bait-mcp-server
)

found=0
for pat in "${patterns[@]}"; do
  for pid in $(pgrep -f "$pat" 2>/dev/null || true); do
    echo "killing pid $pid: $(ps -p "$pid" -o command= | cut -c1-80)"
    kill "$pid" 2>/dev/null && found=1
  done
done

[ "$found" = 0 ] && echo "no bait_mcp background processes found"
exit 0
