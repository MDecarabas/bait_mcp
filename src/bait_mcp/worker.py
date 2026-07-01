from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any

import zmq
from websockets.sync.client import connect as ws_connect

from .config import load_config
from .protocol import Command, error_response, ok_response


logger = logging.getLogger(__name__)

_WS_PATH = "/api/v1/device-socket"


class OphydMCPWorker:
    """Sync worker that fronts OAS device-socket on behalf of MCP clients.

    One short-lived WebSocket connection per call. OAS device_socket
    requires per-connection subscribe state, so we subscribe → use → close
    for every request rather than holding a persistent connection.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        oas = config["oas"]
        self.oas_url = oas["url"].rstrip("/")
        self.default_timeout_s = float(oas.get("request_timeout_s", 5.0))

    def dispatch(self, command: Command) -> Any:
        handlers = {
            "health": self.health,
            "read_device": self.read_device,
            "set_device": self.set_device,
        }
        handler = handlers.get(command.method)
        if handler is None:
            raise ValueError(f"Unknown worker method: {command.method}")
        return handler(**command.params)

    def health(self) -> dict[str, str]:
        return {"status": "ok"}

    def _ws_url(self) -> str:
        return f"{self.oas_url}{_WS_PATH}"

    def read_device(
        self,
        name: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Subscribe → first value-bearing message → close."""
        timeout = float(timeout) if timeout is not None else self.default_timeout_s
        try:
            with ws_connect(self._ws_url(), open_timeout=timeout) as ws:
                ws.send(json.dumps({"action": "subscribeSafely", "device": name}))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    remaining = max(deadline - time.monotonic(), 0.01)
                    try:
                        raw = ws.recv(timeout=remaining)
                    except TimeoutError:
                        break
                    msg = json.loads(raw)
                    if "error" in msg:
                        return {"ok": False, "error": msg["error"]}
                    if "value" in msg:
                        _try_unsubscribe(ws, name)
                        return {
                            "ok": True,
                            "value": msg.get("value"),
                            "timestamp": msg.get("timestamp"),
                            "connected": msg.get("connected"),
                            "signal": msg.get("signal"),
                        }
                return {
                    "ok": False,
                    "error": f"timed out waiting for value of {name!r} after {timeout}s",
                }
        except Exception as exc:
            logger.exception("read_device(%s) failed", name)
            return {"ok": False, "error": f"ws read failed: {exc}"}

    def set_device(
        self,
        name: str,
        value: Any,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Subscribe → set → wait for 'Successfully set ...' confirmation → close.

        OAS device_socket requires a prior subscribe on the same connection
        before any set, hence the two-step. No component support (WS protocol
        limitation); use OAS REST PUT /devices for components.
        """
        timeout = float(timeout) if timeout is not None else self.default_timeout_s
        try:
            with ws_connect(self._ws_url(), open_timeout=timeout) as ws:
                if not _subscribe_and_confirm(ws, name, timeout=timeout / 2):
                    return {
                        "ok": False,
                        "error": f"subscribe to {name!r} did not confirm",
                    }
                ws.send(
                    json.dumps(
                        {
                            "action": "set",
                            "device": name,
                            "value": value,
                            "timeout": int(timeout),
                        }
                    )
                )
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    remaining = max(deadline - time.monotonic(), 0.01)
                    try:
                        raw = ws.recv(timeout=remaining)
                    except TimeoutError:
                        break
                    msg = json.loads(raw)
                    if "error" in msg:
                        return {"ok": False, "error": msg["error"]}
                    message = msg.get("message")
                    if isinstance(message, str) and "Successfully set" in message:
                        _try_unsubscribe(ws, name)
                        return {
                            "ok": True,
                            "result": {
                                "device": name,
                                "value": value,
                                "message": message,
                            },
                        }
                return {
                    "ok": False,
                    "error": f"set on {name!r} did not confirm within {timeout}s",
                }
        except Exception as exc:
            logger.exception("set_device(%s, %s) failed", name, value)
            return {"ok": False, "error": f"ws set failed: {exc}"}


def _subscribe_and_confirm(ws, name: str, timeout: float) -> bool:
    ws.send(json.dumps({"action": "subscribeSafely", "device": name}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(deadline - time.monotonic(), 0.01)
        try:
            raw = ws.recv(timeout=remaining)
        except TimeoutError:
            return False
        msg = json.loads(raw)
        if "error" in msg:
            return False
        message = msg.get("message")
        if isinstance(message, str) and "Subscribed" in message:
            return True
    return False


def _try_unsubscribe(ws, name: str) -> None:
    """Best-effort unsubscribe. Server cleans up on close anyway."""
    try:
        ws.send(json.dumps({"action": "unsubscribe", "device": name}))
    except Exception:
        pass


def serve(bind: str, config: dict[str, Any]) -> None:
    worker = OphydMCPWorker(config)
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    socket.bind(bind)
    try:
        while True:
            try:
                command = Command.from_json(socket.recv_json())
                socket.send_json(ok_response(worker.dispatch(command)))
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                socket.send_json(error_response(str(exc)))
    finally:
        socket.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bait_mcp ophyd worker.")
    parser.add_argument("--config", help="Path to a YAML configuration file.")
    parser.add_argument("--bind", help="ZMQ endpoint to bind.")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args()
    config = load_config(args.config)
    bind = args.bind or config["worker"]["endpoint"]
    try:
        serve(bind, config)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
