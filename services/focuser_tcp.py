import sys
import json
import logging
import time
from typing import Dict, Any, Optional
import zmq

try:
    import win32com.client
    import pythoncom
    ASCOM_AVAILABLE = True
except ImportError:
    ASCOM_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Focuser_TCP): %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


class FocuserHardwareDriver:
    """
    Controlador del enfocador electrónico ZWO EAF (y enfocadores ASCOM estándar).
    Soporta modo Mock y control físico real vía ASCOM Focuser V3.
    """
    def __init__(self, mock_mode: bool = True, ascom_prog_id: str = "ASCOM.EAF.Focuser", default_ring_offset: int = 450):
        self.mock_mode = mock_mode
        self.ascom_prog_id = ascom_prog_id
        self.focuser = None
        self._com_initialized = False

        self.current_position = 24420
        self.target_ring_offset = default_ring_offset
        self.last_focused_position = 24420
        self.is_moving = False

        if not self.mock_mode:
            if not ASCOM_AVAILABLE:
                raise ImportError("pywin32 no está instalado. Ejecute 'pip install pywin32' para hardware real.")

            try:
                pythoncom.CoInitialize()
                self._com_initialized = True
                logging.info(f"Conectando a enfocador físico vía ASCOM (ProgID: {self.ascom_prog_id})...")
                self.focuser = win32com.client.Dispatch(self.ascom_prog_id)
                self.focuser.Connected = True
                if not bool(self.focuser.Connected):
                    raise RuntimeError("El driver ASCOM del enfocador rechazó la conexión (Connected=False).")
                self.current_position = int(self.focuser.Position)
                self.last_focused_position = self.current_position
                logging.info(f"¡Conexión ASCOM Focuser establecida! Posición actual: {self.current_position}")
            except Exception as e:
                self.close()
                logging.error(f"Fallo al conectar con el driver ASCOM del enfocador: {e}")
                raise RuntimeError(f"Error crítico de conexión ASCOM Focuser: {e}")
        else:
            logging.info(f"FocuserHardwareDriver inicializado en [Mock Mode: True | Offset Anillo: {self.target_ring_offset} pasos]")

    def close(self):
        """Libera ordenadamente la conexión ASCOM y el apartamento COM del hilo."""
        if not self.mock_mode:
            try:
                if self.focuser is not None:
                    self.focuser.Connected = False
            except Exception:
                pass
            finally:
                self.focuser = None
                if self._com_initialized and ASCOM_AVAILABLE:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
                    self._com_initialized = False
        logging.info("Controlador de enfocador cerrado limpiamente.")

    def get_position(self) -> Dict[str, Any]:
        if self.mock_mode:
            return {
                "mock": True,
                "hardware": False,
                "connected": True,
                "position": self.current_position,
                "is_moving": self.is_moving
            }
        else:
            try:
                if not bool(self.focuser.Connected):
                    raise RuntimeError("Enfocador físico desconectado.")
                self.current_position = int(self.focuser.Position)
                self.is_moving = bool(getattr(self.focuser, "IsMoving", False))
                return {
                    "mock": False,
                    "hardware": True,
                    "connected": True,
                    "position": self.current_position,
                    "is_moving": self.is_moving
                }
            except Exception as e:
                logging.error(f"Error leyendo posición desde ASCOM Focuser: {e}")
                raise RuntimeError(f"ASCOM Position falló: {e}")

    def move_abs(self, position: int) -> int:
        target = int(position)
        if target < 0 or target > 100000:
            raise ValueError(f"Posición absoluta fuera de rango seguro [0, 100000]: {target}")

        logging.info(f"Moviendo enfocador a posición ABSOLUTA: {target} [Mock: {self.mock_mode}]")

        if self.mock_mode:
            self.is_moving = True
            time.sleep(0.15)
            self.current_position = target
            self.is_moving = False
        else:
            try:
                self.focuser.Move(target)
                t0 = time.time()
                while bool(getattr(self.focuser, "IsMoving", False)):
                    if time.time() - t0 > 30.0:
                        raise TimeoutError("Tiempo de espera agotado moviendo enfocador EAF.")
                    time.sleep(0.05)
                self.current_position = int(self.focuser.Position)
                self.is_moving = False
            except Exception as e:
                self.is_moving = False
                logging.error(f"Error en Move Absoluto ASCOM: {e}")
                raise RuntimeError(f"ASCOM Move Abs falló: {e}")

        return self.current_position

    def move_rel(self, steps: int) -> int:
        target_pos = self.current_position + int(steps)
        if target_pos < 0:
            raise ValueError(f"El movimiento relativo ({steps:+d}) causaría una posición negativa ({target_pos}).")
        logging.info(f"Moviendo enfocador RELATIVO: {steps:+d} pasos [Mock: {self.mock_mode}]")
        return self.move_abs(target_pos)

    def halt(self) -> Dict[str, Any]:
        """Detiene inmediatamente cualquier movimiento del motor EAF (Parada de Emergencia)."""
        logging.warning("¡Comando HALT recibido en enfocador! Deteniendo motor EAF...")
        if self.mock_mode:
            self.is_moving = False
            return {"status": "SUCCESS", "message": "Focuser halted (Mock)", "position": self.current_position}
        else:
            try:
                can_halt = bool(getattr(self.focuser, "CanHalt", False))
                if can_halt:
                    self.focuser.Halt()
                else:
                    logging.warning("El driver ASCOM reporta CanHalt=False. Deteniendo movimiento fijando posición actual...")
                    try:
                        cur_pos = int(self.focuser.Position)
                        self.focuser.Move(cur_pos)
                    except Exception:
                        pass

                t0 = time.time()
                while bool(getattr(self.focuser, "IsMoving", False)) and (time.time() - t0 < 2.0):
                    time.sleep(0.05)

                self.is_moving = bool(getattr(self.focuser, "IsMoving", False))
                self.current_position = int(self.focuser.Position)

                if self.is_moving:
                    raise RuntimeError("El enfocador continúa reportando IsMoving=True tras Halt().")

                return {"status": "SUCCESS", "message": "Focuser halt confirmed", "position": self.current_position}
            except Exception as e:
                logging.error(f"Error ejecutando Halt en ASCOM Focuser: {e}")
                raise RuntimeError(f"ASCOM Focuser Halt falló: {e}")

    def apply_ring_defocus(self) -> Dict[str, Any]:
        """Aplica el offset calibrado para formar el anillo de difracción RINGSS."""
        if not self.mock_mode:
            self.current_position = int(self.focuser.Position)
        self.last_focused_position = self.current_position
        target = self.last_focused_position + self.target_ring_offset
        self.move_abs(target)
        return {
            "status": "SUCCESS",
            "message": "Defocus ring offset applied",
            "previous_focus": self.last_focused_position,
            "ring_position": self.current_position,
            "offset_used": self.target_ring_offset
        }

    def return_to_focus(self) -> Dict[str, Any]:
        """Retorna el enfocador a la posición de foco nítido previa al anillo."""
        logging.info(f"Retornando enfocador al foco previo: {self.last_focused_position}")
        self.move_abs(self.last_focused_position)
        return {
            "status": "SUCCESS",
            "message": "Returned to sharp focus position",
            "position": self.current_position
        }


