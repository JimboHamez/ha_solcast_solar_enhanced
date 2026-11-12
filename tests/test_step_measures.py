"""Export-hold step measures (step_measures.py): synthetic cases plus the owner's 6 Nov step test.

The synthetic cases pin each rule against the wrong implementations it is easiest to
write: raw-sample differencing (books a regulator's few-second correction as the
step), a sign-dependent held test, voltage judged on one tracker only, and a cut test
without its voltage or power floor, or without its look-back (judged against the window
just before the step, a limiter winding back after a load drop reads as a cut).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from custom_components.solcast_solar_enhanced.const import STEP_HOP_S, STEP_LIMITER_RESPONSE_S, STEP_MIN_W, STEP_WINDOW_S
from custom_components.solcast_solar_enhanced.step_measures import held_mean, median_gap, measure_steps

START, END = 0.0, 1800.0
FIXTURE = Path(__file__).parent / "fixtures" / "step_test_2026-11-06.json.gz"


def _series(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A 1 Hz series that is piecewise constant through ``(from_t, value)`` breakpoints."""
    out = []
    for t in range(int(START), int(END)):
        value = next(v for at, v in reversed(points) if at <= t)
        out.append((float(t), value))
    return out


def test_held_mean_is_time_weighted() -> None:
    mean = held_mean([(0.0, 100.0), (10.0, 400.0), (40.0, 0.0)])
    assert mean(0.0, 30.0) == pytest.approx((100 * 10 + 400 * 20) / 30)
    assert mean(30.0, 60.0) == pytest.approx((400 * 10 + 0 * 20) / 30)
    assert mean(5.0, 35.0) == pytest.approx((100 * 5 + 400 * 25) / 30)  # Any window, not only a grid one.


def test_held_mean_needs_a_value_at_the_window_start() -> None:
    mean = held_mean([(45.0, 5.0)])
    assert mean(0.0, 30.0) is None
    assert mean(30.0, 60.0) is None
    assert mean(60.0, 90.0) == pytest.approx(5.0)


def test_a_load_step_under_a_hold_is_held() -> None:
    """Generation rises 1 kW with the house while net stays put: one held step."""
    gen = _series([(0, 2000.0), (300, 3000.0)])
    net = _series([(0, 0.0)])
    got = measure_steps(START, END, net, gen)
    assert (got["gen_steps"], got["held_steps"]) == (1, 1)


@pytest.mark.parametrize("offset_s", range(int(STEP_WINDOW_S)))
def test_a_step_counts_once_wherever_it_falls_in_a_window(offset_s: int) -> None:
    """A 400 W held load step is one held step at any offset from the slot's grid.

    On a fixed 30 s grid a step half way into a window splits into two 200 W changes, both
    under STEP_MIN_W, and vanishes; a 1 kW step there counts twice. Overlapping windows
    with one step kept per window's span count it once.
    """
    gen = _series([(0, 2000.0), (300 + offset_s, 2400.0)])
    got = measure_steps(START, END, _series([(0, 0.0)]), gen)
    assert (got["gen_steps"], got["held_steps"]) == (1, 1)
    big = _series([(0, 2000.0), (300 + offset_s, 3000.0)])
    assert measure_steps(START, END, _series([(0, 0.0)]), big)["gen_steps"] == 1


def test_two_steps_a_window_apart_are_both_counted() -> None:
    """Keeping one step per window's span must not swallow the next real step."""
    gen = _series([(0, 2000.0), (300, 3000.0), (300 + int(STEP_WINDOW_S), 3600.0)])
    assert measure_steps(START, END, _series([(0, 0.0)]), gen)["gen_steps"] == 2


def test_the_hop_resolves_inside_a_window() -> None:
    """The hop must divide the window into several boundaries, or the overlap does nothing."""
    assert STEP_HOP_S <= STEP_WINDOW_S / 3
    assert (STEP_WINDOW_S / STEP_HOP_S).is_integer()


def test_a_cloud_step_lands_on_the_grid() -> None:
    gen = _series([(0, 3000.0), (300, 2000.0)])
    net = _series([(0, -2500.0), (300, -1500.0)])
    got = measure_steps(START, END, net, gen)
    assert (got["gen_steps"], got["held_steps"]) == (1, 0)


