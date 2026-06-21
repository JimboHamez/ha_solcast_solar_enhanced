#!/usr/bin/env python3
"""Prototype: clearness-index clear-sky gate vs the OWM total-cloud gate.

Motivation
----------
The integration currently decides whether a half-hour is "clear sky" (and so
usable for PV tuning / shading-dampening) from OpenWeatherMap total cloud cover
(``clouds < CONF_CLOUD_THRESHOLD``). Total cloud % is a model-diagnosed sky
*coverage* fraction, not a measure of beam attenuation, and on days with thin
mid/high cloud it reports "overcast" while the panels see full sun — so genuine
clear-sky records get filtered out.

This prototype gates instead on a **clearness index** computed from irradiance +
solar geometry we already have on the row:

    Kt = GHI_measured / GHI_clearsky(zenith)

``GHI_clearsky`` is the Haurwitz model — zenith-only, pure Python, no numpy /
scipy / pvlib (keeps the Raspberry-Pi-safe footprint of the integration). A row
is "clear" when ``Kt >= KT_THRESHOLD`` and the sun is high enough for the clear-
sky reference to be meaningful.

It is deliberately stdlib-only so it can run anywhere, including the Pi. Run:

    python3 tools/clearness_gate.py data/solcast_solar_enhanced.db

Caveat: the live coordinator does NOT currently persist ``ghi`` (the column is
stdlib-default 0); the values used here were backfilled by the Open-Meteo
tooling. Productionising this gate requires a live GHI feed (e.g. the keyless
Open-Meteo ``shortwave_radiation``) or the PV-based variant (``pv_actual`` vs a
clear-sky PV expectation). See ``--pv`` below for the latter.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

# Mirror the integration's default OWM gate so the comparison is apples-to-apples.
OWM_CLOUD_THRESHOLD = 20          # DEFAULT_CLOUD_THRESHOLD in const.py
KT_THRESHOLD = 0.75               # clearness index above which a slot is "clear"
ZENITH_MAX = 85.0                 # below the horizon-grazing band (Kt unstable)
GHI_CS_FLOOR = 40.0               # W/m^2; ignore slots where clear-sky GHI is tiny


def clearsky_ghi_haurwitz(zenith_deg: float) -> float:
    """Haurwitz clear-sky global horizontal irradiance (W/m^2) from zenith.

    GHI_cs = 1098 * cos(z) * exp(-0.059 / cos(z)),  for cos(z) > 0 else 0.

    Zenith-only, well validated for clear-sky GHI, and free of any external
    dependency — the same pure-Python ethos as ``pv_tuning.solar_position``.
    """
    cz = math.cos(math.radians(zenith_deg))
    if cz <= 0:
        return 0.0
    return 1098.0 * cz * math.exp(-0.059 / cz)


def clearness_index(ghi: float, zenith_deg: float) -> float | None:
    """Kt = GHI / GHI_clearsky, or None when the sun is too low to judge."""
    if zenith_deg >= ZENITH_MAX:
        return None
    ghi_cs = clearsky_ghi_haurwitz(zenith_deg)
    if ghi_cs < GHI_CS_FLOOR:
        return None
    return ghi / ghi_cs


def load_rows(db_path: str, site: str = "_total") -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT period_end, zenith, ghi, clouds, pv_actual, pv_estimate "
        "FROM solcast_data WHERE site = ? ORDER BY period_end_epoch",
        (site,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", help="path to solcast_solar_enhanced.db")
    ap.add_argument("--site", default="_total")
    ap.add_argument("--kt", type=float, default=KT_THRESHOLD)
    ap.add_argument("--cloud", type=int, default=OWM_CLOUD_THRESHOLD)
    ap.add_argument("--pv", action="store_true",
                    help="use pv_actual/pv_estimate as the clearness proxy instead of ghi "
                         "(production-viable: no external GHI needed)")
    args = ap.parse_args(argv)

    rows = load_rows(args.db, args.site)

    # Restrict to daylight rows the gate can actually judge.
    judged = []
    for r in rows:
        if args.pv:
            # PV-based clearness: measured / forecast, sun up. Proxy only — relies
            # on pv_actual, which is corrupted in pre-2026-06-20 DBs.
            if r["zenith"] >= ZENITH_MAX or not r["pv_estimate"]:
                continue
            kt = r["pv_actual"] / r["pv_estimate"]
        else:
            kt = clearness_index(r["ghi"], r["zenith"])
            if kt is None:
                continue
        r["kt"] = kt
        judged.append(r)

    if not judged:
        print("No judgeable daylight rows (check ghi/zenith population).")
        return 1

    # Buckets
    def is_clear_owm(r): return r["clouds"] < args.cloud
    def is_clear_kt(r):  return r["kt"] >= args.kt

    both = [r for r in judged if is_clear_owm(r) and is_clear_kt(r)]
    owm_only = [r for r in judged if is_clear_owm(r) and not is_clear_kt(r)]
    kt_only = [r for r in judged if not is_clear_owm(r) and is_clear_kt(r)]
    neither = [r for r in judged if not is_clear_owm(r) and not is_clear_kt(r)]

    def avg(seq, key):
        vals = [x[key] for x in seq if x[key] is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    src = "pv_actual/pv_estimate" if args.pv else "ghi/clearsky(Haurwitz)"
    print(f"DB={args.db}  site={args.site}  clearness source={src}")
    print(f"Gates: OWM clear = clouds < {args.cloud};  Kt clear = Kt >= {args.kt}")
    print(f"Judgeable daylight rows: {len(judged)}\n")

    print(f"{'bucket':<26}{'n':>4}  {'avg_Kt':>7} {'avg_cloud':>9} {'avg_act':>8} {'avg_est':>8}")
    for name, seq in [("both clear", both),
                      ("OWM-clear, Kt-cloudy", owm_only),
                      ("Kt-clear, OWM-cloudy", kt_only),
                      ("both cloudy", neither)]:
        print(f"{name:<26}{len(seq):>4}  {avg(seq,'kt'):>7.2f} {avg(seq,'clouds'):>9.1f} "
              f"{avg(seq,'pv_actual'):>8.2f} {avg(seq,'pv_estimate'):>8.2f}")

    n_owm = len(both) + len(owm_only)
    n_kt = len(both) + len(kt_only)
    print(f"\nClear-sky records:  OWM gate = {n_owm}   Kt gate = {n_kt}   "
          f"(Kt recovers {len(kt_only)} that OWM rejects, drops {len(owm_only)} OWM accepts)")

    # The headline cases: records OWM throws away but that look genuinely clear.
    if kt_only:
        print(f"\nRecovered records (OWM-cloudy but Kt-clear) — top by Kt:")
        for r in sorted(kt_only, key=lambda x: -x["kt"])[:12]:
            print(f"  {r['period_end'][:16]}  Kt={r['kt']:.2f}  cloud={r['clouds']:3d}  "
                  f"ghi={r['ghi']:6.1f}  act={r['pv_actual']:.2f}  est={r['pv_estimate']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
