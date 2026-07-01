# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

This repo is an MCP server that exposes ophyd devices over MCP via an OAS (Ophyd-as-a-Service) WebSocket endpoint. The MCP frontend/worker structure intentionally mirrors `control_suite_mcp_aps_12id` — same layout, same protocol/zmq_client/launcher pattern. OAS itself is **vendored in this package** (`src/bait_mcp/ophyd_websocket/`, see "OAS is vendored here") and the launcher can start it, so a single `bait-mcp` invocation brings up OAS + worker + frontend.

- **MCP frontend** (`mcp_server.py`): asyncio FastMCP HTTP server. Exposes two tools (`read_device`, `set_device`). Holds no ophyd state. Each tool call forwards to the worker via `WorkerClient.request`, wrapped in `asyncio.to_thread` so the synchronous ZMQ I/O does not stall the event loop.
- **Ophyd worker** (`worker.py`): synchronous, single-threaded ZMQ REP server. Owns the OAS WebSocket protocol — opens a short-lived `websockets.sync.client` connection per call, subscribes, reads/sets, closes. Processes one ZMQ request at a time.
- **Launcher** (`launcher.py`): when `oas.auto_start` is set, first resolves the selected instrument's OAS startup file (from `bits.package` / `bits.packages`), spawns the vendored OAS (`python -m bait_mcp.ophyd_websocket.server` on `oas.port`, with `OAS_STARTUP_DIR` set so its device registry auto-loads), and waits via `wait_for_oas` (polls `/api/v1/devices`, falling back to `POST /load-devices`). Then spawns the worker, polls `health` until ready (`wait_for_worker`), then the MCP frontend. Forwards SIGINT/SIGTERM to all children with a bounded shutdown timeout.
- **Wire protocol** (`protocol.py`): JSON over ZMQ REQ/REP. `{method, params}` request, `{status: ok|error, result|error}` response. `WorkerClient` creates a fresh REQ socket per call (REQ/REP is strict lock-step; sharing across calls would deadlock on out-of-order replies or timeouts).

### Why two processes here

In `control_suite_mcp_aps_12id` the two-process split is load-bearing because the underlying control stack is genuinely blocking and not asyncio-friendly. Here the split is **not justified by blocking I/O** — `websockets.sync.client` calls are network I/O that could live inside the asyncio event loop. The split is kept for two reasons:

1. **Structural consistency with control_suite_mcp_aps_12id** — operators and tooling treat both MCP servers the same way.
2. **Future extensibility** — if we add MCP tools that *do* need to drive an ophyd session in-process (instead of via OAS), the worker is already the right place for them.

If neither rationale survives, this whole package can collapse to a single FastMCP process with the WS client inline. Don't do that without checking.

### OAS is vendored here

The OAS server lives in this package at `src/bait_mcp/ophyd_websocket/` (vendored from the upstream ophyd-websocket copy, which is unpublished). It runs as `python -m bait_mcp.ophyd_websocket.server --startup-dir <file>`. Operationally:

- When `oas.auto_start` is true, the launcher spawns it automatically on `oas.host:oas.port` (default `127.0.0.1:8002`) against the startup file resolved from `bits.packages[bits.package]`. To point at an already-running OAS instead, set `oas.auto_start: false` and `oas.url` to that server.
- The worker connects to `oas.url` + `/api/v1/device-socket` and speaks the OAS device-socket protocol (subscribe/set/unsubscribe). The device the MCP reads/sets is OAS's own instance, loaded from the startup file — a separate process from any bluesky RunEngine / queueserver.
- Local patch to keep track of: `routers/device_socket.py` `handleSet` uses `getattr(device, "low_limit"/"high_limit", None)` so limitless devices (e.g. `ophyd.sim` `SynAxis`) are movable instead of raising `AttributeError`. This should be upstreamed to ophyd-websocket.

Selecting an instrument: `bits.packages` maps a name → that instrument's OAS startup file (a Python file with module-level ophyd device instances; OAS keys them by variable name). Pass `--bits-package <name>` or set `bits.package`. The mapping is a path lookup only — this package does not import instrument packages.

If you change the OAS device-socket protocol or URL path, update the worker (`_WS_PATH`, subscribe/set handlers) and the vendored OAS in lockstep.

## Configuration

YAML config (`configs/example.yaml`) deep-merges over `DEFAULT_CONFIG` in `config.py`. CLI flags override YAML for that process only. The launcher passes `--config` through to both children so all three layers see the same file.

Sections:

- `launcher` — startup/shutdown timeouts.
- `worker` — ZMQ bind/connect endpoint, request timeout.
- `mcp` — FastMCP HTTP host/port/path.
- `oas` — `url` the worker connects to (e.g. `ws://127.0.0.1:8002`), `host`/`port` the launcher binds the vendored OAS to (keep in sync with `url`), `auto_start` (launcher spawns OAS when true), and the per-call WebSocket timeout.
- `bits` — `package` (default instrument name) and `packages` (name → OAS startup file path) used to launch OAS for the selected instrument.

## Commands

```bash
uv sync                                           # install (uv-managed .venv)
uv run bait-mcp --config configs/example.yaml     # launch OAS + worker + frontend
uv run bait-mcp --config configs/example.yaml --bits-package mcp_instrument  # pick the instrument
uv run bait-mcp-worker --config configs/example.yaml --bind tcp://127.0.0.1:5556  # worker only
uv run bait-mcp-server --config configs/example.yaml --worker tcp://127.0.0.1:5556  # frontend only

# In the shared conda env (has ophyd/EPICS + all instrument packages):
conda run -n bait_mcp_dev pip install -e .        # install into the env
conda run -n bait_mcp_dev bait-mcp --config configs/example.yaml --bits-package mcp_instrument
```

No test suite or linter is configured yet.

## Worker conventions

- Methods on `OphydMCPWorker`: `health`, `read_device`, `set_device`. To add a method, add the handler and register it in `dispatch()`. Keep handler signatures small and JSON-serializable.
- Every OAS call is a **short-lived** connection. Subscribe-on-connect, do the operation, close. Do not introduce a persistent WebSocket — OAS `device_socket` keeps subscribe state per-connection, and a long-lived client would need reconnect logic that isn't worth it at LLM cadence.
- The worker is single-threaded and holds no state between calls. Do not add caching, do not add image buffers. If you need any of that, think hard about whether it belongs in this package or in the consumer.

## HITL is a consumer concern — not implemented here

`set_device` executes the write the moment it's called. There is **no** approval gate on this side. tomo-bait's bits_agent currently implements approval via `pending_writes` + `/chat/confirm` *before* invoking its in-process set tool; that pattern moves with the consumer when it switches to this MCP. If a future tool needs server-enforced safety (e.g. queueserver-running check), add it as an explicit policy in the worker rather than burying it inside `set_device`.

## Duplicated WebSocket client

Both the OAS **client** protocol handling in `worker.py` (subscribe/set/recv loops, `_subscribe_and_confirm`, `_try_unsubscribe`) and the OAS **server** in `src/bait_mcp/ophyd_websocket/` are copies of the upstream ophyd-websocket / `tomo-bait/src/bait/` code. This is intentional — bait_mcp has **no runtime dependency on tomo-bait**; it carries its own copies. The clean fix is to publish ophyd-websocket (and a tiny client) as standalone packages; until that exists, keep the copies equivalent on protocol semantics and upstream local fixes (e.g. the `handleSet` limit guard above). If you change one, change the other.
