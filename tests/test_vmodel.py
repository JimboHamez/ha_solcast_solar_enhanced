"""Learned per-tracker MPP-voltage model (vmodel.py): fit, exclusions, limiting, persistence."""

from __future__ import annotations

import math
import random

import pytest

from custom_components.solcast_solar_enhanced.const import VMODEL_FORGET, VMODEL_MIN_SAMPLES, VMODEL_SHARE_MIN_SLOTS
from custom_components.solcast_solar_enhanced.vmodel import TrackerFit, VoltageModel, _solve, evaluate_slot, features

SLOT = 1800.0
G_FLAT = 900.0
# Issue #109: strings of different lengths on one inverter (East 202 V against North 269 V).
STRING_RATIO = 202.0 / 269.0
UNKNOWN = {"vexcess_s": -1.0, "vratio_p50": -1.0, "vratio_p90": -1.0, "ready": False}


def _true_v(temp: float, g: float) -> float:
    """The array's real MPP voltage: falls with heat, a little with irradiance-driven heating."""
    gk = g / 1000.0
    return 430.0 - 0.8 * temp - 50.0 * gk + 13.0 * math.log(gk)


def _slot(
    start: float,
    *,
    temp: float = 20.0,
    g: float = G_FLAT,
    v_scale: float = 1.0,
    amps: float = 9.0,
    noise: float = 0.002,
    tracker2_scale: float = 1.0,
    scan_every: float | None = None,
    cloudy: bool = False,
    seed: int = 1,
):
    """Two identical trackers sampled at 1 Hz for one slot."""
    rng = random.Random(seed)
    v1, i1, v2, i2 = [], [], [], []
    for k in range(int(SLOT)):
        t = start + k
        v = _true_v(temp, g) * v_scale * (1 + rng.gauss(0, noise))
        if scan_every and k % int(scan_every) < 3:
            v *= 0.8  # An MPPT scan sweeps the voltage down for a few seconds.
        a = amps * (0.4 if cloudy and (k // 60) % 2 else 1.0)
        v1.append((t, v))
        i1.append((t, a))
        v2.append((t, v * tracker2_scale))
        i2.append((t, a))
    return {"t1": (v1, i1), "t2": (v2, i2)}


def _trained(seed: int = 0, tracker2_scale: float = 1.0) -> VoltageModel:
    """A model trained on varied healthy slots until ready (bounded, so a model that never trains fails)."""
    model = VoltageModel()
    rng = random.Random(seed)
    start = 0.0
    for _ in range(500):
        if model.ready("t1") and model.ready("t2"):
            return model
        temp, g = rng.uniform(5, 35), rng.uniform(300, 1050)
        trackers = _slot(start, temp=temp, g=g, seed=int(start), tracker2_scale=tracker2_scale)
        evaluate_slot(start, start + SLOT, trackers, temp, lambda _t, g=g: g, model)
        start += SLOT
    raise AssertionError("the model never became ready")


def _run(model, start=10_000_000.0, temp=20.0, g=G_FLAT, export=None, limit=0.0, **slot_kw):
    return evaluate_slot(
        start, start + SLOT, _slot(start, temp=temp, g=g, **slot_kw), temp, lambda _t: g, model, export, limit
    )


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def test_the_fit_recovers_the_true_voltage_across_temperature_and_irradiance():
    model = _trained()
    for temp, g in ((8, 400), (20, 900), (34, 1000), (15, 600)):
        predicted = model.fit("t1").predict(features(temp, g))
        assert predicted == pytest.approx(_true_v(temp, g), rel=0.003)


def test_not_ready_until_enough_samples_then_everything_unknown_before():
    """Two trackers: the first slots only learn each tracker's share (the shading reference)."""
    model = VoltageModel()
    for k in range(VMODEL_SHARE_MIN_SLOTS):
        assert _run(model, start=k * SLOT, seed=k) == UNKNOWN
        assert model.fit("t1").n == 0
    assert _run(model, start=VMODEL_SHARE_MIN_SLOTS * SLOT) == UNKNOWN
    assert 0 < model.fit("t1").n < VMODEL_MIN_SAMPLES


def test_a_single_tracker_trains_from_its_first_slot():
    """No other tracker to be shaded against, so nothing to wait for."""
    model = VoltageModel()
    trackers = {"t1": _slot(0.0)["t1"]}
    evaluate_slot(0.0, SLOT, trackers, 20.0, lambda _t: G_FLAT, model)
    assert model.fit("t1").n > 0


def test_strings_of_different_lengths_train_and_judge():
    """Issue #109: raw volts 25% apart failed the bypass test on every sample, so n stayed 0 forever."""
    model = _trained(tracker2_scale=STRING_RATIO)
    healthy = _run(model, tracker2_scale=STRING_RATIO)
    assert healthy["ready"] and healthy["vexcess_s"] == 0.0
    assert healthy["vratio_p50"] == pytest.approx(1.0, abs=0.005)
    limited = _run(model, start=11_000_000.0, v_scale=1.08, amps=7.0, tracker2_scale=STRING_RATIO)
    assert limited["vexcess_s"] == pytest.approx(SLOT, abs=60)


def test_bypass_shading_before_the_model_is_ready_is_judged_against_each_trackers_own_share():
    model = VoltageModel()
    for k in range(VMODEL_SHARE_MIN_SLOTS + 1):
        _run(model, start=k * SLOT, seed=k, tracker2_scale=STRING_RATIO)
    n0 = model.fit("t1").n
    assert n0 > 0
    _run(model, start=100 * SLOT, tracker2_scale=STRING_RATIO * 0.85)  # 15% below its own normal.
    assert model.fit("t1").n == pytest.approx(n0)
    _run(model, start=101 * SLOT, tracker2_scale=STRING_RATIO)  # The shaded slot did not shift the shares.
    assert model.fit("t1").n > n0


def test_bypass_shading_on_unequal_strings_is_judged_against_the_model():
    model = _trained(tracker2_scale=STRING_RATIO)
    n0 = model.fit("t1").n
    r = _run(model, v_scale=1.08, tracker2_scale=STRING_RATIO * 0.85)
    assert r["vexcess_s"] == 0.0 and r["vratio_p50"] == -1.0
    assert model.fit("t1").n == pytest.approx(n0)


def test_a_shaded_first_slot_does_not_poison_the_shares():
    """The shares are a median: one whole slot with a string shaded must not set them."""
    model = VoltageModel()
    _run(model, start=0.0, tracker2_scale=0.85)
    for k in range(1, VMODEL_SHARE_MIN_SLOTS):
        _run(model, start=k * SLOT, seed=k)
    n0 = model.fit("t1").n
    _run(model, start=10 * SLOT, seed=10)
    assert model.fit("t1").n > n0


def test_solve_matches_a_known_system_and_refuses_a_singular_one():
    assert _solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0]) == pytest.approx([0.8, 1.4])
    assert _solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]) is None


