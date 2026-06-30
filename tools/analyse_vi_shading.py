"""Characterise MPPT2 (ground array) morning shading from 1-sec DC V/I.

§8.4 deciding question: is the morning shading on MPPT2 *uniform dimming*
(current down, voltage holds near Vmp ⇒ clean multiplicative SVF/mask, Option B as
written) or a *hard partial shadow with bypass diodes* (voltage steps down ⇒
nonlinear, needs a horizon profile)?

MPPT1 (upper, unshaded) is the same-instant / same-temperature control:
  - uniform dimming  ⇒ V2 ≈ V1  while  I2 ≪ I1
  - bypass / shadow   ⇒ V2 markedly below V1 (and toward discrete fractions of Vmp)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CSV = "../data/40q_30day_V_I.csv"
PERSEC_START = "2026-06-17T21:54:01Z"  # per-second data begins here (hourly before)
TZ = "Australia/Melbourne"
OUT_DIR = "../analysis"

KMAP = {
    "sensor.dc_current_mppt_1": "i1",
    "sensor.dc_current_mppt_2": "i2",
    "sensor.dc_voltage_mppt_1": "v1",
    "sensor.dc_voltage_mppt_2": "v2",
}


def load() -> pd.DataFrame:
    """Load the per-second era and resample to a 1-minute grid (mean per minute)."""
    df = pd.read_csv(CSV, usecols=["entity_id", "state", "last_changed"])
    df["ts"] = pd.to_datetime(df["last_changed"], utc=True, format="ISO8601")
    df = df[df["ts"] >= pd.Timestamp(PERSEC_START)]
    df["state"] = pd.to_numeric(df["state"], errors="coerce")
    df["k"] = df["entity_id"].map(KMAP)
    df = df.dropna(subset=["state", "k"])

    cols = {}
    for k in ("i1", "i2", "v1", "v2"):
        s = df.loc[df["k"] == k].set_index("ts")["state"].sort_index()
        cols[k] = s.resample("60s").mean()
    wide = pd.DataFrame(cols).dropna(how="all")
    wide.index = wide.index.tz_convert(TZ)
    wide["hour"] = wide.index.hour + wide.index.minute / 60.0
    wide["date"] = wide.index.normalize()
    return wide


def main() -> None:
    w = load()
    print(f"per-sec rows (1-min grid): {len(w)}  span {w.index.min()} .. {w.index.max()}")

    # Reference Vmp per string: median voltage in strong midday production.
    midday = w[(w.hour >= 10) & (w.hour <= 14)]
    vmp1 = midday.loc[midday.i1 > midday.i1.quantile(0.6), "v1"].median()
    vmp2 = midday.loc[midday.i2 > midday.i2.quantile(0.6), "v2"].median()
    i1_strong = midday.i1.quantile(0.6)
    print(f"Vmp(MPPT1 upper) ~= {vmp1:.1f} V   Vmp(MPPT2 ground) ~= {vmp2:.1f} V")

    # Morning window, both strings producing, MPPT2 underproducing vs MPPT1 (= shaded).
    mor = w[(w.hour >= 6.5) & (w.hour <= 11.0)].dropna(subset=["i1", "i2", "v1", "v2"]).copy()
    mor["i_ratio"] = mor.i2 / mor.i1.replace(0, np.nan)
    producing = mor[mor.i1 > max(0.5, 0.15 * i1_strong)]
    shaded = producing[producing.i_ratio < 0.7].copy()
    print(f"\nmorning samples producing={len(producing)}  shaded(I2/I1<0.7)={len(shaded)}")

    if shaded.empty:
        print("No shaded morning samples found — cannot characterise.")
        return

    shaded["v2_over_vmp2"] = shaded.v2 / vmp2
    shaded["v2_minus_v1"] = shaded.v2 - shaded.v1
    shaded["v2_over_v1"] = shaded.v2 / shaded.v1.replace(0, np.nan)

    def pct(s, q):
        return float(np.nanpercentile(s, q))

    print("\n=== MPPT2 voltage during morning shading ===")
    print(f"  I2/I1            median {shaded.i_ratio.median():.2f}  (production deficit confirms shading)")
    print(
        f"  V2/Vmp2          median {shaded.v2_over_vmp2.median():.3f}  "
        f"[p10 {pct(shaded.v2_over_vmp2, 10):.3f}, p90 {pct(shaded.v2_over_vmp2, 90):.3f}]"
    )
    print(
        f"  V2/V1 (control)  median {shaded.v2_over_v1.median():.3f}  "
        f"[p10 {pct(shaded.v2_over_v1, 10):.3f}, p90 {pct(shaded.v2_over_v1, 90):.3f}]"
    )
    print(f"  V2-V1 (control)  median {shaded.v2_minus_v1.median():.1f} V")
    frac_low = float((shaded.v2_over_vmp2 < 0.8).mean())
    print(f"  fraction of shaded samples with V2/Vmp2 < 0.80 (bypass-like): {frac_low:.1%}")

    # Verdict heuristic.
    vmed = shaded.v2_over_vmp2.median()
    vctrl = shaded.v2_over_v1.median()
    print("\n=== VERDICT ===")
    if vmed >= 0.88 and vctrl >= 0.92 and frac_low < 0.15:
        print("  UNIFORM DIMMING — voltage holds near Vmp / near the unshaded control while")
        print("  current is depressed. The clean multiplicative SVF·DHI + mask·DNI model")
        print("  (Option B as written) is well founded; no bypass-diode signature.")
    elif vmed < 0.8 or frac_low > 0.4 or vctrl < 0.85:
        print("  HARD PARTIAL SHADOW / BYPASS DIODES — voltage collapses well below the")
        print("  unshaded control. The loss is nonlinear; Option B needs the explicit")
        print("  horizon-profile variant, not a smooth scalar.")
    else:
        print("  MIXED / INCONCLUSIVE — voltage is modestly reduced. Inspect per-morning")
        print("  rows and the example trace before committing to Option B's form.")

    # Per-morning breakdown.
    print("\n=== per-morning (shaded samples) ===")
    g = shaded.groupby(shaded.date.dt.date)
    summ = pd.DataFrame(
        {
            "n": g.size(),
            "I2/I1": g.i_ratio.median().round(2),
            "V2/Vmp2": g.v2_over_vmp2.median().round(3),
            "V2/V1": g.v2_over_v1.median().round(3),
            "V2-V1": g.v2_minus_v1.median().round(1),
        }
    )
    print(summ.to_string())
    summ.to_csv(f"{OUT_DIR}/vi_shading_per_morning.csv")

    # Clearest morning example trace (highest midday I1 that day), 06:00-11:00.
    by_day_peak = producing.groupby(producing.date.dt.date).i1.max()
    best_day = by_day_peak.idxmax()
    trace = w[(w.date.dt.date == best_day) & (w.hour >= 6) & (w.hour <= 11)][["i1", "i2", "v1", "v2"]].round(2)
    trace.to_csv(f"{OUT_DIR}/vi_shading_example_morning.csv")
    print(f"\nexample clear-morning trace ({best_day}) → {OUT_DIR}/vi_shading_example_morning.csv ({len(trace)} rows)")


if __name__ == "__main__":
    main()
