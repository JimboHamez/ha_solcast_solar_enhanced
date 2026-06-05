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

A standalone Home Assistant companion integration for [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) that adds:

1. **Built-in history storage** of PV power averages, forecasts, solar position, weather and battery data — a zero-config SQLite file (no server, no credentials, no dependency)
2. **Automatic Rooftop PV Tuning** — daily tilt/azimuth optimisation via scipy (L-BFGS-B)
3. **Adaptive Shading Dampening** — quality-weighted dampening computed purely from your stored actual-vs-forecast history (it never consumes the base integration's own dampening factors), ramping from a neutral no-op toward the measured correction as historical data accumulates
4. **Multi-site support** — multiple Solcast rooftop arrays on one property, auto-discovered from the base integration; per-site storage, tuning and dampening, including DC-ratio apportionment for string inverters (e.g. Fronius) that expose per-MPPT DC
5. **Energy-counter PV input** — reads cumulative energy counters (Wh/kWh/MWh) as the recommended input, deriving average kW from the energy delta over each interval (race-free); a rolling `mean_linear` power helper is supported as a fallback, with unit-first auto-detection

**Zero additional Solcast API calls.** All forecast data is read from the base integration's coordinator.

---

## 🆕 What's new in v1.6.2

**Clear-sky records no longer lost to PV tuning.** A 0% cloud reading — the clearest sky, exactly the data the tilt/azimuth optimiser most wants — was being silently discarded by the cloud-cover filter (a falsy `0` was coerced to fully overcast). Those records are now kept, while a genuinely missing cloud value still defaults to overcast. See the [release notes](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/tag/v1.6.2) and [CHANGELOG](CHANGELOG.md).

_Previously, in v1.6.1:_ the **PV Power**, **PV Export** and **Battery Charge** 30-min average sensors gained **restart resilience** (HA `RestoreSensor`), restoring their last value on startup instead of reading *unknown* until the first half-hour update cycle.

_And in v1.6.0:_ a solar-azimuth **east↔west flip** fix (with in-place repair of existing databases), forecast columns **no longer silently zero-filled**, low-power **performance** work (vectorised tuning, fewer dampening scans, shared HTTP session), the base integration made a **hard dependency**, **single-instance** enforcement, the **OWM API key redacted from logs**, and licensing standardised on **Apache-2.0**.

_Previously, in v1.5.0:_ **zero-config storage** — history moved to a **built-in SQLite store** (a single file, `config/solcast_solar_enhanced.db`, stdlib `sqlite3` — no server, no credentials, no extra dependency), enabled out of the box; **MySQL support was removed** (the storage step is now just an *Enable history storage* toggle).

> **Upgrading from a MySQL setup (pre-1.5.0)?** The built-in store starts fresh and rebuilds as data accumulates. To carry forward old history, export your MySQL `solcast_data` table to CSV before upgrading.

---

## Prerequisites

### 1. Base integration

[BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) must be installed and configured before adding this integration. It is a **hard dependency** — Home Assistant will refuse to set up Solcast Solar Enhanced if the base integration is absent.

> **Single instance.** This integration can only be added **once** — there is one base integration, one property and one shared database, so a second attempt to add it is rejected.

### 2. Generation / export sensors

Point the integration at your inverter's sensors directly. `Auto-detect` (the default) picks the read mode from the sensor's **unit** (see [PV sensor input modes](#pv-sensor-input-modes)):

- **Best practice — cumulative energy counter** (`Wh`/`kWh`/`MWh`, ideally `state_class: total_increasing`), e.g. your inverter's lifetime/daily generation total and your grid-export total. The integration derives the period's average kW from the **energy delta over the actual elapsed interval**. This is exact, needs no helper, and is immune to the `:00`/`:30` reset race that a boundary-windowed averaging sensor introduces.
- **Fallback — rolling `mean_linear` power helper** (`W`/`kW`). If you can't expose an energy counter, feed a **continuous sliding-window** `mean_linear` statistics helper (below). The same applies to per-MPPT **DC** sensors when tracking multiple arrays facing different directions (Step 6), where the value is only used as a ratio.

> ⚠️ **Don't point this at a raw, instantaneous power sensor.** A single spot reading at the poll instant is not the half-hour average and will bias dampening and tuning. Use an energy counter, or wrap the power sensor in the rolling helper below.

You map these in the setup wizard (Step 1); battery is optional. For multi-site systems each array is mapped in Step 6.

<details>
<summary>Rolling mean_linear power helper (only if you have no energy counter)</summary>

A **continuous sliding-window** statistics sensor — it recomputes on every source update and never resets at the half-hour boundary, so it has no reset race:

```yaml
sensor:
  - platform: statistics
    name: "PV Power 30min Rolling Mean"
    entity_id: sensor.YOUR_INVERTER_AC_POWER_SENSOR
    state_characteristic: mean_linear   # time-weighted mean (not plain "mean")
    max_age:
      minutes: 30
    sampling_size: 1800                  # default is tiny — raise it so samples aren't dropped
```

(Repeat for export and per-MPPT DC as needed.) Accuracy depends on how often the source sensor updates. The old **boundary-resetting** 30-minute Statistics approach is no longer recommended — it can be read mid-reset at the `:00`/`:30` border.
</details>

### 3. History storage

Historical storage powers dampening and PV tuning, and **needs nothing** — the integration creates a built-in SQLite file (`config/solcast_solar_enhanced.db`) with no server, credentials or extra dependency. It is enabled by default.

### 4. OpenWeatherMap API key (optional)

A free API key from [openweathermap.org](https://openweathermap.org/api) enables cloud cover data, which significantly improves dampening accuracy by filtering cloudy periods.

---

## Installation

![Solcast Solar Enhanced sensors in Home Assistant](images/dashboard.png)

### HACS (recommended)

1. Add this repository as a custom repository in HACS.
2. Install **Solcast Solar Enhanced**.
3. Restart Home Assistant.

### Manual

1. Copy the `custom_components/solcast_solar_enhanced` folder to your HA `config/custom_components/` directory.
2. Restart Home Assistant.

### Python dependencies

Storage uses the Python standard library — **nothing to install**.

PV tuning is the only optional extra:

```bash
pip install numpy>=1.21.0 scipy>=1.7.0  # only for PV tuning
```

It uses a lazy import — if not installed, tuning is disabled with an informational log message and the integration still runs.

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
| PV Generation sensor | Cumulative energy counter (Wh/kWh/MWh) — recommended; or a rolling `mean_linear` power helper (kW) |
| PV sensor type | `Auto-detect` (default, by unit), `Energy counter (kWh/Wh/MWh)`, or `Averaged power (kW/W)` |
| PV Export sensor | Cumulative export energy counter (Wh/kWh) — recommended; or a rolling `mean_linear` helper |
| PV Export sensor type | As above, for the export sensor |
| Battery Charge sensor | Generation/power or energy-counter sensor for battery charge (optional) |

### Step 2 — Storage

| Field | Default | Description |
|---|---|---|
| Enable history storage | On | Toggle the built-in store on/off |

The store lives at `config/solcast_solar_enhanced.db` and needs no further configuration. To browse it, point the [sqlite-web add-on](https://github.com/hassio-addons/addon-sqlite-web) at that path (it uses WAL mode, so leave the `-wal`/`-shm` sidecar files in place).

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

Each PV sensor (generation and export) is read in one of two ways, chosen per sensor (default `Auto-detect`):

- **Energy counter** (`kWh`/`Wh`/`MWh`) — **recommended.** The average power for the interval is derived from the energy delta over the *actual* elapsed time: `avg_kW = ΔkWh / hours`. Using the real elapsed time (not a hard-coded 30 min) makes it robust to polling drift, and it never depends on a value being correct at the `:00`/`:30` boundary. Counter resets/rollovers (negative delta), the first reading after a restart, and abnormally long gaps are detected and excluded. Baselines are persisted across restarts.
- **Averaged power** (`kW`/`W`) — the value is used directly (converted to kW). Intended for a **rolling `mean_linear` statistics helper**, *not* a raw instantaneous sensor (a single spot read isn't the half-hour average). Also used for per-MPPT DC sensors in multi-array setups, where the value only feeds a `dcᵢ/Σdc` ratio.

`Auto-detect` is **unit-first**: a `Wh`/`kWh`/`MWh` unit is treated as an energy counter and a `W`/`kW` unit as averaged power — `state_class` is only a fallback when the unit is missing. (Previously the energy-vs-power decision keyed on `state_class`, so a counter that omitted it was silently read as instantaneous power — a lifetime `kWh` total interpreted as a huge `kW` value.)

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

### Short-range forecast correction (considered and dropped)

An earlier roadmap item proposed nudging the next 1–6 hours of forecast from the recent `pv_actual / pv_estimate` ratio (an exponentially-decaying, cloud-driven correction). **It was evaluated and dropped** — recorded here so the reasoning isn't lost:

- **The signal decays too fast to be worth it.** Near-term deviation is cloud-driven, where persistence has very short skill: actual at `t` strongly predicts `t+1`, but that correlation falls off within an hour or two. So the nudge would do something only for the very next period and approach a no-op by +3 — exactly where forecast error is largest.
- **It would second-guess Solcast with a cruder model.** Solcast's near-term product already incorporates recent imagery; a single-inverter ratio plus coarse OWM cloud cover is a blunt instrument against it.
- **It would fork the forecast.** A now-relative, decaying, per-horizon correction can't go through `set_dampening` (that array is indexed by local time-of-day and applied every day), so it would have to be exposed as separate "corrected" sensors — forcing users to rewire automations and live with two forecasts that disagree.
- **The durable, predictable part is already captured** by the DB-driven [dampening](#adaptive-dampening), whose ±14-day seasonal window also covers individual missing slots — so there's little residual left for a short-range term to chase.

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
| Database Records | — | Total records in the store (attributes: `latest_period_end`, `distinct_sites`, `sites`) |
| Dampening Hours with DB Data | — | Hours where DB-derived factors are active |
| Weather Temperature | °C | OWM current temperature |
| Cloud Cover | % | OWM cloud cover |
| Battery Charge 30min Average | kW | Value read from the configured battery sensor (restored across restarts) |
| PV Power 30min Average | kW | Average generation for the period from the configured sensor (restored across restarts) |
| PV Export 30min Average | kW | Average export for the period from the configured sensor (restored across restarts) |
| Base Integration Status | — | `connected` or `not_detected` |

The **Dampening Hours with DB Data** sensor exposes per-hour diagnostics as attributes:

```yaml
hour_14_factor:           0.847      # final blended value pushed to base integration
hour_14_alpha:            0.72       # DB confidence (0 = neutral 1.0, 1 = pure DB)
hour_14_source:           db_blended # db_history | db_blended | no_data | night
hour_14_quality_records:  31.4       # quality-weighted record count
hour_14_avg_quality:      0.81       # mean combined weight of contributing records
overall_source:           db_blended
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

The built-in store holds one row per half-hour per site in a single `solcast_data` table:

```sql
CREATE TABLE solcast_data (
  "index"          INTEGER PRIMARY KEY AUTOINCREMENT,
  period_end       TEXT NOT NULL,
  period_end_epoch INTEGER NOT NULL,
  period_start     TEXT NOT NULL,
  site             TEXT NOT NULL DEFAULT '_total',  -- Solcast resource_id, or '_total' aggregate
  pv_actual        REAL NOT NULL,                   -- 30-min avg generation (kW)
  pv_export        REAL NOT NULL DEFAULT 0,         -- 30-min avg export (kW)
  pv_estimate      REAL NOT NULL,                   -- Solcast p50 estimate
  pv_estimate10    REAL NOT NULL,                   -- Solcast p10
  pv_estimate90    REAL NOT NULL,                   -- Solcast p90
  azimuth          REAL NOT NULL,                   -- solar azimuth at period midpoint (°)
  zenith           REAL NOT NULL,                   -- solar zenith at period midpoint (°)
  temp             REAL NOT NULL,                   -- OWM temperature (°C)
  clouds           INTEGER NOT NULL,                -- OWM cloud cover (0–100)
  description      TEXT NOT NULL,                   -- OWM weather description
  battery_charge   REAL NOT NULL DEFAULT 0,         -- 30-min avg battery charge (kW)
  UNIQUE(period_end_epoch, site)
);
```

The complete schema is created on first run (WAL mode), so there are no *schema* migrations. The `UNIQUE(period_end_epoch, site)` constraint enforces one row per slot per site; repeated writes within a slot are coalesced with `INSERT OR IGNORE`. One-time *data* repairs (e.g. recomputing historical `azimuth` after the hour-angle fix) run silently once, gated by SQLite's `PRAGMA user_version`.

---

## Sensor mapping guidance

```
total_pv = pv_actual   (inverter AC output — includes all loads, export, and battery)
```

`pv_export` and `battery_charge` sensors are recorded in the DB for reference and diagnostics but are not used in the `total_pv` calculation. Configure `pv_actual` to read from the inverter's generation meter (total AC output), not a self-consumption-only meter.

---

## Standalone tuning tool

`tools/standalone_tuning.py` runs the **same** tilt/azimuth optimisation outside Home Assistant, against the built-in SQLite store or a CSV export — handy for experimenting with parameters or validating a site without waiting for the daily run. It imports the integration's tuning functions, so results match the running integration.

```bash
# Whole-property tuning from the built-in store
python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db --capacity 6.6

# One site, seeded with that array's orientation
python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db \
    --site b68d-c05a --capacity 5 --tilt 30 --azimuth 67.5

# Every site in the table
python tools/standalone_tuning.py --sqlite config/solcast_solar_enhanced.db --all-sites

# Tune a CSV with the same columns instead
python tools/standalone_tuning.py --csv history.csv --capacity 5
```

Requires `numpy` + `scipy`. The SQLite source uses the standard library; CSV mode needs neither. Run `--help` for all options.

---

## Roadmap

Planned but not yet implemented:

- **Database retention / dampening-scan efficiency.** The store keeps one row per half-hour per site forever (~17.5k rows/site/year), and the seasonal dampening query is a full table scan (its day-of-year filter is a computed expression no index can serve). On a long-lived, multi-site database on a Raspberry Pi this scan gets slower over time. Under consideration: an optional **retention period** (default *keep everything*), and/or a stored, **indexed day-of-year column** to make the seasonal lookup indexed. Deferred — current cost is fine for typical single-site, few-year installs. See the [design document](DESIGN_DOCUMENT.md#roadmap).

---

## Compatibility

| Component | Version |
|---|---|
| Home Assistant | 2026.5.4+ |
| Python | 3.12+ |
| Storage | stdlib `sqlite3` — no install |
| scipy / numpy | Optional (PV tuning) — 1.7.0+ / 1.21.0+ |

---

## License

Apache-2.0 — see [LICENSE](LICENSE).
