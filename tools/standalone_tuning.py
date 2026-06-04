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

    # Tune a CSV export with the same columns instead
    python tools/standalone_tuning.py --csv history.csv --capacity 5

Requirements: numpy + scipy. The SQLite source uses the standard library; CSV
mode needs neither.
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
from custom_components.solcast_solar_enhanced.pv_tuning import (  # noqa: E402
    run_tuning,
)

# Columns the tuner consumes — mirrors SqliteStore.async_get_records_for_tuning.
COLUMNS = ["pv_actual", "pv_export", "pv_estimate", "azimuth", "zenith", "clouds", "battery_charge"]
DEFAULT_SITE = "_total"


def _connect_sqlite(path: str):
    """Open the built-in SQLite store read-only via the standard library."""
    import sqlite3

    if not Path(path).exists():
        raise SystemExit(f"SQLite store not found: {path}")
    return sqlite3.connect(path)


def _fetch_sites(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT site FROM solcast_data")
    sites = [r[0] for r in cur.fetchall() if r and r[0] is not None]
    cur.close()
    return sites


def _fetch_records(conn, site: str | None, limit: int) -> list[dict[str, Any]]:
    cur = conn.cursor()
    clause, params = "", []
    if site is not None:
        clause, params = " AND site = ?", [site]
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
    result = run_tuning(
        records,
        args.capacity,
        args.cloud_threshold,
        args.clipping_threshold,
        args.export_limit,
        args.tilt,
        args.azimuth,
    )
    print(f"\n=== {label} ===")
    print(f"  records fetched : {len(records)}")
    if not result:
        print("  result          : (insufficient clear-sky data or scipy missing)")
        return
    print(f"  tuned tilt      : {result['tilt']:.2f}°")
    print(f"  tuned azimuth   : {result['azimuth']:.2f}°  (0=N, 90=E)")
    print(f"  RMSE            : {result['rmse_kw']:.4f} kW")
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
    tune.add_argument("--clipping-threshold", type=float, default=0.95)
    tune.add_argument("--export-limit", type=float, default=0.0, help="Property export limit kW (0=off)")
    tune.add_argument("--tilt", type=float, default=20.0, help="Seed tilt °")
    tune.add_argument("--azimuth", type=float, default=0.0, help="Seed azimuth ° (0=N, 90=E)")
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
                _tune_and_report(f"site {s}", _fetch_records(conn, s, args.limit), args)
        else:
            site_id = args.site if args.site else DEFAULT_SITE
            _tune_and_report(f"site {site_id}", _fetch_records(conn, site_id, args.limit), args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
