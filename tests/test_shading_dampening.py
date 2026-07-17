"""Test shading dampening: cloud weights, geometry weights, and blending."""
from __future__ import annotations

import math
import pytest

from custom_components.solcast_solar_enhanced.pv_tuning import clearsky_ghi
from custom_components.solcast_solar_enhanced.shading_dampening import (
    _cloud_weight,
    _geometry_weight,
    _kt_weight,
    average_slot_pairs,
    compute_dampening,
)


def _kt_record(kt: float, zenith: float, azimuth: float, ratio: float) -> dict:
    """Build a record whose stored GHI yields the target clearness index Kt."""
    return {
        "pv_actual": ratio,
        "pv_estimate": 1.0,
        "pv_export": 0.0,
        "zenith": zenith,
        "azimuth": azimuth,
        "ghi": kt * clearsky_ghi(zenith),
        # Deliberately overcast cloud value: the Kt path must ignore it.
        "clouds": 100,
    }

# ---------------------------------------------------------------------------
# _cloud_weight
# ---------------------------------------------------------------------------

def test_cloud_weight_clear_sky():
    assert _cloud_weight(5, 20, 60) == 1.0


def test_cloud_weight_at_threshold_boundary():
    assert _cloud_weight(20, 20, 60) == 0.6


def test_cloud_weight_mid_band():
    assert _cloud_weight(25, 20, 60) == 0.6


def test_cloud_weight_upper_band():
    assert _cloud_weight(45, 20, 60) == 0.3


def test_cloud_weight_at_max_include():
    assert _cloud_weight(60, 20, 60) == 0.3


def test_cloud_weight_above_max():
    assert _cloud_weight(61, 20, 60) == 0.0


def test_cloud_weight_zero_clouds():
    assert _cloud_weight(0, 20, 60) == 1.0


# ---------------------------------------------------------------------------
# _geometry_weight
# ---------------------------------------------------------------------------

def test_geometry_weight_exact_match():
    w = _geometry_weight(45.0, 180.0, 45.0, 180.0)
    assert abs(w - 1.0) < 1e-9


def test_geometry_weight_far_zenith():
    """Large zenith difference → small weight."""
    w = _geometry_weight(0.0, 180.0, 80.0, 180.0)
    assert w < 0.01


def test_geometry_weight_far_azimuth():
    """Large azimuth difference → small weight."""
    w = _geometry_weight(45.0, 0.0, 45.0, 180.0)
    assert w < 0.01


def test_geometry_weight_azimuth_wrap():
    """rec=355°, target=5° wraps to a 10° difference, giving a much higher weight than 40°."""
    w_wrapped = _geometry_weight(45.0, 355.0, 45.0, 5.0)   # wrapped diff = 10°
    w_far = _geometry_weight(45.0, 45.0, 45.0, 5.0)        # actual diff  = 40°
    assert w_wrapped > w_far


def test_geometry_weight_range():
    for dz in range(0, 91, 10):
        w = _geometry_weight(float(dz), 0.0, 0.0, 0.0)
        assert 0.0 <= w <= 1.0


# ---------------------------------------------------------------------------
# compute_dampening
# ---------------------------------------------------------------------------

