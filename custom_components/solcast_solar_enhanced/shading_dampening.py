"""Adaptive shading dampening calculation."""
from __future__ import annotations

import logging
import math
from typing import Any

_LOGGER = logging.getLogger(__name__)

BASE_MIDPOINT = 30.0
EARLY_CLAMP_PCT = 0.15  # ±15% clamp when α < 0.5
# Neutral anchor for the confidence blend. The dampening factor is derived purely
# from database-collected records; with little data it sits near this no-op value
# and ramps toward the DB-measured ratio as confidence grows. We deliberately do
# NOT seed this from the base solcast_solar integration's dampening factors.
NEUTRAL_FACTOR = 1.0


def _cloud_weight(clouds: int, threshold: int, max_include: int) -> float:
    """Three-band cloud quality weight."""
    if clouds < threshold:
        return 1.0
    if clouds < int(threshold * 1.5):
        return 0.6
    if clouds <= max_include:
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
) -> dict[str, Any]:
    """
    Compute a single half-hour slot's dampening from database-collected records only.

    The factor is the confidence-weighted blend of a neutral 1.0 anchor and the
    DB-measured actual/estimate ratio; no values from the base solcast_solar
    integration are consulted.

    Returns dict with: factor, alpha, source, quality_records, avg_quality, clipped_excluded
    """
    clip_kw = capacity_kw * clipping_threshold

    total_weight = 0.0
    weighted_ratio_sum = 0.0
    clipped_excluded = 0
    n_records = 0

    for r in records:
        pv_actual = float(r.get("pv_actual", 0) or 0)
        total_pv = pv_actual  # inverter AC output already includes export and battery
        pv_est = float(r.get("pv_estimate", 0) or 0)
        clouds = int(r.get("clouds", 100) or 100)
        zenith = float(r.get("zenith", 90) or 90)
        azimuth = float(r.get("azimuth", 0) or 0)

        if pv_est <= 0:
            continue

        # Clipping exclusion
        if total_pv >= clip_kw and pv_est >= clip_kw and clouds < cloud_threshold:
            clipped_excluded += 1
            continue

        cw = _cloud_weight(clouds, cloud_threshold, cloud_max_include)
        if cw <= 0:
            continue

        gw = _geometry_weight(zenith, azimuth, target_zenith, target_azimuth)
        combined = cw * gw
        if combined < 1e-6:
            continue

        ratio = total_pv / pv_est
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
            "quality_records": 0.0,
            "avg_quality": 0.0,
            "clipped_excluded": clipped_excluded,
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
        "quality_records": round(total_weight, 2),
        "avg_quality": round(avg_quality, 3),
        "clipped_excluded": clipped_excluded,
    }


def average_slot_pairs(slot_factors: list[float]) -> list[float]:
    """Average 48 half-hour slot factors into 24 hourly values."""
    hourly = []
    for i in range(0, 48, 2):
        a = slot_factors[i] if i < len(slot_factors) else 1.0
        b = slot_factors[i + 1] if i + 1 < len(slot_factors) else 1.0
        hourly.append((a + b) / 2.0)
    return hourly
