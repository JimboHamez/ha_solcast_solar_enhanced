# Solcast Solar Enhanced

<!--[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)-->
[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)
![GitHub Release](https://img.shields.io/github/v/release/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
[![hacs_downloads](https://img.shields.io/github/downloads/JimboHamez/ha_solcast_solar_enhanced/latest/total?style=for-the-badge)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/latest)
![GitHub License](https://img.shields.io/github/license/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
![GitHub commit activity](https://img.shields.io/github/commit-activity/y/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
![Maintenance](https://img.shields.io/maintenance/yes/2026?style=for-the-badge)

A standalone Home Assistant companion integration for [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) that adds:

1. **MySQL database storage** of PV power averages, forecasts, solar position, weather and battery data
2. **Automatic Rooftop PV Tuning** — daily tilt/azimuth optimisation via scipy (L-BFGS-B)
3. **Adaptive Shading Dampening** — quality-weighted dampening computed purely from your stored actual-vs-forecast history (it never consumes the base integration's own dampening factors), ramping from a neutral no-op toward the measured correction as historical data accumulates
4. **Multi-site support** — multiple Solcast rooftop arrays on one property, auto-discovered from the base integration; per-site storage, tuning and dampening, including DC-ratio apportionment for string inverters (e.g. Fronius) that expose per-MPPT DC
5. **Flexible PV input** — read either an averaged-power sensor (kW) or a cumulative energy counter (Wh/kWh), with auto-detection
6. **Short-range Forecast Correction** — *planned* (design documented below): a transient, cloud-driven nudge to the next 1–6 hours of forecast, orthogonal to dampening — see [Short-range forecast correction](#short-range-forecast-correction-planned)

**Zero additional Solcast API calls.** All forecast data is read from the base integration's coordinator.

---

## 🆕 What's new in v1.2.0

**Adaptive Shading Dampening is now computed purely from your database-collected history.** It no longer reads or blends in the base `solcast_solar` integration's own dampening factors — the correction ramps from a neutral `1.0` toward your measured actual-vs-forecast ratio as confidence grows. See the [release notes](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/tag/v1.2.0) and [CHANGELOG](CHANGELOG.md) for details.

---

## Prerequisites

### 1. Base integration

[BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) must be installed and configured before adding this integration.

### 2. Generation / export sensors

Point the integration at your inverter's sensors directly — **no helper Statistics sensors are required.** Two input styles are supported per sensor, and `Auto-detect` (the default) picks the right one (see [PV sensor input modes](#pv-sensor-input-modes)):

- **Recommended — cumulative energy counter** (`Wh`/`kWh`/`MWh`, `state_class: total_increasing`), e.g. your inverter's lifetime/daily generation total and your grid-export total. The integration derives the period's average kW from the energy delta over each interval. This is robust to polling drift and avoids the race a 30-minute averaging sensor introduces.
- **Power sensor** (`W`/`kW`) — an instantaneous or already-averaged generation/export power reading, used directly.

You map these in the setup wizard (Step 1); battery is optional. For multi-site systems each array is mapped in Step 6.

<details>
<summary>Optional: legacy 30-minute Statistics-sensor approach</summary>

Earlier versions required HA Statistics sensors producing a 30-minute `mean_linear` average in kW. This still works (select sensor type **Power** or leave on Auto-detect), but is **no longer recommended** — a cumulative energy counter is simpler and avoids the window-reset race. If you prefer it:

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

(Repeat for export and, optionally, battery charge.)
</details>

### 3. MySQL database (optional)

If you want historical storage, dampening and PV tuning, a MySQL 8.0+ database is required. The schema is created automatically on first run.

### 4. OpenWeatherMap API key (optional)

A free API key from [openweathermap.org](https://openweathermap.org/api) enables cloud cover data, which significantly improves dampening accuracy by filtering cloudy periods.

---

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS.
2. Install **Solcast Solar Enhanced**.
3. Restart Home Assistant.

### Manual

1. Copy the `custom_components/solcast_solar_enhanced` folder to your HA `config/custom_components/` directory.
2. Restart Home Assistant.

### Python dependencies

Install required packages in your HA Python environment:

```bash
pip install aiomysql>=0.2.0
```

For PV tuning (optional):
```bash
pip install numpy>=1.21.0 scipy>=1.7.0
```

Both dependencies use lazy imports — if not installed, the relevant features are disabled with an informational log message. The integration will still run.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration → Solcast Solar Enhanced**.

The setup wizard has 5 steps (a 6th, **Per-site sensor mapping**, appears automatically only when more than one Solcast site is detected):

### Step 1 — Site & System

| Field | Description |
|---|---|
| Latitude / Longitude | Your site coordinates |
| System capacity (kW DC) | Total panel DC capacity |
| Panel tilt | 0° = flat, 90° = vertical |
| Panel azimuth | 0° = North, 90° = East, −90° = West |
| PV Power / Generation sensor | Averaged-power sensor **or** cumulative energy counter for generation |
| PV sensor type | `Auto-detect` (default), `Power (kW/W)`, or `Energy counter (kWh/Wh/MWh)` |
| PV Export sensor | Averaged-power sensor **or** cumulative export energy counter |
| PV Export sensor type | As above, for the export sensor |
| Battery Charge sensor | Generation/power or energy-counter sensor for battery charge (optional) |

### Step 2 — MySQL Database

| Field | Default | Description |
|---|---|---|
| Enable MySQL | Off | Toggle DB storage on/off |
| Host | localhost | MySQL server hostname |
| Port | 3306 | MySQL port |
| Username / Password | — | Credentials |
| Database name | solcast | Schema name (created automatically) |
| Read-only mode | Off | Read history only, never write |

### Step 3 — OpenWeatherMap

| Field | Description |
|---|---|
| Enable OWM | Toggle weather data on/off |
| OWM API key | Key from openweathermap.org |

### Step 4 — Battery Storage

Raw sensor fallback for systems without a dedicated battery sensor mapped in Step 1:

| Field | Description |
|---|---|
| Enable raw battery fallback | Toggle |
| Mode | `net` (signed power sensor) or `separate` (charge-only sensor) |
| Net battery sensor | Signed power entity (positive = charging) |
| Charge battery sensor | Charge-only power entity |

### Step 5 — PV Tuning & Dampening

| Field | Default | Description |
|---|---|---|
| Auto PV tuning | On | Run tilt/azimuth optimisation daily |
| Auto dampening | On | Recalculate and push dampening every 6 hours |
| Cloud threshold % | 20 | Records below this are treated as clear-sky |
| Max cloud % to include | 60 | Records above this are excluded entirely |
| Clipping threshold | 0.95 | Fraction of capacity at which clipping is assumed |
| Grid export limit (kW) | 0 | Exclude records where export is at or near this ceiling; 0 = disabled. If the base integration has a `site_export_limit` set, it is used automatically and this field is the fallback |

### Step 6 — Per-site sensor mapping (multi-site only)

Shown automatically when more than one Solcast site is detected. Sites are auto-discovered from the base integration's rooftop sensors (orientation and capacity are read from Solcast, so per-site tuning is seeded automatically). For each site you provide:

| Field | Description |
|---|---|
| `<site>` — generation sensor | The sensor that measures this array's output. Several arrays may share one inverter AC sensor |
| `<site>` — DC/MPPT sensor (optional) | The per-string DC sensor, when arrays share an AC output and the inverter exposes per-MPPT DC (e.g. Fronius) |
| `<site>` — sensor type | Auto-detect / power / energy counter |

How the mapping is interpreted:

- **One array → its own sensor** (e.g. Enphase per-array AC): tuned and dampened individually.
- **Several arrays → one AC sensor + per-MPPT DC**: the measured AC is split between arrays by each string's share of DC (`ac × dcᵢ / Σ dc`), giving per-array generation in the AC domain. Each array is then tuned/dampened individually.
- **Several arrays → one AC sensor, no DC**: cannot be separated, so those sites are left unmapped (per-array output isn't observable).

Leave a site blank to skip it. With no mapping (or a single site), the integration behaves exactly as a single-site install.

---

## How it works

### PV sensor input modes

Each PV sensor (generation and export) can be read in one of two ways, chosen per sensor (default `Auto-detect`):

- **Power** (`kW`/`W`) — the instantaneous/averaged reading is used directly (converted to kW). This is the classic Statistics-sensor path.
- **Energy counter** (`kWh`/`Wh`/`MWh`, `state_class: total_increasing`) — the average power for the interval is derived from the energy delta over the *actual* elapsed time: `avg_kW = ΔkWh / hours`. Using the real elapsed time (not a hard-coded 30 min) makes it robust to polling drift. Counter resets/rollovers (negative delta), the first reading after a restart, and abnormally long gaps are detected and excluded. Baselines are persisted across restarts.

`Auto-detect` inspects the sensor's `state_class` and `unit_of_measurement` to choose. Energy-counter mode avoids the race where an external 30-minute averaging sensor can be cleared before the integration reads it.

### Energy balance

```
total_pv = pv_actual
```

`pv_actual` is the inverter's total AC output — it already includes the self-consumption, grid export, and battery charging portions. `pv_export` and `battery_charge` are recorded in the DB for diagnostics but are not added to `total_pv`. Whether read from an energy counter (average kW over the interval) or a power sensor, `pv_actual` is in the same unit as Solcast's `pv_estimate` (average kW over the period), so the two are directly comparable.

### Adaptive dampening

Dampening is computed at 48 half-hour slots per day. For each slot:

1. Historical records within ±14 calendar days (across all years) are fetched from the DB
2. Each record is weighted by **cloud quality** (three-band: 1.0 / 0.6 / 0.3) and **geometric proximity** (Gaussian on zenith and azimuth distance)
3. The quality-weighted average `total_pv / pv_estimate` ratio becomes the DB-derived dampening factor
4. A **confidence blend** mixes this with a neutral `1.0` anchor (the base integration's own dampening factors are **never** read into the calculation):

```
final = (1 − α) × 1.0 + α × db_factor
```

α grows as more quality-weighted records accumulate, so with little data the factor sits near a no-op `1.0` and ramps toward the DB-measured ratio as confidence builds:

| Quality-weighted records | α (20% threshold) |
|---|---|
| 0 | 0.00 |
| 30 | 0.50 |
| 60 | 0.80 |
| 100 | 0.92 |

When α < 0.5, the result is clamped to ±15% of `1.0` (i.e. 0.85–1.15) to prevent early instability. A slot with no usable DB data stays at a neutral `1.0`.

Adjacent half-hour slot pairs are averaged into 24 hourly values and pushed to the base integration via the `solcast_solar.set_dampening` service (`damp_factor` as a comma-separated string). In multi-site mode a dampening set is pushed **per site** (`set_dampening` with the site's `resource_id`), which overrides the base's global dampening for that site.

> **Important:** the base integration's own **automatic dampening** must be **disabled** (Solcast PV Forecast → Configure). While it is on, the base rejects all manual `set_dampening` calls, so this integration cannot apply its factors — it detects this, skips the push, and logs a one-time warning.

**Convergence time by climate:**

| Climate | Threshold | Time to full confidence |
|---|---|---|
| Clear (Perth, inland QLD) | 20% | 4–6 weeks |
| Mixed (Melbourne, Sydney) | 20–25% | 8–12 weeks |
| Overcast (Hobart, coastal) | 30–35% | 6–10 weeks |

### Short-range forecast correction (planned)

> **Status: planned, not yet implemented.** This section documents the intended design; no correction is applied today.

**Purpose:** adjust the next 1–6 hours of forecast from the live `pv_actual` / `pv_export` readings and current OWM cloud cover, correcting for the satellite-image-processing lag in Solcast's near-term predictions. This is **transient (cloud-driven)** correction, kept orthogonal to dampening, which handles **structural (geometric shading)** correction.

**Activation conditions** (all must hold, to avoid double-correcting what dampening already handles):

- OWM is enabled — cloud data is required to tell cloud attenuation apart from shading
- `pv_actual` and `pv_export` sensors are configured
- ≥2 consecutive recent periods deviate from the estimate in the **same direction** (a single outlier must not trigger a correction)
- `clouds > cloud_threshold` — clear-sky deviations are geometric shading and are already handled by dampening

**Correction formula** — an exponentially-decaying nudge applied per future period:

```
recent_ratio   = mean(total_pv / pv_estimate) over the last 2–3 periods
correction(n)  = 1.0 + (recent_ratio − 1.0) × exp(−n / τ)
```

where `n` = periods ahead (integer) and `τ` = time constant (default 3 periods = 90 min, configurable). The nudge decays toward 1.0 as the forecast horizon lengthens:

| Period ahead | Correction retained |
|---|---|
| +1 (30 min) | 72% |
| +3 (90 min) | 37% |
| +6 (3 hours) | 14% |
| +12 (6 hours) | 2% — effectively zero |

**Stacking with dampening** (the two effects are orthogonal and multiply):

```
final_forecast(period) = solcast_estimate(period)
                       × dampening_factor(hour)        ← structural shading
                       × short_range_correction(period) ← transient cloud
```

Because `pv_actual` is a period-average (kW averaged over each ~30-minute interval — whether from a power sensor or an energy-counter delta), `recent_ratio` is already a stable period-average signal rather than a noisy instantaneous reading, which reduces false-positive corrections.

**Planned configuration:** `correction_tau` (default 3 periods, range 1–12), to be added to the PV Tuning step of the setup wizard.

### PV tuning

Uses `scipy.optimize.minimize` (L-BFGS-B) to find the panel tilt and azimuth that minimise RMSE between measured `total_pv` and the geometrically-scaled Solcast estimate. Runs daily in a thread executor. Requires ≥10 clear-sky, non-clipped records.

Records are excluded from the tuning dataset if:
- Cloud cover ≥ cloud threshold (cloudy periods distort the geometry signal)
- Both `total_pv` and `pv_estimate` exceed the clipping threshold (inverter AC clipping)
- `pv_export` is at or near the configured grid export limit (curtailed output would pull the optimiser toward a lower tilt/azimuth than reality)

In **multi-site** mode each individually-measured site is tuned separately against its own rows, seeded from that array's Solcast tilt/azimuth. The property-wide export limit still applies to every site's exclusion (one export meter for the whole property). Per-site results appear as a `per_site` attribute on the **Tuned Panel Tilt** sensor.

### Multi-site

When the base integration has more than one rooftop site, the enhanced integration discovers them automatically and stores one row per site (keyed by Solcast `resource_id`) alongside the property-wide aggregate (`_total`). Aggregate tuning/dampening continue to use the `_total` rows, so single-site behaviour is unchanged; per-site tuning and dampening are layered on top. See [Step 6](#step-6--per-site-sensor-mapping-multi-site-only) for how generation is mapped to sites.

---

## Sensors (14 total)

| Sensor | Unit | Description |
|---|---|---|
| Forecast Now | kW | Current 30-min PV forecast (from base integration) |
| Forecast Today | kWh | Total forecast for today (from base integration) |
| Tuned Panel Tilt | ° | Optimised tilt from PV tuning |
| Tuned Panel Azimuth | ° | Optimised azimuth from PV tuning |
| Tuning RMSE | kW | Goodness of fit for tuned geometry |
| Tuning Export Limited Excluded | — | Records dropped from last tuning run due to export limit filter |
| Database Records | — | Total records in the DB |
| Dampening Hours with DB Data | — | Hours where DB-derived factors are active |
| Weather Temperature | °C | OWM current temperature |
| Cloud Cover | % | OWM cloud cover |
| Battery Charge 30min Average | kW | Value read from the configured battery sensor |
| PV Power 30min Average | kW | Average generation for the period from the configured sensor |
| PV Export 30min Average | kW | Average export for the period from the configured sensor |
| Base Integration Status | — | `connected` or `not_detected` |

The **Dampening Hours with DB Data** sensor exposes per-hour diagnostics as attributes:

```yaml
hour_14_factor:           0.847    # final blended value pushed to base integration
hour_14_alpha:            0.72     # DB confidence (0 = pure base, 1 = pure DB)
hour_14_source:           blended  # db_history | blended | base_fallback | night
hour_14_quality_records:  31.4     # quality-weighted record count
hour_14_avg_quality:      0.81     # mean combined weight of contributing records
overall_source:           blended
```

In multi-site mode the **Tuned Panel Tilt** sensor additionally carries a `per_site` attribute — a list of `{name, resource_id, tilt, azimuth, rmse_kw, n_records}` for each individually-tuned array.

---

## Services

| Service | Description |
|---|---|
| `solcast_solar_enhanced.run_pv_tuning` | Force immediate PV tuning |
| `solcast_solar_enhanced.run_dampening_update` | Force immediate dampening recalculation and push |
| `solcast_solar_enhanced.fetch_weather` | Force immediate OWM weather fetch |

---

## Database schema

```sql
CREATE TABLE solcast_data (
  `index`          INT AUTO_INCREMENT PRIMARY KEY,
  period_end       TEXT NOT NULL,
  period_end_epoch BIGINT NOT NULL,
  period_start     TEXT NOT NULL,
  site             VARCHAR(64) NOT NULL DEFAULT '_total',  -- Solcast resource_id, or '_total' aggregate
  pv_actual        DECIMAL(10,4) NOT NULL,        -- 30-min avg generation (kW)
  pv_export        DECIMAL(10,4) NOT NULL,        -- 30-min avg export (kW)
  pv_estimate      DECIMAL(10,4) NOT NULL,        -- Solcast p50 estimate
  pv_estimate10    DECIMAL(10,4) NOT NULL,        -- Solcast p10
  pv_estimate90    DECIMAL(10,4) NOT NULL,        -- Solcast p90
  azimuth          DECIMAL(10,5) NOT NULL,        -- solar azimuth at period end (°)
  zenith           DECIMAL(10,5) NOT NULL,        -- solar zenith at period end (°)
  temp             DECIMAL(10,2) NOT NULL,        -- OWM temperature (°C)
  clouds           INT NOT NULL,                  -- OWM cloud cover (0–100)
  description      TEXT NOT NULL,                 -- OWM weather description
  battery_charge   DECIMAL(10,4) NOT NULL,        -- 30-min avg battery charge (kW)
  UNIQUE KEY uq_epoch_site (period_end_epoch, site)
);
```

The schema is created automatically on first run. On subsequent startups the integration checks `information_schema.TABLES` before issuing `CREATE TABLE`, so switching from read-only to read-write mode on an existing database does not require `CREATE` privilege. Columns added in later versions are migrated with idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements.

When upgrading from a single-site schema, the `site` column is added (back-filling existing rows to `_total`) and the unique key is migrated from `(period_end_epoch)` to `(period_end_epoch, site)` — checked against `information_schema` so it runs at most once and is safe to re-run.

---

## Sensor mapping guidance

```
total_pv = pv_actual   (inverter AC output — includes all loads, export, and battery)
```

`pv_export` and `battery_charge` sensors are recorded in the DB for reference and diagnostics but are not used in the `total_pv` calculation. Configure `pv_actual` to read from the inverter's generation meter (total AC output), not a self-consumption-only meter.

---

## Standalone tuning tool

`tools/standalone_tuning.py` runs the **same** tilt/azimuth optimisation outside Home Assistant, against the MySQL history or a CSV export — handy for experimenting with parameters or validating a site without waiting for the daily run. It imports the integration's tuning functions, so results match the running integration.

```bash
# Whole-property tuning from MySQL
python tools/standalone_tuning.py --db solcast --user solcast --password secret --capacity 6.6

# One site, seeded with that array's orientation
python tools/standalone_tuning.py --db solcast --user solcast --password secret \
    --site b68d-c05a --capacity 5 --tilt 30 --azimuth 67.5

# Every site in the table
python tools/standalone_tuning.py --db solcast --user solcast --password secret --all-sites

# No database — tune a CSV with the same columns
python tools/standalone_tuning.py --csv history.csv --capacity 5
```

Requires `numpy` + `scipy`, and for DB mode one of `pymysql` or `mysql-connector-python` (CSV mode needs neither). Run `--help` for all options.

---

## Compatibility

| Component | Version |
|---|---|
| Home Assistant | 2026.5.4+ |
| Python | 3.12+ |
| MySQL | 8.0+ |
| aiomysql | 0.2.0+ |
| scipy / numpy | Optional — 1.7.0+ / 1.21.0+ |

---

## License

MIT — see [LICENSE](LICENSE).
