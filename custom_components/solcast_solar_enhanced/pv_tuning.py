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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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


def panel_azimuth_to_internal(solcast_az: float) -> float:
    """Panel azimuth: Solcast/base convention → the internal solar frame.

    The base integration (and the Solcast API) express panel azimuth as degrees
    from North with **West positive, East negative** (0=N, ±180=S, +90=W, −90=E,
    range −180..180). The internal solar frame used by ``solar_position`` and
    ``_cos_incidence`` is **East positive** (0=N, 90=E, 270=W). The two mirror on
    the East-West axis, so the conversion is a sign flip wrapped to [−180, 180].
    """
    return ((-float(solcast_az) + 180.0) % 360.0) - 180.0


def panel_azimuth_to_solcast(internal_az: float) -> float:
    """Panel azimuth: internal solar frame → Solcast/base convention.

    Inverse of :func:`panel_azimuth_to_internal` (the mirror is its own inverse),
    used to report a tuned azimuth in the same convention the user entered and the
    Solcast site is configured with.
    """
    return ((-float(internal_az) + 180.0) % 360.0) - 180.0


def solar_position(epoch: int, latitude: float, longitude: float) -> tuple[float, float]:
    """Return (azimuth_deg, zenith_deg) for a Unix epoch. Accurate to ±1°."""
    dt = datetime.fromtimestamp(normalize_epoch(epoch), tz=UTC)
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
    cos_zenith = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle)
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


def clearsky_ghi(zenith_deg: float) -> float:
    """Haurwitz clear-sky global horizontal irradiance (W/m²) from solar zenith.

        GHI_cs = 1098 · cos(z) · exp(-0.059 / cos(z)),   for cos(z) > 0 else 0

    Zenith-only and well validated for clear-sky GHI — pure Python, no numpy /
    scipy. Used as the denominator of the clearness index Kt = GHI / clearsky_ghi,
    the irradiance-based replacement for the OWM total-cloud clear-sky gate.
    """
    cos_z = math.cos(math.radians(zenith_deg))
    if cos_z <= 0:
        return 0.0
    return 1098.0 * cos_z * math.exp(-0.059 / cos_z)


def _cos_incidence(tilt_deg: float, azimuth_deg: float, zenith_deg: float, sun_az_deg: float) -> float:
    """Cosine of angle of incidence of sunlight on a tilted panel."""
    tilt = math.radians(tilt_deg)
    panel_az = math.radians(azimuth_deg)
    zenith = math.radians(zenith_deg)
    sun_az = math.radians(sun_az_deg)
    cos_inc = math.cos(zenith) * math.cos(tilt) + math.sin(zenith) * math.sin(tilt) * math.cos(sun_az - panel_az)
    return max(0.0, cos_inc)


# Grid-search refinement schedule: (search half-window °, step °). The first
# stage has no window — it sweeps the full bounds; later stages zoom in around the
# running best. 0.25° final resolution is finer than any real Solcast site config
# (whole degrees) while staying a handful of evaluations.
_GRID_STAGES = (
    (None, 5.0),  # full range, 5° step
    (5.0, 1.0),  # ±5° around best, 1° step
    (1.0, 0.25),  # ±1° around best, 0.25° step
)
_TILT_BOUNDS = (0.0, 90.0)
# Solar constant (W/m²) for the Hay-Davies anisotropy index Ai = DNI / I0.
_SOLAR_CONSTANT = 1361.0