def test_net_sign_convention_does_not_matter() -> None:
    gen = _series([(0, 3000.0), (300, 2000.0), (900, 3500.0)])
    net = _series([(0, -2500.0), (300, -1500.0), (900, -1500.0)])
    flipped = [(t, -v) for t, v in net]
    assert measure_steps(START, END, net, gen) == measure_steps(START, END, flipped, gen)


def test_a_regulator_transient_inside_a_window_is_still_held() -> None:
    """The load steps in, net jumps, and the inverter ramps up over 3 s: the window mean barely moves.

    Differencing raw samples would book the spike as the step and call it unheld.
    """
    gen = _series([(0, 2000.0), (301, 2333.0), (302, 2667.0), (303, 3000.0)])
    net = _series([(0, 0.0), (300, 1000.0), (301, 667.0), (302, 333.0), (303, 0.0)])
    got = measure_steps(START, END, net, gen)
    assert got["held_steps"] == got["gen_steps"] == 1


def test_a_change_below_the_step_size_is_not_a_step() -> None:
    gen = _series([(0, 2000.0), (300, 2000.0 + STEP_MIN_W - 50)])
    got = measure_steps(START, END, _series([(0, 0.0)]), gen)
    assert got["gen_steps"] == 0


def test_regulated_needs_every_tracker_moving_against_power() -> None:
    """A held step with the trackers walking off the MPP is regulated; one tracker flat is not."""
    gen = _series([(0, 2000.0), (300, 3000.0)])
    net = _series([(0, 0.0)])
    v_down = _series([(0, 440.0), (300, 430.0)])  # Power up, voltage down 2.3%: back toward the MPP.
    v_flat = _series([(0, 400.0)])
    assert measure_steps(START, END, net, gen, [v_down, v_down])["regulated_steps"] == 1
    assert measure_steps(START, END, net, gen, [v_down, v_flat])["regulated_steps"] == 0


def test_a_battery_holding_net_is_held_but_not_regulated() -> None:
    """A battery absorbs a cloud: generation falls, net holds, the trackers stay on their MPP."""
    gen = _series([(0, 4000.0), (300, 2500.0)])
    net = _series([(0, 0.0)])
    v = _series([(0, 380.0), (300, 378.0)])  # A cloud nudges Vmp, it does not walk it.
    got = measure_steps(START, END, net, gen, [v, v])
    assert (got["held_steps"], got["regulated_steps"], got["cut_steps"]) == (1, 0, 0)


def test_a_cut_needs_the_voltage_rise_and_the_power_floor() -> None:
    gen = _series([(0, 4000.0), (300, 400.0)])
    net = _series([(0, -3500.0), (300, 100.0)])
    up = _series([(0, 400.0), (300, 430.0)])  # +7.5%: pulled toward open circuit.
    small = _series([(0, 400.0), (300, 406.0)])  # +1.5%: a cloud's cooling.
    assert measure_steps(START, END, net, gen, [up, up])["cut_steps"] == 1
    assert measure_steps(START, END, net, gen, [up, small])["cut_steps"] == 0
    low = _series([(0, 900.0), (300, 100.0)])
    low_net = _series([(0, -400.0), (300, 400.0)])
    assert measure_steps(START, END, low_net, low, [up, up])["cut_steps"] == 0


def test_a_limiter_taking_over_after_a_load_drop_is_not_a_cut() -> None:
    """A big load switches off while export is near the limit: generation is cut, the trackers walk up, but export *rises*.

    Seen ten times on the owner's 8-9 Nov 2026 history (5 kW limit, EV and oven loads), and booked as
    cuts before 1.16.0b1.dev2. A cap or trip lowers export instead, which the first case keeps.
    """
    gen = _series([(0, 7100.0), (300, 5900.0)])
    up = _series([(0, 380.0), (300, 413.0)])  # +8.7%, as at 9 Nov 14:03:30.
    export_rises = _series([(0, -400.0), (300, -5060.0)])  # Import-positive: export 0.4 -> 5.06 kW.
    assert measure_steps(START, END, export_rises, gen, [up, up])["cut_steps"] == 0
    export_falls = _series([(0, -4600.0), (300, 60.0)])  # The same drop landing on the grid.
    assert measure_steps(START, END, export_falls, gen, [up, up])["cut_steps"] == 1


