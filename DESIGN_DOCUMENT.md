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
2. **Automatic Rooftop PV Tuning** — tilt and azimuth optimisation via a
   numpy grid search (no scipy), based on Solcast SDK notebook 3.4
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
├── pv_tuning.py             Tilt/azimuth optimisation (numpy grid search)
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
        ├── run PV tuning      numpy grid search (daily, executor thread; per-site)
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
7. Minimise RMSE via a coarse-to-fine numpy grid search (`_minimize_grid`,
   bounds 0–90° tilt / -180–180° azimuth; full 5° sweep, then ±5° at 1°,
   then ±1° at 0.25° around the running best). This replaces the former
   `scipy.optimize.minimize` (L-BFGS-B) — grid search is the method Solcast
   notebook 3.4 itself uses, and it drops scipy, which has no Raspberry Pi
   wheel and fails to build from source under Home Assistant (issue #85)
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

### The "tuned estimate" prerequisite (relationship to Feature 2 / notebook 3.4)

Notebook 3.4b is explicit that its input must be a **tuned** Solcast estimate
("How to get a Solcast tuned estimate can be found in 3.4"). This is a
prerequisite, not a tip. 3.4 first corrects the site's **tilt / azimuth /
capacity**; 3.4b then computes the *residual* `measured / estimate` ratio per
(zenith, azimuth) bin. Running shading on an **un-tuned** estimate makes each
bin factor silently absorb orientation/capacity error *as well as* shading,
conflating two unrelated effects. "Tuned first" is what makes the residual mean
"shading."

The 3.4b sample dataset is constructed to enforce exactly this separation:
`rooftop_meas` is `rooftop_solcast` multiplied by a fixed shading mask.
**95% of rows are bit-identical** (mask = exactly 1.0 — i.e. zero orientation
error by construction); the ~5% that differ occur **only at low sun**
(zenith 53–86°) in two lobes — morning-east (az ≈ −96°) and afternoon-west
(az ≈ +53°) — with factors clustered around 0.29. There is no weather noise:
the ratio is either 1.0 or a clean shading factor.

**How this integration differs (a deliberate gap to be aware of).** Our pipeline
follows the same tune→shade *shape*, but the tuning loop is **advisory and
manual**, not automatic:

- `compute_dampening` consumes the **raw base-integration forecast** (`pv_estimate`
  is written to the DB straight from `solcast_solar`, unmodified by our tuner).
- `run_tuning`'s output is surfaced only as the **"Tuned Panel Tilt/Azimuth"**
  sensors; it is **never fed back** into the estimate.

So the "tuned estimate" stage is closed by the human: the user reads the suggested
tilt/azimuth, updates their **Solcast account** site configuration, and the base
forecast then becomes the tuned estimate that dampening refines. Consequently our
dampening is strictly a **residual-bias dampening**, not a pure shading
correction — it equals "shading" only when the Solcast site is already
well-configured. On a mis-configured site it would fold orientation/capacity error
into the dampening curve, the very conflation 3.4b's prerequisite avoids. (Two
further divergences from 3.4b: we push a single **hourly** curve applied in **all**
conditions, where 3.4b is a 2-D geometry grid applied on **clear-sky only**.)

**Convergence gate (implemented).** Rather than silently folding that error in,
the dampening push is *gated* on the tuner agreeing with the configured site. In
`_run_dampening`, before each push (per-site and the single-site aggregate),
`_orientation_diverged` compares the latest `run_tuning` result against the
configured/seed orientation. When tuning is **confident**
(`n_records ≥ DAMPENING_GATE_MIN_RECORDS`, 50) **and** the tuned tilt or azimuth
diverges materially (`|Δtilt| > 15°` or shortest-circle `|Δazimuth| > 25°`), that
target's factors are forced to neutral `1.0` and a `dampening_gated` repair issue
is raised telling the user to apply the *Tuned Panel Tilt/Azimuth* sensors in
their Solcast account. The gate is **per-site aware** — each site is judged
against its own seed (`_site_orientation_seed`) using that site's tuning result,
so one mis-configured array is held neutral without freezing the others. It is on
by default (`CONF_DAMPENING_GATE`) and can be disabled in the tuning options. The
azimuth comparison uses `_angle_diff` (signed shortest distance on the circle) so
e.g. 350° vs 10° reads as 20° apart, not 340°.

