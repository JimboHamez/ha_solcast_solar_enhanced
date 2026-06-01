# Solcast Solar Enhanced — Design Document

**Prepared for collaboration with BJReplay/ha-solcast-solar**
**Version 1.4 — June 2026**

---

## Overview

This document describes a proposed enhancement to the
[BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar)
Home Assistant integration. The enhancement adds four capabilities:

1. **MySQL database storage** of PV power averages, forecasts, solar
   position, weather and battery data
2. **Automatic Rooftop PV Tuning** — tilt and azimuth optimisation via
   scipy, based on Solcast SDK notebook 3.4
3. **Adaptive Shading Dampening** — DB-derived quality-weighted dampening
   that blends with and progressively replaces the existing dampening
   system as historical data accumulates, based on Solcast SDK notebook 3.4b
4. **Short-range Forecast Correction** — live cloud/output adjustment of
   the next 1–6 hours of forecast (planned, not yet implemented)

The current working prototype runs as a standalone companion integration
(`solcast_solar_enhanced`) that reads all Solcast data from the base
integration's coordinator — making **zero additional Solcast API calls** —
and pushes improved dampening values back via the existing
`set_dampening_factor` service.

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
├── db_manager.py            Async MySQL pool, schema, migrations
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
        ├── read pv_power      from HA Statistics sensor (30-min linear avg)
        ├── read pv_export     from HA Statistics sensor (30-min linear avg)
        ├── read battery       from HA Statistics sensor (30-min linear avg)
        │                      └─ falls back to raw battery sensor if not configured
        ├── fetch OWM weather  (temp °C, clouds 0–100, description text)
        ├── persist record     to MySQL
        ├── run PV tuning      scipy L-BFGS-B (daily, executor thread)
        ├── compute dampening  quality-weighted blend of DB + base integration
        └── push dampening     → base integration set_dampening_factor service
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

## HA Statistics Sensor Prerequisite

### Why Statistics sensors are used

`pv_actual`, `pv_export` and `battery_charge` must represent the
**true 30-minute average power** over each period, not an instantaneous
reading. This is critical because Solcast's `pv_estimate` is itself a
30-minute average. The dampening ratio `total_pv / pv_estimate` is only
meaningful when both values represent the same time-averaged quantity.

Rather than implementing custom sampling and averaging code, this
integration uses **Home Assistant's built-in Statistics integration**
with the `mean_linear` characteristic. HA handles sample collection
(up to 1800 samples per period), period alignment, linear averaging and
sensor lifecycle — we simply read the resulting sensor state at the
30-minute write interval.

### What mean_linear provides

The `mean_linear` characteristic computes a **time-weighted linear
average** (trapezoidal integration) over the configured window:

```
average = Σ(value_i × duration_i) / total_duration
```

This correctly weights a reading of 5.2 kW that lasted 8 seconds more
than one that lasted 2 seconds, giving a true energy-equivalent average
power over the period that is directly comparable to Solcast's 30-minute
period estimates.

### Required Statistics sensor configuration

Three Statistics sensors must be created in HA before configuring this
integration. This can be done via the HA UI
(**Settings → Devices & Services → Add Integration → Statistics**) or
via YAML:

**PV Power (generation):**
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

**PV Export:**
```yaml
sensor:
  - platform: statistics
    name: "PV Export 30min Average"
    entity_id: sensor.YOUR_GRID_EXPORT_POWER_SENSOR
    state_characteristic: mean_linear
    max_age:
      minutes: 30
    sampling_size: 1800
```

**Battery Charge (optional):**
```yaml
sensor:
  - platform: statistics
    name: "Battery Charge 30min Average"
    entity_id: sensor.YOUR_BATTERY_CHARGE_POWER_SENSOR
    state_characteristic: mean_linear
    max_age:
      minutes: 30
    sampling_size: 1800
```

**Important notes:**

