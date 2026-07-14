from __future__ import annotations

import argparse
import asyncio
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .qserver_client import QServerClient


def create_app(config: dict[str, Any], host: str, port: int, path: str) -> FastMCP:
    mcp = FastMCP("bait_mcp Ophyd Tools", host=host, port=port, streamable_http_path=path)
    client = QServerClient(config)

    async def call(method: str, *args: Any, **kwargs: Any) -> Any:
        # REManagerAPI is synchronous/blocking; keep it off the event loop.
        return await asyncio.to_thread(getattr(client, method), *args, **kwargs)

    # ---- device I/O --------------------------------------------------------

    @mcp.tool()
    async def read_device(
        name: Annotated[str, "Device name as registered in the instrument's oregistry."],
        timeout: Annotated[float | None, "Optional read timeout in seconds."] = None,
    ) -> dict[str, Any]:
        """Read a device's current value via the queueserver's live session.

        Runs the instrument's ``read_device`` function in the RE worker (in the
        background, so it works even while a plan is running). Returns
        ``{"ok": true, "value": {signal: {value, timestamp}}}`` or
        ``{"ok": false, "error": ...}`` (e.g. env not open, unknown device).
        """
        return await call("read_device", name, timeout=timeout)

    @mcp.tool()
    async def set_device(
        name: Annotated[str, "Device name as registered in the instrument's oregistry."],
        value: Annotated[float | str, "New value (number or string) to assign."],
        timeout: Annotated[float | None, "Optional set timeout in seconds."] = None,
    ) -> dict[str, Any]:
        """Set a device's value via the queueserver's live session.

        Runs the instrument's ``set_device`` function in the foreground, so the
        RunEngine serializes it: a write attempted while a plan is running is
        refused/deferred by the queueserver (the safety interlock). Executes
        immediately — no HITL gate; the consumer must gate approval.

        Returns ``{"ok": true, "value": {"device", "value"}}`` or
        ``{"ok": false, "error": ...}``.
        """
        return await call("set_device", name, value, timeout=timeout)

    # ---- discovery ---------------------------------------------------------

    @mcp.tool()
    async def list_devices() -> dict[str, Any]:
        """List device names the queueserver exposes to this user group."""
        return await call("list_devices")

    @mcp.tool()
    async def describe_device(
        name: Annotated[str, "Device name to describe."],
    ) -> dict[str, Any]:
        """Return the queueserver's description of one device (components, type)."""
        return await call("describe_device", name)

    @mcp.tool()
    async def list_plans() -> dict[str, Any]:
        """List plan names the queueserver exposes to this user group."""
        return await call("list_plans")

    @mcp.tool()
    async def describe_plan(
        name: Annotated[str, "Plan name to describe."],
    ) -> dict[str, Any]:
        """Return the queueserver's signature/parameters for one plan."""
        return await call("describe_plan", name)

    # ---- queue / execution -------------------------------------------------

    @mcp.tool()
    async def queue_status() -> dict[str, Any]:
        """Return the RE Manager status (manager_state, running item, queue size)."""
        return await call("queue_status")

    @mcp.tool()
    async def add_plan(
        name: Annotated[str, "Plan name to enqueue."],
        args: Annotated[list[Any] | None, "Positional plan arguments."] = None,
        kwargs: Annotated[dict[str, Any] | None, "Keyword plan arguments."] = None,
    ) -> dict[str, Any]:
        """Add a plan to the queue (does not start it). Returns the queued item."""
        return await call("add_plan", name, args, kwargs)

    @mcp.tool()
    async def start_queue() -> dict[str, Any]:
        """Start executing the queue."""
        return await call("start_queue")

    @mcp.tool()
    async def stop_queue() -> dict[str, Any]:
        """Request the queue to stop after the current item."""
        return await call("stop_queue")

    @mcp.tool()
    async def run_plan(
        name: Annotated[str, "Plan name to execute immediately."],
        args: Annotated[list[Any] | None, "Positional plan arguments."] = None,
        kwargs: Annotated[dict[str, Any] | None, "Keyword plan arguments."] = None,
    ) -> dict[str, Any]:
        """Execute a plan immediately (bypasses the queue). Requires the env open.

        Actuates hardware right away — no HITL gate; the consumer must gate
        approval.
        """
        return await call("run_plan", name, args, kwargs)

    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bait_mcp MCP frontend.")
    parser.add_argument("--config", help="Path to a YAML configuration file.")
    parser.add_argument("--host", help="MCP HTTP bind host.")
    parser.add_argument("--port", type=int, help="MCP HTTP bind port.")
    parser.add_argument("--path", help="MCP HTTP path.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    host = args.host or config["mcp"]["host"]
    port = args.port or int(config["mcp"]["port"])
    path = args.path or config["mcp"]["path"]

    mcp = create_app(config=config, host=host, port=port, path=path)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
