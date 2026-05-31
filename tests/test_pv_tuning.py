"""Test PV tuning: solar position calculation and L-BFGS-B optimisation."""
from __future__ import annotations

import math
import pytest

from custom_components.solcast_solar_enhanced.pv_tuning import (
    _cos_incidence,
    run_tuning,
    solar_position,
)

# ---------------------------------------------------------------------------
# solar_position
# ---------------------------------------------------------------------------

def test_solar_above_horizon_at_melbourne_midday():
    """Sun is above horizon at Melbourne solar noon on summer solstice."""
    # 2024-12-21 02:00 UTC ≈ solar noon at Melbourne (lat -37.9, lon 145.0)
    epoch = 1734746400  # 2024-12-21 02:00 UTC
    az, zen = solar_position(epoch, -37.9, 145.0)
    assert zen < 90.0, f"Expected sun above horizon, got zenith={zen:.1f}°"


def test_solar_below_horizon_at_utc_midnight():
    """Sun is below horizon at 00:00 UTC for a site on the Greenwich meridian."""
    # 2024-06-21 00:00 UTC = midnight for lon=0; sun is well below horizon at London
    epoch = 1718928000
    az, zen = solar_position(epoch, 51.5, 0.0)
    assert zen >= 90.0, f"Expected sun below horizon, got zenith={zen:.1f}°"


def test_solar_zenith_range():
    """Zenith is always in [0°, 180°]."""
    for hour in range(0, 24, 3):
        epoch = 1734739200 + hour * 3600
        az, zen = solar_position(epoch, -37.9, 145.0)
        assert 0.0 <= zen <= 180.0


def test_solar_azimuth_range():
    """Azimuth is always in [0°, 360°]."""
    for hour in range(0, 24, 3):
        epoch = 1734739200 + hour * 3600
        az, zen = solar_position(epoch, -37.9, 145.0)
        assert 0.0 <= az <= 360.0


def test_solar_position_northern_hemisphere():
    """Sun is above horizon at solar noon in London on summer solstice."""
    # 2024-06-21 12:00 UTC ≈ noon at London (lat 51.5, lon 0.0)
    epoch = 1718964000  # 2024-06-21 12:00 UTC
    az, zen = solar_position(epoch, 51.5, 0.0)
    assert zen < 90.0


# ---------------------------------------------------------------------------
# _cos_incidence
# ---------------------------------------------------------------------------

def test_cos_incidence_panel_facing_sun():
    """Panel perfectly facing the sun returns cos_incidence ≈ 1."""
    # Horizontal panel (tilt=0) with sun directly overhead (zenith=0)
    val = _cos_incidence(0.0, 0.0, 0.0, 0.0)
    assert abs(val - 1.0) < 1e-6


def test_cos_incidence_panel_facing_away():
    """Panel facing away from sun clamps to 0."""
    # Sun at zenith=0 (overhead), panel tilted 90° facing away
    val = _cos_incidence(90.0, 180.0, 0.0, 0.0)
    assert val >= 0.0  # clamped, never negative


# ---------------------------------------------------------------------------
# run_tuning
# ---------------------------------------------------------------------------

def test_run_tuning_returns_none_on_empty_records():
    assert run_tuning([], 5.0, 20, 0.95) is None


def test_run_tuning_returns_none_below_10_records():
    records = [
        {"pv_actual": 3.0, "pv_export": 0.0, "battery_charge": 0.0,
         "pv_estimate": 4.0, "clouds": 5, "zenith": 30.0, "azimuth": 45.0}
        for _ in range(9)
    ]
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_filters_cloudy_records():
    """Records with clouds ≥ threshold are excluded; if none remain → None."""
    records = [
        {"pv_actual": 3.0, "pv_export": 0.0, "battery_charge": 0.0,
         "pv_estimate": 4.0, "clouds": 50, "zenith": 30.0, "azimuth": 45.0}
        for _ in range(20)
    ]
    # cloud_threshold=20 — all 50% cloud records are excluded
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_returns_result_with_clear_records():
    """With enough clear-sky, non-clipped records, returns a dict."""
    records = [
        {"pv_actual": 3.0, "pv_export": 0.5, "battery_charge": 0.0,
         "pv_estimate": 4.0, "clouds": 5, "zenith": 30.0 + i * 0.1, "azimuth": 180.0 + i * 0.1}
        for i in range(20)
    ]
    result = run_tuning(records, 5.0, 20, 0.95, initial_tilt=20.0, initial_azimuth=0.0)
    if result is None:
        pytest.skip("scipy not available")

    assert "tilt" in result
    assert "azimuth" in result
    assert "rmse_kw" in result
    assert "n_records" in result
    assert result["n_records"] == 20
    assert 0.0 <= result["tilt"] <= 90.0
    assert -180.0 <= result["azimuth"] <= 180.0
    assert result["rmse_kw"] >= 0.0


def test_run_tuning_excludes_clipped_records():
    """Records where both total_pv and pv_estimate exceed clip threshold are excluded."""
    capacity_kw = 5.0
    clip = 0.95 * capacity_kw  # 4.75 kW
    records = [
        {"pv_actual": 4.8, "pv_export": 0.0, "battery_charge": 0.0,
         "pv_estimate": 4.9, "clouds": 5, "zenith": 30.0, "azimuth": 180.0}
        for _ in range(20)
    ]
    # All records are clipped — should return None (< 10 filtered records)
    result = run_tuning(records, capacity_kw, 20, 0.95)
    assert result is None
