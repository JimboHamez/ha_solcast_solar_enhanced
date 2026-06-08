#!/usr/bin/env python3
"""Per-MPPT DC telemetry analysis — characterise each string's Vmp band, estimate
Voc, fit the (ambient) temperature drift, and screen for off-MPP/curtailment.

This is the exploratory groundwork for the **curtailment detector** (Tier-1,
off-MPP detection — see DESIGN_DOCUMENT.md). It reads the per-MPPT DC voltage /
current columns banked since v1.6.8 (``dc_voltage1`` / ``dc_current1`` /
``dc_voltage2`` / ``dc_current2``) and, per string, works out:

* the **Vmp band** — the operating voltage at *provably-at-MPP* slots (current
  above a floor), i.e. the band a future calibrator will learn as "normal";
* an **Voc estimate** — the highest voltage seen at ~zero current in daylight
  (open-circuit, e.g. first light), the ceiling curtailment walks toward;
* the **temperature drift** of Vmp (least-squares slope, in V/°C and %/°C) —
  noting the DB only has *ambient* (OWM) temperature, a proxy for cell temp;
* an **off-MPP / curtailment screen** — daylight slots where voltage is pushed
  up toward Voc *and* current is low while the forecast says it should be high
  (the curtailment fingerprint), kept distinct from **shading** (low current at
  a *normal* Vmp, e.g. the morning floor-shadow on a string).

Read-only, standard library only (no numpy, no Home Assistant).

Examples
--------
    # Whole-property ('_total') analysis from the built-in store
    python tools/mppt_analysis.py --sqlite config/solcast_solar_enhanced.db

    # A specific Solcast site (multi-site)
    python tools/mppt_analysis.py --sqlite config/solcast_solar_enhanced.db --site b68d-c05a

    # List off-MPP/curtailment candidates and a few shading examples
    python tools/mppt_analysis.py --sqlite config/solcast_solar_enhanced.db --list 20
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from pathlib import Path

# DC telemetry is kept per tracker (up to MAX_MPPT_TRACKERS = 2 in the integration).
TRACKERS = ((1, "dc_voltage1", "dc_current1"), (2, "dc_voltage2", "dc_current2"))
DEFAULT_SITE = "_total"
# AEST is the user's tz; kept as a CLI knob so the report's local times are useful.
DEFAULT_TZ_OFFSET = 10.0


def _connect(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise SystemExit(f"SQLite store not found: {path}")
    return sqlite3.connect(path)


def _fetch(conn: sqlite3.Connection, site: str) -> list[dict]:
    cols = [
        "period_end", "period_end_epoch", "pv_actual", "pv_export", "pv_estimate",
        "clouds", "temp", "zenith",
        "dc_voltage1", "dc_current1", "dc_voltage2", "dc_current2",
    ]
    cur = conn.cursor()
    cur.execute(
        f"SELECT {', '.join(cols)} FROM solcast_data WHERE site = ? ORDER BY period_end_epoch",
        (site,),
    )
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    return rows


def _pct(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100); empty -> nan."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    frac = k - lo
    if lo + 1 >= len(s):
        return s[-1]
    return s[lo] + (s[lo + 1] - s[lo]) * frac


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Ordinary least squares y = a + b*x; returns (a, b) or None if degenerate."""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-9:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    return a, b


def _hhmm(epoch: int, tz: float) -> str:
    import datetime as dt
    base = dt.datetime.fromtimestamp(epoch, dt.timezone.utc) + dt.timedelta(hours=tz)
    return base.strftime("%Y-%m-%d %H:%M")