def run_focuser_server(port: int = 5556, mock_mode: bool = True, ascom_prog_id: str = "ASCOM.EAF.Focuser", offset: int = 450):
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    driver = None
    try:
        try:
            socket.bind(f"tcp://*:{port}")
        except zmq.error.ZMQError as ze:
            logging.info(f"Puerto {port} ya activo en esta sesión ({ze}).")
            return

        driver = FocuserHardwareDriver(mock_mode=mock_mode, ascom_prog_id=ascom_prog_id, default_ring_offset=offset)
        logging.info(f"Servidor Focuser_TCP escuchando en tcp://*:{port} [Mock Mode: {mock_mode}]...")

        while True:
            try:
                message = socket.recv_json()
                action = message.get("action", "").upper()
                logging.info(f"Comando recibido: {action} | Payload: {message}")

                if action in ("GET_POS", "GET_STATUS"):
                    status = driver.get_position()
                    response = {"status": "SUCCESS", **status}

                elif action == "MOVE_ABS":
                    pos = message.get("position")
                    if pos is None:
                        raise ValueError("Parámetro 'position' obligatorio para MOVE_ABS.")
                    new_pos = driver.move_abs(int(pos))
                    response = {"status": "SUCCESS", "position": new_pos}

                elif action == "MOVE_REL":
                    steps = message.get("steps")
                    if steps is None:
                        raise ValueError("Parámetro 'steps' obligatorio para MOVE_REL.")
                    new_pos = driver.move_rel(int(steps))
                    response = {"status": "SUCCESS", "position": new_pos}

                elif action in ("HALT", "STOP"):
                    response = driver.halt()

                elif action == "APPLY_RING_DEFOCUS":
                    response = driver.apply_ring_defocus()

                elif action == "RETURN_TO_FOCUS":
                    response = driver.return_to_focus()

                elif action == "SHUTDOWN":
                    socket.send_json({"status": "SUCCESS", "message": "Focuser server shutting down cleanly"})
                    break

                else:
                    response = {"status": "ERROR", "message": f"Acción desconocida: {action}"}

                socket.send_json(response)

            except ValueError as ve:
                logging.error(f"Error de validación en Focuser_TCP: {ve}")
                socket.send_json({"status": "ERROR", "message": str(ve)})
            except Exception as e:
                logging.exception("Error interno procesando comando de enfocador.")
                socket.send_json({"status": "ERROR", "message": str(e)})

    except KeyboardInterrupt:
        logging.info("Apagando servidor Focuser_TCP...")
    finally:
        if driver is not None:
            driver.close()
        socket.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Focuser TCP Microservice (ZWO EAF)")
    parser.add_argument("--real", action="store_true", help="Conectar a hardware real vía ASCOM Focuser")
    parser.add_argument("--progid", type=str, default="ASCOM.EAF.Focuser", help="ProgID del driver ASCOM Focuser")
    parser.add_argument("--offset", type=int, default=450, help="Pasos de offset para el anillo RINGSS")
    args = parser.parse_args()

    run_focuser_server(port=5556, mock_mode=not args.real, ascom_prog_id=args.progid, offset=args.offset)