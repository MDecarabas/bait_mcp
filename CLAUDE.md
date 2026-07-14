# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

bait_mcp is an MCP server that exposes a Bluesky/BITS instrument to LLM agents by
talking to its **bluesky-queueserver** over the 0MQ control API. It holds **no
ophyd devices of its own** — device reads/writes run as permitted functions in
the queueserver's *live* RE Worker session, and plans go through the queue. The
whole server is an **MCP frontend + one 0MQ client**.

- **MCP frontend** (`mcp_server.py`): asyncio FastMCP HTTP server. Registers the
  tools and forwards each to the client via `asyncio.to_thread` (the client is
  synchronous/blocking). Holds no state beyond the client.
- **Queueserver client** (`qserver_client.py`): thin synchronous wrapper over
  `bluesky_queueserver_api.zmq.REManagerAPI`. Normalizes every call to
  `{"ok": bool, ...}` so tools never raise across the boundary.
  - **Device I/O** runs the instrument's `read_device`/`set_device` functions via
    `function_execute`. **Reads use `run_in_background=True`** (safe while a plan
    runs); **writes run in the foreground** so the RunEngine serializes them — a
    write attempted mid-plan is refused/deferred by the queueserver. *That* is the
    safety interlock; there is no separate interlock code.
  - **Plans/queue** use `plans_allowed`/`devices_allowed`, `status`, `item_add`,
    `queue_start`/`queue_stop`, `item_execute`.
- **Launcher** (`launcher.py`): now just cleans up stale servers and spawns the
  frontend, forwarding signals with a bounded shutdown. The frontend can also run
  standalone (`bait-mcp-server`).

### Requirements this places on the instrument

bait_mcp injects its `read_device`/`set_device` helpers into the RE Worker
namespace via `script_upload` (see `qserver_client.py:_DEVICE_IO_SCRIPT` and
`_ensure_device_functions`), so the instrument's `startup.py` is **not** modified.
The instrument must still:

- **permit** `read_device`/`set_device` in the connecting group's
  `allowed_functions` (`user_group_permissions.yaml`) — the queueserver enforces
  this regardless of how a function was defined (verified: an unpermitted but
  injected function is refused); and
- expose an `oregistry` in the worker namespace (the injected helpers use
  `oregistry[name]`).

Device I/O also requires the queueserver **environment to be open**. bait_mcp does
not manage the queueserver lifecycle; if the env is closed, the call fails and the
error surfaces in `{"ok": false}`. See `README.md` → "What a BITS repo must provide."

### Tools exposed

`read_device`, `set_device`, `list_devices`, `describe_device`, `list_plans`,
`describe_plan`, `queue_status`, `add_plan`, `start_queue`, `stop_queue`,
`run_plan`. All return `{"ok": bool, ...}`.

## Configuration

YAML config (`configs/example.yaml`) deep-merges over `DEFAULT_CONFIG` in
`config.py`; CLI flags override YAML per process. Active sections:

- `mcp` — FastMCP HTTP `host`/`port`/`path`. `host` defaults to `127.0.0.1`
  because this endpoint can write devices and run plans and is unauthenticated;
  set `0.0.0.0` explicitly to expose it.
- `qserver` — `zmq_control_addr` (RE Manager 0MQ, keep in sync with the
  instrument's `qs-config.yml`), `timeout` (seconds to await a task), `user`,
  `user_group` (must permit the device functions + plans).
- `launcher` — shutdown timeout.

## Commands

```bash
uv sync
uv run bait-mcp --config configs/example.yaml        # launch the frontend
uv run bait-mcp-server --config configs/example.yaml  # frontend only (standalone)
./scripts/kill_bait.sh                                # kill strays

# In the shared conda env (has bluesky-queueserver-api etc.):
conda run -n bait_mcp_dev pip install -e .
PYTHONPATH=src python -m pytest tests/ -q             # unit tests
python -m ruff check src/bait_mcp tests               # lint (excludes dormant code)
```

Tests live in `tests/` (mock `REManagerAPI`; no live qserver needed). Ruff/mypy
config is in `pyproject.toml`.

## HITL is a consumer concern — not implemented here

`set_device` and `run_plan` actuate immediately. There is **no** approval gate on
this side; the consumer (e.g. EAA) must gate actuation. bait_mcp only enforces the
machine-level interlock (foreground writes serialize behind the RE).