**Guidance:** apply the *Tuned Panel Tilt/Azimuth* values in the Solcast account
before relying on dampening for accuracy. While they disagree the gate keeps
dampening neutral so no orientation-contaminated curve is pushed.

### Why cloud filtering is essential

The dampening factor is `total_pv / pv_estimate`. On a clear day this
reflects shading geometry. On a cloudy day it reflects cloud attenuation
— a different effect already modelled by Solcast. Including cloudy records
corrupts the dampening factor, causing systematic underestimation on
future clear days. The OWM `clouds` value (0–100) stored per record
drives the filtering.

**OpenWeatherMap is therefore a functional requirement of this feature, not an
optional extra.** The per-record cloud percentage comes *only* from OWM
(`OWMClient.async_fetch` → `clouds.all`). The design is **fail-safe**: when OWM is
disabled (the config default) or a fetch fails, the in-memory weather defaults to
*unknown* (`clouds = None`, `temp = None`), and at the DB-write boundary the
coordinator coerces the unknown cloud value to the **`100` sentinel** — a value
the clear-sky filter *excludes*. So a record written without OWM data can never
masquerade as clear sky: tuning finds nothing to fit (returns `None`) and
dampening reports `no_data` (stays neutral `1.0`, pushes nothing). The Cloud
Cover / Weather Temperature sensors read the raw `None` and show *unavailable*
rather than a misleading `0 %` / `0 °C`. To make this loud rather than silent,
`async_setup` raises a **repair issue** (`ISSUE_OWM_REQUIRED`) whenever a
cloud-driven feature (`auto_tuning` / `auto_dampening`) is enabled but no OWM
source is configured; it clears when OWM is added or on unload.

