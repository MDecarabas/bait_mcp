# bait_mcp

An MCP server that exposes a Bluesky/BITS instrument to LLM agents by talking to
its **bluesky-queueserver** over the 0MQ control API. bait_mcp holds **no ophyd
devices of its own**: device reads/writes run in the queueserver's *live* session,
and plans go through the queue. The whole server is an **MCP frontend + one 0MQ
client** — no ophyd, no EPICS, no second device session.

- **Device I/O** runs the instrument's `read_device`/`set_device` functions in the
  RE Worker (`function_execute`). Reads run in the background (safe during a plan);
  writes run in the foreground, so the RunEngine serializes them — a write during
  a running plan is refused by the queueserver. That is the safety interlock.
- **Plans** are listed, enqueued, and run through the queueserver.

## What a BITS repo must provide (the instrument contract)

bait_mcp works with **any** BITS instrument that satisfies this contract, and it
does **not** require editing the instrument's `startup.py`. bait_mcp injects its
`read_device`/`set_device` helpers into the running RE Worker namespace via
`script_upload` (they read/set the live `oregistry`). The instrument must provide:

1. **Permission to call the injected functions.** The queueserver enforces
   `allowed_functions` regardless of how a function was defined, so add the names
   to the `allowed_functions` of the group bait_mcp connects as
   (`qserver.user_group`, default `primary`) in
   `qserver/user_group_permissions.yaml`:

   ```yaml
   primary:
     allowed_functions:
       - "read_device"
       - "set_device"
   ```

   (Alternatively, connect as a group whose `allowed_functions` is `[null]` —
   allow-all, e.g. `root` — which needs no per-name entry but grants broader
   privilege than required.)

2. **An `oregistry` in the worker namespace.** The injected helpers resolve
   devices via `oregistry[name]` — the standard BITS/Guarneri registry that
   `startup.py` builds. Device names passed to the tools are its names. (Readings
   must be JSON-serializable; numpy-valued signals may need coercion.)

3. **A running queueserver with the environment open.** bait_mcp does not manage
   the queueserver lifecycle. If the environment is closed or the RE Manager is
   unreachable, device I/O returns `{"ok": false, "error": ...}`.

## Setup

```bash
uv sync
# Or install into the conda env that has bluesky-queueserver-api:
conda run -n bait_mcp_dev pip install -e .

cp configs/example.yaml configs/local.yaml   # then edit qserver.zmq_control_addr
```

## Launch

```bash
uv run bait-mcp --config configs/local.yaml           # launcher + frontend
uv run bait-mcp-server --config configs/local.yaml     # frontend only (standalone)
```

With the defaults the MCP endpoint is `http://127.0.0.1:8051/mcp`.

## MCP client configuration

```json
{
  "mcpServers": {
    "bait_mcp": { "url": "http://127.0.0.1:8051/mcp", "transport": "http" }
  }
}
```

This is the shape `eaa_core.tool.mcp_client.MCPTool` expects.

## Tools

All tools return `{"ok": true, ...}` on success or `{"ok": false, "error": ...}`
on failure (e.g. queueserver unreachable, environment closed, unknown name).

| Tool | Purpose |
|---|---|
| `read_device(name, timeout=None)` | Read a device's value (background; works during a plan). Returns `{ok, value}`. |
| `set_device(name, value, timeout=None)` | Set a device (foreground; refused mid-plan). Actuates immediately — no HITL. |
| `list_devices()` | Device names exposed to this user group. |
| `describe_device(name)` | The queueserver's description of one device. |
| `list_plans()` | Plan names exposed to this user group. |
| `describe_plan(name)` | A plan's parameter signature. |
| `queue_status()` | RE Manager status (`manager_state`, running item, queue size). |
| `add_plan(name, args=None, kwargs=None)` | Enqueue a plan (does not start it). |
| `start_queue()` / `stop_queue()` | Start / stop queue execution. |
| `run_plan(name, args=None, kwargs=None)` | Execute a plan immediately. Requires the env open. Actuates — no HITL. |

**Preconditions:** the queueserver must be running with its environment open, and
the instrument must expose the functions above. When they aren't met, tools return
`{"ok": false, "error": ...}` rather than hanging.

## Configuration reference

Config layering: `DEFAULT_CONFIG` (`config.py`) → YAML (`--config`) → CLI flags,
per process.

| Key | Meaning | Default | CLI override |
|---|---|---|---|
| `mcp.host` | HTTP bind host (loopback by default; it can write/run) | `127.0.0.1` | `--mcp-host` / `--host` |
| `mcp.port` | HTTP bind port | `8051` | `--mcp-port` / `--port` |
| `mcp.path` | HTTP path | `/mcp` | `--mcp-path` / `--path` |
| `qserver.zmq_control_addr` | RE Manager 0MQ control address | `tcp://localhost:60615` | — |
| `qserver.timeout` | Seconds to await a task/function | `600` | — |
| `qserver.user` | Identity bait_mcp presents | `bait_mcp` | — |
| `qserver.user_group` | Permission group (must allow the functions/plans) | `primary` | — |
| `launcher.shutdown_timeout_s` | Grace period before children are killed | `5.0` | — |

## Safety / HITL

`set_device` and `run_plan` actuate immediately. There is **no** human-in-the-loop
approval gate here — the consumer (e.g. EAA) must gate actuation. bait_mcp only
enforces the machine-level interlock: foreground writes serialize behind the
RunEngine, so a write during a running plan is refused by the queueserver.

### Interlock scope: the queueserver, not the hardware

That interlock is **scoped to the queueserver's own RunEngine**, not to the
hardware. It stops bait_mcp from colliding with the queueserver's plans and
writes; it does **not** see motion started outside the queueserver — a manual
`caput`, another EPICS client, or an operator jogging a motor. bait_mcp assumes the
**queueserver is the sole controller of the hardware** (the standard
bluesky-queueserver model). If that does not hold at your beamline, fix it at the
access-control layer (EPICS access security, or route all writes through the
queueserver) — **not** with a per-device check here.

### Do not add a per-device "is it moving?" guard

It is tempting to make the instrument's `set_device` refuse when `device.moving` is
true, to catch external motion. Don't — it does more harm than good:

- **False safety (race).** "Check `moving`, then set" is a time-of-check/time-of-use
  race: the device can start moving in the gap. It looks like an interlock but does
  not guarantee one.
- **Partial, inconsistent coverage.** Only devices with a busy signal
  (`EpicsMotor.moving`) are covered; plain `Signal`s, detectors, and sim devices
  (`SynAxis`) have none, so the guard is a silent no-op there. Protection that
  exists for some devices but not others is worse than none, because it gets
  trusted.
- **Blocks legitimate retargeting.** An agent intentionally changing a moving
  motor's target — a valid operation — would be refused.
- **False confidence.** It masks the real problem (two independent controllers on
  one PV is unsafe regardless) and discourages the actual fix: single-controller
  enforcement.

The safe default is the operational model above, not a `.moving` check.
