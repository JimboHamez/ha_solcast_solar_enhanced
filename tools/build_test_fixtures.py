#!/usr/bin/env python3
"""Derive one test fixture database per supported measurement topology.

The integration supports four ways a property's generation can be measured, and
they differ in what ends up in the store:

    topology        rows written              per-MPPT DC columns
    --------------  ------------------------  --------------------
    single_array    '_total' only             populated (flat MPPT keys)
    direct          '_total' + one per site   empty (microinverters expose none)
    dc_split        '_total' + one per site   populated per site
    shared_no_dc    '_total' only             empty (no per-array telemetry)

Note that `single_array` and `shared_no_dc` have the same *shape* — both write
only `_total`, because `shared_no_dc` derives an empty group list. They differ in
whether any DC telemetry exists, which is the distinction that matters to the
readers.

Everything is derived from a real dc_split store so the distributions, gaps and
irradiance are genuine rather than synthesised. Each fixture is built to be
physically self-consistent for the topology it represents:

* `dc_split` is the source, trimmed — no transformation at all.
* `direct` keeps the same rows and clears the DC columns. A direct install
  measures each array on its own AC sensor; the per-array magnitudes are the
  same, and there is no shared inverter to apportion, so the DC telemetry that
  drove the apportionment does not exist.
* `single_array` is built from **one array alone**, relabelled to `_total`, so
  output and DC telemetry describe the same physical array rather than pairing
  one tracker with two arrays' output.
* `shared_no_dc` keeps the real property-wide `_total` (both arrays summed,
  which is what one combined meter reads) and clears DC.

Schema fidelity comes from copying the source file and deleting, so every
fixture carries the exact column set, indexes and `PRAGMA user_version` the
store creates.

    python tools/build_test_fixtures.py [--db SOURCE] [--days 28] [--out tests/fixtures]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.solcast_solar_enhanced.const import (  # noqa: E402
    DAMPENING_WINDOW_BACK_DAYS,
    DAMPENING_WINDOW_FORWARD_DAYS,
)

TOTAL = "_total"

# The signed, wrapped day-of-year difference used by
# ``SqliteStore.async_get_records_for_dampening``. Copied verbatim rather than
# approximated: a fixture selected by a *different* predicate to the one under
# test would silently include or drop the boundary rows that matter most.
DOY_DELTA = "((((CAST(strftime('%j', period_end_epoch, 'unixepoch') AS INTEGER) - ?) + 548) % 366) - 182)"
DC_COLUMNS = (
    "dc_voltage1",
    "dc_current1",
    "dc_voltage2",
    "dc_current2",
    "dc_vmed1",
    "dc_vmed2",
    "dc_imed1",
    "dc_imed2",
)


def site_ids(db: Path) -> list[str]:
    """Per-array site ids in the source, most rows first, excluding the aggregate."""
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT site, COUNT(*) c FROM solcast_data WHERE site != ? GROUP BY site ORDER BY c DESC",
        (TOTAL,),
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def trailing_window(days: int) -> tuple[str, tuple]:
    """Keep the most recent ``days`` of history."""
    return (
        "period_end_epoch < (SELECT MAX(period_end_epoch) FROM solcast_data) - ?",
        (days * 86400,),
    )


def seasonal_window(target_doy: int, back: int, forward: int) -> tuple[str, tuple]:
    """Keep only the seasonal day-of-year band, **across every year present**.

    This is the band ``async_get_records_for_dampening`` actually queries, so a
    fixture built with it exercises the real selection rather than a contiguous
    slice that happens to overlap it. On a single-year source that yields one
    band; once the store spans 13+ months it yields one per year, which is the
    only way to reach the *forward* half of the window — structurally empty in
    year one, since it can only be filled from a previous year.
    """
    return (
        f"NOT ({DOY_DELTA} >= ? AND {DOY_DELTA} <= ?)",
        (target_doy, -abs(back), target_doy, abs(forward)),
    )


def seasonal_target(db: Path, target_date: str | None) -> tuple[int, str]:
    """Resolve the day-of-year to centre the seasonal band on.

    Defaults to the newest date in the source, i.e. "the season the store ends
    in", which is what a live dampening run would be asking for.
    """
    con = sqlite3.connect(db)
    if target_date:
        row = con.execute("SELECT CAST(strftime('%j', ?) AS INTEGER), ?", (target_date, target_date)).fetchone()
    else:
        row = con.execute(
            "SELECT CAST(strftime('%j', MAX(period_end_epoch), 'unixepoch') AS INTEGER),"
            " date(MAX(period_end_epoch), 'unixepoch') FROM solcast_data"
        ).fetchone()
    con.close()
    if row is None or row[0] is None:
        raise SystemExit(f"could not resolve a target day-of-year from {target_date or db}")
    return int(row[0]), str(row[1])


def build(
    source: Path,
    target: Path,
    drop: tuple[str, tuple],
    *,
    keep_sites: bool,
    keep_dc: bool,
    only_site: str | None,
) -> None:
    """Copy the source and reduce it in place to one topology's fixture."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    con = sqlite3.connect(target)

    where, params = drop
    con.execute(f"DELETE FROM solcast_data WHERE {where}", params)

    if only_site is not None:
        # One array's own rows become the whole property: its output and its DC
        # telemetry describe the same physical array, which is what a single-array
        # install actually records.
        con.execute("DELETE FROM solcast_data WHERE site != ?", (only_site,))
        con.execute("UPDATE solcast_data SET site = ?", (TOTAL,))
    elif not keep_sites:
        con.execute("DELETE FROM solcast_data WHERE site != ?", (TOTAL,))

    if not keep_dc:
        con.execute(f"UPDATE solcast_data SET {', '.join(f'{c} = 0' for c in DC_COLUMNS)}")

    con.commit()
    con.execute("VACUUM")
    con.close()


