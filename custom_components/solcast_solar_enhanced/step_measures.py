"""Export-hold step measures: did net power absorb the slot's generation steps?

Record-only (schema v2): nothing reads these yet. Pure Python, no Home Assistant
imports, so it runs in the executor and is tested directly.

While an export limit holds (a fixed limit, zero export or a network operator's
dynamic one), the inverter follows the house: a load step moves generation and
leaves net power where it was. Free running, a generation step is the sky changing
and lands on the grid instead. So each slot counts:

* ``gen_steps`` — changes in generation of at least ``STEP_MIN_W`` between the windows
  either side of a boundary. Boundaries are tried every ``STEP_HOP_S``, not only a
  window apart, and of the qualifying ones the largest step within any one window's
  span is the one counted, so neither the count nor a step's verdict depends on where
  it fell relative to the slot's start.
* ``held_steps`` — those net power absorbed (moved by at most ``STEP_HELD_FRACTION``
  of the step). Needs only grid power and generation power, so it works on any system,
  string or microinverter, and never needs to know the limit.
* ``regulated_steps`` — held steps where every tracker's voltage also moved against
  power. A battery or a solar-surplus EV charger can hold net power too, but behind it
  the trackers stay on their MPP; only a limiter walks them along the curve.
* ``cut_steps`` — drops net power did *not* absorb, with every tracker's voltage up
  by ``STEP_CUT_V``, that the grid still carries one window later: against the window
  before the episode, generation is down and import up by at least
  ``STEP_CUT_ABSORBED`` of it. That is output pulled away from the MPP whatever the
  house drew, the signature of a generation cap or a protection trip rather than an
  export hold. A limiter after a load drop also cuts generation and walks the trackers
  up, but export first overshoots the limit, then settles back *to* it, so against
  the window before the overshoot import has not risen at all. The reference sits
  ``STEP_LIMITER_RESPONSE_S`` before the window ahead of the step, so even a limiter
  that takes the longest any code allows cannot have started by then; a step within
  that reach of the slot's start, or in its last window, cannot be judged a cut.

The last two need DC voltage and are ``-1`` without it; the first two are ``-1``
without both power series. Series are compared as time-weighted window means, on
which see ``STEP_WINDOW_S`` and ``STEP_HOP_S``. Net power is import-positive (Home Assistant's grid
convention); only the cut test reads its sign, the held test uses its size.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import NamedTuple, TypedDict

from .const import (
    STEP_CUT_ABSORBED,
    STEP_CUT_MIN_W,
    STEP_CUT_V,
    STEP_HELD_FRACTION,
    STEP_HOP_S,
    STEP_LIMITER_RESPONSE_S,
    STEP_MIN_W,
    STEP_REGULATED_V,
    STEP_WINDOW_S,
)

Samples = Sequence[tuple[float, float]]
WindowMean = Callable[[float, float], float | None]
UNKNOWN = -1
# Float slack for comparing boundary times built from integer hops.
_EPS = 1e-6


class StepMeasures(TypedDict):
    """One slot's step counts (``-1`` = not measurable) and the inputs' median sample gaps."""

    gen_steps: int
    held_steps: int
    regulated_steps: int
    cut_steps: int
    net_gap_s: float | None
    gen_gap_s: float | None


class _Step(NamedTuple):
    """A qualifying boundary: generation and net power in the windows either side of ``at``."""

    at: float
    g0: float
    g1: float
    n0: float
    n1: float

    @property
    def dp(self) -> float:
        """The generation step, W."""
        return self.g1 - self.g0


def held_mean(samples: Samples) -> WindowMean:
    """The time-weighted mean of a held series over any window ``[a, b)``.

    A recorded state holds until the next one, and the last one holds to the end.
    The mean is ``None`` for a window the series has no value at the start of (no
    sample at or before it). Built once from running totals, so each window costs
    a bisection rather than a scan, which is what makes a short hop affordable.
    The samples must be in time order.
    """
    times = [t for t, _ in samples]
    values = [v for _, v in samples]
    area = [0.0]
    for i in range(1, len(times)):
        area.append(area[-1] + values[i - 1] * (times[i] - times[i - 1]))

    def integral(x: float) -> float | None:
        i = bisect.bisect_right(times, x) - 1
        return None if i < 0 else area[i] + values[i] * (x - times[i])

    def mean(a: float, b: float) -> float | None:
        lo = integral(a)
        hi = integral(b)
        if lo is None or hi is None:
            return None
        return (hi - lo) / (b - a)

    return mean


def median_gap(samples: Samples, start: float, end: float) -> float | None:
    """Median interval between this series' samples inside the slot, or ``None`` with fewer than two."""
    inside = [t for t, _ in samples if start <= t < end]
    gaps = [b - a for a, b in pairwise(inside)]
    return statistics.median(gaps) if gaps else None


