# bait_mcp — TODO / State of the Package

An assessment of the current state, written after reading the full source
(`launcher.py`, `worker.py`, `mcp_server.py`, `config.py`, `protocol.py`,
`zmq_client.py`, and the vendored `ophyd_websocket/`). Claims here were checked
against the code, not just the READMEs.

---

## What the package does well

- **Small, single-purpose modules.** `protocol.py` (wire format), `zmq_client.py`
  (REQ client), `worker.py` (OAS I/O), `mcp_server.py` (FastMCP frontend),
  `launcher.py` (process supervision), `config.py` (config) each do one thing.
  Nothing is over-abstracted.
- **Correct ZMQ usage.** A fresh REQ socket per call (`zmq_client.py`) avoids the
  REQ/REP lock-step deadlock that a shared socket would cause; `LINGER=0` +
  send/recv timeouts mean a dead worker surfaces as a clean `TimeoutError`
  instead of a hang.
- **Short-lived-connection design matches the server contract.** OAS
  `device_socket` keeps subscribe state per connection; subscribe → use → close
  per call (`worker.py`) is the right shape and keeps the worker stateless.
- **Robust process lifecycle in the launcher.** `kill_stale_servers()` is scoped
  to bait_mcp's own signatures (won't kill an editor LSP in the same venv),
  SIGTERM-then-SIGKILL with a bounded wait, signal forwarding to children, and
  ordered health-gated startup (`wait_for_oas` → `wait_for_worker` → frontend).
- **Fail-fast on misconfiguration.** Refuses to start if the BITS package is not
  importable (`resolve_startup_file`) or if `oas.workdir` is unset — no silent
  degradation, no writing instrument data into the repo.
- **Errors cross the boundary as data, not exceptions.** Worker returns
  `{ok: false, error}` and the frontend forwards it; an LLM client gets a
  structured failure instead of a 500. `_device_count` is shape-agnostic, so it
  tolerates variation in the OAS `/devices` response.
- **Clean config layering.** `DEFAULT_CONFIG` → YAML deep-merge → per-process CLI
  override, with the launcher passing `--config` to both children so all three
  processes agree.
- **Honest documentation.** The top-level `CLAUDE.md` / `README.md` openly state
  the two-process split is *not* justified by blocking I/O, and that there is no
  HITL gate. That candor is worth preserving.

## What the package does poorly / gaps

- **No tests. No linter/formatter config.** There is no `tests/` dir and no
  `[tool.ruff]` / `[tool.mypy]` in `pyproject.toml`, even though the workspace
  convention (root `CLAUDE.md`) says ruff is used everywhere and a `.mypy_cache/`
  exists on disk from ad-hoc runs. This is the single biggest gap.
- **No device discovery tool.** Only `read_device` and `set_device` are exposed.
  An agent has no MCP-side way to learn valid device names — it must be told them
  out of band. OAS already serves `/devices` and `/devices-info`; a
  `list_devices` / `describe_device` tool is the obvious missing piece.
- **Confirmation logic is brittle string-matching.** `set_device` success is
  detected by `"Successfully set" in message` and subscribe by `"Subscribed" in
  message` (`worker.py`). Any reword on the server side silently breaks these.
- **`read_device` semantics on compound devices are underspecified.** The
  device_socket recursively subscribes to every leaf signal, and the worker
  returns the **first** value-bearing message. For a multi-component device the
  returned `value`/`signal` is whichever leaf reports first — nondeterministic
  and possibly not the primary readback. Not documented as a limitation.
- **Server-side write safety does not cover the path bait_mcp actually uses.**
  `queue_safety_required` guards only the `core_api` REST `PUT /devices`
  endpoint. bait_mcp writes via the **device_socket WebSocket**, whose
  `handleSet` has no queue-safety check, and the launcher sets
  `OAS_REQUIRE_QSERVER=false`. Net effect: writes have **zero** server-side
  protection. That is consistent with "HITL is a consumer concern," but anyone
  assuming the vendored safety code protects them is wrong — worth stating loudly.
- **Timeout budget is inconsistent.** `set_device` spends up to `timeout/2` on
  subscribe and then a fresh full `timeout` on the set, so worst case is ~1.5×
  the nominal per-call timeout.
- **Default bind is `0.0.0.0`.** `mcp.host` defaults to all interfaces for an
  unauthenticated device-*write* endpoint. `127.0.0.1` would be a safer default;
  `0.0.0.0` should be an opt-in.
