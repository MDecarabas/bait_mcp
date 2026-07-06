from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import load_config
from .zmq_client import WorkerClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch bait_mcp worker and MCP frontend.")
    parser.add_argument("--config", help="Path to a YAML configuration file.")
    parser.add_argument("--worker-endpoint", help="ZMQ endpoint for worker/frontend traffic.")
    parser.add_argument("--worker-startup-timeout-s", type=float, help="Seconds to wait for worker health.")
    parser.add_argument("--request-timeout-ms", type=int, help="Worker request timeout in milliseconds.")
    parser.add_argument("--mcp-host", help="MCP HTTP bind host.")
    parser.add_argument("--mcp-port", type=int, help="MCP HTTP bind port.")
    parser.add_argument("--mcp-path", help="MCP HTTP path.")
    parser.add_argument(
        "--bits-package",
        help="Importable BITS package name whose startup.py OAS loads devices from.",
    )
    return parser


def resolve_startup_file(package: str) -> str:
    """Resolve ``<package>/startup.py`` from an importable BITS package.

    bait_mcp is a frontend to a BITS instrument: it exposes exactly the devices
    that package's ``startup.py`` loads. Refuse to start if the package is not
    importable here, or has no ``startup.py`` — there is no standalone fallback.
    """
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError) as exc:
        raise RuntimeError(
            f"bits package {package!r} is not importable in this environment "
            f"({exc}). Install it (e.g. into the conda env) before launching."
        ) from exc
    if spec is None or not spec.origin:
        raise RuntimeError(
            f"bits package {package!r} is not installed in this environment. "
            f"bait_mcp requires an importable BITS package."
        )
    startup_file = Path(spec.origin).parent / "startup.py"
    if not startup_file.exists():
        raise RuntimeError(
            f"bits package {package!r} has no startup.py at {startup_file}."
        )
    return str(startup_file)


def wait_for_worker(endpoint: str, timeout_s: float, request_timeout_ms: int) -> None:
    client = WorkerClient(endpoint, min(request_timeout_ms, 1000))
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = client.request("health")
            if result == {"status": "ok"}:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise TimeoutError(f"Worker did not become healthy at {endpoint}: {last_error}")


def _device_count(data: object) -> int:
    """Best-effort device count from the OAS /devices response (shape-agnostic)."""
    if isinstance(data, dict):
        for key in ("devices", "device_names"):
            value = data.get(key)
            if isinstance(value, (list, dict)):
                return len(value)
        count = data.get("count")
        if isinstance(count, int):
            return count
    if isinstance(data, list):
        return len(data)
    return 0


def wait_for_oas(host: str, port: int, timeout_s: float) -> None:
    """Poll the OAS REST API until it is up with at least one device registered.

    If the server is up but its registry is empty (auto-load disabled), trigger
    one explicit POST /load-devices and keep polling.
    """
    base = f"http://{host}:{port}/api/v1"
    deadline = time.monotonic() + timeout_s
    tried_load = False
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/devices", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if _device_count(data) > 0:
                return
            if not tried_load:
                tried_load = True
                request = urllib.request.Request(
                    f"{base}/load-devices", method="POST", data=b""
                )
                try:
                    urllib.request.urlopen(request, timeout=10).close()
                except Exception as exc:  # noqa: BLE001 - best effort
                    last_error = exc
        except Exception as exc:  # noqa: BLE001 - server not up yet
            last_error = exc
        time.sleep(0.2)
    raise TimeoutError(f"OAS did not become ready at {host}:{port}: {last_error}")


