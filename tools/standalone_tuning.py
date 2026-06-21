#!/usr/bin/env python3
"""Standalone PV tuning runner — run the tilt/azimuth optimisation outside Home
Assistant, against the built-in SQLite store (or a CSV export).

It reuses the *exact* tuning maths shipped in the integration
(``custom_components/solcast_solar_enhanced/pv_tuning.py``) — there is no
duplicated algorithm here, so results match what the integration computes.

Examples
--------
    # Whole-property ('_total') tuning from the built-in store
    python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db \
        --capacity 6.6

    # One Solcast site (multi-site), seeded with that array's orientation
    python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db \
        --site b68d-c05a --capacity 5 --tilt 30 --azimuth 67.5

    # Every site present in the table
    python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db \
        --all-sites --capacity 5

    # v1.7.0 path: clearness-index clear-sky gate instead of cloud cover
    python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db \
        --capacity 6.6 --kt-threshold 0.75

    # Tune a CSV export with the same columns instead
    python tools/standalone_tuning.py --csv history.csv --capacity 5

Requirements: numpy (no scipy — the optimiser is a pure numpy grid search). The
SQLite source uses the standard library; CSV mode needs only numpy.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

# Import the integration's pure tuning functions (no Home Assistant required).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from custom_components.solcast_solar_enhanced.const import (  # noqa: E402
    DEFAULT_KT_THRESHOLD,
    KT_GHI_CS_FLOOR,
    KT_ZENITH_MAX,
)
from custom_components.solcast_solar_enhanced.pv_tuning import (  # noqa: E402
    clearsky_ghi,
    run_tuning,
)

# Columns the tuner consumes — mirrors SqliteStore.async_get_records_for_tuning.
# ghi/dni/dhi feed the transposition tuner; without them every row is skipped.
COLUMNS = [
    "pv_actual", "pv_export", "pv_estimate", "azimuth", "zenith", "clouds",
    "ghi", "dni", "dhi", "battery_charge",
]
DEFAULT_SITE = "_total"


def _connect_sqlite(path: str):
    """Open the built-in SQLite store read-only via the standard library."""
    import sqlite3

    if not Path(path).exists():
        raise SystemExit(f"SQLite store not found: {path}")
    conn = sqlite3.connect(path)
    # Register the clear-sky model so the Kt gate can run in SQL, exactly as
    # SqliteStore does (SQLite has no native exp()).
    conn.create_function("clearsky_ghi", 1, clearsky_ghi)
    return conn


def _fetch_sites(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT site FROM solcast_data")
    sites = [r[0] for r in cur.fetchall() if r and r[0] is not None]
    cur.close()
    return sites


def _fetch_records(
    conn, site: str | None, limit: int, kt_threshold: float | None = None
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    clause, params = "", []
    if site is not None:
        clause, params = " AND site = ?", [site]
    # Kt clear-sky gate (the v1.7.0 path, when Open-Meteo irradiance is present):
    # select half-hours whose measured GHI is at least kt_threshold of clear-sky
    # GHI. Mirrors SqliteStore.async_get_records_for_tuning.
    if kt_threshold is not None:
        clause += (
            " AND ghi > 0 AND zenith < ? AND clearsky_ghi(zenith) >= ? "
            "AND ghi >= ? * clearsky_ghi(zenith)"
        )
        params += [float(KT_ZENITH_MAX), float(KT_GHI_CS_FLOOR), float(kt_threshold)]
    cur.execute(
        f"SELECT {', '.join(COLUMNS)} FROM solcast_data "
        f"WHERE pv_actual > 0{clause} "
        f"ORDER BY period_end_epoch DESC LIMIT ?",
        (*params, limit),
    )
    rows = [dict(zip(COLUMNS, r)) for r in cur.fetchall()]
    cur.close()
    return rows


def _read_csv(path: str) -> list[dict[str, Any]]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        out = []
        for row in reader:
            rec = {}
            for c in COLUMNS:
                rec[c] = float(row.get(c, 0) or 0) if c != "clouds" else int(float(row.get(c, 100) or 100))
            if rec["pv_actual"] > 0:
                out.append(rec)
        return out


def _tune_and_report(label: str, records: list[dict[str, Any]], args: argparse.Namespace) -> None:
    # When the SQL Kt gate already selected clear-sky rows, disable run_tuning's
    # internal cloud re-filter (a value above 100 is a no-op) — clear rows can
    # carry a 100% `clouds` sentinel when no cloud source wrote them.
    cloud_threshold = 101 if args.kt_threshold is not None else args.cloud_threshold
    # NB: the transposition tuner recovers tilt itself (no seed) and holds azimuth
    # fixed. Pass by keyword — the signature is
    # (records, capacity, cloud_threshold, clipping_threshold, export_limit_kw,
    #  fixed_azimuth, albedo, model) — so --azimuth maps to fixed_azimuth and
    # --tilt is no longer consumed.
    result = run_tuning(
        records,
        args.capacity,
        cloud_threshold,
        args.clipping_threshold,
        export_limit_kw=args.export_limit,
        fixed_azimuth=args.azimuth,
    )
    print(f"\n=== {label} ===")
    print(f"  records fetched : {len(records)}")
    if not result:
        print("  result          : (insufficient clear-sky data or numpy missing)")
        return
    print(f"  tuned tilt      : {result['tilt']:.2f}°")
    print(f"  tuned azimuth   : {result['azimuth']:.2f}°  (0=N, 90=E; held, not tuned)")
    print(f"  MAE             : {result['mae_kw']:.4f} kW")
    print(f"  RMSE            : {result['rmse_kw']:.4f} kW")
    print(f"  capacity scale  : {result['capacity_scale']:.5f} kW per W/m²")
    print(f"  records used    : {result['n_records']}")
    print(f"  export-excluded : {result['export_limited_excluded']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("data source")
    src.add_argument("--sqlite", help="Path to the built-in SQLite store (config/solcast_solar_enhanced.db)")
    src.add_argument("--csv", help="Tune a CSV export instead of the SQLite store")
    src.add_argument("--limit", type=int, default=2000, help="Max recent rows (default 2000)")
    site = p.add_mutually_exclusive_group()
    site.add_argument("--site", help=f"Tune one site (resource_id; default {DEFAULT_SITE})")
    site.add_argument("--all-sites", action="store_true", help="Tune every site in the table")
    tune = p.add_argument_group("tuning parameters")
    tune.add_argument("--capacity", type=float, default=5.0, help="System/array capacity kW")
    tune.add_argument("--cloud-threshold", type=int, default=20)
    tune.add_argument(
        "--kt-threshold", type=float, default=None,
        help=f"Use the clearness-index clear-sky gate (Kt = GHI/clear-sky GHI, "
             f"e.g. {DEFAULT_KT_THRESHOLD}) instead of --cloud-threshold; needs "
             f"ghi columns. This is the v1.7.0 integration path.",
    )
    tune.add_argument("--clipping-threshold", type=float, default=0.95)
    tune.add_argument("--export-limit", type=float, default=0.0, help="Property export limit kW (0=off)")
    tune.add_argument("--tilt", type=float, default=20.0,
                      help="(ignored — transposition recovers tilt; kept for compatibility)")
    tune.add_argument("--azimuth", type=float, default=0.0,
                      help="Fixed azimuth ° (0=N, 90=E); held, not tuned")
    args = p.parse_args()

    if args.csv:
        _tune_and_report(f"CSV {args.csv}", _read_csv(args.csv), args)
        return

    if not args.sqlite:
        raise SystemExit("Provide a data source: --sqlite <path> or --csv <file>")

    conn = _connect_sqlite(args.sqlite)
    try:
        if args.all_sites:
            sites = _fetch_sites(conn)
            if not sites:
                print("No sites found in solcast_data.")
                return
            for s in sites:
                _tune_and_report(
                    f"site {s}",
                    _fetch_records(conn, s, args.limit, args.kt_threshold),
                    args,
                )
        else:
            site_id = args.site if args.site else DEFAULT_SITE
            _tune_and_report(
                f"site {site_id}",
                _fetch_records(conn, site_id, args.limit, args.kt_threshold),
                args,
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