def _minimize_tilt(eval_tilt: Callable[[float], float], initial_tilt: float = 20.0) -> tuple[float, float]:
    """Coarse-to-fine 1-D grid search over panel **tilt**, minimising ``eval_tilt(t)``.

    Azimuth is no longer searched: it is non-identifiable from time-misaligned
    irradiance (degenerate with the irradiance↔power time offset) and biased by
    morning shading, so the tuner fits tilt alone at the configured azimuth (see
    DESIGN_DOCUMENT). The first ``_GRID_STAGES`` stage sweeps the full tilt range,
    so the result has no seed dependence; later stages zoom in around the running
    best. ~30 evaluations total — trivial on a Raspberry Pi. Returns ``(tilt, value)``.
    """
    lo, hi = _TILT_BOUNDS
    best_t = float(initial_tilt)
    best_v = float(eval_tilt(best_t))
    for half_window, step in _GRID_STAGES:
        if half_window is None:
            tilts = np.arange(lo, hi + step / 2, step)
        else:
            t0 = max(lo, best_t - half_window)
            t1 = min(hi, best_t + half_window)
            tilts = np.arange(t0, t1 + step / 2, step)
        for t in tilts:
            v = float(eval_tilt(float(t)))
            if v < best_v:
                best_v, best_t = v, float(t)
    return best_t, best_v


def _extraterrestrial_normal(doy: int) -> float:
    """Extraterrestrial normal irradiance for a day-of-year (W/m²)."""
    return _SOLAR_CONSTANT * (1.0 + 0.033 * math.cos(2.0 * math.pi * doy / 365.0))


