import sys
import os
import time
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional
import zmq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_EXE = sys.executable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Watchdog): %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

SERVICES_SPEC = {
    "mount": {
        "script": "services/mount_tcp.py",
        "port": 5555,
        "ping_action": "GET_STATUS",
        "is_hardware": True
    },
    "focuser": {
        "script": "services/focuser_tcp.py",
        "port": 5556,
        "ping_action": "GET_POS",
        "is_hardware": True
    },
    "camera": {
        "script": "services/camera_tcp.py",
        "port": 5557,
        "ping_action": "GET_STATUS",
        "is_hardware": True
    },
    "orchestrator": {
        "script": "services/pipeline_orchestrator.py",
        "port": 5558,
        "ping_action": "GET_STATUS",
        "is_hardware": False
    }
}


class WatchdogManager:
    """
    Lifecycle supervisor for the RINGSS observatory suite.
    Applies conservative hardware preservation and quiet idle states for unplugged USB devices.
    """
    def __init__(
        self,
        mock_mode: bool = True,
        mount_progid: str = "ASCOM.ZWO.Telescope",
        focuser_progid: str = "ASCOM.EAF.Focuser",
        camera_progid: str = "ASCOM.ASICamera2.Camera"
    ):
        self.mock_mode = mock_mode
        self.progids = {
            "mount": mount_progid,
            "focuser": focuser_progid,
            "camera": camera_progid
        }
        self.processes: Dict[str, subprocess.Popen] = {}
        self.consecutive_failures: Dict[str, int] = {k: 0 for k in SERVICES_SPEC}
        self.service_status: Dict[str, str] = {k: "STOPPED" for k in SERVICES_SPEC}
        self.context = zmq.Context.instance()

    def start_service(self, name: str) -> bool:
        if name not in SERVICES_SPEC:
            logging.error(f"Unknown service: {name}")
            return False

        proc = self.processes.get(name)
        if proc is not None and proc.poll() is None:
            return True

        spec = SERVICES_SPEC[name]
        script_path = str((PROJECT_ROOT / spec["script"]).resolve())
        cmd = [PYTHON_EXE, "-u", script_path]

        if not self.mock_mode and spec["is_hardware"]:
            cmd.append("--real")
            progid = self.progids.get(name)
            if progid:
                cmd.extend(["--progid", progid])

        for attempt in range(1, 4):
            try:
                logging.info(f"Starting '{name}' (Attempt {attempt}/3, Mode: {'MOCK' if self.mock_mode else 'REAL'})...")
                p = subprocess.Popen(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self.processes[name] = p
                self.service_status[name] = "STARTING"
                self.consecutive_failures[name] = 0
                return True
            except PermissionError as pe:
                if attempt < 3:
                    time.sleep(0.4)
                    continue
                logging.error(f"Failed starting '{name}' after 3 attempts: {pe}")
                self.service_status[name] = "ERROR"
                return False
            except Exception as e:
                logging.error(f"Unexpected error starting '{name}': {e}")
                self.service_status[name] = "ERROR"
                return False

    def start_all_services(self) -> Dict[str, bool]:
        logging.info(f"Launching all RINGSS services [Mode: {'MOCK' if self.mock_mode else 'HARDWARE REAL'}]...")
        results = {}
        for name in ["mount", "focuser", "camera", "orchestrator"]:
            results[name] = self.start_service(name)
            time.sleep(0.55)
        return results

    def switch_mode(self, mock_mode: bool) -> Dict[str, bool]:
        logging.info(f">>> Switching cluster to mode: {'MOCK' if mock_mode else 'HARDWARE REAL (ASCOM/SDK)'} <<<")
        self.stop_all_services()
        self.mock_mode = bool(mock_mode)
        time.sleep(1.0)
        results = self.start_all_services()
        time.sleep(1.2)
        self.audit_and_recover()
        return results

    def ping_service(self, name: str, timeout_ms: int = 1200) -> bool:
        spec = SERVICES_SPEC[name]
        port = spec["port"]
        sock = self.context.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
        sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(f"tcp://localhost:{port}")

        try:
            sock.send_json({"action": spec["ping_action"]})
            resp = sock.recv_json()
            if isinstance(resp, dict) and resp.get("status") == "SUCCESS":
                if not self.mock_mode and spec["is_hardware"]:
                    if resp.get("mock") is True:
                        return False
                return True
            return False
        except Exception:
            return False
        finally:
            sock.close()

    def audit_and_recover(self):
        for name, spec in SERVICES_SPEC.items():
            if self.service_status[name] in ("STOPPED", "DISCONNECTED"):
                continue

            proc = self.processes.get(name)

            if proc is not None and proc.poll() is not None:
                exit_code = proc.returncode

                if not self.mock_mode:
                    if spec["is_hardware"]:
                        self.service_status[name] = "DISCONNECTED"
                        logging.warning(
                            f"Hardware device '{name}' not detected on USB/ASCOM bus (exit code {exit_code}). "
                            "Entering idle state (DISCONNECTED)."
                        )
                        continue
                    elif name == "orchestrator":
                        hw_disconnected = any(
                            self.service_status.get(hw) == "DISCONNECTED"
                            for hw in ["mount", "focuser", "camera"]
                        )
                        if hw_disconnected:
                            self.service_status[name] = "DISCONNECTED"
                            logging.warning("Orchestrator in idle state (DISCONNECTED) waiting for hardware.")
                            continue

                logging.warning(f"OS process '{name}' exited unexpectedly (code {exit_code}). Restarting...")
                self.service_status[name] = "DEAD"
                self.start_service(name)
                continue

            is_alive = self.ping_service(name)
            if is_alive:
                self.consecutive_failures[name] = 0
                self.service_status[name] = "HEALTHY"
            else:
                self.consecutive_failures[name] += 1
                fails = self.consecutive_failures[name]

                if spec["is_hardware"] and not self.mock_mode:
                    self.service_status[name] = "UNRESPONSIVE_PRESERVED"
                    logging.critical(
                        f"Physical hardware '{name}' unresponsive (failure #{fails}). "
                        f"Preserving process PID without termination."
                    )
                else:
                    self.service_status[name] = "DEGRADED"
                    if fails >= 3:
                        logging.warning(f"Restarting service '{name}' after {fails} consecutive ping failures.")
                        self._terminate_process(name)
                        self.start_service(name)

    def _terminate_process(self, name: str):
        proc = self.processes.pop(name, None)
        if proc is not None and proc.poll() is None:
            spec = SERVICES_SPEC.get(name)
            if spec:
                try:
                    sock = self.context.socket(zmq.REQ)
                    sock.setsockopt(zmq.RCVTIMEO, 1200)
                    sock.setsockopt(zmq.SNDTIMEO, 800)
                    sock.setsockopt(zmq.LINGER, 0)
                    sock.connect(f"tcp://localhost:{spec['port']}")
                    sock.send_json({"action": "SHUTDOWN"})
                    sock.recv_json()
                    sock.close()
                except Exception:
                    pass

            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        self.service_status[name] = "STOPPED"
        self.consecutive_failures[name] = 0

    def stop_all_services(self):
        logging.info("Stopping all RINGSS microservices cleanly...")
        for name in ["orchestrator", "camera", "focuser", "mount"]:
            self._terminate_process(name)
        logging.info("All services have been stopped.")

    def get_cluster_status(self) -> Dict[str, Any]:
        self.audit_and_recover()
        return {
            "mock_mode": self.mock_mode,
            "services": {
                name: {
                    "status": self.service_status[name],
                    "port": SERVICES_SPEC[name]["port"],
                    "pid": (
                        self.processes[name].pid
                        if (name in self.processes and self.processes[name].poll() is None)
                        else None
                    ),
                    "consecutive_failures": self.consecutive_failures[name]
                }
                for name in SERVICES_SPEC
            }
        }


def run_watchdog_server(port: int = 5559, mock_mode: bool = True):
    manager = WatchdogManager(mock_mode=mock_mode)
    manager.start_all_services()

    context = zmq.Context.instance()
    sock = context.socket(zmq.REP)
    sock.bind(f"tcp://*:{port}")

    logging.info(f"Watchdog Manager listening on tcp://*:{port}...")

    last_audit_time = time.time()
    try:
        while True:
            try:
                if sock.poll(500, zmq.POLLIN):
                    msg = sock.recv_json()
                    action = msg.get("action", "").upper()

                    if action == "GET_STATUS":
                        sock.send_json({"status": "SUCCESS", "cluster": manager.get_cluster_status()})
                    elif action == "START_ALL":
                        res = manager.start_all_services()
                        sock.send_json({"status": "SUCCESS", "results": res})
                    elif action == "STOP_ALL":
                        manager.stop_all_services()
                        sock.send_json({"status": "SUCCESS", "message": "Cluster stopped"})
                        # Salir del bucle para que el hilo termine ordenadamente
                        break
                    elif action == "SWITCH_MODE":
                        target_mock = bool(msg.get("mock_mode", True))
                        res = manager.switch_mode(target_mock)
                        sock.send_json({
                            "status": "SUCCESS",
                            "mock_mode": manager.mock_mode,
                            "results": res,
                            "cluster": manager.get_cluster_status()
                        })
                    elif action == "GET_MODE":
                        sock.send_json({"status": "SUCCESS", "mock_mode": manager.mock_mode})
                    elif action == "RESTART_SERVICE":
                        svc_name = msg.get("service", "")
                        manager._terminate_process(svc_name)
                        started = manager.start_service(svc_name)
                        sock.send_json({"status": "SUCCESS" if started else "ERROR", "service": svc_name})
                    else:
                        sock.send_json({"status": "ERROR", "message": f"Unknown action: {action}"})
            except zmq.error.Again:
                pass

            if time.time() - last_audit_time >= 2.5:
                manager.audit_and_recover()
                last_audit_time = time.time()

    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received in Watchdog.")
    finally:
        manager.stop_all_services()
        sock.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="RINGSS Watchdog Process Manager")
    parser.add_argument("--real", action="store_true", help="Monitor physical ASCOM/SDK hardware")
    parser.add_argument("--port", type=int, default=5559, help="Watchdog service port")
    args = parser.parse_args()

    run_watchdog_server(port=args.port, mock_mode=not args.real)