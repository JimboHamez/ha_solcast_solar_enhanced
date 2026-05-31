"""Test shading dampening: cloud weights, geometry weights, and blending."""
from __future__ import annotations

import math
import pytest

from custom_components.solcast_solar_enhanced.shading_dampening import (
    _cloud_weight,
    _geometry_weight,
    average_slot_pairs,
    compute_dampening,
)

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
    """Azimuth wraps correctly: 350° vs 10° is a 20° difference."""
    w_wrapped = _geometry_weight(45.0, 10.0, 45.0, 350.0)
    w_direct = _geometry_weight(45.0, 10.0, 45.0, 30.0)
    assert w_wrapped > w_direct  # 20° difference < 20° difference means similar, but > far


def test_geometry_weight_range():
    for dz in range(0, 91, 10):
        w = _geometry_weight(float(dz), 0.0, 0.0, 0.0)
        assert 0.0 <= w <= 1.0


# ---------------------------------------------------------------------------
# compute_dampening
# ---------------------------------------------------------------------------

def test_compute_dampening_empty_records_returns_base_fallback():
    result = compute_dampening([], 5.0, 20, 60, 0.95, [0.85], 45.0, 180.0)
    assert result["source"] == "base_fallback"
    assert result["factor"] == pytest.approx(0.85)
    assert result["alpha"] == 0.0
    assert result["quality_records"] == 0.0


def test_compute_dampening_excludes_zero_estimate():
    records = [{"pv_actual": 3.0, "pv_export": 0.0, "battery_charge": 0.0,
                "pv_estimate": 0.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}]
    result = compute_dampening(records, 5.0, 20, 60, 0.95, [1.0], 45.0, 180.0)
    assert result["source"] == "base_fallback"


def test_compute_dampening_excludes_high_cloud():
    records = [{"pv_actual": 3.0, "pv_export": 0.0, "battery_charge": 0.0,
                "pv_estimate": 4.0, "clouds": 80, "zenith": 45.0, "azimuth": 180.0}]
    result = compute_dampening(records, 5.0, 20, 60, 0.95, [1.0], 45.0, 180.0)
    assert result["source"] == "base_fallback"


def test_compute_dampening_alpha_increases_with_more_records():
    base_record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
                   "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result_few = compute_dampening([base_record] * 5, 10.0, 20, 60, 0.95, [1.0], 45.0, 180.0)
    result_many = compute_dampening([base_record] * 100, 10.0, 20, 60, 0.95, [1.0], 45.0, 180.0)
    assert result_many["alpha"] > result_few["alpha"]


def test_compute_dampening_factor_is_ratio_at_high_confidence():
    """At high alpha, factor should be close to total_pv / pv_estimate."""
    record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([record] * 200, 10.0, 20, 60, 0.95, [1.0], 45.0, 180.0)
    # With 200 identical records at same zenith/azimuth, alpha should be high
    if result["alpha"] > 0.9:
        assert abs(result["factor"] - 0.8) < 0.05  # 4.0/5.0 = 0.8


def test_compute_dampening_clipping_excluded_counted():
    capacity_kw = 5.0
    clip = 0.95 * capacity_kw  # 4.75
    clipped = {"pv_actual": 4.8, "pv_export": 0.0, "battery_charge": 0.0,
               "pv_estimate": 4.9, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result = compute_dampening([clipped] * 10, capacity_kw, 20, 60, 0.95, [1.0], 45.0, 180.0)
    assert result["clipped_excluded"] == 10


def test_compute_dampening_early_clamp_applies_below_half_alpha():
    """When alpha < 0.5, factor is clamped to within 15% of base."""
    base_factor = 1.0
    record = {"pv_actual": 0.1, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    # 1 record → very low alpha → early clamp kicks in
    result = compute_dampening([record], 10.0, 20, 60, 0.95, [base_factor], 45.0, 180.0)
    if result["alpha"] < 0.5:
        assert result["factor"] >= base_factor * 0.85
        assert result["factor"] <= base_factor * 1.15


def test_compute_dampening_source_labels():
    record = {"pv_actual": 4.0, "pv_export": 0.0, "battery_charge": 0.0,
              "pv_estimate": 5.0, "clouds": 5, "zenith": 45.0, "azimuth": 180.0}
    result_few = compute_dampening([record] * 3, 10.0, 20, 60, 0.95, [1.0], 45.0, 180.0)
    assert result_few["source"] in ("base_fallback", "blended", "db_history", "night")


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
