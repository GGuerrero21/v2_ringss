import sys
import os
import time
import math
import json
import base64
import threading
import logging
from pathlib import Path
import zmq
import numpy as np

# Forzar explícitamente PySide6 para evitar colisiones de DLLs con PyQt5 en Windows
os.environ["QT_API"] = "pyside6"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def _locate_repo_dirs():
    candidates = [
        PROJECT_ROOT / "ringss_restore_profile",
        PROJECT_ROOT / "extracted_repo" / "ringss_restore_profile-fab3e675ca73943cb7ee4375ebb41bd1e01f4c01" / "ringss_restore_profile",
        PROJECT_ROOT / "repo_extracted" / "ringss_restore_profile-fab3e675ca73943cb7ee4375ebb41bd1e01f4c01" / "ringss_restore_profile",
    ]
    for base in candidates:
        if (base / "data" / "par-tololo.json").exists():
            return base
    return candidates[0]

REPO_BASE = _locate_repo_dirs()
DATA_DIR = REPO_BASE / "data"
LEGACY_DIR = REPO_BASE / "legacy"

for p in [str(PROJECT_ROOT), str(REPO_BASE.parent), str(LEGACY_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from astropy.io import fits
    ASTROPY_AVAILABLE = True
except ImportError:
    ASTROPY_AVAILABLE = False

try:
    import cube2
    CUBE2_AVAILABLE = True
except ImportError:
    CUBE2_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use("QtAgg")
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    MATPLOTLIB_AVAILABLE = True
except Exception as e:
    try:
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure
        MATPLOTLIB_AVAILABLE = True
    except Exception as e2:
        logging.warning(f"No se pudo inicializar Matplotlib Canvas: {e2}")
        MATPLOTLIB_AVAILABLE = False

try:
    from PySide6.QtCore import Qt, QTimer, QObject, Signal
    from PySide6.QtGui import QColor, QFont, QPainter, QPen, QImage, QPixmap
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QLabel, QPushButton,
        QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QLineEdit,
        QTextEdit, QFrame, QSplitter, QGroupBox, QFileDialog
    )
    PYSIDE_AVAILABLE = True
except ImportError:
    PYSIDE_AVAILABLE = False


class ZeroMQClientWorker(QObject):
    """
    Worker asíncrono para peticiones ZeroMQ usando hilos daemon y señales Qt.
    Captura excepciones en slots y libera memoria con deleteLater().
    """
    response_received = Signal(str, dict)

    def __init__(self, port: int, action: str, payload: dict = None, timeout_ms: int = 5000, parent=None):
        super().__init__(parent)
        self.port = port
        self.action = action
        self.payload = payload or {}
        self.timeout_ms = timeout_ms
        self._is_running = False

    def isRunning(self) -> bool:
        return self._is_running

    def start(self):
        self._is_running = True
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        sock = None
        try:
            ctx = zmq.Context.instance()
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            sock.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(f"tcp://localhost:{self.port}")

            req = {"action": self.action, **self.payload}
            sock.send_json(req)
            resp = sock.recv_json()
            sock.close()
            sock = None
            
            data = resp if isinstance(resp, dict) else {"status": "ERROR", "message": "Invalid JSON response"}
            try:
                self.response_received.emit(self.action, data)
            except Exception as slot_err:
                logging.exception(f"Error procesando respuesta del slot para '{self.action}': {slot_err}")

        except Exception as e:
            if sock:
                try: 
                    sock.close()
                except Exception: 
                    pass
                sock = None
            try:
                self.response_received.emit(self.action, {"status": "ERROR", "message": str(e)})
            except Exception as slot_err:
                logging.exception(f"Error emitiendo señal de error para '{self.action}': {slot_err}")
        finally:
            self._is_running = False
            # Programar eliminación del QObject para evitar fugas de memoria
            self.deleteLater()


class RingssMainWindow(QMainWindow):
    COMMON_STAR_NAMES = {
        "472": "Achernar", "1457": "Aldebaran", "1713": "Rigel", "1903": "Alnilam",
        "2061": "Betelgeuse", "2326": "Canopus", "2491": "Sirius", "2943": "Procyon",
        "3982": "Regulus", "4730": "Acrux", "4853": "Mimosa", "5056": "Spica",
        "5267": "Hadar", "5340": "Arcturus", "5459": "Alpha Centauri", "5944": "Pi Scorpii",
        "6134": "Antares", "6527": "Shaula", "6879": "Kaus Australis", "7001": "Vega",
        "7557": "Altair", "7790": "Peacock", "8425": "Alnair", "8728": "Fomalhaut"
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RINGSS MISSION CONTROL - Observatory Suite v2.4")
        self.resize(1540, 930)
        self.setMinimumSize(1360, 820)

        # Telemetría operativa y guardas de estado
        self.history_time = []
        self.history_seeing = []
        self.history_r0 = []
        self.history_wind = []
        self.session_start = time.time()
        self.is_monitoring = False
        self.is_autofocusing = False
        self.is_autocentering = False
        self.is_live_view = False
        self._preview_busy = False
        self._polling_active = False
        self._switching_mode_active = False
        self.current_mode = "MOCK"
        self.target_data_map = {}

        self.site_lat = -30.1684
        self.site_lon = -70.8065
        self.site_name = "CTIO / La Serena"
        self.pixel_scale = 1.78
        self.starcat = {}
        self.par_dict = {}
        self._load_observatory_catalog()

        # Paleta oscura para entorno de observatorio
        self.setStyleSheet("""
            QMainWindow { background-color: #0b0f19; color: #e2e8f0; }
            QWidget { background-color: #0b0f19; color: #e2e8f0; font-family: 'Segoe UI', Arial, sans-serif; font-size: 10pt; }
            
            QGroupBox { 
                border: 1px solid #1e293b; 
                border-radius: 8px; 
                margin-top: 14px; 
                font-weight: bold; 
                color: #38bdf8; 
                background-color: #111827; 
            }
            QGroupBox::title { 
                subcontrol-origin: margin; 
                left: 14px; 
                padding: 0 6px; 
                background-color: #111827; 
            }
            
            QPushButton { 
                background-color: #1e293b; 
                border: 1px solid #334155; 
                border-radius: 5px; 
                color: #f1f5f9; 
                padding: 6px 12px; 
                font-weight: 600; 
            }
            QPushButton:hover { 
                background-color: #2563eb; 
                border-color: #60a5fa; 
                color: #ffffff; 
            }
            QPushButton:pressed { 
                background-color: #1d4ed8; 
            }
            
            QPushButton#emergency_btn { 
                background-color: #b91c1c; 
                border: 1px solid #ef4444; 
                color: #ffffff; 
                font-size: 10.5pt; 
                font-weight: bold; 
                border-radius: 6px; 
            }
            QPushButton#emergency_btn:hover { 
                background-color: #991b1b; 
                border-color: #f87171; 
            }

            QPushButton#dpad_stop { 
                background-color: #334155; 
                border: 1px solid #475569; 
                color: #f87171; 
                font-weight: bold; 
                font-size: 8.5pt; 
            }
            QPushButton#dpad_stop:hover { 
                background-color: #dc2626; 
                color: #ffffff; 
                border-color: #ef4444; 
            }
            
            QComboBox, QLineEdit { 
                background-color: #030712; 
                border: 1px solid #334155; 
                border-radius: 5px; 
                padding: 5px 8px; 
                color: #f8fafc; 
            }
            QComboBox:focus, QLineEdit:focus { 
                border-color: #38bdf8; 
            }
            
            QTextEdit { 
                background-color: #030712; 
                border: 1px solid #1e293b; 
                border-radius: 6px; 
                color: #38bdf8; 
                font-family: 'Cascadia Code', Consolas, monospace; 
                font-size: 9pt; 
            }
            
            QLabel { color: #94a3b8; }
        """)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(12, 10, 12, 12)
        self.main_layout.setSpacing(10)

        self.build_header_bar()

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.addWidget(self.build_mount_panel())
        top_splitter.addWidget(self.build_detector_panel())
        top_splitter.addWidget(self.build_eaf_panel())
        top_splitter.setSizes([390, 670, 390])
        self.main_layout.addWidget(top_splitter, stretch=3)

        bottom_splitter = QSplitter(Qt.Horizontal)
        bottom_splitter.addWidget(self.build_metrics_panel())
        bottom_splitter.addWidget(self.build_charts_panel())
        bottom_splitter.addWidget(self.build_logs_panel())
        bottom_splitter.setSizes([270, 750, 440])
        self.main_layout.addWidget(bottom_splitter, stretch=2)

        # Temporizadores de sondeo y monitoreo
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.on_monitor_tick)

        self.status_poll_timer = QTimer(self)
        self.status_poll_timer.timeout.connect(self.poll_mount_status)
        self.status_poll_timer.start(3000)

        # Temporizador para Live View en RAM (1500 ms)
        self.live_preview_timer = QTimer(self)
        self.live_preview_timer.timeout.connect(self.request_live_preview)

        self.refresh_zenith_star_catalog()
        self.refresh_detector_view(None)
        self.log_message(f"[SYSTEM] RINGSS Mission Control v2.4 initialized ({self.site_name}).")

        QTimer.singleShot(1000, self.scan_all_hardware)

    def _load_observatory_catalog(self):
        candidates = [
            PROJECT_ROOT / "ringss_restore_profile" / "data",
            PROJECT_ROOT / "extracted_repo" / "ringss_restore_profile-fab3e675ca73943cb7ee4375ebb41bd1e01f4c01" / "ringss_restore_profile" / "data",
            PROJECT_ROOT / "repo_extracted" / "ringss_restore_profile-fab3e675ca73943cb7ee4375ebb41bd1e01f4c01" / "ringss_restore_profile" / "data",
        ]
        for data_dir in candidates:
            par_f = data_dir / "par-tololo.json"
            cat_f = data_dir / "starcat.json"
            if par_f.exists() and cat_f.exists():
                try:
                    with open(par_f, "r", encoding="utf-8") as f:
                        self.par_dict = json.load(f)
                    self.site_lat = float(self.par_dict.get("site", {}).get("lat", -30.1684))
                    self.site_lon = float(self.par_dict.get("site", {}).get("lon", -70.8065))
                    self.site_name = str(self.par_dict.get("site", {}).get("name", "CTIO"))
                    self.pixel_scale = float(self.par_dict.get("telescope", {}).get("pixel", 1.78))
                    with open(cat_f, "r", encoding="utf-8") as f:
                        self.starcat = json.load(f)
                    break
                except Exception:
                    pass

    @staticmethod
    def compute_alt_az(ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, unix_ts: float = None):
        if unix_ts is None:
            unix_ts = time.time()
        jd = unix_ts / 86400.0 + 2440587.5
        d = jd - 2451545.0
        gmst_deg = (280.46061837 + 360.98564736629 * d) % 360.0
        lst_deg = (gmst_deg + lon_deg) % 360.0
        ha_rad = math.radians((lst_deg - ra_deg) % 360.0)
        dec_rad = math.radians(dec_deg)
        lat_rad = math.radians(lat_deg)

        sin_alt = math.sin(dec_rad) * math.sin(lat_rad) + math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad)
        sin_alt = max(-1.0, min(1.0, sin_alt))
        alt_rad = math.asin(sin_alt)

        cos_az = (math.sin(dec_rad) - math.sin(alt_rad) * math.sin(lat_rad)) / (math.cos(alt_rad) * math.cos(lat_rad) + 1e-12)
        cos_az = max(-1.0, min(1.0, cos_az))
        az_deg = math.degrees(math.acos(cos_az))
        if math.sin(ha_rad) > 0:
            az_deg = 360.0 - az_deg

        return math.degrees(alt_rad), az_deg, lst_deg / 15.0

    def refresh_zenith_star_catalog(self):
        if not hasattr(self, "combo_target"):
            return

        self.target_data_map = {}
        candidates = []

        if self.starcat:
            now_ts = time.time()
            for hr, vals in self.starcat.items():
                try:
                    ra_deg = float(vals[0])
                    dec_deg = float(vals[1])
                    vmag = float(vals[2])
                    bv = float(vals[3])
                    alt, az, _ = self.compute_alt_az(ra_deg, dec_deg, self.site_lat, self.site_lon, now_ts)

                    if alt >= 48.0 and vmag <= 3.1:
                        zen_deg = 90.0 - alt
                        cos_z = math.cos(math.radians(zen_deg))

                        flux_weight = math.pow(10.0, -0.22 * vmag)
                        airmass_weight = math.pow(cos_z, 1.8)
                        zenith_hole_penalty = 0.75 if alt > 86.5 else 1.0

                        quality_score = flux_weight * airmass_weight * zenith_hole_penalty * 100.0

                        candidates.append({
                            "hr": str(hr),
                            "name": self.COMMON_STAR_NAMES.get(str(hr), ""),
                            "ra_hours": ra_deg / 15.0,
                            "ra_deg": ra_deg,
                            "dec_deg": dec_deg,
                            "alt": alt,
                            "az": az,
                            "zen": zen_deg,
                            "vmag": vmag,
                            "bv": bv,
                            "score": quality_score
                        })
                except Exception:
                    continue

        candidates.sort(key=lambda s: s["score"], reverse=True)

        self.combo_target.clear()
        if candidates:
            for idx, s in enumerate(candidates[:15]):
                star_title = f"HR {s['hr']} {s['name']}".strip()
                prefix = "[OPT] " if idx == 0 else f"#{idx + 1} "
                label = f"{prefix}{star_title} (Alt={s['alt']:.1f} deg, V={s['vmag']:.2f}, B-V={s['bv']:+.2f})"
                self.combo_target.addItem(label)
                self.target_data_map[label] = s

            self.combo_target.setCurrentIndex(0)
            best = candidates[0]
            self.log_message(
                f"[CATALOG] Optimal target selected: HR {best['hr']} {best['name']} "
                f"(Alt={best['alt']:.1f} deg, V={best['vmag']:.2f})."
            )
        else:
            fallback = [
                ("5944", "Pi Scorpii", 15.9808, -26.1142, 82.6, 180.0, 2.89, -0.19, 95.0),
                ("5459", "Alpha Centauri", 14.6600, -60.8353, 75.2, 165.0, -0.01, 0.71, 92.0),
                ("2326", "Canopus", 6.3992, -52.6958, 68.4, 195.0, -0.72, 0.15, 88.0),
                ("2491", "Sirius", 6.7525, -16.7161, 61.5, 340.0, -1.46, 0.00, 85.0)
            ]
            for idx, (hr, name, ra_h, dec_d, alt, az, v, bv, sc) in enumerate(fallback):
                prefix = "[OPT] " if idx == 0 else f"#{idx + 1} "
                label = f"{prefix}HR {hr} {name} (Alt={alt:.1f} deg, V={v:.2f}, B-V={bv:+.2f})"
                self.combo_target.addItem(label)
                self.target_data_map[label] = {
                    "hr": hr, "name": name, "ra_hours": ra_h, "ra_deg": ra_h * 15.0,
                    "dec_deg": dec_d, "alt": alt, "az": az, "zen": 90.0 - alt, "vmag": v, "bv": bv, "score": sc
                }

    def action_select_best_star(self):
        self.refresh_zenith_star_catalog()
        if self.combo_target.count() > 0:
            self.combo_target.setCurrentIndex(0)
            best_label = self.combo_target.currentText()
            self.log_message(f"[TARGET] Automatically locked optimal star: {best_label}")

    def build_header_bar(self):
        header = QFrame()
        header.setStyleSheet("background-color: #111827; border: 1px solid #1e293b; border-radius: 8px; padding: 6px;")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 4, 12, 4)

        title = QLabel("<b>RINGSS</b> <span style='color: #64748b;'>| SEEING MONITOR & MISSION CONTROL</span>")
        title.setStyleSheet("font-size: 12pt; color: #38bdf8;")
        layout.addWidget(title)

        layout.addStretch()

        layout.addWidget(QLabel("MODE:"))
        self.mode_selector = QComboBox()
        self.mode_selector.addItems(["SIMULATION (Mock)", "HARDWARE (ASCOM/SDK)"])
        self.mode_selector.currentIndexChanged.connect(self.on_mode_changed)
        layout.addWidget(self.mode_selector)

        badge_base = "font-weight: bold; padding: 4px 10px; border-radius: 4px; background-color: #030712; border: 1px solid #334155; font-size: 8.5pt;"
        self.mount_badge = QLabel("MOUNT: INIT")
        self.foc_badge = QLabel("FOCUSER: INIT")
        self.cam_badge = QLabel("CAM: INIT")
        self.orch_badge = QLabel("ORCH: INIT")
        for b in [self.mount_badge, self.foc_badge, self.cam_badge, self.orch_badge]:
            b.setStyleSheet(badge_base)
            layout.addWidget(b)

        layout.addStretch()

        btn_export = QPushButton("Export CSV")
        btn_export.clicked.connect(self.export_csv_logs)
        btn_scan = QPushButton("Scan Cluster")
        btn_scan.clicked.connect(self.scan_all_hardware)
        layout.addWidget(btn_export)
        layout.addWidget(btn_scan)

        self.main_layout.addWidget(header)

    def build_mount_panel(self):
        group = QGroupBox("TELESCOPE & MOUNT CONTROL")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        hdr_picker = QHBoxLayout()
        hdr_picker.addWidget(QLabel("ZENITH TARGETS"))

        btn_best_star = QPushButton("Best Target")
        btn_best_star.setToolTip("Select star with highest signal-to-noise ratio and elevation")
        btn_best_star.setStyleSheet("background-color: #0284c7; color: white; padding: 4px 8px; font-size: 8.5pt;")
        btn_best_star.clicked.connect(self.action_select_best_star)
        hdr_picker.addWidget(btn_best_star)

        btn_refresh_cat = QPushButton("Refresh")
        btn_refresh_cat.setToolTip("Recalculate ephemerides for current Local Sidereal Time")
        btn_refresh_cat.setStyleSheet("padding: 4px 8px; font-size: 8.5pt;")
        btn_refresh_cat.clicked.connect(self.refresh_zenith_star_catalog)
        hdr_picker.addWidget(btn_refresh_cat)
        layout.addLayout(hdr_picker)

        self.combo_target = QComboBox()
        layout.addWidget(self.combo_target)

        btn_slew = QPushButton("Slew to Selected Target")
        btn_slew.clicked.connect(self.action_slew_target)
        layout.addWidget(btn_slew)

        tel_box = QFrame()
        tel_box.setStyleSheet("background-color: #030712; border: 1px solid #1e293b; border-radius: 6px; padding: 8px;")
        grid = QGridLayout(tel_box)
        grid.addWidget(QLabel("<b style='color: #38bdf8;'>CURRENT POINTING TELEMETRY</b>"), 0, 0, 1, 2)

        self.lbl_ra = QLabel("R.A.: 15.98h")
        self.lbl_dec = QLabel("Dec: -26.11 deg")
        self.lbl_alt = QLabel("Altitude: +82.6 deg")
        self.lbl_az = QLabel("Azimuth: 180.0 deg")

        for i, lbl in enumerate([self.lbl_ra, self.lbl_dec, self.lbl_alt, self.lbl_az]):
            lbl.setStyleSheet("color: #38bdf8; font-weight: bold;")
            grid.addWidget(lbl, (i // 2) + 1, i % 2)
        layout.addWidget(tel_box)

        btn_autocenter = QPushButton("Auto-Center Target (Closed-Loop)")
        btn_autocenter.clicked.connect(self.action_autocenter_target)
        layout.addWidget(btn_autocenter)

        h_box = QHBoxLayout()
        btn_home = QPushButton("Go Home")
        btn_home.clicked.connect(lambda: self.send_mount_cmd("HOME"))
        btn_park = QPushButton("Park Mount")
        btn_park.clicked.connect(lambda: self.send_mount_cmd("PARK"))
        h_box.addWidget(btn_home)
        h_box.addWidget(btn_park)
        layout.addLayout(h_box)

        track_box = QHBoxLayout()
        self.btn_tracking_toggle = QPushButton("Tracking: OFF")
        self.btn_tracking_toggle.clicked.connect(self.action_toggle_tracking)

        track_box.addWidget(self.btn_tracking_toggle)
        track_box.addWidget(QLabel("Nudge Rate:"))
        self.combo_nudge_rate = QComboBox()
        self.combo_nudge_rate.addItems(["1X", "2X", "8X"])
        self.combo_nudge_rate.setToolTip("Multiplier rate for fine nudge centering")
        track_box.addWidget(self.combo_nudge_rate)
        layout.addLayout(track_box)

        self.btn_emergency = QPushButton("EMERGENCY STOP")
        self.btn_emergency.setObjectName("emergency_btn")
        self.btn_emergency.setMinimumHeight(38)
        self.btn_emergency.clicked.connect(self.trigger_emergency_stop)
        layout.addWidget(self.btn_emergency)

        layout.addWidget(QLabel("Manual Nudge Centering (±10 arcsec * Rate)", alignment=Qt.AlignCenter))
        nudge_grid = QGridLayout()
        btn_n = QPushButton("▲ N")
        btn_s = QPushButton("▼ S")
        btn_e = QPushButton("E ►")
        btn_w = QPushButton("◄ W")
        btn_c = QPushButton("STOP")
        btn_c.setObjectName("dpad_stop")
        btn_c.setToolTip("Immediate manual stop for telescope axes")
        btn_c.clicked.connect(lambda: self.send_mount_cmd("STOP"))

        for b in [btn_n, btn_s, btn_e, btn_w, btn_c]:
            b.setMinimumHeight(28)
        btn_n.clicked.connect(lambda: self.action_nudge_mount("N", 10.0))
        btn_s.clicked.connect(lambda: self.action_nudge_mount("S", 10.0))
        btn_e.clicked.connect(lambda: self.action_nudge_mount("E", 10.0))
        btn_w.clicked.connect(lambda: self.action_nudge_mount("W", 10.0))

        nudge_grid.addWidget(btn_n, 0, 1)
        nudge_grid.addWidget(btn_w, 1, 0)
        nudge_grid.addWidget(btn_c, 1, 1)
        nudge_grid.addWidget(btn_e, 1, 2)
        nudge_grid.addWidget(btn_s, 2, 1)
        layout.addLayout(nudge_grid)

        return group

    def build_detector_panel(self):
        self.detector_group = QGroupBox("Focal Plane - Detector View (ROI 64x64) | Ring Target: 10.5 px")
        layout = QVBoxLayout(self.detector_group)
        layout.setAlignment(Qt.AlignCenter)

        self.canvas_detector = QLabel()
        self.canvas_detector.setMinimumSize(460, 460)
        self.canvas_detector.setStyleSheet("background-color: #030712; border: 1px solid #1e293b; border-radius: 6px;")
        self.canvas_detector.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.canvas_detector)

        return self.detector_group

    def build_eaf_panel(self):
        group = QGroupBox("DETECTOR & EAF FOCUSER CONTROL")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        cfg_grid = QGridLayout()
        cfg_grid.addWidget(QLabel("Exp. (µs):"), 0, 0)
        self.edit_exp = QLineEdit("1000")
        cfg_grid.addWidget(self.edit_exp, 0, 1)

        cfg_grid.addWidget(QLabel("Gain (dB):"), 1, 0)
        self.edit_gain = QLineEdit("100")
        cfg_grid.addWidget(self.edit_gain, 1, 1)

        cfg_grid.addWidget(QLabel("ROI:"), 2, 0)
        self.combo_roi = QComboBox()
        self.combo_roi.addItems(["64x64 (RINGSS)", "128x128", "256x256", "Full Frame (1920x1080)"])
        self.combo_roi.currentIndexChanged.connect(self.on_roi_changed)
        cfg_grid.addWidget(self.combo_roi, 2, 1)
        layout.addLayout(cfg_grid)

        cam_btn_box = QHBoxLayout()
        btn_apply_cam = QPushButton("Apply Settings")
        btn_apply_cam.clicked.connect(self.action_apply_cam_settings)
        self.btn_live_view = QPushButton("Live View: OFF")
        self.btn_live_view.clicked.connect(self.toggle_live_view)
        cam_btn_box.addWidget(btn_apply_cam)
        cam_btn_box.addWidget(self.btn_live_view)
        layout.addLayout(cam_btn_box)

        eaf_box = QHBoxLayout()
        eaf_box.addWidget(QLabel("EAF Step Position:"))
        self.lbl_eaf_step = QLabel("24420")
        self.lbl_eaf_step.setStyleSheet("font-weight: bold; color: #38bdf8; font-size: 11pt;")
        eaf_box.addWidget(self.lbl_eaf_step)
        layout.addLayout(eaf_box)

        steps_grid = QGridLayout()
        steps = [-50, -10, -5, -1, +1, +5, +10, +50]
        for i, s in enumerate(steps):
            btn = QPushButton(f"{s:+d}")
            btn.clicked.connect(lambda checked=False, val=s: self.action_step_eaf(val))
            steps_grid.addWidget(btn, i // 4, i % 4)
        layout.addLayout(steps_grid)

        self.btn_autofocus = QPushButton("Run Ring Auto-Focus (EAF)")
        self.btn_autofocus.clicked.connect(self.action_run_autofocus)
        layout.addWidget(self.btn_autofocus)

        cube_box = QHBoxLayout()
        cube_box.addWidget(QLabel("Frames/Cube:"))
        self.edit_frames = QLineEdit("100")
        cube_box.addWidget(self.edit_frames)
        layout.addLayout(cube_box)

        btn_measure = QPushButton("Measure Seeing (Science Run)")
        btn_measure.clicked.connect(self.action_measure_seeing)
        btn_measure.setStyleSheet("background-color: #2563eb; color: white;")
        layout.addWidget(btn_measure)

        self.btn_continuous = QPushButton("Start Continuous Monitoring")
        self.btn_continuous.clicked.connect(self.toggle_continuous_monitoring)
        self.btn_continuous.setStyleSheet("background-color: #16a34a; color: white;")
        layout.addWidget(self.btn_continuous)

        btn_snapshot = QPushButton("Capture FITS Snapshot")
        btn_snapshot.clicked.connect(self.action_take_snapshot)
        layout.addWidget(btn_snapshot)

        return group

    def build_metrics_panel(self):
        group = QGroupBox("LATEST MEASUREMENTS")
        layout = QVBoxLayout(group)
        self.lbl_seeing = QLabel("Seeing: <b>--</b> arcsec")
        self.lbl_fsee = QLabel("Free Atmosphere: <b>--</b> arcsec")
        self.lbl_fried = QLabel("Fried (r0): <b>--</b> cm")
        self.lbl_zenith = QLabel("Zenith Dist: <b>--</b> deg")
        self.lbl_wind = QLabel("Wind Speed: <b>--</b> m/s")
        self.lbl_tau0 = QLabel("Coherence tau0: <b>--</b> ms")
        self.lbl_theta0 = QLabel("Isoplanatic theta0: <b>--</b> arcsec")

        for lbl in [self.lbl_seeing, self.lbl_fsee, self.lbl_fried, self.lbl_zenith, self.lbl_wind, self.lbl_tau0, self.lbl_theta0]:
            lbl.setStyleSheet("font-size: 9.5pt; padding: 2px; color: #f8fafc;")
            layout.addWidget(lbl)

        layout.addStretch()
        return group

    def build_charts_panel(self):
        group = QGroupBox("SEEING HISTORICAL TREND & TURBULENCE PROFILE (Cn2)")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(6, 6, 6, 6)

        if MATPLOTLIB_AVAILABLE:
            self.fig_trend = Figure(figsize=(4.2, 2.6), facecolor='#111827')
            self.ax_trend = self.fig_trend.add_subplot(111)
            self.canvas_trend = FigureCanvas(self.fig_trend)
            self.init_trend_chart()

            self.fig_prof = Figure(figsize=(3.2, 2.6), facecolor='#111827')
            self.ax_prof = self.fig_prof.add_subplot(111)
            self.canvas_prof = FigureCanvas(self.fig_prof)
            self.init_profile_chart()

            layout.addWidget(self.canvas_trend, stretch=3)
            layout.addWidget(self.canvas_prof, stretch=2)
        else:
            lbl = QLabel("Charts require matplotlib")
            lbl.setAlignment(Qt.AlignCenter)
            layout.addWidget(lbl)

        return group

    def init_trend_chart(self):
        self.ax_trend.set_facecolor('#030712')
        self.ax_trend.tick_params(colors='#94a3b8', labelsize=8)
        self.ax_trend.set_title("Seeing Trend (arcsec)", color='#38bdf8', fontsize=9, fontweight='bold')
        self.ax_trend.set_ylabel("Seeing (arcsec)", color='#94a3b8', fontsize=8)
        self.ax_trend.set_xlabel("Time (s)", color='#94a3b8', fontsize=8)
        for spine in self.ax_trend.spines.values():
            spine.set_edgecolor('#1e293b')
        self.ax_trend.grid(True, linestyle='--', alpha=0.25, color='#334155')
        self.trend_line, = self.ax_trend.plot([], [], 'o-', color='#38bdf8', linewidth=1.5, markersize=3)
        self.fig_trend.tight_layout()

    def init_profile_chart(self, heights=None, cn2_values=None):
        self.ax_prof.set_facecolor('#030712')
        self.ax_prof.tick_params(colors='#94a3b8', labelsize=8)
        self.ax_prof.set_title("Cn2 Turbulence Profile (J)", color='#38bdf8', fontsize=9, fontweight='bold')
        self.ax_prof.set_xlabel("J (1e-13 m^1/3)", color='#94a3b8', fontsize=8)
        self.ax_prof.set_ylabel("Altitude (km)", color='#94a3b8', fontsize=8)
        for spine in self.ax_prof.spines.values():
            spine.set_edgecolor('#1e293b')
        self.ax_prof.grid(True, linestyle='--', alpha=0.25, color='#334155')

        h = heights or [0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
        v = cn2_values or [2.1, 0.9, 0.6, 0.4, 0.3, 0.35, 0.18, 0.05]
        y_pos = np.arange(len(h))
        self.ax_prof.barh(y_pos, v, color='#2563eb', alpha=0.85, height=0.65)
        self.ax_prof.set_yticks(y_pos)
        self.ax_prof.set_yticklabels([f"{val:g}" for val in h])
        self.fig_prof.tight_layout()

    def build_logs_panel(self):
        group = QGroupBox("OBSERVATORY ACTIVITY LOG")
        layout = QVBoxLayout(group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        return group

    def log_message(self, msg: str):
        t_str = time.strftime("[%H:%M:%S]")
        self.log_view.append(f"<span style='color: #64748b;'>{t_str}</span> {msg}")

    def on_roi_changed(self, idx):
        roi_text = self.combo_roi.currentText()
        self.log_message(f"[CAMERA] Field of View / ROI selected: {roi_text}")
        self.refresh_detector_view(None)
        worker = ZeroMQClientWorker(5557, "SET_CONFIG", {"roi": roi_text}, timeout_ms=2000, parent=self)
        worker.start()

    def toggle_live_view(self, force_off=False):
        """Conmuta la previsualización periódica en RAM del detector."""
        if force_off or self.is_live_view:
            self.is_live_view = False
            self.live_preview_timer.stop()
            self.btn_live_view.setText("Live View: OFF")
            self.btn_live_view.setStyleSheet("background-color: #1e293b; color: #f1f5f9;")
            self.log_message("[CAMERA] Live View stopped.")
        else:
            self.is_live_view = True
            self.btn_live_view.setText("Live View: ON")
            self.btn_live_view.setStyleSheet("background-color: #16a34a; color: white;")
            self.log_message("[CAMERA] Live View active (1.5s refresh, RAM-only, zero disk wear).")
            self.live_preview_timer.start(1500)
            self.request_live_preview()

    def request_live_preview(self):
        """Solicita un fotograma individual volátil en RAM a Camera_TCP."""
        if not self.is_live_view or getattr(self, "_preview_busy", False):
            return
        self._preview_busy = True
        worker = ZeroMQClientWorker(5557, "GET_PREVIEW", timeout_ms=3000, parent=self)

        def on_preview_received(act, r):
            self._preview_busy = False
            if r.get("status") == "SUCCESS" and "data_b64" in r:
                try:
                    shape = tuple(r.get("shape", [270, 480]))
                    raw_bytes = base64.b64decode(r["data_b64"])
                    frame_arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(shape)
                    self.refresh_detector_view(frame_arr)
                except Exception as ex:
                    logging.warning(f"Error decoding preview frame: {ex}")

        worker.response_received.connect(on_preview_received)
        worker.start()

    def refresh_detector_view(self, image_data: np.ndarray = None):
        """
        Renderiza la imagen óptica en el visor central.
        Usa buffers .copy() para aislar la memoria de Qt de la recolección de Python.
        """
        size = 460
        roi_text = self.combo_roi.currentText() if hasattr(self, "combo_roi") else "64x64"
        is_panoramic = ("1920" in roi_text or "Full" in roi_text)

        if "128" in roi_text:
            ring_rad = 50.0
            ring_thick = 15.0
            crop_w, crop_h = int(size * (64.0 / 128.0)), int(size * (64.0 / 128.0))
            title_str = "Focal Plane - Detector (ROI 128x128) | RINGSS Window: 64x64"
        elif "256" in roi_text:
            ring_rad = 25.0
            ring_thick = 8.0
            crop_w, crop_h = int(size * (64.0 / 256.0)), int(size * (64.0 / 256.0))
            title_str = "Focal Plane - Detector (ROI 256x256) | RINGSS Window: 64x64"
        elif is_panoramic:
            ring_rad = 0.0
            ring_thick = 0.0
            crop_w, crop_h = 0, 0
            title_str = "Focal Plane - ASI662MM (Full Frame 1920x1080) | Clean Live View"
        else:
            ring_rad = 75.5
            ring_thick = 22.0
            crop_w, crop_h = size, size
            title_str = "Focal Plane - Detector (ROI 64x64) | Ring Nominal: 10.5 px"

        if hasattr(self, "detector_group"):
            self.detector_group.setTitle(title_str)

        pixmap = QPixmap(size, size)
        pixmap.fill(QColor("#030712"))

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        center_x, center_y = size // 2, size // 2

        if image_data is None:
            y, x = np.ogrid[:size, :size]
            noise = np.random.normal(0, 5.0, (size, size))

            if is_panoramic:
                profile = np.zeros((size, size), dtype=np.float32)
                star_points = [
                    (70, 95, 170), (140, 310, 210), (230, 225, 255), (320, 80, 160),
                    (390, 340, 220), (95, 380, 150), (280, 410, 190), (410, 150, 180)
                ]
                for sx, sy, sb in star_points:
                    profile += np.exp(-0.5 * ((x - sx)**2 + (y - sy)**2) / 3.2) * (sb / 255.0)
            else:
                r = np.hypot(x - center_x, y - center_y)
                profile = np.exp(-0.5 * ((r - ring_rad) / (ring_thick / 2.355)) ** 2)

            intensity = np.clip(profile * 230 + noise + 10, 0, 255).astype(np.uint8)
            # .copy() asegura propiedad de memoria C++ independiente
            qimg_bg = QImage(intensity.data, size, size, size, QImage.Format_Grayscale8).copy()
            painter.drawImage(0, 0, qimg_bg)
        else:
            arr = np.ascontiguousarray(image_data, dtype=np.float32)
            ptp = np.ptp(arr) or 1.0
            norm = np.clip(((arr - arr.min()) / ptp) * 255.0, 0, 255).astype(np.uint8)
            norm = np.ascontiguousarray(norm)
            qimg_raw = QImage(norm.data, norm.shape[1], norm.shape[0], norm.shape[1], QImage.Format_Grayscale8).copy()
            scaled = qimg_raw.scaled(size, size, Qt.KeepAspectRatio, Qt.FastTransformation)
            ox = (size - scaled.width()) // 2
            oy = (size - scaled.height()) // 2
            painter.drawImage(ox, oy, scaled)

        if is_panoramic:
            sensor_h = int(size * 1080.0 / 1920.0)
            y_offset = (size - sensor_h) // 2
            painter.setPen(QPen(QColor(51, 65, 85), 1.5))
            painter.drawRect(0, y_offset, size - 1, sensor_h)

            painter.setPen(QPen(QColor(56, 189, 248, 50), 1, Qt.DotLine))
            painter.drawLine(center_x, y_offset, center_x, y_offset + sensor_h)
            painter.drawLine(0, center_y, size - 1, center_y)
        else:
            painter.setPen(QPen(QColor(56, 189, 248, 90), 1, Qt.DotLine))
            painter.drawLine(center_x, 15, center_x, size - 15)
            painter.drawLine(15, center_y, size - 15, center_y)

            if "64" in roi_text:
                painter.setPen(QPen(QColor(34, 197, 94), 1.5, Qt.DashLine))
                painter.drawEllipse(center_x - int(ring_rad), center_y - int(ring_rad), int(ring_rad * 2), int(ring_rad * 2))
            else:
                half_w = max(10, crop_w // 2)
                half_h = max(10, crop_h // 2)
                painter.setPen(QPen(QColor(234, 179, 8), 1.5, Qt.DashLine))
                painter.drawRect(center_x - half_w, center_y - half_h, half_w * 2, half_h * 2)

        painter.end()
        self.canvas_detector.setPixmap(pixmap)

    def on_mode_changed(self, idx):
        if getattr(self, "_switching_mode_active", False):
            return
        self._switching_mode_active = True

        if self.is_live_view:
            self.toggle_live_view(force_off=True)

        target_mock = (idx == 0)
        mode_text = "SIMULATION (Mock)" if target_mock else "PHYSICAL HARDWARE (ASCOM/SDK)"
        self.current_mode = "MOCK" if target_mock else "REAL"

        self.log_message(f"[CLUSTER] Switching cluster mode to: {mode_text}...")
        self.mode_selector.setEnabled(False)

        switching_style = "font-weight: bold; padding: 4px 10px; border-radius: 4px; background-color: #030712; border: 1px solid #d97706; color: #f59e0b; font-size: 8.5pt;"
        for b in [self.mount_badge, self.foc_badge, self.cam_badge, self.orch_badge]:
            prefix = b.text().split(":")[0]
            b.setText(f"{prefix}: SYNCING")
            b.setStyleSheet(switching_style)

        worker = ZeroMQClientWorker(5559, "SWITCH_MODE", {"mock_mode": target_mock}, timeout_ms=14000, parent=self)

        def on_switch_done(act, resp):
            self._switching_mode_active = False
            self.mode_selector.setEnabled(True)
            if resp.get("status") == "SUCCESS":
                self.log_message(f"[CLUSTER] Reconfigured in mode: {mode_text}.")
                if "cluster" in resp:
                    self.on_cluster_status_received("GET_STATUS", {"status": "SUCCESS", "cluster": resp["cluster"]})
                    return
            else:
                self.log_message(f"[WARN] Watchdog reported error during mode switch: {resp.get('message', 'Timeout')}")

            self.refresh_detector_view(None)
            QTimer.singleShot(800, self.scan_all_hardware)

        worker.response_received.connect(on_switch_done)
        worker.start()

    def scan_all_hardware(self):
        worker = ZeroMQClientWorker(5559, "GET_STATUS", timeout_ms=3500, parent=self)
        worker.response_received.connect(self.on_cluster_status_received)
        worker.start()

    def on_cluster_status_received(self, act, resp):
        badge_green = "font-weight: bold; padding: 4px 10px; border-radius: 4px; background-color: #030712; border: 1px solid #16a34a; color: #22c55e; font-size: 8.5pt;"
        badge_red = "font-weight: bold; padding: 4px 10px; border-radius: 4px; background-color: #030712; border: 1px solid #dc2626; color: #ef4444; font-size: 8.5pt;"

        if resp.get("status") != "SUCCESS":
            for b, name in [(self.mount_badge, "MOUNT"), (self.foc_badge, "FOCUSER"), (self.cam_badge, "CAM"), (self.orch_badge, "ORCH")]:
                b.setText(f"{name}: OFFLINE")
                b.setStyleSheet(badge_red)
            return

        cluster = resp.get("cluster", {})
        is_mock = cluster.get("mock_mode", True)
        services = cluster.get("services", {})

        mapping = [
            ("mount", self.mount_badge, "MOUNT"),
            ("focuser", self.foc_badge, "FOCUSER"),
            ("camera", self.cam_badge, "CAM"),
            ("orchestrator", self.orch_badge, "ORCH")
        ]

        for svc_key, badge_widget, display_name in mapping:
            svc_info = services.get(svc_key, {})
            st = svc_info.get("status", "STOPPED")

            if is_mock:
                if st in ("HEALTHY", "STARTING"):
                    badge_widget.setText(f"{display_name}: MOCK")
                    badge_widget.setStyleSheet(badge_green)
                else:
                    badge_widget.setText(f"{display_name}: OFFLINE")
                    badge_widget.setStyleSheet(badge_red)
            else:
                if st == "HEALTHY":
                    label_text = "ONLINE" if svc_key == "orchestrator" else "REAL"
                    badge_widget.setText(f"{display_name}: {label_text}")
                    badge_widget.setStyleSheet(badge_green)
                else:
                    label_text = "OFFLINE" if svc_key == "orchestrator" else "DISCONNECTED"
                    badge_widget.setText(f"{display_name}: {label_text}")
                    badge_widget.setStyleSheet(badge_red)

    def action_slew_target(self):
        label = self.combo_target.currentText()
        star = self.target_data_map.get(label)
        if star:
            ra, dec = star["ra_hours"], star["dec_deg"]
        else:
            ra, dec = 15.9808, -26.1142

        self.log_message(f"[POINTING] Initiating GOTO to {label} -> RA={ra:.4f}h, Dec={dec:.4f} deg")
        worker = ZeroMQClientWorker(5555, "GOTO", {"ra": ra, "dec": dec}, timeout_ms=75000, parent=self)

        def on_slew_done(act, r):
            if r.get("status") == "SUCCESS":
                self.log_message(f"[OK] {r.get('message', 'Target reached')}")
                self.btn_tracking_toggle.setText("Tracking: ON")
                self.btn_tracking_toggle.setStyleSheet("background-color: #16a34a; color: white;")
                self.poll_mount_status()
            else:
                self.log_message(f"[ERROR] GOTO failed: {r.get('message')}")

        worker.response_received.connect(on_slew_done)
        worker.start()

    def action_nudge_mount(self, direction: str, arcsec: float = 10.0):
        rate = self.combo_nudge_rate.currentText() if hasattr(self, "combo_nudge_rate") else "1X"
        payload = {"direction": direction, "arcsec": float(arcsec), "rate": rate}
        worker = ZeroMQClientWorker(5555, "NUDGE", payload, timeout_ms=3000, parent=self)

        def on_nudge_done(act, r):
            if r.get("status") == "SUCCESS":
                self.log_message(f"[NUDGE] {r.get('message')} -> RA={r.get('ra', 0):.4f}h, Dec={r.get('dec', 0):.4f} deg")
                self.poll_mount_status()
            else:
                self.log_message(f"[ERROR] Nudge failed: {r.get('message')}")

        worker.response_received.connect(on_nudge_done)
        worker.start()

    def action_autocenter_target(self):
        if getattr(self, "is_autocentering", False):
            return
        self.is_autocentering = True

        if self.is_live_view:
            self.toggle_live_view(force_off=True)
            self.log_message("[CAMERA] Live View paused for Auto-Centering routine.")

        self.log_message("[AUTOCENTER] Acquiring reference sub-cube for closed-loop centering...")
        snap_file = str(PROJECT_ROOT / "data" / "captures" / "autocenter_probe.fits")
        os.makedirs(os.path.dirname(snap_file), exist_ok=True)
        worker = ZeroMQClientWorker(5557, "CAPTURE", {"filename": snap_file, "frames": 50}, parent=self)

        def on_probe_captured(act, r):
            self.is_autocentering = False
            if r.get("status") != "SUCCESS":
                self.log_message(f"[ERROR] Failed acquiring reference frame: {r.get('message')}")
                return

            dx_px, dy_px = 0.0, 0.0
            fname = r.get("filename", snap_file)
            if os.path.exists(fname) and ASTROPY_AVAILABLE:
                try:
                    with fits.open(fname) as hdul:
                        data = hdul[0].data
                        if data is not None and data.size > 0:
                            frame = np.mean(data[:min(20, len(data))], axis=0) if data.ndim == 3 else data
                            ny, nx = frame.shape
                            y_idx, x_idx = np.indices((ny, nx))
                            bg = np.median(frame)
                            weights = np.clip(frame - bg, 0, None)
                            total_w = float(np.sum(weights)) or 1.0
                            dx_px = float(np.sum(weights * (x_idx - nx / 2.0)) / total_w)
                            dy_px = float(np.sum(weights * (y_idx - ny / 2.0)) / total_w)
                            self.refresh_detector_view(frame)
                except Exception as ex:
                    self.log_message(f"[WARN] Error calculating centroid: {ex}")

            err_r = math.hypot(dx_px, dy_px)
            scale = getattr(self, "pixel_scale", 1.78)

            if err_r < 0.35:
                self.log_message(f"[AUTOCENTER] Optimal centering reached: Residual error = {err_r:.2f} px (< 0.6 arcsec).")
                return

            self.log_message(
                f"[AUTOCENTER] Measured offset: dx={dx_px:+.2f} px, dy={dy_px:+.2f} px (Radial = {err_r:.2f} px). "
                f"Plate scale = {scale:.2f} arcsec/px."
            )

            if abs(dy_px) >= 0.35:
                arcsec_y = max(0.5, round(abs(dy_px) * scale, 1))
                dir_y = "S" if dy_px > 0 else "N"
                self.action_nudge_mount(dir_y, arcsec_y)

            if abs(dx_px) >= 0.35:
                arcsec_x = max(0.5, round(abs(dx_px) * scale, 1))
                dir_x = "W" if dx_px > 0 else "E"
                QTimer.singleShot(600, lambda: self.action_nudge_mount(dir_x, arcsec_x))

        worker.response_received.connect(on_probe_captured)
        worker.start()

    def action_toggle_tracking(self):
        is_currently_on = "ON" in self.btn_tracking_toggle.text()
        new_state = not is_currently_on
        worker = ZeroMQClientWorker(5555, "SET_TRACKING", {"enabled": new_state, "rate": "Sidereal"}, parent=self)

        def on_trk_done(act, r):
            if r.get("status") == "SUCCESS":
                if new_state:
                    self.btn_tracking_toggle.setText("Tracking: ON")
                    self.btn_tracking_toggle.setStyleSheet("background-color: #16a34a; color: white;")
                else:
                    self.btn_tracking_toggle.setText("Tracking: OFF")
                    self.btn_tracking_toggle.setStyleSheet("background-color: #1e293b; color: #f1f5f9;")
                self.log_message(f"[MOUNT] Sidereal tracking: {'ACTIVE' if new_state else 'STOPPED'}.")
            else:
                self.log_message(f"[ERROR] Error updating tracking: {r.get('message')}")

        worker.response_received.connect(on_trk_done)
        worker.start()

    def send_mount_cmd(self, action: str):
        self.log_message(f"[MOUNT] Dispatched command: {action}")
        worker = ZeroMQClientWorker(5555, action, timeout_ms=3000, parent=self)
        def on_cmd_done(act, r):
            self.log_message(f"[MOUNT] Response ({action}): {r.get('message', r.get('state', 'OK'))}")
            if action in ("PARK", "HOME") and r.get("status") == "SUCCESS":
                self.btn_tracking_toggle.setText("Tracking: OFF")
                self.btn_tracking_toggle.setStyleSheet("background-color: #1e293b; color: #f1f5f9;")
            self.poll_mount_status()
        worker.response_received.connect(on_cmd_done)
        worker.start()

    def action_step_eaf(self, delta: int):
        self.log_message(f"[EAF] Moving relative: {delta:+d} steps...")
        worker = ZeroMQClientWorker(5556, "MOVE_REL", {"steps": delta}, timeout_ms=4000, parent=self)
        def on_eaf(act, r):
            if r.get("status") == "SUCCESS":
                pos = r.get("position", "--")
                self.lbl_eaf_step.setText(str(pos))
                self.log_message(f"[EAF] Position updated: {pos} steps.")
            else:
                self.log_message(f"[ERROR] Focuser step failed: {r.get('message')}")
        worker.response_received.connect(on_eaf)
        worker.start()

    def _finish_autofocus(self, success: bool, msg: str):
        """Finaliza ordenadamente la rutina de enfoque iterativo y restaura controles."""
        self.is_autofocusing = False
        self.btn_autofocus.setText("Run Ring Auto-Focus (EAF)")
        self.btn_autofocus.setStyleSheet("background-color: #16a34a; color: white;")
        if success:
            self.log_message(f"[OK] {msg}")
        else:
            self.log_message(f"[WARN] {msg}")

    def _autofocus_iteration(self, iteration: int, max_iters: int, current_pos: int, tolerance: float = 0.35):
        """Ejecuta una iteración cerrada de medición y corrección para converger en el anillo nominal."""
        if not self.is_autofocusing:
            return

        self.btn_autofocus.setText(f"Calibrating Ring (Iter {iteration}/{max_iters})...")
        self.log_message(f"[AUTOFOCUS] Iteration {iteration}/{max_iters}: Acquiring probe frame...")

        probe_file = str(PROJECT_ROOT / "data" / "captures" / "autofocus_probe.fits")
        os.makedirs(os.path.dirname(probe_file), exist_ok=True)

        worker_probe = ZeroMQClientWorker(
            5557,
            "CAPTURE",
            {"filename": probe_file, "frames": 50, "star": self.combo_target.currentText()},
            timeout_ms=15000,
            parent=self
        )

        def on_probe_done(p_act, p_res):
            if not self.is_autofocusing:
                return

            if p_res.get("status") != "SUCCESS":
                self._finish_autofocus(False, f"Capture failed during autofocus: {p_res.get('message')}")
                return

            measured_rad = 10.5
            fname = p_res.get("filename", probe_file)

            if os.path.exists(fname) and ASTROPY_AVAILABLE and CUBE2_AVAILABLE:
                try:
                    with fits.open(fname) as hdul:
                        cube = hdul[0].data
                        if cube is not None and cube.ndim == 3 and cube.shape[0] >= 50:
                            if cube.shape[1] > 64 or cube.shape[2] > 64:
                                cy, cx = cube.shape[1] // 2, cube.shape[2] // 2
                                cube = cube[:, cy - 32:cy + 32, cx - 32:cx + 32]
                            d_dict, imav = cube2.Moments(cube)
                            impar = d_dict["image"]["impar"]
                            val = float(impar[3]) if isinstance(impar, list) else float(impar.get("meanrad", 10.5))
                            if np.isfinite(val) and val > 0.5:
                                measured_rad = val
                            self.refresh_detector_view(imav)
                except Exception as err:
                    self.log_message(f"[WARN] Error extracting ring radius: {err}")

            err_rad = 10.5 - measured_rad
            self.log_message(
                f"[AUTOFOCUS] Iteration {iteration}/{max_iters}: Measured radius = {measured_rad:.2f} px "
                f"(Target: 10.50 px, Error: {err_rad:+.2f} px)."
            )

            # Condición de éxito: convergió dentro de la tolerancia estricta (<= 0.35 px)
            if abs(err_rad) <= tolerance:
                self._finish_autofocus(
                    True,
                    f"Optimal nominal ring reached ({measured_rad:.2f} px) at EAF step {current_pos}."
                )
                return

            # Si se agotaron las iteraciones máximas, finalizar con aviso
            if iteration >= max_iters:
                self._finish_autofocus(
                    False,
                    f"Max iterations ({max_iters}) reached. Ring radius: {measured_rad:.2f} px at step {current_pos}."
                )
                return

            # Calcular paso proporcional seguro hacia 10.5 px (acotado entre -100 y +100 pasos)
            delta_steps = int(max(-100, min(100, round(err_rad * 43.0))))
            self.log_message(f"[AUTOFOCUS] Adjusting EAF fine focus: {delta_steps:+d} steps...")

            worker_step = ZeroMQClientWorker(5556, "MOVE_REL", {"steps": delta_steps}, timeout_ms=4000, parent=self)

            def on_step_done(s_act, s_res):
                if not self.is_autofocusing:
                    return

                if s_res.get("status") != "SUCCESS":
                    self._finish_autofocus(False, f"EAF movement failed: {s_res.get('message')}")
                    return

                new_pos = int(s_res.get("position", current_pos + delta_steps))
                self.lbl_eaf_step.setText(str(new_pos))
                # Ejecutar la siguiente iteración de sondeo
                self._autofocus_iteration(iteration + 1, max_iters, new_pos, tolerance)

            worker_step.response_received.connect(on_step_done)
            worker_step.start()

        worker_probe.response_received.connect(on_probe_done)
        worker_probe.start()

    def action_run_autofocus(self):
        """Inicia el ciclo cerrado iterativo para abrir el anillo a ~10.5 px exactos."""
        if getattr(self, "is_autofocusing", False):
            return
        self.is_autofocusing = True

        if self.is_live_view:
            self.toggle_live_view(force_off=True)
            self.log_message("[CAMERA] Live View paused for Auto-Focus calibration.")

        self.btn_autofocus.setText("Calibrating Ring (Iter 1/3)...")
        self.btn_autofocus.setStyleSheet("background-color: #d97706; color: white;")
        self.log_message("[AUTOFOCUS] Applying base ring defocus offset...")

        worker_init = ZeroMQClientWorker(5556, "APPLY_RING_DEFOCUS", timeout_ms=4000, parent=self)

        def on_defocus_applied(act, r):
            if not self.is_autofocusing:
                return

            if r.get("status") != "SUCCESS":
                self._finish_autofocus(False, f"Failed applying ring defocus: {r.get('message')}")
                return

            ring_pos = r.get("ring_position", 24870)
            self.lbl_eaf_step.setText(str(ring_pos))
            # Disparar la primera iteración del bucle de convergencia
            self._autofocus_iteration(iteration=1, max_iters=3, current_pos=ring_pos)

        worker_init.response_received.connect(on_defocus_applied)
        worker_init.start()

    def action_apply_cam_settings(self):
        exp_us = float(self.edit_exp.text() or 1000)
        gain = int(self.edit_gain.text() or 100)
        roi_text = self.combo_roi.currentText()
        self.log_message(f"[CAMERA] Configuring: Exp={exp_us:.0f} us ({exp_us/1000.0:.2f} ms), Gain={gain} dB, ROI={roi_text}")
        worker = ZeroMQClientWorker(
            5557,
            "SET_CONFIG",
            {"exposure_ms": exp_us / 1000.0, "gain": gain, "roi": roi_text},
            timeout_ms=2500,
            parent=self
        )
        worker.response_received.connect(
            lambda a, r: self.log_message(f"[CAMERA] Parameters applied: ROI {r.get('roi_width', 64)}x{r.get('roi_height', 64)}.")
        )
        worker.start()

    def action_measure_seeing(self):
        if self.is_live_view:
            self.toggle_live_view(force_off=True)
            self.log_message("[CAMERA] Live View paused for exclusive science run.")

        exp_us = float(self.edit_exp.text() or 1000)
        gain = int(self.edit_gain.text() or 100)
        frames = int(self.edit_frames.text() or 100)
        star = self.combo_target.currentText()

        self.log_message(f"[ORCHESTRATOR] Starting science seeing sequence for: {star}...")

        payload = {
            "exposure_ms": exp_us / 1000.0,
            "gain": gain,
            "frames_cube": frames,
            "target_star": star
        }

        worker = ZeroMQClientWorker(5558, "START_RINGSS_RUN", payload=payload, timeout_ms=35000, parent=self)

        def on_orch_run_done(act, resp):
            if resp.get("status") == "SUCCESS":
                seeing = float(resp.get("seeing_arcsec", 0.95))
                fsee = float(resp.get("free_seeing_arcsec", seeing * 0.65))
                fried = float(resp.get("fried_r0_cm", 10.5))
                zenith = float(resp.get("zenith_dist_deg", 14.2))
                wind = resp.get("wind_speed_ms", 11.0)
                tau0 = resp.get("tau0_ms", 4.5)
                theta0 = resp.get("theta0_arcsec", 2.1)
                heights = resp.get("heights_km")
                cn2 = resp.get("cn2_profile")
                fname = resp.get("filename")
                ring_rad = resp.get("ring_radius_px", 10.5)

                self.lbl_seeing.setText(f"Seeing: <b>{seeing:.2f}</b> arcsec")
                self.lbl_fsee.setText(f"Free Atmosphere: <b>{fsee:.2f}</b> arcsec")
                self.lbl_fried.setText(f"Fried (r0): <b>{fried:.1f}</b> cm")
                self.lbl_zenith.setText(f"Zenith Dist: <b>{zenith:.1f}</b> deg")
                self.lbl_wind.setText(f"Wind Speed: <b>{wind}</b> m/s")
                self.lbl_tau0.setText(f"Coherence tau0: <b>{tau0}</b> ms")
                self.lbl_theta0.setText(f"Isoplanatic theta0: <b>{theta0}</b> arcsec")

                self.log_message(
                    f"[RESULT] {resp.get('star_hr', 'HR')}: Seeing={seeing:.2f} arcsec, "
                    f"FA={fsee:.2f} arcsec, r0={fried:.1f} cm, Wind={wind} m/s, Ring={ring_rad} px."
                )

                t_elapsed = round(time.time() - self.session_start, 1)
                self.history_time.append(t_elapsed)
                self.history_seeing.append(seeing)
                self.history_r0.append(fried)
                self.history_wind.append(wind)
                if len(self.history_time) > 40:
                    self.history_time.pop(0)
                    self.history_seeing.pop(0)
                    self.history_r0.pop(0)
                    self.history_wind.pop(0)

                if MATPLOTLIB_AVAILABLE:
                    self.ax_trend.clear()
                    self.init_trend_chart()
                    self.ax_trend.plot(self.history_time, self.history_seeing, 'o-', color='#38bdf8', linewidth=1.5, markersize=4)
                    self.canvas_trend.draw()

                    if heights and cn2 and len(heights) == len(cn2):
                        self.ax_prof.clear()
                        self.init_profile_chart(heights=heights, cn2_values=cn2)
                        self.canvas_prof.draw()

                if fname and os.path.exists(fname) and ASTROPY_AVAILABLE:
                    try:
                        with fits.open(fname) as hdul:
                            data = hdul[0].data
                            if data is not None and data.ndim == 3:
                                imav = np.mean(data[:min(50, len(data))], axis=0)
                                self.refresh_detector_view(imav)
                            elif data is not None and data.ndim == 2:
                                self.refresh_detector_view(data)
                    except Exception:
                        self.refresh_detector_view(None)
                else:
                    self.refresh_detector_view(None)

            else:
                self.log_message(f"[ERROR] Orchestrator reported: {resp.get('message', 'Run failed')}")

        worker.response_received.connect(on_orch_run_done)
        worker.start()

    def toggle_continuous_monitoring(self):
        if not self.is_monitoring:
            self.is_monitoring = True
            if self.is_live_view:
                self.toggle_live_view(force_off=True)
            self.btn_continuous.setText("Stop Monitoring")
            self.btn_continuous.setStyleSheet("background-color: #b91c1c; color: white;")
            self.log_message("[MONITOR] Continuous observation loop started.")
            self.monitor_timer.start(5500)
            self.action_measure_seeing()
        else:
            self.is_monitoring = False
            self.btn_continuous.setText("Start Continuous Monitoring")
            self.btn_continuous.setStyleSheet("background-color: #16a34a; color: white;")
            self.monitor_timer.stop()
            self.log_message("[MONITOR] Continuous observation loop stopped.")

    def on_monitor_tick(self):
        if self.is_monitoring:
            self.action_measure_seeing()

    def action_take_snapshot(self):
        if self.is_live_view:
            self.toggle_live_view(force_off=True)

        self.log_message("[CAMERA] Capturing FITS snapshot...")
        snap_file = str(PROJECT_ROOT / "data" / "captures" / "snapshot_frame.fits")
        os.makedirs(os.path.dirname(snap_file), exist_ok=True)
        worker = ZeroMQClientWorker(
            5557,
            "CAPTURE",
            {"filename": snap_file, "frames": 50, "star": self.combo_target.currentText()},
            timeout_ms=10000,
            parent=self
        )
        def on_snap(act, r):
            fname = r.get("filename", snap_file)
            self.log_message(f"[CAMERA] Frame saved: {fname}")
            if os.path.exists(fname) and ASTROPY_AVAILABLE:
                try:
                    with fits.open(fname) as hdul:
                        data = hdul[0].data
                        if data is not None and data.size > 0:
                            if data.ndim == 3:
                                data = data[0]
                            self.refresh_detector_view(data)
                            return
                except Exception:
                    pass
            self.refresh_detector_view(None)
        worker.response_received.connect(on_snap)
        worker.start()

    def trigger_emergency_stop(self):
        self.log_message("[ALERT] EMERGENCY STOP ACTIVATED: Halting Orchestrator, Mount, and Focuser...")

        if self.is_live_view:
            self.toggle_live_view(force_off=True)

        if self.is_monitoring:
            self.toggle_continuous_monitoring()

        if getattr(self, "is_autofocusing", False):
            self.is_autofocusing = False
            self.btn_autofocus.setText("Run Ring Auto-Focus (EAF)")
            self.btn_autofocus.setStyleSheet("background-color: #16a34a; color: white;")

        worker_abort = ZeroMQClientWorker(5558, "ABORT", timeout_ms=1200, parent=self)
        worker_abort.start()

        worker_stop = ZeroMQClientWorker(5555, "STOP", timeout_ms=1200, parent=self)
        worker_stop.response_received.connect(
            lambda a, r: self.log_message(f"[MOUNT] Halted: {r.get('message', r.get('state'))}")
        )
        worker_stop.start()

        worker_halt = ZeroMQClientWorker(5556, "HALT", timeout_ms=1200, parent=self)
        worker_halt.response_received.connect(
            lambda a, r: self.log_message(f"[EAF] Halted: {r.get('message', 'HALT')}")
        )
        worker_halt.start()

        self.btn_tracking_toggle.setText("Tracking: OFF")
        self.btn_tracking_toggle.setStyleSheet("background-color: #1e293b; color: #f1f5f9;")

        stop_style = "font-weight: bold; padding: 4px 10px; border-radius: 4px; background-color: #030712; border: 1px solid #dc2626; color: #ef4444; font-size: 8.5pt;"
        self.mount_badge.setText("MOUNT: STOPPED")
        self.mount_badge.setStyleSheet(stop_style)
        self.foc_badge.setText("FOCUSER: HALTED")
        self.foc_badge.setStyleSheet(stop_style)

    def poll_mount_status(self):
        if getattr(self, "_polling_active", False):
            return
        self._polling_active = True

        worker = ZeroMQClientWorker(5555, "GET_STATUS", timeout_ms=1500, parent=self)
        def on_poll(act, r):
            self._polling_active = False
            if r.get("status") == "SUCCESS":
                ra_h = float(r.get("ra", 15.9808))
                dec_d = float(r.get("dec", -26.1142))
                alt_d, az_d, _ = self.compute_alt_az(ra_h * 15.0, dec_d, self.site_lat, self.site_lon)
                self.lbl_ra.setText(f"R.A.: {ra_h:.3f}h")
                self.lbl_dec.setText(f"Dec: {dec_d:+.2f} deg")
                self.lbl_alt.setText(f"Altitude: {alt_d:+.1f} deg")
                self.lbl_az.setText(f"Azimuth: {az_d:.1f} deg")

        worker.response_received.connect(on_poll)
        worker.start()

    def export_csv_logs(self):
        try:
            path, _ = QFileDialog.getSaveFileName(self, "Export CSV Logs", "ringss_logs.csv", "CSV Files (*.csv)")
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("Elapsed_s,Seeing_arcsec,Fried_r0_cm,Wind_ms\n")
                    for idx, t in enumerate(self.history_time):
                        s = self.history_seeing[idx] if idx < len(self.history_seeing) else 0.0
                        r0 = self.history_r0[idx] if idx < len(self.history_r0) else 0.0
                        w = self.history_wind[idx] if idx < len(self.history_wind) else 0.0
                        f.write(f"{t},{s},{r0},{w}\n")
                self.log_message(f"[SYSTEM] Log file exported to: {path}")
        except Exception as e:
            self.log_message(f"[ERROR] Error exporting CSV: {e}")

    def closeEvent(self, event):
        if hasattr(self, "live_preview_timer"):
            self.live_preview_timer.stop()
        if hasattr(self, "status_poll_timer"):
            self.status_poll_timer.stop()
        if hasattr(self, "monitor_timer"):
            self.monitor_timer.stop()
        event.accept()


def main():
    if not PYSIDE_AVAILABLE:
        print("Critical error: PySide6 is not installed.")
        print("Install it with: pip install PySide6")
        return
    app = QApplication(sys.argv)
    window = RingssMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()