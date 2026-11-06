#!/usr/bin/env python3
"""Audit the dampening slot gates (Kt clear-sky gate and export-cap test) against the DC telemetry.

Why this exists
---------------
Dampening decides which half-hours are fit to measure shading with two gates, and
neither looks at the panels:

* the **Kt clear-sky gate** judges the sky from Open-Meteo's modelled GHI. In
  Australia its ``minutely_15`` radiation is interpolated from hourly output, so a
  half-hour that was 20 minutes clear and 10 minutes cloudy can still score
  Kt >= 0.75. Cloud-edge enhancement hides the dip further.
* the **export-cap test** (``_export_ceiling``) calls a slot capped when the
  interval's peak export reaches ``limit x clipping_threshold``. A slot whose
  curtailment never lifts the 5-minute peak that far is booked as shading.

The DC trackers record what physically happened. This script classifies every
daylight half-hour from them, then cross-tabulates that verdict against each gate,
so "is the Kt gate working?" gets a number rather than an inference.

DC verdict per slot (judged on the ``_total`` row, which carries every tracker):

* ``curtailed`` — the inverter left the maximum power point, on either of two
  pieces of evidence (``source`` says which):

  - ``median-jump``: the slot's median voltage at least ``--trace-dv`` above, and
    median current at least ``--trace-di`` below, the nearest earlier healthy slot,
    on every tracker. Validated against the 22 Oct 2026 1 Hz trace: it flagged the
    four fully limited slots and nothing else. It is blind to a limit that binds for
    only part of a slot (the median stays at MPP). An earlier max-voltage-over-median
    proxy was dropped: it missed every fully limited slot and flagged 26 broken-cloud
    slots as curtailment.
  - ``offmpp``: the per-sample ``dc_offmpp_min`` column (1.15.0b2+), where present.
  The cause is attributed for reporting only: ``export`` (peak export near the
  limit), ``ac-clip`` (output at the inverter's AC rating), ``inverter`` (the
  inverter-limited binary sensor fired) or ``unattributed`` (none of those — a
  DOE/backstop, an export spike too short for the stored 5-minute peak to register,
  such as a large load switching off, or a false positive).
  **The shipped ``dc_offmpp_min`` misses moderate export limiting.** It needs
  current below ``OFFMPP_CURRENT_RATIO`` (0.3x) of the reference, which suits a
  near-zero generation ceiling. A Fronius GEN24 holding a 5 kW export limit on 22 Oct
  2026 raised V 7-9% and cut I 23-27%, so the column read 0 for two fully limited
  hours. It also refreshes its reference from any slot that was off-MPP for less
  than half the slot, so it then adopts the parked point. Use ``--trace`` for a
  per-sample verdict that does not depend on it.
* ``cloud`` — at MPP, and every tracker's minimum current fell below
  ``--dip-threshold`` of its slot median in the same slot, after removing the
  clear-sky ramp across the slot (otherwise every sunrise half-hour reads as cloud).
  Multiple arrays see one sky, so a coincident dip is the sky. **Caveat:** the
  stored minimum and median cannot say *when* in the slot each dip happened, nor
  whether it oscillated (cloud) or moved one way (a shadow edge). Where every array
  has its own obstruction at the same sun position — typically low morning or
  evening sun — a shadow edge crossing all of them in one half-hour reads as
  ``cloud``. Treat a ``-cloud`` change at the edges of the day as an upper bound;
  the midday verdict does not have this problem.
* ``one-array`` — some trackers dipped but not all. That is the shading signature
  and must stay in (a filter that dropped it would erase the real shading the
  integration exists to measure). On a single-tracker install a dip cannot be told
  from cloud and is reported as ``cloud``.
* ``smooth`` — at MPP, no dip.
* ``no-dc`` — no DC telemetry on the row.

Read-only. Calls the shipped ``compute_dampening`` / ``_clearness`` / ``_export_ceiling``
/ ``_kt_weight`` directly, so the gate verdicts and the replayed factors are exactly
what the integration computes.

Usage
-----
    python tools/slot_validity_audit.py --db config/solcast_solar_enhanced.db \\
        --lat -37.9 --lon 145.0 --tz Australia/Melbourne --export-limit 5 \\
        --capacity _total=8 --capacity <resource_id>=4 --since 2026-09-15

``--date`` picks the replay day (default: the last day in the DB). ``--csv`` writes
the per-slot classification for plotting.

Per-sample trace
----------------
The stored row keeps only extremes and medians. A Home Assistant history CSV export
(``entity_id,state,last_changed``) of the export power and each tracker's DC V/I
gives the deterministic answer for the slots it covers (section 5). Export one day per
file: for a long range the HA history panel returns hourly statistics, not states.
Traced slots also replace the stored verdict in the replay (section 4):

    python tools/slot_validity_audit.py ... --since 2026-10-22 --until 2026-10-22 \\
        --trace day1.csv day2.csv ... --trace-export sensor.power_grid_export \\
        --trace-mppt sensor.dc_voltage_mppt_1,sensor.dc_current_mppt_1 \\
        --trace-mppt sensor.dc_voltage_mppt_2,sensor.dc_current_mppt_2 \\
        --tilt 24.75 --azimuth -6

Flags per slot: ``MISSED-CAP`` (curtailed per sample, not flagged capped, so booked
as shading), ``FALSE-CAP``, ``KT-LEAK`` (admitted by Kt, coincident dip per sample),
``KT-REJECT`` (rejected by Kt, smooth per sample), ``PROXY-CURT`` (the stored-row
median-jump test disagrees with the per-sample curtailment verdict) and ``OFFMPP``
(the stored ``dc_offmpp_min`` disagrees with it). ``--tilt``/``--azimuth``
normalise the dip test by the clear-sky plane-of-array shape. Without them the GHI
shape leaves a sunrise/sunset ramp that reads as a dip on every array.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import sqlite3
import sys
from array import array
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.solcast_solar_enhanced.const import (  # noqa: E402
    CURTAILMENT_UNKNOWN,
    DAMPENING_WINDOW_BACK_DAYS,
    DAMPENING_WINDOW_FORWARD_DAYS,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_CLOUD_THRESHOLD,
    DEFAULT_KT_THRESHOLD,
    DEFAULT_SITE_ID,
    KT_ZENITH_MAX,
)
from custom_components.solcast_solar_enhanced.pv_tuning import (  # noqa: E402
    clearsky_ghi,
    cos_incidence,
    panel_azimuth_to_internal,
    solar_position,
)
from custom_components.solcast_solar_enhanced.shading_dampening import (  # noqa: E402
    _clearness,
    _export_ceiling,
    _kt_weight,
    _record_estimate,
    compute_dampening,
)

# Columns added after the original schema. A read-only open of an older DB lacks
# some of them, so each is substituted with its "unknown" sentinel when absent —
# the same contract as SqliteStore._optional_column.
OPTIONAL_COLUMNS: dict[str, float] = {
    "ghi": 0.0,
    "pv_export_max": 0.0,
    "pv_estimate_undampened": 0.0,
    "dc_voltage1": 0.0,
    "dc_voltage2": 0.0,
    "dc_current1": 0.0,
    "dc_current2": 0.0,
    "dc_vmed1": 0.0,
    "dc_vmed2": 0.0,
    "dc_imed1": 0.0,
    "dc_imed2": 0.0,
    "dc_offmpp_min": CURTAILMENT_UNKNOWN,
    "inverter_limited": CURTAILMENT_UNKNOWN,
    "export_limit": CURTAILMENT_UNKNOWN,
}
BASE_COLUMNS = (
    "period_end_epoch",
    "site",
    "pv_actual",
    "pv_export",
    "pv_estimate",
    "pv_estimate10",
    "pv_estimate90",
    "azimuth",
    "zenith",
    "clouds",
)
TRACKERS = (1, 2)
DC_VERDICTS = ("smooth", "one-array", "cloud", "curtailed", "no-dc")
KT_VERDICTS = ("clear", "graded", "rejected", "no-kt")
SLOT_S = 1800


# --------------------------------------------------------------------------- loading


def load_rows(db: Path) -> list[dict[str, Any]]:
    """Every row in the store, with absent optional columns filled by their sentinel."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # Schema v2 stores read through the slot_rows view (the only source once compacted).
    table = "slot_rows" if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'slot_rows'").fetchone() else "solcast_data"
    present = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    select = list(BASE_COLUMNS)
    select += [c if c in present else f"{default!r} AS {c}" for c, default in OPTIONAL_COLUMNS.items()]
    rows = con.execute(f"SELECT {', '.join(select)} FROM {table} ORDER BY period_end_epoch").fetchall()
    con.close()
    missing = [c for c in OPTIONAL_COLUMNS if c not in present]
    if missing:
        print(f"note: DB predates columns {', '.join(missing)}; they read as unknown.\n")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- DC classification


