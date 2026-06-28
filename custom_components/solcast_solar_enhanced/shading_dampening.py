"""Adaptive shading dampening calculation."""

from __future__ import annotations

import logging
import math
from typing import Any

from .const import KT_GHI_CS_FLOOR
from .pv_tuning import clearsky_ghi

_LOGGER = logging.getLogger(__name__)

BASE_MIDPOINT = 30.0
EARLY_CLAMP_PCT = 0.15  # ±15% clamp when α < 0.5
# Neutral anchor for the confidence blend. The dampening factor is derived purely
# from database-collected records; with little data it sits near this no-op value
# and ramps toward the DB-measured ratio as confidence grows. We deliberately do
# NOT seed this from the base solcast_solar integration's dampening factors.
NEUTRAL_FACTOR = 1.0

# Kt (clearness-index) quality bands — the irradiance-based replacement for the
# OWM cloud bands when Open-Meteo GHI is available. A higher Kt is a clearer sky
# and the best data for a shading ratio, mirroring `_cloud_weight`'s intent
# without the unreliable cloud field. Bands step down from the configured
# clear-sky Kt threshold so the gate and the weighting stay in step.
KT_BAND_MID_OFFSET = 0.15  # Weight 0.6 down to (kt_threshold − 0.15).
KT_BAND_LOW_OFFSET = 0.35  # Weight 0.3 down to (kt_threshold − 0.35); below → 0.


def _cloud_weight(clouds: int, threshold: int, max_include: int) -> float:
    """Three-band cloud quality weight."""
    if clouds < threshold:
        return 1.0
    if clouds < int(threshold * 1.5):
        return 0.6
    if clouds <= max_include:
        return 0.3
    return 0.0


def _kt_weight(kt: float, kt_threshold: float) -> float:
    """Three-band clear-sky-index quality weight (the Kt analogue of ``_cloud_weight``).

    A higher Kt is a clearer sky and the highest-quality data for a shading ratio.
    Bands step down from the configured clear-sky threshold; below the low band the
    sky is too overcast to trust and the record is dropped (weight 0), mirroring the
    cloud weight's max-include cutoff.
    """
    if kt >= kt_threshold:
        return 1.0
    if kt >= kt_threshold - KT_BAND_MID_OFFSET:
        return 0.6
    if kt >= kt_threshold - KT_BAND_LOW_OFFSET:
        return 0.3
    return 0.0


def _geometry_weight(
    rec_zenith: float,
    rec_azimuth: float,
    target_zenith: float,
    target_azimuth: float,
) -> float:
    dz = rec_zenith - target_zenith
    da = rec_azimuth - target_azimuth
    # Wrap azimuth difference to [-180, 180]
    while da > 180:
        da -= 360
    while da < -180:
        da += 360
    z_w = math.exp(-0.5 * (dz / 10.0) ** 2)
    a_w = math.exp(-0.5 * (da / 20.0) ** 2)
    return z_w * a_w


