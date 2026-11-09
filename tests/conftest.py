import zmq
import pytest


@pytest.fixture(scope="module")
def shutdown_service():
    def shutdown(port):
        context = zmq.Context()
        socket = context.socket(zmq.REQ)
        socket.setsockopt(zmq.RCVTIMEO, 2000)
        socket.setsockopt(zmq.SNDTIMEO, 1000)
        socket.setsockopt(zmq.LINGER, 0)
        try:
            socket.connect(f"tcp://localhost:{port}")
            socket.send_json({"action": "SHUTDOWN"})
            response = socket.recv_json()
            assert response.get("status") == "SUCCESS", response
        finally:
            socket.close()
            context.term()

    return shutdown