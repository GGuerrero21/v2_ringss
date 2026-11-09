import numpy as np
from astropy.io import fits

import services.science_adapter as science_adapter
from services.science_adapter import RingssSciencePipeline


def test_missing_fits_never_returns_synthetic_success(tmp_path):
    result = RingssSciencePipeline(allow_mock_fallback=True).process_cube(
        str(tmp_path / "missing.fits")
    )

    assert result["status"] == "ERROR"


def test_invalid_fits_shape_never_returns_synthetic_success(tmp_path):
    fits_path = tmp_path / "invalid-shape.fits"
    fits.writeto(fits_path, np.zeros((64, 64), dtype=np.float32))

    result = RingssSciencePipeline(allow_mock_fallback=True).process_cube(str(fits_path))

    assert result["status"] == "ERROR"
    assert "50 frames" in result["message"]


def test_core_fallback_is_explicitly_mock_only(tmp_path, monkeypatch):
    fits_path = tmp_path / "valid-shape.fits"
    header = fits.Header()
    header["EXPOSURE"] = 1000
    header["GAIN"] = 100
    header["DATE-OBS"] = "2026-11-09T00:00:00"
    header["STAR"] = "5944"
    fits.writeto(fits_path, np.zeros((50, 64, 64), dtype=np.float32), header)
    monkeypatch.setattr(
        science_adapter.cube2,
        "Moments",
        lambda cube: (_ for _ in ()).throw(RuntimeError("test core failure")),
    )

    real_result = RingssSciencePipeline().process_cube(str(fits_path))
    mock_result = RingssSciencePipeline(allow_mock_fallback=True).process_cube(str(fits_path))

    assert real_result["status"] == "ERROR"
    assert mock_result["status"] == "SUCCESS"
    assert mock_result["mode"] == "MOCK_FALLBACK"