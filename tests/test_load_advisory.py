"""Tests for the short-horizon load-scheduling advisory (item 3)."""
from __future__ import annotations

from custom_components.solcast_solar_enhanced.load_advisory import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    compute_confidence,
)


def test_confidence_empty_is_unknown():
    r = compute_confidence([])
    assert r["confidence"] is None
    assert r["rating"] == "unknown"
    assert r["n_slots"] == 0


def test_confidence_no_daylight_is_unknown():
    # All estimates zero (night) ⇒ nothing usable.
    r = compute_confidence([(0.0, 0.0), (0.0, 0.0)])
    assert r["rating"] == "unknown"


def test_confidence_perfect_tracking_is_max():
    r = compute_confidence([(3.0, 3.0)] * 4)
    assert r["confidence"] == 100
    assert r["rating"] == "high"
    assert r["recent_bias"] == 1.0
    assert r["n_slots"] == 4


def test_confidence_high_within_band():
    # ~10% under-production ⇒ still high trust.
    r = compute_confidence([(2.7, 3.0)] * 3)
    assert r["confidence"] >= CONFIDENCE_HIGH
    assert r["rating"] == "high"


def test_confidence_medium_on_moderate_divergence():
    # ~30% over-production vs forecast ⇒ medium.
    r = compute_confidence([(3.9, 3.0)] * 3)
    assert CONFIDENCE_MEDIUM <= r["confidence"] < CONFIDENCE_HIGH
    assert r["rating"] == "medium"


def test_confidence_low_on_large_divergence():
    # Output less than half the forecast ⇒ low trust.
    r = compute_confidence([(1.0, 3.0)] * 3)
    assert r["confidence"] < CONFIDENCE_MEDIUM
    assert r["rating"] == "low"


def test_confidence_energy_weighted_bias():
    # Σactual / Σestimate, not a mean of ratios: a big well-tracked slot dominates a
    # tiny noisy one.
    r = compute_confidence([(10.0, 10.0), (0.1, 0.5)])
    assert r["recent_bias"] == round(10.1 / 10.5, 3)


def test_confidence_zero_output_is_low_not_crash():
    r = compute_confidence([(0.0, 3.0)] * 3)
    assert r["confidence"] == 0
    assert r["rating"] == "low"