- Source sensors must report **power in kW** (not energy in kWh)
- For battery net sensors (signed), the Statistics sensor correctly
  averages signed values; the integration takes `max(0, value)` to
  extract charge-only periods
- `sampling_size: 1800` supports one sample per second; actual buffer
  usage depends on source sensor update frequency
- `max_age: minutes: 30` ensures the average always covers the last
  30-minute period, aligning with Solcast period boundaries

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

## Feature 1 — MySQL Database Storage

### Purpose

Persists historical PV data alongside solar position, weather and battery
state for use by the shading dampening and PV tuning calculations.

### Database schema

```sql
CREATE TABLE solcast_data (
  `index`          INT AUTO_INCREMENT PRIMARY KEY,
  period_end       TEXT NOT NULL,
  period_end_epoch BIGINT NOT NULL,
  period_start     TEXT NOT NULL,
  pv_actual        DECIMAL(10,4) NOT NULL,        -- 30-min linear avg (kW)
  pv_export        DECIMAL(10,4) NOT NULL DEFAULT 0.0000,  -- 30-min linear avg (kW)
  pv_estimate      DECIMAL(10,4) NOT NULL,        -- from Solcast via base integration
  pv_estimate10    DECIMAL(10,4) NOT NULL,
  pv_estimate90    DECIMAL(10,4) NOT NULL,
  azimuth          DECIMAL(10,5) NOT NULL,        -- solar azimuth at period end (°)
  zenith           DECIMAL(10,5) NOT NULL,        -- solar zenith at period end (°)
  temp             DECIMAL(10,2) NOT NULL,        -- OWM temperature (°C)
  clouds           INT NOT NULL,                  -- OWM cloud cover (0–100)
  description      TEXT NOT NULL,                 -- OWM weather description
  battery_charge   DECIMAL(10,4) NOT NULL DEFAULT 0.0000,  -- 30-min linear avg (kW)
  UNIQUE KEY uq_epoch (period_end_epoch),
  INDEX idx_period_end ((CAST(period_end AS CHAR(25))))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

### Total PV energy balance

Throughout the codebase total PV output is always computed as:

```
total_pv = pv_actual + pv_export + battery_charge
```

All three values are 30-minute linear averages from Statistics sensors,
directly comparable to `pv_estimate`. Used in:

- Dampening ratio: `total_pv / pv_estimate`
- Clipping detection: `total_pv >= capacity × clipping_threshold`
- PV tuning RMSE: `total_pv` vs geometrically-scaled estimate

### Schema initialisation and migration

On every startup `_init_schema()` runs the following sequence:

1. Queries `information_schema.TABLES` to check whether `solcast_data` already exists
2. Issues `CREATE TABLE` **only if the table does not exist** — skips it entirely otherwise
3. Runs two idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements for `battery_charge` and `pv_export`

This means switching from `db_readonly = True` to `False` on an existing database does not fail if the MySQL user lacks `CREATE TABLE` privilege. The `SELECT` required for `information_schema` is available to any connected user.

```sql
ALTER TABLE solcast_data
  ADD COLUMN IF NOT EXISTS battery_charge DECIMAL(10,4) NOT NULL DEFAULT 0.0000;
ALTER TABLE solcast_data
  ADD COLUMN IF NOT EXISTS pv_export DECIMAL(10,4) NOT NULL DEFAULT 0.0000;
