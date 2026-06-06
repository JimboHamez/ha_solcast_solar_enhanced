"""Rooftop PV tuning — tilt/azimuth optimisation via a numpy grid search.

The optimiser is a coarse-to-fine grid search (the same method Solcast SDK
notebook 3.4 uses), deliberately **without scipy**: scipy has no prebuilt wheel
for ARM/Raspberry Pi and its from-source build fails under Home Assistant's
locked-down environment (meson permission denial — see BJReplay/ha-solcast-solar
issue #85). numpy alone is enough here (it is a core Home Assistant dependency and
ships Pi wheels), so tuning works out of the box on the hardware most HA users run.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

try:
    import numpy as np
    TUNING_AVAILABLE = True
except ImportError:
    TUNING_AVAILABLE = False
    _LOGGER.info("numpy not installed — PV tuning disabled")


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
    # Normalise the hour angle to [-180, 180]. When the local morning falls on a
    # different UTC calendar day from solar noon (e.g. a UTC+10 morning is the
    # previous UTC day, a UTC-10 afternoon the next), the raw value overflows past
    # ±180°, which would wrongly trip the afternoon branch below and mirror the
    # azimuth east<->west. cos() is periodic so zenith is unaffected, but the
    # azimuth's morning/afternoon sign decision is not. Hemisphere-agnostic.
    hour_angle_deg = ((15 * (hour_utc - solar_noon)) + 180) % 360 - 180
    hour_angle = math.radians(hour_angle_deg)

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


# Grid-search refinement schedule: (search half-window °, step °). The first
# stage has no window — it sweeps the full bounds; later stages zoom in around the
# running best. 0.25° final resolution is finer than any real Solcast site config
# (whole degrees) while staying a handful of evaluations.
_GRID_STAGES = (
    (None, 5.0),   # full range, 5° step
    (5.0, 1.0),    # ±5° around best, 1° step
    (1.0, 0.25),   # ±1° around best, 0.25° step
)
_TILT_BOUNDS = (0.0, 90.0)
_AZIMUTH_BOUNDS = (-180.0, 180.0)


def _minimize_grid(
    eval_row, initial_tilt: float, initial_azimuth: float
) -> tuple[float, float, float]:
    """Coarse-to-fine grid search, **azimuth-outer**, minimising the RMSE returned
    by ``eval_row(tilts, az)``.

    ``eval_row`` takes a 1-D array of tilt candidates and a single azimuth and
    returns one RMSE per tilt. This batched contract is the Raspberry-Pi
    optimisation: it lets the caller compute the single expensive transcendental
    (``cos(sun_az − panel_az)`` over all records) **once per azimuth** and then
    evaluate the whole column of tilts as one vectorised numpy multiply-add,
    instead of recomputing that cosine at every (tilt, azimuth) point. Peak memory
    stays bounded to one ``tilts × records`` block (tilts ≈ 19), never a full grid.
    Replaces the former ``scipy.optimize.minimize`` (L-BFGS-B) — grid search is the
    method Solcast notebook 3.4 uses and needs no compiled extension. Returns
    ``(tilt, azimuth, value)`` with azimuth normalised to ``[-180, 180]``.
    """
    t_lo, t_hi = _TILT_BOUNDS
    a_lo, a_hi = _AZIMUTH_BOUNDS
    best_tilt, best_az = float(initial_tilt), float(initial_azimuth)
    best_val = float(eval_row(np.array([best_tilt]), best_az)[0])

    for half_window, step in _GRID_STAGES:
        if half_window is None:
            tilts = np.arange(t_lo, t_hi + step / 2, step)
            # Azimuth is periodic in the objective; sweep [-180, 180) so +180 isn't
            # a duplicate of -180.
            azimuths = np.arange(a_lo, a_hi, step)
        else:
            t0 = max(t_lo, best_tilt - half_window)
            t1 = min(t_hi, best_tilt + half_window)
            tilts = np.arange(t0, t1 + step / 2, step)
            azimuths = np.arange(best_az - half_window, best_az + half_window + step / 2, step)
        for a in azimuths:
            vals = eval_row(tilts, float(a))         # one RMSE per tilt candidate
            j = int(np.argmin(vals))
            if vals[j] < best_val:
                best_val, best_tilt, best_az = float(vals[j]), float(tilts[j]), float(a)

    # Fold the (possibly out-of-range, from a refinement window) azimuth back into
    # the canonical [-180, 180] band.
    best_az = ((best_az + 180.0) % 360.0) - 180.0
    return best_tilt, best_az, best_val


def run_tuning(
    records: list[dict[str, Any]],
    capacity_kw: float,
    cloud_threshold: int,
    clipping_threshold: float,
    export_limit_kw: float = 0.0,
    initial_tilt: float = 20.0,
    initial_azimuth: float = 0.0,
) -> dict[str, Any] | None:
    """Grid-search tilt/azimuth optimisation. Returns dict with tilt, azimuth, rmse, n_records or None."""
    if not TUNING_AVAILABLE:
        _LOGGER.warning("numpy not available — skipping PV tuning")
        return None

    clip_kw = capacity_kw * clipping_threshold
    export_clip_kw = export_limit_kw * clipping_threshold if export_limit_kw > 0 else 0.0

    # Pull the needed columns into arrays in a single pass, preserving the exact
    # None/missing coercions of the former per-record loop. Clouds is the only
    # special case: a missing value becomes the 100 "overcast" sentinel so that an
    # unknown-weather row is excluded by the cloud filter rather than mistaken for
    # clear sky (a bare `or 100` would also drop a genuine, falsy 0% — the clearest
    # sky, the records tuning most wants — so None is distinguished from 0).
    n = len(records)
    pv_actual = np.empty(n)
    pv_export = np.empty(n)
    pv_est = np.empty(n)
    clouds = np.empty(n)
    zenith = np.empty(n)
    azimuth = np.empty(n)
    for i, r in enumerate(records):
        pv_actual[i] = float(r.get("pv_actual", 0) or 0)
        pv_export[i] = float(r.get("pv_export", 0) or 0)
        pv_est[i] = float(r.get("pv_estimate", 0) or 0)
        raw_clouds = r.get("clouds")
        clouds[i] = 100.0 if raw_clouds is None else float(raw_clouds)
        zenith[i] = float(r.get("zenith", 90) or 90)
        azimuth[i] = float(r.get("azimuth", 0) or 0)

    total_pv = pv_actual  # inverter AC output already includes export and battery

    # Vectorised filter — the same exclusions as the former Python loop, as boolean
    # masks. Order is preserved for the export-limited tally: a row is only counted
    # as export-limited if it first passed the cloud and clipping filters (for an
    # integer threshold, clouds < T is exactly int(clouds) < T, so no truncation is
    # needed).
    passed_clip = (clouds < cloud_threshold) & ~((total_pv >= clip_kw) & (pv_est >= clip_kw))
    if export_clip_kw > 0:
        export_fail = passed_clip & (pv_export >= export_clip_kw)
    else:
        export_fail = np.zeros(n, dtype=bool)
    export_limited_excluded = int(np.count_nonzero(export_fail))
    mask = passed_clip & ~export_fail & (pv_est > 0) & (zenith < 90)

    n_filtered = int(np.count_nonzero(mask))
    if n_filtered < 10:
        _LOGGER.debug("Insufficient tuning records: %d (need 10)", n_filtered)
        return None

    # Vectorised geometry. The sun position (zenith/azimuth) is fixed per record,
    # so precompute its trig once; the optimiser then only varies tilt/panel-az.
    # This replaces a per-record Python _cos_incidence call on every objective
    # evaluation (~millions of calls over a run) with array math — the same
    # formula, far cheaper on low-power CPUs.
    zenith_rad = np.radians(zenith[mask])
    sun_az_rad = np.radians(azimuth[mask])
    cos_zenith = np.cos(zenith_rad)
    sin_zenith = np.sin(zenith_rad)
    pv_est_arr = pv_est[mask]
    total_pv_arr = total_pv[mask]

    def cos_incidence_vec(tilt_deg: float, az_deg: float) -> np.ndarray:
        """Vectorised equivalent of _cos_incidence over all records (single point)."""
        tilt = math.radians(tilt_deg)
        panel_az = math.radians(az_deg)
        cos_inc = (
            cos_zenith * math.cos(tilt)
            + sin_zenith * math.sin(tilt) * np.cos(sun_az_rad - panel_az)
        )
        return np.maximum(0.0, cos_inc)

    nominal_cos = cos_incidence_vec(initial_tilt, initial_azimuth)
    safe_nom = np.where(nominal_cos > 1e-6, nominal_cos, 1e-6)

    def eval_row(tilts_deg: np.ndarray, az_deg: float) -> np.ndarray:
        """RMSE for each candidate tilt at one azimuth (the batched objective the
        grid search drives). The azimuth-dependent cosine is computed once here —
        the single costly transcendental — then the tilt sweep is an outer-product
        multiply-add over (tilts × records). See ``_minimize_grid`` for why this
        shape is the Raspberry-Pi win."""
        az_term = sin_zenith * np.cos(sun_az_rad - math.radians(az_deg))   # (N,)
        cos_t = np.cos(np.radians(tilts_deg))[:, None]                     # (T, 1)
        sin_t = np.sin(np.radians(tilts_deg))[:, None]                     # (T, 1)
        cos_inc = np.maximum(0.0, cos_zenith[None, :] * cos_t + az_term[None, :] * sin_t)  # (T, N)
        scaled = pv_est_arr[None, :] * (cos_inc / safe_nom[None, :])
        return np.sqrt(np.mean((scaled - total_pv_arr[None, :]) ** 2, axis=1))  # (T,)

    initial_rmse = float(eval_row(np.array([float(initial_tilt)]), float(initial_azimuth))[0])
    best_tilt, best_az, best_rmse = _minimize_grid(eval_row, initial_tilt, initial_azimuth)

    if best_rmse >= initial_rmse:
        _LOGGER.debug("PV tuning did not improve on initial values")

    return {
        "tilt": best_tilt,
        "azimuth": best_az,
        "rmse_kw": best_rmse,
        "n_records": n_filtered,
        "export_limited_excluded": export_limited_excluded,
    }
