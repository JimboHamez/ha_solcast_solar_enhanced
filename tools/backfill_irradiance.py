#!/usr/bin/env python3
"""Backfill plane-of-array irradiance (GHI/DNI/DHI) onto existing store rows.

The transposition-based PV tuner needs the irradiance components Open-Meteo
provides. New rows collect them live, but rows written before this feature have
ghi/dni/dhi = 0. This one-pass tool fetches the Open-Meteo **historical archive**
(keyless) for your data's date range and fills those columns in place, so tuning
is useful immediately instead of waiting months for fresh data to accumulate.

Runs standalone — no Home Assistant required, stdlib only (urllib + sqlite3). The
irradiance is interpolated to each slot's **midpoint** (period_end_epoch − 15 min),
matching how the live loop samples it and how solar position is computed. Safe to
re-run; by default it only touches rows still missing irradiance.

Notes
-----
* The archive (ERA5) finalises with a ~5-day lag, so the most recent few days may
  come back null and stay 0 — they fill in on a later run, or live as new data.
* Irradiance is property-wide weather, so every site's row at a given timestamp
  gets the same values.

Examples
--------
    python tools/backfill_irradiance.py --sqlite config/solcast_solar_enhanced.db \\
        --lat -37.9046 --lon 145.0362

    # Preview without writing
    python tools/backfill_irradiance.py --sqlite out.db --lat -37.9 --lon 145.0 --dry-run
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sqlite3
import sys
import types
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
# The store module and what it imports are stdlib-only (numpy is optional), but the
# package __init__ imports Home Assistant. Register the package without running it, so
# this tool works on plain Python too. A package already imported is used as it is.
_PKG = "custom_components.solcast_solar_enhanced"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_ROOT / "custom_components" / "solcast_solar_enhanced")]
    sys.modules[_PKG] = _pkg
from custom_components.solcast_solar_enhanced.sqlite_store import (  # noqa: E402
    V2_SCALES,
    read_source,
    sync_v2_from_legacy,
    to_stored,
)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
VARS = ("shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation")
KEYS = ("ghi", "dni", "dhi")
CHUNK_DAYS = 365  # split long ranges into yearly archive requests


def fetch_archive(lat: float, lon: float, start: dt.date, end: dt.date) -> list[dict]:
    """Return sorted hourly samples [{epoch, ghi, dni, dhi}] over [start, end]."""
    out: list[dict] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), end)
        params = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "start_date": cur.isoformat(), "end_date": chunk_end.isoformat(),
            "hourly": ",".join(VARS), "timezone": "UTC",
        })
        with urllib.request.urlopen(f"{ARCHIVE_URL}?{params}", timeout=120) as resp:
            data = json.load(resp)
        h = data.get("hourly", {})
        times = h.get("time") or []
        for i, t in enumerate(times):
            epoch = int(dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp())
            out.append({
                "epoch": epoch,
                **{k: (h.get(v) or [None] * len(times))[i] for k, v in zip(KEYS, VARS)},
            })
        print(f"  fetched {cur} .. {chunk_end}: {len(times)} hourly samples")
        cur = chunk_end + dt.timedelta(days=1)
    out.sort(key=lambda r: r["epoch"])
    return out


def interp(series: list[dict], epochs: list[int], t: int) -> dict | None:
    """Linear interpolation of each component at instant ``t`` (None if unusable)."""
    i = bisect.bisect_left(epochs, t)
    if i <= 0:
        lo = hi = series[0]
    elif i >= len(series):
        lo = hi = series[-1]
    else:
        lo, hi = series[i - 1], series[i]
    span = hi["epoch"] - lo["epoch"]
    f = (t - lo["epoch"]) / span if span else 0.0
    res = {}
    for k in KEYS:
        a, b = lo[k], hi[k]
        if a is None and b is None:
            return None
        if a is None:
            res[k] = b
        elif b is None:
            res[k] = a
        else:
            res[k] = a + f * (b - a)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", required=True, help="path to solcast_solar_enhanced.db")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--all-rows", action="store_true",
                    help="overwrite even rows that already have irradiance (default: only ghi=0 rows)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.sqlite)
    conn.row_factory = sqlite3.Row
    # A compacted (schema v3) store has no legacy table: the irradiance lives once per
    # slot in slot_property, as scaled integers. Otherwise write the legacy table and
    # carry the change into v2 afterwards (schema v2, dual-writing).
    compacted = read_source(conn) == "slot_rows" and not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'solcast_data'"
    ).fetchone()
    table = "slot_property" if compacted else "solcast_data"

    def stored(name: str, value: float) -> float:
        return to_stored(value, 0.0, V2_SCALES[name]) if compacted else round(value, 2)

    rng = conn.execute(f"SELECT MIN(period_end_epoch) a, MAX(period_end_epoch) b FROM {table}").fetchone()
    if not rng or rng["a"] is None:
        print("No rows in store — nothing to backfill.")
        return 0
    start = dt.datetime.fromtimestamp(rng["a"], tz=dt.timezone.utc).date()
    end = dt.datetime.fromtimestamp(rng["b"], tz=dt.timezone.utc).date()
    print(f"Store spans {start} .. {end}. Fetching Open-Meteo archive at {args.lat},{args.lon} ...")
    series = fetch_archive(args.lat, args.lon, start, end)
    if not series:
        print("Archive returned no data.")
        return 1
    epochs = [r["epoch"] for r in series]

    # Distinct timestamps (all sites at one epoch share the property-wide irradiance).
    where = "" if args.all_rows else " WHERE ghi = 0 AND dni = 0 AND dhi = 0"
    rows = conn.execute(
        f"SELECT DISTINCT period_end_epoch FROM {table}{where}"
    ).fetchall()
    print(f"{len(rows)} timestamp(s) to fill ({'all rows' if args.all_rows else 'missing only'}).")

    updated, skipped = 0, 0
    for r in rows:
        ep = int(r["period_end_epoch"])
        vals = interp(series, epochs, ep - 900)  # midpoint
        if vals is None:
            skipped += 1
            continue
        if not args.dry_run:
            conn.execute(
                f"UPDATE {table} SET ghi=?, dni=?, dhi=? WHERE period_end_epoch=?"
                + ("" if args.all_rows else " AND ghi=0 AND dni=0 AND dhi=0"),
                (stored("ghi", vals["ghi"]), stored("dni", vals["dni"]), stored("dhi", vals["dhi"]), ep),
            )
        updated += 1
    if not args.dry_run:
        conn.commit()
        if not compacted:
            # Schema v2 during the soak: readers go through the v2 view, so the new
            # irradiance has to reach the v2 tables as well.
            sync_v2_from_legacy(conn, refresh=KEYS)
    conn.close()
    print(f"{'Would update' if args.dry_run else 'Updated'} {updated} timestamp(s); "
          f"{skipped} had no archive coverage (left at 0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
