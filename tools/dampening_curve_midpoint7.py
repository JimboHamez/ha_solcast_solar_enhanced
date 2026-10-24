"""Model the dampening curve with BASE_MIDPOINT=7 and write it to CSV.

Runs the REAL production compute_dampening (BASE_MIDPOINT monkeypatched 30 -> 7,
sigma_zen left at the production 10 deg) against the single-site DB's current
day-of-year window, for all 48 half-hour slots. Emits the slot curve plus the 24
hourly values that would actually be pushed to solcast_solar.set_dampening (with
the same [0,1] clamp the coordinator applies), alongside the BASE_MIDPOINT=30
baseline for comparison.
"""
from __future__ import annotations

import csv
import importlib.util
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "custom_components", "solcast_solar_enhanced")
DB = os.path.join(ROOT, "data", "solcast_solar_enhanced_single_site.db")
OUTDIR = os.path.join(ROOT, "analysis")

LAT, LON = -37.9046, 145.0362
TZ = ZoneInfo("Australia/Melbourne")
CAP, EXP, CTHR, CMAX, CLIP = 8.0, 5.0, 20, 60, 0.95
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)
DOY = NOW.timetuple().tm_yday


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load("shading_dampening", os.path.join(PKG, "shading_dampening.py"))
pt = _load("pv_tuning", os.path.join(PKG, "pv_tuning.py"))


def fetch():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    sql = (
        "SELECT pv_actual, pv_export, pv_estimate, azimuth, zenith, clouds, "
        "COALESCE(battery_charge, 0.0) AS battery_charge FROM solcast_data "
        "WHERE pv_actual > 0 AND pv_estimate > 0 AND "
        "ABS(CAST(strftime('%j', period_end_epoch, 'unixepoch') AS INTEGER) - ?) <= 14"
    )
    rows = [dict(r) for r in c.execute(sql, (DOY,)).fetchall()]
    c.close()
    return rows


def compute_all(records, base_midpoint):
    """Run real compute_dampening for all 48 slots at a given BASE_MIDPOINT."""
    orig = sd.BASE_MIDPOINT
    sd.BASE_MIDPOINT = float(base_midpoint)
    out = []
    try:
        for slot in range(48):
            h, m = divmod(slot * 30, 60)
            sl = NOW.replace(hour=h, minute=m, second=0, microsecond=0)
            az, zen = pt.solar_position(int(sl.timestamp()) + 900, LAT, LON)
            if zen >= 90:
                out.append(dict(slot=slot, h=h, m=m, az=az, zen=zen, factor=1.0,
                                alpha=0.0, source="night", db=None, q=0.0, n=0))
                continue
            d = sd.compute_dampening(
                records=records, capacity_kw=CAP, cloud_threshold=CTHR,
                cloud_max_include=CMAX, clipping_threshold=CLIP,
                target_zenith=zen, target_azimuth=az, export_limit_kw=EXP)
            # db_factor (raw ratio) recovered from the blend: factor=(1-a)+a*db
            db = None
            if d["alpha"] > 0 and d["source"] not in ("no_data", "night"):
                # only exact when no early-clamp; recompute raw ratio independently
                db = _raw_ratio(records, zen, az)
            out.append(dict(slot=slot, h=h, m=m, az=az, zen=zen, factor=d["factor"],
                            alpha=d["alpha"], source=d["source"], db=db,
                            q=d["quality_records"], n=d.get("forecast_clipped", 0) or 0))
    finally:
        sd.BASE_MIDPOINT = orig
    return out


def _raw_ratio(records, tz, ta):
    """Quality-weighted raw measured/effective-estimate ratio (the 3.4b correction)."""
    tw = 0.0
    wrs = 0.0
    clip_kw = CAP * CLIP
    for r in records:
        a = float(r["pv_actual"] or 0)
        est = float(r["pv_estimate"] or 0)
        ex = float(r["pv_export"] or 0)
        cl = 100 if r["clouds"] is None else int(r["clouds"])
        z = float(r["zenith"] or 90)
        az = float(r["azimuth"] or 0)
        if est <= 0:
            continue
        if a >= clip_kw and est >= clip_kw and cl < CTHR:
            continue
        cw = sd._cloud_weight(cl, CTHR, CMAX)
        if cw <= 0:
            continue
        comb = cw * sd._geometry_weight(z, az, tz, ta)
        if comb < 1e-6:
            continue
        eff = est
        ceil = a + (EXP - ex)
        cl2 = max(a, min(est, ceil))
        if cl2 < est - 1e-9:
            eff = cl2
        wrs += comb * (a / eff)
        tw += comb
    return None if tw < 1e-6 else wrs / tw


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    recs = fetch()
    m7 = compute_all(recs, 7)
    m30 = compute_all(recs, 30)

    # 48-slot curve CSV.
    path1 = os.path.join(OUTDIR, "dampening_curve_midpoint7.csv")
    with open(path1, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slot", "local_time", "sun_zenith", "sun_azimuth", "db_factor_3_4b",
                    "alpha_m7", "source_m7", "factor_m7", "factor_m30", "alpha_m30",
                    "quality_records", "forecast_clipped"])
        for a, b in zip(m7, m30):
            w.writerow([a["slot"], f"{a['h']:02d}:{a['m']:02d}", round(a["zen"], 2),
                        round(a["az"], 2), "" if a["db"] is None else round(a["db"], 4),
                        a["alpha"], a["source"], a["factor"], b["factor"], b["alpha"],
                        a["q"], a["n"]])

    # 24 hourly push CSV (averaged slot pairs + [0,1] clamp, as _push_dampening does).
    hourly7 = sd.average_slot_pairs([s["factor"] for s in m7])
    hourly30 = sd.average_slot_pairs([s["factor"] for s in m30])
    push7 = [min(1.0, max(0.0, round(v, 4))) for v in hourly7]
    push30 = [min(1.0, max(0.0, round(v, 4))) for v in hourly30]
    path2 = os.path.join(OUTDIR, "dampening_push_midpoint7.csv")
    with open(path2, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hour_local", "push_factor_m7", "push_factor_m30", "delta_m7_vs_m30"])
        for hr in range(24):
            w.writerow([f"{hr:02d}:00", push7[hr], push30[hr],
                        round(push7[hr] - push30[hr], 4)])

    # Console summary.
    day = [s for s in m7 if s["source"] != "night"]
    released = sum(1 for s in day if s["alpha"] >= 0.5)
    hist = sum(1 for s in day if s["alpha"] > 0.95)
    print(f"±14d window, {len(recs)} records.  BASE_MIDPOINT=7, sigma_zen=10 (production)")
    print(f"Daylight slots: {len(day)}  |  alpha>=0.5 (clamp released): {released}"
          f"  |  db_history (alpha>0.95): {hist}")
    print(f"Pushed-factor range m7: {min(push7[h] for h in range(6,20)):.3f}"
          f"–{max(push7[h] for h in range(6,20)):.3f}"
          f"  (m30 baseline: {min(push30[h] for h in range(6,20)):.3f}"
          f"–{max(push30[h] for h in range(6,20)):.3f})")
    print("\ndaytime push curve (local hour: m7 -> m30):")
    for hr in range(5, 21):
        bar = "#" * int(round((1 - push7[hr]) * 100))
        print(f"  {hr:02d}:00  {push7[hr]:.3f}  (m30 {push30[hr]:.3f})  {bar}")
    print(f"\nWrote:\n  {path1}\n  {path2}")


if __name__ == "__main__":
    main()