def slot_ramp(end_epoch: int, lat: float, lon: float) -> float:
    """Clear-sky min/median current over a slot — what an unclouded slot would show.

    ``dc_current`` is the slot minimum and ``dc_imed`` its median. Under a clear sky
    the minimum still sits below the median whenever irradiance is rising or falling
    across the half-hour (sunrise, sunset), so the raw ratio reads every edge-of-day
    slot as cloud. Dividing by this expected ratio removes the ramp.
    """
    css = [
        clearsky_ghi(solar_position(t, lat, lon)[1]) for t in (end_epoch - SLOT_S, end_epoch - SLOT_S // 2, end_epoch)
    ]
    mid = css[1]
    if mid <= 0:
        return 1.0
    return max(0.05, min(1.0, min(css[0], css[2]) / mid))


def median_jump(
    r: dict[str, Any],
    by_epoch: dict[int, dict[str, Any]],
    export_limit: float,
    near_limit_frac: float,
    look: int,
    shape: Callable[[float], float],
) -> tuple[float, float] | None:
    """Slot-median V and I against the nearest earlier healthy slot: ``(dV, dI)`` as fractions.

    Export limiting walks the operating point up the I-V curve for as long as it
    lasts, so the *median* voltage of a limited slot sits well above the last
    unlimited one while the median current sits below it. A cloud cuts current with
    voltage flat or lower. ``dV`` is the smallest rise across trackers and ``dI``
    the smallest fall, so both must hold on every tracker. The reference is the
    nearest earlier slot within ``look`` half-hours whose peak export stayed under
    the near-limit band, so a run of limited slots never becomes its own reference.
    The reference current is scaled by the clear-sky irradiance shape between the
    two slots, so the afternoon decline is not read as curtailment. Voltage is not
    temperature-compensated: cells cooling through the afternoon sit a few per cent
    above a hot midday reference, which is why ``look`` defaults to 2 h.
    Returns ``None`` when there is no such slot.
    """
    trackers = [i for i in TRACKERS if r[f"dc_imed{i}"] > 0 and r[f"dc_vmed{i}"] > 0]
    if not trackers:
        return None
    ceiling = export_limit * near_limit_frac if export_limit > 0 else float("inf")
    end = int(r["period_end_epoch"])
    for k in range(1, look + 1):
        ref = by_epoch.get(end - SLOT_S * k)
        if ref is None or float(ref["pv_export_max"]) >= ceiling:
            continue
        if all(ref[f"dc_vmed{i}"] > 0 and ref[f"dc_imed{i}"] > 0 for i in trackers):
            ref_end = int(ref["period_end_epoch"])
            scale = shape(end - SLOT_S / 2) / max(shape(ref_end - SLOT_S / 2), 1.0)
            dv = min(r[f"dc_vmed{i}"] / ref[f"dc_vmed{i}"] for i in trackers) - 1
            di = max(r[f"dc_imed{i}"] / (ref[f"dc_imed{i}"] * scale) for i in trackers) - 1
            return dv, di
    return None


def dc_verdict(
    r: dict[str, Any],
    ramp: float,
    jump: tuple[float, float] | None,
    args: argparse.Namespace,
    ac_clip_kw: float,
) -> tuple[str, str, str]:
    """Classify one ``_total`` row from its DC telemetry.

    Returns ``(verdict, cause, source)``. ``cause`` is set only for ``curtailed``;
    ``source`` names the evidence for the curtailment call: ``offmpp`` (the stored
    per-sample column), ``median-jump`` (slot medians against the last healthy slot),
    ``both``, or ``none`` when neither fired.
    """
    trackers = [i for i in TRACKERS if r[f"dc_imed{i}"] > 0 and r[f"dc_vmed{i}"] > 0]
    if not trackers:
        return "no-dc", "", ""
    dips = [(r[f"dc_current{i}"] / r[f"dc_imed{i}"]) / ramp < args.dip_threshold for i in trackers]

    offmpp = float(r["dc_offmpp_min"])
    by_offmpp = offmpp >= args.offmpp_minutes
    by_jump = jump is not None and jump[0] >= args.trace_dv and jump[1] <= -args.trace_di
    source = "both" if by_offmpp and by_jump else "offmpp" if by_offmpp else "median-jump" if by_jump else "none"
    curtailed = by_offmpp or by_jump
    export_limit, near_limit_frac = args.export_limit, args.near_limit_frac

    if curtailed:
        peak = float(r["pv_export_max"])
        if float(r["inverter_limited"]) > 0:
            cause = "inverter"
        elif export_limit > 0 and peak >= export_limit * near_limit_frac:
            cause = "export"
        elif ac_clip_kw > 0 and float(r["pv_actual"]) >= ac_clip_kw:
            cause = "ac-clip"
        else:
            cause = "unattributed"
        return "curtailed", cause, source
    if all(dips):
        return "cloud", "", source
    if any(dips):
        return "one-array", "", source
    return "smooth", "", source


def kt_verdict(r: dict[str, Any], kt_threshold: float, cloud_threshold: int) -> tuple[str, float, float | None]:
    """The production clear-sky verdict for a row: ``(verdict, weight, kt)``."""
    zenith = float(r.get("zenith", 90) or 90)
    kt, _clouds, is_clear = _clearness(r, zenith, cloud_threshold, kt_threshold)
    if kt is None:
        return "no-kt", 0.0, None
    weight = _kt_weight(kt, kt_threshold)
    if is_clear:
        return "clear", weight, kt
    return ("graded" if weight > 0 else "rejected"), weight, kt


# ------------------------------------------------------------------- aggregation


class Bucket:
    """Running energy totals for one cell of a cross-tab."""

    __slots__ = ("actual", "eff", "est", "n", "w_est")

    def __init__(self) -> None:
        self.n = 0
        self.actual = 0.0
        self.est = 0.0
        self.eff = 0.0
        self.w_est = 0.0

    def add(self, actual: float, est: float, eff: float, weight: float) -> None:
        self.n += 1
        self.actual += actual
        self.est += est
        self.eff += eff
        self.w_est += weight * est * SLOT_S / 3600  # mean kW over the half-hour -> kWh

    def ratio(self) -> str:
        return f"{self.actual / self.est:.3f}" if self.est > 0 else "  -  "

    def eff_ratio(self) -> str:
        return f"{self.actual / self.eff:.3f}" if self.eff > 0 else "  -  "


def table(title: str, header: list[str], rows: Iterable[list[str]]) -> None:
    rows = list(rows)
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(header)]
    print(title)
    print("  " + "  ".join(h.rjust(w) for h, w in zip(header, widths, strict=True)))
    for r in rows:
        print("  " + "  ".join(c.rjust(w) for c, w in zip(r, widths, strict=True)))
    print()