*Why `100`, not `0`:* both `0` and `100` are valid real readings, so the
"unknown" sentinel must sit on the **excluded** side of the filter. `0` collides
with real clear sky (the highest-quality data → would be *trusted*); `100`
collides with real overcast (already excluded → *safe*). The same reasoning fixed
the falsy-`0` coercion bug in v1.6.2/3. See the README *OpenWeatherMap*
prerequisite for the access requirements.

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
  # Optional per-MPPT capture (Phase 2), on a single-site group or per string:
  "mppts": [{"voltage_sensor": "sensor.mppt1_v", "current_sensor": "sensor.mppt1_i"}],
}
```

The config-flow `sites` step collects, per discovered site, a generation sensor,
an optional DC/MPPT (power) sensor, a mode, and optional per-tracker DC
voltage/current capture sensors; `_derive_groups()` then groups sites that share an
AC sensor (shared → DC-apportioned; alone → single-site; shared-without-DC →
omitted) and attaches each site's `mppts` list. `_groups_to_assignments()` reverses
this for options-flow prefill. Note the two DC roles are distinct: `dc_sensor` is
**power**, used only as an apportionment ratio; `mppts` is **instantaneous
voltage/current**, captured for curtailment detection.

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

## Sensors (15 total)

| Sensor class | `_attr_name` | Unit | Description |
|---|---|---|---|
| `ForecastNowSensor` | Forecast Now | kW | Current 30-min PV forecast |
| `ForecastTodaySensor` | Forecast Today | kWh | Total forecast for today |
| `TuningTiltSensor` | Tuned Panel Tilt | ° | Optimised tilt |
| `TuningAzimuthSensor` | Tuned Panel Azimuth | ° | Optimised azimuth |
| `TuningRmseSensor` | Tuning RMSE | kW | Goodness of fit |
| `TuningExportExcludedSensor` | Tuning Export Limited Excluded | — | Records dropped by export limit filter in last tuning run |
| `DbRecordsSensor` | Database Records | — | Total DB record count |
| `MpptDcSensor` | MPPT DC Voltage (max) | V | Diagnostic: latest captured per-MPPT DC telemetry (max string voltage; per-tracker V/I + per-site in attributes). Unavailable when no DC sensors configured |
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

The three 30-min average sensors (`PvActualSensor`, `PvExportSensor`,
`BatteryChargeSensor`) extend `_RestoringSensorBase` (HA `RestoreSensor`): the
coordinator only produces data on the half-hour grid, so after a restart they
restore their last value rather than reading *unknown* until the first update
cycle (~30 min); the live coordinator value supersedes the restored one once it
arrives.

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
| Azimuth | Number | Solcast convention — 0°=North, positive=West (→+180°), negative=East (→−179°). Converted to the internal East-positive frame for tuning (`panel_azimuth_to_internal`) |
| PV Power / Generation sensor | Entity selector* | Energy counter or power sensor for generation |
| PV sensor type | Select | Auto-detect / power / energy counter |
| PV Export sensor | Entity selector* | Energy counter or power sensor for export |
| PV Export sensor type | Select | Auto-detect / power / energy counter |
| Battery sensor | Entity selector* | Energy counter or power sensor for battery charge |
| MPPT 1/2 voltage + current | Entity selector* | Optional per-tracker **instantaneous** DC voltage/current for curtailment-detection capture (raw per-string sensors; the integration aggregates max-V/min-I over each slot) |

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

**Step 3 — OpenWeatherMap (required for tuning & dampening)**

Off by default. Without it, cloud cover is stored as the unknown/excluded
sentinel, so tuning/dampening have no clear-sky data and stay inert, and a
repair issue is raised — see
[Why cloud filtering is essential](#why-cloud-filtering-is-essential). Needed
unless you only want raw history logging.

| Field | Default | Description |
|---|---|---|
| Enable OWM | Off | Fetch per-cycle cloud cover (drives the clear-sky filter) |
| OWM API Key | "" | Free key from openweathermap.org — Current Weather Data API |

OWM endpoint used (free tier; ~48 calls/day vs the free 60/min, ~1M/month limit):
```
GET https://api.openweathermap.org/data/2.5/weather
    ?lat={latitude}&lon={longitude}&appid={key}&units=metric