def terminate_processes(processes: list[subprocess.Popen[bytes]], timeout_s: float) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + timeout_s
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes:
        process.wait()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_stale_servers(timeout_s: float = 5.0) -> None:
    """Terminate any pre-existing bait_mcp servers before starting fresh ones.

    Prevents an orphaned launcher / frontend / worker / OAS from holding our
    ports across restarts. Scoped to bait_mcp's own process signatures only, so
    it never matches unrelated tools running from the same venv (e.g. an editor's
    language server). Skips this launcher's own PID. SIGTERM first (lets the
    launcher shut its children down cleanly), SIGKILL any that outlive the wait.
    """
    patterns = (r"bait_mcp\.ophyd_websocket\.server", "/bin/bait-mcp")
    self_pid = os.getpid()
    pids: set[int] = set()
    for pattern in patterns:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        for token in result.stdout.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid != self_pid:
                pids.add(pid)

    if not pids:
        return

    for pid in pids:
        print(f"[launcher] killing stale bait_mcp process {pid}", file=sys.stderr)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.1)

    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)

    worker_endpoint = args.worker_endpoint or config["worker"]["endpoint"]
    startup_timeout_s = (
        args.worker_startup_timeout_s or config["launcher"]["worker_startup_timeout_s"]
    )
    shutdown_timeout_s = float(config["launcher"]["shutdown_timeout_s"])
    request_timeout_ms = args.request_timeout_ms or int(config["worker"]["request_timeout_ms"])
    mcp_host = args.mcp_host or config["mcp"]["host"]
    mcp_port = args.mcp_port or int(config["mcp"]["port"])
    mcp_path = args.mcp_path or config["mcp"]["path"]

    oas_config = config.get("oas", {})
    oas_host = str(oas_config.get("host", "127.0.0.1"))
    oas_port = int(oas_config.get("port", 8002))

    # The OAS process runs the bits startup.py, which writes RunEngine metadata,
    # logs, and any data files to CWD-relative paths. Pin that CWD explicitly so
    # nothing lands in the bait_mcp repo (or wherever the launcher happens to run).
    oas_workdir_cfg = oas_config.get("workdir")
    if not oas_workdir_cfg:
        raise RuntimeError(
            "oas.workdir is not set. bait_mcp requires an explicit directory for "
            "the OAS/bits session's output (RunEngine metadata, logs, data files); "
            "set oas.workdir in the config to an absolute path."
        )
    oas_workdir = Path(oas_workdir_cfg).expanduser()
    oas_workdir.mkdir(parents=True, exist_ok=True)

    bits_package = args.bits_package or config.get("bits", {}).get("package")
    if not bits_package:
        raise RuntimeError(
            "No bits package configured. bait_mcp requires a BITS package: set "
            "bits.package in the config or pass --bits-package."
        )
    oas_startup_file = resolve_startup_file(bits_package)

    worker_script = shutil.which("bait-mcp-worker")
    mcp_script = shutil.which("bait-mcp-server")
    if worker_script is None or mcp_script is None:
        raise RuntimeError("Could not resolve bait-mcp-worker and bait-mcp-server from PATH.")

    worker_cmd = [worker_script, "--bind", worker_endpoint]
    mcp_cmd = [
        mcp_script,
        "--worker",
        worker_endpoint,
        "--timeout-ms",
        str(request_timeout_ms),
        "--host",
        str(mcp_host),
        "--port",
        str(mcp_port),
        "--path",
        str(mcp_path),
    ]
    if args.config:
        worker_cmd.extend(["--config", args.config])
        mcp_cmd.extend(["--config", args.config])

    # Clean up any orphaned bait_mcp servers before starting, so a stale process
    # can't hold our ports. Bounded by the launcher's shutdown timeout.
    kill_stale_servers(shutdown_timeout_s)

    processes: list[subprocess.Popen[bytes]] = []
    shutting_down = False

    def handle_signal(signum: int, _frame: object) -> None:
        nonlocal shutting_down
        shutting_down = True
        terminate_processes(processes, shutdown_timeout_s)
        raise SystemExit(128 + signum)

    previous_sigint = signal.signal(signal.SIGINT, handle_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    try:
        oas_env = os.environ.copy()
        oas_env["OAS_HOST"] = oas_host
        oas_env["OAS_PORT"] = str(oas_port)
        oas_env["OAS_REQUIRE_QSERVER"] = "false"
        # Set before the OAS process imports its device_registry singleton so
        # it auto-loads devices from the bits package's startup.py at import time.
        oas_env["OAS_STARTUP_DIR"] = oas_startup_file
        oas_cmd = [
            sys.executable,
            "-m",
            "bait_mcp.ophyd_websocket.server",
            "--startup-dir",
            oas_startup_file,
        ]
        oas_process = subprocess.Popen(oas_cmd, env=oas_env, cwd=str(oas_workdir))
        processes.append(oas_process)
        # OAS cold start imports ophyd/fastapi and loads devices; allow extra time.
        wait_for_oas(oas_host, oas_port, max(float(startup_timeout_s), 30.0))

        worker_process = subprocess.Popen(worker_cmd)
        processes.append(worker_process)
        wait_for_worker(worker_endpoint, float(startup_timeout_s), request_timeout_ms)

        mcp_process = subprocess.Popen(mcp_cmd)
        processes.append(mcp_process)

        while True:
            for process in processes:
                returncode = process.poll()
                if returncode is not None:
                    if not shutting_down:
                        terminate_processes(processes, shutdown_timeout_s)
                    return returncode
            time.sleep(0.5)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if processes and not shutting_down:
            terminate_processes(processes, shutdown_timeout_s)


if __name__ == "__main__":
    sys.exit(main())
