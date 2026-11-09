"""
RINGSS Observatory Suite - Master Launcher
Starts the Watchdog Manager as a background server (port 5559) to oversee
the microservices (Mount 5555, Focuser 5556, Camera 5557, Orchestrator 5558),
enables hot switching (Mock <-> Hardware), and launches the mission control GUI.
On exit, terminates all child processes cleanly.
"""

import sys
import os
import time
import argparse
import logging
import threading
from pathlib import Path
import zmq

# Forzar explícitamente PySide6 para evitar conflictos de bibliotecas Qt cruzadas
os.environ["QT_API"] = "pyside6"

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.watchdog_manager import run_watchdog_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Launcher): %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


def ping_watchdog(port: int = 5559, timeout_ms: int = 600) -> bool:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
    sock.setsockopt(zmq.LINGER, 0)
    try:
        sock.connect(f"tcp://localhost:{port}")
        sock.send_json({"action": "GET_MODE"})
        resp = sock.recv_json()
        return isinstance(resp, dict) and resp.get("status") == "SUCCESS"
    except Exception:
        return False
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description="RINGSS Master Observatory Suite Launcher")
    parser.add_argument("--real", action="store_true", help="Operate with physical ASCOM/SDK hardware")
    parser.add_argument("--no-gui", action="store_true", help="Launch microservices only without GUI")
    parser.add_argument("--port", type=int, default=5559, help="Watchdog service port")
    args = parser.parse_args()

    mock_mode = not args.real
    logging.info(f"Starting RINGSS Observatory Suite [Initial Mode: {'MOCK' if mock_mode else 'PHYSICAL HARDWARE'}]...")

    logging.info(f"Launching Watchdog Manager in background on port {args.port}...")
    wd_thread = threading.Thread(
        target=run_watchdog_server,
        kwargs={"port": args.port, "mock_mode": mock_mode},
        daemon=True
    )
    wd_thread.start()

    logging.info("Waiting for microservice cluster initialization and stabilization...")
    t0 = time.time()
    wd_ready = False
    while time.time() - t0 < 12.0:
        if ping_watchdog(port=args.port):
            wd_ready = True
            break
        time.sleep(0.4)

    if wd_ready:
        logging.info("[OK] Watchdog and microservices online and listening on assigned ports.")
    else:
        logging.warning("[WARN] Watchdog initialization took longer than expected. Continuing launch...")

    if args.no_gui:
        logging.info("Headless mode active (--no-gui). Press Ctrl+C to terminate services.")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            logging.info("Stopping services...")
        return

    logging.info("Launching RINGSS Mission Control GUI...")
    try:
        from PySide6.QtWidgets import QApplication
        from services.gui_app import RingssMainWindow

        app = QApplication(sys.argv)
        window = RingssMainWindow()

        if hasattr(window, "mode_selector"):
            window.mode_selector.blockSignals(True)
            window.mode_selector.setCurrentIndex(0 if mock_mode else 1)
            window.current_mode = "MOCK" if mock_mode else "REAL"
            window.mode_selector.blockSignals(False)

        window.show()
        exit_code = app.exec()
        logging.info(f"GUI closed (exit code: {exit_code}).")

    except ImportError:
        logging.error("PySide6 is not available. Install it with: pip install PySide6")
    except Exception as e:
        logging.exception(f"Error during GUI execution: {e}")
    finally:
        logging.info("Sending orderly shutdown signal to cluster...")
        try:
            ctx = zmq.Context.instance()
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, 2500)
            sock.setsockopt(zmq.SNDTIMEO, 2500)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(f"tcp://localhost:{args.port}")
            sock.send_json({"action": "STOP_ALL"})
            sock.recv_json()
            sock.close()
        except Exception:
            pass

        # Esperar a que el hilo del Watchdog termine antes de salir para evitar el error 0xC0000005
        if "wd_thread" in locals() and wd_thread.is_alive():
            wd_thread.join(timeout=3.0)

        logging.info("RINGSS Suite shut down cleanly. Clear skies!")


if __name__ == "__main__":
    main()