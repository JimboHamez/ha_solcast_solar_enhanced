"""Analyse the built-in adaptive shading dampening against Solcast notebook 3.4b.

Notebook 3.4b ("Rooftop Shading Corrections") computes a per-time-of-day shading
correction = measured / *tuned* estimate over clear-sky periods, applied on top of
an orientation-corrected (notebook 3.4) Solcast estimate.

This script runs the integration's PRODUCTION dampening calculation faithfully
against the single-site analysis DB, reproduces every per-record contribution that
feeds each half-hour slot, asserts the instrumented re-implementation matches the
real `compute_dampening`, and writes the data used to perform the calculation to CSV.

Usage:  python3 tools/dampening_3_4b_analysis.py
Outputs (analysis/):
  dampening_input_records.csv      raw DOY-window records (the calc input set)
  dampening_contributions.csv      per (slot, record) weights + ratio (the calc data)
  dampening_slot_summary.csv       48-slot output (factor, alpha, source, db_factor)
"""
from __future__ import annotations

import csv
import importlib.util
import math
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "custom_components", "solcast_solar_enhanced")
DB = os.path.join(ROOT, "data", "solcast_solar_enhanced_single_site.db")
OUTDIR = os.path.join(ROOT, "analysis")

# --- System / config constants (analysis DB provenance + production defaults) ---
LAT, LON = -37.9046, 145.0362          # coords the live integration uses
TZ = ZoneInfo("Australia/Melbourne")
CAPACITY_KW = 8.0                       # real system is 8 kW DC+AC (not 5.0 default)
EXPORT_LIMIT_KW = 5.0                   # 5 kW export limit
CLOUD_THRESHOLD = 20                    # DEFAULT_CLOUD_THRESHOLD
CLOUD_MAX_INCLUDE = 60                  # DEFAULT_CLOUD_MAX_INCLUDE
CLIPPING_THRESHOLD = 0.95               # DEFAULT_CLIPPING_THRESHOLD
DOY_WINDOW = 14                         # ±14-day day-of-year window
ANALYSIS_DATE = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)  # "now" for the DOY window


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load the real production modules directly (bypass the HA-coupled package __init__).
sd = _load("shading_dampening", os.path.join(PKG, "shading_dampening.py"))
pt = _load("pv_tuning", os.path.join(PKG, "pv_tuning.py"))
compute_dampening = sd.compute_dampening
_cloud_weight = sd._cloud_weight
_geometry_weight = sd._geometry_weight
solar_position = pt.solar_position