def _limiter_after_load_drop(drop_at: float, response_s: float) -> tuple[list, list, list]:
    """Export pinned at a 5 kW limit; a 2 kW load switches off at ``drop_at``.

    Export overshoots to 7 kW, then the inverter winds generation from 7 kW to 5 kW in a
    straight line over ``response_s``, walking the trackers 9% up the curve as it goes.
    Net power is import-positive: house load minus generation.
    """
    gen, net, volts = [], [], []
    for t in range(int(START), int(END)):
        wound = min(max((t - drop_at) / response_s, 0.0), 1.0)
        g = 7000.0 - 2000.0 * wound
        gen.append((float(t), g))
        net.append((float(t), (2000.0 if t < drop_at else 0.0) - g))
        volts.append((float(t), 380.0 + 35.0 * wound))
    return gen, net, volts


@pytest.mark.parametrize("response_s", [5.0, 15.0, 30.0])
@pytest.mark.parametrize("offset_s", [0.0, 7.0, 15.0, 22.0, 29.0])
def test_a_limiter_winding_back_after_a_load_drop_is_never_a_cut(response_s: float, offset_s: float) -> None:
    """Every response a code allows (UK 5 s, AU 15 s, US 30 s), wherever the drop falls in a window.

    Judged only between the windows either side of the step, the export overshoot settling
    back reads as a cut: generation down, import up, trackers walked up. The owner's 11 Nov
    2026 history booked one that way at 11:35:30 under 1.16.0b1.dev2.
    """
    gen, net, v = _limiter_after_load_drop(600.0 + offset_s, response_s)
    got = measure_steps(START, END, net, gen, [v, v])
    assert got["gen_steps"] >= 1
    assert got["cut_steps"] == 0


def test_a_cut_must_still_be_on_the_grid_a_window_later() -> None:
    """A drop that lands on the grid and recovers inside the next window is not a cap or a trip."""
    gen = _series([(0, 4000.0), (300, 400.0), (330, 4000.0)])
    net = _series([(0, -3500.0), (300, 100.0), (330, -3500.0)])
    up = _series([(0, 400.0), (300, 430.0), (330, 400.0)])
    assert measure_steps(START, END, net, gen, [up, up])["cut_steps"] == 0


@pytest.mark.parametrize(("at", "judged"), [(60, False), (90, True), (1740, True), (1770, False)])
def test_a_cut_needs_its_reference_and_settled_windows_inside_the_slot(at: int, judged: bool) -> None:
    """The reference sits three windows back and the settled one a window on, so the slot's edges go unjudged."""
    gen = _series([(0, 4000.0), (at, 400.0)])
    net = _series([(0, -3500.0), (at, 100.0)])
    up = _series([(0, 400.0), (at, 430.0)])
    assert measure_steps(START, END, net, gen, [up, up])["cut_steps"] == int(judged)


def test_the_look_back_covers_the_slowest_limiter_any_code_allows() -> None:
    """A US power control system may take 30 s; the reference must not drift inside that."""
    assert STEP_LIMITER_RESPONSE_S >= 30.0


def test_a_rise_is_never_a_cut() -> None:
    """Above the power floor, an unabsorbed rise with voltage moving 7% against it is still not a cut."""
    gen = _series([(0, 1500.0), (300, 5000.0)])
    net = _series([(0, -1000.0), (300, -4500.0)])
    down = _series([(0, 430.0), (300, 400.0)])
    assert measure_steps(START, END, net, gen, [down, down])["cut_steps"] == 0


def test_without_dc_the_voltage_counts_are_unknown() -> None:
    gen = _series([(0, 2000.0), (300, 3000.0)])
    got = measure_steps(START, END, _series([(0, 0.0)]), gen)
    assert (got["regulated_steps"], got["cut_steps"]) == (-1, -1)
    assert got["gen_steps"] == 1


@pytest.mark.parametrize("missing", ["net", "gen"])
def test_without_both_power_series_nothing_is_counted(missing: str) -> None:
    series = _series([(0, 1000.0)])
    got = measure_steps(START, END, None if missing == "net" else series, None if missing == "gen" else series)
    assert (got["gen_steps"], got["held_steps"], got["regulated_steps"], got["cut_steps"]) == (-1, -1, -1, -1)
    assert got["net_gap_s" if missing == "gen" else "gen_gap_s"] == pytest.approx(1.0)


