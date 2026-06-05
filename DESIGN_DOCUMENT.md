# Solcast Solar Enhanced — Design Document

**Prepared for collaboration with BJReplay/ha-solcast-solar**
**Version 1.7 — June 2026**

---

## Overview

This document describes a proposed enhancement to the
[BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar)
Home Assistant integration. The enhancement adds three capabilities:

1. **Built-in SQLite database storage** of PV power averages, forecasts,
   solar position, weather and battery data — a single zero-config file
   using the Python standard-library `sqlite3` module (no server, no
   credentials, no extra dependency)
2. **Automatic Rooftop PV Tuning** — tilt and azimuth optimisation via
   scipy, based on Solcast SDK notebook 3.4
3. **Adaptive Shading Dampening** — quality-weighted dampening computed
   purely from DB-collected actual-vs-forecast history (it never consumes
   the base integration's own dampening factors), ramping from a neutral
   no-op toward the measured ratio as data accumulates, based on Solcast
   SDK notebook 3.4b

A fourth capability — **Short-range Forecast Correction** — was designed but
**evaluated and dropped**; see [Feature 4](#feature-4--short-range-forecast-correction-dropped)
for the reasoning.

The current working prototype runs as a standalone companion integration
(`solcast_solar_enhanced`) that reads all Solcast data from the base
integration's coordinator — making **zero additional Solcast API calls** —
and pushes improved dampening values back via the base integration's
`set_dampening` service.

The goal is to merge this enhancement into the main repository so all
users benefit from a single, unified integration.

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
├── pv_tuning.py             Tilt/azimuth optimisation (scipy)
├── shading_dampening.py     Quality-weighted dampening calculation
├── solcast_api.py           OWM client only (no Solcast API calls)
├── sensor.py                13 HA sensor entities
├── services.yaml            Service definitions
└── translations/en.json     UI strings
```

### Data flow

```
base solcast_solar coordinator
        │
        │  reads forecast + estimated actuals (no API call)
        ▼
solcast_solar_enhanced coordinator
        │
        ├── read pv_actual     inverter sensor → avg kW (energy counter or power)
        ├── read pv_export     inverter/grid sensor → avg kW
        ├── read battery       battery sensor (energy counter or power)
        │                      └─ falls back to raw battery sensor if not configured
        ├── read per-site      multi-site: per-array kW (DC-ratio apportionment)
        ├── fetch OWM weather  (temp °C, clouds 0–100, description text)
        ├── persist records    to SQLite ('_total' + one row per site)
        ├── run PV tuning      scipy L-BFGS-B (daily, executor thread; per-site)
        ├── compute dampening  quality-weighted DB ratio blended toward neutral 1.0
        └── push dampening     → base integration set_dampening service (per-site)
```

### API quota impact

**Zero additional Solcast API calls.** All forecast and estimated actuals
data is read directly from the base integration's coordinator via
`hass.data["solcast_solar"]`, with fallback to reading sensor state
attributes if the coordinator structure differs between versions.

The only external HTTP call added is to OpenWeatherMap (no quota
restriction for standard use).

---

## Code quality

The codebase is linted and validated against HA 2026.5.4 (Core 2026.5.4,
Supervisor 2026.05.1, OS 17.3, Frontend 20260429.4):

| Check | Status |
|---|---|
| flake8 (PEP8, max line 120) | ✅ Zero issues |
| pyflakes (unused imports, undefined names) | ✅ Zero issues |
| Python syntax (ast.parse all files) | ✅ All pass |
| HA 2026.5.4 deprecation checks | ✅ All pass |
| Selector compatibility (confirmed working set) | ✅ Verified |

### HA 2026.5.4 specific fixes applied

**`DeviceEntryType.SERVICE` enum** — `DeviceInfo(entry_type=...)` uses
`DeviceEntryType.SERVICE` from `homeassistant.components.device_registry`,
not the deprecated raw string `"service"`.

**`ConfigEntryNotReady`** — `async_config_entry_first_refresh()` is
wrapped in `try/except ConfigEntryNotReady` with proper cleanup and
re-raise so HA retries setup on transient failures (e.g. base integration
not yet loaded on startup).

**`UpdateFailed`** — `_async_update_data()` wraps its body in a thin
dispatcher that catches all exceptions and raises `UpdateFailed`, as
required by HA's `DataUpdateCoordinator` contract. This ensures the
integration is correctly marked unavailable in the UI on error rather
than silently returning stale data.

**Selector top-level imports** — only the confirmed working selector set
is imported at module level: `BooleanSelector`, `NumberSelector`,
`NumberSelectorConfig`, `SelectSelector`, `SelectSelectorConfig`,
`TextSelector`, `TextSelectorConfig`. `EntitySelector` uses a lazy
`try/except` import with `TextSelector` fallback to prevent the config
flow from failing if `EntitySelector` is unavailable or its API changes.

**`OptionsFlow` pattern** — `OptionsFlowWithReload` was removed in HA 2024.4+.
The options flow subclasses `config_entries.OptionsFlow` directly. `__init__`
takes no arguments; `async_get_options_flow` returns `SolcastEnhancedOptionsFlow()`
with no `config_entry` argument. Current options are read from
`self.config_entry.data` and `self.config_entry.options` at the start of each step.

### Confirmed working selector configuration

```python
# TextSelectorConfig — type as string, not TextSelectorType enum
TextSelector(TextSelectorConfig(type="password"))

# SelectSelectorConfig — mode as string, not SelectSelectorMode enum
SelectSelector(SelectSelectorConfig(options=[...], mode="dropdown"))

# NumberSelectorConfig — no mode="box" parameter
NumberSelector(NumberSelectorConfig(min=0, max=90, step=0.1))

# BooleanSelector — no parameters needed
BooleanSelector()

# EntitySelector — lazy import with TextSelector fallback
try:
    from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
    return EntitySelector(EntitySelectorConfig(domain="sensor"))
except Exception:
    return TextSelector()
```

---

## PV Sensor Input

### Requirement

`pv_actual`, `pv_export` and `battery_charge` must represent the **average
power over each 30-minute period**, not a raw instantaneous reading, because
Solcast's `pv_estimate` is itself a half-hourly average. The dampening ratio
`total_pv / pv_estimate` is only meaningful when both sides are the same
time-averaged quantity.

The integration reads the inverter's sensors **directly** — no helper sensors
are required. `_read_pv_value` (see [Feature 5](#feature-5--pv-sensor-input-modes-power-vs-energy-counter))
supports two input families, with `auto` detection from `state_class` and
`unit_of_measurement`:

- **Cumulative energy counter (recommended)** — `Wh`/`kWh`/`MWh`,
  `state_class: total_increasing` (e.g. an inverter generation total and a grid
  export total). The period's average power is the energy delta over the actual
  elapsed time (`ΔkWh / hours`). This is the energy-equivalent average that
  matches Solcast, is robust to polling drift, and avoids the reset race that an
  external averaging window introduces.
- **Power sensor** — `W`/`kW`, instantaneous or already-averaged; used directly.

### Legacy: HA Statistics sensor (mean_linear)

Earlier versions required a HA **Statistics** sensor per signal, using the
`mean_linear` characteristic — a time-weighted linear average
(`Σ(valueᵢ × durationᵢ) / total_duration`) over a 30-minute `max_age` window.
This still works (sensor type **Power**, or Auto-detect), but is **no longer
recommended**: a cumulative energy counter gives the same energy-equivalent
average more simply and without the window-reset race. Example, if you still
prefer it:

```yaml
sensor:
  - platform: statistics
    name: "PV Power 30min Average"
    entity_id: sensor.YOUR_INVERTER_AC_POWER_SENSOR
    state_characteristic: mean_linear
    max_age:
      minutes: 30
    sampling_size: 1800
```

(Repeat for export and, optionally, battery charge. Source sensors must report
power in kW; `max_age: 30 min` aligns the average with Solcast period boundaries.)

### pv_actual vs pv_power naming

The DB column is named `pv_actual` for backward compatibility with
existing schemas and Solcast SDK terminology. The value stored is a
30-minute linear average power in kW — more precisely described as
`pv_power`. UI labels and sensor names use `pv_power` terminology.

See **Question 7** for BJReplay regarding preferred naming resolution.

### Safety defaults for all three sensor reads

| Condition | Behaviour |
|---|---|
| Sensor entity ID not configured | 0.0 stored, debug log |
| Sensor state unavailable / unknown | 0.0 stored, debug log |
| Sensor not yet computed (HA startup) | 0.0 stored, debug log |
| Sensor value non-numeric | 0.0 stored, debug log |
| Negative value (discharge / import) | Clamped to 0.0 |

Battery sensor priority:
1. Statistics sensor (`battery_stat_sensor`) — 30-min average, preferred
2. Raw battery sensor (net or separate) — fallback if Statistics not configured

---

## Feature 1 — Built-in SQLite Database Storage

### Purpose

Persists historical PV data alongside solar position, weather and battery
state for use by the shading dampening and PV tuning calculations. Storage is
**zero-config** and enabled by default: a single file
(`config/solcast_solar_enhanced.db`) using the Python standard-library
`sqlite3` module — no server, no credentials and no extra dependency.

> **Storage history.** v1.0.0 shipped a MySQL backend (`aiomysql`). v1.5.0
> removed MySQL entirely; the integration is now SQLite-only. To carry forward
> an existing MySQL history, export it to CSV before upgrading — otherwise the
> built-in store starts fresh and rebuilds as data accumulates.

### Implementation

`SqliteStore` (`sqlite_store.py`) wraps stdlib `sqlite3`: every call runs via
`async_add_executor_job` and is serialised by a lock, the connection uses WAL
mode (`synchronous=NORMAL`), and the complete schema is created on first run —
so the `site` and `battery_charge` columns are always present (no *schema*
migrations). Writes use `INSERT OR IGNORE` on `(period_end_epoch, site)`.
The store logs its file path and row count at startup.

**Data repairs.** One-time, in-place data fixes are gated by SQLite's built-in
`PRAGMA user_version` (`SCHEMA_VERSION`), so they run silently once and are a
no-op on later starts and on fresh databases. v1 recomputes the solar `azimuth`
column for rows written before the hour-angle wrap fix (which mirrored azimuth
east↔west for sites whose local morning/afternoon fell on a different UTC day
from solar noon); the value is reconstructable in place because solar azimuth
depends only on each row's stored `period_end_epoch` and the site lat/lon. Only
rows whose value actually moved are rewritten, to spare SD-card wear.

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
  UNIQUE(period_end_epoch, site)
);
```

To browse the file, point the [sqlite-web add-on](https://github.com/hassio-addons/addon-sqlite-web)
at it (WAL mode, so leave the `-wal`/`-shm` sidecar files in place).

### Total PV energy balance

Throughout the codebase total PV output is:

```
total_pv = pv_actual
```

`pv_actual` is the inverter's total AC output — it already includes
self-consumption, grid export, and battery charging. Adding `pv_export`
or `battery_charge` would double-count flows that are already measured
by the generation meter. Both sensors are still stored in the DB for
diagnostics and reference, but are not summed into `total_pv`.

`total_pv` is used in:

- Dampening ratio: `total_pv / pv_estimate`
- Clipping detection: `total_pv >= capacity × clipping_threshold`
- PV tuning RMSE: `total_pv` vs geometrically-scaled estimate

### Schema initialisation

On first run the store creates the complete `solcast_data` table (and its
`UNIQUE(period_end_epoch, site)` constraint) in one `CREATE TABLE IF NOT
EXISTS`, with WAL mode enabled. Because the full schema — including the `site`
and `battery_charge` columns — is created up front, there are **no schema
migrations** and no `ALTER TABLE`/`information_schema` probing; the
`has_site_col` / `has_battery_col` flags are always true. One-time *data*
repairs (not schema changes) are handled separately via `async_migrate`, gated
by `PRAGMA user_version` — see [Implementation](#implementation).

### Battery charge safety layers

| Layer | Location | Default |
|---|---|---|
| Statistics sensor not configured | Config check | 0.0 |
| Sensor state unavailable | `_safe_read_sensor()` | 0.0 |
| DB value NULL | SQL: `COALESCE(battery_charge, 0.0)` | 0.0 |
| Calculation | `float(rec.get("battery_charge", 0) or 0)` | 0.0 |

### Sensor mapping guidance

`pv_actual` must be configured to read from the **inverter generation meter**
(total AC output). It already includes self-consumption, grid export, and
battery charging — so `pv_export` and `battery_charge` must **not** be
added to it. Configuring `pv_actual` to a self-consumption-only meter will
produce systematically low dampening factors and poor tuning results.

---

## Feature 2 — Rooftop PV Tuning

### Purpose

Optimises panel tilt and azimuth to minimise RMSE between measured
`total_pv` and the geometrically-corrected Solcast estimate. Based on
[Solcast SDK notebook 3.4](https://solcast.github.io/solcast-api-python-sdk/notebooks/3.4%20Rooftop%20PV%20Tuning/).

### Algorithm

1. Fetch up to 2000 recent records from DB (`pv_actual > 0`)
2. Filter: clear-sky only (`clouds < cloud_threshold`)
3. Exclude: clipped records (`total_pv >= capacity × clipping_threshold`
   AND `pv_estimate >= capacity × clipping_threshold`)
4. Exclude: export-limited records (`pv_export >= export_limit_kw × clipping_threshold`,
   when `export_limit_kw > 0`) — see **Export limit filtering** below
5. For each candidate (tilt, azimuth) compute cosine of incidence angle
   relative to nominal geometry (20° tilt, 0° azimuth)
6. Scale Solcast estimates by the incidence angle ratio
7. Minimise RMSE via `scipy.optimize.minimize` (L-BFGS-B, bounds
   0–90° tilt, -180–180° azimuth, max 300 iterations)
8. Run in `hass.async_add_executor_job` to avoid blocking the event loop
9. Requires ≥10 qualifying records; runs daily

### Solar position calculation

Implemented locally in `pv_tuning.py` — no additional library required.
Uses solar declination, equation of time and hour angle to compute
(azimuth°, zenith°) accurate to ±1°. The same function populates the
`azimuth` and `zenith` DB columns at record write time.

### Clipping / curtailment detection

```
is_clipped = (
    total_pv    >= capacity × clipping_threshold   AND
    pv_estimate >= capacity × clipping_threshold
)
```

Excludes inverter AC clipping from the tuning dataset. The
`battery_full + export_capped` double-curtailment case (low `total_pv`
despite high irradiance) is a known limitation.

### Export limit filtering

Sites with a grid export limit (e.g. 5 kW export cap on a 10 kW system)
produce artificially low `total_pv` when export is being curtailed —
`pv_actual` stays flat while `pv_export` is pegged at the limit. These
records would otherwise pull the optimiser toward a shallower tilt or
more northerly azimuth than reality.

```
is_export_limited = (
    export_limit_kw > 0   AND
    pv_export >= export_limit_kw × clipping_threshold
)
```

The same `clipping_threshold` fraction is reused so that only records
clearly at or near the ceiling are excluded — marginal export values are
retained. `export_limit_kw = 0` (the default) disables the filter
entirely.

### Results

Exposed on the `Tuned Panel Tilt` sensor (`_attr_name: "Tuned Panel Tilt"`)
with attributes: `azimuth`, `rmse_kw`, `n_records`. Also surfaced in the
Settings → Configure page via `description_placeholders`.

---

## Feature 3 — Adaptive Shading Dampening

### Purpose

Automatically computes per-hour shading dampening factors from historical
clear-sky actual vs estimate ratios, replacing manual hourly dampening
configuration. Based on
[Solcast SDK notebook 3.4b](https://solcast.github.io/solcast-api-python-sdk/notebooks/3.4b%20Rooftop%20Shading%20Corrections/).

Because `pv_actual` is a 30-minute linear average rather than an
instantaneous reading, transient cloud effects within the period are
already smoothed — making the ratio more stable and reliable as input
to the dampening calculation.

### Why cloud filtering is essential

The dampening factor is `total_pv / pv_estimate`. On a clear day this
reflects shading geometry. On a cloudy day it reflects cloud attenuation
— a different effect already modelled by Solcast. Including cloudy records
corrupts the dampening factor, causing systematic underestimation on
future clear days. The OWM `clouds` value (0–100) stored per record
drives the filtering.

### Cloud quality weighting

Three-band weighting, bands scaling relative to the configured threshold:

| Cloud cover | Weight |
|---|---|
| Below threshold | 1.0 — clear sky, full quality |
| Threshold to 1.5× threshold | 0.6 — marginal |
| 1.5× threshold to max\_include | 0.3 — poor but usable |
| Above max\_include | 0.0 — excluded |

Default threshold: 20%. Configurable 10–50%. Default max\_include: 60%.

**Convergence guidance by climate:**

| Climate | Threshold | Expected time to full confidence |
|---|---|---|
| Clear (Perth, inland QLD) | 20% | 4–6 weeks |
| Mixed (Melbourne, Sydney) | 20–25% | 8–12 weeks |
| Overcast (Hobart, coastal) | 30–35% | 6–10 weeks at relaxed threshold |

### Geometric proximity weighting

Each record is weighted by how similar its solar geometry is to the
current target slot, using Gaussian proximity on both zenith and azimuth:

```python
zenith_weight  = exp(-0.5 × ((Δzenith  / 10°)²))
azimuth_weight = exp(-0.5 × ((Δazimuth / 20°)²))
combined_weight = cloud_weight × zenith_weight × azimuth_weight
```

This ensures records from days where the sun was at a similar position
count more than calendar-proximate records with different geometry — which
matters because shading from nearby objects is highly angle-dependent.

### Seasonal window

The `±14-day calendar day-of-year` window is applied in the DB query,
across all years in the database. This means:

- Early in the data collection period the window provides natural
  extrapolation from seasonally similar dates in the current year
- As the DB grows, same-year matches dominate

### Clipping exclusion in dampening

The same clipping detection used in PV tuning is applied per record
before it contributes to the dampening ratio. Clipped records are counted
in `hour_XX_clipped_excluded` sensor attributes for diagnostics.

### Confidence model (α blending)

The system blends a **neutral `1.0` anchor** with the DB-derived factor — the
base integration's own dampening factors are **never** read into the
calculation (changed in v1.2.0; previously the anchor was the base factor):

```
final_factor(h) = (1 - α) × 1.0  +  α × db_factor(h)
```

So with little data the factor sits near a no-op `1.0` and ramps toward the
DB-measured `total_pv / pv_estimate` ratio as confidence builds.

**α** is a per-half-hour-slot quality-weighted sigmoid:

```
α = x² / (x² + midpoint²)

x        = quality_weighted_count  (Σ of combined_weight per record)
midpoint = BASE_MIDPOINT / average_quality
         = 30 / (Σ weights / n_records)
```

Scaling the midpoint by average quality means a loose cloud threshold
(lower average quality per record) requires proportionally more records
before trusting the DB factor.

**Convergence table:**

| Quality-weighted records | α (20% threshold, avg quality 0.9) | α (35% threshold, avg quality 0.5) |
|---|---|---|
| 0 | 0.00 | 0.00 |
| 10 | 0.10 | 0.04 |
| 30 | 0.50 | 0.20 |
| 60 | 0.80 | 0.50 |
| 100 | 0.92 | 0.74 |

**Early stability clamp:** when α < 0.5 the blended factor is constrained
to within ±15% of `1.0` (i.e. 0.85–1.15). This prevents a single anomalous day
(sensor fault, heavy partial cloud) from distorting the dampening while
the DB is still accumulating data.

### Fallback chain when DB is unavailable or insufficient

For each half-hour slot where DB data is insufficient, the system falls
back through:

1. **DB data with seasonal extrapolation** — `±14-day` window queries
   already provide extrapolation for slots with few records (source
   `db_history` / `db_blended`)
2. **Neutral no-op** — a slot with no usable DB data stays at `1.0`
   (source `no_data`); α = 0.0. The base integration's own factors are
   never read back in.

This ensures dampening always works from day one — a fresh install simply
pushes neutral `1.0` factors until data accumulates.

### Per-slot vs hourly resolution

The calculation operates at **48 half-hour slots per day** internally.
Each slot gets its own α and quality metrics. Adjacent pairs of 30-min
slots are averaged into 24 hourly values for the `set_dampening`
service call (which accepts hourly or half-hourly resolution). The full
48-slot table is preserved for internal diagnostics.

### Dampening sensor attributes

The `Dampening Hours with DB Data` sensor (`_attr_name`) exposes
per-hour diagnostics:

```yaml
hour_14_factor:           0.847      # final blended value pushed to base integration
hour_14_alpha:            0.72       # DB confidence (0 = neutral 1.0, 1 = pure DB)
hour_14_source:           db_blended # db_history | db_blended | no_data | night
hour_14_quality_records:  31.4       # quality-weighted record count
hour_14_avg_quality:      0.81       # mean combined_weight of contributing records
hour_14_clipped_excluded: 2          # records excluded due to clipping (shown if > 0)
overall_source:           db_blended # summary across all hours
```

### Recalculation schedule

Dampening is recomputed every 6 hours. The schedule can be triggered
manually via the `solcast_solar_enhanced.run_dampening_update` service.

---

## Feature 4 — Short-range Forecast Correction (Dropped)

> **Status: evaluated and dropped** (v1.3.0). The design below is retained for
> the record. It was dropped because: the near-term deviation signal is
> cloud-driven and decays within an hour or two, so the nudge approaches a no-op
> by +3 — exactly where forecast error is largest; it would second-guess
> Solcast's imagery-based near-term product with a cruder single-inverter ratio
> plus coarse OWM cloud cover; a now-relative, decaying, per-horizon correction
> can't go through `set_dampening` (indexed by local time-of-day), so it would
> have to fork the forecast into separate "corrected" sensors; and the durable,
> predictable part is already captured by the DB-driven dampening, whose
> ±14-day seasonal window also covers individual missing slots.

### Purpose

Adjust the next 1–6 hours of forecast based on live Statistics sensor
output and current OWM cloud cover, correcting for satellite image
processing lag in Solcast's near-term predictions.

### Design (not implemented)

**Activation conditions:**

- OWM is enabled (cloud data required to distinguish cloud attenuation
  from shading — must not double-correct)
- Statistics sensors configured (pv_actual, pv_export)
- ≥2 consecutive recent periods show the same direction of deviation
  from estimate (prevents a single outlier triggering correction)
- `clouds > cloud_threshold` — clear-sky deviations are geometric
  shading, already handled by dampening; do not double-correct

**Correction formula:**

```
recent_ratio = mean(total_pv / pv_estimate) over last 2–3 periods
correction(n) = 1.0 + (recent_ratio - 1.0) × exp(-n / τ)
```

Where:
- `n` = periods ahead (integer)
- `τ` = time constant (default 3 periods = 90 min, configurable)

**Decay:**

| Period ahead | Correction retained |
|---|---|
| +1 (30 min) | 72% |
| +3 (90 min) | 37% |
| +6 (3 hours) | 14% |
| +12 (6 hours) | 2% — effectively zero |

**Stacking with dampening (orthogonal effects):**

```
final_forecast(period) = solcast_estimate(period)
                         × dampening_factor(hour)         ← structural shading
                         × short_range_correction(period)  ← transient cloud
```

Because `pv_actual` is a 30-minute linear average, `recent_ratio` is
already a stable period-average signal rather than a noisy instantaneous
reading — reducing false positive corrections.

**Configuration addition:** `correction_tau` (default 3 periods,
range 1–12). Would have been added to the tuning step of the setup wizard.

---

## Feature 5 — PV sensor input modes (power vs energy counter)

### Purpose

The original design required HA Statistics sensors producing a 30-minute
`mean_linear` average in kW. That introduces a **race**: the external averaging
window can be reset/cleared on its own schedule, unsynchronised with the
integration's 30-minute poll, so a read can catch a half-reset or stale value.
It also assumes a perfectly-spaced 30-minute cadence.

### Design

`_read_pv_value(entity_id, mode, key, now_epoch)` resolves each PV sensor in one
of two families (mode `auto` detects from `state_class` + `unit_of_measurement`):

- **Power** (`power_kw`, `power_w`) — the reading is converted to kW and used
  directly (the classic Statistics-sensor path).
- **Energy counter** (`energy_kwh`, `energy_wh`, `energy_mwh`;
  `state_class: total_increasing`) — the interval's average power is derived from
  the energy delta over the **actual** elapsed time:

  ```
  avg_kW = (counter_now − counter_prev) / ((epoch_now − epoch_prev) / 3600)
  ```

  Dividing by the real elapsed time (not a hard-coded 1800 s) makes the value
  robust to polling drift. The reading returns `0.0` (excluded by the
  `pv_actual > 0` filters) when: it is the first read after setup/restart
  (no baseline yet), the delta is negative (counter reset / rollover / inverter
  reboot), or the elapsed time is outside `[0.5×, 2×]` the expected interval
  (restart gap / missed cycle). Baselines `{value, epoch}` are persisted across
  restarts via HA's `Store` (`{DOMAIN}_{entry_id}_energy_baseline`).

This keeps `pv_actual` in the same unit as Solcast's `pv_estimate` (average kW
over the period), so tuning/dampening maths is unchanged.

---

## Feature 6 — Multi-site support

### Purpose

Solcast lets a user define multiple rooftop **sites** — in practice, multiple
arrays on one property, each at a different orientation. Tuning a single
tilt/azimuth across differently-oriented arrays is meaningless, and shading
dampening differs per array. This feature stores, tunes and dampens each site
independently where the hardware allows.

### Governing constraint

**Tuning/dampening granularity is capped by measurement granularity** — a site
can only be tuned/dampened individually if its generation can be measured
separately:

| Measurement | Per-array tuning |
|---|---|
| Dedicated AC sensor per array (e.g. Enphase) | ✅ direct |
| Shared inverter AC + per-MPPT DC (e.g. Fronius) | ✅ via DC-ratio apportionment |
| Shared AC, no per-MPPT DC | ❌ not observable |

### DC-ratio apportionment

For a string inverter exposing one AC total plus per-MPPT DC, the measured AC is
split across arrays by each string's share of total DC:

```
ac_arrayᵢ = ac_total × (dcᵢ / Σ dc)
```

Since `ac_total ≈ η × Σ dc` (η = inverter efficiency, ~constant), this yields
each array's production **in the AC domain** (matching Solcast's estimate), sums
back exactly to the metered AC total, and handles clipping correctly (the capped
AC is apportioned proportionally; clipped windows are excluded by the clipping
filter anyway). Guarded against `Σ dc ≈ 0`.

### Site discovery

`discover_sites(hass)` (shared by the coordinator and config flow) enumerates the
base integration's per-site RooftopSensors, reading `resource_id`, `name`,
`capacity`, `capacity_dc`, `tilt`, `azimuth` and `compass_degrees`. Orientation
seeds per-site tuning; `resource_id` keys storage and targets `set_dampening`.

### Configuration model

`CONF_SITE_GROUPS` is a list of measurement groups:

```python
{
  "ac_sensor": "sensor.inverter_ac", "ac_mode": "auto",
  "site": "<resource_id>",                       # single-site group
  "strings": [                                    # OR: DC-apportioned group
    {"site": "<rid>", "dc_sensor": "sensor.mppt1"},
    {"site": "<rid>", "dc_sensor": "sensor.mppt2"},
  ],
}
```

The config-flow `sites` step collects, per discovered site, a generation sensor,
an optional DC/MPPT sensor and a mode; `_derive_groups()` then groups sites that
share an AC sensor (shared → DC-apportioned; alone → single-site; shared-without-DC
→ omitted). `_groups_to_assignments()` reverses this for options-flow prefill.

### Storage and the aggregate guard

The `site` column (default `'_total'`) and composite unique key
`(period_end_epoch, site)` let each site own its rows. Each cycle writes the
property-wide `_total` row (unchanged from single-site) **plus** one row per
configured site. Property-wide `pv_export` is replicated onto site rows (for each
site's export-clip exclusion) but `battery_charge` is kept on `_total` only.

To avoid double-counting, aggregate tuning/dampening pass `site='_total'`; per-site
runs pass the `resource_id`. In single-site installs everything is `_total`, so
behaviour is identical and per-site logic is inert.

### Per-site tuning and dampening

- **Tuning** (`_run_site_tuning`): each configured site is tuned against its own
  rows, seeded from its Solcast orientation. The azimuth seed is converted to the
  tuner's frame (0=N/90=E) from `compass_degrees`. Results surface as a `per_site`
  attribute on the Tuned Panel Tilt sensor.
- **Dampening** (`_compute_dampening_slots` per site): pushed via
  `solcast_solar.set_dampening` with the site's `resource_id`, which overrides the
  base's global dampening for that site (the conflicting global push is skipped).

> **Service note:** the base integration's service is `set_dampening`
> (`damp_factor` CSV + optional `site`), not `set_dampening_factor`. The earlier
> code called the latter; this was corrected as part of multi-site work.

### Standalone tuning tool

`tools/standalone_tuning.py` runs the same optimisation (importing
`pv_tuning.run_tuning`) outside HA against the built-in SQLite store or a CSV,
with `--site` / `--all-sites`, for offline validation.

---

## Sensors (14 total)

| Sensor class | `_attr_name` | Unit | Description |
|---|---|---|---|
| `ForecastNowSensor` | Forecast Now | kW | Current 30-min PV forecast |
| `ForecastTodaySensor` | Forecast Today | kWh | Total forecast for today |
| `TuningTiltSensor` | Tuned Panel Tilt | ° | Optimised tilt |
| `TuningAzimuthSensor` | Tuned Panel Azimuth | ° | Optimised azimuth |
| `TuningRmseSensor` | Tuning RMSE | kW | Goodness of fit |
| `TuningExportExcludedSensor` | Tuning Export Limited Excluded | — | Records dropped by export limit filter in last tuning run |
| `DbRecordsSensor` | Database Records | — | Total DB record count |
| `DampeningSensor` | Dampening Hours with DB Data | — | Hours with DB-derived factors |
| `WeatherTempSensor` | Weather Temperature | °C | OWM current temperature |
| `WeatherCloudsSensor` | Cloud Cover | % | OWM cloud cover |
| `BatteryChargeSensor` | Battery Charge 30min Average | kW | Configured battery sensor value |
| `PvActualSensor` | PV Power 30min Average | kW | Period-average generation (kW) |
| `PvExportSensor` | PV Export 30min Average | kW | Period-average export (kW) |
| `BaseIntegrationSensor` | Base Integration Status | — | connected / not_detected |

All sensors implement `_attr_has_entity_name = True`, `_attr_should_poll = False`,
and unique IDs derived from `f"{DOMAIN}_{entry_id}_{key}"`.

`DeviceInfo` uses `DeviceEntryType.SERVICE` (enum, not string).

---

## Services

| Service | Description |
|---|---|
| `run_pv_tuning` | Force PV tuning immediately (requires DB) |
| `run_dampening_update` | Force dampening recalculation (DB or fallback) |
| `fetch_weather` | Force OWM weather fetch |

---

## Configuration

### Prerequisites

No helper sensors are required — map the inverter's sensors directly in the
wizard. Each may be a **cumulative energy counter** (`Wh`/`kWh`/`MWh`,
`total_increasing`; recommended) or a **power sensor** (`W`/`kW`); `Auto-detect`
chooses. See [PV Sensor Input](#pv-sensor-input). (A legacy HA Statistics
`mean_linear` sensor still works if you prefer it.)

### Setup wizard (5 steps, + per-site step when multi-site)

**Step 1 — Site & System:**

| Field | Type | Description |
|---|---|---|
| Latitude | Number | Site latitude (-90 to 90) |
| Longitude | Number | Site longitude (-180 to 180) |
| Capacity (kW) | Number | System DC capacity |
| Tilt | Number | Panel tilt 0° (flat) to 90° (vertical) |
| Azimuth | Number | 0°=North, 90°=East, -90°=West |
| PV Power / Generation sensor | Entity selector* | Energy counter or power sensor for generation |
| PV sensor type | Select | Auto-detect / power / energy counter |
| PV Export sensor | Entity selector* | Energy counter or power sensor for export |
| PV Export sensor type | Select | Auto-detect / power / energy counter |
| Battery sensor | Entity selector* | Energy counter or power sensor for battery charge |

A final **Per-site sensor mapping** step appears automatically when more than
one Solcast site is detected (see [Feature 6](#feature-6--multi-site-support)).

*Falls back to text input if EntitySelector is unavailable in the
running HA version.

**Step 2 — Storage**

| Field | Default | Description |
|---|---|---|
| Enable history storage | On | Toggle the built-in SQLite store on/off |

The store lives at `config/solcast_solar_enhanced.db` and needs no further
configuration — no host, port, credentials or schema name.

**Step 3 — OpenWeatherMap (optional)**

| Field | Description |
|---|---|
| Enable OWM | Toggle weather data on/off |
| OWM API Key | Key from openweathermap.org |

OWM endpoint used:
```
GET https://api.openweathermap.org/data/2.5/weather
    ?lat={latitude}&lon={longitude}&appid={key}&units=metric
```
Parses: `main.temp` (°C), `clouds.all` (0–100 int),
`weather[0].description` (text).

**Step 4 — Battery Storage (optional)**

Raw battery sensor fallback for systems without a dedicated battery sensor mapped in Step 1:

| Field | Description |
|---|---|
| Enable raw battery fallback | Toggle |
| Mode | net (signed) or separate (charge only) |
| Net battery entity | Signed power entity ID |
| Charge battery entity | Charge-only power entity ID |

**Step 5 — PV Tuning & Dampening**

| Field | Default | Range | Description |
|---|---|---|---|
| Auto PV tuning | True | — | Run daily |
| Auto dampening | True | — | Run every 6 hours |
| Cloud threshold | 20% | 10–50% | Clear-sky cutoff |
| Max cloud include | 60% | 20–100% | Hard exclusion ceiling |
| Clipping threshold | 0.95 | 0.5–1.0 | Fraction of capacity |
| Grid export limit (kW) | 0.0 | 0–100 | Exclude export-curtailed records from tuning; 0 = disabled |

### Full options reference

| Constant | Key | Default |
|---|---|---|
| `CONF_LATITUDE` | latitude | -37.9 |
| `CONF_LONGITUDE` | longitude | 145.0 |
| `CONF_CAPACITY_KW` | capacity\_kw | 5.0 |
| `CONF_TILT` | tilt | 20.0 |
| `CONF_AZIMUTH` | azimuth | 0.0 |
| `CONF_PV_ACTUAL_SENSOR` | pv\_actual\_sensor | "" |
| `CONF_PV_EXPORT_SENSOR` | pv\_export\_sensor | "" |
| `CONF_BATTERY_STAT_SENSOR` | battery\_stat\_sensor | "" |
| `CONF_DB_ENABLED` | db\_enabled | True |
| `CONF_OWM_ENABLED` | owm\_enabled | False |
| `CONF_OWM_API_KEY` | owm\_api\_key | "" |
| `CONF_BATTERY_ENABLED` | battery\_enabled | False |
| `CONF_BATTERY_MODE` | battery\_mode | net |
| `CONF_BATTERY_NET_SENSOR` | battery\_net\_sensor | "" |
| `CONF_BATTERY_CHARGE_SENSOR` | battery\_charge\_sensor | "" |
| `CONF_AUTO_TUNING` | auto\_tuning | True |
| `CONF_AUTO_DAMPENING` | auto\_dampening | True |
| `CONF_CLOUD_THRESHOLD` | cloud\_threshold | 20 |
| `CONF_CLOUD_MAX_INCLUDE` | cloud\_max\_include | 60 |
| `CONF_CLIPPING_THRESHOLD` | clipping\_threshold | 0.95 |
| `CONF_EXPORT_LIMIT_KW` | export\_limit\_kw | 0.0 |

---

## Proposed Merge Strategy

### Phase 1 — DB storage + OWM weather (low risk)

- Add PV sensor entity selector fields to config flow step 1
- Add a built-in SQLite store toggle to config flow options
- Add OWM API key to config flow options
- Extend coordinator to read PV sensors and persist to the store
- Add battery charge Statistics sensor as primary, raw sensor as fallback
- No changes to existing forecast, dampening or sensor behaviour

**New dependency:** none — storage uses the stdlib `sqlite3` module

**Prerequisite documentation:** PV sensor (energy counter) setup guide in README

### Phase 2 — Adaptive shading dampening (medium risk)

- Add `shading_dampening.py`
- Extend coordinator: dampening recalculation every 6 hours
- Blend DB-derived factor with existing dampening via confidence model
- Add cloud threshold, max include, clipping threshold to options
- Existing dampening system unchanged when DB disabled

**New dependencies:** none beyond Phase 1

### Phase 3 — PV tuning (optional)

- Add `pv_tuning.py`
- Add tilt/azimuth optimisation (daily, executor thread)
- Expose tuning results on new sensors and in Settings page
- Fully optional — guard behind `auto_tuning` toggle
- Lazy scipy import — feature disabled with informational log if not installed

**New dependencies:** `numpy>=1.21.0`, `scipy>=1.7.0`

### Phase 4 — Short-range forecast correction (dropped)

Evaluated and **not pursued** — see [Feature 4](#feature-4--short-range-forecast-correction-dropped)
for the reasoning. No implementation work is planned.

---

## Roadmap

Planned work, not yet implemented.

### Database retention / dampening-scan efficiency (low-power devices)

**Problem.** The store accumulates one row per half-hour per site
(≈17.5k rows/site/year) and never prunes, so the table grows without bound.
The dampening recalculation runs a seasonal day-of-year query
(`async_get_records_for_dampening`) whose filter is
`ABS(CAST(strftime('%j', period_end_epoch, 'unixepoch') AS INTEGER) - ?) <= ?`.
Because the day-of-year is a *computed* expression, no index can serve it — the
query is a full table scan. On a multi-year, multi-site database running on a
Raspberry Pi (SD-card I/O), this scan gets progressively slower. (The 48×
redundant re-scan per run was already removed — the fetch is hoisted to once per
`_compute_dampening_slots` call — so what remains is the single O(N) scan.)

**Options under consideration.**

1. **Optional retention period.** A config setting (e.g. *Keep history for N
   years*) that prunes rows older than the window. Default must be *keep
   everything* so existing behaviour never changes silently. Pruning would run
   on the same low-frequency timer as tuning/dampening.
2. **Stored `day_of_year` column + index.** Persist the UTC day-of-year at insert
   time and index it, turning the seasonal scan into an indexed range lookup.
   Requires a schema addition and a one-time backfill migration (the
   `PRAGMA user_version` mechanism added for the azimuth repair already provides
   the gating for this).
3. **Both** — retention to bound size, the indexed column to bound per-query cost.

**Status.** Deferred. Current scan cost is acceptable for typical single-site,
few-year databases; this becomes worthwhile for long-lived multi-site Pi
installs. Tracked here so the reasoning isn't lost.

---

## Dependency handling

```python
# Storage has no optional dependency — it uses the stdlib sqlite3 module,
# so the built-in store always works.

# PV tuning is the only optional extra — lazy import, degrades gracefully:
try:
    from scipy.optimize import minimize
    import numpy as np
    TUNING_AVAILABLE = True
except ImportError:
    TUNING_AVAILABLE = False
    _LOGGER.info("scipy/numpy not installed — PV tuning disabled")
```

Storage adds no dependency at all; users who do not need PV tuning are
unaffected by the optional scipy/numpy extras.

### Base integration (hard dependency)

`manifest.json` lists `"dependencies": ["solcast_solar"]`, so Home Assistant
refuses to set up this integration when the base integration is absent — it
reads `hass.data["solcast_solar"]` and cannot function without it. The manifest
also sets `"single_config_entry": true`: only one instance can be added, since
there is one base integration, one property and one shared database
(`config/solcast_solar_enhanced.db`); a second add is rejected by HA with
`single_instance_allowed`.

---

## Questions for BJReplay

**1. Dependency preference**
Hard requirements in `manifest.json`, or lazy imports with graceful
feature degradation? Lazy imports keep the base install lightweight but
make features silently unavailable if packages are missing.

**2. Config flow placement**
Extend the existing options flow with new sections (simpler for users),
or a separate "Configure Enhanced Features" sub-flow (cleaner separation)?

**3. Coordinator pattern**
Extend the existing `SolcastUpdateCoordinator` class directly, or a
companion coordinator that subscribes to it via `async_add_listener`?
The companion approach keeps the existing code untouched.

**4. Sensor naming convention**
Follow the existing `solcast_pv_forecast_` prefix for new sensors, or
introduce `solcast_pv_enhanced_` to distinguish enhanced sensors? The
former is more consistent; the latter makes it clear which sensors are
from the enhancement.

**5. Database as separate integration**
Would a separate optional integration (`solcast_solar_db`) that depends
on `solcast_solar` be preferable to embedding the DB feature in the main
integration? This would keep the main integration lean and make the DB
feature independently versioned.

**6. OWM vs existing HA weather platform**
Some users already have `weather.*` entities providing cloud cover via
state attributes (e.g. from `met.no`, `openweathermap`). Would it be
preferable to read cloud cover from an existing HA `weather` entity
rather than making a direct OWM API call? This would avoid requiring a
separate OWM API key for users who already have a weather integration.

**7. pv\_actual vs pv\_power column naming**
The DB column is currently `pv_actual` (Solcast SDK terminology) but the
stored value is a 30-minute linear average from an HA Statistics sensor —
more accurately `pv_power`. Options:
  - Keep `pv_actual` with documentation
  - Rename to `pv_power` with a migration
  - Store both columns

**8. Statistics sensor prerequisite**
Should the setup wizard include a checklist step confirming the user has
created the three Statistics sensors before proceeding, or document this
as a README prerequisite only?

**9. Entity selector compatibility**
`EntitySelector` is currently loaded via a lazy `try/except` with
`TextSelector` fallback due to uncertain compatibility across HA versions.
What is the minimum HA version that the merge PR should target, and is
`EntitySelector` available in that version?

**10. Testing framework**
What test framework is in place (`tests/` directory structure, fixtures,
mocking patterns) and what coverage expectations exist for new features?

---

## Change log

| Version | Date | Changes |
|---|---|---|
| 1.0 | May 2026 | Initial design document |
| 1.1 | May 2026 | Added pv\_actual and pv\_export sensor configuration; corrected DB storage to use real sensor readings; added PvActualSensor and PvExportSensor |
| 1.2 | May 2026 | Replaced instantaneous sensor reads with HA Statistics integration (mean\_linear, 30-min, 1800 samples); added entity selector for all three sensors; added pv\_actual vs pv\_power naming question |
| 1.3 | May 2026 | Full document completion: added code quality section with HA 2026.5.4 lint results and all fixes; documented confirmed working selector set; completed Feature 3 dampening section with full convergence tables, seasonal window, clipping exclusion details; completed Feature 4 short-range correction design; completed sensors table with all class names and units; completed configuration reference table; added Questions 8–10 for BJReplay |
| 1.4 | Jun 2026 | Added export limit filtering to PV tuning (CONF\_EXPORT\_LIMIT\_KW, default 0 = disabled); updated DB schema init to check information\_schema before CREATE TABLE; corrected OptionsFlowWithReload reference to OptionsFlow |
| 1.5 | Jun 2026 | Added TuningExportExcludedSensor — exposes count of records dropped by export limit filter from last tuning run; sensor count updated to 14 |
| 1.6 | Jun 2026 | Fixed total_pv calculation in pv_tuning and shading_dampening — pv_actual is inverter AC output and already includes export and battery; removed double-counting |
| 1.7 | Jun 2026 | Aligned with the v1.5.0 release: Feature 1 rewritten as built-in stdlib `sqlite3` storage (MySQL/`aiomysql` removed, no migrations, `site` column, `INSERT OR IGNORE`); dampening confidence model re-anchored on a neutral `1.0` (base factors never read; source labels `db_blended`/`no_data`); Feature 4 short-range correction marked dropped; `set_dampening_factor` → `set_dampening`; config Step 2 and options reference updated for the single storage toggle |

---

*End of design document*
