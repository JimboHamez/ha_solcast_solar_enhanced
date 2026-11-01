"""Per-slot gate measures (slot_gate.py): time-weighted, rate-invariant, sky vs shade vs export."""

from __future__ import annotations

import pytest

from custom_components.solcast_solar_enhanced.const import GateFlag
from custom_components.solcast_solar_enhanced.slot_gate import counter_power, dc_power, measure_slot

START, END = 0.0, 1800.0


def _signal(dips: list[tuple[float, float]], base: float = 1000.0, low: float = 400.0):
    """Power as a function of time: ``base``, dropping to ``low`` inside each dip."""
    return lambda t: low if any(a <= t < b for a, b in dips) else base


def _sampled(fn, step: float) -> list[tuple[float, float]]:
    """``fn`` recorded every ``step`` seconds (a held-state series)."""
    n = round((END - START) / step)
    return [(START + k * step, fn(START + k * step)) for k in range(n)]


def _on_change(fn, step: float = 0.1) -> list[tuple[float, float]]:
    """``fn`` recorded only when it changes, as HA records a sensor."""
    out, last = [], None
    for t, v in _sampled(fn, step):
        if v != last:
            out.append((t, v))
            last = v
    return out


def _kinds(*sites, kind="dc"):
    return dict.fromkeys(sites, kind)


# ---------------------------------------------------------------------------
# Time weighting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record", ["10 Hz", "1 Hz", "60 s", "on change"])
def test_the_same_signal_gives_the_same_seconds_at_any_sample_rate(record):
    """Counting samples instead of seconds would weight a fast sensor 600x a slow one."""
    fn = _signal([(600.0, 900.0)])
    series = {"10 Hz": _sampled(fn, 0.1), "1 Hz": _sampled(fn, 1.0), "60 s": _sampled(fn, 60.0), "on change": _on_change(fn)}[record]
    m = measure_slot(START, END, {"a": series, "b": series}, _kinds("a", "b"), shape=None)
    assert m["sky_dip_s"] == pytest.approx(300.0, abs=0.5)
    assert m["sky_dip_runs"] == 1


def test_sample_gap_reports_the_recording_resolution():
    fn = _signal([])
    fast = measure_slot(START, END, {"a": _sampled(fn, 1.0)}, _kinds("a"), shape=None)
    slow = measure_slot(START, END, {"a": _sampled(fn, 120.0)}, _kinds("a"), shape=None)
    assert fast["sample_gap_s"] == 1.0 and not fast["gate_flags"] & GateFlag.COARSE
    assert slow["sample_gap_s"] == 120.0 and slow["gate_flags"] & GateFlag.COARSE


# ---------------------------------------------------------------------------
# Sky versus shade
# ---------------------------------------------------------------------------


def test_a_dip_on_every_array_is_sky_and_on_one_is_shade():
    a = _sampled(_signal([(100.0, 400.0), (1000.0, 1200.0)]), 1.0)
    b = _sampled(_signal([(100.0, 400.0)]), 1.0)
    m = measure_slot(START, END, {"a": a, "b": b}, _kinds("a", "b"), shape=None)
    assert m["sky_dip_s"] == pytest.approx(300.0)
    assert m["shade_dip_s"] == {"a": pytest.approx(200.0), "b": 0.0}
    assert m["gate_flags"] & GateFlag.CROSS_REF


def test_a_single_array_cannot_tell_shade_from_sky():
    a = _sampled(_signal([(100.0, 400.0)]), 1.0)
    m = measure_slot(START, END, {"a": a}, _kinds("a"), shape=None)
    assert m["sky_dip_s"] == pytest.approx(300.0)
    assert m["shade_dip_s"] == {"a": -1.0}  # Unknown, not "no shade".
    assert not m["gate_flags"] & GateFlag.CROSS_REF


def test_separate_dips_are_separate_runs_and_a_blip_is_not_a_run():
    fn = _signal([(100.0, 200.0), (700.0, 900.0), (1500.0, 1505.0)])
    s = _sampled(fn, 1.0)
    m = measure_slot(START, END, {"a": s, "b": s}, _kinds("a", "b"), shape=None)
    assert m["sky_dip_runs"] == 2
    assert m["sky_dip_s"] == pytest.approx(305.0)  # The blip's seconds still count.


