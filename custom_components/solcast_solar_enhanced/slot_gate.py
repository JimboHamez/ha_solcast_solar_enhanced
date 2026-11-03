"""Per-slot gate measures: what each half-hour's samples say about sky, shade and export limiting.

Record-only (schema v2): nothing reads these yet. Pure Python, no Home Assistant
imports, so it runs in the executor and is tested directly.

Everything is **time-weighted on the merged event timeline**. A recorded state
holds until the next one, so a sensor reporting at 10 Hz, at 1 Hz or only when its
value changes gives the same seconds for the same signal. Counting samples instead
would weight a fast sensor more and miss everything a slow one held.

The measures, all property-wide except ``shade_dip_s``:

* ``sky_dip_s`` — seconds every array's normalised power was in a dip at once.
  Arrays on one roof share the sky, so a coincident dip is cloud.
* ``shade_dip_s`` (per array) — seconds this array dipped while another did not.
  That is the shading signature, and step 3 must keep these slots.
* ``sky_dip_runs`` — separate sky-dip episodes. One run at the edge of a slot is
  usually a shadow edge crossing every array at low sun; several are cloud.
* ``export_pinned_s`` — seconds export sat at or above the limit's clipping band.
* ``sample_gap_s`` — median interval between samples, so a coarse slot is known
  to be coarse rather than mistaken for a smooth one.
* ``gate_flags`` — a ``GateFlag`` bit vector of what could be measured.

"Normalised power" is an array's power over its clear-sky shape, which removes the
sunrise and sunset ramp; without it every edge-of-day slot reads as a dip. A dip is
a value below ``dip_threshold`` of the array's own time-weighted p90 for the slot,
judged only outside export-pinned time (a limited inverter is not a cloud).
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from typing import Literal, TypedDict

from .const import (
    GATE_COARSE_GAP_S,
    GATE_COUNTER_WINDOW_S,
    GATE_DIP_THRESHOLD,
    GATE_PARTIAL_FRACTION,
    GATE_RUN_MIN_S,
    GateFlag,
)

Samples = Sequence[tuple[float, float]]
SourceKind = Literal["dc", "ac", "counter"]
UNKNOWN = -1.0


class SlotMeasures(TypedDict):
    """One slot's gate measures, as stored (``-1`` = not measurable)."""

    sky_dip_s: float
    sky_dip_runs: int
    export_pinned_s: float
    sample_gap_s: float
    gate_flags: int
    shade_dip_s: dict[str, float]


def dc_power(trackers: Sequence[tuple[Samples, Samples]]) -> list[tuple[float, float]]:
    """One array's DC power (W) on the merged timeline of its trackers' V and I samples.

    Each tracker contributes ``V × I`` with both held until their next sample; an
    array wired across several trackers is their sum.
    """
    series = [s for pair in trackers for s in pair]
    if not series or any(not s for s in series):
        return []
    times = sorted({t for s in series for t, _ in s})
    held = [(_holder(v), _holder(i)) for v, i in trackers]
    out: list[tuple[float, float]] = []
    for t in times:
        total = 0.0
        for volts, amps in held:
            v, i = volts(t), amps(t)
            if v is None or i is None:
                break
            total += v * i
        else:
            out.append((t, total))
    return out


def counter_power(
    samples: Samples, window_s: float = GATE_COUNTER_WINDOW_S, end: float | None = None
) -> list[tuple[float, float]]:
    """A cumulative energy counter (kWh) as average power (kW) over windows of at least ``window_s``.

    Differencing adjacent samples would turn the counter's resolution into spikes
    (a 10 Wh tick 0.3 s after the last read is 120 kW), so each step spans at least
    a full window; the error is then bounded by resolution ÷ window.

    With ``end``, the last reading is held until then, because a recorded state holds
    until the next one. The recorder logs a counter only when it changes, so one that
    never ticks in the slot (export while the house uses every watt) has a single
    reading: without the hold it yields no series and the slot reads as unmeasured,
    when it measured exactly 0 kW. The hold also counts the minutes after the last
    tick. Pass ``end`` only while the sensor is still reporting: a counter that went
    unavailable is unknown, not flat.
    """
    out: list[tuple[float, float]] = []
    if end is not None and samples and samples[-1][0] < end:
        samples = [*samples, (end, samples[-1][1])]
    if len(samples) < 2:
        return out
    anchor_t, anchor_v = samples[0]
    for t, v in samples[1:]:
        dt = t - anchor_t
        if dt < window_s:
            continue
        if v >= anchor_v:  # A reset or rollover yields no reading for this window.
            out.append((anchor_t, (v - anchor_v) / (dt / 3600.0)))
        anchor_t, anchor_v = t, v
    return out


