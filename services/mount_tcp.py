import sys
import json
import logging
import time
import math
from typing import Dict, Any, Tuple, Optional
import zmq

try:
    import win32com.client
    import pythoncom
    ASCOM_AVAILABLE = True
except ImportError:
    ASCOM_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Mount_TCP): %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


class MountHardwareDriver:
    """
    Controlador de la montura ZWO AM5 / AM5N (y monturas ASCOM estándar).
    Soporta modo Mock (simulación) y control físico real mediante ASCOM Telescope V3.
    """
    def __init__(self, mock_mode: bool = True, ascom_prog_id: str = "ASCOM.ZWO.Telescope"):
        self.mock_mode = mock_mode
        self.ascom_prog_id = ascom_prog_id
        self.telescope = None
        self._com_initialized = False

        # Coordenadas iniciales por defecto (cenit astronómico en Tololo / La Serena)
        self.current_ra = 15.9808    # Horas [0, 24)
        self.current_dec = -26.1142  # Grados [-90, +90]
        self.state = "IDLE"          # IDLE, SLEWING, TRACKING, PARKED, ERROR
        self.tracking_enabled = False
        self.tracking_rate = "Sidereal"
        self.nudge_rate = "1X"

        if not self.mock_mode:
            if not ASCOM_AVAILABLE:
                raise ImportError("pywin32 no está instalado. Ejecute 'pip install pywin32' para hardware real.")

            try:
                pythoncom.CoInitialize()
                self._com_initialized = True
                logging.info(f"Conectando a montura física vía ASCOM (ProgID: {self.ascom_prog_id})...")
                self.telescope = win32com.client.Dispatch(self.ascom_prog_id)
                self.telescope.Connected = True
                if not bool(self.telescope.Connected):
                    raise RuntimeError("El driver ASCOM de la montura rechazó la conexión (Connected=False).")

                self.current_ra = float(self.telescope.RightAscension)
                self.current_dec = float(self.telescope.Declination)
                self.tracking_enabled = bool(getattr(self.telescope, "Tracking", False))
                self.state = "TRACKING" if self.tracking_enabled else "IDLE"
                logging.info(f"Conexión ASCOM establecida: RA={self.current_ra:.4f}h, Dec={self.current_dec:.4f}°")
            except Exception as e:
                self.close()
                logging.error(f"Fallo al conectar con el driver ASCOM de la montura: {e}")
                raise RuntimeError(f"Error crítico de conexión ASCOM Mount: {e}")
        else:
            logging.info("MountHardwareDriver inicializado en [Mock Mode: True]")

    def close(self):
        """Libera ordenadamente la conexión ASCOM y el apartamento COM."""
        if not self.mock_mode:
            try:
                if self.telescope is not None:
                    self.telescope.Connected = False
            except Exception:
                pass
            finally:
                self.telescope = None
                if self._com_initialized and ASCOM_AVAILABLE:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
                    self._com_initialized = False

    @staticmethod
    def _validate_coordinates(ra: float, dec: float):
        if not (0.0 <= ra < 24.0):
            raise ValueError(f"RA fuera de rango válido [0, 24): {ra}")
        if not (-90.0 <= dec <= 90.0):
            raise ValueError(f"DEC fuera de rango válido [-90, 90]: {dec}")

    def get_status(self) -> Dict[str, Any]:
        if self.mock_mode:
            return {
                "mock": True,
                "hardware": False,
                "connected": True,
                "ra": round(self.current_ra, 6),
                "dec": round(self.current_dec, 6),
                "state": self.state,
                "tracking": self.tracking_enabled,
                "tracking_rate": self.tracking_rate,
                "nudge_rate": self.nudge_rate
            }
        else:
            try:
                connected = bool(self.telescope.Connected)
                if not connected:
                    raise RuntimeError("Montura física desconectada inesperadamente.")

                at_park = bool(getattr(self.telescope, "AtPark", False))
                slewing = bool(getattr(self.telescope, "Slewing", False))
                self.tracking_enabled = bool(getattr(self.telescope, "Tracking", False))

                if at_park:
                    self.state = "PARKED"
                elif slewing:
                    self.state = "SLEWING"
                elif self.tracking_enabled:
                    self.state = "TRACKING"
                else:
                    self.state = "IDLE"

                self.current_ra = float(self.telescope.RightAscension)
                self.current_dec = float(self.telescope.Declination)

                return {
                    "mock": False,
                    "hardware": True,
                    "connected": True,
                    "ra": round(self.current_ra, 6),
                    "dec": round(self.current_dec, 6),
                    "state": self.state,
                    "tracking": self.tracking_enabled,
                    "tracking_rate": self.tracking_rate,
                    "nudge_rate": self.nudge_rate
                }
            except Exception as e:
                self.state = "ERROR"
                logging.error(f"Error leyendo estado desde ASCOM Mount: {e}")
                raise RuntimeError(f"Fallo de telemetría ASCOM Mount: {e}")

    def goto(self, ra: float, dec: float) -> Tuple[float, float]:
        self._validate_coordinates(ra, dec)
        self.state = "SLEWING"
        logging.info(f"Iniciando GOTO hacia RA: {ra:.4f}h, DEC: {dec:.4f}° [Mock: {self.mock_mode}]")

        if self.mock_mode:
            time.sleep(0.3)
            self.current_ra = ra
            self.current_dec = dec
            self.tracking_enabled = True
            self.state = "TRACKING"
        else:
            try:
                if getattr(self.telescope, "AtPark", False):
                    if getattr(self.telescope, "CanUnpark", False):
                        self.telescope.Unpark()
                    else:
                        raise RuntimeError("La montura está aparcada. Desaparque antes de ejecutar GOTO.")

                if getattr(self.telescope, "CanSetTracking", False):
                    self.telescope.Tracking = True
                    self.tracking_enabled = True

                if getattr(self.telescope, "CanSlewAsync", False):
                    self.telescope.SlewToCoordinatesAsync(ra, dec)
                    t0 = time.time()
                    while getattr(self.telescope, "Slewing", False):
                        if time.time() - t0 > 70.0:
                            raise TimeoutError("Tiempo de espera agotado (70s) durante Slew asíncrono de la montura.")
                        time.sleep(0.15)
                else:
                    self.telescope.SlewToCoordinates(ra, dec)

                self.current_ra = float(self.telescope.RightAscension)
                self.current_dec = float(self.telescope.Declination)
                self.state = "TRACKING"
            except Exception as e:
                self.state = "ERROR"
                logging.error(f"Error ejecutando GOTO en ASCOM: {e}")
                raise RuntimeError(f"ASCOM GOTO falló: {e}")

        return self.current_ra, self.current_dec

    def nudge(self, direction: str, arcsec: float = 10.0, rate: Optional[str] = None) -> Tuple[float, float, float]:
        """
        Aplica corrección fina de centrado (N, S, E, W) en segundos de arco.
        Retorna (current_ra, current_dec, effective_arcsec).
        """
        dir_up = str(direction).upper().strip()
        if dir_up not in ("N", "S", "E", "W"):
            raise ValueError(f"Dirección de nudge inválida: {direction}. Use N, S, E, W.")
        if arcsec <= 0 or arcsec > 3600:
            raise ValueError(f"Magnitud de nudge fuera de rango (0, 3600] arcsec: {arcsec}")

        if rate is not None:
            self.nudge_rate = str(rate)

        mult = 1.0
        if "2X" in self.nudge_rate:
            mult = 2.0
        elif "8X" in self.nudge_rate:
            mult = 8.0

        effective_arcsec = arcsec * mult

        deg_delta = effective_arcsec / 3600.0
        cos_dec = max(0.015, math.cos(math.radians(self.current_dec)))
        ra_delta_h = effective_arcsec / (54000.0 * cos_dec)

        target_ra = self.current_ra
        target_dec = self.current_dec

        if dir_up == "N":
            target_dec = min(90.0, self.current_dec + deg_delta)
        elif dir_up == "S":
            target_dec = max(-90.0, self.current_dec - deg_delta)
        elif dir_up == "E":
            target_ra = (self.current_ra + ra_delta_h) % 24.0
        elif dir_up == "W":
            target_ra = (self.current_ra - ra_delta_h) % 24.0

        logging.info(
            f"Nudge {dir_up} ({effective_arcsec:.1f} arcsec [{self.nudge_rate}, cos(dec)={cos_dec:.3f}]) "
            f"-> Objetivo RA={target_ra:.5f}h, Dec={target_dec:.5f}°"
        )

        if self.mock_mode:
            time.sleep(0.1)
            self.current_ra = target_ra
            self.current_dec = target_dec
            return self.current_ra, self.current_dec, effective_arcsec

        can_pulse = bool(getattr(self.telescope, "CanPulseGuide", False))
        if can_pulse and effective_arcsec <= 60.0:
            ascom_dirs = {"N": 0, "S": 1, "E": 2, "W": 3}
            guide_rate_arcsec_s = 7.52
            try:
                if dir_up in ("N", "S") and hasattr(self.telescope, "GuideRateDeclination"):
                    gr = float(self.telescope.GuideRateDeclination) * 3600.0
                    if gr > 0: guide_rate_arcsec_s = gr
                elif dir_up in ("E", "W") and hasattr(self.telescope, "GuideRateRightAscension"):
                    gr = float(self.telescope.GuideRateRightAscension) * 3600.0
                    if gr > 0: guide_rate_arcsec_s = gr
            except Exception:
                pass

            duration_ms = int(max(20, min(10000, (effective_arcsec / guide_rate_arcsec_s) * 1000.0)))
            try:
                self.telescope.PulseGuide(ascom_dirs[dir_up], duration_ms)
                t0 = time.time()
                while bool(getattr(self.telescope, "IsPulseGuiding", False)):
                    if time.time() - t0 > (duration_ms / 1000.0 + 3.0):
                        break
                    time.sleep(0.02)
                self.current_ra = float(self.telescope.RightAscension)
                self.current_dec = float(self.telescope.Declination)
                return self.current_ra, self.current_dec, effective_arcsec
            except Exception as pe:
                logging.warning(f"PulseGuide falló ({pe}). Recurriendo a micro-GOTO...")

        final_ra, final_dec = self.goto(target_ra, target_dec)
        return final_ra, final_dec, effective_arcsec

    def set_tracking(self, enabled: bool, rate: str = "Sidereal") -> Dict[str, Any]:
        """Activa/desactiva el seguimiento sideral continuo."""
        self.tracking_enabled = bool(enabled)
        self.tracking_rate = str(rate)
        if not self.mock_mode:
            try:
                if getattr(self.telescope, "CanSetTracking", False):
                    self.telescope.Tracking = self.tracking_enabled
            except Exception as e:
                raise RuntimeError(f"Error configurando Tracking en ASCOM: {e}")

        self.state = "TRACKING" if self.tracking_enabled else "IDLE"
        logging.info(f"Tracking actualizado: {self.tracking_enabled} (Tasa: {self.tracking_rate})")
        return self.get_status()

    def home(self) -> str:
        """Envía la montura a su posición neutral mecánica (Home)."""
        logging.info("Enviando montura a posición Home...")
        self.state = "SLEWING"
        if self.mock_mode:
            time.sleep(0.4)
            self.current_ra = 0.0
            self.current_dec = 0.0
            self.tracking_enabled = False
            self.state = "IDLE"
        else:
            try:
                if getattr(self.telescope, "CanFindHome", False):
                    self.telescope.FindHome()
                else:
                    self.goto(0.0, 0.0)
                self.current_ra = float(self.telescope.RightAscension)
                self.current_dec = float(self.telescope.Declination)
                self.state = "IDLE"
            except Exception as e:
                self.state = "ERROR"
                logging.error(f"Error al enviar a Home en ASCOM: {e}")
                raise RuntimeError(f"ASCOM FindHome falló: {e}")
        return self.state

    def stop(self) -> str:
        """Parada de emergencia verificada (AbortSlew + corte de Tracking)."""
        logging.warning("Comando STOP de emergencia recibido. Abortando movimiento de montura...")
        if self.mock_mode:
            self.tracking_enabled = False
            self.state = "IDLE"
            return self.state
        else:
            try:
                if hasattr(self.telescope, "AbortSlew"):
                    self.telescope.AbortSlew()
                if getattr(self.telescope, "CanSetTracking", False):
                    self.telescope.Tracking = False
                self.tracking_enabled = False

                t0 = time.time()
                while getattr(self.telescope, "Slewing", False) and (time.time() - t0 < 2.0):
                    time.sleep(0.05)

                if getattr(self.telescope, "Slewing", False):
                    raise RuntimeError("La montura sigue reportando Slewing=True tras AbortSlew.")

                self.state = "IDLE"
                logging.info("Parada de emergencia confirmada por la montura ASCOM.")
                return self.state
            except Exception as e:
                self.state = "ERROR"
                logging.error(f"Error al abortar slew en ASCOM: {e}")
                raise RuntimeError(f"ASCOM AbortSlew falló: {e}")

    def park(self) -> str:
        self.state = "PARKING"
        logging.info("Aparcando la montura...")
        if self.mock_mode:
            time.sleep(0.4)
            self.current_ra = 0.0
            self.current_dec = -90.0
            self.tracking_enabled = False
            self.state = "PARKED"
        else:
            try:
                if getattr(self.telescope, "CanPark", False):
                    self.telescope.Park()
                else:
                    if hasattr(self.telescope, "AbortSlew"):
                        self.telescope.AbortSlew()
                    if getattr(self.telescope, "CanSetTracking", False):
                        self.telescope.Tracking = False
                self.tracking_enabled = False
                self.state = "PARKED"
                logging.info("Montura aparcada exitosamente.")
            except Exception as e:
                self.state = "ERROR"
                logging.error(f"Error al aparcar en ASCOM: {e}")
                raise RuntimeError(f"ASCOM Park falló: {e}")
        return self.state


