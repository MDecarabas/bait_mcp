from __future__ import annotations

import argparse
import asyncio
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .zmq_client import WorkerClient


def create_app(
    worker_endpoint: str,
    timeout_ms: int,
    host: str,
    port: int,
    path: str,
) -> FastMCP:
    mcp = FastMCP(
        "bait_mcp Ophyd Tools",
        host=host,
        port=port,
        streamable_http_path=path,
    )
    client = WorkerClient(worker_endpoint, timeout_ms)

    async def call_worker(method: str, params: dict[str, Any]) -> Any:
        return await asyncio.to_thread(client.request, method, params)

    @mcp.tool()
    async def read_device(
        name: Annotated[str, "Device name as registered in the OAS device registry."],
        timeout: Annotated[
            float | None,
            "Optional read timeout in seconds (overrides server default).",
        ] = None,
    ) -> dict[str, Any]:
        """Read the current value of a registered ophyd device via OAS.

        Opens a short-lived WebSocket to the OAS device-socket, subscribes
        safely to the device, takes the first value-bearing message, and
        closes. Returns a JSON object with the device value, timestamp,
        connection status, and which underlying signal produced the value.

        On any failure (timeout, unknown device, EPICS CA refusal) returns
        {"ok": false, "error": "..."}; on success returns
        {"ok": true, "value": ..., "timestamp": ..., "connected": ...,
        "signal": ...}.
        """
        params: dict[str, Any] = {"name": name}
        if timeout is not None:
            params["timeout"] = timeout
        return await call_worker("read_device", params)

    @mcp.tool()
    async def set_device(
        name: Annotated[str, "Device name as registered in the OAS device registry."],
        value: Annotated[float | str, "New value (number or string) to assign."],
        timeout: Annotated[
            float | None,
            "Optional set timeout in seconds (overrides server default).",
        ] = None,
    ) -> dict[str, Any]:
        """Set the value of a registered ophyd device via OAS.

        Executes immediately — this tool does NOT perform a HITL/approval
        gate. If your agent needs human confirmation before a write, gate
        the tool call on the client side before invoking it.

        Subscribes, sets, and waits for "Successfully set ..." confirmation.
        Component access is not supported (OAS WebSocket limitation); use
        the OAS REST endpoint for component-level sets.

        Returns {"ok": true, "result": {"device", "value", "message"}} on
        success or {"ok": false, "error": "..."} on failure.
        """
        params: dict[str, Any] = {"name": name, "value": value}
        if timeout is not None:
            params["timeout"] = timeout
        return await call_worker("set_device", params)

    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bait_mcp MCP frontend.")
    parser.add_argument("--config", help="Path to a YAML configuration file.")
    parser.add_argument("--worker", help="ZMQ endpoint for the worker.")
    parser.add_argument("--timeout-ms", type=int, help="Worker request timeout in milliseconds.")
    parser.add_argument("--host", help="MCP HTTP bind host.")
    parser.add_argument("--port", type=int, help="MCP HTTP bind port.")
    parser.add_argument("--path", help="MCP HTTP path.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    worker_endpoint = args.worker or config["worker"]["endpoint"]
    timeout_ms = args.timeout_ms or int(config["worker"]["request_timeout_ms"])
    host = args.host or config["mcp"]["host"]
    port = args.port or int(config["mcp"]["port"])
    path = args.path or config["mcp"]["path"]

    mcp = create_app(
        worker_endpoint=worker_endpoint,
        timeout_ms=timeout_ms,
        host=host,
        port=port,
        path=path,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
