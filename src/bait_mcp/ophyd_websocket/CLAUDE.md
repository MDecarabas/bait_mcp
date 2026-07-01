# bait.ophyd_websocket — vendored OAS reference

This package is a vendored copy of the upstream **Ophyd-as-a-Service (OAS)**
FastAPI server. Treat the files in this folder as a faithful vendor — bait-
specific glue (supervisor, WebSocket client, agent tools) lives one directory
up, not in here.

If you are about to touch any file under `src/bait/ophyd_websocket/`, read this
file first. It exists to make the contract explicit so changes don't drift.

---

## What this is

OAS exposes ophyd Devices over HTTP + WebSocket. It owns a `device_registry`
populated from a Python "startup file" (BITS convention: `<bits>/src/<pkg>/startup.py`)
and serves four router surfaces on a single FastAPI app:

| Router | Mount | What it does |
|---|---|---|
| `core_api` | `/api/v1/...` | REST: `/devices`, `/devices/{name}`, `/devices-info`, `PUT /devices` (set with optional component), `POST /load-devices` (reload from startup file), `/queue-server/status`, plus PV-by-name endpoints |
| `pv_socket` | `WS /api/v1/pv-socket` | Per-connection live EPICS PV subscribe/set. Raw PV names, no registry lookup. |
| `device_socket` | `WS /api/v1/device-socket` | Per-connection live ophyd Device subscribe/set. Resolves names through `device_registry`; recursively subscribes to every component. **This is the surface bait uses.** |
| `camera_socket` | `WS /api/v1/camera-socket` | Area-detector image streaming (binary JPEG frames). |
| `qs_console_socket` | `WS /api/v1/qs-console-socket` | Bluesky queue-server console tail (ZMQ → WS bridge). |

## Two usage modes

The package supports two completely different consumption patterns. Pick the
right one before you change anything:

1. **As a library** — import `device_registry` directly, call `load_startup_files()`,
   drive ophyd in-process via EPICS CA. **Bait no longer uses this mode.** It's
   still importable for tests/scripts that want bare ophyd without a subprocess.

2. **As a standalone server** — `python -m bait.ophyd_websocket.server --startup-dir <file>`
   launches the FastAPI app on `OAS_HOST:OAS_PORT` with the device registry
   populated. **This is how bait uses it**, spawned as a subprocess from
   `bait.app`'s lifespan via `bait.ophyd_websocket_supervisor.OASSupervisor`.

## Module map

| File | Role |
|---|---|
| `server.py` | FastAPI app factory + `__main__` entry point. Reads `OAS_HOST`, `OAS_PORT`, `OAS_STARTUP_DIR` from env, wires the lifespan, mounts all five routers. |
| `device_registry.py` | `DeviceRegistry` class + module-global `device_registry` singleton. Loads ophyd Device instances from a startup .py file. Duck-types BITS/Guarneri `oregistry` (anything with `device_names` + `__getitem__`). |
| `queue_safety.py` | **Server-side** safety: `@queue_safety_required` decorator + `check_queue_server_safety()`. Used by `core_api` to refuse writes while the QS RE is running. **NOT the bait-side check** — that's `bait.device_io.check_queueserver`. |
| `routers/core_api.py` | REST endpoints. Includes `POST /load-devices` for reloading the registry without restarting. |
| `routers/pv_socket.py` | Raw-PV WebSocket. Independent of `device_registry`. |
| `routers/device_socket.py` | Device-by-name WebSocket. Walks `device.component_names` and subscribes to every leaf signal. |
| `routers/camera_socket.py` | Camera image stream. |
| `routers/qs_console_socket.py` | QS console ZMQ → WS bridge. |
| `__init__.py` | Module docstring documenting the two usage modes. |

## Lifecycle (subprocess mode — how bait uses it)

```
bait.app lifespan starts
  └─ OASSupervisor(config).start()
       └─ subprocess.Popen([sys.executable, "-m", "bait.ophyd_websocket.server"],
                            env={OAS_HOST, OAS_PORT, OAS_STARTUP_DIR, ...})

   server.py __main__
     └─ args.startup_dir → os.environ["OAS_STARTUP_DIR"]
     └─ uvicorn.run(app)
          └─ lifespan(app):
               └─ device_registry.set_startup_dir(OAS_STARTUP_DIR)
               (devices NOT loaded yet — wait for explicit POST /load-devices,
               OR for the DeviceRegistry __init__ which auto-loads if a path
               is set. The vendor's lifespan defers loading. See note below.)

  └─ OASSupervisor.wait_ready()
       └─ httpx.get(/api/v1/devices) every 0.5s until count > 0

  yield (bait serves traffic; agent tools open WS to /api/v1/device-socket)

bait.app lifespan exit
  └─ OASSupervisor.stop()
       └─ proc.terminate(); proc.wait(5s) else kill
```

**Note about device loading timing:** `server.py`'s lifespan currently only
calls `set_startup_dir` — it does **not** call `load_startup_files`. Devices
are loaded by `DeviceRegistry.__init__(auto_load=True)` when the module is
first imported. If you change `DeviceRegistry` to default `auto_load=False`,
add a `device_registry.load_startup_files()` call to `server.py:lifespan`
to keep the subprocess loading devices on its own. The supervisor's
`wait_ready` will catch the regression (it polls until `count > 0`).

