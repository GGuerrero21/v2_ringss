import sys
import threading
import time
from pathlib import Path
import zmq
import pytest

# Asegurar que la raíz del proyecto esté en el sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.mount_tcp import run_mount_server

@pytest.fixture(scope="module", autouse=True)
def mount_server_thread(shutdown_service):
    """Levanta el servidor Mount_TCP en un hilo daemon antes de correr los tests."""
    server_thread = threading.Thread(
        target=run_mount_server, kwargs={"port": 5555, "mock_mode": True}, daemon=True
    )
    server_thread.start()
    # Breve pausa para asegurar que el socket ZMQ REP se vincule correctamente
    time.sleep(0.5)
    yield
    shutdown_service(5555)
    server_thread.join(timeout=3.0)


@pytest.fixture
def zmq_client():
    """Configura y destruye un cliente ZeroMQ REQ para cada test."""
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect("tcp://localhost:5555")
    yield socket
    socket.close()
    context.term()


def test_get_status(zmq_client):
    """Verifica la respuesta al solicitar el estado inicial de la montura."""
    zmq_client.send_json({"action": "GET_STATUS"})
    response = zmq_client.recv_json()
    
    assert response["status"] == "SUCCESS"
    assert "ra" in response
    assert "dec" in response
    assert "state" in response
    assert response["state"] == "IDLE"


def test_goto_valid(zmq_client):
    """Verifica un comando GOTO con coordenadas válidas."""
    zmq_client.send_json({"action": "GOTO", "ra": 5.84, "dec": -5.26})
    response = zmq_client.recv_json()
    
    assert response["status"] == "SUCCESS"
    assert response["ra"] == 5.84
    assert response["dec"] == -5.26
    assert response["state"] == "TRACKING"


def test_goto_invalid_range(zmq_client):
    """Verifica que el servidor rechace coordenadas fuera de rango (RA fuera de [0, 24))."""
    zmq_client.send_json({"action": "GOTO", "ra": 25.5, "dec": -10.0})
    response = zmq_client.recv_json()
    
    assert response["status"] == "ERROR"
    assert "fuera de rango" in response["message"].lower()


def test_park_and_stop(zmq_client):
    """Verifica los comandos de estacionamiento (PARK) y parada de emergencia (STOP)."""
    # Probar PARK
    zmq_client.send_json({"action": "PARK"})
    res_park = zmq_client.recv_json()
    assert res_park["status"] == "SUCCESS"
    assert res_park["state"] == "PARKED"

    # Probar STOP
    zmq_client.send_json({"action": "STOP"})
    res_stop = zmq_client.recv_json()
    assert res_stop["status"] == "SUCCESS"
    assert res_stop["state"] == "IDLE"