def compute_dampening(
    records: list[dict[str, Any]],
    capacity_kw: float,
    cloud_threshold: int,
    cloud_max_include: int,
    clipping_threshold: float,
    target_zenith: float,
    target_azimuth: float,
    export_limit_kw: float = 0.0,
    kt_threshold: float | None = None,
) -> dict[str, Any]:
    """Compute a single half-hour slot's dampening from database-collected records only.

    The factor is the confidence-weighted blend of a neutral 1.0 anchor and the
    DB-measured actual/estimate ratio; no values from the base solcast_solar
    integration are consulted.

    Each record's quality weight comes from its clear-sky basis. When
    ``kt_threshold`` is given (Open-Meteo irradiance available) the measured
    clearness index ``Kt = ghi / clearsky_ghi(zenith)`` drives the weight, since the
    model cloud field is biased and false-overcasts clear days; when it is ``None``
    the legacy OWM cloud bands are used instead.

    When ``export_limit_kw > 0`` the forecast is clipped to the achievable ceiling
    for export-curtailed records (see the loop below) so curtailment is not
    mistaken for shading.

    Returns dict with: factor, alpha, source, clear_sky_basis, quality_records,
    avg_quality, clipped_excluded, forecast_clipped
    """
    clip_kw = capacity_kw * clipping_threshold
    basis = "kt" if kt_threshold is not None else "cloud"

    total_weight = 0.0
    weighted_ratio_sum = 0.0
    clipped_excluded = 0
    forecast_clipped = 0
    n_records = 0

    for r in records:
        pv_actual = float(r.get("pv_actual", 0) or 0)
        total_pv = pv_actual  # inverter AC output already includes export and battery
        pv_est = float(r.get("pv_estimate", 0) or 0)
        pv_export = float(r.get("pv_export", 0) or 0)
        # Distinguish a genuine 0% (clearest sky — the highest-quality records for
        # a shading ratio) from a missing value. A bare `or 100` would coerce a
        # falsy 0 to 100, so `_cloud_weight` would score the clearest sky in its
        # lowest/zero band and drop or under-weight exactly the best data.
        raw_clouds = r.get("clouds")
        clouds = 100 if raw_clouds is None else int(raw_clouds)
        zenith = float(r.get("zenith", 90) or 90)
        azimuth = float(r.get("azimuth", 0) or 0)

        if pv_est <= 0:
            continue

        # Clear-sky basis. Prefer the measured clearness index
        # ``Kt = ghi / clearsky_ghi(zenith)`` when Open-Meteo irradiance is available
        # (``kt_threshold`` set): the model cloud field is biased high and
        # false-overcasts clear days, so it over-rejects exactly the clear records a
        # shading ratio needs. Kt is judged only where the clear-sky reference is
        # meaningful — near-horizon sun gives a tiny, noisy denominator — so such
        # records get no Kt and drop out of the Kt path. Falls back to the cloud
        # bands when Open-Meteo is disabled.
        kt: float | None = None
        if kt_threshold is not None:
            ghi = float(r.get("ghi", 0) or 0)
            cs = clearsky_ghi(zenith)
            if ghi > 0 and cs >= KT_GHI_CS_FLOOR:
                kt = ghi / cs
            is_clear = kt is not None and kt >= kt_threshold
        else:
            is_clear = clouds < cloud_threshold

        # Clipping exclusion — a clear-sky slot whose actual and forecast both pin
        # the clip ceiling is curtailment, not shading.
        if total_pv >= clip_kw and pv_est >= clip_kw and is_clear:
            clipped_excluded += 1
            continue

        if kt_threshold is not None:
            cw = _kt_weight(kt, kt_threshold) if kt is not None else 0.0
        else:
            cw = _cloud_weight(clouds, cloud_threshold, cloud_max_include)
        if cw <= 0:
            continue

        gw = _geometry_weight(zenith, azimuth, target_zenith, target_azimuth)
        combined = cw * gw
        if combined < 1e-6:
            continue

        # Export-curtailment forecast clipping. When grid export is pegged at the
        # limit the inverter holds total output below pv_estimate, so the raw
        # actual/estimate ratio reads spuriously low — curtailment masquerading as
        # shading. Clip the forecast to the achievable ceiling (the delivered
        # output plus whatever export headroom remained) so a curtailed clear-sky
        # record contributes a valid ~1.0 ratio instead of a false penalty. The
        # clip only ever lowers the forecast, never below the delivered output
        # (so ratio ≤ 1.0), and is a no-op when export_limit_kw <= 0 or there was
        # export headroom (i.e. the inverter was not curtailing).
        effective_est = pv_est
        if export_limit_kw > 0:
            ceiling = total_pv + (export_limit_kw - pv_export)
            clipped = max(total_pv, min(pv_est, ceiling))
            if clipped < pv_est - 1e-9:
                effective_est = clipped
                forecast_clipped += 1

        ratio = total_pv / effective_est
        weighted_ratio_sum += combined * ratio
        total_weight += combined
        n_records += 1

    if total_weight < 1e-6 or n_records == 0:
        # No usable DB data — stay neutral (no dampening). We do not consult the
        # base integration's factors.
        return {
            "factor": NEUTRAL_FACTOR,
            "alpha": 0.0,
            "source": "no_data",
            "clear_sky_basis": basis,
            "quality_records": 0.0,
            "avg_quality": 0.0,
            "clipped_excluded": clipped_excluded,
            "forecast_clipped": forecast_clipped,
        }

    db_factor = weighted_ratio_sum / total_weight
    avg_quality = total_weight / n_records

    # α sigmoid: x² / (x² + midpoint²), midpoint scaled by quality
    midpoint = BASE_MIDPOINT / max(avg_quality, 0.1)
    x = total_weight
    alpha = (x * x) / (x * x + midpoint * midpoint)
    alpha = max(0.0, min(1.0, alpha))

    # Blend the DB-measured ratio toward a neutral 1.0 anchor by confidence.
    blended = (1.0 - alpha) * NEUTRAL_FACTOR + alpha * db_factor

    # Early stability clamp when α < 0.5
    if alpha < 0.5:
        lo = NEUTRAL_FACTOR * (1.0 - EARLY_CLAMP_PCT)
        hi = NEUTRAL_FACTOR * (1.0 + EARLY_CLAMP_PCT)
        blended = max(lo, min(hi, blended))
        source = "db_blended"
    else:
        source = "db_history" if alpha > 0.95 else "db_blended"

    return {
        "factor": round(blended, 4),
        "alpha": round(alpha, 4),
        "source": source,
        "clear_sky_basis": basis,
        "quality_records": round(total_weight, 2),
        "avg_quality": round(avg_quality, 3),
        "clipped_excluded": clipped_excluded,
        "forecast_clipped": forecast_clipped,
    }


def average_slot_pairs(slot_factors: list[float]) -> list[float]:
    """Average 48 half-hour slot factors into 24 hourly values."""
    hourly = []
    for i in range(0, 48, 2):
        a = slot_factors[i] if i < len(slot_factors) else 1.0
        b = slot_factors[i + 1] if i + 1 < len(slot_factors) else 1.0
        hourly.append((a + b) / 2.0)
    return hourly
