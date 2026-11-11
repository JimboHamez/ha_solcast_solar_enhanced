"""Export-hold step measures: did net power absorb the slot's generation steps?

Record-only (schema v2): nothing reads these yet. Pure Python, no Home Assistant
imports, so it runs in the executor and is tested directly.

While an export limit holds (a fixed limit, zero export or a network operator's
dynamic one), the inverter follows the house: a load step moves generation and
leaves net power where it was. Free running, a generation step is the sky changing
and lands on the grid instead. So each slot counts:

* ``gen_steps`` — window-to-window changes in generation of at least ``STEP_MIN_W``.
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
which see ``STEP_WINDOW_S``. Net power is import-positive (Home Assistant's grid
convention); only the cut test reads its sign, the held test uses its size.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Sequence
from itertools import pairwise
from typing import TypedDict

from .const import (
    STEP_CUT_ABSORBED,
    STEP_CUT_MIN_W,
    STEP_CUT_V,
    STEP_HELD_FRACTION,
    STEP_LIMITER_RESPONSE_S,
    STEP_MIN_W,
    STEP_REGULATED_V,
    STEP_WINDOW_S,
)

Samples = Sequence[tuple[float, float]]
UNKNOWN = -1


class StepMeasures(TypedDict):
    """One slot's step counts (``-1`` = not measurable) and the inputs' median sample gaps."""

    gen_steps: int
    held_steps: int
    regulated_steps: int
    cut_steps: int
    net_gap_s: float | None
    gen_gap_s: float | None


def window_means(samples: Samples, start: float, end: float, window_s: float = STEP_WINDOW_S) -> list[float | None]:
    """Time-weighted mean of a held series over each ``window_s`` window in ``[start, end)``.

    A recorded state holds until the next one. A window the series has no value at the
    start of (no sample at or before it) is ``None``.
    """
    times = [t for t, _ in samples]
    out: list[float | None] = []
    a = start
    while a + window_s <= end + 1e-9:
        b = a + window_s
        i = bisect.bisect_right(times, a) - 1
        if i < 0:
            out.append(None)
            a = b
            continue
        total, t, value = 0.0, a, samples[i][1]
        j = i + 1
        while j < len(samples) and samples[j][0] < b:
            total += value * (samples[j][0] - t)
            t, value = samples[j][0], samples[j][1]
            j += 1
        total += value * (b - t)
        out.append(total / window_s)
        a = b
    return out


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
    net = window_means(net_w, start, end)
    gen = window_means(gen_w, start, end)
    volts = [window_means(v, start, end) for v in tracker_volts if v]
    has_dc = bool(volts) and len(volts) == len(tracker_volts)
    steps = held = regulated = cut = 0
    for k in range(1, len(gen)):
        g0, g1, n0, n1 = gen[k - 1], gen[k], net[k - 1], net[k]
        if g0 is None or g1 is None or n0 is None or n1 is None:
            continue
        dp = g1 - g0
        if abs(dp) < STEP_MIN_W:
            continue
        steps += 1
        is_held = abs(n1 - n0) <= STEP_HELD_FRACTION * abs(dp)
        held += is_held
        if not has_dc:
            continue
        against = _against(volts, k, dp)
        if against is None:
            continue
        if is_held and against >= STEP_REGULATED_V:
            regulated += 1
        elif (
            not is_held
            and dp < 0
            and against >= STEP_CUT_V
            and g0 >= STEP_CUT_MIN_W
            and _grid_carries_the_drop(gen, net, k)
        ):
            cut += 1
    out["gen_steps"], out["held_steps"] = steps, held
    if has_dc:
        out["regulated_steps"], out["cut_steps"] = regulated, cut
    return out


def _grid_carries_the_drop(gen: Sequence[float | None], net: Sequence[float | None], k: int) -> bool:
    """Whether the drop at window ``k`` still lands on the grid, judged from before the episode.

    The reference window ends ``STEP_LIMITER_RESPONSE_S`` before window ``k - 1`` starts,
    so a limiter's overshoot after a load drop cannot reach it; the settled window is
    ``k + 1``. A limiter still winding back there holds export above the limit, which
    only lowers import, so it cannot pass for a cut either.
    """
    ref, settled = k - 2 - math.ceil(STEP_LIMITER_RESPONSE_S / STEP_WINDOW_S), k + 1
    if ref < 0 or settled >= len(gen):
        return False
    g0, g1, n0, n1 = gen[ref], gen[settled], net[ref], net[settled]
    if g0 is None or g1 is None or n0 is None or n1 is None:
        return False
    lost = g0 - g1
    return lost >= STEP_MIN_W and n1 - n0 >= STEP_CUT_ABSORBED * lost


def _against(volts: Sequence[Sequence[float | None]], k: int, dp: float) -> float | None:
    """The smallest relative voltage move against the power step, over every tracker.

    Positive when every tracker's voltage moved opposite to power (power up, voltage
    down, or the reverse). ``None`` when a tracker has no value either side.
    """
    sign = 1.0 if dp > 0 else -1.0
    moves: list[float] = []
    for series in volts:
        v0, v1 = series[k - 1], series[k]
        if v0 is None or v1 is None or v0 <= 0:
            return None
        moves.append(-sign * (v1 - v0) / v0)
    return min(moves)
