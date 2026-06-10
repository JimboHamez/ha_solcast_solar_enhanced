# Solcast Solar Enhanced

<!--[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)-->
[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)
![GitHub Release](https://img.shields.io/github/v/release/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
[![hacs_downloads](https://img.shields.io/github/downloads/JimboHamez/ha_solcast_solar_enhanced/latest/total?style=for-the-badge)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/latest)
![GitHub License](https://img.shields.io/github/license/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
![GitHub commit activity](https://img.shields.io/github/commit-activity/y/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
![Maintenance](https://img.shields.io/maintenance/yes/2026?style=for-the-badge)

[![Tests](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/test.yml/badge.svg)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/test.yml)
[![Validate](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/validate.yml/badge.svg)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/validate.yml)

A companion to [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) that learns from your own generation history to make your Solcast forecasts more accurate — automatically, and entirely on your device.

It adds:

- **History storage** — keeps your PV, forecast, weather and battery data in a built-in SQLite file. No server, no setup.
- **Automatic panel tuning** — works out your real panel tilt and azimuth from generation data and corrects the forecast geometry.
- **Adaptive dampening** — learns where your forecast runs high or low (shading, local conditions) and pushes a correction back to Solcast. Starts neutral and gets stronger as it gathers data.
- **Multi-site** — handles multiple rooftop arrays on one property, discovered automatically.
- **Flexible inputs** — reads energy counters (recommended) or power sensors, with auto-detection.
- **Curtailment-aware** — knows when your inverter is export-limited so curtailed output isn't mistaken for shading.

**No extra Solcast API calls** — it reads forecast data straight from the base integration.

---

## Why this exists

Solcast [discontinued PV Tuning for free accounts](https://kb.solcast.com.au/pv-tuning-discontinued), so home users can no longer feed their real generation back to Solcast to sharpen forecasts.

This integration brings that back, on your own hardware. It records your actual-vs-forecast history locally and computes its own tuning and dampening — and because it also folds in local cloud cover, per-array geometry and export-limit handling, the result can be *better* than the old service, not just a replacement.

---

## 🆕 What's new in v1.6.9

A new **MPPT DC Voltage** diagnostic sensor lets you confirm that per-string DC telemetry capture (added in v1.6.8) is actually wired up and reading — it shows your highest string voltage with per-tracker detail in the attributes, and stays *unavailable* until you point it at per-string sensors.

Full history in the [CHANGELOG](CHANGELOG.md) · [release notes](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/tag/v1.6.9).

---

## Prerequisites

### 1. Base integration

[BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) must be installed and configured first. It's a hard dependency — Home Assistant won't set this up without it. You can only add this integration **once** (one property, one database).

### 2. Generation / export sensors

Point the integration at your inverter's sensors. Two kinds work:

- **Best — an energy counter** (`Wh`/`kWh`/`MWh`, e.g. your lifetime or daily generation total, and your grid-export total). The integration works out average power from how much the counter moved over each interval. Exact, and no helper needed.
- **Fallback — a rolling power helper** (`W`/`kW`). If you can't expose an energy counter, wrap your power sensor in a `mean_linear` statistics helper (below).

> ⚠️ **Don't use a raw instantaneous power sensor.** A single spot reading isn't the half-hour average and will skew the results. Use an energy counter, or the helper below.

You map these in the setup wizard (Step 1). Battery is optional; multi-site arrays are mapped in Step 6.

<details>
<summary>Rolling mean_linear power helper (only if you have no energy counter)</summary>

A continuous sliding-window sensor that never resets at the half-hour mark:

```yaml
sensor:
  - platform: statistics
    name: "PV Power 30min Rolling Mean"
    entity_id: sensor.YOUR_INVERTER_AC_POWER_SENSOR
    state_characteristic: mean_linear   # time-weighted mean (not plain "mean")
    max_age:
      minutes: 30
    sampling_size: 1800                  # raise it so samples aren't dropped
```

(Repeat for export and per-MPPT DC as needed.)
</details>

### 3. History storage

Powers dampening and tuning, and needs nothing — a built-in SQLite file (`config/solcast_solar_enhanced.db`) is created automatically. On by default.

### 4. OpenWeatherMap API key (required for tuning & dampening)

> **Without OpenWeatherMap, tuning and dampening stay inactive.** They only learn from *clear-sky* periods (the cloudy ones tell you nothing about your panels), and the cloud-cover reading that finds those periods comes only from OWM. History is still recorded, but with no cloud data every record is treated as overcast and skipped. A repair issue prompts you until a key is added.

It's free and easy:

| What | Detail |
|---|---|
| Account | Free at [openweathermap.org](https://openweathermap.org/api) |
| API key | Created under **API keys**. New keys can take up to ~2 hours to activate |
| Plan | The free **Current Weather Data** API — no paid plan |
| Usage | One call per 30-min cycle (~48/day), far under the free limit |
| Enable | Off by default — turn it on in setup **Step 3** and paste the key |

**Check it's working** after setup: the **Cloud Cover** sensor should show a real percentage, the log should be free of `OWM fetch failed`, and the repair issue should be gone.

---

## Installation

![Solcast Solar Enhanced sensors in Home Assistant](images/dashboard.png)

### HACS (recommended)

1. Add this repository as a custom repository in HACS.
2. Install **Solcast Solar Enhanced**.
3. Restart Home Assistant.

### Manual

1. Copy `custom_components/solcast_solar_enhanced` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

Storage uses the Python standard library, so there's nothing to install. PV tuning uses **numpy**, which Home Assistant already ships (and which runs on a Raspberry Pi) — so a normal HA install needs nothing extra.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration → Solcast Solar Enhanced**.

The wizard has 5 steps (a 6th, **Per-site sensor mapping**, appears only when more than one Solcast site is detected).

### Step 1 — Site & System

| Field | Description |
|---|---|
| Latitude / Longitude | Your site coordinates |
| System capacity (kW DC) | Total panel DC capacity |
| Panel tilt | 0° = flat, 90° = vertical |
| Panel azimuth | Solcast convention — 0° = North, **positive = West**, **negative = East**. E.g. +6 = 6° West of North |
| PV Generation sensor | Energy counter (recommended) or a rolling power helper |
| PV sensor type | `Auto-detect` (default), `Energy counter`, or `Averaged power` |
| PV Export sensor | Export energy counter (recommended) or a rolling helper |
| PV Export sensor type | As above, for export |
| Battery Charge sensor | Battery charge sensor (optional) |
| MPPT 1/2 DC voltage + current | Optional — your inverter's per-string voltage/current sensors, for curtailment-detection capture. Leave MPPT 2 blank for single-tracker inverters |

### Step 2 — Storage

| Field | Default | Description |
|---|---|---|
| Enable history storage | On | Toggle the built-in store on/off |
| Keep history for (days) | 0 | `0` keeps everything. A positive value prunes older rows daily to save space. Seasonal dampening works best with ≥ ~400 days |

The store lives at `config/solcast_solar_enhanced.db`. To browse it, point the [sqlite-web add-on](https://github.com/hassio-addons/addon-sqlite-web) at that path.

### Step 3 — OpenWeatherMap

Required for tuning & dampening (see [§4 above](#4-openweathermap-api-key-required-for-tuning--dampening)). Off by default.

| Field | Default | Description |
|---|---|---|
| Enable OWM | **Off** | Turn on to fetch cloud cover |
| OWM API key | — | Free key from openweathermap.org |

### Step 4 — Battery Storage

A fallback for systems without a battery sensor mapped in Step 1.

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
| Cloud threshold % | 20 | Records below this count as clear-sky |
| Max cloud % to include | 60 | Records above this are excluded |
| Clipping threshold | 0.95 | Fraction of capacity at which clipping is assumed |
| Grid export limit (kW) | 0 | Exclude records pegged at this ceiling; 0 = disabled. Read automatically from the base integration if set |

### Step 6 — Per-site sensor mapping (multi-site only)

Shown when more than one Solcast site is detected. Sites are auto-discovered from the base integration (orientation and capacity come from Solcast). For each site you map its generation sensor, and optionally its per-string DC sensors. See [Multi-site](#multi-site) for how shared inverters are split between arrays.

> **Heads up:** the base integration's own **automatic dampening** must be **disabled** (Solcast PV Forecast → Configure). While it's on, the base rejects manual dampening, so this integration can't apply its factors — it detects this, skips the push, and logs a warning.

---

## How it works

- **PV tuning** runs daily: it searches for the panel tilt and azimuth that best explain your clear-sky generation, and reports them on the **Tuned Panel Tilt/Azimuth** sensors. Needs at least ~10 clear-sky, non-clipped records.
- **Adaptive dampening** compares your actual output to the forecast across a ±14-day seasonal window, weighting each record by how clear the sky was and how close the sun was to the same position. It starts at a neutral no-op and ramps toward the measured correction as data builds, then pushes 24 hourly factors to Solcast via `set_dampening`. The base integration's own dampening factors are never read into this — the correction is learned purely from your history.
- **Curtailment** — when your inverter is export-limited, that capped output is detected and handled so it doesn't look like shading: tuning excludes it, and dampening clips it to the achievable ceiling so a curtailed clear day stays neutral.

Full detail — the confidence model, the weighting maths, convergence timelines by climate, and design decisions — lives in the [design document](DESIGN_DOCUMENT.md).

### Multi-site

When the base integration has more than one rooftop array, each is stored, tuned and dampened separately (keyed by its Solcast `resource_id`) alongside the property-wide aggregate. Single-site behaviour is unchanged.

If several arrays share one AC sensor, the integration splits the measured AC between them using each string's share of DC current (`ac × dcᵢ / Σ dc`), so each array can still be tuned individually. Arrays sharing one AC sensor with no DC sensors can't be separated and are left unmapped.

---

## Sensors

| Sensor | Unit | Description |
|---|---|---|
| Forecast Now | kW | Current 30-min PV forecast (from base integration) |
| Forecast Today | kWh | Total forecast for today (from base integration) |
| Tuned Panel Tilt | ° | Optimised tilt from PV tuning (carries a `per_site` attribute in multi-site mode) |
| Tuned Panel Azimuth | ° | Optimised azimuth from PV tuning |
| Tuning RMSE | kW | Goodness of fit for the tuned geometry |
| Tuning Export Limited Excluded | — | Records dropped from the last tuning run by the export-limit filter |
| Database Records | — | Total records in the store |
| MPPT DC Voltage (max) | V | Diagnostic — highest captured string voltage this cycle (per-tracker detail in attributes). Unavailable until per-string DC sensors are configured |
| Dampening Hours with DB Data | — | Hours where DB-derived factors are active (per-hour diagnostics in attributes) |
| Weather Temperature | °C | OWM current temperature |
| Cloud Cover | % | OWM cloud cover |
| Battery Charge 30min Average | kW | From the configured battery sensor (restored across restarts) |
| PV Power 30min Average | kW | Average generation for the period (restored across restarts) |
| PV Export 30min Average | kW | Average export for the period (restored across restarts) |
| Base Integration Status | — | `connected` or `not_detected` |

---

## Services

| Service | Description |
|---|---|
| `solcast_solar_enhanced.run_pv_tuning` | Force immediate PV tuning |
| `solcast_solar_enhanced.run_dampening_update` | Force immediate dampening recalculation and push |
| `solcast_solar_enhanced.fetch_weather` | Force immediate OWM weather fetch |

---

## Standalone tuning tool

`tools/standalone_tuning.py` runs the same tilt/azimuth optimisation outside Home Assistant, against the SQLite store or a CSV export — handy for experimenting without waiting for the daily run.

```bash
# Whole-property tuning from the built-in store
python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db --capacity 6.6

# One site, seeded with that array's orientation
python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db \
    --site b68d-c05a --capacity 5 --tilt 30 --azimuth 67.5

# Every site in the table
python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db --all-sites
```

Requires `numpy`. Run `--help` for all options.

---

## Roadmap

- **Curtailment detector (DC-telemetry).** Tells real curtailment apart from shading on the DC side. Phase 1 (dampening clip-forecast) and Phase 2 (per-string DC capture + diagnostic sensor) are done; a self-calibrating per-string voltage model is next as telemetry accumulates.
- **Emergency-backstop and variable export limits** — recognising market-operator and dynamic DNSP curtailment so those intervals aren't mistaken for shading.

See the [design document](DESIGN_DOCUMENT.md#roadmap) for the full plan and the database schema.

---

## Compatibility

| Component | Version |
|---|---|
| Home Assistant | 2026.5.4+ |
| Python | 3.12+ |
| Storage | stdlib `sqlite3` — no install |
| numpy | PV tuning — 1.21.0+ (ships with Home Assistant) |

---

## License

Apache-2.0 — see [LICENSE](LICENSE).