def test_compute_dampening_empty_records_returns_no_data():
    result = compute_dampening([], 5.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["source"] == "no_data"
    assert result["factor"] == pytest.approx(1.0)
    assert result["alpha"] == 0.0
    assert result["quality_records"] == 0.0


def test_compute_dampening_excludes_zero_estimate():
    records = [{"pv_actual": 3.0, "pv_export": 0.0, "battery_charge": 0.0,
                "pv_estimate": 0.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}]
    result = compute_dampening(records, 5.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["source"] == "no_data"


def test_compute_dampening_keeps_zero_cloud_records():
    """A genuine 0% cloud reading (clearest sky — the best data for a shading
    ratio) must be kept, not coerced to overcast and dropped.

    Regression: clouds were read as `int(r.get("clouds", 100) or 100)`, so a
    falsy 0 became 100, `_cloud_weight` scored it in its zero band, and every
    clear-sky record was excluded → spurious 'no_data'.
    """
    record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 0, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([record] * 50, 10.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["source"] != "no_data", "0% cloud records were wrongly excluded"
    assert result["quality_records"] > 0.0


def test_compute_dampening_missing_cloud_treated_overcast():
    """A missing/None cloud value still defaults to overcast (excluded)."""
    record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": None, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([record] * 50, 10.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["source"] == "no_data"


def test_compute_dampening_excludes_no_owm_sentinel():
    """The no-OWM storage sentinel (clouds=100) is excluded → stays neutral.

    Without an OWM source the coordinator stores clouds=100, so dampening finds
    no usable records and reports 'no_data' (neutral 1.0, nothing pushed).
    """
    record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 100, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([record] * 50, 10.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["source"] == "no_data"
    assert result["factor"] == pytest.approx(1.0)


def test_compute_dampening_excludes_high_cloud():
    records = [{"pv_actual": 3.0, "pv_export": 0.0, "battery_charge": 0.0,
                "pv_estimate": 4.0, "clouds": 80, "zenith": 45.0, "azimuth": 180.0}]
    result = compute_dampening(records, 5.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["source"] == "no_data"


def test_compute_dampening_alpha_increases_with_more_records():
    base_record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
                   "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result_few = compute_dampening([base_record] * 5, 10.0, 20, 60, 0.95, 45.0, 180.0)
    result_many = compute_dampening([base_record] * 100, 10.0, 20, 60, 0.95, 45.0, 180.0)
    assert result_many["alpha"] > result_few["alpha"]


def test_compute_dampening_factor_is_ratio_at_high_confidence():
    """At high alpha, factor should be close to total_pv / pv_estimate."""
    record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([record] * 200, 10.0, 20, 60, 0.95, 45.0, 180.0)
    # With 200 identical records at same zenith/azimuth, alpha should be high
    if result["alpha"] > 0.9:
        assert abs(result["factor"] - 0.8) < 0.05  # 4.0/5.0 = 0.8


def test_compute_dampening_clipping_excluded_counted():
    capacity_kw = 5.0
    clip = 0.95 * capacity_kw  # 4.75
    clipped = {"pv_actual": 4.8, "pv_export": 0.0, "battery_charge": 0.0,
               "pv_estimate": 4.9, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([clipped] * 10, capacity_kw, 20, 60, 0.95, 45.0, 180.0)
    assert result["clipped_excluded"] == 10


def test_compute_dampening_clip_forecast_recovers_curtailed_ratio():
    """Export-curtailed clear-sky record: raw ratio reads low (curtailment looks
    like shading); clipping the forecast to the achievable ceiling recovers it
    toward a neutral ~1.0. Mirrors the real signature (export pegged at ~5 kW,
    actual held below a 7.6 kW estimate on an 8 kW array)."""
    rec = {"pv_actual": 5.9, "pv_export": 4.98, "battery_charge": 0.0,
           "pv_estimate": 7.62, "clouds": 5, "zenith": 30.0, "azimuth": 0.0}
    # export disabled → the spurious penalty stands
    raw = compute_dampening([rec] * 200, 8.0, 20, 60, 0.95, 30.0, 0.0)
    # export limit known → forecast clipped to load+limit, penalty removed
    clipped = compute_dampening([rec] * 200, 8.0, 20, 60, 0.95, 30.0, 0.0,
                                export_limit_kw=5.0)
    assert raw["forecast_clipped"] == 0
    assert clipped["forecast_clipped"] == 200
    assert clipped["factor"] > raw["factor"]
    if clipped["alpha"] > 0.9:
        assert clipped["factor"] == pytest.approx(0.997, abs=0.01)  # 5.9/5.92
        assert raw["factor"] == pytest.approx(0.774, abs=0.01)      # 5.9/7.62


def test_compute_dampening_clip_forecast_noop_with_export_headroom():
    """Not curtailed (export well below the limit): forecast is not clipped, so
    genuine shading is preserved and the result matches the export-disabled case."""
    rec = {"pv_actual": 3.0, "pv_export": 1.0, "battery_charge": 0.0,
           "pv_estimate": 3.1, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    raw = compute_dampening([rec] * 200, 8.0, 20, 60, 0.95, 45.0, 180.0)
    with_limit = compute_dampening([rec] * 200, 8.0, 20, 60, 0.95, 45.0, 180.0,
                                   export_limit_kw=5.0)
    assert with_limit["forecast_clipped"] == 0
    assert with_limit["factor"] == pytest.approx(raw["factor"])


def test_compute_dampening_clip_forecast_never_exceeds_unity():
    """Measured export slightly over the configured limit must not push the ratio
    above 1.0 — the clip floors the effective estimate at the delivered output."""
    rec = {"pv_actual": 5.0, "pv_export": 5.1, "battery_charge": 0.0,
           "pv_estimate": 7.0, "clouds": 5, "zenith": 30.0, "azimuth": 0.0}
    result = compute_dampening([rec] * 200, 8.0, 20, 60, 0.95, 30.0, 0.0,
                               export_limit_kw=5.0)
    assert result["forecast_clipped"] == 200
    assert result["factor"] <= 1.0 + 1e-6


def test_compute_dampening_early_clamp_applies_below_half_alpha():
    """When alpha < 0.5, factor is clamped to within 15% of the neutral 1.0 anchor."""
    neutral = 1.0
    record = {"pv_actual": 0.1, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    # 1 record → very low alpha → early clamp kicks in
    result = compute_dampening([record], 10.0, 20, 60, 0.95, 45.0, 180.0)
    if result["alpha"] < 0.5:
        assert result["factor"] >= neutral * 0.85
        assert result["factor"] <= neutral * 1.15


def test_compute_dampening_source_labels():
    record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result_few = compute_dampening([record] * 3, 10.0, 20, 60, 0.95, 45.0, 180.0)
    assert result_few["source"] in ("no_data", "db_blended", "db_history", "night")


# ---------------------------------------------------------------------------
# average_slot_pairs
# ---------------------------------------------------------------------------

def test_average_slot_pairs_basic():
    slots = [1.0, 0.8] * 24  # 48 alternating slots
    hourly = average_slot_pairs(slots)
    assert len(hourly) == 24
    for h in hourly:
        assert abs(h - 0.9) < 1e-9


def test_average_slot_pairs_uniform():
    slots = [0.75] * 48
    hourly = average_slot_pairs(slots)
    assert all(abs(h - 0.75) < 1e-9 for h in hourly)


def test_average_slot_pairs_short_list():
    """Handles lists shorter than 48 without raising."""
    slots = [1.0, 0.5]
    hourly = average_slot_pairs(slots)
    assert len(hourly) == 24
    assert abs(hourly[0] - 0.75) < 1e-9
    assert abs(hourly[1] - 1.0) < 1e-9  # missing slot defaults to 1.0


# ---------------------------------------------------------------------------
# _kt_weight (Kt clear-sky-index quality bands)
# ---------------------------------------------------------------------------

def test_kt_weight_clear_sky():
    assert _kt_weight(0.90, 0.75) == 1.0


def test_kt_weight_at_threshold():
    assert _kt_weight(0.75, 0.75) == 1.0


def test_kt_weight_mid_band():
    assert _kt_weight(0.65, 0.75) == 0.6


def test_kt_weight_low_band():
    assert _kt_weight(0.50, 0.75) == 0.3


def test_kt_weight_overcast_excluded():
    assert _kt_weight(0.30, 0.75) == 0.0


# ---------------------------------------------------------------------------
# compute_dampening — Kt clear-sky basis
# ---------------------------------------------------------------------------

def test_compute_dampening_cloud_basis_by_default():
    """No kt_threshold ⇒ legacy cloud basis is reported and used."""
    record = {"pv_actual": 0.8, "pv_estimate": 1.0, "clouds": 5, "zenith": 30.0, "azimuth": 180.0}
    result = compute_dampening([record] * 50, 10.0, 20, 60, 0.95, 30.0, 180.0)
    assert result["clear_sky_basis"] == "cloud"


def test_compute_dampening_kt_basis_reported():
    record = _kt_record(kt=0.9, zenith=30.0, azimuth=180.0, ratio=0.8)
    result = compute_dampening([record] * 50, 10.0, 20, 60, 0.95, 30.0, 180.0, kt_threshold=0.75)
    assert result["clear_sky_basis"] == "kt"


def test_compute_dampening_kt_keeps_clear_record_despite_overcast_cloud():
    """The decisive behaviour: clouds=100 but Kt clear ⇒ record is KEPT and weighted.

    Under the legacy cloud bands clouds=100 is excluded (>max_include), leaving
    neutral no_data. With the Kt basis the measured-clear record drives the factor.
    """
    record = _kt_record(kt=0.9, zenith=30.0, azimuth=180.0, ratio=0.8)
    cloud_mode = compute_dampening([record] * 200, 10.0, 20, 60, 0.95, 30.0, 180.0)
    kt_mode = compute_dampening([record] * 200, 10.0, 20, 60, 0.95, 30.0, 180.0, kt_threshold=0.75)
    assert cloud_mode["source"] == "no_data"  # overcast cloud ⇒ excluded
    assert kt_mode["source"] != "no_data"  # Kt says clear ⇒ used
    assert abs(kt_mode["factor"] - 0.8) < 0.05


def test_compute_dampening_kt_excludes_overcast():
    """Low Kt (genuinely overcast) ⇒ excluded ⇒ stays neutral."""
    record = _kt_record(kt=0.30, zenith=30.0, azimuth=180.0, ratio=0.8)
    result = compute_dampening([record] * 200, 10.0, 20, 60, 0.95, 30.0, 180.0, kt_threshold=0.75)
    assert result["source"] == "no_data"
    assert result["factor"] == 1.0


def test_compute_dampening_kt_drops_near_horizon_record():
    """Near-horizon sun ⇒ clear-sky reference below floor ⇒ no Kt ⇒ dropped."""
    # zenith 88° ⇒ clearsky_ghi ≈ 7 W/m² < KT_GHI_CS_FLOOR (40), so Kt is undefined.
    record = _kt_record(kt=0.9, zenith=88.0, azimuth=180.0, ratio=0.8)
    assert clearsky_ghi(88.0) < 40.0
    result = compute_dampening([record] * 200, 10.0, 20, 60, 0.95, 88.0, 180.0, kt_threshold=0.75)
    assert result["source"] == "no_data"


# ---------------------------------------------------------------------------
# Undampened denominator (issue #50)
# ---------------------------------------------------------------------------

def _loop_record(pv_actual: float, dampened: float, undampened: float) -> dict:
    """A clear-sky record whose dampened and pre-dampening forecasts differ."""
    return {
        "pv_actual": pv_actual,
        "pv_estimate": dampened,
        "pv_estimate_undampened": undampened,
        "pv_export": 0.0,
        "battery_charge": 0.0,
        "clouds": 0,
        "zenith": 45.0,
        "azimuth": 180.0,
    }


def test_compute_dampening_prefers_undampened_denominator():
    """The ratio must divide by the base's pre-dampening forecast.

    Reading `pv_estimate` back out of the base's already-dampened detailedForecast
    measures output against a forecast our own pushed factors produced, which biases
    the ratio toward 1.0 and hides shading (issue #50).
    """
    # True shading is 4.0/10.0 = 0.4. A previously-pushed 0.5 factor left the
    # dampened forecast at 5.0, which would read as a much milder 4.0/5.0 = 0.8.
    records = [_loop_record(pv_actual=4.0, dampened=5.0, undampened=10.0)] * 400
    result = compute_dampening(records, 20.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["alpha"] > 0.95, "expected a converged alpha for this record count"
    assert result["factor"] == pytest.approx(0.4, abs=0.02)
    assert result["undampened_records"] == 400


def test_compute_dampening_falls_back_to_dampened_estimate():
    """Rows predating 1.10.0b6 store 0, so the dampened figure is still used."""
    records = [_loop_record(pv_actual=4.0, dampened=5.0, undampened=0.0)] * 400
    result = compute_dampening(records, 20.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["factor"] == pytest.approx(0.8, abs=0.02)
    assert result["undampened_records"] == 0


def test_compute_dampening_mixed_undampened_availability():
    """A window spanning the upgrade uses each record's best available denominator."""
    records = [_loop_record(4.0, 5.0, 10.0)] * 200 + [_loop_record(4.0, 5.0, 0.0)] * 200
    result = compute_dampening(records, 20.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["undampened_records"] == 200
    # Half the records read 0.4, half read the biased 0.8 ⇒ between the two.
    assert 0.4 < result["factor"] < 0.8


def test_compute_dampening_undampened_absent_key_is_safe():
    """A record dict without the key at all must not raise."""
    record = {"pv_actual": 4.0, "pv_estimate": 5.0, "pv_export": 0.0,
              "battery_charge": 0.0, "clouds": 0, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([record] * 50, 20.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["undampened_records"] == 0
    assert result["source"] != "no_data"


def test_compute_dampening_undampened_count_excludes_filtered_records():
    """The count reports records actually weighted in, not merely seen.

    An overcast record carries an undampened forecast but is dropped by the
    clear-sky gate, so it must not inflate the diagnostic.
    """
    clear = _loop_record(4.0, 5.0, 10.0)
    overcast = dict(_loop_record(4.0, 5.0, 10.0), clouds=100)
    result = compute_dampening([clear] * 30 + [overcast] * 70, 20.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["undampened_records"] == 30


# ---------------------------------------------------------------------------
# Energy-weighted aggregate ratio (issue #52)
# ---------------------------------------------------------------------------

def _ratio_record(pv_actual: float, pv_estimate: float) -> dict:
    """A clear-sky record with an explicit actual/estimate pair."""
    return {
        "pv_actual": pv_actual,
        "pv_estimate": pv_estimate,
        "pv_export": 0.0,
        "battery_charge": 0.0,
        "clouds": 0,
        "zenith": 45.0,
        "azimuth": 180.0,
    }


def test_db_factor_is_energy_aggregate_not_mean_of_ratios():
    """One tiny-denominator record must not swamp many honest ones.

    A slot forecast at 0.2 kW that produced 1.0 kW is a 5.0 ratio. Under the old
    weighted-mean-of-ratios it dragged the hour to 1.22 — above 1.0, which the push
    clamps to "no dampening", silently discarding the real 20% shading the other
    records measured. Σ(actual)/Σ(estimate) bounds its influence to its own energy.
    """
    records = [_ratio_record(0.8, 1.0)] * 360 + [_ratio_record(1.0, 0.2)] * 40
    result = compute_dampening(records, 20.0, 20, 60, 0.95, 45.0, 180.0)
    # Σa/Σe = (360·0.8 + 40·1.0) / (360·1.0 + 40·0.2) = 328/368
    assert result["alpha"] > 0.95
    assert result["factor"] == pytest.approx(328 / 368, abs=0.01)
    assert result["factor"] < 1.0, "mean-of-ratios would have exceeded 1.0 here"


def test_bad_forecast_poll_cannot_cancel_an_hour():
    """A clear-sky Solcast poll failure carries full quality weight; it must not
    erase the shading every other record agrees on."""
    honest = [_ratio_record(0.7, 1.0)] * 380          # 30% shading
    collapsed = [_ratio_record(4.0, 1.0)] * 20        # forecast collapsed to 1 kW
    result = compute_dampening(honest + collapsed, 20.0, 20, 60, 0.95, 45.0, 180.0)
    # Σa/Σe = (380·0.7 + 20·4.0) / 400 = 346/400 = 0.865 — still pulled up, but
    # proportionally, and the shading survives rather than being clamped away.
    assert result["factor"] == pytest.approx(0.865, abs=0.01)
    assert result["factor"] < 1.0


def test_db_factor_matches_simple_ratio_for_uniform_records():
    """With identical records the aggregate must equal the plain ratio (no drift)."""
    result = compute_dampening([_ratio_record(3.0, 4.0)] * 400, 20.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["factor"] == pytest.approx(0.75, abs=0.01)


def test_zero_weighted_estimate_is_no_data():
    """Records whose forecasts all round to ~0 carry no shading signal."""
    records = [_ratio_record(0.5, 1e-12)] * 100
    result = compute_dampening(records, 20.0, 20, 60, 0.95, 45.0, 180.0)
    assert result["source"] == "no_data"
    assert result["factor"] == pytest.approx(1.0)


def test_average_slot_pairs_preserves_hour_identity():
    """Hour i must come from slots 2i/2i+1 — not reversed, rotated or offset.

    The other tests here use values that are constant across hours, so a shifted or
    reversed mapping would still satisfy them. The base applies damp_factor[i] to the
    i-th local hour, so a mis-mapped curve dampens the wrong part of the day.
    """
    # Slot pair for hour i averages to exactly i, so each hour is distinguishable.
    slots = []
    for hour in range(24):
        slots += [hour - 0.25, hour + 0.25]
    hourly = average_slot_pairs(slots)
    assert hourly == pytest.approx([float(h) for h in range(24)])