```
Parses: `main.temp` (°C), `clouds.all` (0–100 int),
`weather[0].description` (text). A failed/invalid fetch falls back to *unknown*
(`temp`/`clouds` = `None` → stored as the excluded `100` sentinel; sensors show
*unavailable*), logged as `OWM fetch failed`.

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
| `CONF_MPPT1_VOLTAGE_SENSOR` | mppt1\_voltage\_sensor | "" |
| `CONF_MPPT1_CURRENT_SENSOR` | mppt1\_current\_sensor | "" |
| `CONF_MPPT2_VOLTAGE_SENSOR` | mppt2\_voltage\_sensor | "" |
| `CONF_MPPT2_CURRENT_SENSOR` | mppt2\_current\_sensor | "" |
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
- Lazy numpy import (no scipy) — feature disabled with informational log if numpy
  is somehow absent (it ships with Home Assistant)

**New dependencies:** `numpy>=1.21.0` (ships with Home Assistant). No scipy.

### Phase 4 — Short-range forecast correction (dropped)

Evaluated and **not pursued** — see [Feature 4](#feature-4--short-range-forecast-correction-dropped)
for the reasoning. No implementation work is planned.

---

## Roadmap

Planned work, plus recently-landed items kept here with their rationale.

### Indexed day-of-year column for the seasonal dampening scan

**Problem.** The dampening recalculation runs a seasonal day-of-year query
(`async_get_records_for_dampening`) whose filter is
`ABS(CAST(strftime('%j', period_end_epoch, 'unixepoch') AS INTEGER) - ?) <= ?`.
Because the day-of-year is a *computed* expression, no index can serve it — the
query is a full table scan. On a multi-year, multi-site database running on a
Raspberry Pi (SD-card I/O), this scan gets progressively slower. (The 48×
redundant re-scan per run was already removed — the fetch is hoisted to once per
`_compute_dampening_slots` call — so what remains is the single O(N) scan.)

**Option.** Persist the UTC day-of-year in a stored column at insert time and
index it, turning the seasonal scan into an indexed range lookup. Requires a
schema addition and a one-time backfill migration (the `PRAGMA user_version`
mechanism added for the azimuth repair already provides the gating for this).

**Status.** Deferred — the **retention** half of this roadmap item is now
implemented (see below), which already bounds the row count (and therefore the
scan) on long-lived installs. The indexed column would additionally bound
per-query cost when retention is left at *keep everything*; revisit if that
proves necessary.

### Database retention (implemented)

`CONF_DB_RETENTION_DAYS` (Storage step; default `0` = keep everything, so existing
behaviour is unchanged) prunes rows older than the window. `SqliteStore.async_prune`
runs a plain `DELETE … WHERE period_end_epoch < cutoff` on a **daily** timer in the
coordinator (independent of auto-tuning, so it applies to logging-only setups). No
`VACUUM`: in the steady state old rows are deleted as fast as new ones arrive, so
SQLite reuses freed pages and the file size stabilises without a heavy SD-card
rewrite. Because seasonal dampening uses a cross-year day-of-year window, a value
below `DB_RETENTION_MIN_RECOMMENDED_DAYS` (≈13 months) logs a warning but is still
honoured (not a hard floor).

### Curtailment-aware actual/forecast filtering (DC-telemetry off-MPP detection)

**Problem.** When clear-sky PV output exceeds the combination of household load
and the grid export limit, the inverter *curtails* — it holds output below what
the panels could make. The resulting `pv_actual` no longer measures available
generation, so the actual-vs-forecast comparison that drives **both** tuning and
dampening is corrupted on exactly the clear-sky days those features depend on.
This already affects sites with an export limit below their array's clear-sky
peak (most residential self-consumption sites), and will become near-universal as
**variable export limits** and **emergency-stop / curtailment** schemes roll out.

Measured on a 12 k-row Melbourne database (single 5 kW-export site): the export
meter pegs at a hard ~5 kW ceiling, household load sits at p50 ≈ 0.53 kW, and
**~50 % of high-sun clear-sky records (Oct–Apr) are curtailed** — clustering in
the high-irradiance shoulder/summer months and vanishing in deep winter (May:
0 %), the inverse of the "clearer = faster convergence" intuition. The raw
clear-sky `actual/forecast` ratio reads **0.890** — an apparent 11 % shading
penalty that is mostly curtailment, not shading. Two independent corrections
(clip-the-forecast and headroom-only) both recover **≈0.955**, i.e. the true
unshaded ratio, confirming ~5 % real shading masked by ~6 % spurious curtailment.

**Current state.** Detection is a *heuristic*; both consumers now handle export
curtailment (Phase 1 landed it on the dampening side — see Rollout below):

| Consumer | Inverter-clip guard | Export-cap handling | Method |
|---|---|---|---|
| Tuning (`pv_tuning.run_tuning`) | yes (`total_pv ≥ capacity×threshold`) | yes (`pv_export ≥ export_limit×threshold`, `export_limited_excluded`) | excludes curtailed records |
| Dampening (`shading_dampening.compute_dampening`) | yes (`clipped_excluded`) | yes (`export_limit_kw`, `forecast_clipped`) | clips the forecast to the achievable ceiling |

Both guards remain heuristics — they infer curtailment from the AC side (output
flat, export pegged) and so are forecast-/limit-dependent, blind to the cause, and
miss the `battery-full + export-capped` double-curtailment case. Tier-1 DC
telemetry (below) removes those limitations.

**The off-MPP signal (why DC voltage is the ground truth).** Curtailment *is* a
DC-side phenomenon. A PV string is a current source; to deliver less power the
inverter cannot lower current at fixed voltage — it walks the operating point off
the maximum-power point **up the I-V curve toward open-circuit**: voltage rises
toward `Voc`, current collapses. So an elevated DC string voltage is a *direct
measurement* of curtailment, independent of the forecast, independent of the
(possibly dynamic) export limit, and identical regardless of cause (static cap,
variable limit, emergency stop, frequency-watt, battery-full). It also unifies
the two heuristics above: inverter AC clipping and export curtailment are the
*same* off-MPP excursion on the DC side, so one measured flag subsumes both.

**Tiered detection (graceful degradation).** Because per-string DC telemetry is
opt-in and brand-dependent, detection is a ladder; nothing is removed, it is
ranked, and the best available tier is used:

| Tier | Signal | Catches | Applies to |
|---|---|---|---|
| 1 (best) | per-MPPT DC voltage (+ current) → off-MPP | export curtailment **and** inverter clip, cause-agnostic, limit-independent | most local-Modbus inverters with DC entities |
| 2 | `pv_export ≥ export_limit × threshold` (ideally the *dynamic* limit) | export curtailment only | SolarEdge, cloud-only, no DC sensors |
| 3 | `total_pv ≥ capacity × clipping_threshold` (existing) | inverter AC clip only | last resort |

**Effectiveness scales with how much DC data is provided — and why.** Within
Tier 1 the detector's accuracy is a function of the channels it is given. Each
extra channel removes a specific, physically-identifiable failure mode, so more
DC data buys strictly higher effectiveness:

| DC data provided | Detection level | What it resolves | Residual blind spot |
|---|---|---|---|
| None (AC heuristics only) | baseline | export-pegged + AC-clip | cause-blind, forecast-dependent, misses battery-full+capped & unknown/dynamic limits |
| Per-string **voltage** | direct off-MPP | curtailment seen as a measurement, not an inference; limit-independent | cold-clear mornings sit at genuinely high `Vmp` *at* MPP → false positive |
| Voltage **+ current** | disambiguated | the cold-morning case: curtailment is high-V **and** low-I; MPP is high-V **and** high-I | a static voltage threshold still drifts with temperature |
| Per-**MPPT** (not inverter-aggregate) | per-site | *asymmetric* curtailment — one string pushed off-MPP while another (past its own peak) stays at MPP | — matches the per-site tuning/dampening granularity |
| + DC power / multi-MPPT + `temp` | self-calibrating | learns each string's `Vmp` band from high-current intervals, tracking the temperature drift of `Vmp`/`Voc`; no hand-set threshold | (enables future *wing-reconstruction* to recover curtailed days for tuning) |

The physical reasons, in order:

- **Voltage alone works** because curtailment is, definitionally, an excursion in
  voltage toward `Voc` — it is the single most informative channel.
- **Current is needed** because voltage is ambiguous at the cold/clear extreme:
  a string genuinely at MPP on a cold morning sits at a naturally high `Vmp`,
  which looks like the start of an off-MPP excursion. Current resolves it — at MPP
  the current is high (tracking irradiance); off-MPP it collapses. High-V+low-I is
  curtailment; high-V+high-I is just a cold clear morning.
- **Per-MPPT matters** because curtailment is enforced at the inverter's AC
  setpoint but distributes *unequally* across trackers — each string is pulled off
  MPP according to its own instantaneous position on its own power curve. Only
  per-string voltage sees *which* strings were actually throttled, which is the
  granularity per-site tuning and per-site dampening already operate at.
- **Power + temperature context matters** because `Vmp`/`Voc` shift with cell
  temperature (~−0.3 %/°C on `Voc`), so any *fixed* voltage line is a
  climate-specific fit. Learning the `Vmp` band from high-current (provably-at-MPP)
  intervals gives a *relative*, temperature-tracking threshold that needs no user
  input and stays correct across seasons.

**Consumer wiring (unchanged by tier).** What each feature does with a flagged
record is independent of how the flag was derived:

- **Tuning → exclude** the record (a flat-topped clipped peak has no geometry to
  fit; it cannot be tuned on). Costs ~50 % of high-sun clear-sky records at an
  export-limited site, hence the convergence-timeline caveat below.
- **Dampening → clip the forecast** to the achievable ceiling
  (`min(pv_estimate, load + export_limit)`) so the record still contributes a
  valid ≈1.0 ratio instead of a spurious penalty (recovers 0.890 → 0.954 with **no
  record discarded**); or, with a hard Tier-1 flag, simply **neutralise** the
  record (treat like `no_data` for the shading signal — simpler, but forgoes the
  partial-clip signal). The `export_limit` ceiling is still required for the
  clip-forecast math and for the Tier-2 fallback, so it is stored regardless.

**Storage shape.** Per-record columns capture each tracker's pair, kept
**per-MPPT (not aggregated)** so a later `Vmp`-band calibrator can learn each
string: `dc_voltage1` / `dc_current1` / `dc_voltage2` / `dc_current2` (up to
`MAX_MPPT_TRACKERS = 2`). Each per-site row carries that site's trackers; the
`_total` row carries the property-wide / single-inverter trackers. Still to add
when detection lands: `export_limit` (the active, possibly dynamic, limit) and a
derived `curtailed` boolean (`_total.curtailed = OR` across contributing strings —
if any was throttled, the property-wide comparison is compromised for that slot).
All new columns are **forward-only** (not retro-modellable on existing rows; the
0.890/0.955 figures above stand as the heuristic baseline). The DC read is
**aggregated over the slot** — **maximum voltage** (most off-MPP) and **minimum
current** (most throttled) from recorder history (`_interval_values` →
`get_significant_states`), so a mid-slot off-MPP excursion is caught rather than
only what the half-hour-boundary sample happens to show; it falls back to the
instantaneous state when the recorder is unavailable, so users can point at raw
per-string sensors (no statistics helper needed).

**Hardware applicability.** The integration consumes HA *entities*, not inverters,
so this works wherever the upstream integration surfaces per-string DC voltage (+
current). **SunSpec Model 160** ("Multiple MPPT Inverter Extension") over Modbus
TCP/RTU is the common denominator — SMA, Huawei (`huawei_solar`), Sungrow, GoodWe,
SolaX, Victron (via GX → Modbus/MQTT) and Fronius (Solar API + Modbus) all expose
it. **Cloud APIs** (Growatt cloud, SolarEdge monitoring, Fronius Solar.web) are
unsuitable — latency/rate-limits break per-half-hour sampling. **CAN bus** is a
non-path: in solar it is the inverter↔battery-BMS link, not PV-string telemetry
(Victron VE.Can data reaches HA re-published as Modbus/MQTT). The structural
exception is **SolarEdge**: per-panel optimizers hold the string at a fixed DC-bus
voltage, so the inverter never walks voltage toward `Voc` to curtail — the off-MPP
fingerprint does not exist, and SolarEdge is therefore **Tier-2 only**.

**Effect on the convergence documentation.** The "time to full confidence" tables
(README / DESIGN) assume every clear-sky record is usable, which is false under an
export limit — and the clearest, sunniest sites curtail *most*, inverting the
table's "clearer = faster" ordering. With curtailment-aware filtering the honest
statement is: dampening's broad clear-sky pool loses only ~22 % (≈1.3× slower, or
*unchanged* if clip-forecast keeps the records), while tuning's high-sun subset
loses ~50 % (**≈2× slower** for export-limited sites). The quality-weighted
*record-count* tables are the climate- and curtailment-independent truth and
should lead; the *weeks* tables should be framed as illustrative for an
unconstrained site, with an export-limit caveat. With a Tier-1 flag the caveat
strengthens from "heuristically inferred" to "reliably detected via inverter DC
telemetry," tier-dependent.

**Rollout (two phases).**
1. **Implemented (Phase 1, data-only).** Export-aware handling on the **dampening**
   side: `compute_dampening` takes `export_limit_kw` (sourced from the base
   `site_export_limit`, falling back to `CONF_EXPORT_LIMIT_KW`) and clips the
   forecast to the achievable ceiling `total_pv + (export_limit − pv_export)`,
   floored at the delivered output so the ratio never exceeds 1.0. A curtailed
   clear-sky record now contributes a neutral ≈1.0 ratio instead of a spurious
   penalty, with **no record discarded**; a `forecast_clipped` counter is returned
   and surfaced per hour on the Dampening sensor (`hour_NN_forecast_clipped`).
   Validated on the 12 k-row reference DB: the high-sun-slot `db_factor` recovers
   0.909 → 0.943 (320 records clipped). Tuning already had the Tier-2/3 guards, so
   it was unchanged. Works on the **existing** database, no new hardware data.
2. **Phase 2 — capture started.** The data-banking foundation is implemented,
   capturing **paired per-MPPT** telemetry (up to `MAX_MPPT_TRACKERS = 2` trackers,
   voltage + current kept together per tracker so the pairing the `Vmp` calibrator
   needs survives):
   - **Schema.** `dc_voltage1` / `dc_current1` / `dc_voltage2` / `dc_current2`
     columns on `solcast_data`, added to existing databases in place by an additive
     `ALTER TABLE` (`_ADDED_COLUMNS` / `_ensure_columns`); legacy rows backfill to 0
     (verified on the 12 k-row reference DB — all four columns added, 12 000 rows
     intact).
   - **Config.** Flat per-tracker keys (`CONF_MPPT1_VOLTAGE_SENSOR` /
     `…1_CURRENT` / `…2_VOLTAGE` / `…2_CURRENT`) on the site step for the
     property-wide / single-inverter case, and four per-site **MPPT 1/2
     voltage/current** fields in the multi-site mapping step — derived into an
     `mppts` list on each `CONF_SITE_GROUPS` single-site group or per-string entry
     (`_fields_to_mppts` compacts trackers that have a voltage sensor), reversible
     for prefill.
   - **Read + store.** Each cycle one batched `get_significant_states`
     (`_interval_values`) fetches the slot's recorded values for all DC entities
     (`_collect_dc_entities`); `_interval_extreme` takes **max voltage / min
     current** over the slot (plus the instantaneous reading), so a mid-slot
     off-MPP excursion isn't missed. `_read_mppt_telemetry` assembles the flat
     `(v1, i1, v2, i2)` per tracker (or `None` when nothing is configured),
     `_read_site_dc_telemetry` maps each site, `_mppt_list_from_opts` builds the
     property-wide list for the `_total` row. Falls back to the instantaneous state
     when the recorder is unavailable.

   Still to do before promotion: the per-string **`Vmp`-band calibrator**; the
   **`curtailed` flag** + `export_limit` column; and wiring detection into the
   consumers (tuning excludes, dampening clips). These wait on accumulated
   telemetry.

**Status.** Phase 1 **implemented** (dampening clip-forecast). Phase 2 **capture
implemented** (DC telemetry schema + config + per-cycle store) — banking data now;
Tier-1 **detection** (Vmp calibrator, `curtailed` flag, consumer wiring) pending
data accumulation. *Wing-reconstruction* (fit the clear-sky curve to a day's
unclipped morning/evening points and interpolate the clipped midday, to recover
curtailed days for tuning rather than discarding them) remains proposed — Tier-1
detection perfects the *flag*, but recovering available generation from an off-MPP
point still needs the curve fit.

---

## Dependency handling

```python
# Storage has no optional dependency — it uses the stdlib sqlite3 module,
# so the built-in store always works.