def test_median_gap() -> None:
    samples = [(0.0, 1.0), (60.0, 1.0), (120.0, 1.0), (300.0, 1.0)]
    assert median_gap(samples, 0.0, 1800.0) == pytest.approx(60.0)
    assert median_gap([(5.0, 1.0)], 0.0, 1800.0) is None


def test_the_window_is_long_enough_for_a_regulator_to_settle() -> None:
    """The 6 Nov test showed 5 s windows cannot separate a hold; the constant must not drift there."""
    assert STEP_WINDOW_S >= 30.0


# --- the owner's 6 Nov 2026 step test --------------------------------------------------


@pytest.fixture(scope="module")
def step_test() -> dict:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))["slots"]


def _real(slot: dict, key: str) -> list[tuple[float, float]]:
    return [(float(t), float(v)) for t, v in slot[key]]


@pytest.mark.parametrize("generation", ["ac", "dc"])
def test_real_export_hold_is_held_and_regulated(step_test: dict, generation: str) -> None:
    """0 kW then 2 kW limits with oven steps: held and regulated steps, and the 0 kW switch as a cut."""
    slot = step_test["hold"]
    gen = _real(slot, "ac") if generation == "ac" else _dc_power(slot)
    got = measure_steps(START, END, _real(slot, "grid"), gen, [_real(slot, "v1"), _real(slot, "v2")])
    assert got["held_steps"] >= 12
    assert got["regulated_steps"] >= 6
    assert got["cut_steps"] >= 1
    assert got["held_steps"] / got["gen_steps"] > 0.3


@pytest.mark.parametrize("shift_s", range(0, int(STEP_WINDOW_S), 3))
def test_real_step_test_cut_survives_any_alignment(step_test: dict, shift_s: int) -> None:
    """The 6 Nov 0 kW switch is a cut whatever second the slot's grid happens to start on.

    The fixed 30 s grid of 1.16.0b1.dev3 missed it at 3 of these 10 alignments.
    """
    slot = step_test["hold"]

    def shifted(key: str) -> list[tuple[float, float]]:
        return [(t - shift_s, v) for t, v in _real(slot, key)]

    got = measure_steps(START, END, shifted("grid"), shifted("ac"), [shifted("v1"), shifted("v2")])
    assert got["cut_steps"] >= 1


# The free slot's last two minutes break into sun and brush the 5 kW limit (export
# 4.82 -> 4.91 kW at 28:50): a real, brief hold. Free running is judged before it.
FREE_RUNNING_END = 1680.0


@pytest.mark.parametrize("generation", ["ac", "dc"])
def test_real_free_running_slot_holds_nothing(step_test: dict, generation: str) -> None:
    """The same morning an hour earlier, unconstrained under passing cloud: many steps, none held."""
    slot = step_test["free"]
    gen = _real(slot, "ac") if generation == "ac" else _dc_power(slot)
    got = measure_steps(START, FREE_RUNNING_END, _real(slot, "grid"), gen, [_real(slot, "v1"), _real(slot, "v2")])
    assert got["gen_steps"] >= 10
    assert got["held_steps"] == 0
    assert got["regulated_steps"] == 0
    assert got["cut_steps"] == 0


@pytest.mark.parametrize("shift_s", range(0, int(STEP_WINDOW_S), 3))
def test_real_limit_brush_is_held_at_any_alignment(step_test: dict, shift_s: int) -> None:
    """The free slot's closing brush with the 5 kW limit is one held step, wherever the grid starts.

    The fixed 30 s grid of 1.16.0b1.dev3 saw it at only 3 of these 10 alignments, and counted it
    twice at two of those.
    """
    slot = step_test["free"]

    def shifted(key: str) -> list[tuple[float, float]]:
        return [(t - shift_s, v) for t, v in _real(slot, key)]

    got = measure_steps(START, END, shifted("grid"), shifted("ac"), [shifted("v1"), shifted("v2")])
    assert got["held_steps"] == 1
    assert got["cut_steps"] == 0


def _dc_power(slot: dict) -> list[tuple[float, float]]:
    from custom_components.solcast_solar_enhanced.slot_gate import dc_power

    return dc_power([(_real(slot, "v1"), _real(slot, "i1")), (_real(slot, "v2"), _real(slot, "i2"))])
