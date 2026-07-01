# bait_mcp

MCP server exposing ophyd devices for Bluesky/BITS instruments. The server
fronts an externally-running **OAS** (Ophyd-as-a-Service) WebSocket endpoint
and re-exposes its device read/write surface as two MCP tools (`read_device`,
`set_device`) for LLM agents.

Two processes by design: an asyncio MCP frontend and a synchronous ZMQ worker
that owns all OAS WebSocket I/O. Mirrors the layout of
`control_suite_mcp_aps_12id`.

## Prerequisites

An OAS server must be reachable. For the tomo-bits test bed:

```bash
# In the BITS conda env, with bait.ophyd_websocket.server importable
# (e.g. pip install -e /path/to/tomo-bait):
cd /Users/ecodrea/tomo_ws/tomo-bits
./scripts/tomo_2bm_oas_host.sh start
```

By default OAS listens on `ws://127.0.0.1:8002` — the same default
`bait_mcp` connects to.

## Setup

```bash
uv sync
```

Copy and edit the example configuration before running:

```bash
cp configs/example.yaml configs/local.yaml
```

YAML values are loaded first; explicit CLI flags override YAML for the
process they're passed to.

## Launch both processes

```bash
uv run bait-mcp \
  --config configs/example.yaml \
  --worker-endpoint tcp://127.0.0.1:5556 \
  --worker-startup-timeout-s 10 \
  --request-timeout-ms 30000 \
  --mcp-host 0.0.0.0 \
  --mcp-port 8051 \
  --mcp-path /mcp
```

With these defaults the MCP endpoint is:

```text
http://127.0.0.1:8051/mcp
```

## Debug launches

Worker only:

```bash
uv run bait-mcp-worker \
  --config configs/example.yaml \
  --bind tcp://127.0.0.1:5556
```

MCP frontend only:

```bash
uv run bait-mcp-server \
  --config configs/example.yaml \
  --worker tcp://127.0.0.1:5556 \
  --timeout-ms 30000 \
  --host 0.0.0.0 \
  --port 8051 \
  --path /mcp
```

## MCP client configuration

```json
{
  "mcpServers": {
    "bait_mcp": {
      "url": "http://127.0.0.1:8051/mcp",
      "transport": "http"
    }
  }
}
```

This is the shape `eaa_core.tool.mcp_client.MCPTool` expects — `bait_mcp` is
usable as an EAA tool out of the box.

## Tools exposed

| Tool | Purpose |
|---|---|
| `read_device(name, timeout=None)` | Subscribe, take the first value-bearing message, close. Returns `{ok, value, timestamp, connected, signal}` on success or `{ok: false, error}` on failure. |
| `set_device(name, value, timeout=None)` | Subscribe, set, wait for "Successfully set" confirmation, close. Returns `{ok, result}` or `{ok: false, error}`. Executes immediately — no HITL gate. |

`name` is whatever the OAS device registry knows the device by (populated
from the BITS `startup.py`). `value` is a number or string; component access
is not supported via the WebSocket protocol (use OAS REST `PUT /devices` for
that case).

## Important options

- `worker.endpoint` — ZMQ endpoint used between the MCP frontend and worker.
- `worker.request_timeout_ms` — frontend request timeout for worker calls.
- `worker.startup_timeout_s` — launcher health-check timeout before starting MCP.
- `mcp.host`, `mcp.port`, `mcp.path` — HTTP transport settings.
- `oas.url` — base URL of the OAS server (the worker appends `/api/v1/device-socket`).
- `oas.request_timeout_s` — default per-call OAS WebSocket timeout.

## Safety / HITL

The MCP server does **not** implement a human-in-the-loop approval gate for
writes. `set_device` is a real write the moment the tool is called. If your
agent needs confirmation before a device write, gate the tool invocation on
the client side. (Tomo-bait does this with `pending_writes` + `/chat/confirm`
in its bits agent.)