```

The `battery_charge` column's presence is detected via `information_schema` on startup (`has_battery_col` flag) and SQL queries substitute `0.0 AS battery_charge` when absent.

### Battery charge safety layers

| Layer | Location | Default |
|---|---|---|
| Statistics sensor not configured | Config check | 0.0 |
| Sensor state unavailable | `_safe_read_sensor()` | 0.0 |
| DB column absent (read-only external DB) | SQL: `0.0 AS battery_charge` | 0.0 |
| DB value NULL | SQL: `COALESCE(battery_charge, 0.0)` | 0.0 |
| Calculation | `float(rec.get("battery_charge", 0) or 0)` | 0.0 |
| Read-only DB, column always zero | Warning logged + sensor attribute | — |

### Sensor mapping guidance

```
Total PV Output = pv_actual + pv_export + battery_charge
```

| Scenario | pv_actual source | pv_export source |
|---|---|---|
| Generation meter (total inverter AC) | Generation meter | Grid export meter |
| Self-consumption meter only | Self-consumption meter | Grid export meter |

The sum `pv_actual + pv_export + battery_charge` must equal total panel
generation (minus conversion losses) for dampening ratios to be correct.

### Read-only mode

When `db_readonly = True` the integration reads dampening/tuning history
but never writes. Intended for databases populated by an external source.
`battery_charge` column detection still runs — absence is handled
transparently via SQL substitution.

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

The system blends the base integration's existing dampening with the
DB-derived factor:

```
final_factor(h) = (1 - α) × base_factor(h)  +  α × db_factor(h)
```

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
to within ±15% of the base factor. This prevents a single anomalous day
(sensor fault, heavy partial cloud) from distorting the dampening while
the DB is still accumulating data.

### Fallback chain when DB is unavailable or insufficient

For each half-hour slot where DB data is insufficient, the system falls
back through:

1. **DB data with seasonal extrapolation** — `±14-day` window queries
   already provide extrapolation for slots with few records
2. **Base integration config entry** — reads `entry.options["dampening"]`
   from the base `solcast_solar` config entry; α = 0.0
3. **Base integration sensor states** — reads
   `sensor.*solcast*dampening*hour_XX` entities; α = 0.0
4. **Retain existing table** — logs a warning, keeps previous values

This ensures dampening always works from day one without a database.

### Per-slot vs hourly resolution

The calculation operates at **48 half-hour slots per day** internally.
Each slot gets its own α and quality metrics. Adjacent pairs of 30-min
slots are averaged into 24 hourly values for the `set_dampening_factor`
service call (which accepts hourly resolution). The full 48-slot table
is preserved for internal diagnostics.

### Dampening sensor attributes

The `Dampening Hours with DB Data` sensor (`_attr_name`) exposes
per-hour diagnostics:

```yaml
hour_14_factor:           0.847    # final blended value pushed to base integration
hour_14_alpha:            0.72     # DB confidence (0 = pure base, 1 = pure DB)
hour_14_source:           blended  # db_history | blended | base_fallback | night
hour_14_quality_records:  31.4     # quality-weighted record count
hour_14_avg_quality:      0.81     # mean combined_weight of contributing records
hour_14_clipped_excluded: 2        # records excluded due to clipping (shown if > 0)
overall_source:           blended  # summary across all hours
```

### Recalculation schedule

Dampening is recomputed every 6 hours. The schedule can be triggered
manually via the `solcast_solar_enhanced.run_dampening_update` service.

---

## Feature 4 — Short-range Forecast Correction (Planned)

### Purpose

Adjust the next 1–6 hours of forecast based on live Statistics sensor
output and current OWM cloud cover, correcting for satellite image
processing lag in Solcast's near-term predictions.

### Design (not yet implemented)

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
range 1–12). Will be added to the tuning step of the setup wizard.

---

## Sensors (13 total)

| Sensor class | `_attr_name` | Unit | Description |
|---|---|---|---|
| `ForecastNowSensor` | Forecast Now | kW | Current 30-min PV forecast |
| `ForecastTodaySensor` | Forecast Today | kWh | Total forecast for today |
| `TuningTiltSensor` | Tuned Panel Tilt | ° | Optimised tilt |
| `TuningAzimuthSensor` | Tuned Panel Azimuth | ° | Optimised azimuth |
| `TuningRmseSensor` | Tuning RMSE | kW | Goodness of fit |
| `DbRecordsSensor` | Database Records | — | Total DB record count |
| `DampeningSensor` | Dampening Hours with DB Data | — | Hours with DB-derived factors |
| `WeatherTempSensor` | Weather Temperature | °C | OWM current temperature |
| `WeatherCloudsSensor` | Cloud Cover | % | OWM cloud cover |
| `BatteryChargeSensor` | Battery Charge 30min Average | kW | Stats sensor value |
| `PvActualSensor` | PV Power 30min Average | kW | Stats sensor value |
| `PvExportSensor` | PV Export 30min Average | kW | Stats sensor value |
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

Before configuring this integration, create three Statistics sensors in
HA (Settings → Devices & Services → Add Integration → Statistics):

| Statistics sensor | Source sensor | Characteristic | max\_age | sampling\_size |
|---|---|---|---|---|
| PV Power 30min Average | Inverter AC power (kW) | mean\_linear | 30 min | 1800 |
| PV Export 30min Average | Grid export power (kW) | mean\_linear | 30 min | 1800 |
| Battery Charge 30min Average | Battery charge power (kW) | mean\_linear | 30 min | 1800 |

Battery and export Statistics sensors are optional — only configure if
the relevant hardware is present and the DB feature is enabled.

### Setup wizard (5 steps)

**Step 1 — Site & System:**

| Field | Type | Description |
|---|---|---|
| Latitude | Number | Site latitude (-90 to 90) |
| Longitude | Number | Site longitude (-180 to 180) |
| Capacity (kW) | Number | System DC capacity |
| Tilt | Number | Panel tilt 0° (flat) to 90° (vertical) |
| Azimuth | Number | 0°=North, 90°=East, -90°=West |
| PV Power sensor | Entity selector* | Statistics sensor: 30-min avg generation |
| PV Export sensor | Entity selector* | Statistics sensor: 30-min avg export |
| Battery sensor | Entity selector* | Statistics sensor: 30-min avg battery charge |

*Falls back to text input if EntitySelector is unavailable in the
running HA version.

**Step 2 — MySQL Database (optional)**

| Field | Default | Description |
|---|---|---|
| Enable MySQL | False | Toggle database on/off |
| Host | localhost | MySQL host |
| Port | 3306 | MySQL port |
| Username | — | Credentials |
| Password | — | Credentials |
| Database name | solcast | Schema name |
| Read-only | False | External DB, do not write |

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

Raw battery sensor fallback for sites without a Battery Statistics sensor:

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
| `CONF_DB_ENABLED` | db\_enabled | False |
| `CONF_DB_HOST` | db\_host | localhost |
| `CONF_DB_PORT` | db\_port | 3306 |
| `CONF_DB_USER` | db\_user | "" |
| `CONF_DB_PASSWORD` | db\_password | "" |
| `CONF_DB_NAME` | db\_name | solcast |
| `CONF_DB_READONLY` | db\_readonly | False |
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

- Add Statistics sensor entity selector fields to config flow step 1
- Add optional MySQL connection to config flow options
- Add OWM API key to config flow options
- Extend coordinator to read Statistics sensors and persist to DB
- Add battery charge Statistics sensor as primary, raw sensor as fallback
- No changes to existing forecast, dampening or sensor behaviour

**New dependency:** `aiomysql>=0.2.0`

**Prerequisite documentation:** Statistics sensor setup guide in README

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

### Phase 4 — Short-range forecast correction (experimental)

- Implement correction layer in coordinator update loop
- Add `correction_tau` setting to tuning step
- Gate on OWM enabled + Statistics sensors configured
- Expose correction factor per period as forecast sensor attribute

**New dependencies:** none beyond Phase 1

---

## Dependency handling

```python
# Lazy import pattern — feature degrades gracefully if not installed

try:
    import aiomysql
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    _LOGGER.info("aiomysql not installed — database features disabled")

try:
    from scipy.optimize import minimize
    import numpy as np
    TUNING_AVAILABLE = True
except ImportError:
    TUNING_AVAILABLE = False
    _LOGGER.info("scipy/numpy not installed — PV tuning disabled")
```

Users who do not need DB storage or PV tuning are unaffected by the
additional dependencies.

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

---

*End of design document*
