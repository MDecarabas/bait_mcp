from __future__ import annotations

from typing import Any

from bluesky_queueserver_api import BFunc, BPlan
from bluesky_queueserver_api.zmq import REManagerAPI

# Injected into the RE Worker namespace via script_upload so bait_mcp works
# against an unmodified instrument startup.py. The functions read/set the live
# oregistry; their names must still be permitted in user_group_permissions.yaml
# (the queueserver enforces allowed_functions regardless of how a function was
# defined). Return values must be JSON-serializable.
_DEVICE_IO_SCRIPT = """
def read_device(name):
    return {signal: dict(reading) for signal, reading in oregistry[name].read().items()}

def set_device(name, value):
    status = oregistry[name].set(value)
    status.wait()
    return {"device": name, "value": value}
"""


class QServerClient:
    """Thin, synchronous wrapper over the bluesky-queueserver 0MQ control API.

    bait_mcp holds no devices of its own. Device reads/writes run as functions in
    the RE Worker's live session (``function_execute``); those functions are
    injected via ``script_upload`` so the instrument ``startup.py`` is unmodified,
    but their names must still be permitted in ``user_group_permissions.yaml``.
    Plans use the queue. Every method normalizes to ``{"ok": bool, ...}`` so the
    MCP tools return a stable shape and never raise across the tool boundary.

    Reads run with ``run_in_background=True`` (safe while a plan is running);
    writes run in the foreground so the RE serializes them (a write submitted
    mid-plan is refused/deferred by the queueserver — that is the interlock).

    Device I/O requires the queueserver environment to be **open**; if it is not,
    ``function_execute`` fails and the error is surfaced in ``{"ok": false}``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        qs = config["qserver"]
        self._timeout = float(qs.get("timeout", 600))
        self._user = qs.get("user", "bait_mcp")
        self._user_group = qs.get("user_group", "primary")
        self._api = REManagerAPI(zmq_control_addr=qs["zmq_control_addr"])
        self._functions_injected = False

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _err(exc: Exception) -> dict[str, Any]:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _ensure_device_functions(self) -> None:
        """Inject read_device/set_device into the worker namespace (once per env)."""
        if self._functions_injected:
            return
        try:
            resp = self._api.script_upload(
                _DEVICE_IO_SCRIPT, update_lists=False, update_re=False
            )
            self._api.wait_for_completed_task(resp["task_uid"], timeout=self._timeout)
            self._functions_injected = True
        except Exception:  # noqa: BLE001 - the device call surfaces the real error
            pass

    def _invoke(
        self, name: str, args: list[Any], *, background: bool, timeout: float | None
    ) -> dict[str, Any]:
        try:
            resp = self._api.function_execute(
                BFunc(name, *args),
                run_in_background=background,
                user=self._user,
                user_group=self._user_group,
            )
            task_uid = resp["task_uid"]
            self._api.wait_for_completed_task(task_uid, timeout=timeout or self._timeout)
            result = self._api.task_result(task_uid)
        except Exception as exc:  # noqa: BLE001 - normalize all failures for the tool
            return self._err(exc)
        return self._normalize_task_result(result)

    def _call_function(
        self, name: str, args: list[Any], *, background: bool, timeout: float | None
    ) -> dict[str, Any]:
        """Inject our functions if needed, call one, and re-inject once if the
        environment was restarted and the function vanished."""
        self._ensure_device_functions()
        out = self._invoke(name, args, background=background, timeout=timeout)
        if not out["ok"] and self._is_missing_function(name, out.get("error", "")):
            self._functions_injected = False
            self._ensure_device_functions()
            out = self._invoke(name, args, background=background, timeout=timeout)
        return out

    @staticmethod
    def _is_missing_function(name: str, error: Any) -> bool:
        """True if `error` looks like the queueserver reporting our injected
        function is absent (the environment was restarted, wiping it).

        The real message is ``Function 'read_device' is not found in the worker
        namespace`` (queueserver ``profile_ops.py``). We match on stable tokens —
        the function name plus a "not available" phrase — rather than that one
        exact string, so a reworded queueserver message still triggers the
        re-inject. The function name keeps this from firing on ordinary device
        errors (e.g. a bad device name), whose traceback lacks those phrases."""
        err = str(error).lower()
        return name.lower() in err and any(
            phrase in err for phrase in ("not found", "not defined", "does not exist")
        )

    @staticmethod
    def _normalize_task_result(result: Any) -> dict[str, Any]:
        # task_result -> {"status":.., "result": {"success":.., "return_value":..,
        #                 "traceback":.., "msg":..}}
        inner = result.get("result") if isinstance(result, dict) else None
        if not isinstance(inner, dict):
            return {"ok": False, "error": f"unexpected task result: {result!r}"}
        if inner.get("success"):
            return {"ok": True, "value": inner.get("return_value")}
        error = inner.get("traceback") or inner.get("msg") or "function failed in RE worker"
        return {"ok": False, "error": error}

    def _allowed(self, kind: str) -> dict[str, Any]:
        """kind is 'plans' or 'devices' -> the *_allowed mapping for our group."""
        getter = getattr(self._api, f"{kind}_allowed")
        resp = getter(user_group=self._user_group)
        return resp.get(f"{kind}_allowed") or {}

    # ---- device I/O --------------------------------------------------------

    def read_device(self, name: str, timeout: float | None = None) -> dict[str, Any]:
        return self._call_function("read_device", [name], background=True, timeout=timeout)

    def set_device(
        self, name: str, value: Any, timeout: float | None = None
    ) -> dict[str, Any]:
        return self._call_function(
            "set_device", [name, value], background=False, timeout=timeout
        )

    # ---- discovery ---------------------------------------------------------

    def list_devices(self) -> dict[str, Any]:
        try:
            return {"ok": True, "devices": sorted(self._allowed("devices").keys())}
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    def describe_device(self, name: str) -> dict[str, Any]:
        try:
            devices = self._allowed("devices")
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)
        if name not in devices:
            return {"ok": False, "error": f"unknown device: {name!r}"}
        return {"ok": True, "device": devices[name]}

    def list_plans(self) -> dict[str, Any]:
        try:
            return {"ok": True, "plans": sorted(self._allowed("plans").keys())}
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    def describe_plan(self, name: str) -> dict[str, Any]:
        try:
            plans = self._allowed("plans")
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)
        if name not in plans:
            return {"ok": False, "error": f"unknown plan: {name!r}"}
        return {"ok": True, "plan": plans[name]}

    # ---- queue / execution -------------------------------------------------

    def queue_status(self) -> dict[str, Any]:
        try:
            # reload=True: bypass the client's status cache so callers see live state.
            return {"ok": True, "status": self._api.status(reload=True)}
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    def add_plan(
        self, name: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            resp = self._api.item_add(
                BPlan(name, *(args or []), **(kwargs or {})),
                user=self._user,
                user_group=self._user_group,
            )
            return {"ok": True, "item": resp.get("item"), "qsize": resp.get("qsize")}
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    def start_queue(self) -> dict[str, Any]:
        try:
            return {"ok": True, "status": self._api.queue_start()}
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    def stop_queue(self) -> dict[str, Any]:
        try:
            return {"ok": True, "status": self._api.queue_stop()}
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    def run_plan(
        self, name: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a plan immediately (item_execute). Requires the environment open."""
        try:
            resp = self._api.item_execute(
                BPlan(name, *(args or []), **(kwargs or {})),
                user=self._user,
                user_group=self._user_group,
            )
            return {"ok": True, "item": resp.get("item")}
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)
