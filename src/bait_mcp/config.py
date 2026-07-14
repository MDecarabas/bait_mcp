from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "launcher": {
        "shutdown_timeout_s": 5.0,
    },
    "mcp": {
        # Bind loopback by default: this endpoint can write devices and run plans,
        # and is unauthenticated. Set to 0.0.0.0 explicitly to expose it.
        "host": "127.0.0.1",
        "port": 8051,
        "path": "/mcp",
    },
    "qserver": {
        # bluesky-queueserver RE Manager 0MQ control address. Device I/O and plans
        # both go through this; keep in sync with the instrument's qs-config.yml.
        "zmq_control_addr": "tcp://localhost:60615",
        # Seconds to wait for a function/task to complete (wait_for_completed_task).
        "timeout": 600,
        # Identity bait_mcp presents. user_group must permit read_device/set_device
        # and the plans in the instrument's user_group_permissions.yaml.
        "user": "bait_mcp",
        "user_group": "primary",
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
