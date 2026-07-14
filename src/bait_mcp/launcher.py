from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time

from .config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the bait_mcp MCP frontend.")
    parser.add_argument("--config", help="Path to a YAML configuration file.")
    parser.add_argument("--mcp-host", help="MCP HTTP bind host.")
    parser.add_argument("--mcp-port", type=int, help="MCP HTTP bind port.")
    parser.add_argument("--mcp-path", help="MCP HTTP path.")
    return parser


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
    """Terminate any pre-existing bait_mcp frontend before starting a fresh one.

    Prevents an orphaned launcher / frontend from holding our port across
    restarts. Scoped to bait_mcp's own console entrypoint signature only, so it
    never matches unrelated tools running from the same venv (e.g. an editor's
    language server). Skips this launcher's own PID. SIGTERM first, SIGKILL any
    that outlive the wait.
    """
    self_pid = os.getpid()
    pids: set[int] = set()
    result = subprocess.run(["pgrep", "-f", "/bin/bait-mcp"], capture_output=True, text=True)
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

    shutdown_timeout_s = float(config["launcher"]["shutdown_timeout_s"])
    mcp_host = args.mcp_host or config["mcp"]["host"]
    mcp_port = args.mcp_port or int(config["mcp"]["port"])
    mcp_path = args.mcp_path or config["mcp"]["path"]

    mcp_script = shutil.which("bait-mcp-server")
    if mcp_script is None:
        raise RuntimeError("Could not resolve bait-mcp-server from PATH.")

    mcp_cmd = [
        mcp_script,
        "--host",
        str(mcp_host),
        "--port",
        str(mcp_port),
        "--path",
        str(mcp_path),
    ]
    if args.config:
        mcp_cmd.extend(["--config", args.config])

    # Clean up any orphaned bait_mcp frontend before starting.
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