def fetch_doy_records(slot_doy: int) -> list[dict]:
    """Mirror SqliteStore.async_get_records_for_dampening (±DOY_WINDOW, all years)."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT period_end_epoch, pv_actual, pv_export, pv_estimate, "
        "pv_estimate10, pv_estimate90, azimuth, zenith, clouds, "
        "COALESCE(battery_charge, 0.0) AS battery_charge "
        "FROM solcast_data "
        "WHERE pv_actual > 0 AND pv_estimate > 0 "
        "AND ABS(CAST(strftime('%j', period_end_epoch, 'unixepoch') AS INTEGER) - ?) <= ?"
    )
    rows = [dict(r) for r in conn.execute(sql, (slot_doy, DOY_WINDOW)).fetchall()]
    conn.close()
    return rows


def instrument_slot(records, target_zenith, target_azimuth):
    """Re-implement compute_dampening's inner loop, capturing every contribution.

    Returns (contributions, agg) where agg mirrors compute_dampening's outputs so we
    can assert equality with the production function.
    """
    clip_kw = CAPACITY_KW * CLIPPING_THRESHOLD
    contribs = []
    total_weight = 0.0
    weighted_ratio_sum = 0.0
    clipped_excluded = 0
    forecast_clipped = 0
    n_records = 0

    for r in records:
        pv_actual = float(r.get("pv_actual", 0) or 0)
        total_pv = pv_actual
        pv_est = float(r.get("pv_estimate", 0) or 0)
        pv_export = float(r.get("pv_export", 0) or 0)
        raw_clouds = r.get("clouds")
        clouds = 100 if raw_clouds is None else int(raw_clouds)
        zenith = float(r.get("zenith", 90) or 90)
        azimuth = float(r.get("azimuth", 0) or 0)

        reason = "included"
        if pv_est <= 0:
            continue
        if total_pv >= clip_kw and pv_est >= clip_kw and clouds < CLOUD_THRESHOLD:
            clipped_excluded += 1
            reason = "clip_excluded"
            contribs.append(_row(r, clouds, zenith, azimuth, 0, 0, 0, pv_est, None, reason))
            continue
        cw = _cloud_weight(clouds, CLOUD_THRESHOLD, CLOUD_MAX_INCLUDE)
        if cw <= 0:
            reason = "cloud_excluded"
            contribs.append(_row(r, clouds, zenith, azimuth, cw, 0, 0, pv_est, None, reason))
            continue
        gw = _geometry_weight(zenith, azimuth, target_zenith, target_azimuth)
        combined = cw * gw
        if combined < 1e-6:
            reason = "geom_negligible"
            contribs.append(_row(r, clouds, zenith, azimuth, cw, gw, combined, pv_est, None, reason))
            continue

        effective_est = pv_est
        fclip = False
        if EXPORT_LIMIT_KW > 0:
            ceiling = total_pv + (EXPORT_LIMIT_KW - pv_export)
            clipped = max(total_pv, min(pv_est, ceiling))
            if clipped < pv_est - 1e-9:
                effective_est = clipped
                forecast_clipped += 1
                fclip = True

        ratio = total_pv / effective_est
        weighted_ratio_sum += combined * ratio
        total_weight += combined
        n_records += 1
        reason = "fclip_included" if fclip else "included"
        contribs.append(_row(r, clouds, zenith, azimuth, cw, gw, combined, effective_est, ratio, reason))

    if total_weight < 1e-6 or n_records == 0:
        agg = dict(factor=sd.NEUTRAL_FACTOR, alpha=0.0, source="no_data",
                   quality_records=0.0, avg_quality=0.0, db_factor=None,
                   clipped_excluded=clipped_excluded, forecast_clipped=forecast_clipped,
                   n_records=0)
        return contribs, agg

    db_factor = weighted_ratio_sum / total_weight
    avg_quality = total_weight / n_records
    midpoint = sd.BASE_MIDPOINT / max(avg_quality, 0.1)
    x = total_weight
    alpha = max(0.0, min(1.0, (x * x) / (x * x + midpoint * midpoint)))
    blended = (1.0 - alpha) * sd.NEUTRAL_FACTOR + alpha * db_factor
    if alpha < 0.5:
        lo = sd.NEUTRAL_FACTOR * (1.0 - sd.EARLY_CLAMP_PCT)
        hi = sd.NEUTRAL_FACTOR * (1.0 + sd.EARLY_CLAMP_PCT)
        blended = max(lo, min(hi, blended))
        source = "db_blended"
    else:
        source = "db_history" if alpha > 0.95 else "db_blended"
    agg = dict(factor=round(blended, 4), alpha=round(alpha, 4), source=source,
               quality_records=round(total_weight, 2), avg_quality=round(avg_quality, 3),
               db_factor=db_factor, clipped_excluded=clipped_excluded,
               forecast_clipped=forecast_clipped, n_records=n_records)
    return contribs, agg


def _row(r, clouds, zenith, azimuth, cw, gw, combined, eff_est, ratio, reason):
    return {
        "period_end_epoch": r["period_end_epoch"],
        "local_time": datetime.fromtimestamp(r["period_end_epoch"], tz=TZ).isoformat(),
        "pv_actual": round(float(r["pv_actual"]), 4),
        "pv_estimate": round(float(r["pv_estimate"]), 4),
        "effective_est": round(float(eff_est), 4),
        "pv_export": round(float(r["pv_export"]), 4),
        "clouds": clouds,
        "rec_zenith": round(float(zenith), 3),
        "rec_azimuth": round(float(azimuth), 3),
        "cloud_weight": round(cw, 4),
        "geom_weight": round(gw, 6),
        "combined_weight": round(combined, 6),
        "ratio": None if ratio is None else round(ratio, 4),
        "reason": reason,
    }


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    slot_doy = ANALYSIS_DATE.timetuple().tm_yday
    records = fetch_doy_records(slot_doy)
    print(f"DB: {os.path.basename(DB)}")
    print(f"Analysis 'now': {ANALYSIS_DATE.date()} (day-of-year {slot_doy}, ±{DOY_WINDOW}d window)")
    print(f"DOY-window daytime records: {len(records)}")

    # Raw input records CSV (the calc input set).
    with open(os.path.join(OUTDIR, "dampening_input_records.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["period_end_utc", "local_time", "doy", "pv_actual", "pv_estimate",
                    "pv_export", "clouds", "zenith", "azimuth", "raw_ratio"])
        for r in sorted(records, key=lambda x: x["period_end_epoch"]):
            e = r["period_end_epoch"]
            est = float(r["pv_estimate"])
            w.writerow([
                datetime.fromtimestamp(e, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(e, tz=TZ).isoformat(),
                int(datetime.fromtimestamp(e, tz=timezone.utc).strftime("%j")),
                round(float(r["pv_actual"]), 4), round(est, 4),
                round(float(r["pv_export"]), 4),
                "" if r["clouds"] is None else int(r["clouds"]),
                round(float(r["zenith"] or 90), 3), round(float(r["azimuth"] or 0), 3),
                "" if est <= 0 else round(float(r["pv_actual"]) / est, 4),
            ])

    now_local = ANALYSIS_DATE
    contrib_rows = []
    slot_rows = []
    mismatches = 0

    for slot in range(48):
        hour, minute = divmod(slot * 30, 60)
        slot_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        slot_epoch = int(slot_local.timestamp())
        az_slot, zen_slot = solar_position(slot_epoch + 900, LAT, LON)
        local_label = f"{hour:02d}:{minute:02d}"

        if zen_slot >= 90:  # night
            slot_rows.append(dict(slot=slot, local_time=local_label,
                                  sun_azimuth=round(az_slot, 2), sun_zenith=round(zen_slot, 2),
                                  factor=1.0, alpha=0.0, source="night", db_factor="",
                                  quality_records=0.0, avg_quality=0.0, n_contributing=0,
                                  clipped_excluded=0, forecast_clipped=0))
            continue

        contribs, agg = instrument_slot(records, zen_slot, az_slot)
        # Faithfulness check: production compute_dampening must agree with our instrumentation.
        prod = compute_dampening(
            records=records, capacity_kw=CAPACITY_KW, cloud_threshold=CLOUD_THRESHOLD,
            cloud_max_include=CLOUD_MAX_INCLUDE, clipping_threshold=CLIPPING_THRESHOLD,
            target_zenith=zen_slot, target_azimuth=az_slot, export_limit_kw=EXPORT_LIMIT_KW)
        if (prod["factor"] != agg["factor"] or prod["alpha"] != agg["alpha"]
                or prod["quality_records"] != agg["quality_records"]
                or prod["source"] != agg["source"]):
            mismatches += 1
            print(f"  ! slot {slot} mismatch: prod={prod} instrumented={agg}")

        slot_rows.append(dict(slot=slot, local_time=local_label,
                              sun_azimuth=round(az_slot, 2), sun_zenith=round(zen_slot, 2),
                              factor=agg["factor"], alpha=agg["alpha"], source=agg["source"],
                              db_factor="" if agg["db_factor"] is None else round(agg["db_factor"], 4),
                              quality_records=agg["quality_records"], avg_quality=agg["avg_quality"],
                              n_contributing=agg["n_records"], clipped_excluded=agg["clipped_excluded"],
                              forecast_clipped=agg["forecast_clipped"]))
        for c in contribs:
            if c["combined_weight"] > 0 or c["reason"] != "geom_negligible":
                row = {"slot": slot, "slot_local_time": local_label,
                       "slot_sun_zenith": round(zen_slot, 2), "slot_sun_azimuth": round(az_slot, 2)}
                row.update(c)
                contrib_rows.append(row)

    # Slot summary CSV (final calculation output).
    with open(os.path.join(OUTDIR, "dampening_slot_summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(slot_rows[0].keys()))
        w.writeheader()
        w.writerows(slot_rows)

    # Per-(slot, record) contributions CSV (the actual data each weighted average uses).
    fields = ["slot", "slot_local_time", "slot_sun_zenith", "slot_sun_azimuth",
              "period_end_epoch", "local_time", "pv_actual", "pv_estimate", "effective_est",
              "pv_export", "clouds", "rec_zenith", "rec_azimuth", "cloud_weight",
              "geom_weight", "combined_weight", "ratio", "reason"]
    with open(os.path.join(OUTDIR, "dampening_contributions.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        # Only emit rows with non-trivial weight to keep the file focused on the data used.
        w.writerows([r for r in contrib_rows if r["combined_weight"] >= 1e-6])

    # --- Console summary: how well the data meets notebook 3.4b ---
    day = [s for s in slot_rows if s["source"] != "night"]
    n_history = sum(1 for s in day if s["source"] == "db_history")
    n_blended = sum(1 for s in day if s["source"] == "db_blended")
    n_nodata = sum(1 for s in day if s["source"] == "no_data")
    print(f"\nFaithfulness check: {mismatches} slot mismatch(es) vs production compute_dampening")
    print(f"Daylight slots: {len(day)}  |  db_history(α>0.95): {n_history}  "
          f"db_blended: {n_blended}  no_data: {n_nodata}")
    emitted = sum(1 for r in contrib_rows if r['combined_weight'] >= 1e-6)
    print(f"Contribution rows emitted (combined_weight≥1e-6): {emitted}")
    print(f"\nWrote 3 CSVs to {OUTDIR}/")


if __name__ == "__main__":
    main()
