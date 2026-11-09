import sys
import threading
import time
from pathlib import Path
import zmq
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.focuser_tcp import run_focuser_server

@pytest.fixture(scope="module", autouse=True)
def focuser_server_thread(shutdown_service):
    """Levanta el servidor Focuser_TCP en puerto 5556."""
    server_thread = threading.Thread(
        target=run_focuser_server, kwargs={"port": 5556, "mock_mode": True, "offset": 400}, daemon=True
    )
    server_thread.start()
    time.sleep(0.5)
    yield
    shutdown_service(5556)
    server_thread.join(timeout=3.0)


@pytest.fixture
def zmq_focuser_client():
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect("tcp://localhost:5556")
    yield socket
    socket.close()
    context.term()


def test_get_pos(zmq_focuser_client):
    zmq_focuser_client.send_json({"action": "GET_POS"})
    res = zmq_focuser_client.recv_json()
    assert res["status"] == "SUCCESS"
    assert "position" in res


def test_move_abs_and_rel(zmq_focuser_client):
    # Mover absoluto
    zmq_focuser_client.send_json({"action": "MOVE_ABS", "position": 15000})
    res = zmq_focuser_client.recv_json()
    assert res["status"] == "SUCCESS"
    assert res["position"] == 15000

    # Mover relativo (+500)
    zmq_focuser_client.send_json({"action": "MOVE_REL", "steps": 500})
    res = zmq_focuser_client.recv_json()
    assert res["status"] == "SUCCESS"
    assert res["position"] == 15500


def test_ring_defocus_and_return(zmq_focuser_client):
    # Establecer base en 20000
    zmq_focuser_client.send_json({"action": "MOVE_ABS", "position": 20000})
    zmq_focuser_client.recv_json()

    # Aplicar offset de anillo RINGSS (+400 según configuración)
    zmq_focuser_client.send_json({"action": "APPLY_RING_DEFOCUS"})
    res_defocus = zmq_focuser_client.recv_json()
    assert res_defocus["status"] == "SUCCESS"
    assert res_defocus["ring_position"] == 20400
    assert res_defocus["previous_focus"] == 20000

    # Retornar al foco previo
    zmq_focuser_client.send_json({"action": "RETURN_TO_FOCUS"})
    res_return = zmq_focuser_client.recv_json()
    assert res_return["status"] == "SUCCESS"
    assert res_return["position"] == 20000