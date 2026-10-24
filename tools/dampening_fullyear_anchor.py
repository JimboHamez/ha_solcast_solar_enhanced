"""Model the 'full-year geometry anchor' dampening variant and write the curve to CSV.

Production blend rests low-confidence slots on a NEUTRAL 1.0 anchor:
    final = (1-alpha)*1.0 + alpha*db_factor_seasonal
so a data-starved morning slot stays ~neutral even though shading is real there.

This variant swaps the anchor for the FULL-DB clear-sky quality-weighted ratio at the
SAME sun geometry (target zenith/azimuth) as the slot:
    final = (1-alpha)*R_full(geom) + alpha*db_factor_seasonal(geom)
R_full is computed over the whole dataset (all days-of-year), so it has a high
quality-weighted count and already encodes the year-round morning-shading dip. alpha
and db_factor_seasonal still come from the production +/-14d window. Net effect: the
morning dip is surfaced immediately (via the anchor) while midday is NOT spuriously
dampened for a low-alpha winter seasonal bias (R_full midday ~= 1.0).

sigma_zen=10, BASE_MIDPOINT=30 (production confidence) throughout.
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


def fetch(window_days=None):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    where = "pv_actual > 0 AND pv_estimate > 0"
    params = ()
    if window_days is not None:
        where += (" AND ABS(CAST(strftime('%j', period_end_epoch, 'unixepoch') "
                  "AS INTEGER) - ?) <= ?")
        params = (DOY, window_days)
    rows = [dict(r) for r in c.execute(
        "SELECT pv_actual, pv_export, pv_estimate, azimuth, zenith, clouds "
        f"FROM solcast_data WHERE {where}", params).fetchall()]
    c.close()
    return rows


def weighted_ratio(records, tz, ta):
    """Quality-weighted measured/effective-estimate ratio + weight at a geometry."""
    clip_kw = CAP * CLIP
    tw = wrs = 0.0
    n = 0
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
        n += 1
    if tw < 1e-6 or n == 0:
        return None, 0.0, 0
    return wrs / tw, tw, n


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    seasonal = fetch(14)        # production window
    full = fetch(None)          # whole dataset
    print(f"seasonal +/-14d records: {len(seasonal)}   full-DB records: {len(full)}")

    slot_rows = []
    factors_var, factors_prod = [], []
    for slot in range(48):
        h, m = divmod(slot * 30, 60)
        sl = NOW.replace(hour=h, minute=m, second=0, microsecond=0)
        az, zen = pt.solar_position(int(sl.timestamp()) + 900, LAT, LON)
        if zen >= 90:
            slot_rows.append(dict(slot=slot, t=f"{h:02d}:{m:02d}", zen=zen, az=az,
                                  r_full=None, qf=0.0, db=None, alpha=0.0,
                                  f_var=1.0, f_prod=1.0, src="night"))
            factors_var.append(1.0)
            factors_prod.append(1.0)
            continue

        r_full, qf, _ = weighted_ratio(full, zen, az)        # anchor
        db_seasonal, _, _ = weighted_ratio(seasonal, zen, az)

        # Production result (anchor 1.0) via the real function.
        prod = sd.compute_dampening(
            records=seasonal, capacity_kw=CAP, cloud_threshold=CTHR,
            cloud_max_include=CMAX, clipping_threshold=CLIP,
            target_zenith=zen, target_azimuth=az, export_limit_kw=EXP)
        alpha = prod["alpha"]
        f_prod = prod["factor"]

        # Variant: same alpha, anchor swapped from 1.0 -> r_full.
        if db_seasonal is None or r_full is None:
            f_var = r_full if r_full is not None else 1.0
        else:
            f_var = (1.0 - alpha) * r_full + alpha * db_seasonal
        f_var = round(f_var, 4)

        slot_rows.append(dict(slot=slot, t=f"{h:02d}:{m:02d}", zen=zen, az=az,
                              r_full=r_full, qf=qf, db=db_seasonal, alpha=alpha,
                              f_var=f_var, f_prod=f_prod, src=prod["source"]))
        factors_var.append(f_var)
        factors_prod.append(f_prod)

    # 48-slot curve CSV.
    path1 = os.path.join(OUTDIR, "dampening_fullyear_anchor_curve.csv")
    with open(path1, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slot", "local_time", "sun_zenith", "sun_azimuth",
                    "anchor_full_year", "anchor_qweight", "db_seasonal_14d", "alpha",
                    "factor_variant", "factor_production", "source"])
        for s in slot_rows:
            w.writerow([s["slot"], s["t"], round(s["zen"], 2), round(s["az"], 2),
                        "" if s["r_full"] is None else round(s["r_full"], 4),
                        round(s["qf"], 1),
                        "" if s["db"] is None else round(s["db"], 4),
                        round(s["alpha"], 4), s["f_var"], s["f_prod"], s["src"]])

    # 24 hourly push CSV.
    hv = sd.average_slot_pairs(factors_var)
    hp = sd.average_slot_pairs(factors_prod)
    pv = [min(1.0, max(0.0, round(x, 4))) for x in hv]
    pp = [min(1.0, max(0.0, round(x, 4))) for x in hp]
    path2 = os.path.join(OUTDIR, "dampening_fullyear_anchor_push.csv")
    with open(path2, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hour_local", "push_variant", "push_production", "delta"])
        for hr in range(24):
            w.writerow([f"{hr:02d}:00", pv[hr], pp[hr], round(pv[hr] - pp[hr], 4)])

    # Console: show the anchor's morning shape + side-by-side curve.
    print("\nslot   zen   anchor(full)  q_anchor  db(14d)  alpha  f_var  f_prod")
    for s in slot_rows:
        if s["src"] == "night":
            continue
        rf = "  -  " if s["r_full"] is None else f"{s['r_full']:.3f}"
        db = "  -  " if s["db"] is None else f"{s['db']:.3f}"
        print(f"{s['t']}  {s['zen']:5.1f}      {rf}     {s['qf']:6.1f}   {db}  "
              f"{s['alpha']:.3f}  {s['f_var']:.3f}  {s['f_prod']:.3f}")
    print("\ndaytime push curve (local hour: variant -> production):")
    for hr in range(5, 21):
        bar = "#" * int(round((1 - pv[hr]) * 100))
        print(f"  {hr:02d}:00  {pv[hr]:.3f}  (prod {pp[hr]:.3f})  {bar}")
    print(f"\nWrote:\n  {path1}\n  {path2}")


if __name__ == "__main__":
    main()
