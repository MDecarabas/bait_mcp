from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "launcher": {
        "worker_startup_timeout_s": 10.0,
        "shutdown_timeout_s": 5.0,
    },
    "worker": {
        "endpoint": "tcp://127.0.0.1:5556",
        "request_timeout_ms": 30000,
    },
    "mcp": {
        "host": "0.0.0.0",
        "port": 8051,
        "path": "/mcp",
    },
    "oas": {
        "url": "ws://127.0.0.1:8002",
        "host": "127.0.0.1",
        "port": 8002,
        "request_timeout_s": 5.0,
        # Required: absolute directory the OAS process runs in. The bits
        # startup.py writes RunEngine metadata / logs / data files to
        # CWD-relative paths, so this is where that data lands. No default —
        # the launcher refuses to start until it is set.
        "workdir": None,
    },
    "bits": {
        # Importable BITS package name. The launcher resolves <package>/startup.py
        # from it and spawns OAS against that file, so the exposed devices are
        # exactly what the package's startup.py loads into its oregistry.
        # bait_mcp refuses to start if this package is not importable in the
        # environment it runs in — there is no sim/standalone fallback.
        "package": "mcp_instrument",
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    if not path:
        return config

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {config_path}")
    return deep_merge(config, loaded)
