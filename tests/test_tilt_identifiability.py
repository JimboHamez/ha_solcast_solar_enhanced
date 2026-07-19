"""Tilt is only reported when the fit actually determines it.

Changing tilt is nearly degenerate with changing the fitted capacity scale, so once the
residual noise floor is comparable to that ~1-2% shape difference the argmin is set by
noise. `run_tuning` flags that, and every consumer (both tilt sensors, the orientation
advisory) must decline to act rather than publish a number the data does not support.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.solcast_solar_enhanced.const import DAMPENING_GATE_MIN_RECORDS
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.pv_tuning import (
    TUNING_AVAILABLE,
    _extraterrestrial_normal,
    panel_azimuth_to_internal,
    run_tuning,
    solar_position,
)

pytestmark = pytest.mark.skipif(not TUNING_AVAILABLE, reason="numpy not installed")

LAT, LON = -37.9, 145.04
AZ_SOLCAST = -6.0
TRUE_TILT = 25.0


def _records(noise: float = 0.0, low_sun_deficit: float = 0.0, days: int = 12, seed: int = 3):
    """Synthetic clear-sky records for an array at TRUE_TILT.

    ``noise`` is multiplicative scatter; ``low_sun_deficit`` scales output down as the
    sun gets low, imitating shading / incidence-angle losses.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    az_internal = math.radians(panel_azimuth_to_internal(AZ_SOLCAST))
    tr = math.radians(TRUE_TILT)
    out = []
    start = datetime(2026, 7, 1, tzinfo=UTC)
    for d in range(days):
        for slot in range(48):
            when = start + timedelta(days=d, minutes=30 * slot)
            epoch = int(when.timestamp())
            sun_az, zen = solar_position(epoch, LAT, LON)
            if zen >= 85:
                continue
            zr = math.radians(zen)
            cz, sz = math.cos(zr), math.sin(zr)
            dni, dhi = 850.0, 90.0
            ghi = dni * cz + dhi
            if ghi < 60:
                continue
            cos_aoi = max(0.0, cz * math.cos(tr) + sz * math.sin(tr) * math.cos(math.radians(sun_az) - az_internal))
            # Hay-Davies, matching run_tuning's default model — an isotropic generator
            # here would bias the recovered tilt through model mismatch alone.
            ai = min(1.0, max(0.0, dni / _extraterrestrial_normal(when.timetuple().tm_yday)))
            rb = cos_aoi / max(cz, 0.035)
            diffuse = dhi * (ai * rb + (1 - ai) * (1 + math.cos(tr)) / 2)
            poa = dni * cos_aoi + diffuse + ghi * 0.2 * (1 - math.cos(tr)) / 2
            power = 0.0028 * poa
            if low_sun_deficit:
                elev = 90.0 - zen
                power *= 1.0 - low_sun_deficit * min(1.0, max(0.0, (25.0 - elev) / 25.0))
            if noise:
                power *= 1.0 + float(rng.normal(0, noise))
            power = max(power, 1e-4)
            out.append(
                {
                    "period_end_epoch": epoch,
                    "pv_actual": power,
                    "pv_export": 0.0,
                    "pv_estimate": power,
                    "azimuth": sun_az,
                    "zenith": zen,
                    "clouds": 0,
                    "ghi": ghi,
                    "dni": dni,
                    "dhi": dhi,
                    "battery_charge": 0.0,
                }
            )
    return out


def _tune(records):
    return run_tuning(
        records,
        capacity_kw=4.0,
        cloud_threshold=101,
        clipping_threshold=0.95,
        export_limit_kw=0.0,
        fixed_azimuth=panel_azimuth_to_internal(AZ_SOLCAST),
    )


# ---------------------------------------------------------------------------
# run_tuning — the flag itself
# ---------------------------------------------------------------------------


def test_clean_fit_is_identifiable_and_recovers_the_tilt():
    """A tight fit must NOT be rejected — otherwise the check is useless."""
    res = _tune(_records(noise=0.02))
    assert res is not None
    assert res["tilt_identifiable"] is True
    assert res["tilt_unidentifiable_reason"] is None
    assert res["fit_rel_error"] < 0.15
    assert res["tilt"] == pytest.approx(TRUE_TILT, abs=3.0)


def test_noisy_fit_is_rejected_as_too_loose():
    """At the noise level the real arrays sit at, a clean synthetic fit already
    mis-recovers the tilt — so the answer must be withheld, not published.
    """
    res = _tune(_records(noise=0.45))
    assert res is not None
    assert res["tilt_identifiable"] is False
    assert res["tilt_unidentifiable_reason"] == "fit_too_loose"
    assert res["fit_rel_error"] > 0.15


def test_railed_fit_is_rejected():
    """A strong low-sun deficit drives the fit onto the lower grid bound; that is the
    optimiser running out of range, not finding a minimum.
    """
    res = _tune(_records(noise=0.02, low_sun_deficit=0.9))
    assert res is not None
    assert res["tilt"] == pytest.approx(0.0, abs=0.01)
    assert res["tilt_identifiable"] is False
    assert res["tilt_unidentifiable_reason"] == "railed"


def test_moderate_shading_bias_is_a_known_blind_spot():
    """Documented limitation: a mid-range low-sun deficit biases tilt badly downward
    while the fit stays tight, so it passes both checks. Pinned so the gap is visible
    rather than assumed away — if a future check closes it, this test should change.
    """
    res = _tune(_records(noise=0.02, low_sun_deficit=0.4))
    assert res is not None
    assert res["tilt"] < TRUE_TILT - 8  # badly wrong
    assert res["tilt_identifiable"] is True  # ...and not caught


# ---------------------------------------------------------------------------
# Consumers must all honour the flag
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, mock_config_entry)


async def test_property_tilt_sensor_hides_unidentifiable_tilt(coordinator):
    coordinator._tuning_result = {"tilt": 2.5, "tilt_identifiable": False, "fit_rel_error": 0.37}
    assert coordinator.tuning_tilt is None

    coordinator._tuning_result = {"tilt": 24.0, "tilt_identifiable": True, "fit_rel_error": 0.05}
    assert coordinator.tuning_tilt == pytest.approx(24.0)


async def test_site_tilt_sensor_hides_unidentifiable_tilt(coordinator):
    coordinator._site_tuning_results = {
        "a": {"tilt": 0.0, "tilt_identifiable": False},
        "b": {"tilt": 23.7, "tilt_identifiable": True},
    }
    assert coordinator.site_tuned_tilt("a") is None
    assert coordinator.site_tuned_tilt("b") == pytest.approx(23.7)


async def test_orientation_advisory_silent_when_tilt_unidentifiable(coordinator):
    """The advisory rests entirely on the tuned tilt. A 22° 'divergence' built from a
    noise-driven 2.5° must not tell the user their Solcast site is misconfigured.
    """
    diverged_but_unusable = {
        "tilt": 2.5,
        "azimuth": 0.0,
        "n_records": DAMPENING_GATE_MIN_RECORDS + 50,
        "tilt_identifiable": False,
    }
    assert coordinator._orientation_diverged(diverged_but_unusable, 24.75, 0.0) is None

    # Same divergence, but this time the fit determined it — the advisory should fire.
    usable = {**diverged_but_unusable, "tilt_identifiable": True}
    assert coordinator._orientation_diverged(usable, 24.75, 0.0) is not None


async def test_missing_flag_is_treated_as_identifiable(coordinator):
    """Older stored results have no flag; they must not silently vanish."""
    coordinator._tuning_result = {"tilt": 21.0}
    assert coordinator.tuning_tilt == pytest.approx(21.0)
