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
3. **Adaptive Shading Dampening** — quality-weighted dampening that blends with and progressively replaces the base integration's manual dampening as historical data accumulates
4. **Short-range Forecast Correction** — planned, not yet implemented

**Zero additional Solcast API calls.** All forecast data is read from the base integration's coordinator.

---

## Prerequisites

### 1. Base integration

[BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) must be installed and configured before adding this integration.

### 2. Statistics sensors

Three HA Statistics sensors must be created before configuring this integration. Go to **Settings → Devices & Services → Add Integration → Statistics**, or add via YAML:

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

**Battery Charge (optional — only if you have battery storage):**
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

> Source sensors must report **power in kW** (not energy in kWh). The `mean_linear` characteristic computes a true time-weighted average over the 30-minute window, directly comparable to Solcast's period estimates.

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

The setup wizard has 5 steps:

### Step 1 — Site & System

| Field | Description |
|---|---|
| Latitude / Longitude | Your site coordinates |
| System capacity (kW DC) | Total panel DC capacity |
| Panel tilt | 0° = flat, 90° = vertical |
| Panel azimuth | 0° = North, 90° = East, −90° = West |
| PV Power 30min Average sensor | Statistics sensor for generation |
| PV Export 30min Average sensor | Statistics sensor for grid export |
| Battery Charge 30min Average sensor | Statistics sensor for battery charge (optional) |

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

Raw sensor fallback for sites without a Battery Statistics sensor:

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

---

## How it works

### Energy balance

```
total_pv = pv_actual + pv_export + battery_charge
```

All three values are 30-minute linear averages. Their sum equals total panel generation (minus conversion losses) and is directly comparable to Solcast's `pv_estimate`.

### Adaptive dampening

Dampening is computed at 48 half-hour slots per day. For each slot:

1. Historical records within ±14 calendar days (across all years) are fetched from the DB
2. Each record is weighted by **cloud quality** (three-band: 1.0 / 0.6 / 0.3) and **geometric proximity** (Gaussian on zenith and azimuth distance)
3. The quality-weighted average `total_pv / pv_estimate` ratio becomes the DB-derived dampening factor
4. A **confidence blend** mixes this with the base integration's existing factor:

```
final = (1 − α) × base_factor + α × db_factor
```

α grows as more quality-weighted records accumulate:

| Quality-weighted records | α (20% threshold) |
|---|---|
| 0 | 0.00 |
| 30 | 0.50 |
| 60 | 0.80 |
| 100 | 0.92 |

When α < 0.5, the result is clamped to ±15% of the base factor to prevent early instability.

Adjacent half-hour slot pairs are averaged into 24 hourly values and pushed to the base integration via `solcast_solar.set_dampening_factor`.

**Convergence time by climate:**

| Climate | Threshold | Time to full confidence |
|---|---|---|
| Clear (Perth, inland QLD) | 20% | 4–6 weeks |
| Mixed (Melbourne, Sydney) | 20–25% | 8–12 weeks |
| Overcast (Hobart, coastal) | 30–35% | 6–10 weeks |

### PV tuning

Uses `scipy.optimize.minimize` (L-BFGS-B) to find the panel tilt and azimuth that minimise RMSE between measured `total_pv` and the geometrically-scaled Solcast estimate. Runs daily in a thread executor. Requires ≥10 clear-sky, non-clipped records.

---

## Sensors (13 total)

| Sensor | Unit | Description |
|---|---|---|
| Forecast Now | kW | Current 30-min PV forecast (from base integration) |
| Forecast Today | kWh | Total forecast for today (from base integration) |
| Tuned Panel Tilt | ° | Optimised tilt from PV tuning |
| Tuned Panel Azimuth | ° | Optimised azimuth from PV tuning |
| Tuning RMSE | kW | Goodness of fit for tuned geometry |
| Database Records | — | Total records in the DB |
| Dampening Hours with DB Data | — | Hours where DB-derived factors are active |
| Weather Temperature | °C | OWM current temperature |
| Cloud Cover | % | OWM cloud cover |
| Battery Charge 30min Average | kW | Value read from Statistics sensor |
| PV Power 30min Average | kW | Value read from Statistics sensor |
| PV Export 30min Average | kW | Value read from Statistics sensor |
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
  UNIQUE KEY uq_epoch (period_end_epoch)
);
```

The schema is created automatically. Existing databases are migrated with idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements on every startup.

---

## Sensor mapping guidance

```
Total PV Output = pv_actual + pv_export + battery_charge
```

| Scenario | pv_actual source | pv_export source |
|---|---|---|
| Generation meter (total inverter AC output) | Generation meter | Grid export meter |
| Self-consumption meter only | Self-consumption meter | Grid export meter |

The sum must equal total panel generation (minus conversion losses) for dampening ratios to be meaningful.

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
