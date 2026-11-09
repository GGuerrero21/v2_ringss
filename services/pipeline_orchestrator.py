import sys
import os
import time
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import zmq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.science_adapter import RingssSciencePipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Orchestrator): %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

CAPTURES_DIR = PROJECT_ROOT / "data" / "captures"
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)


class PipelineOrchestrator:
    """
    Orquestador científico de la misión RINGSS.
    Coordina de forma atómica el enfocador (5556), la cámara (5557), la montura (5555)
    y el núcleo de reducción astrofísica (RingssSciencePipeline).
    """
    def __init__(self, mount_port: int = 5555, focuser_port: int = 5556, camera_port: int = 5557):
        self.mount_port = mount_port
        self.focuser_port = focuser_port
        self.camera_port = camera_port

        self.state = "IDLE"
        self.abort_requested = False
        self.context = zmq.Context.instance()
        self.pipeline = RingssSciencePipeline()

    def _send_request(self, port: int, action: str, payload: Optional[Dict[str, Any]] = None, timeout_ms: int = 5000) -> Dict[str, Any]:
        sock = self.context.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
        sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(f"tcp://localhost:{port}")

        req = {"action": action}
        if payload is not None:
            req.update(payload)

        try:
            sock.send_json(req)
            resp = sock.recv_json()
            if not isinstance(resp, dict):
                raise ValueError(f"Respuesta inválida en puerto {port}: se esperaba dict, recibido {type(resp)}")
            if "status" not in resp:
                raise KeyError(f"Violación de contrato en puerto {port}: falta campo obligatorio 'status'")
            return resp
        finally:
            sock.close()

    def pre_flight_check(self) -> Dict[str, bool]:
        logging.info("Ejecutando Pre-Flight Check de microservicios...")
        health = {"mount": False, "focuser": False, "camera": False}

        for key, port, act in [
            ("mount", self.mount_port, "GET_STATUS"),
            ("focuser", self.focuser_port, "GET_POS"),
            ("camera", self.camera_port, "GET_STATUS")
        ]:
            try:
                res = self._send_request(port, act, timeout_ms=1500)
                if res.get("status") == "SUCCESS":
                    health[key] = True
            except Exception as e:
                logging.warning(f"Microservicio en puerto {port} ({key}) no superó pre-flight: {e}")

        return health

    def execute_ringss_sequence(
        self,
        exp_ms: float = 1.0,
        gain: int = 100,
        frames_cube: int = 100,
        target_star: str = "HR 5944"
    ) -> Dict[str, Any]:
        if self.state == "RUNNING":
            return {"status": "ERROR", "message": "Ya existe una secuencia científica en ejecución."}

        self.state = "RUNNING"
        self.abort_requested = False
        initial_eaf_pos = None

        try:
            logging.info(f">>> Iniciando secuencia científica RINGSS (Objetivo: {target_star}) <<<")

            # Paso 1: Registrar posición de foco nítido base
            logging.info("[Paso 1/5] Registrando foco base en EAF...")
            foc_res = self._send_request(self.focuser_port, "GET_POS")
            if foc_res.get("status") != "SUCCESS" or "position" not in foc_res:
                raise RuntimeError(f"Violación de contrato o fallo al leer posición del enfocador: {foc_res}")
            initial_eaf_pos = foc_res["position"]

            if self.abort_requested:
                raise InterruptedError("Operación abortada por usuario.")

            # Paso 2: Aplicar offset calibrado para formar el anillo de difracción
            logging.info("[Paso 2/5] Aplicando offset de desenfoque RINGSS en EAF...")
            defocus_res = self._send_request(self.focuser_port, "APPLY_RING_DEFOCUS")
            if defocus_res.get("status") != "SUCCESS":
                raise RuntimeError(f"Fallo aplicando desenfoque: {defocus_res.get('message')}")

            if self.abort_requested:
                raise InterruptedError("Operación abortada por usuario.")

            # Paso 3: Configurar cámara forzando explícitamente ROI 64x64 para el cubo científico
            logging.info(f"[Paso 3/5] Configurando cámara ({exp_ms} ms, ganancia {gain}, ROI 64x64)...")
            cfg_res = self._send_request(
                self.camera_port,
                "SET_CONFIG",
                {"exposure_ms": exp_ms, "gain": gain, "roi": "64x64"},
                timeout_ms=3000
            )
            if cfg_res.get("status") != "SUCCESS":
                raise RuntimeError(f"Fallo configurando cámara: {cfg_res.get('message')}")

            if self.abort_requested:
                raise InterruptedError("Operación abortada por usuario.")

            # Paso 4: Adquirir cubo FITS pasando frames y estrella seleccionada
            fits_filename = str(CAPTURES_DIR / f"ring_cube_{int(time.time())}.fits")
            logging.info(f"[Paso 4/5] Adquiriendo cubo FITS ({fits_filename}, frames={frames_cube}, star={target_star})...")
            cap_res = self._send_request(
                self.camera_port,
                "CAPTURE",
                {
                    "filename": fits_filename,
                    "frames": int(frames_cube),
                    "star": target_star
                },
                timeout_ms=30000
            )
            if cap_res.get("status") != "SUCCESS":
                raise RuntimeError(f"Fallo en adquisición de cámara: {cap_res.get('message')}")

            # Paso 5: Retornar enfocador inmediatamente al punto de foco óptimo
            logging.info(f"[Paso 5/5] Retornando enfocador a foco óptimo ({initial_eaf_pos})...")
            self._send_request(self.focuser_port, "RETURN_TO_FOCUS", timeout_ms=4000)

            # Reducción matemática con el núcleo científico de Tokovinin
            logging.info("Ejecutando reducción astrofísica del cubo con ringss_restore_profile...")
            science_results = self.pipeline.process_cube(fits_filename, star_hr=target_star)

            self._cleanup_old_captures(keep_last=10)

            self.state = "IDLE"
            logging.info(
                f">>> Reducción finalizada: Seeing = {science_results['seeing_arcsec']} arcsec, "
                f"r0 = {science_results['fried_r0_cm']} cm, Viento = {science_results['wind_speed_ms']} m/s <<<"
            )

            return {
                "status": "SUCCESS",
                "filename": fits_filename,
                **science_results,
                "timestamp": time.time()
            }

        except InterruptedError as ie:
            logging.warning(f"Secuencia interrumpida: {ie}")
            self._safety_rollback(initial_eaf_pos)
            self.state = "IDLE"
            return {"status": "ABORTED", "message": str(ie)}

        except Exception as e:
            logging.exception("Fallo durante la secuencia científica.")
            self._safety_rollback(initial_eaf_pos)
            self.state = "ERROR"
            return {"status": "ERROR", "message": str(e)}

    def _cleanup_old_captures(self, keep_last: int = 10):
        try:
            files = sorted(CAPTURES_DIR.glob("ring_cube_*.fits"), key=lambda p: p.stat().st_mtime)
            if len(files) > keep_last:
                for old_f in files[:-keep_last]:
                    try:
                        old_f.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    def _safety_rollback(self, target_eaf_pos: Optional[int]):
        logging.info("Ejecutando rollback de seguridad en hardware...")
        if target_eaf_pos is not None:
            try:
                self._send_request(self.focuser_port, "RETURN_TO_FOCUS", timeout_ms=3000)
            except Exception as e:
                logging.error(f"Rollback de enfocador falló: {e}")

    def abort_sequence(self):
        logging.warning("¡Señal de ABORT recibida en Orchestrator! Deteniendo montura y enfocador...")
        self.abort_requested = True
        self.state = "ABORTING"
        try:
            self._send_request(self.mount_port, "STOP", timeout_ms=1000)
        except Exception:
            pass
        try:
            self._send_request(self.focuser_port, "HALT", timeout_ms=1000)
        except Exception:
            pass
        self.state = "IDLE"


