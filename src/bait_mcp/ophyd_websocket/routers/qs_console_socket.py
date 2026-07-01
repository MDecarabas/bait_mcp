"""WebSocket router for tailing the Bluesky queue-server console.

Endpoint: ``ws://<host>:<port>/api/v1/qs-console-socket``

Subscribes to a ZMQ ``SUB`` socket where the queue-server's console publisher
broadcasts plan/log output, and forwards every line (except the literal
``"QS_Console"`` heartbeat) to the connected WebSocket client. One ZMQ
context+socket per WS connection — close the WS to release them.

Environment:
  - ``ZMQ_HOST`` (default ``localhost``) — host of the QS console publisher.
  - ``ZMQ_PORT`` (default ``60625``) — port. Note this is NOT the QS HTTP
    port (60610) or the QS ZMQ control port (60615); console publishing uses
    its own bound port.

Client → server messages are logged but otherwise ignored — this is a
one-way stream from the server's perspective.

Intended use: a UI panel that shows live plan execution output (the same
text you'd see in the screen session running the QS RE manager). Useful
when bait or another orchestrator submits a plan and you want to display
progress to the operator without parsing it.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import zmq
import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO)

router = APIRouter()

@router.websocket("/qs-console-socket")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logging.info("New WebSocket connection")

    context = zmq.Context()
    socket = context.socket(zmq.SUB)

    # Determine the connection address
    zmq_host = os.getenv("ZMQ_HOST", "localhost")
    zmq_port = os.getenv("ZMQ_PORT", "60625")
    zmq_address = f"tcp://{zmq_host}:{zmq_port}"

    socket.connect(zmq_address)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    logging.info(f"Connected to ZMQ service at {zmq_address}...")

    async def zmq_listener():
        while True:
            try:
                message = socket.recv_string(flags=zmq.NOBLOCK) # the no block option will raise an error if there's no message
                if (message != "QS_Console"):
                    logging.info(f"Received message from ZMQ: {message}")
                    await websocket.send_text(message)
            except zmq.Again as e:
                await asyncio.sleep(0.1)

    try:
        zmq_task = asyncio.create_task(zmq_listener())
        while True:
            data = await websocket.receive_text()
            logging.info(f"Message from client: {data}")
    except WebSocketDisconnect:
        logging.info("WebSocket connection closed")
        zmq_task.cancel()
    finally:
        socket.close()
        context.term()
