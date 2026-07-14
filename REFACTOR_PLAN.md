# Refactor plan — bait_mcp as a queueserver client (function_execute)

**Chosen architecture:** bait_mcp holds no devices. Device reads/writes run in the
**qserver's live session** via `REManagerAPI.function_execute`, and plans go
through the *same* client. bait_mcp collapses to **an MCP frontend + one 0MQ
client** — no ophyd worker, no OAS in the hot path, no interlock code (the RE
serializes writes for us). Single instrument, single source of truth, "pinged by
guarneri in the running session."

Keep all OAS + worker code in the tree this run; delete next run.

Verified facts this rests on:
- `REManagerAPI` (zmq) exposes `function_execute(item, run_in_background=...)`,
  `task_result`, `wait_for_completed_task`, plus the plan/queue surface.
- The function must be **defined in the RE Worker namespace and permitted**
  (`allowed_functions`) — so it requires a small change in the instrument
  (eric-bits/`mcp_instrument`), not just bait_mcp.

---

## Target architecture

```
FastMCP frontend (mcp_server.py, asyncio)
   └─ qserver_client.py ── REManagerAPI(zmq) ──► RE Manager :60615 ──► RE Worker
                                                                        (live oregistry)
Device tools:  read_device / set_device   → function_execute(read_device/set_device)
Plan tools:    list_plans / describe_plan / queue_status /
               add_plan / start_queue / stop_queue / run_plan
```

Read = `run_in_background=True` (safe during a running plan). Write =
**foreground** (`run_in_background=False`) so it serializes behind the RE — the
qserver refuses/defers it while a plan runs. That *is* the interlock; no extra
code.

---

## Two-repo split

**A. eric-bits (`mcp_instrument`) — expose device I/O as permitted functions**
- Define in the worker namespace (startup.py or an imported module it runs):
  ```python
  def read_device(name):
      return oregistry[name].read()
  def set_device(name, value):
      st = oregistry[name].set(value); st.wait()
      return {"device": name, "value": value}
  ```
- Permit them in `qserver/user_group_permissions.yaml` (`allowed_functions`).

**B. bait_mcp — client + tools; stop launching worker/OAS**
- New `qserver_client.py`; rewrite `mcp_server.py` tools; launcher stops spawning
  worker + OAS (files stay). Add `bluesky-queueserver-api` dep.

---

## Decisions to confirm before coding

1. **Who opens the qserver environment?** Recommended: **operator/beamline owns
   qserver lifecycle**; bait_mcp assumes env is open and errors clearly if not
   (optionally a `queue_status` tool surfaces `manager_state`). bait_mcp should
   NOT manage the qserver process. Confirm (alt: bait_mcp calls
   `environment_open` on demand).
2. **Background/foreground rule** as above (reads bg, writes fg). Confirm.
3. **Generic vs named functions:** add explicit `read_device`/`set_device`
   functions (recommended, permissioned, minimal) rather than `script_upload` of
   arbitrary code (powerful but bypasses `allowed_functions`). Confirm.
4. **Keep-OAS mechanic (now trivial):** this run we simply stop launching the
   worker + OAS; all those files sit dormant until next run's deletion. No
   backend flag needed anymore. Confirm.

---

## Phase 0 — De-risk the round-trip (spike, needs a running qserver)

The premise now rests on "function_execute returns a device value." Prove it
before building tools.

1. Against a running `mcp_instrument` qserver (env open), from a throwaway script:
   register/permit `read_device`, then
   `uid = api.function_execute({"name":"read_device","args":["sim_motor"]}, run_in_background=True)`
   → `api.wait_for_completed_task(uid)` → `api.task_result(uid)`.
   - **verify:** result carries `sim_motor`'s reading.
2. `set_device("sim_motor", 5)` foreground → task succeeds; a follow-up read shows 5.
   - **verify:** set works and returns a result.
3. Start a long sim plan (`run_plan`); during it, a background `read_device`
   still returns, and a foreground `set_device` is refused/deferred by the qserver.
   - **verify:** confirms the "free interlock" claim. If foreground writes are
     silently *queued* rather than refused, decide handling here — don't assume.

If Phase 0 fails (function_execute can't reach `oregistry`, or results don't
round-trip), stop and reconsider — this is the load-bearing assumption.

## Phase 1 — Instrument-side functions (eric-bits)

4. Add `read_device`/`set_device` to `mcp_instrument`'s worker namespace; permit
   in `user_group_permissions.yaml`.
   - **verify:** `plans_allowed`/allowed-functions lists them; Phase-0 calls work
     through a normally-launched qserver.

## Phase 2 — bait_mcp qserver client + device tools

5. `src/bait_mcp/qserver_client.py`: `REManagerAPI(zmq)` wrapper. Config
   `qserver.zmq_control_addr` (`tcp://localhost:60615`), `timeout`, `user`/
   `user_group`. Helper `_call_function(name, args, background)` → submit +
   `wait_for_completed_task` + `task_result`, normalized to `{ok, ...}`.
