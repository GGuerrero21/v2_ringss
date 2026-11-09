import sys
import threading
import time
from pathlib import Path
import zmq
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.mount_tcp import run_mount_server
from services.focuser_tcp import run_focuser_server
from services.camera_tcp import run_camera_server
from services.pipeline_orchestrator import PipelineOrchestrator
import services.pipeline_orchestrator as pipeline_orchestrator
from services.abort_control import CAMERA_CONTROL_PORT, FOCUSER_CONTROL_PORT, MOUNT_CONTROL_PORT

@pytest.fixture(scope="module", autouse=True)
def launch_all_mock_services(shutdown_service):
    """Levanta los tres microservicios en puertos estándar para la prueba."""
    threads = [
        threading.Thread(target=run_mount_server, kwargs={"port": 5555, "mock_mode": True}, daemon=True),
        threading.Thread(target=run_focuser_server, kwargs={"port": 5556, "mock_mode": True}, daemon=True),
        threading.Thread(target=run_camera_server, kwargs={"port": 5557, "mock_mode": True}, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(0.6)
    yield
    for port in (5557, 5556, 5555):
        shutdown_service(port)
    for thread in threads:
        thread.join(timeout=3.0)


def test_pre_flight_check():
    orch = PipelineOrchestrator(5555, 5556, 5557)
    health = orch.pre_flight_check()
    assert health["mount"] is True
    assert health["focuser"] is True
    assert health["camera"] is True


def test_abort_signals_all_devices_without_resetting_running_state(monkeypatch):
    orch = PipelineOrchestrator()
    orch.state = "RUNNING"
    calls = []

    def record_control(port, action, timeout_ms=1000):
        calls.append((port, action))
        return {"status": "SUCCESS"}

    monkeypatch.setattr(orch, "_send_control_request", record_control)
    response = orch.abort_sequence()

    assert response["status"] == "SUCCESS"
    assert orch.abort_event.is_set()
    assert orch.state == "ABORTING"
    assert set(calls) == {
        (MOUNT_CONTROL_PORT, "ABORT"),
        (FOCUSER_CONTROL_PORT, "ABORT"),
        (CAMERA_CONTROL_PORT, "ABORT"),
    }


def test_failed_preflight_prevents_hardware_commands(monkeypatch):
    orch = PipelineOrchestrator()
    monkeypatch.setattr(
        orch,
        "pre_flight_check",
        lambda: {"mount": True, "focuser": False, "camera": True},
    )
    monkeypatch.setattr(
        orch,
        "_send_request",
        lambda *args, **kwargs: pytest.fail("No device command should run after failed pre-flight"),
    )

    result = orch.execute_ringss_sequence()

    assert result["status"] == "ERROR"
    assert "no se iniciaron movimientos" in result["message"]


def test_safety_rollback_moves_to_captured_initial_position(monkeypatch):
    orch = PipelineOrchestrator()
    requests = []

    def record_request(port, action, payload=None, timeout_ms=5000):
        requests.append((port, action, payload))
        return {"status": "SUCCESS", "position": 12345}

    monkeypatch.setattr(orch, "_send_request", record_request)
    orch._safety_rollback(12345)

    assert requests == [(5556, "MOVE_ABS", {"position": 12345})]


def test_execute_ringss_sequence(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_orchestrator, "CAPTURES_DIR", tmp_path)
    orch = PipelineOrchestrator(5555, 5556, 5557)
    res = orch.execute_ringss_sequence(exp_ms=10.0, gain=80, frames_cube=100)
    
    assert res["status"] == "SUCCESS"
    assert "seeing_arcsec" in res
    assert "fried_r0_cm" in res
    assert res["seeing_arcsec"] > 0
    assert orch.state == "IDLE"