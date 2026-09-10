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
from pathlib import Path

TOTAL = "_total"
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


def build(source: Path, target: Path, days: int, *, keep_sites: bool, keep_dc: bool, only_site: str | None) -> None:
    """Copy the source and reduce it in place to one topology's fixture."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    con = sqlite3.connect(target)

    cutoff = con.execute("SELECT MAX(period_end_epoch) - ? FROM solcast_data", (days * 86400,)).fetchone()[0]
    con.execute("DELETE FROM solcast_data WHERE period_end_epoch < ?", (cutoff,))

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
    con.close()
    size = path.stat().st_size / 1024
    return (
        f"  {path.name:<20} {size:>7.0f} KB  rows={rows:<5} daylight={daylight:<5} "
        f"dc_rows={dc:<5} sites={len(sites)} ({', '.join(sites)})  {span[0]}..{span[1]}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path("data/latest_DB/solcast_solar_enhanced.db"))
    ap.add_argument("--days", type=int, default=28, help="trailing window to keep (default: the dampening look-back)")
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    args = ap.parse_args()

    sites = site_ids(args.db)
    if len(sites) < 2:
        print(f"source has {len(sites)} per-array site(s); need 2 to derive the multi-array fixtures")
        return 1
    print(f"source: {args.db}  sites: {', '.join(sites)}  window: {args.days} days\n")

    specs = [
        ("topology_single_array.db", dict(keep_sites=False, keep_dc=True, only_site=sites[0])),
        ("topology_direct.db", dict(keep_sites=True, keep_dc=False, only_site=None)),
        ("topology_dc_split.db", dict(keep_sites=True, keep_dc=True, only_site=None)),
        ("topology_shared_no_dc.db", dict(keep_sites=False, keep_dc=False, only_site=None)),
    ]
    for name, kw in specs:
        build(args.db, args.out / name, args.days, **kw)
    for name, _ in specs:
        print(describe(args.out / name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
