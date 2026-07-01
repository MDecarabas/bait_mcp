# bait_mcp

MCP server exposing ophyd devices for Bluesky/BITS instruments. The server
re-exposes an **OAS** (Ophyd-as-a-Service) device read/write surface as two MCP
tools (`read_device`, `set_device`) for LLM agents.

OAS is **vendored in this package** (`src/bait_mcp/ophyd_websocket/`), so a
single `bait-mcp` invocation brings up everything: it spawns OAS against your
BITS package's `startup.py`, then the worker, then the MCP frontend. bait_mcp is
a **frontend to a BITS instrument** — it requires an installed BITS package and
refuses to start without one. There is no dependency on tomo-bait or any one
instrument; it works with *any* BITS package you install and select.

Two processes by design: an asyncio MCP frontend and a synchronous ZMQ worker
that owns all OAS WebSocket I/O. Mirrors the layout of
`control_suite_mcp_aps_12id`.

## Prerequisites

bait_mcp needs an **installed, importable BITS package** in the environment you
launch from. It does not ship devices of its own and has no sim/standalone mode:
the devices it exposes are exactly the ones that package's `startup.py` loads
into its `oregistry` (the BITS/Guarneri device registry). Those registered names
are what you pass to `read_device` / `set_device`.

At launch the vendored OAS runs `startup.py` in full — the same file the
queue server runs — so anything it needs (EPICS access, catalog config) applies.
A device that fails to connect still loads; a `startup.py` that fails to import
means bait_mcp fails, by design.

**Environment requirement:** the vendored OAS runs under the *same* interpreter
as `bait-mcp` (`python -m bait_mcp.ophyd_websocket.server`). The launcher
resolves `<package>/startup.py` by importing the package name, so the BITS
package (and its dependencies, e.g. `apsbits`) must be importable there. This
almost always means launching from the conda env that has your instrument —
see [Setup](#setup).

## Setup

```bash
uv sync
```

Because a BITS package must be importable at launch, install bait_mcp into the
conda env that already has your instrument package (and EPICS/ophyd support):

```bash
conda run -n <your_env> pip install -e .
```

Copy the example configuration and edit it before running:

```bash
cp configs/example.yaml configs/local.yaml
```

YAML values are loaded first; explicit CLI flags override YAML for the
process they're passed to.

## Point it at your instrument

Set `bits.package` to the **importable name** of your BITS package. The launcher
resolves `<package>/startup.py` from it and spawns OAS against that file, so the
exposed devices are exactly what `startup.py` registers. The package must be
installed in the launch environment — if it is not importable, bait_mcp refuses
to start (there is no fallback):

```yaml
bits:
  package: "my_beamline"   # importable BITS package name (not a path)
```

Override at launch without editing YAML:

```bash
uv run bait-mcp --config configs/local.yaml --bits-package my_beamline
```

## Launch (OAS + worker + frontend)

One command brings up all three processes (OAS is always started from the
selected BITS package):

```bash
uv run bait-mcp \
  --config configs/local.yaml \
  --bits-package my_beamline \
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
  --config configs/local.yaml \
  --bind tcp://127.0.0.1:5556
```

MCP frontend only:

```bash
uv run bait-mcp-server \
  --config configs/local.yaml \
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

`name` is whatever the OAS device registry knows the device by — i.e. the name
your BITS package's `startup.py` registers it under in the `oregistry`. `value`
is a number or string; component access is not supported via the WebSocket
protocol (use OAS REST `PUT /devices` for that case).

## Important options

- `worker.endpoint` — ZMQ endpoint used between the MCP frontend and worker.
- `worker.request_timeout_ms` — frontend request timeout for worker calls.
- `launcher.worker_startup_timeout_s` — launcher health-check timeout before starting MCP.
- `launcher.shutdown_timeout_s` — grace period before children are killed on shutdown.
- `mcp.host`, `mcp.port`, `mcp.path` — HTTP transport settings.
- `oas.host`, `oas.port` — where the launcher binds the vendored OAS (keep in sync with `oas.url`).
- `oas.url` — base URL the worker connects to (it appends `/api/v1/device-socket`).
- `oas.request_timeout_s` — default per-call OAS WebSocket timeout.
- `bits.package` — importable BITS package name; the launcher loads `<package>/startup.py`. Override with `--bits-package`.

## Safety / HITL

The MCP server does **not** implement a human-in-the-loop approval gate for
writes. `set_device` is a real write the moment the tool is called. If your
agent needs confirmation before a device write, gate the tool invocation on the
client side.