def run_orchestrator_service(port: int = 5558):
    context = zmq.Context.instance()
    sock = context.socket(zmq.REP)
    try:
        try:
            sock.bind(f"tcp://*:{port}")
        except zmq.error.ZMQError as ze:
            logging.info(f"Puerto {port} ya activo en esta sesión ({ze}).")
            return

        orchestrator = PipelineOrchestrator()
        logging.info(f"Pipeline Orchestrator escuchando en tcp://*:{port}...")

        while True:
            msg = sock.recv_json()
            action = msg.get("action", "").upper()

            if action == "PREFLIGHT":
                res = orchestrator.pre_flight_check()
                sock.send_json({"status": "SUCCESS", "health": res})

            elif action == "START_RINGSS_RUN":
                exp = float(msg.get("exposure_ms", 1.0))
                gain = int(msg.get("gain", 100))
                frames = int(msg.get("frames_cube", 100))
                star = str(msg.get("target_star", "HR 5944"))
                res = orchestrator.execute_ringss_sequence(
                    exp_ms=exp,
                    gain=gain,
                    frames_cube=frames,
                    target_star=star
                )
                sock.send_json(res)

            elif action == "ABORT":
                orchestrator.abort_sequence()
                sock.send_json({"status": "SUCCESS", "message": "Abort signal dispatched to Mount and Focuser"})

            elif action == "GET_STATUS":
                sock.send_json({"status": "SUCCESS", "state": orchestrator.state})

            elif action == "SHUTDOWN":
                sock.send_json({"status": "SUCCESS", "message": "Pipeline Orchestrator shutting down cleanly"})
                break

            else:
                sock.send_json({"status": "ERROR", "message": f"Acción desconocida: {action}"})

    except KeyboardInterrupt:
        logging.info("Apagando Pipeline Orchestrator...")
    finally:
        sock.close()


if __name__ == "__main__":
    run_orchestrator_service(port=5558)