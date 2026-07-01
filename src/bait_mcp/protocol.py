from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class Command:
    method: str
    params: JsonDict = field(default_factory=dict)

    def to_json(self) -> JsonDict:
        return {"method": self.method, "params": self.params}

    @classmethod
    def from_json(cls, payload: JsonDict) -> "Command":
        method = payload.get("method")
        params = payload.get("params", {})
        if not isinstance(method, str) or not method:
            raise ValueError("Command 'method' must be a non-empty string.")
        if not isinstance(params, dict):
            raise ValueError("Command 'params' must be a JSON object.")
        return cls(method=method, params=params)


def ok_response(result: Any) -> JsonDict:
    return {"status": "ok", "result": result}


def error_response(error: str) -> JsonDict:
    return {"status": "error", "error": error}


def unwrap_response(payload: JsonDict) -> Any:
    status = payload.get("status")
    if status == "ok":
        return payload.get("result")
    if status == "error":
        raise RuntimeError(str(payload.get("error", "Worker returned an error.")))
    raise RuntimeError(f"Invalid worker response status: {status!r}")
