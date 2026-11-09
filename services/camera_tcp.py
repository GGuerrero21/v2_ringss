import sys
import os
import json
import logging
import time
import re
import base64
from pathlib import Path
from typing import Dict, Any, Optional
import zmq
import numpy as np
from astropy.io import fits

try:
    import zwoasi as asi
    ZWO_SDK_AVAILABLE = True
except ImportError:
    ZWO_SDK_AVAILABLE = False

try:
    import win32com.client
    import pythoncom
    ASCOM_CAMERA_AVAILABLE = True
except ImportError:
    ASCOM_CAMERA_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Camera_TCP): %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


def find_zwo_asi_dll() -> str:
    """Localiza defensivamente ASICamera2.dll en el sistema Windows / Linux."""
    env_path = os.environ.get("ZWO_ASI_LIB")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = [
        "ASICamera2.dll",
        "libASICamera2.so",
        r"C:\Program Files\ZWO Design\ASIStudio\ASICamera2.dll",
        r"C:\Program Files (x86)\ZWO Design\ASIStudio\ASICamera2.dll",
        r"C:\ASI_SDK\lib\x64\ASICamera2.dll",
        r"C:\ASI_SDK\lib\x86\ASICamera2.dll",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "ASICamera2.dll"


class CameraHardwareDriver:
    """
    Controlador de la cámara astronómica ZWO ASI662MM (1920x1080 nativo, píxel 2.9 µm, sensor IMX662).
    Soporta modo Mock y control físico real mediante SDK nativo (zwoasi) y ASCOM Camera.
    """
    SENSOR_WIDTH = 1920
    SENSOR_HEIGHT = 1080

    def __init__(self, mock_mode: bool = True, camera_id: int = 0, ascom_prog_id: str = "ASCOM.ASICamera2.Camera"):
        self.mock_mode = mock_mode
        self.camera_id = camera_id
        self.ascom_prog_id = ascom_prog_id
        self.backend_type = "MOCK"  # MOCK, ZWO_SDK, ASCOM
        self._com_initialized = False

        self.exposure_ms = 1.0      # 1000 µs nominal científico para RINGSS (texp = 1e-3)
        self.gain = 100
        self.binning = 1
        self.roi_width = 64
        self.roi_height = 64
        self.state = "IDLE"         # IDLE, EXPOSURING, ERROR
        self.camera_obj = None
        self.last_frame_timestamp = 0.0
        self.last_health_timestamp = time.time()

        if not self.mock_mode:
            if ZWO_SDK_AVAILABLE:
                try:
                    dll_path = find_zwo_asi_dll()
                    logging.info(f"Inicializando SDK nativo ZWO ASI con DLL: {dll_path}...")
                    try:
                        asi.init(dll_path)
                    except Exception as init_err:
                        logging.debug(f"Aviso en asi.init: {init_err}")

                    num_cams = asi.get_num_cameras()
                    if num_cams <= 0:
                        raise RuntimeError("No se detectaron cámaras ZWO ASI conectadas por bus USB.")

                    self.camera_obj = asi.Camera(self.camera_id)
                    cam_info = self.camera_obj.get_camera_property()
                    self.backend_type = "ZWO_SDK"
                    self._apply_zwo_sdk_settings()
                    logging.info(f"¡Cámara ZWO conectada vía SDK! Modelo: {cam_info.get('Name', 'ASI662MM')}")
                except Exception as e:
                    logging.warning(f"No se pudo inicializar vía SDK nativo ({e}). Conmutando a ASCOM Camera...")
                    self._init_ascom(self.ascom_prog_id)
            elif ASCOM_CAMERA_AVAILABLE:
                self._init_ascom(self.ascom_prog_id)
            else:
                raise ImportError("No hay librerías de cámara instaladas (ni zwoasi ni pywin32/ASCOM).")
        else:
            logging.info("CameraHardwareDriver inicializado en [Mock Mode: True | Sensor: 1920x1080]")

    def _init_ascom(self, prog_id: str):
        try:
            pythoncom.CoInitialize()
            self._com_initialized = True
            logging.info(f"Conectando a cámara vía ASCOM (ProgID: {prog_id})...")
            self.camera_obj = win32com.client.Dispatch(prog_id)
            self.camera_obj.Connected = True
            if not bool(self.camera_obj.Connected):
                raise RuntimeError("El driver ASCOM Camera rechazó la conexión (Connected=False).")
            self.backend_type = "ASCOM"
            logging.info("¡Conexión ASCOM Camera establecida con éxito!")
        except Exception as e:
            self.close()
            logging.error(f"Fallo al conectar con ASCOM Camera: {e}")
            raise RuntimeError(f"Error crítico de conexión ASCOM Camera: {e}")

    def _ensure_video_stopped(self):
        """Detiene de forma segura el modo video del SDK para evitar colisiones en USB."""
        if self.backend_type == "ZWO_SDK" and self.camera_obj is not None:
            try:
                self.camera_obj.stop_video_capture()
            except Exception:
                pass

    def close(self):
        """Cierra de forma segura la cámara y libera recursos USB/COM."""
        if not self.mock_mode:
            try:
                if self.backend_type == "ZWO_SDK" and self.camera_obj is not None:
                    try:
                        self.camera_obj.stop_video_capture()
                    except Exception:
                        pass
                    try:
                        self.camera_obj.stop_exposure()
                    except Exception:
                        pass
                    self.camera_obj.close()
                elif self.backend_type == "ASCOM" and self.camera_obj is not None:
                    self.camera_obj.Connected = False
            except Exception:
                pass
            finally:
                self.camera_obj = None
                if self._com_initialized and ASCOM_CAMERA_AVAILABLE:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
                    self._com_initialized = False
        logging.info("Controlador de cámara cerrado limpiamente.")

    def _apply_zwo_sdk_settings(self):
        """Aplica exposición, ganancia y ROI centrado en el sensor 1920x1080 vía ZWO SDK."""
        if self.backend_type == "ZWO_SDK" and self.camera_obj is not None:
            self._ensure_video_stopped()
            self.camera_obj.disable_dark_subtract()
            self.camera_obj.set_control_value(asi.ASI_BANDWIDTHOVERLOAD, 80)
            self.camera_obj.set_control_value(asi.ASI_HIGH_SPEED_MODE, 1)
            self.camera_obj.set_control_value(asi.ASI_EXPOSURE, int(round(self.exposure_ms * 1000)))
            self.camera_obj.set_control_value(asi.ASI_GAIN, int(self.gain))
            start_x = int((self.SENSOR_WIDTH - self.roi_width) // 2)
            start_y = int((self.SENSOR_HEIGHT - self.roi_height) // 2)
            self.camera_obj.set_roi(
                start_x=start_x,
                start_y=start_y,
                width=self.roi_width,
                height=self.roi_height,
                bins=self.binning,
                image_type=asi.ASI_IMG_RAW16
            )

    def get_status(self) -> Dict[str, Any]:
        self.last_health_timestamp = time.time()
        if not self.mock_mode and self.backend_type == "ASCOM":
            if not bool(self.camera_obj.Connected):
                raise RuntimeError("Cámara ASCOM desconectada.")
        return {
            "mock": self.mock_mode,
            "hardware": not self.mock_mode,
            "backend": self.backend_type,
            "connected": True,
            "state": self.state,
            "exposure_ms": self.exposure_ms,
            "gain": self.gain,
            "binning": self.binning,
            "roi_width": self.roi_width,
            "roi_height": self.roi_height,
            "last_frame_ts": self.last_frame_timestamp,
            "last_health_ts": self.last_health_timestamp
        }

    def set_config(self, exposure_ms: float = None, gain: int = None, binning: int = None, roi: str = None) -> Dict[str, Any]:
        if exposure_ms is not None:
            val_exp = float(exposure_ms)
            if val_exp <= 0:
                raise ValueError(f"Exposición inválida: {val_exp} ms")
            self.exposure_ms = val_exp
        if gain is not None:
            self.gain = max(0, int(gain))
        if binning is not None:
            self.binning = max(1, int(binning))
        if roi is not None:
            roi_str = str(roi)
            if "1920" in roi_str or "Full" in roi_str:
                self.roi_width, self.roi_height = 1920, 1080
            elif "256" in roi_str:
                self.roi_width, self.roi_height = 256, 256
            elif "128" in roi_str:
                self.roi_width, self.roi_height = 128, 128
            else:
                self.roi_width, self.roi_height = 64, 64

        if not self.mock_mode:
            if self.backend_type == "ZWO_SDK":
                self._apply_zwo_sdk_settings()
            elif self.backend_type == "ASCOM":
                try:
                    self.camera_obj.BinX = self.binning
                    self.camera_obj.BinY = self.binning
                    self.camera_obj.NumX = self.roi_width
                    self.camera_obj.NumY = self.roi_height
                    self.camera_obj.StartX = int((self.SENSOR_WIDTH - self.roi_width) // 2)
                    self.camera_obj.StartY = int((self.SENSOR_HEIGHT - self.roi_height) // 2)
                    if hasattr(self.camera_obj, "Gain"):
                        self.camera_obj.Gain = int(self.gain)
                except Exception as e:
                    logging.warning(f"Aviso configurando ROI/Gain en ASCOM Camera: {e}")

        logging.info(
            f"Configuración de cámara aplicada -> Exp: {self.exposure_ms:.2f}ms, "
            f"Gain: {self.gain}, ROI: {self.roi_width}x{self.roi_height}"
        )
        return self.get_status()

    def get_preview_frame(self) -> Dict[str, Any]:
        """Captura un fotograma individual en memoria RAM sin escribir en disco."""
        try:
            if self.mock_mode:
                if self.roi_width >= 1920:
                    ny, nx = 270, 480
                    y, x = np.ogrid[:ny, :nx]
                    noise = np.random.normal(0, 4.0, (ny, nx))
                    img = np.zeros((ny, nx), dtype=np.float32)
                    star_list = [
                        (65, 90, 160), (135, 240, 255), (180, 80, 180),
                        (50, 390, 190), (220, 350, 170), (105, 420, 140),
                        (140, 240, 250), (200, 170, 130), (80, 290, 150)
                    ]
                    for sy, sx, sb in star_list:
                        img += np.exp(-0.5 * ((x - sx)**2 + (y - sy)**2) / 2.5) * (sb / 255.0)
                    preview_arr = np.clip(img * 220.0 + noise + 12.0, 0, 255).astype(np.uint8)
                else:
                    nx = self.roi_width
                    ny = self.roi_height
                    y, x = np.ogrid[:ny, :nx]
                    cx, cy = nx / 2.0, ny / 2.0
                    r = np.hypot(x - cx, y - cy)
                    ring = np.exp(-0.5 * ((r - 10.5) / 2.2) ** 2) * 200.0
                    noise = np.random.normal(0, 5.0, (ny, nx))
                    preview_arr = np.clip(ring + noise + 15.0, 0, 255).astype(np.uint8)

            else:
                if self.backend_type == "ZWO_SDK":
                    self._apply_zwo_sdk_settings()
                    timeout_ms = int(max(self.exposure_ms * 2 + 500, 1200))
                    self._ensure_video_stopped()
                    self.camera_obj.start_video_capture()
                    try:
                        frame = self.camera_obj.capture_video_frame(timeout=timeout_ms)
                        arr = np.asarray(frame, dtype=np.float32)
                        if arr.max() > 4095.0:
                            arr = arr / 16.0
                    finally:
                        self._ensure_video_stopped()

                elif self.backend_type == "ASCOM":
                    exp_sec = max(0.0001, self.exposure_ms / 1000.0)
                    self.camera_obj.StartExposure(exp_sec, True)
                    t0 = time.time()
                    while not bool(self.camera_obj.ImageReady):
                        if time.time() - t0 > (exp_sec + 8.0):
                            raise TimeoutError("ASCOM ImageReady timeout during preview.")
                        time.sleep(0.01)
                    arr = np.array(self.camera_obj.ImageArray, dtype=np.float32)
                    if arr.ndim == 2:
                        arr = arr.T
                else:
                    raise RuntimeError(f"Unsupported camera backend: {self.backend_type}")

                if arr.shape[1] > 640:
                    step = max(1, arr.shape[1] // 480)
                    arr = arr[::step, ::step]

                ptp = np.ptp(arr) or 1.0
                preview_arr = np.clip(((arr - arr.min()) / ptp) * 255.0, 0, 255).astype(np.uint8)

            b64_str = base64.b64encode(preview_arr.tobytes()).decode("ascii")
            self.last_health_timestamp = time.time()
            return {
                "status": "SUCCESS",
                "shape": list(preview_arr.shape),
                "data_b64": b64_str,
                "roi_width": self.roi_width,
                "roi_height": self.roi_height
            }
        except Exception as e:
            logging.error(f"Error during preview frame capture: {e}")
            return {"status": "ERROR", "message": str(e)}

    @staticmethod
    def _clean_star_hr(star_label: str) -> str:
        """Extrae el número HR limpio para el header FITS."""
        if not star_label:
            return "5944"
        m = re.search(r"HR\s*(\d+)", str(star_label), re.IGNORECASE)
        if m:
            return m.group(1)
        m2 = re.search(r"(\d{3,4})", str(star_label))
        if m2:
            return m2.group(1)
        name_up = str(star_label).upper()
        if "CENTAURI" in name_up:
            return "5459"
        if "CANOPUS" in name_up:
            return "2326"
        if "SIRIUS" in name_up:
            return "2491"
        return "5944"

    def capture(self, filename: str = None, frames: int = 100, star: str = "HR 5944") -> Dict[str, Any]:
        """Adquiere un cubo FITS 3D tanto en modo Mock como con Hardware Real."""
        self.state = "EXPOSURING"
        out_filename = filename or f"ring_cube_{int(time.time())}.fits"
        n_frames = max(50, min(int(frames or 100), 2000))
        if self.mock_mode and n_frames > 120:
            n_frames = 100

        clean_hr = self._clean_star_hr(star)
        nx = 64

        try:
            if self.mock_mode:
                time.sleep(min(self.exposure_ms / 1000.0, 0.15))

                y, x = np.ogrid[:nx, :nx]
                cx, cy = nx / 2.0, nx / 2.0
                phi = np.arctan2(y - cy, x - cx)

                m_orders = np.arange(1, 16)
                mode_amps = 0.085 * np.exp(-0.14 * m_orders)
                cos_state = np.zeros(len(m_orders))
                sin_state = np.zeros(len(m_orders))
                alpha_ar = 0.78

                dx, dy = 0.0, 0.0
                quad_a, quad_b = 0.0, 0.0
                data_cube = np.zeros((n_frames, nx, nx), dtype=np.float32)

                for i in range(n_frames):
                    dx = 0.75 * dx + np.random.normal(0, 0.22)
                    dy = 0.75 * dy + np.random.normal(0, 0.22)
                    quad_a = 0.70 * quad_a + np.random.normal(0, 0.28)
                    quad_b = 0.70 * quad_b + np.random.normal(0, 0.28)

                    cos_state = alpha_ar * cos_state + np.sqrt(1.0 - alpha_ar**2) * np.random.normal(0, mode_amps)
                    sin_state = alpha_ar * sin_state + np.sqrt(1.0 - alpha_ar**2) * np.random.normal(0, mode_amps)

                    mod_angular = np.ones((nx, nx), dtype=np.float32)
                    for idx_m, m_val in enumerate(m_orders):
                        mod_angular += cos_state[idx_m] * np.cos(m_val * phi) + sin_state[idx_m] * np.sin(m_val * phi)

                    r_distorted = (
                        np.hypot(x - cx - dx, y - cy - dy)
                        + quad_a * np.cos(2.0 * phi)
                        + quad_b * np.sin(2.0 * phi)
                    )
                    ring = np.exp(-0.5 * ((r_distorted - 10.5) / 2.2) ** 2) * 1800.0 * np.clip(mod_angular, 0.25, 2.5)
                    noise = np.random.normal(0, 3.5, (nx, nx))
                    data_cube[i] = np.clip(ring + noise + 100.0, 0, 4095)

            else:
                if self.backend_type == "ZWO_SDK":
                    self._ensure_video_stopped()
                    self._apply_zwo_sdk_settings()
                    timeout_ms = int(max(self.exposure_ms * 2 + 500, 1000))
                    frames_list = []

                    self.camera_obj.start_video_capture()
                    try:
                        for _ in range(n_frames):
                            frame = self.camera_obj.capture_video_frame(timeout=timeout_ms)
                            arr = np.asarray(frame, dtype=np.float32)
                            if arr.max() > 4095.0:
                                arr = arr / 16.0
                            frames_list.append(arr)
                    finally:
                        self._ensure_video_stopped()

                    data_cube = np.array(frames_list, dtype=np.float32)

                elif self.backend_type == "ASCOM":
                    exp_sec = max(0.0001, self.exposure_ms / 1000.0)
                    n_ascom = min(n_frames, 50)
                    frames_list = []
                    for _ in range(n_ascom):
                        self.camera_obj.StartExposure(exp_sec, True)
                        t0 = time.time()
                        while not bool(self.camera_obj.ImageReady):
                            if time.time() - t0 > (exp_sec + 10.0):
                                raise TimeoutError("Timeout esperando ImageReady en ASCOM Camera.")
                            time.sleep(0.005)
                        img_arr = np.array(self.camera_obj.ImageArray, dtype=np.float32)
                        if img_arr.ndim == 2:
                            frames_list.append(img_arr.T)
                    data_cube = np.array(frames_list, dtype=np.float32)
                else:
                    raise RuntimeError(f"Backend de cámara no soportado: {self.backend_type}")

            hdr = fits.Header()
            hdr["NAXIS"] = 3
            hdr["NAXIS1"] = int(data_cube.shape[2])
            hdr["NAXIS2"] = int(data_cube.shape[1])
            hdr["NAXIS3"] = int(data_cube.shape[0])
            hdr["EXPOSURE"] = int(round(self.exposure_ms * 1000))  # Microsegundos
            hdr["GAIN"] = float(self.gain)
            hdr["DATE-OBS"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            hdr["STAR"] = clean_hr

            fits.writeto(out_filename, data_cube, hdr, overwrite=True)
            self.last_frame_timestamp = time.time()
            self.state = "IDLE"

            logging.info(f"Cubo FITS guardado exitosamente: {out_filename} ({data_cube.shape[0]} frames, STAR=HR {clean_hr})")
            return {
                "status": "SUCCESS",
                "message": "FITS cube captured",
                "filename": out_filename,
                "frames": int(data_cube.shape[0]),
                "star": clean_hr,
                "timestamp": self.last_frame_timestamp
            }

        except Exception as e:
            self.state = "ERROR"
            logging.error(f"Error durante captura de cámara: {e}")
            raise RuntimeError(f"Fallo en captura FITS: {e}")


def run_camera_server(port: int = 5557, mock_mode: bool = True, camera_id: int = 0, ascom_prog_id: str = "ASCOM.ASICamera2.Camera"):
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    driver = None
    try:
        try:
            socket.bind(f"tcp://*:{port}")
        except zmq.error.ZMQError as ze:
            logging.info(f"Puerto {port} ya activo en esta sesión ({ze}).")
            return

        driver = CameraHardwareDriver(mock_mode=mock_mode, camera_id=camera_id, ascom_prog_id=ascom_prog_id)
        logging.info(f"Servidor Camera_TCP escuchando en tcp://*:{port} [Mock Mode: {mock_mode}]...")

        while True:
            try:
                message = socket.recv_json()
                action = message.get("action", "").upper()

                if action == "GET_STATUS":
                    status = driver.get_status()
                    response = {"status": "SUCCESS", **status}

                elif action == "SET_CONFIG":
                    exp = message.get("exposure_ms")
                    gain = message.get("gain")
                    binning = message.get("binning")
                    roi = message.get("roi")
                    res = driver.set_config(exposure_ms=exp, gain=gain, binning=binning, roi=roi)
                    response = {"status": "SUCCESS", **res}

                elif action in ("GET_PREVIEW", "PREVIEW"):
                    response = driver.get_preview_frame()

                elif action == "CAPTURE":
                    fname = message.get("filename")
                    frames = message.get("frames", 100)
                    star = message.get("star", "HR 5944")
                    response = driver.capture(filename=fname, frames=frames, star=star)

                elif action == "SHUTDOWN":
                    socket.send_json({"status": "SUCCESS", "message": "Camera server shutting down cleanly"})
                    break

                else:
                    response = {"status": "ERROR", "message": f"Acción desconocida: {action}"}

                socket.send_json(response)

            except ValueError as ve:
                logging.error(f"Error de validación en Camera_TCP: {ve}")
                socket.send_json({"status": "ERROR", "message": str(ve)})
            except Exception as e:
                logging.exception("Error interno procesando comando de cámara.")
                socket.send_json({"status": "ERROR", "message": str(e)})

    except KeyboardInterrupt:
        logging.info("Apagando servidor Camera_TCP...")
    finally:
        if driver is not None:
            driver.close()
        socket.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Camera TCP Microservice (ZWO ASI662MM)")
    parser.add_argument("--real", action="store_true", help="Conectar a hardware real (ZWO SDK / ASCOM)")
    parser.add_argument("--cid", type=int, default=0, help="ID de cámara ZWO")
    parser.add_argument("--progid", type=str, default="ASCOM.ASICamera2.Camera", help="ProgID de ASCOM Camera")
    args = parser.parse_args()

    run_camera_server(port=5557, mock_mode=not args.real, camera_id=args.cid, ascom_prog_id=args.progid)