def run_mount_server(port: int = 5555, mock_mode: bool = True, ascom_prog_id: str = "ASCOM.ZWO.Telescope"):
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    driver = None
    try:
        try:
            socket.bind(f"tcp://*:{port}")
        except zmq.error.ZMQError as ze:
            logging.info(f"Puerto {port} ya activo en esta sesión ({ze}).")
            return

        driver = MountHardwareDriver(mock_mode=mock_mode, ascom_prog_id=ascom_prog_id)
        logging.info(f"Servidor Mount_TCP escuchando en tcp://*:{port} [Mock Mode: {mock_mode}]...")

        while True:
            try:
                message = socket.recv_json()
                action = message.get("action", "").upper()

                if action == "GET_STATUS":
                    status = driver.get_status()
                    response = {"status": "SUCCESS", **status}

                elif action == "GOTO":
                    ra = message.get("ra")
                    dec = message.get("dec")
                    if ra is None or dec is None:
                        raise ValueError("Parámetros 'ra' y 'dec' son obligatorios para GOTO.")
                    final_ra, final_dec = driver.goto(float(ra), float(dec))
                    response = {
                        "status": "SUCCESS",
                        "message": f"Objetivo alcanzado (RA={final_ra:.4f}h, Dec={final_dec:.4f}°)",
                        "ra": final_ra,
                        "dec": final_dec,
                        "state": driver.state
                    }

                elif action == "NUDGE":
                    direction = message.get("direction", "N")
                    arcsec = float(message.get("arcsec", 10.0))
                    rate = message.get("rate")
                    final_ra, final_dec, effective_arcsec = driver.nudge(direction, arcsec, rate=rate)
                    response = {
                        "status": "SUCCESS",
                        "message": f"Nudge {direction} ({effective_arcsec:.1f} arcsec [{driver.nudge_rate}]) aplicado",
                        "ra": final_ra,
                        "dec": final_dec,
                        "state": driver.state
                    }

                elif action == "SET_TRACKING":
                    enabled = bool(message.get("enabled", True))
                    rate = str(message.get("rate", "Sidereal"))
                    st = driver.set_tracking(enabled=enabled, rate=rate)
                    response = {"status": "SUCCESS", "message": f"Tracking {'ON' if enabled else 'OFF'} ({rate})", **st}

                elif action == "HOME":
                    state = driver.home()
                    response = {"status": "SUCCESS", "message": "Montura en posición Home", "state": state}

                elif action == "STOP":
                    state = driver.stop()
                    response = {"status": "SUCCESS", "message": "Parada confirmada", "state": state}

                elif action == "PARK":
                    state = driver.park()
                    response = {"status": "SUCCESS", "message": "Montura aparcada", "state": state}

                else:
                    response = {"status": "ERROR", "message": f"Acción desconocida: {action}"}

                socket.send_json(response)

            except ValueError as ve:
                logging.error(f"Error de validación en Mount_TCP: {ve}")
                socket.send_json({"status": "ERROR", "message": str(ve)})
            except Exception as e:
                logging.exception("Error procesando comando en Mount_TCP.")
                socket.send_json({"status": "ERROR", "message": str(e)})

    except KeyboardInterrupt:
        logging.info("Apagando servidor Mount_TCP...")
    finally:
        if driver is not None:
            driver.close()
        socket.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mount TCP Microservice (ZWO AM5 / ASCOM)")
    parser.add_argument("--real", action="store_true", help="Conectar a hardware real vía ASCOM")
    parser.add_argument("--progid", type=str, default="ASCOM.ZWO.Telescope", help="ProgID del driver ASCOM")
    args = parser.parse_args()

    run_mount_server(port=5555, mock_mode=not args.real, ascom_prog_id=args.progid)