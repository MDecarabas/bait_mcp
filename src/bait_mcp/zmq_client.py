from __future__ import annotations

from typing import Any

import zmq

from .protocol import Command, unwrap_response


class WorkerClient:
    def __init__(self, endpoint: str, timeout_ms: int) -> None:
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        context = zmq.Context.instance()
        socket = context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        try:
            socket.connect(self.endpoint)
            socket.send_json(Command(method=method, params=params or {}).to_json())
            return unwrap_response(socket.recv_json())
        except zmq.Again as exc:
            raise TimeoutError(
                f"Timed out waiting for worker at {self.endpoint} after {self.timeout_ms} ms."
            ) from exc
        finally:
            socket.close()