# PV tuning needs only numpy (a core Home Assistant dependency with Raspberry Pi
# wheels). It is imported lazily so an unusual env without numpy degrades
# gracefully rather than failing to load. There is deliberately NO scipy: scipy
# has no prebuilt ARM/Pi wheel and its from-source build fails under HA's locked
# environment (BJReplay/ha-solcast-solar #85), so the optimiser is a pure numpy
# grid search instead (pv_tuning._minimize_grid).
try:
    import numpy as np
    TUNING_AVAILABLE = True
except ImportError:
    TUNING_AVAILABLE = False
    _LOGGER.info("numpy not installed — PV tuning disabled")
```

`manifest.json` keeps `"requirements": []` — numpy is not pinned there because HA
already provides it, and adding scipy there is exactly what broke the base
integration on Raspberry Pi (#85). Storage adds no dependency at all.

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
| 1.8 | Jun 2026 | Aligned with the v1.6.4 release: PV tuning optimiser switched from scipy L-BFGS-B to a pure-numpy coarse-to-fine grid search (`_minimize_grid`, azimuth-outer + tilt-batched for Raspberry Pi; scipy dependency removed, issue #85); per-site dampening convergence gate (`CONF_DAMPENING_GATE`, `_orientation_diverged`, `dampening_gated` repair issue); optional history retention (`CONF_DB_RETENTION_DAYS`, `SqliteStore.async_prune`); vectorised tuning record filter; per-site weather-coercion crash fix (`_weather_for_storage`); translations added for de/es/fr/it/ja/nl/pl/pt/sk/ur |
| 1.9 | Jun 2026 | Aligned with the v1.6.5 release: fixed the panel-azimuth convention mismatch — `CONF_AZIMUTH` (Solcast West-positive) is now converted to the internal East-positive solar frame at every tuning seed and the dampening gate, and the tuned azimuth is converted back for display (`panel_azimuth_to_internal` / `panel_azimuth_to_solcast`); `tools/import_history.py` recomputes zenith as well as azimuth from the epoch midpoint and creates the destination directory if missing |
| 1.10 | Jun 2026 | Aligned with the v1.6.6 release: `async_get_records_for_tuning` applies the clear-sky filter (`clouds < cloud_threshold`) in SQL before the LIMIT, so tuning fits the most recent clear-sky records across seasons rather than a recent cloudy window; dampening factors are clamped to `[0,1]` in `_push_dampening` before the base `set_dampening` call (the base rejects values outside that range), with the unclamped value retained in the dampening sensor attributes |
| 1.11 | Jun 2026 | Aligned with the v1.6.7 release: curtailment-aware dampening (Phase 1 of the DC-telemetry roadmap) — `compute_dampening` takes `export_limit_kw` and clips the forecast to the achievable ceiling `total_pv + (export_limit − pv_export)`, floored at the delivered output so the ratio ≤ 1.0; curtailed clear-sky records contribute a neutral ≈1.0 ratio instead of a spurious shading penalty, none discarded; `forecast_clipped` count surfaced per hour (`hour_NN_forecast_clipped`); export limit sourced from the base `site_export_limit` (manual fallback, `0` = no-op). Brings dampening to parity with tuning's existing export-limited exclusion |
| 1.12 | Jun 2026 | Aligned with the v1.6.8 release: Phase 2 **capture** of the DC-telemetry roadmap — paired per-MPPT DC string voltage/current (up to `MAX_MPPT_TRACKERS = 2`, kept per-tracker for a future `Vmp`-band calibrator). New `dc_voltage1/current1/voltage2/current2` columns added to existing DBs by additive `ALTER TABLE` (`_ensure_columns`); config fields on the site step (flat keys) and per-site multi-site mapping step (`mppts` list); reads aggregated over each slot from recorder history as max-voltage/min-current (`_interval_values`/`_interval_extreme`) with an instantaneous fallback. Capture only — no detection acts on it yet; forward-only. Field translations added across all 11 languages |
| 1.13 | Jun 2026 | Aligned with the v1.6.9 release: diagnostic `MpptDcSensor` ("MPPT DC Voltage (max)") surfaces the latest captured per-MPPT DC telemetry (`coordinator.data["dc_telemetry"]` via `_dc_telemetry_summary`) for wiring verification — state = max string voltage, attributes = per-tracker V/I + per-site; entity category diagnostic, unavailable when no DC sensors configured. Sensor count 14 → 15 |

---

*End of design document*