# ---------------------------------------------------------------------------
# Limiting
# ---------------------------------------------------------------------------


def test_a_healthy_slot_sits_on_the_model():
    r = _run(_trained())
    assert r["ready"] and r["vexcess_s"] == 0.0
    assert r["vratio_p50"] == pytest.approx(1.0, abs=0.005)


def test_a_limited_slot_sits_above_the_model_on_every_tracker():
    r = _run(_trained(), v_scale=1.08, amps=7.0)
    assert r["vexcess_s"] == pytest.approx(SLOT, abs=60)
    assert r["vratio_p50"] == pytest.approx(1.08, abs=0.005)


def test_a_cool_afternoon_is_not_limiting():
    """The fixed-reference rule failed here: cool panels sit 4-5% above a hot midday reference."""
    model = _trained()
    hot = model.fit("t1").predict(features(34, 1000))
    cool = _true_v(12, 600)
    assert cool / hot > 1.04  # What a fixed midday reference would have seen.
    assert _run(model, temp=12, g=600)["vexcess_s"] == 0.0


def test_limiting_needs_every_tracker_above_the_model():
    """An inverter limits every tracker at once. Here tracker 2 is 7% up but tracker 1
    only 3%, inside the bypass-spread allowance, so it is judged, and is not limiting."""
    r = _run(_trained(), v_scale=1.03, tracker2_scale=1.07 / 1.03)
    assert r["vexcess_s"] == 0.0
    assert r["vratio_p50"] == pytest.approx(1.03, abs=0.005)  # The lower tracker sets the ratio.


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_export", [False, True])
def test_limited_samples_are_never_trained_on(with_export):
    """With export headroom (a generation cap rather than an export limit) only the
    limiting check itself keeps these samples out; without a series the outlier rule
    would too, which is why both cases are run."""
    model = _trained()
    before = model.fit("t1").predict(features(20, G_FLAT))
    export = {"export": [(0.0, 1.0)], "limit": 5.0} if with_export else {}
    for k in range(20):
        _run(model, start=20_000_000.0 + k * SLOT, v_scale=1.08, amps=7.0, seed=k, **export)
    assert model.fit("t1").predict(features(20, G_FLAT)) == pytest.approx(before, rel=1e-6)