def test_the_clear_sky_shape_removes_the_sunrise_ramp():
    """Raw power ramping up reads as a dip early in the slot; divided by the shape it is flat."""
    ramp = [(float(t), 200.0 + t) for t in range(0, 1800)]
    raw = measure_slot(START, END, {"a": ramp, "b": ramp}, _kinds("a", "b"), shape=None)
    shaped = measure_slot(START, END, {"a": ramp, "b": ramp}, _kinds("a", "b"), shape=lambda _site, t: 200.0 + t)
    assert raw["sky_dip_s"] > 300.0
    assert shaped["sky_dip_s"] == 0.0
    assert shaped["gate_flags"] & GateFlag.POA_SHAPE and not raw["gate_flags"] & GateFlag.POA_SHAPE


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_pinned_export_is_timed_and_kept_out_of_the_dips():
    """A limited inverter drops output on every array at once; that is not cloud."""
    s = _sampled(_signal([(600.0, 900.0)]), 1.0)
    export = [(0.0, 2.0), (600.0, 5.0), (900.0, 2.0)]
    m = measure_slot(START, END, {"a": s, "b": s}, _kinds("a", "b"), shape=None, export=export, export_limit_kw=5.0)
    assert m["export_pinned_s"] == pytest.approx(300.0)
    assert m["sky_dip_s"] == 0.0
    assert m["gate_flags"] & GateFlag.EXPORT_SERIES


def test_the_p90_reference_ignores_pinned_time():
    """If the pinned stretch set the reference, the healthy remainder would read as above it, not as normal."""
    healthy = [(float(t), 1000.0) for t in range(0, 1800)]
    held_down = [(t, 300.0 if 0 <= t < 1500 else v) for t, v in healthy]
    export = [(0.0, 5.0), (1500.0, 1.0)]
    m = measure_slot(START, END, {"a": held_down, "b": held_down}, _kinds("a", "b"), shape=None, export=export, export_limit_kw=5.0)
    assert m["export_pinned_s"] == pytest.approx(1500.0)
    assert m["sky_dip_s"] == 0.0


def test_a_cloud_in_the_unpinned_minutes_of_a_mostly_pinned_slot_is_still_seen():
    """Held-down output during the limit must not set the reference: if it did, the
    p90 would sit at the limited level and the cloud afterwards would not read as a dip."""
    series = [(0.0, 300.0), (1700.0, 1000.0), (1750.0, 450.0)]
    export = [(0.0, 5.0), (1700.0, 1.0)]
    m = measure_slot(START, END, {"a": series, "b": series}, _kinds("a", "b"), shape=None, export=export, export_limit_kw=5.0)
    assert m["export_pinned_s"] == pytest.approx(1700.0)
    assert m["sky_dip_s"] == pytest.approx(50.0)


def test_a_dip_running_into_the_slot_end_is_a_run_but_a_blip_there_is_not():
    long_tail = _sampled(_signal([(1700.0, 1800.0)]), 1.0)
    blip_tail = _sampled(_signal([(1795.0, 1800.0)]), 1.0)
    assert measure_slot(START, END, {"a": long_tail, "b": long_tail}, _kinds("a", "b"), shape=None)["sky_dip_runs"] == 1
    assert measure_slot(START, END, {"a": blip_tail, "b": blip_tail}, _kinds("a", "b"), shape=None)["sky_dip_runs"] == 0


def test_no_limit_means_pinned_time_is_unknown():
    s = _sampled(_signal([]), 1.0)
    m = measure_slot(START, END, {"a": s}, _kinds("a"), shape=None, export=[(0.0, 4.9)], export_limit_kw=0.0)
    assert m["export_pinned_s"] == -1.0
    assert not m["gate_flags"] & GateFlag.EXPORT_SERIES


# ---------------------------------------------------------------------------
# Flags and missing inputs
# ---------------------------------------------------------------------------


def test_flags_describe_the_inputs():
    s = _sampled(_signal([]), 1.0)
    dc = measure_slot(START, END, {"a": s, "b": s}, _kinds("a", "b"), shape=None, low_sun=True)
    assert dc["gate_flags"] == GateFlag.DC_TRACKERS | GateFlag.CROSS_REF | GateFlag.LOW_SUN
    counter = measure_slot(START, END, {"a": s}, {"a": "counter"}, shape=None)
    assert counter["gate_flags"] == GateFlag.AC_ARRAYS | GateFlag.COUNTER_SOURCE


def test_an_input_that_starts_late_marks_the_slot_partial():
    late = [(600.0, 1000.0), (1200.0, 1000.0)]
    m = measure_slot(START, END, {"a": late}, _kinds("a"), shape=None)
    assert m["gate_flags"] & GateFlag.PARTIAL
    on_time = measure_slot(START, END, {"a": [(0.0, 1000.0)]}, _kinds("a"), shape=None)
    assert not on_time["gate_flags"] & GateFlag.PARTIAL


def test_an_array_with_no_samples_is_partial_and_does_not_count_as_a_reference():
    s = _sampled(_signal([(100.0, 400.0)]), 1.0)
    m = measure_slot(START, END, {"a": s, "b": []}, _kinds("a", "b"), shape=None)
    assert m["gate_flags"] & GateFlag.PARTIAL and not m["gate_flags"] & GateFlag.CROSS_REF


