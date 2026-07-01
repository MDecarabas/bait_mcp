"""OAS server entry point — vendored from ophyd-websocket.

Run as: ``python -m bait_mcp.ophyd_websocket.server --startup-dir <file.py>``

Mounts the full upstream surface: the REST router (``core_api``) plus four
WebSocket routers (``pv_socket``, ``device_socket``, ``camera_socket``,
``qs_console_socket``). Bait's own device tools only talk to ``core_api``,
but the WebSocket routers are kept available for future UI/dashboard work
that wants live PV, device, camera, or queue-console streams. See each
router's module docstring for protocol details.
"""
import argparse
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .device_registry import device_registry
from .routers.camera_socket import router as camera_socket_router
from .routers.core_api import router as core_api_router
from .routers.device_socket import router as device_socket_router
from .routers.pv_socket import router as pv_socket_router
from .routers.qs_console_socket import router as qs_console_socket_router

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Ophyd as a Service (OAS) - FastAPI Server"
    )
    parser.add_argument(
        "--startup-dir",
        type=str,
        help="Startup directory or .py file for device loading",
        default=None,
    )
    return parser.parse_args()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[LIFESPAN] FastAPI startup event triggered")
    startup_dir = os.getenv("OAS_STARTUP_DIR")
    logger.info(f"[LIFESPAN] OAS_STARTUP_DIR environment variable: {startup_dir}")
    if startup_dir:
        logger.info(
            f"[LIFESPAN] Setting startup directory in device registry: {startup_dir}"
        )
        device_registry.set_startup_dir(startup_dir)
    stored_dir = device_registry.get_startup_dir()
    logger.info(f"[LIFESPAN] Final startup directory in registry: {stored_dir}")
    logger.info("[LIFESPAN] Server ready - use /load-devices endpoint to load devices")
    yield
    logger.info("[LIFESPAN] FastAPI shutdown: Cleaning up...")


app = FastAPI(
    title="Ophyd as a Service",
    description="Vendored REST surface for Ophyd device control.",
    version="1.0.0",
    lifespan=lifespan,
)

OAS_PORT = os.getenv("OAS_PORT", "8001")
OAS_HOST = os.getenv("OAS_HOST", "localhost")

origins = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "*",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(core_api_router, prefix="/api/v1")
app.include_router(pv_socket_router, prefix="/api/v1", tags=["PV WebSocket"])
app.include_router(device_socket_router, prefix="/api/v1", tags=["Device WebSocket"])
app.include_router(camera_socket_router, prefix="/api/v1", tags=["Camera Streaming"])
app.include_router(
    qs_console_socket_router, prefix="/api/v1", tags=["Queue Server Console"]
)


@app.get("/api/v1/websockets", tags=["WebSocket Info"])
def list_websockets():
    """Discovery endpoint enumerating WebSocket endpoints + their protocols."""
    base_ws = f"ws://{OAS_HOST}:{OAS_PORT}"
    return {
        "websockets": {
            "pv_socket": {
                "endpoint": "/api/v1/pv-socket",
                "url": f"{base_ws}/api/v1/pv-socket",
                "description": "Live EPICS PV monitoring + control (raw PV name).",
                "actions": ["subscribe", "subscribeSafely", "subscribeReadOnly",
                            "unsubscribe", "refresh", "set"],
                "example": {"action": "subscribe", "pv": "IOC:m1"},
            },
            "device_socket": {
                "endpoint": "/api/v1/device-socket",
                "url": f"{base_ws}/api/v1/device-socket",
                "description": "Live ophyd Device monitoring + control (by name).",
                "actions": ["subscribe", "subscribeSafely", "subscribeReadOnly",
                            "unsubscribe", "refresh", "set"],
                "example": {"action": "subscribe", "device": "tomoscan"},
            },
            "camera_socket": {
                "endpoint": "/api/v1/camera-socket",
                "url": f"{base_ws}/api/v1/camera-socket",
                "description": "Area-detector image streaming (binary JPEG frames).",
                "init_message": {"imageArray_PV": "MYDET:image1:ArrayData"},
            },
            "qs_console_socket": {
                "endpoint": "/api/v1/qs-console-socket",
                "url": f"{base_ws}/api/v1/qs-console-socket",
                "description": "Bluesky queue-server console tail (ZMQ → WS bridge).",
            },
        },
    }


@app.get("/", tags=["Root"])
def read_root():
    base = f"http://{OAS_HOST}:{OAS_PORT}"
    base_ws = f"ws://{OAS_HOST}:{OAS_PORT}"
    return {
        "message": "Ophyd as a Service (vendored)",
        "version": "1.0.0",
        "rest": {
            "devices": f"{base}/api/v1/devices",
            "load_devices": f"{base}/api/v1/load-devices",
            "queue_server_status": f"{base}/api/v1/queue-server/status",
            "docs": f"{base}/docs",
        },
        "websockets": {
            "discovery": f"{base}/api/v1/websockets",
            "pv_socket": f"{base_ws}/api/v1/pv-socket",
            "device_socket": f"{base_ws}/api/v1/device-socket",
            "camera_socket": f"{base_ws}/api/v1/camera-socket",
            "qs_console_socket": f"{base_ws}/api/v1/qs-console-socket",
        },
    }


if __name__ == "__main__":
    args = parse_arguments()
    if args.startup_dir:
        os.environ["OAS_STARTUP_DIR"] = args.startup_dir
        startup_path = Path(args.startup_dir)
        if not startup_path.exists():
            logger.warning(f"[SERVER] startup path does not exist: {args.startup_dir}")
    port = int(OAS_PORT)
    host = os.getenv("OAS_HOST", "0.0.0.0")
    logger.info(
        f"[SERVER] launching uvicorn on {host}:{port} "
        f"startup_dir={args.startup_dir}"
    )
    uvicorn.run(app, host=host, port=port)