def test_export_near_the_limit_keeps_samples_out_of_training():
    model = _trained()
    n0 = model.fit("t1").n
    _run(model, export=[(0.0, 4.6)], limit=5.0)  # Above 0.8 x limit for the whole slot.
    assert model.fit("t1").n == pytest.approx(n0)
    _run(model, start=30_000_000.0, export=[(0.0, 1.0)], limit=5.0)
    assert model.fit("t1").n > n0


def test_without_an_export_series_samples_far_above_the_fit_are_not_trained_on():
    """+4% is under the +5% limiting threshold, so only the outlier rule keeps it out."""
    model = _trained()
    n0 = model.fit("t1").n
    _run(model, v_scale=1.04, noise=0.0005)
    assert model.fit("t1").n == pytest.approx(n0)
    # The same slot with an export series showing headroom is trusted and trained on.
    _run(model, start=40_000_000.0, v_scale=1.04, noise=0.0005, export=[(0.0, 1.0)], limit=5.0)
    assert model.fit("t1").n > n0


def test_bypass_shaded_samples_are_neither_judged_nor_trained_on():
    model = _trained()
    n0 = model.fit("t1").n
    r = _run(model, v_scale=1.08, tracker2_scale=0.85)  # One tracker 15% low: bypass diodes.
    assert r["vexcess_s"] == 0.0 and r["vratio_p50"] == -1.0
    assert model.fit("t1").n == pytest.approx(n0)


def test_mppt_scan_dips_are_neither_judged_nor_trained_on():
    model = _trained()
    n0 = model.fit("t1").n
    clean = _run(model, start=50_000_000.0)
    n1 = model.fit("t1").n
    scanned = _run(model, start=60_000_000.0, scan_every=60)
    n2 = model.fit("t1").n
    assert scanned["vratio_p50"] == pytest.approx(clean["vratio_p50"], abs=0.003)
    # The dips (and the samples around them, judged against the rolling max) are skipped.
    assert (n2 - n1) < (n1 - n0)


def test_unsteady_current_is_not_judged():
    """A passing cloud is not a steady state; the slot reads no evidence rather than a false one."""
    r = _run(_trained(), cloudy=True, v_scale=1.08)
    assert r["vexcess_s"] == 0.0 and r["vratio_p50"] == -1.0


def test_low_irradiance_is_neither_judged_nor_trained_on():
    """Below VMODEL_MIN_G the fit extrapolates (31 Oct 19:00 read +5% at 9 W/m²)."""
    model = _trained()
    n0 = model.fit("t1").n
    r = _run(model, g=60.0, v_scale=1.08)
    assert r["vexcess_s"] == 0.0 and r["vratio_p50"] == -1.0
    assert model.fit("t1").n == pytest.approx(n0)


def test_a_tracker_without_samples_makes_the_slot_unknown():
    model = _trained()
    trackers = _slot(70_000_000.0)
    trackers["t2"] = ([], [])
    r = evaluate_slot(70_000_000.0, 70_000_000.0 + SLOT, trackers, 20.0, lambda _t: G_FLAT, model)
    assert r["ready"] is False and r["vexcess_s"] == -1.0


# ---------------------------------------------------------------------------
# Forgetting and persistence
# ---------------------------------------------------------------------------


def test_decay_fades_old_evidence_without_moving_the_fit():
    model = _trained()
    fit = model.fit("t1")
    n0, coef = fit.n, list(fit.coef)
    model.decay(VMODEL_FORGET)
    assert fit.n == pytest.approx(n0 * VMODEL_FORGET)
    fit.solve(min_samples=0)
    assert fit.coef == pytest.approx(coef, rel=1e-9)  # Uniform decay leaves the solution unchanged.


def test_the_model_survives_a_restart():
    model = _trained()
    restored = VoltageModel.from_dict(model.to_dict())
    x = features(20, G_FLAT)
    assert restored.ready("t1") and restored.fit("t1").predict(x) == pytest.approx(model.fit("t1").predict(x))
    assert restored.fit("t1").n == pytest.approx(model.fit("t1").n)
    assert restored.fit("t1").sigma == pytest.approx(model.fit("t1").sigma)  # The outlier rule needs it.


@pytest.mark.parametrize("data", [None, [], {"version": 2}, {"version": 1, "trackers": {"t1": {"xtx": [[1]]}}}])
def test_an_unreadable_store_starts_fresh(data):
    model = VoltageModel.from_dict(data)
    assert not model.ready("t1")
    assert isinstance(model.fit("t1"), TrackerFit) and model.fit("t1").n == 0