def test_no_arrays_at_all_is_unknown():
    m = measure_slot(START, END, {}, {}, shape=None, export=[(0.0, 5.0)], export_limit_kw=5.0)
    assert m["sky_dip_s"] == -1.0 and m["sky_dip_runs"] == -1
    assert m["export_pinned_s"] == pytest.approx(1800.0)  # Export is still measurable.


# ---------------------------------------------------------------------------
# Series builders
# ---------------------------------------------------------------------------


def test_dc_power_multiplies_held_values_and_sums_trackers():
    v1, i1 = [(0.0, 400.0), (10.0, 380.0)], [(0.0, 5.0), (5.0, 6.0)]
    v2, i2 = [(2.0, 300.0)], [(0.0, 2.0)]
    p = dc_power([(v1, i1), (v2, i2)])
    # t=0 and t=1 skipped (tracker 2 has no voltage yet), then V×I summed at every change.
    assert p == [(2.0, 400 * 5 + 300 * 2), (5.0, 400 * 6 + 300 * 2), (10.0, 380 * 6 + 300 * 2)]


def test_counter_power_never_differences_below_the_window():
    """A counter ticking in 10 Wh steps at uneven intervals must not produce spikes."""
    samples = [(0.0, 100.0)]
    t, e = 0.0, 100.0
    while t < 1800:
        t += 0.3 if int(t) % 97 == 0 else 4.5  # Uneven, sometimes 0.3 s apart.
        e += 0.01 * (t - samples[-1][0]) / 4.5
        samples.append((t, round(e, 2)))
    p = counter_power(samples)
    assert p and all(t2 - t1 >= 300.0 for (t1, _), (t2, _) in zip(p, p[1:]))
    true_kw = 0.01 / 4.5 * 3600
    assert all(abs(kw - true_kw) / true_kw < 0.1 for _, kw in p)


def test_counter_power_skips_a_reset():
    assert counter_power([(0.0, 50.0), (400.0, 51.0), (800.0, 0.2), (1200.0, 1.2)]) == [
        (0.0, pytest.approx(9.0)),
        (800.0, pytest.approx(9.0)),
    ]


def test_counter_power_without_an_end_needs_two_readings():
    """The pure differencing contract is unchanged when no end is given."""
    assert counter_power([(0.0, 50.0)]) == []
    assert counter_power([(0.0, 50.0), (100.0, 50.0)]) == []


def test_a_counter_that_never_ticks_holds_to_the_end_as_zero():
    """Export while the house uses every watt: one reading, held, is 0 kW, not no series."""
    assert counter_power([(0.0, 50.0)], end=1800.0) == [(0.0, 0.0)]


def test_the_hold_counts_the_minutes_after_the_last_tick():
    """0.5 kWh in the first 400 s, then nothing: the last 1400 s read 0 kW, not missing."""
    assert counter_power([(0.0, 50.0), (400.0, 50.5)], end=1800.0) == [
        (0.0, pytest.approx(4.5)),
        (400.0, 0.0),
    ]


def test_the_hold_never_shortens_a_window_below_the_minimum():
    """A tick 100 s before the end must not become a 100 s window (resolution spikes)."""
    p = counter_power([(0.0, 50.0), (1700.0, 50.85)], end=1800.0)
    assert p == [(0.0, pytest.approx(1.8))]


def test_an_end_at_or_before_the_last_reading_adds_nothing():
    samples = [(0.0, 50.0), (400.0, 50.5)]
    assert counter_power(samples, end=400.0) == counter_power(samples) == [(0.0, pytest.approx(4.5))]


def test_a_flat_export_counter_measures_zero_pinned_seconds():
    """End to end through measure_slot: a held flat export is a measured series, pinned 0 s."""
    m = measure_slot(
        0.0,
        1800.0,
        {"A": [(0.0, 1.0)], "B": [(0.0, 1.0)]},
        {"A": "dc", "B": "dc"},
        None,
        export=counter_power([(0.0, 12.0)], end=1800.0),
        export_limit_kw=5.0,
    )
    assert GateFlag.EXPORT_SERIES in GateFlag(m["gate_flags"])
    assert m["export_pinned_s"] == 0.0


def test_the_design_document_flag_table_matches_the_enum():
    """The bits are part of the stored format; the documented table and the code must agree."""
    import re
    from pathlib import Path

    text = (Path(__file__).parents[1] / "DESIGN_DOCUMENT.md").read_text(encoding="utf-8")
    section = text.split("`GateFlag` (`const.py`", 1)[1].split("\n\n", 2)[1]
    documented = {int(bit): name for bit, name in re.findall(r"^\| (\d+) \| `(\w+)` \|", section, re.M)}
    assert documented == {flag.value.bit_length() - 1: flag.name for flag in GateFlag}