- **Frontend has no logging setup.** `worker.main()` calls
  `logging.basicConfig(INFO)`; `mcp_server.main()` does not, so frontend-side
  diagnostics are silent.

## Next steps (roughly in priority order)

1. **Add a `list_devices` / `describe_device` tool** backed by OAS
   `/devices` + `/devices-info` so agents can discover device names.
2. **Add a test suite.** Unit: `Command`/response round-trip (`protocol.py`),
   `deep_merge` (`config.py`), `resolve_startup_file` (importable vs not),
   `_device_count` shapes. Integration: launch OAS against an `ophyd.sim`
   `SynAxis` startup and exercise `read_device`/`set_device` end to end.
3. **Add ruff + mypy config** to `pyproject.toml` and match the rest of the
   workspace; the mypy cache implies mypy is already being run without config.
4. **Fix the stale vendored docs** (see legacy section) — they actively
   misdescribe how bait_mcp wires OAS.
5. **Decide the fate of the unused routers** (camera/pv/qs_console): trim them
   and their deps, or explicitly document them as "kept as faithful vendor,
   unused by bait_mcp." Pick one; don't leave it ambiguous.
6. **Replace string-match confirmation** in `worker.py` with a more robust signal
   if the device_socket protocol offers one (e.g. a status field).
7. **Reconsider the two-process split.** The package's own `CLAUDE.md` says this
   can collapse to a single FastMCP process with the WS client inline if neither
   the "consistency" nor "future in-process ophyd" rationale holds. Either
   validate a rationale or plan the collapse.
8. **Change the default `mcp.host` to `127.0.0.1`** and make `0.0.0.0` opt-in.
9. **Document `read_device` behavior for compound devices** (which signal wins),
   or narrow it to a chosen primary readback.
10. **Commit the pending `--oas-workdir` change** in `launcher.py` (currently
    uncommitted working-tree edit).

## Leftover / legacy code to remove on further revisions

- **`src/bait_mcp/ophyd_websocket/CLAUDE.md` is tomo-bait legacy.** It contains
  ~19 references to `bait.*` modules that do not exist here: `bait.app`,
  `OASSupervisor` / `bait.ophyd_websocket_supervisor`, `bait.ophyd_ws_client`,
  `bait.device_io.check_queueserver`, the `bait_tomo` conda env, `src/bait/`
  paths, and the `/chat/confirm` HITL flow. It also states the client "only talks
  to `core_api`," which is wrong for bait_mcp (the worker talks to
  `device_socket`; the launcher uses `core_api` REST). Rewrite for bait_mcp or
  delete.
- **`server.py` module docstring is stale.** It claims "Bait's own device tools
  only talk to `core_api`, but the WebSocket routers are kept available for
  future UI/dashboard work." In bait_mcp the worker uses the `device_socket`
  WebSocket and the launcher uses `core_api` REST — the docstring is a
  copy-paste from tomo-bait and should be corrected.
- **Unused vendored routers.** `routers/camera_socket.py` (~358 lines),
  `routers/pv_socket.py` (~253), and `routers/qs_console_socket.py` (~71) are
  mounted in `server.py` but never consumed by bait_mcp (worker → device_socket,
  launcher → core_api). ~680 lines of surface area. Removing camera_socket would
  also drop the `pillow` dependency, which exists solely for JPEG frame
  encoding.
- **`queue_safety.py` is dead for bait_mcp's actual path.** It only guards
  `core_api` REST writes, which bait_mcp does not use, and is disabled via
  `OAS_REQUIRE_QSERVER=false`. Either wire an equivalent check into the
  `device_socket` set path (if server-side safety is wanted) or drop it and rely
  entirely on the documented consumer-side HITL.
- **`pyproject.toml` dependencies tied to unused code.** `pillow` is only needed
  by `camera_socket`; if the unused routers go, this and possibly other deps
  (`numpy`, `httpx`) shrink. Re-audit deps after trimming.
- **On-disk `.mypy_cache/`** — gitignored and not tracked, but present locally as
  noise from ad-hoc mypy runs. Safe to delete; will regenerate if/when mypy is
  configured properly.

---

*Note on scope:* the duplication between the vendored OAS server and the worker's
WS client is **intentional** per `CLAUDE.md` (bait_mcp carries its own copies, no
runtime dependency on tomo-bait). It is listed nowhere above as "remove" — the
real fix there is upstreaming ophyd-websocket as a package, which is out of scope
for this repo.