def band_of(local: datetime) -> str:
    h = local.hour + local.minute / 60
    return "am (<10:30)" if h < 10.5 else ("midday" if h <= 15.0 else "pm (>15:00)")


# -------------------------------------------------------------------------- report


def audit(slots: list[dict[str, Any]], dc_source_counts: dict[str, int], export_limit: float) -> None:
    """Print the Kt-gate and export-cap cross-tabs."""
    print(
        f"Daylight slots audited: {len(slots)}  "
        "(curtailment evidence: "
        + ", ".join(f"{k or 'no-dc'}={v}" for k, v in sorted(dc_source_counts.items()))
        + ")\n"
    )

    # --- Kt gate: how much of the energy it admits did the DC say was cloudy?
    by_kt: dict[tuple[str, str], Bucket] = defaultdict(Bucket)
    for s in slots:
        if s["dc"] == "curtailed":
            continue  # judged in the export section; mixing them muddies the sky question
        by_kt[(s["kt"], s["dc"])].add(s["actual"], s["est"], s["eff"], s["kt_weight"])
    rows = []
    for kv in KT_VERDICTS:
        for dv in DC_VERDICTS:
            b = by_kt.get((kv, dv))
            if b and b.n:
                rows.append([kv, dv, str(b.n), b.ratio(), f"{b.w_est:.1f}"])
    table(
        "1. Kt gate vs DC sky verdict (uncurtailed slots). ratio = actual / undampened forecast; "
        "weighted kWh = forecast energy x Kt weight, i.e. what the gate lets into the estimator.",
        ["Kt", "DC", "n", "ratio", "weighted kWh"],
        rows,
    )
    admitted = sum(b.w_est for (kv, _dv), b in by_kt.items() if kv in ("clear", "graded"))
    leaked = sum(b.w_est for (kv, dv), b in by_kt.items() if kv in ("clear", "graded") and dv == "cloud")
    dc_known = sum(b.w_est for (kv, dv), b in by_kt.items() if kv in ("clear", "graded") and dv != "no-dc")
    smooth_rejected = sum(b.n for (kv, dv), b in by_kt.items() if kv == "rejected" and dv == "smooth")
    smooth_total = sum(b.n for (_kv, dv), b in by_kt.items() if dv == "smooth")
    if dc_known > 0:
        print(
            f"   Kt leak: {leaked / dc_known:.1%} of the weighted forecast energy the gate admits "
            f"(where DC is known) came from slots the DC calls cloud ({leaked:.1f} of {dc_known:.1f} kWh; "
            f"{admitted:.1f} kWh admitted in all)."
        )
    if smooth_total:
        print(
            f"   Kt over-rejection: {smooth_rejected} of {smooth_total} DC-smooth slots "
            f"({smooth_rejected / smooth_total:.1%}) were rejected outright by the gate."
        )
    print()

    # --- Kt leak by time of day: where in the curve does it land?
    by_band: dict[tuple[str, str], Bucket] = defaultdict(Bucket)
    for s in slots:
        if s["dc"] != "curtailed" and s["kt"] in ("clear", "graded") and s["dc"] != "no-dc":
            by_band[(s["band"], s["dc"])].add(s["actual"], s["est"], s["eff"], s["kt_weight"])
    rows = []
    for band in ("am (<10:30)", "midday", "pm (>15:00)"):
        cells = {dv: by_band.get((band, dv)) for dv in ("smooth", "one-array", "cloud")}
        tot = sum(b.w_est for b in cells.values() if b)
        cloud = cells["cloud"]
        rows.append(
            [band]
            + [f"{b.n}/{b.ratio()}" if b else "0/  -  " for b in cells.values()]
            + [f"{(cloud.w_est / tot) if cloud and tot else 0:.1%}"]
        )
    table(
        "2. Kt-admitted, uncurtailed slots by time of day (cells: n/ratio).",
        ["band", "smooth", "one-array", "cloud", "leak share"],
        rows,
    )

    # --- Export-cap test vs DC curtailment.
    if export_limit <= 0:
        print("3. Export-cap audit skipped: no --export-limit given.\n")
        return
    by_cap: dict[tuple[str, str], Bucket] = defaultdict(Bucket)
    for s in slots:
        if s["kt"] not in ("clear", "graded"):
            continue  # only slots the estimator actually sees
        dc = f"curtailed/{s['cause']}" if s["dc"] == "curtailed" else ("at-MPP" if s["dc"] != "no-dc" else "no-dc")
        by_cap[("capped" if s["capped"] else "not capped", dc)].add(s["actual"], s["est"], s["eff"], s["kt_weight"])
    rows = [[cap, dc, str(b.n), b.ratio(), b.eff_ratio()] for (cap, dc), b in sorted(by_cap.items()) if b.n]
    table(
        "3. Export-cap test vs DC curtailment (Kt-admitted slots). eff ratio = actual / forecast after "
        "_export_ceiling, i.e. what the estimator books.",
        ["export test", "DC", "n", "raw ratio", "eff ratio"],
        rows,
    )
    missed = by_cap.get(("not capped", "curtailed/export"))
    if missed and missed.n:
        print(
            f"   Missed export curtailment: {missed.n} slots the DC shows curtailed near the export limit "
            f"were not flagged capped; they book {missed.eff_ratio()} as shading."
        )
    false_cap = by_cap.get(("capped", "at-MPP"))
    if false_cap and false_cap.n:
        print(
            f"   Capped but DC at-MPP: {false_cap.n} slots. Mostly a limit that bound for only part of the slot "
            "(the medians stay at MPP, so median-jump cannot see it); --trace says which."
        )
    unattr = sum(b.n for (_c, dc), b in by_cap.items() if dc == "curtailed/unattributed")
    if unattr:
        print(
            f"   Unattributed curtailment: {unattr} slots off-MPP with export, AC output and the inverter flag "
            "all clear. With a static export limit: a DOE/backstop, a short export spike, or a false positive."
        )
    print()


