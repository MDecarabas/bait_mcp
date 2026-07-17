# bait_mcp — TODO / State of the Package

An assessment of the current state, written after reading the full source
(`qserver_client.py`, `mcp_server.py`, `config.py`, `launcher.py`) and the tests.
Claims here were checked against the code, not just the READMEs. This reflects the
package **after** the queueserver-client refactor: bait_mcp is an MCP frontend +
one 0MQ client, with no ophyd/OAS/worker of its own.

---

## What the package does well

- **Thin, single-responsibility client.** `qserver_client.py` (0MQ wrapper),
  `mcp_server.py` (FastMCP frontend), `config.py` (config), `launcher.py` (cleanup
  + spawn) — no second device session, no ophyd, no EPICS in bait_mcp itself. The
  whole package is a stateless forwarder onto `REManagerAPI`.
- **The interlock is free, not coded.** Reads run `run_in_background=True` (safe
  during a plan); writes run in the foreground so the RunEngine serializes them and
  the queueserver refuses a write mid-plan. bait_mcp adds *no* interlock code — it
  leans on the qserver's own idle-state rule.
- **Uniform `{ok, ...}` boundary.** Every method normalizes success and failure to
  a dict (`_normalize_task_result`, `_err`); nothing raises across the tool
  boundary, so an LLM client always gets structured data, never a stack trace.
- **Instrument stays untouched.** Device I/O is injected into the RE Worker via
  `script_upload` (`_DEVICE_IO_SCRIPT`), so bait_mcp works against any BITS
  instrument that permits the two functions and exposes an `oregistry` — no
  `startup.py` edit required. Injection is lazy (once per env) with a
  re-inject-on-restart retry that matches the queueserver's missing-function
  error on stable tokens (function name + a "not available" phrase), not one
  exact string, so a reworded message still self-heals (`_is_missing_function`).
- **Single source of truth for discovery.** `list_devices`/`list_plans` /
  `describe_*` read `devices_allowed`/`plans_allowed` straight from the qserver —
  no second registry to drift out of sync.
- **Tests exist and cover the tricky paths.** 13 tests against a mocked
  `REManagerAPI`, including inject-once (`test_functions_injected_once`),
  re-inject-on-missing (`test_reinjects_when_function_missing`), and the
  foreground/background interlock semantics. ruff is configured and clean.
- **Safe defaults + honest docs.** `mcp.host` binds `127.0.0.1` for an
  unauthenticated write/run endpoint; the README documents the injection ("How
  stuff works") candidly and states plainly that HITL is the consumer's job.

## What the package does poorly / gaps

- **Device I/O is a code-string side channel.** `_DEVICE_IO_SCRIPT` is Python
  source `exec`'d into a live process, not an API. It works, but it is inherently
  more fragile than a real endpoint and couples bait_mcp to the worker's internals
  (`oregistry`).
- **First call during a running plan may not inject.** `script_upload` needs an idle
  worker; a cold client whose very first device call lands mid-plan can fail to
  inject and won't self-heal until an idle call. There is no warm-up inject at
  startup; the workaround is documented in README → "Potential issues".
- **Readings must be JSON-serializable, and that isn't enforced.** `read_device`
  returns whatever `oregistry[name].read()` yields; numpy-valued signals can break
  serialization. The requirement is documented but not coerced or validated.
- **No static type checking.** mypy config was removed (no sibling repo runs it
  either); linting is ruff-only. A type checker could catch shape errors in the
  `{ok, ...}` normalization, but it's a deliberate omission, not an oversight.
- **No live-qserver test in CI.** The `function_execute` round-trip and the
  interlock were verified manually against a real qserver, but every automated test
  mocks the API. Nothing guards a regression in the real 0MQ path.
- **One-shot polls only.** Each device op is submit→wait→result over 0MQ. Fine at
  LLM cadence; there is no subscription/streaming path (the OAS subscribe/unsubscribe
  capability was dropped in the refactor), so live dashboards are out of scope today.

## Next steps (roughly in priority order)

1. **Add a startup warm-up inject (optional).** Inject `read_device`/`set_device`
   when the client is constructed / env is confirmed open, so the first device call
   during a plan doesn't fail. Until then the manual workaround is documented in
   README → "Potential issues".
2. **Add a marked live-qserver integration test.** Exercise the real round-trip and
   the foreground-write interlock against a sim instrument; mark it manual/optional
   so unit runs stay hermetic.
3. **Coerce or validate non-serializable readings** in `read_device` (numpy →
   Python scalars), or reject them with a clear error rather than failing in the
   transport.
4. **Add a subscription/streaming path** only if a consumer needs live values —
   currently out of scope, tracked here so the gap isn't forgotten.

Done: hardened re-inject detection — `_is_missing_function` now matches stable
tokens (function name + a "not available" phrase) instead of one exact substring,
so a reworded queueserver message still triggers the automatic re-inject
(`test_reinjects_when_missing_message_reworded`).

## Leftover / legacy code to remove on further revisions

None outstanding.
