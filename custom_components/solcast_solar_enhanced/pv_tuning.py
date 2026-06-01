"""Rooftop PV tuning — tilt/azimuth optimisation via scipy."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

try:
    import numpy as np
    from scipy.optimize import minimize
    TUNING_AVAILABLE = True
except ImportError:
    TUNING_AVAILABLE = False
    _LOGGER.info("scipy/numpy not installed — PV tuning disabled")


def normalize_epoch(epoch: float) -> int:
    """Coerce a Unix epoch to seconds.

    Accepts seconds, milliseconds, or microseconds and scales them down to
    seconds. A real seconds epoch stays below 1e11 until the year ~5138, so any
    larger value is treated as a finer-grained unit and divided down. This guards
    ``datetime.fromtimestamp`` against the "year NNNNN is out of range" error that
    a millisecond timestamp would otherwise trigger.
    """
    value = float(epoch)
    while value >= 1e11:
        value /= 1000.0
    return int(value)


def solar_position(epoch: int, latitude: float, longitude: float) -> tuple[float, float]:
    """Return (azimuth_deg, zenith_deg) for a Unix epoch. Accurate to ±1°."""
    dt = datetime.fromtimestamp(normalize_epoch(epoch), tz=timezone.utc)
    doy = dt.timetuple().tm_yday
    hour_utc = dt.hour + dt.minute / 60.0 + dt.second / 3600.0

    # Solar declination
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (doy - 81))))

    # Equation of time (minutes)
    B = math.radians(360 / 365 * (doy - 81))
    eot = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)

    # Solar noon local
    solar_noon = 12 - longitude / 15 - eot / 60
    hour_angle = math.radians(15 * (hour_utc - solar_noon))

    lat_r = math.radians(latitude)
    cos_zenith = (
        math.sin(lat_r) * math.sin(decl)
        + math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.degrees(math.acos(cos_zenith))

    # Azimuth (0=North, 90=East)
    sin_zenith = math.sin(math.acos(cos_zenith))
    if sin_zenith < 1e-6:
        azimuth = 0.0
    else:
        cos_az = (math.sin(decl) - math.sin(lat_r) * cos_zenith) / (math.cos(lat_r) * sin_zenith)
        cos_az = max(-1.0, min(1.0, cos_az))
        azimuth = math.degrees(math.acos(cos_az))
        if hour_angle > 0:
            azimuth = 360 - azimuth
    return azimuth, zenith


def _cos_incidence(tilt_deg: float, azimuth_deg: float, zenith_deg: float, sun_az_deg: float) -> float:
    """Cosine of angle of incidence of sunlight on a tilted panel."""
    tilt = math.radians(tilt_deg)
    panel_az = math.radians(azimuth_deg)
    zenith = math.radians(zenith_deg)
    sun_az = math.radians(sun_az_deg)
    cos_inc = (
        math.cos(zenith) * math.cos(tilt)
        + math.sin(zenith) * math.sin(tilt) * math.cos(sun_az - panel_az)
    )
    return max(0.0, cos_inc)


def run_tuning(
    records: list[dict[str, Any]],
    capacity_kw: float,
    cloud_threshold: int,
    clipping_threshold: float,
    initial_tilt: float = 20.0,
    initial_azimuth: float = 0.0,
) -> dict[str, Any] | None:
    """Run L-BFGS-B optimisation. Returns dict with tilt, azimuth, rmse, n_records or None."""
    if not TUNING_AVAILABLE:
        _LOGGER.warning("scipy/numpy not available — skipping PV tuning")
        return None

    clip_kw = capacity_kw * clipping_threshold

    filtered = []
    for r in records:
        pv_actual = float(r.get("pv_actual", 0) or 0)
        pv_export = float(r.get("pv_export", 0) or 0)
        battery = float(r.get("battery_charge", 0) or 0)
        total_pv = pv_actual + pv_export + battery
        pv_est = float(r.get("pv_estimate", 0) or 0)
        clouds = int(r.get("clouds", 100) or 100)
        zenith = float(r.get("zenith", 90) or 90)
        azimuth = float(r.get("azimuth", 0) or 0)

        if clouds >= cloud_threshold:
            continue
        if total_pv >= clip_kw and pv_est >= clip_kw:
            continue
        if pv_est <= 0 or zenith >= 90:
            continue
        filtered.append({
            "total_pv": total_pv,
            "pv_estimate": pv_est,
            "zenith": zenith,
            "azimuth": azimuth,
        })

    if len(filtered) < 10:
        _LOGGER.debug("Insufficient tuning records: %d (need 10)", len(filtered))
        return None

    # Nominal geometry cosines
    nominal_cos = np.array([
        _cos_incidence(initial_tilt, initial_azimuth, r["zenith"], r["azimuth"])
        for r in filtered
    ])
    pv_est_arr = np.array([r["pv_estimate"] for r in filtered])
    total_pv_arr = np.array([r["total_pv"] for r in filtered])

    def rmse(params: np.ndarray) -> float:
        tilt, az = params
        candidate_cos = np.array([
            _cos_incidence(tilt, az, r["zenith"], r["azimuth"]) for r in filtered
        ])
        safe_nom = np.where(nominal_cos > 1e-6, nominal_cos, 1e-6)
        scaled = pv_est_arr * (candidate_cos / safe_nom)
        return float(np.sqrt(np.mean((scaled - total_pv_arr) ** 2)))

    result = minimize(
        rmse,
        x0=np.array([initial_tilt, initial_azimuth]),
        method="L-BFGS-B",
        bounds=[(0, 90), (-180, 180)],
        options={"maxiter": 300},
    )

    if not result.success and result.fun > rmse(np.array([initial_tilt, initial_azimuth])):
        _LOGGER.debug("PV tuning did not improve on initial values")

    return {
        "tilt": float(result.x[0]),
        "azimuth": float(result.x[1]),
        "rmse_kw": float(result.fun),
        "n_records": len(filtered),
    }