# -------------------------------------------------------------------------- replay


def window_records(rows: list[dict[str, Any]], site: str, target: date, cutoff_epoch: int) -> list[dict[str, Any]]:
    """The production dampening query for ``site`` on ``target``, as of ``cutoff_epoch``.

    Mirrors ``async_get_records_for_dampening``: UTC day-of-year, signed and wrapped,
    28 back / 14 forward. The cutoff keeps the replay walk-forward — records after
    the push could not have informed it.
    """
    tdoy = target.timetuple().tm_yday
    out = []
    for r in rows:
        if r["site"] != site or r["pv_actual"] <= 0 or r["pv_estimate"] <= 0 or r["period_end_epoch"] > cutoff_epoch:
            continue
        doy = datetime.fromtimestamp(r["period_end_epoch"], UTC).timetuple().tm_yday
        delta = (((doy - tdoy) + 548) % 366) - 182
        if -DAMPENING_WINDOW_BACK_DAYS <= delta <= DAMPENING_WINDOW_FORWARD_DAYS:
            out.append(r)
    return out


def replay(
    rows: list[dict[str, Any]],
    verdicts: dict[int, dict[str, Any]],
    sites: list[str],
    capacities: dict[str, float],
    target: date,
    tz: tzinfo,
    args: argparse.Namespace,
) -> None:
    """Re-run the shipped estimator on ``target`` with the DC-flagged slots removed."""

    def v(r: dict[str, Any]) -> dict[str, Any]:
        return verdicts.get(r["period_end_epoch"], {})

    def missed(r: dict[str, Any]) -> bool:
        x = v(r)
        return x.get("dc") == "curtailed" and not x.get("capped")

    variants: dict[str, Callable[[dict[str, Any]], bool]] = {
        "as-is": lambda r: True,
        "-cloud": lambda r: v(r).get("dc") != "cloud",
        "-missed curt": lambda r: not missed(r),
        "-cloud -missed": lambda r: v(r).get("dc") != "cloud" and not missed(r),
        "-all curtailed": lambda r: v(r).get("dc") not in ("cloud", "curtailed"),
    }
    day_start = datetime(target.year, target.month, target.day, tzinfo=tz)
    cutoff = int((day_start + timedelta(days=1)).timestamp())
    print(
        f"4. Replay of {target.isoformat()} through compute_dampening. Cells: factor/alpha. "
        "'-cloud' drops DC-cloud slots; '-missed curt' drops DC-curtailed slots the export test did not cap; "
        "'-all curtailed' also drops the capped ones (today they count as a neutral 1.0)."
    )
    traced = sum(1 for v in verdicts.values() if v.get("from_trace"))
    print(f"   {traced} slots are judged by the per-sample trace, the rest by the stored row.\n" if traced else "")
    for site in sites:
        records = window_records(rows, site, target, cutoff)
        cap = capacities.get(site) or max((r["pv_actual"] for r in rows if r["site"] == site), default=0.0)
        header = ["slot", *variants]
        out = []
        for slot in range(48):
            local = day_start + timedelta(minutes=30 * slot)
            az, zen = solar_position(int(local.timestamp()) + SLOT_S // 2, args.lat, args.lon)
            if zen >= KT_ZENITH_MAX:
                continue
            cells = [local.strftime("%H:%M")]
            for keep in variants.values():
                res = compute_dampening(
                    [r for r in records if keep(r)],
                    cap,
                    args.cloud_threshold,
                    args.cloud_max_include,
                    args.clipping_threshold,
                    zen,
                    az,
                    args.export_limit,
                    args.kt_threshold,
                )
                cells.append(f"{res['factor']:.3f}/{res['alpha']:.2f}")
            out.append(cells)
        table(f"   site {site}  (capacity {cap:.2f} kW, {len(records)} window records)", header, out)


# --------------------------------------------------------------------------- trace


class Trace:
    """Home Assistant history CSV exports (``entity_id,state,last_changed``) as step-held series.

    Several files may be given (one per day is what the HA history panel produces at
    full resolution; a long range comes back as hourly statistics instead). Only the
    entities asked for are kept, as compact arrays, so ten days of 1 Hz data fit
    comfortably. Overlapping or duplicated files are merged and de-duplicated.
    Coverage is judged per file, so a missing day between two files is a gap rather
    than one stale value held across it.
    """

    def __init__(self, paths: list[Path], entities: set[str]) -> None:
        raw: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.spans: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for path in paths:
            lo: dict[str, float] = {}
            hi: dict[str, float] = {}
            with path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    entity = row["entity_id"]
                    if entity not in entities:
                        continue
                    ts = datetime.fromisoformat(row["last_changed"].replace("Z", "+00:00")).timestamp()
                    lo[entity] = min(lo.get(entity, ts), ts)
                    hi[entity] = max(hi.get(entity, ts), ts)
                    try:
                        raw[entity].append((ts, float(row["state"])))
                    except ValueError:
                        continue  # unavailable / unknown
            for entity, first in lo.items():
                self.spans[entity].append((first, hi[entity]))
        self.times: dict[str, array] = {}
        self.values: dict[str, array] = {}
        for entity, pairs in raw.items():
            merged = sorted(set(pairs))
            self.times[entity] = array("d", (t for t, _ in merged))
            self.values[entity] = array("d", (v for _, v in merged))
        missing = entities - set(self.times)
        if missing:
            print(f"note: trace has no samples for {', '.join(sorted(missing))}.\n")

    def covers(self, entity: str, start: float, end: float) -> bool:
        """Whether one file's span for ``entity`` contains the slot (to within a minute)."""
        return any(lo <= start + 60 and hi >= end - 60 for lo, hi in self.spans.get(entity, []))

    def maximum(self, entity: str) -> float:
        return max(self.values[entity]) if self.values.get(entity) else 0.0

    def at(self, entity: str, t: float) -> float | None:
        """The value holding at ``t`` (each recorded state holds until the next)."""
        i = bisect.bisect_right(self.times[entity], t) - 1
        return self.values[entity][i] if i >= 0 else None


# Clear-sky diffuse share of GHI. Only the *shape* across a half-hour matters here,
# so a fixed split is enough to get the plane-of-array rise and fall right.
CLEAR_DIFFUSE_SHARE = 0.15


def clear_sky_shape(t: float, args: argparse.Namespace) -> float:
    """Clear-sky irradiance shape at ``t``: on the panel plane when tilt/azimuth are given, else horizontal.

    The trackers see plane-of-array light. Near sunrise and sunset a tilted array's
    POA rises and falls on a different curve from GHI, so normalising by GHI leaves
    a residual ramp that reads as a coincident dip on every array.
    """
    sun_az, zen = solar_position(int(t), args.lat, args.lon)
    ghi = clearsky_ghi(zen)
    if ghi <= 0:
        return 1.0
    if args.tilt is None or args.azimuth is None:
        return ghi
    cos_z = max(math.cos(math.radians(zen)), 0.035)
    beam = (1 - CLEAR_DIFFUSE_SHARE) * cos_incidence(args.tilt, panel_azimuth_to_internal(args.azimuth), zen, sun_az)
    sky = CLEAR_DIFFUSE_SHARE * cos_z * (1 + math.cos(math.radians(args.tilt))) / 2
    return max(ghi * (beam + sky) / cos_z, 1.0)


def _runs_at_least(flags: list[bool], min_len: int) -> int:
    """Seconds covered by runs of True at least ``min_len`` long (shorter runs are MPPT scans)."""
    total = run = 0
    for f in [*flags, False]:
        if f:
            run += 1
            continue
        if run >= min_len:
            total += run
        run = 0
    return total


def trace_slots(trace: Trace, verdicts: dict[int, dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """Judge each slot the trace covers from its per-second samples.

    * **export-pinned seconds** — export at or above ``limit x clipping_threshold``.
      Direct evidence of export limiting, whatever the stored 5-minute peak says.
    * **DC-curtailed seconds** — on every tracker at once, voltage at least
      ``--trace-dv`` above *and* current at least ``--trace-di`` below the last
      healthy slot's medians. Export limiting walks the operating point up the I-V
      curve, raising V and cutting I together; a cloud cuts I with V roughly flat.
      The reference is never refreshed from a slot that shows curtailment, so a run
      of limited slots cannot adopt the parked point as normal, and its current is
      scaled by the clear-sky shape. **Informational by default** (flag ``DC-ONLY``):
      voltage is not temperature-compensated, and cells cooling through the
      afternoon or under cloud sit 4-5% above a hot midday reference, which with the
      falling current reads as curtailment (30 Oct 2026, 16:00-17:00). The verdict
      counts pinned export only unless ``--trace-dc-curtail`` is given, which a
      static export limit makes the right ground truth. (The shipped
      ``_offmpp_minutes`` asks for I below 0.3x, which suits a near-zero ceiling but
      not a ~20% export trim, and refreshes from any slot under half curtailed.)
    * **sky-dip / one-array seconds** — at MPP, each tracker's DC power over the
      clear-sky GHI shape falls below ``--dip-threshold`` of its own slot p90:
      on every tracker at once (sky) or on some only (shade).
    """
    ex_id = args.trace_export
    mppts = [tuple(m.split(",", 1)) for m in args.trace_mppt]
    ex_scale = 1.0
    if ex_id and trace.maximum(ex_id) > 100:
        ex_scale = 1 / 1000  # recorded in W
    limit = args.export_limit
    ref: dict[str, tuple[float, float, float]] = {}
    out = []
    for end in sorted(verdicts):
        start = end - SLOT_S
        if not mppts or not all(trace.covers(v, start, end) and trace.covers(i, start, end) for v, i in mppts):
            continue
        grid = range(int(start), int(end))
        exp = (
            [((trace.at(ex_id, t) or 0.0) * ex_scale) for t in grid]
            if ex_id and trace.covers(ex_id, start, end)
            else []
        )
        pinned = [limit > 0 and e >= limit * args.clipping_threshold for e in exp]

        cs = [clear_sky_shape(t, args) for t in grid]
        volts = {v: [trace.at(v, t) or 0.0 for t in grid] for v, _ in mppts}
        amps = {i: [trace.at(i, t) or 0.0 for t in grid] for _, i in mppts}
        curtailed = [True] * len(grid)
        have_ref = True
        for v_id, i_id in mppts:
            r = ref.get(v_id)
            if r is None or end - r[2] > args.trace_ref_age_h * 3600:
                have_ref = False
                break
            v_ref, i_ref, cs_ref = r[0], r[1], clear_sky_shape(r[2] - SLOT_S / 2, args)
            for k in range(len(grid)):
                i_expected = i_ref * cs[k] / max(cs_ref, 1.0)
                if not (
                    volts[v_id][k] >= v_ref * (1 + args.trace_dv) and amps[i_id][k] <= i_expected * (1 - args.trace_di)
                ):
                    curtailed[k] = False
        if not have_ref:
            curtailed = [False] * len(grid)
        dc_curtail_s = _runs_at_least(curtailed, 60)
        pinned_s = sum(pinned)
        use_dc = args.trace_dc_curtail
        off = [(c and use_dc) or p for c, p in zip(curtailed, pinned or [False] * len(grid), strict=True)]

        dips = []
        for v_id, i_id in mppts:
            norm = [volts[v_id][k] * amps[i_id][k] / cs[k] for k in range(len(grid))]
            healthy = sorted(x for x, o in zip(norm, off, strict=True) if not o)
            p90 = healthy[int(0.9 * (len(healthy) - 1))] if healthy else 0.0
            dips.append([p90 > 0 and not off[k] and norm[k] < args.dip_threshold * p90 for k in range(len(grid))])
        sky_s = sum(all(d[k] for d in dips) for k in range(len(grid)))
        one_s = sum(any(d[k] for d in dips) and not all(d[k] for d in dips) for k in range(len(grid)))

        if pinned_s >= 60 or (use_dc and dc_curtail_s >= 60):
            verdict = "curtailed"
        elif sky_s >= args.trace_dip_s:
            verdict = "cloud"
        elif one_s >= args.trace_dip_s:
            verdict = "one-array"
        else:
            verdict = "smooth"

        # Only a slot with no sign of limiting may become the next reference.
        if pinned_s == 0 and dc_curtail_s == 0:
            for v_id, i_id in mppts:
                i_med = sorted(amps[i_id])[len(grid) // 2]
                if i_med > 0:
                    ref[v_id] = (sorted(volts[v_id])[len(grid) // 2], i_med, end)
        out.append(
            {
                **verdicts[end],
                "pinned_s": pinned_s,
                "dc_curtail_s": dc_curtail_s,
                "sky_s": sky_s,
                "one_s": one_s,
                "trace": verdict,
                "ref": have_ref,
            }
        )
    return out


def _trace_flags(s: dict[str, Any]) -> list[str]:
    """Where the stored row or a gate disagrees with the per-sample verdict."""
    admitted = s["kt"] in ("clear", "graded")
    flags = []
    if s["trace"] == "curtailed" and not s["capped"] and admitted:
        flags.append("MISSED-CAP")
    if s["capped"] and s["trace"] != "curtailed":
        flags.append("FALSE-CAP")
    if s["trace"] == "cloud" and admitted:
        flags.append("KT-LEAK")
    if s["trace"] == "smooth" and not admitted:
        flags.append("KT-REJECT")
    if (s["dc"] == "curtailed") != (s["trace"] == "curtailed"):
        flags.append("PROXY-CURT")
    if s["offmpp_min"] >= 0 and (s["offmpp_min"] >= 1) != (s["trace"] == "curtailed"):
        flags.append("OFFMPP")
    if s["dc_curtail_s"] >= 60 and s["pinned_s"] < 60:
        flags.append("DC-ONLY")
    return flags


def trace_report(tslots: list[dict[str, Any]], show_all: bool) -> None:
    """Print the per-sample verdict beside what the stored row and each gate concluded."""
    if not tslots:
        print("5. Trace: no slot is fully covered by the given entities.\n")
        return
    one_day = len({x["local"][:10] for x in tslots}) == 1
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = []
    for s in tslots:
        flags = _trace_flags(s)
        day = per_day[s["local"][:10]]
        day["slots"] += 1
        day[s["trace"]] += 1
        day["capped"] += s["capped"]
        for f in flags:
            day[f] += 1
        if not (show_all or one_day or flags):
            continue
        ratio = s["actual"] / s["est"] if s["est"] > 0 else 0.0
        rows.append(
            [
                s["local"][-5:] if one_day else s["local"],
                f"{ratio:.2f}",
                f"{s['kt_value'] if s['kt_value'] is not None else '-'}",
                "Y" if s["capped"] else "-",
                str(s["pinned_s"]),
                str(s["dc_curtail_s"]) + ("" if s["ref"] else "*"),
                "-" if s["offmpp_min"] < 0 else f"{s['offmpp_min']:g}",
                str(s["sky_s"]),
                str(s["one_s"]),
                s["dc"] + (f"/{s['cause']}" if s["cause"] else ""),
                s["trace"],
                " ".join(flags),
            ]
        )
    flag_names = ("MISSED-CAP", "FALSE-CAP", "KT-LEAK", "KT-REJECT", "PROXY-CURT", "OFFMPP", "DC-ONLY")
    table(
        "5a. Per-sample trace, per day: slot counts by trace verdict, and flags (see 5b).",
        ["day", "slots", "smooth", "one-array", "cloud", "curtailed", "capped", *flag_names],
        [
            [d, str(c["slots"]), *(str(c[k]) for k in ("smooth", "one-array", "cloud", "curtailed", "capped"))]
            + [str(c[f]) for f in flag_names]
            for d, c in sorted(per_day.items())
        ],
    )

    admitted = [s for s in tslots if s["kt"] in ("clear", "graded") and s["trace"] != "curtailed"]
    w_all = sum(s["kt_weight"] * s["est"] for s in admitted)
    w_cloud = sum(s["kt_weight"] * s["est"] for s in admitted if s["trace"] == "cloud")
    if w_all > 0:
        print(
            f"   Per-sample Kt leak: {w_cloud / w_all:.1%} of the weighted forecast energy the gate admits "
            f"came from slots the trace calls cloud ({sum(s['trace'] == 'cloud' for s in admitted)} of "
            f"{len(admitted)} admitted uncurtailed slots)."
        )
    for verdict in ("smooth", "one-array", "cloud"):
        sel = [s for s in admitted if s["trace"] == verdict]
        a, e = sum(s["actual"] for s in sel), sum(s["est"] for s in sel)
        if e > 0:
            print(f"     admitted & trace {verdict:9s}: n={len(sel):4d}  ratio {a / e:.3f}")
    print()
    table(
        "5b. "
        + ("Every traced slot" if (show_all or one_day) else "Flagged slots only (--trace-all for every slot)")
        + ". pinned / DC-curt / sky / one = seconds; offmpp = stored dc_offmpp_min (minutes); "
        "* = no healthy reference yet, so DC curtailment is not judged.",
        ["slot", "ratio", "Kt", "cap", "pinned", "DC-curt", "offmpp", "sky", "one", "stored", "trace", "flags"],
        rows,
    )


# ---------------------------------------------------------------------------- main


def parse_capacities(items: list[str]) -> dict[str, float]:
    out = {}
    for item in items:
        site, _, kw = item.partition("=")
        out[site] = float(kw)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, required=True, help="the integration's SQLite store")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--tz", default=None, help="IANA zone for local slots (default: this machine's)")
    ap.add_argument("--export-limit", type=float, default=0.0, help="property export limit, kW (0 = no export audit)")
    ap.add_argument("--capacity", action="append", default=[], metavar="SITE=KW", help="AC capacity per site")
    ap.add_argument("--since", type=date.fromisoformat, help="audit rows from this local date")
    ap.add_argument("--until", type=date.fromisoformat, help="audit rows up to this local date (inclusive)")
    ap.add_argument("--date", type=date.fromisoformat, help="replay day (default: last day in the DB)")
    ap.add_argument("--no-replay", action="store_true")
    ap.add_argument("--dip-threshold", type=float, default=0.8, help="ramp-corrected min/median current for a dip")
    ap.add_argument("--near-limit-frac", type=float, default=0.7, help="peak export / limit attributed to export")
    ap.add_argument("--jump-lookback", type=int, default=4, help="half-hours back to find a healthy reference")
    ap.add_argument("--offmpp-minutes", type=float, default=1.0, help="off-MPP minutes that make a slot curtailed")
    ap.add_argument("--min-forecast-kw", type=float, default=0.5, help="skip slots forecast below this")
    ap.add_argument("--kt-threshold", type=float, default=DEFAULT_KT_THRESHOLD)
    ap.add_argument("--cloud-threshold", type=int, default=DEFAULT_CLOUD_THRESHOLD)
    ap.add_argument("--cloud-max-include", type=int, default=DEFAULT_CLOUD_MAX_INCLUDE)
    ap.add_argument("--clipping-threshold", type=float, default=DEFAULT_CLIPPING_THRESHOLD)
    ap.add_argument("--csv", type=Path, help="write the per-slot classification here")
    tr = ap.add_argument_group("per-sample trace (HA history CSV export: entity_id,state,last_changed)")
    tr.add_argument("--trace", type=Path, nargs="+", help="history CSV(s) covering some of the DB's slots")
    tr.add_argument("--trace-export", help="grid export power entity (W or kW)")
    tr.add_argument("--trace-mppt", action="append", default=[], metavar="V_ENTITY,I_ENTITY", help="one per tracker")
    tr.add_argument(
        "--trace-dv", type=float, default=0.04, help="V rise over reference marking off-MPP (also median-jump)"
    )
    tr.add_argument(
        "--trace-di", type=float, default=0.10, help="I fall under reference marking off-MPP (also median-jump)"
    )
    tr.add_argument("--trace-ref-age-h", type=float, default=2.0, help="oldest healthy reference, hours")
    tr.add_argument(
        "--trace-dc-curtail",
        action="store_true",
        help="count DC-only curtailment in the trace verdict (default: pinned export only; DC-only is flagged)",
    )
    tr.add_argument("--trace-all", action="store_true", help="list every traced slot, not only flagged ones")
    tr.add_argument("--tilt", type=float, help="panel tilt for the clear-sky POA shape (else GHI shape)")
    tr.add_argument("--azimuth", type=float, help="panel azimuth, Solcast convention (West +, East -)")
    tr.add_argument("--trace-dip-s", type=int, default=120, help="dip seconds that make a slot cloud / one-array")
    args = ap.parse_args()

    if args.tz:
        tz: tzinfo = ZoneInfo(args.tz)
    else:
        tz = datetime.now().astimezone().tzinfo or UTC
        print(f"note: no --tz given; local slots and the am/midday/pm bands use this machine's zone ({tz}).\n")
    capacities = parse_capacities(args.capacity)
    rows = load_rows(args.db)
    if not rows:
        sys.exit("DB is empty")
    total_cap = capacities.get(DEFAULT_SITE_ID, 0.0)
    ac_clip_kw = total_cap * args.clipping_threshold if total_cap > 0 else 0.0

    # Classify every _total daylight slot once; the per-site rows share its verdict,
    # since the sky and the inverter are property-wide.
    by_epoch = {int(r["period_end_epoch"]): r for r in rows if r["site"] == DEFAULT_SITE_ID}
    verdicts: dict[int, dict[str, Any]] = {}
    slots: list[dict[str, Any]] = []
    source_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["site"] != DEFAULT_SITE_ID or float(r["zenith"]) >= KT_ZENITH_MAX:
            continue
        end = int(r["period_end_epoch"])
        local = datetime.fromtimestamp(end - SLOT_S, tz)  # label by period start, like the slot grid
        ramp = slot_ramp(end, args.lat, args.lon)
        jump = median_jump(
            r, by_epoch, args.export_limit, args.near_limit_frac, args.jump_lookback, lambda t: clear_sky_shape(t, args)
        )
        dc, cause, source = dc_verdict(r, ramp, jump, args, ac_clip_kw)
        kt, kt_w, kt_val = kt_verdict(r, args.kt_threshold, args.cloud_threshold)
        est, _undamp = _record_estimate(r)
        eff, _clipped, capped = _export_ceiling(
            r, float(r["pv_actual"]), est, args.export_limit, args.clipping_threshold
        )
        v = {
            "epoch": end,
            "local": local.strftime("%Y-%m-%d %H:%M"),
            "band": band_of(local),
            "dc": dc,
            "cause": cause,
            "source": source,
            "ramp": round(ramp, 3),
            "kt": kt,
            "kt_value": None if kt_val is None else round(kt_val, 3),
            "kt_weight": kt_w,
            "capped": capped,
            "actual": float(r["pv_actual"]),
            "est": est,
            "eff": eff,
            "export_max": float(r["pv_export_max"]),
            "offmpp_min": float(r["dc_offmpp_min"]),
        }
        verdicts[end] = v
        in_range = (args.since is None or local.date() >= args.since) and (
            args.until is None or local.date() <= args.until
        )
        if in_range and est >= args.min_forecast_kw:
            slots.append(v)
            source_counts[source] += 1

    span = f"{slots[0]['local']} .. {slots[-1]['local']}" if slots else "none"
    print(
        f"DB {args.db}  slots {span}  tz {tz}  export limit {args.export_limit} kW  "
        f"Kt threshold {args.kt_threshold}  dip threshold {args.dip_threshold}\n"
    )
    audit(slots, source_counts, args.export_limit)

    if args.csv:
        with args.csv.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(slots[0]) if slots else ["epoch"])
            w.writeheader()
            w.writerows(slots)
        print(f"wrote {len(slots)} slots to {args.csv}\n")

    if args.trace:
        in_audit = {s["epoch"] for s in slots}
        wanted = {e for m in args.trace_mppt for e in m.split(",", 1)} | (
            {args.trace_export} if args.trace_export else set()
        )
        trace = Trace(args.trace, wanted)
        tslots = trace_slots(trace, {e: v for e, v in verdicts.items() if e in in_audit}, args)
        trace_report(tslots, args.trace_all)
        # The replay judges traced slots by their per-sample verdict and the rest by
        # the stored row, so a trace sharpens the replay wherever it reaches.
        for t in tslots:
            verdicts[t["epoch"]] = {**verdicts[t["epoch"]], "dc": t["trace"], "from_trace": True}

    if not args.no_replay:
        sites = sorted({r["site"] for r in rows}, key=lambda s: (s != DEFAULT_SITE_ID, s))
        last = datetime.fromtimestamp(rows[-1]["period_end_epoch"] - SLOT_S, tz).date()
        replay(rows, verdicts, sites, capacities, args.date or last, tz, args)


if __name__ == "__main__":
    main()