def describe(path: Path) -> str:
    con = sqlite3.connect(path)
    rows, daylight = con.execute("SELECT COUNT(*), SUM(ghi > 0) FROM solcast_data").fetchone()
    sites = [r[0] for r in con.execute("SELECT DISTINCT site FROM solcast_data ORDER BY site")]
    dc = con.execute("SELECT SUM(dc_voltage1 != 0) FROM solcast_data").fetchone()[0] or 0
    span = con.execute(
        "SELECT date(MIN(period_end_epoch),'unixepoch'), date(MAX(period_end_epoch),'unixepoch') FROM solcast_data"
    ).fetchone()
    years = [r[0] for r in con.execute(
        "SELECT DISTINCT strftime('%Y', period_end_epoch, 'unixepoch') FROM solcast_data ORDER BY 1"
    )]
    con.close()
    size = path.stat().st_size / 1024
    return (
        f"  {path.name:<26} {size:>6.0f} KB  rows={rows:<5} daylight={daylight or 0:<5} "
        f"dc_rows={dc:<5} sites={len(sites)}  years={','.join(years)}  {span[0]}..{span[1]}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path("data/latest_DB/solcast_solar_enhanced.db"))
    ap.add_argument("--days", type=int, default=28, help="trailing window to keep (default: the dampening look-back)")
    ap.add_argument(
        "--seasonal",
        action="store_true",
        help="keep the seasonal day-of-year band across EVERY year present, instead of a trailing window",
    )
    ap.add_argument(
        "--target-date",
        help="day-of-year to centre --seasonal on, as YYYY-MM-DD (default: the newest date in the source)",
    )
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    args = ap.parse_args()

    sites = site_ids(args.db)
    if len(sites) < 2:
        print(f"source has {len(sites)} per-array site(s); need 2 to derive the multi-array fixtures")
        return 1
    if args.seasonal:
        target_doy, label = seasonal_target(args.db, args.target_date)
        drop = seasonal_window(target_doy, DAMPENING_WINDOW_BACK_DAYS, DAMPENING_WINDOW_FORWARD_DAYS)
        prefix = "seasonal_"
        window = (
            f"seasonal band, doy {target_doy} ({label}), "
            f"-{DAMPENING_WINDOW_BACK_DAYS}/+{DAMPENING_WINDOW_FORWARD_DAYS} days, all years"
        )
    else:
        drop = trailing_window(args.days)
        prefix = "topology_"
        window = f"trailing {args.days} days"
    print(f"source: {args.db}  sites: {', '.join(sites)}\nwindow: {window}\n")

    specs = [
        ("single_array.db", dict(keep_sites=False, keep_dc=True, only_site=sites[0])),
        ("direct.db", dict(keep_sites=True, keep_dc=False, only_site=None)),
        ("dc_split.db", dict(keep_sites=True, keep_dc=True, only_site=None)),
        ("shared_no_dc.db", dict(keep_sites=False, keep_dc=False, only_site=None)),
    ]
    for name, kw in specs:
        build(args.db, args.out / (prefix + name), drop, **kw)
    for name, _ in specs:
        print(describe(args.out / (prefix + name)))

    if args.seasonal:
        con = sqlite3.connect(args.out / (prefix + specs[0][0]))
        n_years = con.execute(
            "SELECT COUNT(DISTINCT strftime('%Y', period_end_epoch, 'unixepoch')) FROM solcast_data"
        ).fetchone()[0]
        con.close()
        print()
        if n_years < 2:
            print(
                f"NOTE: only {n_years} year present, so the +{DAMPENING_WINDOW_FORWARD_DAYS}-day forward half of the\n"
                "      window is empty — which is exactly the year-one behaviour, but it means these\n"
                "      fixtures cannot yet exercise the year-2 seasonal path or the New Year doy wrap.\n"
                "      Re-run once the source spans 13+ months."
            )
        else:
            print(f"{n_years} years present — the forward half of the window is populated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