6. `mcp_server.py`: `read_device`/`set_device` now call the client (same tool
   signatures + return shapes as today, so MCP consumers don't change).
   - **verify:** through MCP, `read_device("sim_motor")`/`set_device` work with
     **no worker and no OAS running**.

## Phase 3 — Plan tools (full run control)

7. Tools: `list_plans` (`plans_allowed`), `describe_plan`, `queue_status`
   (`status`), `add_plan` (`item_add`), `start_queue`/`stop_queue`, `run_plan`
   (env-open-if-needed → `item_execute`).
   - **verify:** `list_plans` returns plans w/ signatures; `run_plan` executes a
     sim plan; `queue_status` reflects state transitions.

## Phase 4 — Stop launching worker/OAS; docs; tests

8. `launcher.py`: launch only the frontend (which holds the client). Leave OAS +
   worker spawn code and files dormant (deletion is next run). If the frontend can
   run standalone, the launcher may become a thin wrapper — keep it, don't gut it.
   - **verify:** `bait-mcp` brings up one process; device + plan tools work.
9. Dev/architecture docs only here: rewrite `CLAUDE.md` for the client
   architecture and mark worker + OAS **deprecated, delete next run**.
   (User-facing docs — BITS integration, MCP usage, config reference — are
   Phase 5.)
10. Tests (repo's first): unit for `qserver_client` result normalization (mock
    REManagerAPI: success / task-error / device-not-found); `config.deep_merge`.
    Integration (real qserver) marked/manual.

## Phase 5 — User-facing documentation (README.md)

The refactor isn't done until an integrator who has never seen this repo can
stand it up from the docs alone. Three deliverables; keep them in `README.md`
(with a dedicated `docs/BITS_INTEGRATION.md` if 5a grows long).

**5a. What a BITS repo must provide (the instrument contract).** Written to
generalize beyond `mcp_instrument` — *any* instrument bait_mcp targets needs this:
- The two worker-namespace functions `read_device(name)` / `set_device(name, value)`
  and what they must return (the shape bait_mcp normalizes) — with a copy-paste
  snippet referencing `oregistry`.
- The `user_group_permissions.yaml` `allowed_functions` entries that expose them
  (example patterns), and which `user_group` bait_mcp connects as.
- The runtime precondition: **qserver running with the environment open** —
  bait_mcp does not manage that lifecycle.
- That device names == the instrument's `oregistry`/`devices.yml` names.
- **verify:** a fresh instrument, following only 5a, answers a bait_mcp
  `read_device` — confirm against a second (non-`mcp_instrument`) BITS package if
  one is available, else note the assumption.

**5b. How to use the MCP (consumer instructions).**
- MCP client config block (HTTP URL/transport) and the `eaa_core ... MCPTool`
  shape.
- Every tool with signature + return shape: `read_device`, `set_device`,
  `list_devices`, `describe_device`, `list_plans`, `describe_plan`,
  `queue_status`, `add_plan`, `start_queue`, `stop_queue`, `run_plan`.
- Preconditions (qserver up + env open) and the failure a caller sees when they
  aren't met.
- The safety note: `set_device`/`run_plan` actuate immediately; HITL/approval is
  the consumer's (EAA's) job. Reads run in background, writes serialize behind the
  RE.
- **verify:** every documented tool name exists in `mcp_server.py`; every
  documented precondition maps to a real error path.

**5c. Config reference (every key, meaning, default, override).**
- Table of all config keys with: purpose, default, and the CLI flag that
  overrides it. Cover `mcp.*` (host/port/path), `qserver.*` (zmq_control_addr,
  timeout, user, user_group), and whatever remains of `launcher.*`.
- Mark `worker.*` / `oas.*` sections **deprecated (unused, removed next run)** —
  don't silently drop them while the code still reads them this run.
- Document precedence: `DEFAULT_CONFIG` → YAML → CLI (per-process).
- **verify:** every key in the reference exists in `config.py:DEFAULT_CONFIG`, and
  every key in `DEFAULT_CONFIG` appears in the reference (no drift either way).

---

## Anything else worth doing in this refactor

**Include now (on the critical path):**
- `list_devices`/`describe_device` tools — from `devices_allowed` (the qserver
  already knows the device list; no second source needed). Closes the discovery
  gap from `todo.md` for free.
- Consistent `{ok, ...}` result shape across device + plan tools.

**Recommend but confirm (adjacent, small):**
- `mcp.host` default `0.0.0.0` → `127.0.0.1` (now an unauthenticated device-write
  AND plan-run endpoint — should not bind all interfaces by default).
- Add `[tool.ruff]`/`[tool.mypy]` config (workspace convention; stray
  `.mypy_cache/` with no config).

**Defer to the deletion run:**
- Delete `src/bait_mcp/ophyd_websocket/`, `worker.py`, `protocol.py`,
  `zmq_client.py`, the OAS/worker launcher code, and the stale vendored
  `CLAUDE.md`.
- Drop deps that only the worker/OAS needed: `pyzmq` (direct), `ophyd`, `fastapi`,
  `uvicorn`, `numpy`, `httpx`, `pillow`, `websockets`. Net deps become ~`mcp`,
  `pyyaml`, `bluesky-queueserver-api`.

**Out of scope (name only):** HITL/approval stays a consumer (EAA) concern — with
`run_plan` + `set_device` both live, docs should say EAA must gate actuation.

---

## Risks

- **Phase 0 is the gate** — function_execute round-trip + the foreground-write
  interlock behavior must be confirmed against a real qserver before anything is
  built on them.
- **Hard dependency on qserver env open.** No env → no device I/O (accepted
  tradeoff of this choice). Surface it in errors, don't silently hang.
- **Instrument coupling.** bait_mcp now depends on the instrument exposing
  `read_device`/`set_device` functions + permissions. Any instrument bait_mcp
  targets must provide them (document as a bait_mcp requirement).
- **Latency/chattiness.** Each device op is submit→poll over 0MQ. Fine at LLM
  cadence; note it if a caller ever wants high-rate polling (that's a
  subscription/dashboard concern, out of scope here).
```
