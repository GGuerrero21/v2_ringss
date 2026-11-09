import sys
import threading
import time
from pathlib import Path
import zmq
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.camera_tcp import CameraHardwareDriver, run_camera_server

@pytest.fixture(scope="module", autouse=True)
def camera_server_thread(shutdown_service):
    """Levanta el servidor Camera_TCP en puerto 5557."""
    server_thread = threading.Thread(
        target=run_camera_server, kwargs={"port": 5557, "mock_mode": True}, daemon=True
    )
    server_thread.start()
    time.sleep(0.5)
    yield
    shutdown_service(5557)
    server_thread.join(timeout=3.0)


@pytest.fixture
def zmq_camera_client():
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect("tcp://localhost:5557")
    yield socket
    socket.close()
    context.term()


def test_get_status(zmq_camera_client):
    zmq_camera_client.send_json({"action": "GET_STATUS"})
    res = zmq_camera_client.recv_json()
    assert res["status"] == "SUCCESS"
    assert "exposure_ms" in res
    assert res["state"] == "IDLE"


def test_set_config(zmq_camera_client):
    zmq_camera_client.send_json({"action": "SET_CONFIG", "exposure_ms": 250.0, "gain": 150, "binning": 1})
    res = zmq_camera_client.recv_json()
    assert res["status"] == "SUCCESS"
    assert res["exposure_ms"] == 250.0
    assert res["gain"] == 150


def test_capture(zmq_camera_client, tmp_path):
    filename = str(tmp_path / "test_frame.fits")
    zmq_camera_client.send_json({"action": "CAPTURE", "filename": filename})
    res = zmq_camera_client.recv_json()
    assert res["status"] == "SUCCESS"
    assert res["filename"] == filename


def test_invalid_config_does_not_mutate_camera_state():
    driver = CameraHardwareDriver(mock_mode=True)
    before = (driver.exposure_ms, driver.gain, driver.binning, driver.roi_width, driver.roi_height)

    with pytest.raises(ValueError):
        driver.set_config(gain=-1)
    with pytest.raises(ValueError):
        driver.set_config(roi="unknown ROI")

    after = (driver.exposure_ms, driver.gain, driver.binning, driver.roi_width, driver.roi_height)
    assert after == before


def test_mock_capture_can_be_aborted_without_writing_a_cube(tmp_path):
    driver = CameraHardwareDriver(mock_mode=True)
    filename = tmp_path / "aborted.fits"
    result = {}
    thread = threading.Thread(target=lambda: result.update(driver.capture(str(filename), frames=100)))
    thread.start()

    deadline = time.time() + 2.0
    while driver.state != "EXPOSURING" and time.time() < deadline:
        time.sleep(0.001)
    driver.abort_requested.set()
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert result.get("status") == "ABORTED"
    assert not filename.exists()


def test_control_port_aborts_active_capture(tmp_path):
    context = zmq.Context()
    capture_socket = context.socket(zmq.REQ)
    control_socket = context.socket(zmq.REQ)
    for socket in (capture_socket, control_socket):
        socket.setsockopt(zmq.RCVTIMEO, 3000)
        socket.setsockopt(zmq.SNDTIMEO, 1000)
        socket.setsockopt(zmq.LINGER, 0)
    try:
        capture_socket.connect("tcp://localhost:5557")
        capture_socket.send_json({
            "action": "CAPTURE",
            "filename": str(tmp_path / "control-aborted.fits"),
            "frames": 100,
        })
        time.sleep(0.03)

        control_socket.connect("tcp://localhost:5563")
        control_socket.send_json({"action": "ABORT"})
        control_response = control_socket.recv_json()
        capture_response = capture_socket.recv_json()

        assert control_response["status"] == "SUCCESS"
        assert capture_response["status"] == "ABORTED"
        assert not (tmp_path / "control-aborted.fits").exists()
    finally:
        capture_socket.close()
        control_socket.close()
        context.term()