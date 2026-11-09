import socket
import threading

import zmq

from services.abort_control import AbortControlServer


def test_abort_control_endpoint_delivers_signal():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    abort_event = threading.Event()
    server = AbortControlServer(
        port,
        lambda message: (abort_event.set() or {"status": "SUCCESS"}),
        "Test",
    )
    context = zmq.Context()
    client = context.socket(zmq.REQ)
    client.setsockopt(zmq.RCVTIMEO, 1500)
    client.setsockopt(zmq.SNDTIMEO, 1500)
    client.setsockopt(zmq.LINGER, 0)
    try:
        server.start()
        client.connect(f"tcp://127.0.0.1:{port}")
        client.send_json({"action": "ABORT"})
        response = client.recv_json()
        assert response["status"] == "SUCCESS"
        assert abort_event.is_set()
    finally:
        client.close()
        context.term()
        server.close()