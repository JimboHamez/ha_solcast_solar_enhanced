# Bug: shading dampening measures actual against its own dampened forecast

Status: **fixed in 1.10.0b6, pending live validation** · Version: **1.1** · Updated: **2026-07-17**

Filed as [issue #50](https://github.com/JimboHamez/ha_solcast_solar_enhanced/issues/50).
Affects `shading_dampening.compute_dampening` / `coordinator._compute_dampening_slots`.
Confirmed against live data on 2026-07-17 (dual-site 4 kW + 4 kW install, base
integration `auto_dampen` off, factors actively pushed). The fix in §5 shipped in
1.10.0b6; the issue stays open until a live install confirms the base answers
`query_forecast_data` as expected and `undampened_records` climbs off 0.

> **One-line scope.** The shading ratio's denominator (`pv_estimate`) is read from the base's
> **already-dampened** forecast — the one our own pushed factors just modified — closing a
> feedback loop. The loop is stable, not oscillatory, and settles at **√R instead of R**.
> Impact today is ~0.5% (α is starved); at mature α it is ~20 percentage points of shading
> permanently unapplied. Fix is to read the base's undampened forecast for the denominator.

## 1. Mechanism

We push dampening factors into the base via `solcast_solar.set_dampening`
(`coordinator.py:1064`). The base applies them by multiplying its forecast
(`dampen.py:199-201`) and rebuilds `detailedForecast` from the **dampened**
`data_forecasts` set (`forecast.py:190,256`) — it keeps `data_forecasts_undampened`
separately, but that is not what the sensor attribute exposes.

We then read `pv_estimate` back out of that same `detailedForecast` attribute
(`coordinator.py:1448`, and `_site_forecast_for_period` for the per-site variant), store it
in the DB, and use it as the denominator of the shading ratio
(`shading_dampening.py:186`, `ratio = total_pv / effective_est`).

So each cycle measures actual output against a forecast we have already corrected. With true
shading ratio `R` and applied factor `f`, the measured ratio is `R/f`, not `R`.

## 2. Evidence (measured, not inferred)

Using the live HA data files (`solcast.json`, `solcast-undampened.json`,
`solcast-dampening.json`) plus the integration DB to 2026-07-17:

- **Our factors are live.** `solcast-dampening.json` carries our per-site factor shape,
  non-neutral: site `ae8c` (Ground Floor) down to **0.9337** at hour 11, site `8be0`
  (1st Floor) to **0.9858** at hour 10. The base's `auto_dampen` is off, so
  `_read_base_auto_dampen()` is not gating the push.
- **Exact applied factor**, from `solcast.json ÷ solcast-undampened.json` per period (the
  base multiplies all three percentiles by the same scalar, so this division is exact):
  `ae8c` hour 11 mean **0.9802**, minimum **0.9179**.
- **The DB stores the dampened value.** Matching each DB row's `pv_estimate` against both
  files for periods where the two differ: `ae8c` **185 dampened vs 48 undampened**; `8be0`
  **75 vs 28**. Roughly 4:1 — the contamination is real.
- **Current distortion is small.** Un-dampening the DB by the exact known factor and
  recomputing shifts factors by at most **0.0054** (`ae8c` hour 12: 0.9356 → **0.9301**).
  The direction is as predicted: correcting makes factors *more* aggressive, i.e. **the loop
  hides shading**.

## 3. Behaviour: it converges to the wrong answer

The loop does **not** oscillate. Iterating `f ← (1−α) + α·(R/f)` (`shading_dampening.py:215`)
gives the fixed point:

```
f* = [ (1−α) + √((1−α)² + 4αR) ] / 2     →     √R  as α → 1     (not R)
```

It is stable for all α < 1 (`|g'(f*)| = αR/f*² < 1`), so it settles quietly and *looks*
converged. Only at α = 1 exactly is it marginally stable.

Isolating the loop's cost from the α blend's *intended* conservatism (`clean = (1−α) + αR`),
for a real 50% shaded slot (`R = 0.5`):

| α | clean factor | loop factor | loop cost |
|---|---|---|---|
| 0.23 (today) | 0.885 | 0.898 | +0.013 |
| 0.50 | 0.750 | 0.809 | +0.059 |
| 0.90 | 0.550 | 0.723 | +0.173 |
| 0.99 | 0.505 | 0.709 | **+0.204** |

At mature confidence the system settles at 0.71 where it should sit at 0.50 — roughly **20
percentage points of real shading never applied** — while showing every sign of having
converged. The bug is invisible while α is record-starved and bites precisely when the
feature starts working.

## 4. Why the DB alone cannot detect it

Statistical detection is hopeless and a null result there means nothing: per-slot clear-sky
`actual/estimate` noise is **73%** (sd 0.725 on mean 0.995) across only ~16 clear slots per
hour bin, giving a standard error near 24% against a ≤7.5% signal. Detecting it at 1σ would
need ~160+ clear slots per hour bin.

The `solcast.json ÷ solcast-undampened.json` comparison sidesteps statistics entirely and is
the only clean test. Note also that the base scales `pv_estimate`, `pv_estimate10` and
`pv_estimate90` by the *same* scalar, so percentile ratios cannot reveal dampening.

## 5. Fix (shipped in 1.10.0b6)

Read the **undampened** forecast for the ratio denominator:

1. Source it via the base's existing `query_forecast_data` action with `undampened: true`
   (supported per `services.yaml`, optionally per `site`), which returns
   `data_forecasts_undampened`.
2. Add an additive `pv_estimate_undampened` column via the established `_ADDED_COLUMNS`
   pattern in `sqlite_store.py` (idempotent `ALTER TABLE … NOT NULL DEFAULT 0`).
3. Use it as `effective_est` in `compute_dampening` when present; fall back to today's
   behaviour when absent, so pre-existing rows keep working.

**Rejected alternative:** dividing our own factor back out of the stored value. We cannot be
sure our factors are the ones applied — the `_read_base_auto_dampen()` gate
(`coordinator.py`) means the base may be running its own automatic dampening instead, and a
user may have set manual factors.

## 6. Scope notes

- **Not affected — PV tuning.** `pv_tuning.run_tuning` fits Open-Meteo irradiance against
  `pv_actual` and never reads `pv_estimate`.
- **Not affected — the `forecast_today` sensor.** It copies the base's already-dampened
  value, which is correct as-is. It must **not** have dampening applied on our side; that
  would double-dampen.
- **Arguably correct as-is — the confidence advisory.** `load_advisory.compute_confidence`
  asks whether output tracks the forecast the *user actually sees*, which is the dampened
  one. Leave it reading the dampened estimate.
- **Backfill.** Not possible for historical rows — the base retains only ~28 days of
  undampened data. The corrected denominator applies forward only.

## 7. Related

- Record starvation keeping α ≈ 0.23 and masking this: winter α / shading suppression.
- Clear-sky reference quality (GHI-based Kt admitting beam-poor slots): discussion #47.
- Open: the `_total` aggregate previously showed clear-sky midday `pv_actual/pv_estimate` of
  1.8–2.0 and factors above 1.0 (amplifying). Per-site factors on the 2026-07-17 DB look
  sane; `_total` not yet re-checked.