def run_tuning(
    records: list[dict[str, Any]],
    capacity_kw: float,
    cloud_threshold: int,
    clipping_threshold: float,
    export_limit_kw: float = 0.0,
    fixed_azimuth: float = 0.0,
    albedo: float = 0.2,
    model: str = "hay_davies",
) -> dict[str, Any] | None:
    """Transposition-based **tilt** optimisation at a fixed azimuth.

    For each candidate tilt the stored Open-Meteo GHI/DNI/DHI are transposed to the
    panel plane (Hay-Davies anisotropic sky, or isotropic when ``model`` is not
    ``"hay_davies"``), a single capacity scale is fitted by least squares, and the
    mean-absolute error against measured ``pv_actual`` is scored. The lowest-MAE
    tilt over a coarse-to-fine grid wins. This is the notebook-3.4 approach adapted
    to run offline: real per-orientation irradiance instead of a per-orientation
    Solcast API call.

    Azimuth is held at ``fixed_azimuth`` (internal solar frame) — deliberately not
    tuned. It is non-identifiable from this data (degenerate with the
    irradiance↔power time offset, and biased by morning shading), so tuning it would
    do more harm than good. Unlike the former cosine-ratio tuner this needs no seed
    and does not echo the configured orientation back; it recovers tilt from the
    physical transposition. Returns a result dict (``azimuth`` echoed back as the
    fixed value) or ``None`` (numpy absent, or < 10 usable irradiance-bearing rows).
    """
    if not TUNING_AVAILABLE:
        _LOGGER.warning("numpy not available — skipping PV tuning")
        return None

    clip_kw = capacity_kw * clipping_threshold
    export_clip_kw = export_limit_kw * clipping_threshold if export_limit_kw > 0 else 0.0

    obs, zenith, sun_az, ghi, dni, dhi, i0 = [], [], [], [], [], [], []
    export_limited_excluded = 0
    for r in records:
        raw_ghi = r.get("ghi")
        if raw_ghi is None:
            continue
        g = float(raw_ghi)
        if g <= 0.0:  # no daytime irradiance (night / not backfilled)
            continue
        # Distinguish a genuine 0% cloud (clearest sky — the best data) from a
        # missing value, as the old tuner did: a bare ``or 100`` would drop a falsy
        # 0 and lose exactly the records tuning most wants.
        raw_clouds = r.get("clouds")
        clouds = 100.0 if raw_clouds is None else float(raw_clouds)
        if clouds >= cloud_threshold:
            continue
        zen = float(r.get("zenith", 90) or 90)
        if zen >= 90.0:
            continue
        total_pv = float(r.get("pv_actual", 0) or 0)  # AC output incl. export + battery
        pv_est = float(r.get("pv_estimate", 0) or 0)
        pv_export = float(r.get("pv_export", 0) or 0)
        # Clipping exclusion: both delivered and forecast pinned at the ceiling.
        if total_pv >= clip_kw and pv_est >= clip_kw:
            continue
        # Export-curtailment exclusion — after the cloud/clip filters so the tally
        # matches the former ordering.
        if export_clip_kw > 0 and pv_export >= export_clip_kw:
            export_limited_excluded += 1
            continue
        epoch = r.get("period_end_epoch")
        doy = datetime.fromtimestamp(normalize_epoch(epoch), tz=UTC).timetuple().tm_yday if epoch else 172
        obs.append(total_pv)
        zenith.append(zen)
        sun_az.append(float(r.get("azimuth", 0) or 0))
        ghi.append(g)
        dni.append(float(r.get("dni", 0) or 0))
        dhi.append(float(r.get("dhi", 0) or 0))
        i0.append(_extraterrestrial_normal(doy))

    n_filtered = len(obs)
    if n_filtered < 10:
        _LOGGER.debug("Insufficient tuning records with irradiance: %d (need 10)", n_filtered)
        return None

    obs_a = np.array(obs)
    zen_r = np.radians(zenith)
    sun_az_r = np.radians(sun_az)
    cos_z = np.cos(zen_r)
    sin_z = np.sin(zen_r)
    ghi_a, dni_a, dhi_a = np.array(ghi), np.array(dni), np.array(dhi)
    # Hay-Davies anisotropy index: fraction of diffuse arriving from the sun's
    # direction (circumsolar) rather than the whole sky dome.
    ai = np.clip(dni_a / np.maximum(np.array(i0), 1.0), 0.0, 1.0)
    fixed_az_r = math.radians(fixed_azimuth)
    use_hd = model == "hay_davies"

    def poa(tilt_deg: float) -> np.ndarray:
        """Plane-of-array irradiance over all records for one tilt (W/m²)."""
        tr = math.radians(tilt_deg)
        cos_aoi = np.maximum(0.0, cos_z * math.cos(tr) + sin_z * math.sin(tr) * np.cos(sun_az_r - fixed_az_r))
        beam = dni_a * cos_aoi
        iso = (1.0 + math.cos(tr)) / 2.0
        if use_hd:
            rb = cos_aoi / np.maximum(cos_z, 0.035)  # clamp low-sun blow-up
            diffuse = dhi_a * (ai * rb + (1.0 - ai) * iso)
        else:
            diffuse = dhi_a * iso
        ground = ghi_a * albedo * (1.0 - math.cos(tr)) / 2.0
        total: np.ndarray = beam + diffuse + ground
        return total

    def scale_for(p: np.ndarray) -> float:
        """Closed-form least-squares capacity scale (kW per W/m²)."""
        denom = float(np.dot(p, p))
        return float(np.dot(p, obs_a) / denom) if denom > 0 else 0.0

    def eval_tilt(tilt_deg: float) -> float:
        p = poa(tilt_deg)
        s = scale_for(p)
        return float(np.mean(np.abs(s * p - obs_a)))

    best_tilt, best_mae = _minimize_tilt(eval_tilt)
    best_poa = poa(best_tilt)
    scale = scale_for(best_poa)
    rmse = float(np.sqrt(np.mean((scale * best_poa - obs_a) ** 2)))

    return {
        "tilt": best_tilt,
        # Azimuth is fixed (not tuned); echo it back normalised so the existing
        # consumers (panel_azimuth_to_solcast, the dampening convergence gate) work
        # unchanged — the gate's azimuth delta is then identically ~0.
        "azimuth": ((fixed_azimuth + 180.0) % 360.0) - 180.0,
        "rmse_kw": rmse,
        "mae_kw": best_mae,
        "capacity_scale": scale,
        "n_records": n_filtered,
        "export_limited_excluded": export_limited_excluded,
        "source": model,
    }