def measure_steps(
    start: float,
    end: float,
    net_w: Samples | None,
    gen_w: Samples | None,
    tracker_volts: Sequence[Samples] = (),
) -> StepMeasures:
    """Count the slot's generation steps, and how net power and the trackers responded.

    Args:
        start: Slot start, epoch seconds.
        end: Slot end, epoch seconds.
        net_w: Grid power in W, positive while importing; ``None`` if not configured.
        gen_w: Generation power in W (AC output or DC V × I); ``None`` if unavailable.
        tracker_volts: Each DC tracker's voltage samples; empty without DC telemetry.

    Returns:
        The four counts and the two inputs' median sample gaps (``None`` when absent).
    """
    out: StepMeasures = {
        "gen_steps": UNKNOWN,
        "held_steps": UNKNOWN,
        "regulated_steps": UNKNOWN,
        "cut_steps": UNKNOWN,
        "net_gap_s": median_gap(net_w, start, end) if net_w else None,
        "gen_gap_s": median_gap(gen_w, start, end) if gen_w else None,
    }
    if not net_w or not gen_w:
        return out
    gen, net = held_mean(gen_w), held_mean(net_w)
    volts = [held_mean(v) for v in tracker_volts if v]
    has_dc = bool(volts) and len(volts) == len(tracker_volts)
    held = regulated = cut = 0
    steps = _peak_steps(_candidates(start, end, gen, net))
    for step in steps:
        is_held = abs(step.n1 - step.n0) <= STEP_HELD_FRACTION * abs(step.dp)
        held += is_held
        if not has_dc:
            continue
        against = _against(volts, step.at, step.dp)
        if against is None:
            continue
        if is_held and against >= STEP_REGULATED_V:
            regulated += 1
        elif (
            not is_held
            and step.dp < 0
            and against >= STEP_CUT_V
            and step.g0 >= STEP_CUT_MIN_W
            and _grid_carries_the_drop(gen, net, step.at, start, end)
        ):
            cut += 1
    out["gen_steps"], out["held_steps"] = len(steps), held
    if has_dc:
        out["regulated_steps"], out["cut_steps"] = regulated, cut
    return out


def _candidates(start: float, end: float, gen: WindowMean, net: WindowMean) -> list[_Step]:
    """Every boundary on the ``STEP_HOP_S`` grid whose generation step is at least ``STEP_MIN_W``.

    A boundary needs a full window either side inside the slot, and a value from both
    series in each.
    """
    out: list[_Step] = []
    j = 0
    while (at := start + STEP_WINDOW_S + j * STEP_HOP_S) + STEP_WINDOW_S <= end + _EPS:
        j += 1
        g0, g1 = gen(at - STEP_WINDOW_S, at), gen(at, at + STEP_WINDOW_S)
        n0, n1 = net(at - STEP_WINDOW_S, at), net(at, at + STEP_WINDOW_S)
        if g0 is None or g1 is None or n0 is None or n1 is None or abs(g1 - g0) < STEP_MIN_W:
            continue
        out.append(_Step(at, g0, g1, n0, n1))
    return out


def _peak_steps(candidates: list[_Step]) -> list[_Step]:
    """The largest steps, no two closer than a window: one step is counted once.

    Overlapping windows see a step at every boundary within a window of it, each
    with a share of its size. Taking the largest first keeps the boundary that
    straddles the step, and a window's spacing is what the fixed grid allowed, so
    two real steps a window apart are both kept.
    """
    picked: list[_Step] = []
    for step in sorted(candidates, key=lambda s: (-abs(s.dp), s.at)):
        if all(abs(step.at - p.at) >= STEP_WINDOW_S - _EPS for p in picked):
            picked.append(step)
    return sorted(picked, key=lambda s: s.at)


def _grid_carries_the_drop(gen: WindowMean, net: WindowMean, at: float, start: float, end: float) -> bool:
    """Whether the drop at ``at`` still lands on the grid, judged from before the episode.

    The reference window ends ``STEP_LIMITER_RESPONSE_S`` before the window ahead of
    the step starts, so a limiter's overshoot after a load drop cannot reach it; the
    settled window is the one after the window behind the step. A limiter still
    winding back there holds export above the limit, which only lowers import, so it
    cannot pass for a cut either.
    """
    ref_end = at - STEP_WINDOW_S - STEP_LIMITER_RESPONSE_S
    ref_start, settled_start = ref_end - STEP_WINDOW_S, at + STEP_WINDOW_S
    if ref_start < start - _EPS or settled_start + STEP_WINDOW_S > end + _EPS:
        return False
    g0, g1 = gen(ref_start, ref_end), gen(settled_start, settled_start + STEP_WINDOW_S)
    n0, n1 = net(ref_start, ref_end), net(settled_start, settled_start + STEP_WINDOW_S)
    if g0 is None or g1 is None or n0 is None or n1 is None:
        return False
    lost = g0 - g1
    return lost >= STEP_MIN_W and n1 - n0 >= STEP_CUT_ABSORBED * lost


def _against(volts: Sequence[WindowMean], at: float, dp: float) -> float | None:
    """The smallest relative voltage move against the power step, over every tracker.

    Positive when every tracker's voltage moved opposite to power (power up, voltage
    down, or the reverse). ``None`` when a tracker has no value either side.
    """
    sign = 1.0 if dp > 0 else -1.0
    moves: list[float] = []
    for mean in volts:
        v0, v1 = mean(at - STEP_WINDOW_S, at), mean(at, at + STEP_WINDOW_S)
        if v0 is None or v1 is None or v0 <= 0:
            return None
        moves.append(-sign * (v1 - v0) / v0)
    return min(moves)