def _analyse_tracker(
    rows: list[dict], idx: int, vcol: str, icol: str, args: argparse.Namespace
) -> None:
    have = [r for r in rows if (r[vcol] or 0) != 0 or (r[icol] or 0) != 0]
    print(f"\n=== MPPT string {idx} ({vcol} / {icol}) ===")
    if not have:
        print("  no telemetry captured for this tracker (all zero) — not wired, or single-MPPT inverter")
        return
    print(f"  rows with telemetry : {len(have)}  ({_hhmm(have[0]['period_end_epoch'], args.tz)}"
          f"  ..  {_hhmm(have[-1]['period_end_epoch'], args.tz)} local)")

    # --- Vmp band: provably-at-MPP = current above a floor (string is delivering) ---
    mpp = [r for r in have if (r[icol] or 0) >= args.i_min and (r[vcol] or 0) > args.v_floor]
    vmps = [r[vcol] for r in mpp]
    if vmps:
        vmp_p10, vmp_med, vmp_p90 = _pct(vmps, 10), statistics.median(vmps), _pct(vmps, 90)
        print(f"  Vmp band (I>={args.i_min} A): "
              f"p10 {vmp_p10:.0f} V | median {vmp_med:.0f} V | p90 {vmp_p90:.0f} V   (n={len(mpp)})")
        peak_i = _pct([r[icol] for r in mpp], 95)
        print(f"  peak current (p95)  : {peak_i:.2f} A")
    else:
        print("  Vmp band            : (no at-MPP samples yet — need daylight production rows)")
        return

    # --- Voc estimate: daylight, ~zero current, voltage above the Vmp band ---
    voc_cands = [
        r[vcol] for r in have
        if (r["zenith"] or 99) < args.day_zenith and (r[icol] or 0) < args.i_zero and (r[vcol] or 0) > vmp_med
    ]
    if voc_cands:
        print(f"  Voc estimate (max @ I~0, daylight): {max(voc_cands):.0f} V  (p90 {_pct(voc_cands, 90):.0f} V)")
        print(f"  off-MPP headroom (Voc - Vmp p90)  : {max(voc_cands) - vmp_p90:.0f} V")
    else:
        print("  Voc estimate        : (no open-circuit daylight samples yet)")

    # --- Temperature drift of Vmp (ambient proxy) ---
    fit = _linfit([r["temp"] for r in mpp], vmps)
    if fit:
        _, slope = fit
        print(f"  Vmp vs ambient temp : {slope:+.2f} V/°C  ({100 * slope / vmp_med:+.2f} %/°C)   "
              f"[ambient proxy — cell temp would be steeper]")
    # temperature buckets
    if len(mpp) >= 12:
        lo_t = [r[vcol] for r in mpp if (r["temp"] or 0) < args.temp_split]
        hi_t = [r[vcol] for r in mpp if (r["temp"] or 0) >= args.temp_split]
        if lo_t and hi_t:
            print(f"    median Vmp  <{args.temp_split:.0f}°C: {statistics.median(lo_t):.0f} V   "
                  f">={args.temp_split:.0f}°C: {statistics.median(hi_t):.0f} V")

    # --- Off-MPP / curtailment screen (the fingerprint we ultimately detect) ---
    # Curtailment: forecast says produce, but voltage is pushed up toward Voc and
    # current is low. Distinct from shading: low current at a *normal* Vmp.
    i_low = max(args.i_zero, args.curt_i_frac * peak_i)
    v_hi = vmp_p90 + args.curt_v_margin
    curt, shade = [], []
    for r in have:
        if (r["zenith"] or 99) >= args.day_zenith:
            continue
        if (r["pv_estimate"] or 0) < args.curt_est_min:
            continue
        if (r[icol] or 0) >= i_low:
            continue
        if (r[vcol] or 0) >= v_hi:
            curt.append(r)            # high V + low I + forecast high  -> curtailment-like
        elif (r[vcol] or 0) > args.v_floor:
            shade.append(r)           # normal Vmp + low I              -> shading-like
    print(f"  off-MPP screen (forecast>={args.curt_est_min} kW, I<{i_low:.2f} A): "
          f"curtailment-like (V>{v_hi:.0f} V) = {len(curt)}   shading-like (V~Vmp) = {len(shade)}")

    if args.list and (curt or shade):
        def _show(tag: str, rs: list[dict]) -> None:
            for r in rs[: args.list]:
                print(f"    {tag:11} {_hhmm(r['period_end_epoch'], args.tz)}  "
                      f"V={r[vcol]:6.0f} I={r[icol]:5.2f}  est={r['pv_estimate']:.2f} act={r['pv_actual']:.2f} "
                      f"cloud={r['clouds']:>3} zen={r['zenith']:.0f}")
        _show("CURTAIL?", curt)
        _show("SHADE?", shade)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqlite", required=True, help="Path to the built-in SQLite store")
    p.add_argument("--site", default=DEFAULT_SITE, help=f"Site resource_id (default {DEFAULT_SITE})")
    p.add_argument("--tz", type=float, default=DEFAULT_TZ_OFFSET, help="Hours to add to UTC for local times (default 10 = AEST)")
    p.add_argument("--list", type=int, default=0, metavar="N", help="List up to N example rows per off-MPP category")
    band = p.add_argument_group("classification thresholds")
    band.add_argument("--i-min", type=float, default=0.5, help="Min current (A) for an at-MPP sample (default 0.5)")
    band.add_argument("--i-zero", type=float, default=0.1, help="Current (A) treated as ~open-circuit (default 0.1)")
    band.add_argument("--v-floor", type=float, default=20.0, help="Min voltage (V) to exclude night floor (default 20)")
    band.add_argument("--day-zenith", type=float, default=88.0, help="Sun-up cutoff: zenith < this is daylight (default 88)")
    band.add_argument("--temp-split", type=float, default=15.0, help="Ambient temp split for the warm/cool Vmp buckets (default 15)")
    curt = p.add_argument_group("off-MPP / curtailment screen")
    curt.add_argument("--curt-est-min", type=float, default=1.0, help="Min forecast kW for a curtailment candidate (default 1.0)")
    curt.add_argument("--curt-i-frac", type=float, default=0.30, help="Current below this fraction of peak counts as 'low' (default 0.30)")
    curt.add_argument("--curt-v-margin", type=float, default=10.0, help="Volts above Vmp-p90 to call it off-MPP (default 10)")
    args = p.parse_args()

    conn = _connect(args.sqlite)
    try:
        rows = _fetch(conn, args.site)
    finally:
        conn.close()

    print(f"site '{args.site}': {len(rows)} rows")
    if not rows:
        raise SystemExit("No rows for that site.")
    dc_rows = [r for r in rows if any((r[v] or 0) or (r[i] or 0) for _, v, i in TRACKERS)]
    print(f"rows with any DC telemetry: {len(dc_rows)}")
    if not dc_rows:
        raise SystemExit("No DC telemetry captured yet — configure the MPPT voltage/current sensors first.")

    for idx, vcol, icol in TRACKERS:
        _analyse_tracker(rows, idx, vcol, icol, args)


if __name__ == "__main__":
    main()
