import sys
import socket
import threading
import time
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.watchdog_manager import WatchdogManager, run_watchdog_server

@pytest.fixture
def watchdog_instance():
    """Instancia de WatchdogManager en modo Mock para tests."""
    wd = WatchdogManager(mock_mode=True)
    yield wd
    wd.stop_all_services()


def test_watchdog_cluster_lifecycle(watchdog_instance):
    # 1. Arrancar clúster de microservicios
    results = watchdog_instance.start_all_services()
    assert all(results.values()) is True, f"Fallo en arranque de subprocesos: {results}"

    # 2. Sondeo activo: esperar a que cada servicio levante y responda (hasta 6 segundos por servicio)
    for name in ["mount", "focuser", "camera", "orchestrator"]:
        is_ready = False
        start_wait = time.time()
        
        while time.time() - start_wait < 6.0:
            if watchdog_instance.ping_service(name, timeout_ms=500):
                is_ready = True
                break
            time.sleep(0.25)
            
        assert is_ready is True, f"El microservicio '{name}' no respondió en el tiempo límite."

    # 3. Validar estado general del clúster
    status = watchdog_instance.get_cluster_status()
    assert status["mock_mode"] is True
    child_processes = list(watchdog_instance.processes.values())
    for name in ["mount", "focuser", "camera", "orchestrator"]:
        assert status["services"][name]["pid"] is not None

    # 4. Detener clúster limpiamente
    watchdog_instance.stop_all_services()
    assert all(process.poll() is not None for process in child_processes)
    assert all(status == "STOPPED" for status in watchdog_instance.service_status.values())


def test_watchdog_conservative_hardware_policy():
    """Valida que los procesos de hardware físico no sean terminados arbitrariamente ante fallos de red."""
    wd = WatchdogManager(mock_mode=False)  # Modo Hardware
    wd.service_status["mount"] = "STARTING"
    wd.consecutive_failures["mount"] = 2
    
    wd.audit_and_recover()
    
    assert wd.service_status["mount"] == "UNRESPONSIVE_PRESERVED"
    assert wd.consecutive_failures["mount"] == 3


def test_watchdog_bind_failure_does_not_start_children(monkeypatch):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        startup_event = threading.Event()
        monkeypatch.setattr(
            WatchdogManager,
            "start_all_services",
            lambda manager: pytest.fail("Children must not start when the watchdog port is occupied"),
        )

        run_watchdog_server(port=port, startup_event=startup_event)

    assert startup_event.is_set()