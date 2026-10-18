"""Adaptive shading dampening calculation."""

from __future__ import annotations

import logging
import math
from typing import Any

from .const import KT_GHI_CS_FLOOR, SUNPOS_MIN_COVERAGE_KW, SUNPOS_PRIOR_KW, SUNPOS_SIGMA_AZIM, SUNPOS_SIGMA_ELEV
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


def _record_estimate(r: dict[str, Any]) -> tuple[float, bool]:
    """Return a record's forecast denominator and whether it is the undampened one.

    The denominator must be the forecast *before* our own dampening. ``pv_estimate``
    is read back from the base's detailedForecast, which the base has already
    multiplied by the factors we pushed, so measuring against it closes a feedback
    loop that settles at sqrt(ratio) rather than the true ratio (issue #50). A 0
    means the base could not supply an undampened value (rows written before
    1.10.0b6, or a base too old to answer), so fall back to the dampened figure — a
    slightly biased record still beats no record.
    """
    pv_est_raw = float(r.get("pv_estimate_undampened", 0) or 0)
    if pv_est_raw > 0:
        return pv_est_raw, True
    return float(r.get("pv_estimate", 0) or 0), False


def _clearness(
    r: dict[str, Any], zenith: float, cloud_threshold: int, kt_threshold: float | None
) -> tuple[float | None, int, bool]:
    """Return a record's clearness index, cloud cover and whether its sky counts as clear.

    Prefer the measured clearness index ``Kt = ghi / clearsky_ghi(zenith)`` when
    Open-Meteo irradiance is available (``kt_threshold`` set): the model cloud field
    is biased high and false-overcasts clear days, so it over-rejects exactly the
    clear records a shading ratio needs. Kt is judged only where the clear-sky
    reference is meaningful — near-horizon sun gives a tiny, noisy denominator — so
    such records get no Kt and drop out of the Kt path. Falls back to the cloud bands
    when Open-Meteo is disabled.
    """
    # Distinguish a genuine 0% (clearest sky — the highest-quality records for a
    # shading ratio) from a missing value. A bare `or 100` would coerce a falsy 0 to
    # 100, so `_cloud_weight` would score the clearest sky in its lowest/zero band
    # and drop or under-weight exactly the best data.
    raw_clouds = r.get("clouds")
    clouds = 100 if raw_clouds is None else int(raw_clouds)
    kt: float | None = None
    if kt_threshold is not None:
        ghi = float(r.get("ghi", 0) or 0)
        cs = clearsky_ghi(zenith)
        if ghi > 0 and cs >= KT_GHI_CS_FLOOR:
            kt = ghi / cs
        return kt, clouds, kt is not None and kt >= kt_threshold
    return kt, clouds, clouds < cloud_threshold


