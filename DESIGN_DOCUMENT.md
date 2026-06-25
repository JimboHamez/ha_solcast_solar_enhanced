# Solcast Solar Enhanced — Design Document

**A companion integration for BJReplay/ha-solcast-solar**
**Version 1.13 — June 2026**

---

## Overview

This document describes the `solcast_solar_enhanced` integration and the design thinking behind it. It is a **standalone companion** to [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) — it installs alongside the base integration and depends on it, but is built and versioned independently rather than merged into it.

It adds three capabilities:

1. **Built-in SQLite storage** of PV power, forecasts, solar position, weather and battery data — a single zero-config file via the stdlib `sqlite3` module (no server, no credentials, no extra dependency).
2. **Automatic Rooftop PV Tuning** — tilt/azimuth optimisation via a numpy grid search (no scipy), based on Solcast SDK notebook 3.4.
3. **Adaptive Shading Dampening** — quality-weighted dampening computed purely from stored actual-vs-forecast history (it never reads the base integration's own dampening factors), ramping from a neutral no-op toward the measured ratio as data accumulates, based on Solcast SDK notebook 3.4b.

A fourth capability, **Short-range Forecast Correction**, was designed and **dropped** — see [Feature 4](#feature-4--short-range-forecast-correction-dropped).

The integration runs standalone, reading all Solcast data from the base coordinator (**zero additional Solcast API calls**) and pushing improved dampening back via the base `set_dampening` service.

### Why this exists — Solcast discontinued hobbyist PV tuning

Solcast [discontinued PV Tuning for free accounts](https://kb.solcast.com.au/pv-tuning-discontinued): home users can no longer POST measured generation back to Solcast to tune forecasts (site-measurement tuning is now a commercial tier).

This enhancement restores that on-device: it banks actual-vs-forecast history locally and computes its own tuning and dampening, never depending on Solcast's server-side tuning. Because it also folds in signals the old hobbyist tuning never had — local cloud cover (clear-sky filter), per-site geometry, multi-array DC apportionment, and export-curtailment handling — the result should be *more accurate* than the discontinued service, not a like-for-like replacement.

---

## Architecture

### File structure

```
solcast_solar_enhanced/
├── __init__.py              Integration setup, service registration
├── manifest.json            HA integration metadata
├── config_flow.py           5-step setup wizard + options flow
├── const.py                 All constants
├── coordinator.py           DataUpdateCoordinator — orchestrates everything
├── sqlite_store.py          Built-in stdlib sqlite3 store (executor jobs, WAL)
├── pv_tuning.py             Tilt/azimuth optimisation (numpy grid search)
├── shading_dampening.py     Quality-weighted dampening calculation
├── solcast_api.py           OWM client only (no Solcast API calls)
├── sensor.py                15 HA sensor entities
├── services.yaml            Service definitions
└── translations/            UI strings (11 languages)
```

### Data flow

```
base solcast_solar coordinator
        │  reads forecast + estimated actuals (no API call)
        ▼
solcast_solar_enhanced coordinator
        ├── read pv_actual     inverter sensor → avg kW (energy counter or power)
        ├── read pv_export     inverter/grid sensor → avg kW
        ├── read battery       battery sensor (falls back to raw sensor)
        ├── read per-site      multi-site: per-array kW (DC-ratio apportionment)
        ├── fetch OWM weather  (optional: temp °C, clouds 0–100, description)
        ├── fetch Open-Meteo   (keyless, default-on: plane GHI/DNI/DHI + clouds at period midpoint)
        ├── persist records    to SQLite ('_total' + one row per site, incl. ghi/dni/dhi)
        ├── run PV tuning      numpy grid search (daily, executor thread; per-site)
        ├── compute dampening  quality-weighted DB ratio blended toward neutral 1.0
        └── push dampening     → base set_dampening service (per-site)
```

All forecast data is read from `hass.data["solcast_solar"]` (with a sensor-attribute fallback), so the external HTTP calls added are the keyless **Open-Meteo** irradiance fetch (default-on) and the optional **OpenWeatherMap** weather fetch. The codebase is linted against HA 2026.5.4 (flake8/pyflakes clean), follows the current `OptionsFlow` pattern, wraps setup in `ConfigEntryNotReady` and updates in `UpdateFailed`, and uses `DeviceEntryType.SERVICE`.

---

## PV Sensor Input

`pv_actual`, `pv_export` and `battery_charge` must represent the **average power over each 30-minute period**, because Solcast's `pv_estimate` is itself a half-hourly average — the dampening ratio `total_pv / pv_estimate` is only meaningful when both sides are the same time-averaged quantity.

The integration reads the inverter's sensors directly. `_read_pv_value` (see [Feature 5](#feature-5--pv-sensor-input-modes-power-vs-energy-counter)) supports two input families with `auto` detection from unit and `state_class`:

- **Cumulative energy counter (recommended)** — `Wh`/`kWh`/`MWh`, `total_increasing`. The period average is the energy delta over the actual elapsed time (`ΔkWh / hours`) — the energy-equivalent average that matches Solcast, robust to polling drift, and free of the reset race an external averaging window introduces.
- **Power sensor** — `W`/`kW`, instantaneous or already-averaged; used directly. A legacy HA Statistics `mean_linear` sensor still works under this mode but is no longer recommended.

The DB column is named `pv_actual` (Solcast SDK terminology); the stored value is a 30-minute average power in kW. UI labels use `pv_power`.

**Safety defaults** — all three reads store `0.0` (debug-logged) when the entity is unconfigured, unavailable/unknown, not-yet-computed, or non-numeric; negative values are clamped to `0.0`. Battery prefers the Statistics sensor, falling back to the raw net/separate sensor.

---

## Feature 1 — Built-in SQLite Database Storage

Persists historical PV data alongside solar position, weather and battery state for the dampening and tuning calculations. Zero-config and on by default: a single `config/solcast_solar_enhanced.db` file via stdlib `sqlite3`.

> **Storage history.** v1.0.0 shipped a MySQL backend; v1.5.0 removed it — the integration is now SQLite-only. To carry forward MySQL history, export it to CSV before upgrading; otherwise the store starts fresh.

### Implementation

`SqliteStore` runs every call via `async_add_executor_job` under a serialising lock, in WAL mode (`synchronous=NORMAL`). The core schema is created complete on first run (so the `site` and `battery_charge` columns are always present); writes use `INSERT OR IGNORE` on `(period_end_epoch, site)`. Schema evolution is additive only: the per-MPPT `dc_*` columns (v1.6.8) and the `ghi`/`dni`/`dhi` irradiance columns are `ALTER TABLE`d into older DBs (`_ensure_columns`), backfilled to `0`.

One-time **data repairs** (not schema changes) are gated by `PRAGMA user_version`, so they run silently once and no-op thereafter. v1 recomputes the solar `azimuth` column for rows written before the hour-angle wrap fix — reconstructable in place from each row's `period_end_epoch` + site lat/lon, rewriting only rows whose value actually moved (to spare SD-card wear).

### Database schema

```sql
CREATE TABLE solcast_data (
  "index"          INTEGER PRIMARY KEY AUTOINCREMENT,
  period_end       TEXT NOT NULL,
  period_end_epoch INTEGER NOT NULL,
  period_start     TEXT NOT NULL,
  site             TEXT NOT NULL DEFAULT '_total', -- Solcast resource_id, or '_total' aggregate
  pv_actual        REAL NOT NULL,                  -- 30-min avg generation (kW)
  pv_export        REAL NOT NULL DEFAULT 0,        -- 30-min avg export (kW)
  pv_estimate      REAL NOT NULL,                  -- from Solcast via base integration
  pv_estimate10    REAL NOT NULL,                  -- Solcast p10
  pv_estimate90    REAL NOT NULL,                  -- Solcast p90
  azimuth          REAL NOT NULL,                  -- solar azimuth at period midpoint (°)
  zenith           REAL NOT NULL,                  -- solar zenith at period midpoint (°)
  temp             REAL NOT NULL,                  -- OWM temperature (°C)
  clouds           INTEGER NOT NULL,               -- OWM cloud cover (0–100)
  description      TEXT NOT NULL,                  -- OWM weather description
  battery_charge   REAL NOT NULL DEFAULT 0,        -- 30-min avg battery charge (kW)
  dc_voltage1      REAL NOT NULL DEFAULT 0,        -- MPPT 1 DC voltage, slot max (V)
  dc_current1      REAL NOT NULL DEFAULT 0,        -- MPPT 1 DC current, slot min (A)
  dc_voltage2      REAL NOT NULL DEFAULT 0,        -- MPPT 2 DC voltage, slot max (V)
  dc_current2      REAL NOT NULL DEFAULT 0,        -- MPPT 2 DC current, slot min (A)
  ghi              REAL NOT NULL DEFAULT 0,        -- Open-Meteo global horizontal irradiance (W/m²)
  dni              REAL NOT NULL DEFAULT 0,        -- Open-Meteo direct normal irradiance (W/m²)
  dhi              REAL NOT NULL DEFAULT 0,        -- Open-Meteo diffuse horizontal irradiance (W/m²)
  UNIQUE(period_end_epoch, site)
);
```

The four `dc_*` columns are kept **per-tracker** (not aggregated, up to `MAX_MPPT_TRACKERS = 2`) so a future per-string `Vmp`-band calibrator can learn each string; per-site rows carry that site's trackers, `_total` the property-wide ones. They are forward-only (not reconstructable on older rows). See the [curtailment roadmap](#curtailment-aware-actualforecast-filtering-dc-telemetry-off-mpp-detection).

The three irradiance columns (`ghi`/`dni`/`dhi`) are the plane-of-array inputs for transposition-based PV tuning, collected from **Open-Meteo** (keyless) at the period midpoint. Unlike the `dc_*` columns they **are** reconstructable on older rows — from each row's `period_end_epoch` + site lat/lon against Open-Meteo's historical archive — so `tools/backfill_irradiance.py` can fill them in one pass (interpolated to the midpoint) instead of waiting for fresh collection.

To browse the file, point the [sqlite-web add-on](https://github.com/hassio-addons/addon-sqlite-web) at it (WAL mode — leave the `-wal`/`-shm` sidecars in place).

### Total PV energy balance

```
total_pv = pv_actual
```

`pv_actual` is the inverter's total AC output — it already includes self-consumption, grid export and battery charging, so adding `pv_export` or `battery_charge` would double-count. Both are still stored for diagnostics. `total_pv` drives the dampening ratio (`total_pv / pv_estimate`), clipping detection (`total_pv ≥ capacity × clipping_threshold`) and the tuning RMSE. **Configure `pv_actual` to the inverter generation meter, not a self-consumption-only meter** — the latter produces systematically low factors and poor tuning.

---

## Feature 2 — Rooftop PV Tuning

Recovers panel **tilt** by transposing measured irradiance to the panel plane and fitting it to measured `total_pv`. Follows the approach of [Solcast SDK notebook 3.4](https://solcast.github.io/solcast-api-python-sdk/notebooks/3.4%20Rooftop%20PV%20Tuning/) — comparing measured generation against a *per-orientation* model — but adapted to run **offline**: the notebook re-queries the Solcast API for a fresh physical forecast at each candidate orientation, which we can't do on a Pi, so we transpose stored **Open-Meteo** irradiance (GHI/DNI/DHI) instead.

> **Why not the old cosine-ratio tuner?** The previous implementation had only the single configured-orientation `pv_estimate` and re-scaled it by `cos(incidence)/cos(incidence at the seed)`. That normalisation is identity at the seed, so the RMSE surface was flat and the result simply **echoed the configured orientation back** — seed-degenerate, not a measurement. Transposing real per-orientation irradiance removes that degeneracy.

### Algorithm

1. Fetch up to 2000 recent **clear-sky** records (`pv_actual > 0`), including `ghi`/`dni`/`dhi`. The clear-sky gate is applied **in SQL before the `LIMIT`** (see *Clear-sky selection* below) so the result is the most recent 2000 *clear-sky* rows, not the most recent rows of any weather.
2. Skip rows lacking irradiance (`ghi = 0`). The in-tuning cloud re-filter is **disabled** (threshold `101`) when the SQL Kt gate already ran, so it can't wrongly drop rows when OWM is absent and `clouds` is the `100` sentinel.
3. Exclude clipped records (`total_pv` **and** `pv_estimate` ≥ `capacity × clipping_threshold`).
4. Exclude export-limited records (`pv_export ≥ export_limit_kw × clipping_threshold`, when set) — see below.
5. For each candidate **tilt** (azimuth held fixed — see below), transpose to plane-of-array irradiance (Hay-Davies anisotropic sky by default, isotropic fallback), fit a single capacity scale by least squares (`s = Σ(poa·obs)/Σ(poa²)` — closed-form, no scipy), and score **MAE** against `total_pv`.
6. Minimise MAE via a coarse-to-fine numpy **1-D grid search** over tilt (`_minimize_tilt`: full 5° sweep, then ±5° at 1°, then ±1° at 0.25°). ~30 evaluations; no seed dependence; ~750 ms on a Pi. Drops scipy (no Pi wheel, issue #85).
7. Run in an executor thread; requires ≥10 qualifying irradiance-bearing records; runs daily.

**Azimuth is held fixed at the configured value, not tuned.** It is non-identifiable from this data: a shift in the irradiance↔power time alignment is mathematically degenerate with a panel rotation (~1° of azimuth per ~3 min of offset), and morning shading biases any azimuth estimate westward regardless of method. Fitting it would do more harm than good, so the tuner recovers tilt only; the result echoes the configured azimuth back (the dampening gate's azimuth delta is then identically 0). Recovering azimuth would need precisely-timestamped 15-min irradiance *and* shading-aware AM/PM weighting — a separate workstream.

Solar position (azimuth/zenith, ±1°) is computed locally in `pv_tuning.py` from declination, equation of time and hour angle — no extra library — and the same function populates the `azimuth`/`zenith` columns at write time.

### Clear-sky selection — clearness-index (Kt) gate

Tuning needs sun-angle diversity across seasons; the clear-sky rows are chosen in `async_get_records_for_tuning` by one of **two mutually exclusive gates**, both applied in SQL before the `LIMIT`:

- **Kt gate (preferred, used whenever Open-Meteo irradiance is enabled — the default).** A row qualifies when the **measured clearness index** `Kt = ghi / clearsky_ghi(zenith) ≥ CONF_KT_THRESHOLD` (default `0.75`, configurable 0.5–1.0), judged only where the sun is meaningfully up (`zenith < KT_ZENITH_MAX = 85°`) and the clear-sky reference is non-trivial (`clearsky_ghi(zenith) ≥ KT_GHI_CS_FLOOR = 40 W/m²`). `clearsky_ghi` is the pure-Python Haurwitz model registered as a SQLite function so the gate runs in-query; the comparison is written in multiply form (`ghi ≥ Kt × clearsky_ghi`) to avoid dividing by a near-zero reference.
- **Cloud gate (fallback, only when Open-Meteo is disabled).** The legacy OWM total-cloud gate `clouds < cloud_threshold`.

**Why Kt replaced total cloud.** Total-cloud % over-rejects genuinely clear slots that happen to carry harmless **high/mid cloud** (cirrus, distant cumulus) — those slots have near-full ground irradiance but a non-trivial reported cloud fraction, so a `clouds < 30%` gate discards usable clear-sky data. `Kt` measures the *actual* irradiance reaching the ground relative to a clear-sky reference, so it admits those slots and excludes only real attenuation — independent of any model cloud %. On the real winter DB this widened the clear-sky set roughly 9× (≈2 records under the cloud gate → ≈18 under Kt ≥ 0.75), which is what let the transposition tuner converge where the legacy path returned "insufficient". The Kt gate shipped in **v1.7.0**, built on the Open-Meteo transposition work.

> The dampening clear-sky selection (Feature 3) still uses the cloud-cover gate/weighting; moving it onto measured Kt is a planned follow-up.

### Export limit filtering

A site with a grid export cap produces artificially low `total_pv` while `pv_export` is pegged at the limit, which would pull the optimiser toward a shallower/more-northerly geometry. Records at the ceiling are excluded:

```
is_export_limited = export_limit_kw > 0 AND pv_export >= export_limit_kw × clipping_threshold
```

Reusing `clipping_threshold` keeps marginal export values; `export_limit_kw = 0` (default) disables it. This filter matters more now: with a capacity scale fitted across all records, a cluster of export-curtailed points would drag the fit. Results surface on the `Tuned Panel Tilt` sensor (`azimuth` — the fixed configured value, `azimuth_tuned: false`, `rmse_kw`, `mae_kw`, `capacity_scale`, `n_records` attributes) and the Configure page. The `battery_full + export_capped` double-curtailment case remains a known AC-side limitation, addressed by the DC-telemetry [roadmap](#curtailment-aware-actualforecast-filtering-dc-telemetry-off-mpp-detection).

---

## Feature 3 — Adaptive Shading Dampening

Computes per-hour dampening factors from historical clear-sky actual-vs-estimate ratios. Based on [Solcast SDK notebook 3.4b](https://solcast.github.io/solcast-api-python-sdk/notebooks/3.4b%20Rooftop%20Shading%20Corrections/). Because `pv_actual` is a 30-minute average, within-period cloud transients are already smoothed, making the ratio a stable input.

### The "tuned estimate" prerequisite, and our convergence gate

Notebook 3.4b requires a **tuned** Solcast estimate as input: 3.4 first corrects tilt/azimuth/capacity, then 3.4b computes the *residual* `measured / estimate` ratio. Running shading on an un-tuned estimate makes each factor silently absorb orientation error as well as shading.

This integration follows the same tune→shade shape, but the tuning loop is **advisory**: `compute_dampening` consumes the raw base forecast, and `run_tuning`'s output is surfaced only on the Tuned Panel Tilt/Azimuth sensors — never fed back into the estimate. The user closes the loop by applying the suggested orientation in their **Solcast account**. So our dampening is a **residual-bias** correction that equals "shading" only when the Solcast site is well-configured.

To stop a mis-configured site baking orientation error into the curve, the push is **gated**: in `_run_dampening`, `_orientation_diverged` compares the latest tuning result against the configured seed. When tuning is confident (`n_records ≥ DAMPENING_GATE_MIN_RECORDS`, 50) **and** tilt or azimuth diverges materially (`|Δtilt| > 15°` or shortest-circle `|Δazimuth| > 25°`), that target's factors are forced to neutral `1.0` and a `dampening_gated` repair issue tells the user to apply the tuned values. The gate is per-site aware (each site judged against its own seed) and on by default (`CONF_DAMPENING_GATE`).

### Why cloud filtering is essential

The factor `total_pv / pv_estimate` reflects shading geometry on a clear day but cloud attenuation (already modelled by Solcast) on a cloudy one — including cloudy records corrupts the factor, so a cloud signal is needed to filter them. That signal comes from either source: **Open-Meteo** supplies a keyless `cloud_cover` (default-on), and **OWM** supplies one when configured. When OWM is absent, Open-Meteo's `clouds` doubles as the cloud source, so **OWM is now optional** — a cloud source is required, but not specifically OpenWeatherMap. (Tuning's clear-sky selection has moved off cloud cover entirely onto the measured Kt gate — see [Feature 2](#clear-sky-selection--clearness-index-kt-gate); dampening still uses the cloud-cover weighting below.)

The design is **fail-safe**: when *no* cloud source is available (OWM unconfigured **and** Open-Meteo disabled, or a fetch fails), cloud cover defaults to *unknown* and is coerced at the DB-write boundary to the **`100` sentinel** — a value the clear-sky filter excludes. So such a record can never masquerade as clear sky: tuning finds nothing to fit (returns `None`), dampening reports `no_data` (stays neutral, pushes nothing), and the Cloud Cover / Weather sensors show *unavailable* rather than a misleading `0`. `async_setup` raises an `ISSUE_OWM_REQUIRED` repair issue only when a cloud-driven feature is enabled with **neither** Open-Meteo nor OWM configured; enabling either source clears it on reload.

*Why `100`, not `0`:* both are valid real readings, so the unknown sentinel must sit on the excluded side. `0` collides with real clear sky (would be trusted); `100` collides with real overcast (already excluded — safe). The same reasoning fixed the falsy-`0` bug in v1.6.2/3.

### Cloud quality weighting

| Cloud cover | Weight |
|---|---|
| Below threshold | 1.0 — clear sky, full quality |
| Threshold to 1.5× threshold | 0.6 — marginal |
| 1.5× threshold to max\_include | 0.3 — poor but usable |
| Above max\_include | 0.0 — excluded |

Default threshold 20% (configurable 10–50%); default max\_include 60%.

### Geometric proximity weighting

Each record is weighted by how close its solar geometry is to the target slot, since shading from nearby objects is highly angle-dependent:

```python
zenith_weight  = exp(-0.5 × (Δzenith  / 10°)²)
azimuth_weight = exp(-0.5 × (Δazimuth / 20°)²)
combined_weight = cloud_weight × zenith_weight × azimuth_weight
```

### Seasonal window

A `±14-day` day-of-year window is applied in the DB query across all years. Early on it gives natural extrapolation from seasonally-similar dates; as the DB grows, same-year matches dominate. The same clipping detection used in tuning excludes clipped records before they contribute (counted in `hour_XX_clipped_excluded`).

### Confidence model (α blending)

The DB factor is blended with a **neutral `1.0` anchor** — the base integration's own factors are never read in:

```
final_factor(h) = (1 − α) × 1.0  +  α × db_factor(h)

α        = x² / (x² + midpoint²)
x        = quality_weighted_count (Σ combined_weight per record)
midpoint = 30 / average_quality
```

So with little data the factor sits near a no-op `1.0` and ramps toward the measured ratio as confidence builds. Scaling the midpoint by average quality means a looser cloud threshold needs proportionally more records before the DB factor is trusted.

| Quality-weighted records | α (20% thr, avg q 0.9) | α (35% thr, avg q 0.5) |
|---|---|---|
| 0 | 0.00 | 0.00 |
| 10 | 0.10 | 0.04 |
| 30 | 0.50 | 0.20 |
| 60 | 0.80 | 0.50 |
| 100 | 0.92 | 0.74 |

**Early stability clamp:** when α < 0.5 the result is constrained to ±15% of `1.0` (0.85–1.15), so a single anomalous day can't distort the curve while data accumulates. A slot with no usable data stays at `1.0` (`no_data`), so dampening works from day one — a fresh install pushes neutral factors until data builds.

**Convergence guidance by climate** (illustrative for an unconstrained site; see the curtailment caveat in the [roadmap](#curtailment-aware-actualforecast-filtering-dc-telemetry-off-mpp-detection)):

| Climate | Threshold | Expected time to full confidence |
|---|---|---|
| Clear (Perth, inland QLD) | 20% | 4–6 weeks |
| Mixed (Melbourne, Sydney) | 20–25% | 8–12 weeks |
| Overcast (Hobart, coastal) | 30–35% | 6–10 weeks at relaxed threshold |

### Resolution, schedule and diagnostics

The calculation runs at **48 half-hour slots/day**, each with its own α; adjacent pairs are averaged into 24 hourly values for `set_dampening` (which accepts hourly or half-hourly). Recomputed every 6 hours (or via the `run_dampening_update` service). The `Dampening Hours with DB Data` sensor exposes per-hour diagnostics:

```yaml
hour_14_factor:           0.847      # final blended value pushed
hour_14_alpha:            0.72       # DB confidence (0 = neutral, 1 = pure DB)
hour_14_source:           db_blended # db_history | db_blended | no_data | night
hour_14_quality_records:  31.4       # quality-weighted record count
hour_14_avg_quality:      0.81       # mean combined_weight of contributors
hour_14_clipped_excluded: 2          # shown if > 0
overall_source:           db_blended
```

---

## Feature 4 — Short-range Forecast Correction (Dropped)

> **Status: evaluated and dropped (v1.3.0).** Recorded for the design record.

The idea was to nudge the next 1–6 hours of forecast from the recent `total_pv / pv_estimate` ratio with an exponentially-decaying correction. It was dropped because:

- The near-term deviation signal is cloud-driven and decays within an hour or two, so the nudge approaches a no-op by +3 — exactly where forecast error is largest.
- It would second-guess Solcast's imagery-based near-term product with a cruder single-inverter ratio plus coarse OWM cloud.
- A now-relative, decaying, per-horizon correction can't go through `set_dampening` (indexed by local time-of-day), so it would fork the forecast into separate "corrected" sensors, forcing users to rewire automations.
- The durable, predictable part is already captured by the DB-driven dampening, whose ±14-day window also covers individual missing slots.

No implementation is planned.

---

## Feature 5 — PV sensor input modes (power vs energy counter)

The original design required HA Statistics `mean_linear` sensors, which introduce a **race** — the external averaging window can reset on its own schedule, unsynchronised with the 30-minute poll, so a read can catch a half-reset value — and assume a perfectly-spaced cadence.

`_read_pv_value(entity_id, mode, key, now_epoch)` resolves each sensor in one of two families (`auto` detects from unit + `state_class`):

- **Power** (`power_kw`, `power_w`) — converted to kW and used directly (the classic Statistics path).
- **Energy counter** (`energy_kwh`/`wh`/`mwh`, `total_increasing`) — the interval average is the energy delta over the **actual** elapsed time:

  ```
  avg_kW = (counter_now − counter_prev) / ((epoch_now − epoch_prev) / 3600)
  ```

  Dividing by real elapsed time (not a hard-coded 1800 s) is robust to polling drift. Returns `0.0` (excluded by the `pv_actual > 0` filters) on the first read after restart (no baseline), a negative delta (reset/rollover), or an elapsed time outside `[0.5×, 2×]` the interval. Baselines `{value, epoch}` persist across restarts via HA `Store`.

This keeps `pv_actual` in the same unit as `pv_estimate`, so the tuning/dampening maths is unchanged.

---

## Feature 6 — Multi-site support

Solcast lets a user define multiple rooftop arrays on one property, each at a different orientation. Tuning one tilt/azimuth across them is meaningless, and shading differs per array, so each site is stored, tuned and dampened independently **where the hardware allows**.

**Governing constraint — tuning granularity is capped by measurement granularity:**

| Measurement | Per-array tuning |
|---|---|
| Dedicated AC sensor per array (e.g. Enphase) | ✅ direct |
| Shared inverter AC + per-MPPT DC (e.g. Fronius) | ✅ via DC-ratio apportionment |
| Shared AC, no per-MPPT DC | ❌ not observable |

### DC-ratio apportionment

For a string inverter exposing one AC total plus per-MPPT DC, the AC is split by each string's share of total DC:

```
ac_arrayᵢ = ac_total × (dcᵢ / Σ dc)
```

Since `ac_total ≈ η × Σ dc` (η ≈ constant), this yields each array's production in the AC domain (matching Solcast), sums back to the metered total, and handles clipping proportionally. Guarded against `Σ dc ≈ 0`.

### Discovery, config model and storage

`discover_sites(hass)` (shared by coordinator and config flow) enumerates the base RooftopSensors, reading `resource_id`, `name`, `capacity`, `capacity_dc`, `tilt`, `azimuth`, `compass_degrees`. Orientation seeds per-site tuning; `resource_id` keys storage and targets `set_dampening`.

`CONF_SITE_GROUPS` is a list of measurement groups — either a single-site group (`site` + `ac_sensor`) or a DC-apportioned group (a `strings` list of `{site, dc_sensor}`), each optionally carrying an `mppts` list of per-tracker voltage/current capture sensors. The config-flow `sites` step collects per-site fields and `_derive_groups()` groups sites sharing an AC sensor (shared → apportioned; alone → single; shared-without-DC → omitted). Note the two DC roles are distinct: `dc_sensor` is **power** (apportionment ratio only); `mppts` is **instantaneous voltage/current** (curtailment capture).

**Field placement by topology (v1.8.0).** Every field lives in exactly one step, decided by topology so nothing is entered twice. Site discovery runs in Step 1 (`_is_single_site`, cached) so the wizard knows the topology before rendering. `_build_site_schema(..., single_site=)` shows the flat per-inverter MPPT V/I fields (`CONF_MPPT*`) on Step 1 **only** for single-array systems; multi-array systems map MPPT trackers per array in the `sites` step instead, and Step 1 hides them. The `sites` step prefills each array's generation field from `CONF_PV_ACTUAL_SENSOR` (`default_ac`, correct for a single inverter feeding several arrays — the shared-meter case) and migrates any pre-existing flat MPPT keys into per-array suggestions (`_seed_flat_mppt`), clearing them on save (`_clear_flat_mppt`). Because multi-array systems no longer carry the flat keys, `MpptDcSensor`'s `max_voltage` spans the property-wide **and** per-site trackers so the diagnostic stays populated.

The `site` column (default `'_total'`) and `(period_end_epoch, site)` key let each site own its rows. Each cycle writes the `_total` row **plus** one per site; `pv_export` is replicated onto site rows (for export-clip exclusion), `battery_charge` stays on `_total`. Aggregate tuning/dampening pass `site='_total'`; per-site runs pass the `resource_id`. In single-site installs everything is `_total`, so behaviour is identical and per-site logic is inert.

Per-site **tuning** (`_run_site_tuning`) fits each site against its own rows, seeded from its Solcast orientation (azimuth converted to the tuner's frame), surfaced as a `per_site` attribute. Per-site **dampening** is pushed via `set_dampening` with the site's `resource_id`, overriding the base global for that site.

---

## Sensors (15 total)

| `_attr_name` | Unit | Description |
|---|---|---|
| Forecast Now | kW | Current 30-min PV forecast |
| Forecast Today | kWh | Total forecast for today |
| Tuned Panel Tilt | ° | Optimised tilt (`per_site` attribute in multi-site mode) |
| Tuned Panel Azimuth | ° | Optimised azimuth |
| Tuning RMSE | kW | Goodness of fit |
| Tuning Export Limited Excluded | — | Records dropped by the export-limit filter last run |
| Database Records | — | Total DB record count |
| MPPT DC Voltage (max) | V | Diagnostic: latest captured DC telemetry (max string voltage across the property-wide *and* per-site trackers; per-tracker V/I + per-site in attributes). Unavailable when no DC sensors configured |
| Dampening Hours with DB Data | — | Hours with DB-derived factors (per-hour diagnostics in attributes) |
| Weather Temperature | °C | OWM temperature |
| Cloud Cover | % | OWM cloud cover |
| Battery Charge 30min Average | kW | Configured battery sensor value (restored across restarts) |
| PV Power 30min Average | kW | Period-average generation (restored across restarts) |
| PV Export 30min Average | kW | Period-average export (restored across restarts) |
| Base Integration Status | — | connected / not_detected |

All use `_attr_has_entity_name = True`, `_attr_should_poll = False`, unique IDs `f"{DOMAIN}_{entry_id}_{key}"`, and `DeviceEntryType.SERVICE`. The three 30-min averages extend `_RestoringSensorBase` (HA `RestoreSensor`) so they restore their last value after a restart rather than reading *unknown* until the first half-hour cycle.

---

## Services

| Service | Description |
|---|---|
| `run_pv_tuning` | Force PV tuning immediately (requires DB) |
| `run_dampening_update` | Force dampening recalculation (DB or fallback) |
| `fetch_weather` | Force OWM weather fetch |

---

## Configuration

No helper sensors are required — map the inverter's sensors directly. The wizard has 5 steps, plus a **Per-site sensor mapping** step shown automatically when more than one Solcast site is detected ([Feature 6](#feature-6--multi-site-support)). The per-step fields are documented in the [README](README.md#configuration); the full key/default reference:

| Key | Default | | Key | Default |
|---|---|---|---|---|
| latitude | -37.9 | | owm\_enabled | False |
| longitude | 145.0 | | owm\_api\_key | "" |
| capacity\_kw | 5.0 | | battery\_enabled | False |
| tilt | 20.0 | | battery\_mode | net |
| azimuth | 0.0 | | battery\_net\_sensor | "" |
| pv\_actual\_sensor | "" | | battery\_charge\_sensor | "" |
| pv\_export\_sensor | "" | | auto\_tuning | True |
| battery\_stat\_sensor | "" | | auto\_dampening | True |
| mppt{1,2}\_{voltage,current}\_sensor | "" | | cloud\_threshold | 20 |
| db\_enabled | True | | cloud\_max\_include | 60 |
| db\_retention\_days | 0 | | clipping\_threshold | 0.95 |
| | | | export\_limit\_kw | 0.0 |

Azimuth uses the Solcast convention (0°=North, positive=West), converted to the internal East-positive frame for tuning (`panel_azimuth_to_internal`). The OWM endpoint is the free Current Weather Data API (`GET /data/2.5/weather`, ~48 calls/day vs the 60/min free limit), parsing `main.temp`, `clouds.all`, `weather[0].description`.

---

## How it was layered

The three features were added in order of increasing risk, each independent and behind its own toggle, so any one can be disabled without affecting the others.

1. **DB storage + OWM weather (foundation).** PV sensor fields in config step 1; a storage toggle and OWM key in options; the coordinator reads sensors and persists. **New dependency: none** (stdlib `sqlite3`).
2. **Adaptive dampening.** `shading_dampening.py`; 6-hourly recalculation; the DB factor blended via the confidence model. Inert when the DB is disabled. **No new dependencies.**
3. **PV tuning (optional).** `pv_tuning.py`; daily tilt/azimuth optimisation behind the `auto_tuning` toggle; lazy numpy import. **New dependency: `numpy>=1.21.0`** (ships with HA; no scipy).

A fourth feature, short-range forecast correction, was designed and dropped ([Feature 4](#feature-4--short-range-forecast-correction-dropped)).

---

## Roadmap

### Database retention (implemented)

`CONF_DB_RETENTION_DAYS` (Storage step; default `0` = keep everything) prunes rows older than the window via `SqliteStore.async_prune` (`DELETE … WHERE period_end_epoch < cutoff`) on a daily timer, independent of auto-tuning. No `VACUUM` — in the steady state SQLite reuses freed pages and the file size stabilises. A value below `DB_RETENTION_MIN_RECOMMENDED_DAYS` (≈13 months) logs a warning (seasonal dampening uses a cross-year window) but is still honoured.

### Indexed day-of-year column for the seasonal dampening scan

The dampening query filters on a *computed* day-of-year expression (`strftime('%j', …)`), which no index can serve — so it is a full table scan that slows on multi-year DBs on SD-card I/O. (The 48× redundant re-scan was already removed.) **Option:** persist and index a UTC day-of-year column at insert time, turning the scan into an indexed range lookup (a schema add + one-time backfill, gated by the existing `PRAGMA user_version` mechanism). **Deferred** — the retention option above already bounds the row count; revisit if per-query cost matters when retention is left at *keep everything*.

### Curtailment-aware actual/forecast filtering (DC-telemetry off-MPP detection)

**Problem.** When clear-sky output exceeds household load plus the export limit, the inverter *curtails* — `pv_actual` stops measuring available generation, corrupting the actual-vs-forecast comparison on exactly the clear-sky days tuning and dampening depend on. This already affects any site whose export limit sits below its clear-sky peak, and becomes near-universal as two schemes roll out: **variable (dynamic) export limits** set by the DNSP, and **emergency backstop** throttling operated at the market/system level (AEMO/ARENA). They differ only in who sets the constraint and how often it changes; on the DC side they are the same off-MPP excursion, which is why the Tier-1 signal below subsumes both cause-agnostically.

Measured on a 12 k-row Melbourne DB (single 5 kW-export site): ~50% of high-sun clear-sky records (Oct–Apr) are curtailed — clustering in summer, vanishing in deep winter — *inverting* the "clearer = faster convergence" intuition. The raw clear-sky `actual/forecast` ratio reads **0.890**, an apparent 11% shading penalty that is mostly curtailment; two independent corrections both recover **≈0.955**, i.e. ~5% real shading masked by ~6% spurious curtailment.

**Current state (heuristic, AC-side).** Both consumers now handle export curtailment:

| Consumer | Method |
|---|---|
| Tuning (`run_tuning`) | excludes export-limited records (`pv_export ≥ export_limit × threshold`) |
| Dampening (`compute_dampening`) | clips the forecast to the achievable ceiling so a curtailed record contributes ≈1.0 |

Both infer curtailment from the AC side (output flat, export pegged) — so they are forecast-/limit-dependent, cause-blind, and miss the `battery-full + export-capped` case. DC telemetry removes those limits.

**The off-MPP signal (why DC voltage is ground truth).** Curtailment is a DC-side phenomenon. A PV string is a current source; to deliver less power the inverter walks the operating point off MPP **up the I-V curve toward `Voc`** — voltage rises, current collapses. So an elevated DC string voltage is a *direct measurement* of curtailment, independent of forecast and export limit, and identical regardless of cause. It also unifies the two AC heuristics: inverter clipping and export curtailment are the same off-MPP excursion, so one measured flag subsumes both.

**Tiered detection (graceful degradation).** Because per-string DC telemetry is opt-in and brand-dependent, the best available tier is used:

| Tier | Signal | Catches |
|---|---|---|
| 1 (best) | per-MPPT DC voltage (+ current) → off-MPP | export curtailment **and** inverter clip, cause-agnostic, limit-independent |
| 2 | `pv_export ≥ export_limit × threshold` (ideally the *dynamic* limit) | export curtailment only |
| 3 | `total_pv ≥ capacity × clipping_threshold` (existing) | inverter AC clip only |

Within Tier 1, each extra DC channel removes a specific failure mode, so more data buys strictly higher accuracy:

- **Voltage alone** — curtailment is definitionally an excursion toward `Voc`; the single most informative channel. Blind spot: a cold clear morning sits at a naturally high `Vmp` *at* MPP → false positive.
- **+ current** — resolves the cold-morning case: curtailment is high-V **and** low-I; genuine MPP is high-V **and** high-I.
- **Per-MPPT (not inverter-aggregate)** — curtailment is enforced at the AC setpoint but distributes *unequally* across strings; only per-string voltage sees which were throttled, at the granularity per-site tuning/dampening already use.
- **+ temperature context** — `Vmp`/`Voc` drift ~−0.3%/°C, so any fixed voltage line is climate-specific. Learning the `Vmp` band from high-current (provably-at-MPP) intervals gives a relative, temperature-tracking threshold needing no user input.

**Consumer wiring (independent of tier).** Tuning **excludes** a flagged record (a flat-topped peak has no geometry to fit — costs ~50% of high-sun clear-sky records at an export-limited site, hence the ~2× slower tuning caveat). Dampening **clips the forecast** to the achievable ceiling (`min(pv_estimate, load + export_limit)`) so the record still contributes ≈1.0 with none discarded — or, with a hard Tier-1 flag, simply neutralises it.

**Storage shape.** Per-record `dc_voltage1/current1/voltage2/current2` (up to `MAX_MPPT_TRACKERS = 2`), kept **per-MPPT** so a later `Vmp`-band calibrator can learn each string; per-site rows carry that site's trackers, `_total` the property-wide ones. Still to add when detection lands: `export_limit` (the active, possibly dynamic, limit) and a derived `curtailed` boolean (`_total.curtailed = OR` across strings). All forward-only. The DC read is **aggregated over the slot** — max voltage (most off-MPP) and min current (most throttled) from recorder history (`_interval_values` → `get_significant_states`), falling back to the instantaneous state so users can point at raw per-string sensors.

**Hardware applicability.** The integration consumes HA *entities*, so this works wherever the upstream integration surfaces per-string DC voltage (+ current). **SunSpec Model 160** over Modbus is the common denominator — SMA, Huawei, Sungrow, GoodWe, SolaX, Victron (via GX), Fronius all expose it. Cloud APIs (Growatt/SolarEdge/Solar.web) are unsuitable (latency/rate-limits break per-half-hour sampling). **SolarEdge** is a structural exception: per-panel optimizers hold the string at a fixed DC-bus voltage, so the off-MPP fingerprint never appears — Tier-2 only.

**Rollout.**
1. **Implemented (Phase 1, data-only).** Export-aware **dampening**: `compute_dampening` takes `export_limit_kw` (from the base `site_export_limit`, manual fallback) and clips the forecast to `total_pv + (export_limit − pv_export)`, floored at delivered output (ratio ≤ 1.0). Curtailed clear-sky records contribute ≈1.0 instead of a penalty, none discarded; a `forecast_clipped` count is surfaced per hour. Validated on the reference DB: high-sun `db_factor` recovers 0.909 → 0.943. Works on the existing database.
2. **Implemented (Phase 2, capture).** Paired per-MPPT telemetry banked each cycle: schema columns (additive `ALTER TABLE`, legacy rows → 0), flat config keys on the site step + per-site fields in the multi-site step (derived into an `mppts` list), and a batched `get_significant_states` read taking max-voltage/min-current over the slot. **Confirmed logging real production data** — a full clear day yields a clean `Vmp` band with `Voc` at first light. Capture only; nothing acts on it yet.

**Still to do** before promotion (waiting on accumulated telemetry): the per-string `Vmp`-band calibrator, the `curtailed` flag + `export_limit` column, and wiring detection into the consumers. *Wing-reconstruction* (fit the clear-sky curve to a day's unclipped points and interpolate the clipped midday to recover curtailed days for tuning) remains proposed — Tier-1 perfects the flag, but recovering generation from an off-MPP point still needs the curve fit.

---

## Dependency handling

Storage has no optional dependency (stdlib `sqlite3`). PV tuning needs only **numpy** (a core HA dependency with Raspberry Pi wheels), imported lazily so an unusual env without it degrades gracefully:

```python
try:
    import numpy as np
    TUNING_AVAILABLE = True
except ImportError:
    TUNING_AVAILABLE = False
    _LOGGER.info("numpy not installed — PV tuning disabled")
```

There is deliberately **no scipy** — it has no ARM/Pi wheel and its from-source build fails under HA (issue #85), so the optimiser is a pure numpy grid search. `manifest.json` keeps `"requirements": []` (numpy is already provided; pinning scipy is what broke the base on Pi).

`manifest.json` lists `"dependencies": ["solcast_solar"]`, so HA refuses setup when the base is absent, and `"single_config_entry": true` — one base, one property, one shared database, so a second add is rejected.

---

## Coordination with the base integration

As a companion, this integration reaches into the base at points that are currently internal. Two open asks to the base maintainer (BJReplay) would make that coupling sturdier:

1. **A supported read interface for forecast data.** The companion reads `hass.data["solcast_solar"].data` and the per-site `detailedForecast-<resource_id>` sensor attribute (with sensor-state fallbacks). These are internal and can shift between base releases. A documented, stable read surface for the forecast and per-site detail would let a companion depend on it safely.
2. **`set_dampening` while the base's automatic dampening is on.** The base rejects manual `set_dampening` while its own auto-dampening is enabled, so the companion detects this and skips the push — users have to turn the base feature off to benefit. A trusted-source / manual-override path would let a companion supply factors without the user disabling the base feature.

A separate enhancement (within this integration, no base change needed): the clear-sky cloud signal currently requires a direct OWM key, but could instead be read from an existing HA `weather.*` entity (met.no, openweathermap) to spare users a second account. Tracked as possible future work.

*Settled during development, so no longer open: pv_actual/pv_power naming (kept `pv_actual` with `pv_power` UI labels), the Statistics-sensor prerequisite (replaced by energy counters), EntitySelector compatibility (lazy import + `TextSelector` fallback), the coordinator design (a standalone companion coordinator with its own update loop, reading base data each cycle), and storage packaging (one integration, not a separate DB add-on).*

---

## Change log

The per-release history lives in [CHANGELOG.md](CHANGELOG.md). This document tracks the design and is aligned to **v1.8.0** (config-flow field placement by topology + the multi-site MPPT-diagnostic fix). Earlier milestones: the move to stdlib `sqlite3` storage (v1.5.0), the scipy→numpy grid-search switch and convergence gate (v1.6.4), the azimuth-convention fix (v1.6.5), clear-sky SQL filtering and `[0,1]` dampening clamp (v1.6.6), the curtailment-aware rollout (Phase 1 dampening clip-forecast v1.6.7, Phase 2 DC capture v1.6.8), DC-telemetry capture + diagnostic sensor (v1.6.9), and Open-Meteo plane-of-array transposition tilt tuning + the clearness-index Kt clear-sky gate (v1.7.0).

---

*End of design document*
