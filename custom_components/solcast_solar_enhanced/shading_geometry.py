"""Geometric shading advisory — a sky map of where an array loses beam irradiance.

This module answers a question the half-hourly dampening path structurally cannot.
Shading is a function of **sun position**, but ``shading_dampening`` buckets history
by clock hour and day-of-year, which scramble solar elevation; it then applies a
clear-sky gate that discards exactly the overcast records establishing the unshaded
baseline. The result is a curve that can only ever under-correct a shaded morning.

Here the same database is read in sun-position space instead. For each record the
stored Open-Meteo irradiance is transposed to the panel plane, split into beam and
diffuse, and the measured-to-forecast ratio is inverted for the beam transmission
that would explain it:

    R = k · [ f(elev, azim) · b  +  s · (1 − b) ]

    b   beam fraction of plane-of-array irradiance (from stored DNI/DHI/GHI)
    f   shadow transmission — the fitted surface, one median per sky cell
    k   intrinsic capacity ratio, fitted where the array is known to be unshaded
    s   diffuse transmission (sky-view factor), fitted on overcast records

``f`` is recovered per record by closed-form inversion rather than by optimisation,
so there is no solver and no numpy dependency.

**This module is advisory only.** Nothing here is pushed to the base integration.
Two limits make that the right call, both measured rather than assumed:

* Against a single array the reference is Solcast's own undampened forecast, which
  carries its own low-sun bias. Fitting the same array both ways (differentially
  against a co-oriented neighbour, and singly against the forecast) agrees well
  above 30° elevation (mean absolute difference 0.03) and **fails below 15°**
  (0.39, with cells inverting to negative transmission). The cause is structural:
  ``s`` is held constant, but an obstruction blocking the beam also blocks part of
  the sky dome, so ``s`` is too generous in precisely the shaded directions.
* Shading common to every array on the property is absorbed into the forecast
  residual and is invisible here.

So the honest claim is that a site can detect that it has low-sun shading and
roughly where — not that the number is accurate enough to divide a forecast by.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from statistics import median
from typing import Any

from .const import (
    MAX_MPPT_TRACKERS,
    SHADING_AZIM_STEP,
    SHADING_BEAM_FRAC_HIGH,
    SHADING_BEAM_FRAC_LOW,
    SHADING_BYPASS_VOLTAGE_MAX,
    SHADING_CLEAN_ELEV_MIN,
    SHADING_ELEV_STEP,
    SHADING_MECHANISM_CURRENT_MAX,
    SHADING_MIN_CELL_RECORDS,
    SHADING_MIN_RECORDS,
    SHADING_UNIFORM_VOLTAGE_MIN,
)
from .pv_tuning import cos_incidence, extraterrestrial_normal, normalize_epoch

_LOGGER = logging.getLogger(__name__)

# Below this the expected output is too small for a ratio to mean anything: the
# denominator's own noise dominates and R explodes.
_MIN_EXPECTED_KW = 0.05
# Plane-of-array irradiance floor (W/m²) below which the beam/diffuse split is noise.
_MIN_POA_W = 20.0
# Fitted transmission is clamped to this range. Above 1 is physically "unshaded plus
# forecast error"; the ceiling keeps one optimistic record from dragging a cell's
# median, and the floor rejects the negative values that low-sun inversion produces
# when the constant sky-view assumption fails.
_TRANSMISSION_BOUNDS = (0.0, 1.5)
# The intrinsic capacity ratio is a property of the array, not of the weather; a
# fitted value outside this range means the forecast reference is unusable.
_CAPACITY_RATIO_BOUNDS = (0.2, 2.0)
# Elevation below which a record counts as "low sun" for the mechanism test.
_MECHANISM_SHADED_ELEV_MAX = 20.0
# Below this elevation the single-array fit cannot separate a blocked beam from a
# blocked sky dome, so a worst cell down here is flagged rather than silently trusted.
_LOW_SUN_UNCERTAIN_ELEV = 15.0
# 16-point compass, indexed by wrapped azimuth / 22.5 with 0 = North, East positive.
_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)  # fmt: skip


def wrap_azimuth(azimuth_deg: float) -> float:
    """Wrap a solar azimuth to [−180, 180) with North at 0 and East positive.

    The stored ``azimuth`` column runs 0–360 in the internal solar frame (0=N,
    90=E). Wrapping it signed keeps the sun's transit in the middle of the range
    rather than at a cell boundary, so a bin never straddles solar noon.
    """
    return ((float(azimuth_deg) + 180.0) % 360.0) - 180.0


def compass_point(azimuth_deg: float) -> str:
    """Return the 16-point compass label for a wrapped solar azimuth."""
    idx = int((wrap_azimuth(azimuth_deg) % 360.0) / 22.5 + 0.5) % 16
    return _COMPASS[idx]


def poa_components(
    tilt_deg: float,
    panel_azimuth_deg: float,
    zenith_deg: float,
    sun_azimuth_deg: float,
    ghi: float,
    dni: float,
    dhi: float,
    doy: int,
    albedo: float = 0.2,
) -> tuple[float, float, float]:
    """Split irradiance into plane-of-array (beam, diffuse, ground) in W/m².

    Uses the same Hay-Davies anisotropic sky as ``pv_tuning.run_tuning`` so the two
    consumers of the stored irradiance cannot drift apart.

    Args:
        tilt_deg: Panel tilt from horizontal.
        panel_azimuth_deg: Panel azimuth in the internal solar frame (0=N, East positive).
        zenith_deg: Solar zenith angle.
        sun_azimuth_deg: Solar azimuth in the internal solar frame.
        ghi: Global horizontal irradiance (W/m²).
        dni: Direct normal irradiance (W/m²).
        dhi: Diffuse horizontal irradiance (W/m²).
        doy: Day of year, for the extraterrestrial normal irradiance.
        albedo: Ground reflectance.

    Returns:
        The (beam, diffuse, ground) plane-of-array components in W/m².
    """
    tilt_r = math.radians(tilt_deg)
    cos_aoi = cos_incidence(tilt_deg, panel_azimuth_deg, zenith_deg, sun_azimuth_deg)
    cos_z = max(math.cos(math.radians(zenith_deg)), 0.035)  # Clamp the low-sun blow-up.
    beam = dni * cos_aoi
    iso = (1.0 + math.cos(tilt_r)) / 2.0
    ai = min(1.0, max(0.0, dni / max(extraterrestrial_normal(doy), 1.0)))
    diffuse = dhi * (ai * (cos_aoi / cos_z) + (1.0 - ai) * iso)
    ground = ghi * albedo * (1.0 - math.cos(tilt_r)) / 2.0
    return beam, max(0.0, diffuse), max(0.0, ground)


def _cell(elevation_deg: float, azimuth_deg: float) -> tuple[int, int]:
    """Return the (elevation, azimuth) sky-cell index a sun position falls in."""
    return (
        math.floor(elevation_deg / SHADING_ELEV_STEP),
        math.floor(wrap_azimuth(azimuth_deg) / SHADING_AZIM_STEP),
    )


def _cell_centre(cell: tuple[int, int]) -> tuple[float, float]:
    """Return the (elevation, azimuth) centre of a sky cell in degrees."""
    return (
        (cell[0] + 0.5) * SHADING_ELEV_STEP,
        (cell[1] + 0.5) * SHADING_AZIM_STEP,
    )


def _prepare(
    records: list[dict[str, Any]],
    tilt_deg: float,
    panel_azimuth_deg: float,
    albedo: float,
) -> list[dict[str, float]]:
    """Reduce raw store rows to the per-record geometry the fit and the classifier share."""
    prepared: list[dict[str, float]] = []
    for record in records:
        zenith = float(record.get("zenith") or 90.0)
        if zenith >= 90.0:
            continue
        observed = float(record.get("pv_actual") or 0.0)
        # The forecast *before* our own pushed factors. Falling back to the dampened
        # figure would measure our own correction back into the ratio.
        expected = float(record.get("pv_estimate_undampened") or 0.0) or float(record.get("pv_estimate") or 0.0)
        if observed <= 0.0 or expected <= _MIN_EXPECTED_KW:
            continue
        epoch = record.get("period_end_epoch")
        doy = datetime.fromtimestamp(normalize_epoch(epoch), tz=UTC).timetuple().tm_yday if epoch else 172
        beam, diffuse, ground = poa_components(
            tilt_deg,
            panel_azimuth_deg,
            zenith,
            float(record.get("azimuth") or 0.0),
            float(record.get("ghi") or 0.0),
            float(record.get("dni") or 0.0),
            float(record.get("dhi") or 0.0),
            doy,
            albedo,
        )
        total = beam + diffuse + ground
        if total < _MIN_POA_W:
            continue
        entry: dict[str, float] = {
            "elevation": 90.0 - zenith,
            "azimuth": wrap_azimuth(float(record.get("azimuth") or 0.0)),
            "beam_fraction": beam / total,
            "poa": total,
            "ratio": observed / expected,
            "expected": expected,
        }
        for idx in range(1, MAX_MPPT_TRACKERS + 1):
            entry[f"v{idx}"] = float(record.get(f"dc_vmed{idx}") or 0.0)
            entry[f"i{idx}"] = float(record.get(f"dc_imed{idx}") or 0.0)
        prepared.append(entry)
    return prepared


def classify_mechanism(prepared: list[dict[str, float]]) -> dict[str, Any]:
    """Decide whether a low-sun loss is a uniform shadow line or a bypass-diode event.

    This is what the per-tracker median DC telemetry exists for, and it is not
    obtainable from AC power alone. A shadow lying across *every* module derates each
    cell string by the same illuminated fraction: current falls in proportion and the
    tracker never leaves Vmp, so the loss is **linear in unshaded area** and a
    multiplicative transmission factor is a valid model of it. A shadow covering some
    modules and not others makes bypass diodes conduct, and the operating voltage
    drops in steps — a non-linearity no single factor can represent.

    The voltage ratio is compared between low-sun and high-sun records directly
    (Vmp is only weakly irradiance-dependent), while the current ratio is normalised
    by plane-of-array irradiance first, because current falls at low sun for the
    entirely innocent reason that there is less light.

    Args:
        prepared: Per-record geometry from ``_prepare``.

    Returns:
        A dict with ``mechanism`` (``uniform`` / ``bypass`` / ``undetermined``), the
        supporting ``voltage_ratio`` and ``current_ratio``, the per-tracker breakdown,
        and ``model_valid`` — whether a multiplicative factor is defensible here.
    """
    trackers: list[dict[str, Any]] = []
    for idx in range(1, MAX_MPPT_TRACKERS + 1):
        low_v: list[float] = []
        low_i: list[float] = []
        high_v: list[float] = []
        high_i: list[float] = []
        for entry in prepared:
            voltage, current, poa = entry[f"v{idx}"], entry[f"i{idx}"], entry["poa"]
            if voltage <= 0.0 or current <= 0.0 or poa <= 0.0:
                continue
            if entry["elevation"] < _MECHANISM_SHADED_ELEV_MAX:
                low_v.append(voltage)
                low_i.append(current / poa)
            elif entry["elevation"] >= SHADING_CLEAN_ELEV_MIN:
                high_v.append(voltage)
                high_i.append(current / poa)
        if len(low_v) < SHADING_MIN_CELL_RECORDS or len(high_v) < SHADING_MIN_CELL_RECORDS:
            continue
        high_v_med, high_i_med = median(high_v), median(high_i)
        if high_v_med <= 0.0 or high_i_med <= 0.0:
            continue
        trackers.append(
            {
                "tracker": idx,
                "voltage_ratio": round(median(low_v) / high_v_med, 4),
                "current_ratio": round(median(low_i) / high_i_med, 4),
                "n_low": len(low_v),
                "n_high": len(high_v),
            }
        )

    if not trackers:
        return {"mechanism": "undetermined", "model_valid": None, "trackers": []}

    # Worst tracker drives the verdict: one shaded string is enough to invalidate a
    # single multiplicative factor for the array.
    voltage_ratio = min(float(t["voltage_ratio"]) for t in trackers)
    current_ratio = min(float(t["current_ratio"]) for t in trackers)

    if current_ratio > SHADING_MECHANISM_CURRENT_MAX:
        # The low-sun current deficit is too small to classify a mechanism from.
        # This is a statement about the DC test, not a claim that nothing is shaded:
        # the fitted surface can still carry a mild mask.
        mechanism, model_valid = "minimal", True
    elif voltage_ratio >= SHADING_UNIFORM_VOLTAGE_MIN:
        mechanism, model_valid = "uniform", True
    elif voltage_ratio <= SHADING_BYPASS_VOLTAGE_MAX:
        mechanism, model_valid = "bypass", False
    else:
        mechanism, model_valid = "undetermined", None

    return {
        "mechanism": mechanism,
        "model_valid": model_valid,
        "voltage_ratio": round(voltage_ratio, 4),
        "current_ratio": round(current_ratio, 4),
        "trackers": trackers,
    }


def analyse_shading(
    records: list[dict[str, Any]],
    tilt_deg: float,
    panel_azimuth_deg: float,
    albedo: float = 0.2,
) -> dict[str, Any] | None:
    """Fit the shading sky map for one array and summarise it.

    Args:
        records: Rows from ``SqliteStore.async_get_records_for_shading``.
        tilt_deg: Configured panel tilt.
        panel_azimuth_deg: Panel azimuth in the internal solar frame (0=N, East positive).
        albedo: Ground reflectance for the transposition.

    Returns:
        The advisory result, or ``None`` when there is not enough history to fit one.
    """
    prepared = _prepare(records, tilt_deg, panel_azimuth_deg, albedo)
    if len(prepared) < SHADING_MIN_RECORDS:
        _LOGGER.debug("Insufficient records for shading advisory: %d (need %d)", len(prepared), SHADING_MIN_RECORDS)
        return None

    # k — intrinsic capacity ratio, measured where the array is high in the sky and
    # beam-dominated, i.e. where it should be unshaded. This absorbs the array's
    # standing offset against Solcast (loss factor, soiling, a mis-stated capacity)
    # so that it is not mistaken for shading.
    clean = [
        e["ratio"]
        for e in prepared
        if e["elevation"] >= SHADING_CLEAN_ELEV_MIN and e["beam_fraction"] >= SHADING_BEAM_FRAC_HIGH
    ]
    capacity_source = "high_sun"
    if len(clean) < SHADING_MIN_CELL_RECORDS:
        # A site that never gets high sun (high latitude, or a winter-only history)
        # still needs a scale; the best available is the whole beam-dominated set,
        # which biases k low if the array is shaded and so *understates* the advisory.
        clean = [e["ratio"] for e in prepared if e["beam_fraction"] >= SHADING_BEAM_FRAC_HIGH]
        capacity_source = "all_beam"
    if len(clean) < SHADING_MIN_CELL_RECORDS:
        return None
    capacity_ratio = min(max(median(clean), _CAPACITY_RATIO_BOUNDS[0]), _CAPACITY_RATIO_BOUNDS[1])

    # s — diffuse transmission (sky-view factor). Overcast records carry almost no
    # beam, so their residual against the forecast is what the array loses to a
    # blocked sky dome rather than to a blocked sun.
    #
    # This MUST be restricted to high sun as well. Diffuse-dominated records skew
    # heavily towards low elevation (dawn, dusk and deep overcast all land in the
    # same bin), so pooling every beam fraction measures the beam shading a second
    # time and books it as sky view. Measured on a live store: the unrestricted fit
    # gives 0.53, the high-sun fit 0.85 on an unshaded array and 0.77 on a shaded
    # neighbour — the latter pair being real, and the difference between the two
    # arrays being the genuine sky-view difference.
    diffuse_ratios = [
        e["ratio"] / capacity_ratio
        for e in prepared
        if e["beam_fraction"] <= SHADING_BEAM_FRAC_LOW and e["elevation"] >= SHADING_CLEAN_ELEV_MIN
    ]
    sky_view_source = "high_sun"
    if len(diffuse_ratios) < SHADING_MIN_CELL_RECORDS:
        # No high-sun overcast history yet. A neutral 1.0 attributes the whole
        # residual to the beam mask, which overstates the mask rather than
        # inventing a sky-view number the data does not support.
        diffuse_ratios, sky_view_source = [], "unavailable"
    sky_view = min(max(median(diffuse_ratios), 0.0), 1.0) if diffuse_ratios else 1.0

    # f — invert each beam-dominated record for the transmission that explains it.
    buckets: dict[tuple[int, int], list[float]] = {}
    for entry in prepared:
        beam_fraction = entry["beam_fraction"]
        if beam_fraction < SHADING_BEAM_FRAC_HIGH:
            continue
        transmission = ((entry["ratio"] / capacity_ratio) - sky_view * (1.0 - beam_fraction)) / beam_fraction
        transmission = min(max(transmission, _TRANSMISSION_BOUNDS[0]), _TRANSMISSION_BOUNDS[1])
        buckets.setdefault(_cell(entry["elevation"], entry["azimuth"]), []).append(transmission)

    surface = {cell: median(values) for cell, values in buckets.items() if len(values) >= SHADING_MIN_CELL_RECORDS}
    if not surface:
        return None

    # Energy-weighted loss, counting the **beam term only**. The diffuse residual is
    # deliberately excluded from the headline: against a single array it conflates a
    # blocked sky dome with Solcast's own overcast bias, and the two cannot be
    # separated without a co-oriented reference array. Only the beam mask is
    # attributable to shading, so only the beam mask is reported as shading.
    weighted_loss = 0.0
    total_weight = 0.0
    for entry in prepared:
        cell_transmission = surface.get(_cell(entry["elevation"], entry["azimuth"]))
        if cell_transmission is None:
            continue
        beam_fraction = entry["beam_fraction"]
        weighted_loss += entry["expected"] * beam_fraction * (1.0 - min(1.0, cell_transmission))
        total_weight += entry["expected"]
    loss_pct = round(100.0 * weighted_loss / total_weight, 1) if total_weight > 0 else 0.0

    worst_cell = min(surface, key=lambda c: surface[c])
    worst_elev, worst_azim = _cell_centre(worst_cell)
    # A cell sitting on the clamp floor did not measure total darkness — the
    # inversion went negative, which is the constant-sky-view assumption failing.
    floored = sum(1 for v in surface.values() if v <= _TRANSMISSION_BOUNDS[0])

    return {
        "loss_pct": loss_pct,
        "capacity_ratio": round(capacity_ratio, 4),
        "capacity_source": capacity_source,
        "sky_view": round(sky_view, 4),
        "sky_view_source": sky_view_source,
        "n_records": len(prepared),
        "n_cells": len(surface),
        "worst_transmission": round(surface[worst_cell], 3),
        "worst_elevation": round(worst_elev, 1),
        "worst_azimuth": round(worst_azim, 1),
        "worst_bearing": compass_point(worst_azim),
        # The single-array fit is unreliable below ~15 deg elevation: the sky-view
        # factor is held constant, but an obstruction blocking the beam blocks part
        # of the sky dome too, so s is too generous exactly where the mask is
        # deepest. Measured against a differential fit, agreement is 0.03 above
        # 30 deg and 0.39 below 15 deg. Flag it rather than hide it.
        "low_sun_uncertain": worst_elev < _LOW_SUN_UNCERTAIN_ELEV or floored > 0,
        "n_cells_floored": floored,
        "surface": {f"{int(_cell_centre(c)[0])}:{int(_cell_centre(c)[1])}": round(v, 3) for c, v in surface.items()},
        **classify_mechanism(prepared),
    }


def evaluate(result: dict[str, Any], elevation_deg: float, azimuth_deg: float, beam_fraction: float) -> float | None:
    """Predict the shading factor at a sun position from a fitted advisory result.

    Args:
        result: A result dict from :func:`analyse_shading`.
        elevation_deg: Solar elevation.
        azimuth_deg: Solar azimuth in the internal solar frame.
        beam_fraction: Beam share of plane-of-array irradiance at that moment.

    Returns:
        The predicted transmission in [0, 1], or ``None`` when that sky cell was
        never populated — an unfitted cell is unknown, not unshaded.
    """
    centre = _cell_centre(_cell(elevation_deg, azimuth_deg))
    transmission = result.get("surface", {}).get(f"{int(centre[0])}:{int(centre[1])}")
    if transmission is None:
        return None
    sky_view = float(result.get("sky_view", 1.0))
    return min(1.0, max(0.0, float(transmission) * beam_fraction + sky_view * (1.0 - beam_fraction)))
