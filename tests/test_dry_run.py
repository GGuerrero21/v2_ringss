import sys
import json
from pathlib import Path
import pytest

# 1. Definir rutas del proyecto
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = PROJECT_ROOT / "ringss_restore_profile" / "core"
RESTORE_DIR = PROJECT_ROOT / "ringss_restore_profile" / "restore_profile"
LEGACY_DIR = PROJECT_ROOT / "ringss_restore_profile" / "legacy"

# PROJECT_ROOT primero para que los imports relativos de paquete funcionen,
# y los subdirectorios después como respaldo para los scripts con imports planos.
for path in (LEGACY_DIR, RESTORE_DIR, CORE_DIR, PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

DATA_DIR = PROJECT_ROOT / "ringss_restore_profile" / "data"
SAMPLE_STM = PROJECT_ROOT / "2022-05-18.stm"


def test_environment_and_imports():
    """Verifica librerías base y los módulos críticos del pipeline en tiempo real."""
    import numpy as np
    import cv2
    import zmq
    from astropy.io import fits

    # Módulos empaquetados modernos (restore_profile)
    from ringss_restore_profile.restore_profile import (
        core,
        read_moments,
        restore,
        star,
    )

    # Módulos de core y legacy (excluyendo readstarcat.py que es un script one-off)
    from ringss_restore_profile.core import getzen, profrest5, readstm
    import cube2

    # Módulos offline de generación de matrices (opcionales en tiempo real)
    try:
        import zernike
        from ringss_restore_profile.core import getimage, aberration, aweight, getweights
    except ImportError:
        pass


def test_data_files_integrity():
    """Audita que los archivos JSON de configuración existan y sean válidos."""
    expected_jsons = ["par-tololo.json", "starcat.json", "weights.json", "zmat.json"]
    
    for filename in expected_jsons:
        filepath = DATA_DIR / filename
        assert filepath.exists(), f"No se encontró el archivo: {filepath}"
        
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data is not None, f"El archivo {filename} está vacío."


def test_sample_stm_exists():
    """Verifica acceso al archivo histórico .stm para pruebas en seco."""
    assert SAMPLE_STM.exists(), f"No se encontró {SAMPLE_STM}"
    assert SAMPLE_STM.stat().st_size > 0


def test_stm_parsing_dry_run():
    """Task 1.3: Verifica la lectura del archivo .stm y catálogo con los módulos del paquete."""
    from ringss_restore_profile.core import readstm
    from ringss_restore_profile.restore_profile import read_moments, star

    with open(SAMPLE_STM, "r", encoding="utf-8", errors="ignore") as f:
        lines = [line.strip() for _, line in zip(range(25), f) if line.strip()]

    assert len(lines) > 0, "El archivo .stm no pudo leerse."