def _export_ceiling(
    r: dict[str, Any],
    total_pv: float,
    pv_est: float,
    export_limit_kw: float,
    clipping_threshold: float,
) -> tuple[float, bool, bool]:
    """Clip a record's forecast to what export curtailment allowed.

    Returns ``(effective_estimate, forecast_clipped, export_capped)``. When grid
    export is pegged at the limit the inverter holds total output below pv_estimate,
    so the raw actual/estimate ratio reads spuriously low — curtailment
    masquerading as shading. Clip the forecast to the achievable ceiling so a
    curtailed clear-sky record contributes a valid ~1.0 ratio instead of a false
    penalty. The clip only ever lowers the forecast, never below the delivered output
    (so ratio <= 1.0), and is a no-op when export_limit_kw <= 0.

    Which ceiling depends on whether the interval was capped at any point, which
    only the stored PEAK export can answer (issue #86):

    - **Peak below the limit — nothing was capped.** The mean headroom
      ``export_limit_kw - pv_export`` is then the right figure and is exact: for a
      slot that is capped for a fraction c, mean headroom works out to
      ``L - mean_export`` regardless of c, so the arithmetic was never the problem.
    - **Peak at the limit — the interval was capped.** The problem is that headroom
      is not *fungible across time*: the surplus generation the forecast expects
      would have arrived in exactly the minutes the inverter was already pinned, and
      export headroom that existed twenty minutes later could not have carried it.
      The only defensible ceiling is the delivered output itself, so the record
      contributes a neutral 1.0 rather than a shading penalty it did not earn.
      Recovering the capped fraction and clipping proportionally would need an
      at-limit *duty cycle* stored per row, which bakes today's limit into the data
      (see the column note in sqlite_store) — the conservative ceiling needs no such
      assumption and errs toward under-dampening, the safe direction here.

    ``pv_export_max`` is 0 on rows written before the column existed, which falls
    into the first branch and preserves the previous behaviour exactly.
    """
    if export_limit_kw <= 0:
        return pv_est, False, False
    pv_export = float(r.get("pv_export", 0) or 0)
    pv_export_peak = float(r.get("pv_export_max", 0) or 0)
    capped = pv_export_peak >= export_limit_kw * clipping_threshold
    ceiling = total_pv if capped else total_pv + (export_limit_kw - pv_export)
    clipped = max(total_pv, min(pv_est, ceiling))
    if clipped < pv_est - 1e-9:
        return clipped, True, capped
    return pv_est, False, capped


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

    That ratio is the **energy-weighted aggregate** ``Σ(w·actual) / Σ(w·estimate)``,
    not the weighted mean of each record's own ``actual/estimate``. The mean of
    ratios is unusable here: a slot the base forecast at 0.2 kW that produced 1 kW
    contributes 5.0 and swamps the honest records, and a single bad Solcast poll can
    lift a whole hour's mean above 1.0 — which ``_push_dampening`` then clamps to
    "no dampening at all", silently discarding real shading. Under the aggregate form
    each record's influence is bounded by its own energy (issue #52).

    Each record's quality weight comes from its clear-sky basis. When
    ``kt_threshold`` is given (Open-Meteo irradiance available) the measured
    clearness index ``Kt = ghi / clearsky_ghi(zenith)`` drives the weight, since the
    model cloud field is biased and false-overcasts clear days; when it is ``None``
    the legacy OWM cloud bands are used instead.

    When ``export_limit_kw > 0`` the forecast is clipped to the achievable ceiling
    for export-curtailed records (see the loop below) so curtailment is not
    mistaken for shading.

    The ratio's denominator is each record's ``pv_estimate_undampened`` — the base's
    forecast *before* it applied the factors this integration pushed. Falling back to
    the dampened ``pv_estimate`` (rows predating 1.10.0b6) biases the ratio toward
    1.0, since that forecast already carries our own correction (issue #50).

    ``avg_quality`` is the geometry-weighted mean clear-sky weight of the records
    informing the slot, ``Sigma(gw*cw)/Sigma(gw)`` — "how clear were the skies that
    matter here", independent of how many irrelevant records the history window
    happened to contain.

    Returns dict with: factor, alpha, source, clear_sky_basis, quality_records,
    avg_quality, clipped_excluded, forecast_clipped, export_capped, undampened_records
    """
    clip_kw = capacity_kw * clipping_threshold
    basis = "kt" if kt_threshold is not None else "cloud"

    total_weight = 0.0
    geometry_weight_sum = 0.0
    weighted_actual_sum = 0.0
    weighted_est_sum = 0.0
    clipped_excluded = 0
    forecast_clipped = 0
    export_capped = 0
    n_records = 0
    n_undampened = 0

    for r in records:
        pv_actual = float(r.get("pv_actual", 0) or 0)
        total_pv = pv_actual  # inverter AC output already includes export and battery
        pv_est, used_undampened = _record_estimate(r)
        zenith = float(r.get("zenith", 90) or 90)
        azimuth = float(r.get("azimuth", 0) or 0)

        if pv_est <= 0:
            continue

        kt, clouds, is_clear = _clearness(r, zenith, cloud_threshold, kt_threshold)

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

        # Export-curtailment forecast clipping, so curtailment is not booked as
        # shading — see ``_export_ceiling`` for the mean-vs-peak reasoning (issue #86).
        effective_est, was_clipped, was_capped = _export_ceiling(
            r, total_pv, pv_est, export_limit_kw, clipping_threshold
        )
        export_capped += was_capped
        forecast_clipped += was_clipped

        # Accumulate the *energy* either side of the ratio rather than each record's
        # own ratio. db_factor is Σ(w·actual)/Σ(w·estimate), not the weighted mean of
        # actual/estimate: E[A/F] ≠ E[A]/E[F], and A/F has an exploding right tail as
        # F approaches zero, so a slot forecast at 0.2 kW that produced 1 kW enters a
        # mean-of-ratios at 5.0 and drowns out every honest record. Under the
        # aggregate form that record contributes only its 0.2 kW to the denominator,
        # so its influence is bounded by its own size. Mirrors the estimator
        # ``load_advisory.compute_confidence`` already uses (Σactual/Σestimate).
        weighted_actual_sum += combined * total_pv
        weighted_est_sum += combined * effective_est
        total_weight += combined
        geometry_weight_sum += gw
        n_records += 1
        # Counted only once the record survives every filter, so the diagnostic
        # reflects the records actually weighted into the factor.
        if used_undampened:
            n_undampened += 1

    # A zero weighted forecast leaves the ratio undefined (every surviving record
    # forecast ~0 kW), which carries no shading signal — treat it as no data.
    if total_weight < 1e-6 or n_records == 0 or weighted_est_sum < 1e-9:
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
            "export_capped": export_capped,
            "undampened_records": 0,
        }

    db_factor = weighted_actual_sum / weighted_est_sum
    # Average *sky* quality among the records that actually inform this slot — the
    # geometry-weighted mean of the clear-sky weight, Sigma(gw*cw)/Sigma(gw).
    # Dividing the combined weight by a plain record count instead conflates "how
    # clear was the sky" with "how close in solar geometry", because a record hours
    # away in sun position contributes ~0 to the numerator but a full 1 to the
    # denominator. That made avg_quality fall as the history window grew (more
    # far-off records), which via the midpoint below *raised* the confidence bar the
    # more data was collected.
    avg_quality = total_weight / geometry_weight_sum if geometry_weight_sum > 1e-9 else 0.0

    # α sigmoid: x² / (x² + midpoint²), midpoint scaled by sky quality — a clearer
    # history reaches confidence sooner.
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
        "export_capped": export_capped,
        "undampened_records": n_undampened,
    }


def _sunpos_weight(rec_zenith: float, rec_azimuth: float, target_zenith: float, target_azimuth: float) -> float:
    """Narrow sun-position kernel for the sun-position estimator."""
    dz = rec_zenith - target_zenith
    da = (rec_azimuth - target_azimuth + 180.0) % 360.0 - 180.0
    return math.exp(-0.5 * ((dz / SUNPOS_SIGMA_ELEV) ** 2 + (da / SUNPOS_SIGMA_AZIM) ** 2))


def compute_sunpos_dampening(
    records: list[dict[str, Any]],
    capacity_kw: float,
    cloud_threshold: int,
    cloud_max_include: int,
    clipping_threshold: float,
    target_zenith: float,
    target_azimuth: float,
    export_limit_kw: float = 0.0,
    kt_threshold: float | None = None,
) -> dict[str, Any] | None:
    """Compute a half-hour slot's factor from records taken with the sun in nearly the same place.

    The opt-in alternative to ``compute_dampening`` for a sharp, hard horizon (issue
    #91). Shading is a function of where the sun is, and against a hard edge output
    goes from almost nothing to almost everything across 3–4° of elevation. The
    standard estimator cannot follow that: its geometry kernel is ten degrees wide,
    so records taken after the sun cleared the obstruction dilute the ones taken
    behind it, and its confidence blend holds the factor near 1.0 (no deeper than
    0.85 while α < 0.5) long after the measurement is clear.

    This estimator differs in three ways, each fixed by the walk-forward replay:

    - A narrow kernel (``SUNPOS_SIGMA_ELEV`` × ``SUNPOS_SIGMA_AZIM``).
    - **All weather.** One factor is applied to every day, so it should be fitted to
      every day. Clear-sky gating measures the clear mornings only, while the factor
      is also applied to the cloudy ones. The clear-sky weight is still computed, but
      only to report ``avg_quality``.
    - **No confidence blend.** In its place, a small pseudo-energy prior
      (``SUNPOS_PRIOR_KW``) on both sides of the ratio pulls thin coverage toward
      neutral.

    The ratio is the same energy-weighted aggregate as the standard estimator
    (issue #52), and the clipping exclusion and export-curtailment ceiling are
    shared with it.

    Returns ``None`` when the geometry-weighted forecast is below
    ``SUNPOS_MIN_COVERAGE_KW``: that sun position has not been seen often enough,
    and the caller falls back to ``compute_dampening``. Otherwise it returns the
    same keys as ``compute_dampening``, with ``source`` ``"sun_position"``. Its
    ``alpha`` is the weight the measurement carries against the prior,
    ``Σw·estimate / (Σw·estimate + prior)``, and ``coverage_kw`` is that
    ``Σw·estimate``.
    """
    clip_kw = capacity_kw * clipping_threshold
    basis = "kt" if kt_threshold is not None else "cloud"
    weighted_actual_sum = 0.0
    weighted_est_sum = 0.0
    geometry_weight_sum = 0.0
    quality_weight_sum = 0.0
    clipped_excluded = 0
    forecast_clipped = 0
    export_capped = 0
    n_undampened = 0

    for r in records:
        total_pv = float(r.get("pv_actual", 0) or 0)
        pv_est, used_undampened = _record_estimate(r)
        if pv_est <= 0:
            continue
        zenith = float(r.get("zenith", 90) or 90)
        azimuth = float(r.get("azimuth", 0) or 0)
        kt, clouds, is_clear = _clearness(r, zenith, cloud_threshold, kt_threshold)
        if total_pv >= clip_kw and pv_est >= clip_kw and is_clear:
            clipped_excluded += 1
            continue
        gw = _sunpos_weight(zenith, azimuth, target_zenith, target_azimuth)
        if gw < 1e-6:
            continue
        effective_est, was_clipped, was_capped = _export_ceiling(
            r, total_pv, pv_est, export_limit_kw, clipping_threshold
        )
        forecast_clipped += was_clipped
        export_capped += was_capped
        if kt_threshold is not None:
            cw = _kt_weight(kt, kt_threshold) if kt is not None else 0.0
        else:
            cw = _cloud_weight(clouds, cloud_threshold, cloud_max_include)
        weighted_actual_sum += gw * total_pv
        weighted_est_sum += gw * effective_est
        geometry_weight_sum += gw
        quality_weight_sum += gw * cw
        if used_undampened:
            n_undampened += 1

    if weighted_est_sum < SUNPOS_MIN_COVERAGE_KW:
        return None

    factor = (weighted_actual_sum + SUNPOS_PRIOR_KW) / (weighted_est_sum + SUNPOS_PRIOR_KW)
    return {
        "factor": round(factor, 4),
        "alpha": round(weighted_est_sum / (weighted_est_sum + SUNPOS_PRIOR_KW), 4),
        "source": "sun_position",
        "clear_sky_basis": basis,
        "quality_records": round(geometry_weight_sum, 2),
        "avg_quality": round(quality_weight_sum / geometry_weight_sum, 3),
        "clipped_excluded": clipped_excluded,
        "forecast_clipped": forecast_clipped,
        "export_capped": export_capped,
        "undampened_records": n_undampened,
        "coverage_kw": round(weighted_est_sum, 2),
    }


def average_slot_pairs(slot_factors: list[float]) -> list[float]:
    """Average 48 half-hour slot factors into 24 hourly values."""
    hourly = []
    for i in range(0, 48, 2):
        a = slot_factors[i] if i < len(slot_factors) else 1.0
        b = slot_factors[i + 1] if i + 1 < len(slot_factors) else 1.0
        hourly.append((a + b) / 2.0)
    return hourly
