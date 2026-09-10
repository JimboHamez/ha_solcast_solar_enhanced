#!/usr/bin/env python3
"""Differential (array-vs-neighbour) shading fit, and its agreement with the shipped single-array fit.

Why this exists
---------------
``shading_geometry.analyse_shading`` fits one array against the *Solcast forecast*:
``ratio = observed / expected``. Every error source Solcast carries — model bias,
a mis-stated capacity, soiling — lands in that ratio and is then split between the
capacity term ``k``, the sky-view term ``s`` and the beam mask ``f``. The module
holds ``s`` constant, which is the assumption that breaks near the horizon: an
obstruction blocking the beam blocks part of the sky dome too, so ``s`` is too
generous exactly where the mask is deepest, and the inversion can drive cells onto
the clamp floor.

Two **co-oriented** arrays remove the forecast from the measurement entirely. With
identical tilt and azimuth they see identical plane-of-array irradiance, so::

    rho = P_a / P_b = (C_a/C_b) * [f_a*b + s_a*(1-b)] / [f_b*b + s_b*(1-b)]

POA cancels, and so does Solcast. What is recovered is the *ratio* of the two
arrays' transmissions, which is common-mode-free. Shading affecting both arrays
equally still cancels and stays invisible — that limit is structural, not fixed here.

The headline number is the agreement between the two methods: the single-array
surfaces predict ``f_a/f_b`` per sky cell, and so does the differential fit. Where
they agree, the single-array fit is trustworthy; where they diverge, it is not.
CLAUDE.md quotes 0.03 above 30 deg and 0.39 below 15 deg from an earlier run of
this comparison — this script is what re-derives those figures.

Usage
-----
    python tools/differential_shading_fit.py --db data/latest_DB/solcast_solar_enhanced.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.solcast_solar_enhanced.const import (  # noqa: E402
    SHADING_AZIM_STEP,
    SHADING_BEAM_FRAC_HIGH,
    SHADING_BEAM_FRAC_LOW,
    SHADING_CLEAN_ELEV_MIN,
    SHADING_ELEV_STEP,
    SHADING_MIN_CELL_RECORDS,
)
from custom_components.solcast_solar_enhanced.pv_tuning import panel_azimuth_to_internal  # noqa: E402
from custom_components.solcast_solar_enhanced.shading_geometry import (  # noqa: E402
    _prepare,
    analyse_shading,
    compass_point,
)

# Columns async_get_records_for_shading returns; kept in one place so a schema
# change surfaces here rather than as a silent KeyError mid-fit.
COLUMNS = (
    "period_end_epoch, pv_actual, pv_estimate, pv_estimate_undampened, "
    "zenith, azimuth, ghi, dni, dhi, clouds, "
    "dc_vmed1, dc_vmed2, dc_imed1, dc_imed2"
)

ELEVATION_BANDS = (("below 15", 0.0, 15.0), ("15-30", 15.0, 30.0), ("above 30", 30.0, 91.0))


def load(db: Path, site: str) -> list[dict]:
    """Every daylight row for one site, as plain dicts."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT {COLUMNS} FROM solcast_data WHERE site = ? AND zenith < 90 ORDER BY period_end_epoch", (site,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def prepare_by_epoch(records: list[dict], tilt: float, internal_az: float, albedo: float) -> dict[int, dict]:
    """Run each record through the component's own ``_prepare``, keyed by epoch.

    Prepared one row at a time so the epoch survives the mapping. Reusing
    ``_prepare`` rather than reimplementing it is the point: the transposition,
    the undampened-denominator preference and every filter stay identical to what
    ships, so a disagreement here is a real disagreement and not a second
    implementation drifting.
    """
    out: dict[int, dict] = {}
    for record in records:
        prepared = _prepare([record], tilt, internal_az, albedo)
        if prepared:
            out[int(record["period_end_epoch"])] = prepared[0]
    return out


def cell(elevation: float, azimuth: float) -> tuple[int, int]:
    return (int(elevation // SHADING_ELEV_STEP), int(azimuth // SHADING_AZIM_STEP))


def cell_centre(c: tuple[int, int]) -> tuple[float, float]:
    return ((c[0] + 0.5) * SHADING_ELEV_STEP, (c[1] + 0.5) * SHADING_AZIM_STEP)


def differential_fit(target: dict[int, dict], reference: dict[int, dict]) -> dict:
    """Fit f_target/f_reference per sky cell from the two arrays' output ratio."""
    shared = sorted(set(target) & set(reference))
    paired = []
    for epoch in shared:
        a, b = target[epoch], reference[epoch]
        # Both arrays are co-oriented, so their beam fractions agree by construction;
        # averaging guards against a rounding difference rather than a real one.
        paired.append(
            {
                "elevation": a["elevation"],
                "azimuth": a["azimuth"],
                "beam_fraction": 0.5 * (a["beam_fraction"] + b["beam_fraction"]),
                # ratio = observed/expected against the SAME forecast for both arrays,
                # so dividing them cancels the forecast exactly.
                "rho": a["ratio"] / b["ratio"],
                "expected": a["expected"],
            }
        )

    # C_a/C_b — the intrinsic capacity ratio, on high-sun beam-dominated records
    # where neither array should be shaded.
    clean = [p["rho"] for p in paired if p["elevation"] >= SHADING_CLEAN_ELEV_MIN and p["beam_fraction"] >= SHADING_BEAM_FRAC_HIGH]
    capacity_ratio = median(clean) if len(clean) >= SHADING_MIN_CELL_RECORDS else float("nan")

    # s_a/s_b — the sky-view ratio, on HIGH-SUN overcast records only. Pooling all
    # elevations here is the bug the shipped module documents: diffuse-dominated
    # records skew to low sun, so the beam mask gets measured a second time and
    # booked as sky view.
    diffuse = [
        p["rho"] / capacity_ratio
        for p in paired
        if p["beam_fraction"] <= SHADING_BEAM_FRAC_LOW and p["elevation"] >= SHADING_CLEAN_ELEV_MIN
    ]
    sky_view_ratio = median(diffuse) if len(diffuse) >= SHADING_MIN_CELL_RECORDS else float("nan")

    buckets: dict[tuple[int, int], list[float]] = {}
    for p in paired:
        b = p["beam_fraction"]
        if b < SHADING_BEAM_FRAC_HIGH:
            continue
        # rho/C = (f_a*b + s_a*(1-b)) / (f_b*b + s_b*(1-b)). Taking the reference as
        # locally unshaded in its own denominator would reintroduce the assumption
        # this fit exists to avoid, so solve for the transmission RATIO directly:
        # with s_a/s_b known, the diffuse parts divide out to that constant.
        tau_ratio = p["rho"] / capacity_ratio
        f_ratio = (tau_ratio - sky_view_ratio * (1.0 - b)) / b
        buckets.setdefault(cell(p["elevation"], p["azimuth"]), []).append(f_ratio)

    surface = {c: median(v) for c, v in buckets.items() if len(v) >= SHADING_MIN_CELL_RECORDS}
    return {
        "capacity_ratio": capacity_ratio,
        "sky_view_ratio": sky_view_ratio,
        "n_paired": len(paired),
        "n_clean": len(clean),
        "n_diffuse": len(diffuse),
        "surface": surface,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path("data/latest_DB/solcast_solar_enhanced.db"))
    ap.add_argument("--target", default="ae8c-4cf5-2cc3-6564", help="array being measured")
    ap.add_argument("--reference", default="8be0-533e-baad-4841", help="co-oriented neighbour")
    ap.add_argument("--tilt", type=float, default=24.75)
    ap.add_argument("--azimuth", type=float, default=7.0, help="panel azimuth, Solcast convention (West positive)")
    ap.add_argument("--albedo", type=float, default=0.2)
    args = ap.parse_args()

    internal_az = panel_azimuth_to_internal(args.azimuth)
    print(f"DB          : {args.db}")
    print(f"target      : {args.target}")
    print(f"reference   : {args.reference}")
    print(f"orientation : tilt {args.tilt}deg, azimuth {args.azimuth} (Solcast) = {internal_az:.1f} (internal)")
    print("              BOTH arrays assumed co-oriented — the fit is invalid otherwise.\n")

    raw = {site: load(args.db, site) for site in (args.target, args.reference)}
    for site, rows in raw.items():
        print(f"  {site}: {len(rows)} daylight rows")

    prepared = {s: prepare_by_epoch(r, args.tilt, internal_az, args.albedo) for s, r in raw.items()}
    for site, p in prepared.items():
        print(f"  {site}: {len(p)} usable after _prepare")

    diff = differential_fit(prepared[args.target], prepared[args.reference])
    print(f"\nDIFFERENTIAL FIT ({diff['n_paired']} paired records)")
    print(f"  capacity ratio C_t/C_r : {diff['capacity_ratio']:.4f}  (from {diff['n_clean']} high-sun beam records)")
    print(f"  sky-view ratio s_t/s_r : {diff['sky_view_ratio']:.4f}  (from {diff['n_diffuse']} high-sun overcast records)")
    print(f"  cells fitted           : {len(diff['surface'])}")

    single = {s: analyse_shading(raw[s], args.tilt, internal_az, args.albedo) for s in raw}
    print("\nSINGLE-ARRAY FITS (as shipped)")
    for site, res in single.items():
        if res is None:
            print(f"  {site}: no fit (insufficient history)")
            continue
        print(
            f"  {site}: loss {res['loss_pct']}%  k={res['capacity_ratio']}  s={res['sky_view']} "
            f"({res['sky_view_source']})  cells={res['n_cells']}  floored={res['n_cells_floored']}  "
            f"worst {res['worst_transmission']} at {res['worst_elevation']}deg {res['worst_bearing']}  "
            f"mechanism={res.get('mechanism')} model_valid={res.get('model_valid')}"
        )

    t_res, r_res = single[args.target], single[args.reference]
    if not t_res or not r_res:
        print("\nCannot compare: a single-array fit is missing.")
        return 1

    def surface_of(res: dict) -> dict[tuple[int, int], float]:
        out = {}
        for key, value in res["surface"].items():
            elev, azim = (float(x) for x in key.split(":"))
            out[cell(elev, azim)] = value
        return out

    t_surf, r_surf = surface_of(t_res), surface_of(r_res)

    print("\nAGREEMENT — |single-array (f_t/f_r) - differential (f_t/f_r)| per sky cell")
    print(f"{'band':>10} {'cells':>6} {'mean':>7} {'median':>7} {'max':>7}")
    overall: list[float] = []
    for name, lo, hi in ELEVATION_BANDS:
        deltas = []
        for c, diff_ratio in diff["surface"].items():
            elev, _ = cell_centre(c)
            if not (lo <= elev < hi):
                continue
            if c not in t_surf or c not in r_surf or r_surf[c] <= 0:
                continue
            deltas.append(abs((t_surf[c] / r_surf[c]) - diff_ratio))
        overall.extend(deltas)
        if deltas:
            print(f"{name:>10} {len(deltas):>6} {sum(deltas) / len(deltas):>7.3f} {median(deltas):>7.3f} {max(deltas):>7.3f}")
        else:
            print(f"{name:>10} {0:>6} {'-':>7} {'-':>7} {'-':>7}")
    if overall:
        print(f"{'ALL':>10} {len(overall):>6} {sum(overall) / len(overall):>7.3f} {median(overall):>7.3f} {max(overall):>7.3f}")

    print("\nWORST DIFFERENTIAL CELLS (target losing most against its neighbour)")
    for c in sorted(diff["surface"], key=lambda c: diff["surface"][c])[:8]:
        elev, azim = cell_centre(c)
        print(f"  {elev:>5.1f}deg {compass_point(azim):>3} ({azim:>5.1f}) : f_t/f_r = {diff['surface'][c]:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