## WebSocket protocol cheat sheet (`device_socket`)

The bait-side WebSocket client (`bait.ophyd_ws_client`) speaks this protocol.
Keep them in sync — change to the router means change to the client.

Client → server (JSON):

```json
{"action": "subscribe",         "device": "<name>"}   // tolerant
{"action": "subscribeSafely",   "device": "<name>"}   // requires device.get() pass
{"action": "subscribeReadOnly", "device": "<name>"}
{"action": "unsubscribe",       "device": "<name>"}
{"action": "refresh"}                                  // re-get all subscribed
{"action": "set", "device": "<name>", "value": <num|str>, "timeout": <int>}
```

Server → client (JSON):

```json
// On successful subscribe:
{"message": "Subscribed to device <name>"}

// On every value change (one per component for compound devices):
{
  "device": "<name>", "value": ..., "timestamp": ...,
  "connected": true, "read_access": true, "write_access": true,
  "signal": "<leaf-signal-name>"
}

// On metadata change (connect/disconnect):
{"device": "<name>", "connected": true|false, ...kwargs from ophyd meta event...}

// On set success:
{"message": "Successfully set <name> to <value>"}

// On any error:
{"error": "..."}
```

Important constraints:

- The `set` handler **requires prior subscription on the same connection**.
  Always subscribe first.
- `device_socket` has **no `component` field** in any action. To set a
  component (e.g. `tomoscan.rotation_start`), either register the component
  as its own name in the registry, or use the REST `PUT /api/v1/devices`
  endpoint instead.
- `PseudoPositioner` devices skip the `low_limit`/`high_limit` bounds check
  (those attrs don't exist there).
- For `EpicsMotor`, only `readback` value events stream — not setpoint.

## How bait wires this up

Three files outside this package:

- **`bait.ophyd_websocket_supervisor`** (`OASSupervisor`) — owns the subprocess
  lifetime: spawn, poll-until-ready, terminate. Inherits parent env so the
  active conda env (`bait_tomo`) is preserved without activation magic.
- **`bait.ophyd_ws_client`** (`read_device`, `set_device`) — sync WebSocket
  client speaking the `device_socket` protocol. One short-lived connection
  per agent tool call.
- **`bait.app`** — lifespan wires the supervisor; `/chat/confirm` calls
  `ophyd_ws_client.set_device` to execute HITL-approved writes.

QS safety is **separate**:

- `bait.ophyd_websocket.queue_safety.queue_safety_required` runs **inside the
  OAS subprocess** — protects OAS's own REST `PUT /devices` endpoint.
- `bait.device_io.check_queueserver` runs **inside the bait backend** —
  protects `/chat/confirm` before it sends a WS `set`. Sync httpx (vs the
  async one above).

Both probe the same `bluesky-httpserver` `/api/status` endpoint on
`QSERVER_HTTP_SERVER_PORT` (default 60610). Keep them in sync.

## Adding a new router

1. Create `routers/<name>.py`. Pattern after `device_socket.py` for WS or
   `core_api.py` for REST.
2. `app.include_router(...)` in `server.py` next to the existing four.
3. Document the new endpoint in `server.py:list_websockets()` and `read_root()`.
4. Update this CLAUDE.md (the "Module map" table and any relevant protocol
   section).
5. If bait should consume the new router, add a client in `src/bait/` — not
   here.

## Debugging

| Symptom | Likely cause | Where to look |
|---|---|---|
| `OASNotReadyError: ... devices list empty` | startup.py loaded but registered no devices | OAS subprocess logs (forwarded via `_pipe_to_logger`). Check that `make_devices()` in startup.py is finding `devices.yml`. |
| `OASNotReadyError: ... connect: Connection refused` | Subprocess died on startup | OAS subprocess stderr — usually an import error in startup.py. |
| `OASNotReadyError: ... HTTP 500` | OAS running but `/devices` endpoint blew up | OAS logs. Usually `device_registry.list_devices()` raised. |
| WS subscribe times out, no error | EPICS CA can't connect to the IOC | OAS logs may be quiet; check `softIOC` or hardware. PV name correctness. |
| Bait's `set_device` returns `"queue server is currently running"` | Correct — RE has the queue mid-plan | Wait for plan to complete or stop the queue. |
| Two OAS processes on the same port | Old subprocess didn't die cleanly | `lsof -nP -iTCP:8002 -sTCP:LISTEN`, kill stragglers, restart bait. |
| `Failed to connect to device <name>: ...` (from `subscribeSafely`) | Device exists in registry but EPICS CA refuses | Check the device's `prefix` in `devices.yml`; verify IOC up. |

## What does NOT belong in this folder

- Bait-specific config (lives in `bait.config.OphydWebsocketConfig`).
- The subprocess supervisor (lives in `bait.ophyd_websocket_supervisor`).
- The WebSocket client used by the agent (lives in `bait.ophyd_ws_client`).
- Agent tool definitions or system prompts (live in `bait.agents`).
- The QS safety check used from the bait process (lives in `bait.device_io`).

This package is the vendor. Keep it close to upstream so updates can be
re-applied with minimal merge pain.