def measure_slot(
    start: float,
    end: float,
    arrays: Mapping[str, Samples],
    kinds: Mapping[str, SourceKind],
    shape: Callable[[str, float], float] | None,
    export: Samples | None = None,
    export_kind: SourceKind = "ac",
    export_limit_kw: float = 0.0,
    clipping_threshold: float = 0.95,
    dip_threshold: float = GATE_DIP_THRESHOLD,
    low_sun: bool = False,
) -> SlotMeasures:
    """Measure one slot. See the module docstring for what each value means.

    Args:
        start: Slot start, epoch seconds.
        end: Slot end, epoch seconds.
        arrays: Per-array power samples (any unit; only ratios are used), keyed by site.
        kinds: How each array's series was obtained, for the flags.
        shape: Clear-sky plane-of-array shape per array and time, or None to judge
            dips against the raw power (``POA_SHAPE`` stays unset).
        export: Grid export samples in kW, if a series exists.
        export_kind: How the export series was obtained; ``counter`` sets ``EXPORT_COUNTER``.
        export_limit_kw: The limit in force; 0 or less means none.
        clipping_threshold: Share of the limit that counts as pinned.
        dip_threshold: Share of the array's p90 below which it is in a dip.
        low_sun: Whether the sun is low enough for shadow edges to be ambiguous.

    Returns:
        The stored measures.
    """
    span = end - start
    flags = GateFlag(0)
    if any(k == "dc" for k in kinds.values()):
        flags |= GateFlag.DC_TRACKERS
    if any(k in ("ac", "counter") for k in kinds.values()):
        flags |= GateFlag.AC_ARRAYS
    if any(k == "counter" for k in kinds.values()):
        flags |= GateFlag.COUNTER_SOURCE
    if export and export_kind == "counter":
        flags |= GateFlag.EXPORT_COUNTER
    if low_sun:
        flags |= GateFlag.LOW_SUN

    live = {site: s for site, s in arrays.items() if s}
    inputs = list(live.values()) + ([export] if export else [])
    gap = _median_gap(inputs, start, end)
    if gap is not None and gap > GATE_COARSE_GAP_S:
        flags |= GateFlag.COARSE
    if any(_uncovered(s, start, end) > GATE_PARTIAL_FRACTION * span for s in inputs) or len(live) < len(arrays):
        flags |= GateFlag.PARTIAL

    pinned_hold = None
    if export and export_limit_kw > 0:
        flags |= GateFlag.EXPORT_SERIES
        pinned_hold = _holder(export)
    threshold_kw = export_limit_kw * clipping_threshold

    if not live:
        return {
            "sky_dip_s": UNKNOWN,
            "sky_dip_runs": int(UNKNOWN),
            "export_pinned_s": _pinned_only(export, start, end, threshold_kw) if pinned_hold else UNKNOWN,
            "sample_gap_s": gap if gap is not None else UNKNOWN,
            "gate_flags": int(flags),
            "shade_dip_s": {},
        }
    if len(live) >= 2:
        flags |= GateFlag.CROSS_REF
    if shape is not None:
        flags |= GateFlag.POA_SHAPE

    sites = list(live)
    holders = {site: _holder(live[site]) for site in sites}
    events = sorted({start} | {t for s in inputs for t, _ in s if start < t < end})
    shape_cache: dict[tuple[str, int], float] = {}

    def norm(site: str, t: float, value: float) -> float:
        if shape is None:
            return value
        key = (site, int(t // 60))  # The clear-sky shape is smooth: one evaluation a minute.
        if key not in shape_cache:
            shape_cache[key] = max(shape(site, key[1] * 60.0 + 30.0), 1e-6)
        return value / shape_cache[key]

    # Walk the timeline once: each piece is (duration, pinned, {site: normalised power}).
    pieces: list[tuple[float, bool, dict[str, float | None]]] = []
    pinned_s = 0.0
    for k, t in enumerate(events):
        nxt = events[k + 1] if k + 1 < len(events) else end
        dur = nxt - t
        if dur <= 0:
            continue
        pinned = False
        if pinned_hold is not None:
            e = pinned_hold(t)
            pinned = e is not None and e >= threshold_kw
        if pinned:
            pinned_s += dur
        values: dict[str, float | None] = {}
        for site in sites:
            v = holders[site](t)
            values[site] = None if v is None else norm(site, t, v)
        pieces.append((dur, pinned, values))

    p90 = {site: _weighted_quantile([(d, v[site]) for d, p, v in pieces if not p], 0.9) for site in sites}
    sky = 0.0
    runs = 0
    run_len = 0.0
    shade = dict.fromkeys(sites, 0.0)
    for dur, pinned, values in pieces:
        dips = {site: _in_dip(values[site], p90[site], dip_threshold, pinned) for site in sites}
        everyone = all(dips.values())
        if everyone:
            sky += dur
            run_len += dur
        else:
            if run_len >= GATE_RUN_MIN_S:
                runs += 1
            run_len = 0.0
            for site in sites:
                if dips[site]:
                    shade[site] += dur
    if run_len >= GATE_RUN_MIN_S:
        runs += 1

    return {
        "sky_dip_s": round(sky, 1),
        "sky_dip_runs": runs,
        "export_pinned_s": round(pinned_s, 1) if pinned_hold is not None else UNKNOWN,
        "sample_gap_s": gap if gap is not None else UNKNOWN,
        "gate_flags": int(flags),
        # A single array has nothing to compare against: shade and cloud look alike.
        "shade_dip_s": {site: round(v, 1) for site, v in shade.items()}
        if len(sites) >= 2
        else dict.fromkeys(sites, UNKNOWN),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _holder(samples: Samples) -> Callable[[float], float | None]:
    """The value holding at ``t``: the latest sample at or before it, else None."""
    times = [t for t, _ in samples]
    values = [v for _, v in samples]

    def at(t: float) -> float | None:
        i = bisect.bisect_right(times, t) - 1
        return values[i] if i >= 0 else None

    return at


def _in_dip(value: float | None, p90: float, dip_threshold: float, pinned: bool) -> bool:
    """A dip: below the threshold share of the slot's p90, outside export-pinned time."""
    return not pinned and value is not None and p90 > 0 and value < dip_threshold * p90


def _median_gap(series: Sequence[Samples], start: float, end: float) -> float | None:
    gaps = [b - a for s in series for (a, _), (b, _) in pairwise(s) if b > a and start <= b <= end]
    return round(statistics.median(gaps), 2) if gaps else None


def _uncovered(samples: Samples, start: float, end: float) -> float:
    """Seconds of the slot before the series has any value."""
    first = samples[0][0] if samples else end
    return max(0.0, min(first, end) - start)


def _pinned_only(export: Samples | None, start: float, end: float, threshold_kw: float) -> float:
    if not export:
        return UNKNOWN
    hold = _holder(export)
    events = sorted({start} | {t for t, _ in export if start < t < end})
    total = 0.0
    for k, t in enumerate(events):
        nxt = events[k + 1] if k + 1 < len(events) else end
        e = hold(t)
        if e is not None and e >= threshold_kw:
            total += nxt - t
    return round(total, 1)


def _weighted_quantile(pairs: Sequence[tuple[float, float | None]], q: float) -> float:
    """Time-weighted quantile of ``(duration, value)`` pairs; 0 when there is nothing to weigh."""
    data = sorted((v, d) for d, v in pairs if v is not None and d > 0)
    total = sum(d for _, d in data)
    if total <= 0:
        return 0.0
    target = q * total
    acc = 0.0
    for v, d in data:
        acc += d
        if acc >= target:
            return v
    return data[-1][0]
