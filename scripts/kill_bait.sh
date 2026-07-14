#!/usr/bin/env bash
# Kill any running bait_mcp servers: the launcher and the MCP frontend.
#
# Scoped precisely on purpose. It matches ONLY the console entrypoints
# (.../bin/bait-mcp[-server]), never a bare "bait_mcp", so it does not touch
# unrelated processes that merely run from bait_mcp/.venv (e.g. a language
# server). SIGTERM is used so the launcher shuts its child down cleanly.
set -u

found=0
for pid in $(pgrep -f '/bin/bait-mcp' 2>/dev/null || true); do
  echo "killing pid $pid: $(ps -p "$pid" -o command= | cut -c1-80)"
  kill "$pid" 2>/dev/null && found=1
done

[ "$found" = 0 ] && echo "no bait_mcp processes found"
exit 0
