"""Vendored OAS (Ophyd-as-a-Service): server + reusable device registry.

bait uses this two ways:

- As a library: ``bait.device_io`` imports ``device_registry`` and drives
  ophyd Devices in-process — no subprocess, no HTTP, EPICS Channel Access only.
- As a standalone server: ``python -m bait.ophyd_websocket.server
  --startup-dir <file>`` launches a FastAPI app exposing the REST router
  (``routers.core_api``) and four WebSocket routers (``pv_socket``,
  ``device_socket``, ``camera_socket``, ``qs_console_socket``). bait itself
  does not start or connect to this server.
"""
