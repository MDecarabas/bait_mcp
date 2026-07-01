from __future__ import annotations

import argparse
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
        help="Name of the BITS instrument to bring up (key in config bits.packages).",
    )
    return parser


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
    oas_auto_start = bool(oas_config.get("auto_start", False))
    oas_host = str(oas_config.get("host", "127.0.0.1"))
    oas_port = int(oas_config.get("port", 8002))
    oas_startup_file: str | None = None
    if oas_auto_start:
        bits_config = config.get("bits", {})
        bits_package = args.bits_package or bits_config.get("package")
        packages = bits_config.get("packages", {})
        if not bits_package or bits_package not in packages:
            raise RuntimeError(
                f"bits package {bits_package!r} not found in config bits.packages "
                f"(known: {sorted(packages)})"
            )
        oas_startup_file = packages[bits_package]
        if not Path(oas_startup_file).exists():
            raise RuntimeError(
                f"OAS startup file for {bits_package!r} does not exist: {oas_startup_file}"
            )

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
        if oas_auto_start and oas_startup_file is not None:
            oas_env = os.environ.copy()
            oas_env["OAS_HOST"] = oas_host
            oas_env["OAS_PORT"] = str(oas_port)
            oas_env["OAS_REQUIRE_QSERVER"] = "false"
            # Set before the OAS process imports its device_registry singleton so
            # it auto-loads devices from the startup file at import time.
            oas_env["OAS_STARTUP_DIR"] = oas_startup_file
            oas_cmd = [
                sys.executable,
                "-m",
                "bait_mcp.ophyd_websocket.server",
                "--startup-dir",
                oas_startup_file,
            ]
            oas_process = subprocess.Popen(oas_cmd, env=oas_env)
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
