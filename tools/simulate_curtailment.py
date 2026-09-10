#!/usr/bin/env python3
"""Simulate partial-interval curtailment against the shipped dampening code.

Reproduces the defects in issues #85 and #86 on synthetic data, because neither is
testable on the reference system (no home battery, and the curtailment season is
seasonal). Everything here calls ``shading_dampening.compute_dampening`` directly —
nothing is reimplemented, so the numbers are what the integration would actually do.

The array is defined as **completely unshaded**: available generation always equals
the undampened forecast, so the only thing that can move the ratio away from 1.0 is
curtailment being mistaken for shading. Any deviation the run reports is error.

Minute-level truth is generated first and then averaged to the half-hour, exactly as
``coordinator._read_pv_value`` would, so the mean-versus-instantaneous mismatch at
the heart of #86 is reproduced rather than assumed.

    python tools/simulate_curtailment.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.solcast_solar_enhanced.shading_dampening import compute_dampening  # noqa: E402

CAPACITY_KW = 8.0
EXPORT_LIMIT_KW = 5.0
CLIPPING_THRESHOLD = 0.95
HOUSE_LOAD_KW = 0.0  # Worst case: nothing absorbs the surplus locally.

TARGET_ZENITH = 30.0
TARGET_AZIMUTH = 0.0


def half_hour(
    *,
    available_kw: float,
    capped_minutes: int,
    cloudy_kw: float,
    battery_absorb_kw: float = 0.0,
) -> dict[str, float]:
    """One slot's stored row, averaged from minute-level truth.

    ``capped_minutes`` of clear sun at ``available_kw`` (curtailed to whatever the
    house plus the export limit can take), then the balance of the half hour at
    ``cloudy_kw`` with no curtailment. ``battery_absorb_kw`` is headroom the battery
    provides while it is still charging.
    """
    ceiling = HOUSE_LOAD_KW + battery_absorb_kw + EXPORT_LIMIT_KW
    clear_out = min(available_kw, ceiling)
    clear_exp = max(0.0, clear_out - HOUSE_LOAD_KW - battery_absorb_kw)

    cloudy_minutes = 30 - capped_minutes
    cloudy_exp = max(0.0, cloudy_kw - HOUSE_LOAD_KW - battery_absorb_kw)

    actual = (capped_minutes * clear_out + cloudy_minutes * cloudy_kw) / 30.0
    export = (capped_minutes * clear_exp + cloudy_minutes * cloudy_exp) / 30.0
    # Unshaded array: the forecast equals what was actually available.
    estimate = (capped_minutes * available_kw + cloudy_minutes * cloudy_kw) / 30.0
    return {"pv_actual": actual, "pv_export": export, "pv_estimate_undampened": estimate}


def record(slot: dict[str, float], epoch: int) -> dict[str, object]:
    """Wrap a slot as a store row at the target sun position under a clear sky."""
    return {
        "period_end_epoch": epoch,
        "pv_actual": slot["pv_actual"],
        "pv_export": slot["pv_export"],
        "pv_estimate_undampened": slot["pv_estimate_undampened"],
        "pv_estimate": slot["pv_estimate_undampened"],
        "zenith": TARGET_ZENITH,
        "azimuth": TARGET_AZIMUTH,
        "clouds": 0,
        "ghi": 900.0,  # Well above the clear-sky floor; Kt reads clear.
        "battery_charge": 0.0,
    }


def run(label: str, slots: list[dict[str, float]]) -> dict[str, object]:
    base = int(datetime(2026, 1, 15, 12, 0, tzinfo=UTC).timestamp())
    records = [record(s, base + i * 86400) for i, s in enumerate(slots)]
    result = compute_dampening(
        records,
        capacity_kw=CAPACITY_KW,
        cloud_threshold=30,
        cloud_max_include=60,
        clipping_threshold=CLIPPING_THRESHOLD,
        target_zenith=TARGET_ZENITH,
        target_azimuth=TARGET_AZIMUTH,
        export_limit_kw=EXPORT_LIMIT_KW,
        kt_threshold=0.75,
    )
    mean_actual = sum(s["pv_actual"] for s in slots) / len(slots)
    mean_est = sum(s["pv_estimate_undampened"] for s in slots) / len(slots)
    print(
        f"{label:<34} ratio={mean_actual / mean_est:5.3f}  "
        f"factor={result['factor']:5.3f}  alpha={result['alpha']:5.3f}  "
        f"clipped={result['forecast_clipped']:>2}  excluded={result['clipped_excluded']:>2}"
    )
    return result


def main() -> int:
    print(__doc__.strip().splitlines()[0])
    print(f"\ncapacity {CAPACITY_KW} kW | export limit {EXPORT_LIMIT_KW} kW | "
          f"house load {HOUSE_LOAD_KW} kW | array UNSHADED, so the correct factor is 1.000\n")

    n = 60  # Enough records for alpha to lift off the neutral anchor.

    print("--- baseline -------------------------------------------------------------")
    run("no curtailment", [half_hour(available_kw=4.0, capped_minutes=30, cloudy_kw=4.0)] * n)

    print("\n--- #86: does the export gate catch it? ----------------------------------")
    run("fully capped (30/30 min)", [half_hour(available_kw=8.0, capped_minutes=30, cloudy_kw=0.0)] * n)
    for minutes in (28, 25, 22, 20, 18, 16, 15, 14, 12, 10, 8, 5, 2):
        run(
            f"partly capped ({minutes}/30 min)",
            [half_hour(available_kw=8.0, capped_minutes=minutes, cloudy_kw=1.0)] * n,
        )

    print("\n--- #85: the export gate cannot run (no export limit configured) ---------")
    # A zero-export install: the battery is full, the house takes nothing, so the
    # inverter caps at ~0 export. Build the slot under the real 5 kW physical cap,
    # then hand compute_dampening export_limit_kw=0 as an unconfigured site would.
    capped = [half_hour(available_kw=8.0, capped_minutes=30, cloudy_kw=0.0)] * n
    base = int(datetime(2026, 1, 15, 12, 0, tzinfo=UTC).timestamp())
    result = compute_dampening(
        [record(s, base + i * 86400) for i, s in enumerate(capped)],
        capacity_kw=CAPACITY_KW,
        cloud_threshold=30,
        cloud_max_include=60,
        clipping_threshold=CLIPPING_THRESHOLD,
        target_zenith=TARGET_ZENITH,
        target_azimuth=TARGET_AZIMUTH,
        export_limit_kw=0.0,  # <-- the gate is disabled
        kt_threshold=0.75,
    )
    print(
        f"{'battery full, no export limit set':<34} ratio=0.625  "
        f"factor={result['factor']:5.3f}  alpha={result['alpha']:5.3f}  "
        f"clipped={result['forecast_clipped']:>2}  excluded={result['clipped_excluded']:>2}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
