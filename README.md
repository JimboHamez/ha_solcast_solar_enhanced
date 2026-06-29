# Solcast Solar Enhanced

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
![GitHub Release](https://img.shields.io/github/v/release/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
[![hacs_downloads](https://img.shields.io/github/downloads/JimboHamez/ha_solcast_solar_enhanced/latest/total?style=for-the-badge)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/latest)
![GitHub License](https://img.shields.io/github/license/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
![GitHub commit activity](https://img.shields.io/github/commit-activity/y/JimboHamez/ha_solcast_solar_enhanced?style=for-the-badge)
![Maintenance](https://img.shields.io/maintenance/yes/2026?style=for-the-badge)

[![Tests](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/test.yml/badge.svg)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/test.yml)
[![Validate](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/validate.yml/badge.svg)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/validate.yml)
[![Security](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/security.yml/badge.svg)](https://github.com/JimboHamez/ha_solcast_solar_enhanced/actions/workflows/security.yml)

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

## 🆕 What's new in v1.10.0b1 (beta)

**Adaptive dampening now finds clear-sky periods from measured irradiance, not cloud cover.** Each historical record's quality weight is graded by the **clearness index** `Kt = GHI / clear-sky GHI` (from Open-Meteo, on by default) instead of the model total-cloud field. That cloud field is biased high and often reports "overcast" on genuinely clear days — so it was rejecting exactly the clear-sky records a shading ratio depends on. This brings dampening in line with the clear-sky filter PV tuning already uses.

- **Nothing to configure** — the existing **Clearness index threshold** option (already used by tuning) now also governs dampening.
- The dampening sensor exposes a new `clear_sky_basis` attribute (`kt` or `cloud`) so you can see which signal is active.
- If you've disabled Open-Meteo, dampening falls back to the old cloud-cover bands, unchanged.

**New — PV Forecast Confidence (load scheduling):** a 0–100 sensor (with a high/medium/low rating) for *"can I trust the next few hours enough to run a heavy load right now?"* It scores how well your recent measured output is tracking the Solcast forecast — high = go (run the EV/pool pump), low = local conditions are diverging, so hold. It's a decision aid, not a forecast, and never changes your Solcast figures. (Automatable "Good/Next Load Window" entities are coming next.)

**Multi-site:** **per-site shading dampening now actually engages,** and **each array gets its own device.** Per-site dampening needs a per-site forecast to compare against per-site output, which most Solcast setups don't expose — so when it's missing the property forecast is split across your arrays by capacity share (only when they share orientation, so timing stays correct). Each array now appears as its **own HA device** (its own card, nested under the main integration), carrying three entities: **PV Power** (that array's measured generation), **Shading** (its measured dampening, with orientation/confidence/shading % attributes) and **Tuned Tilt** (its optimised tilt). Name each array on the sites step — it defaults to your Solcast site name.

**Also:** Open-Meteo irradiance is now recorded as a true **half-hour mean** (the two 15-minute samples covering each period, averaged) instead of a single point sample — so it lines up with your half-hour-averaged generation. No extra API calls; biggest improvement on partly-cloudy slots and around sunrise/sunset.

**Upgrading?** Drop-in — no config changes, no migration. Existing setups simply start weighting their clear-sky records by Kt on the next dampening cycle.

> Earlier (v1.9.x): config-wizard screenshots in the README, the "How are your arrays measured?" topology selector with validation, and pure-microinverter setups no longer needing a whole-system generation sensor.

Full history in the [CHANGELOG](CHANGELOG.md) · [release notes](https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases).

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

### 4. Weather & irradiance (Open-Meteo — keyless, on by default)

Tuning and dampening only learn from *clear-sky* periods (cloudy ones tell you nothing about your panels), and PV tilt tuning additionally needs solar **irradiance**. Both now come from [**Open-Meteo**](https://open-meteo.com/), which is **free and needs no API key** — it's enabled by default, so there's nothing to set up. It supplies the irradiance components (GHI/DNI/DHI) plus cloud cover and temperature.

> **OpenWeatherMap is now optional (legacy).** If you'd rather use OWM for cloud/temperature, enable it in setup **Step 3** and paste a free key — it then takes precedence for cloud/temperature, while irradiance still comes from Open-Meteo. A repair issue appears only if you disable Open-Meteo *and* don't configure OWM, leaving no weather source at all.

**Check it's working** after setup: the **Cloud Cover** sensor should show a real percentage and the repair issue (if any) should be gone. To make tuning useful on day one rather than waiting for fresh data, backfill irradiance onto your existing history with `tools/backfill_irradiance.py` (see [Standalone tools](#standalone-tools)).

---

## Installation

<p align="center">
  <a href="images/dashboard.png"><img width="700" alt="Solcast Solar Enhanced sensors in Home Assistant" src="images/dashboard.png"></a>
</p>

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

<p align="center">
  <a href="images/config-step1-site.png"><img width="420" alt="Step 1 — Site & System" src="images/config-step1-site.png"></a>
</p>

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
| MPPT 1/2 DC voltage + current | Optional — your inverter's per-string voltage/current sensors, for curtailment-detection capture. Leave MPPT 2 blank for single-tracker inverters. **Single-array systems only** — these fields are hidden for multi-array systems, which map per-array MPPT in Step 6 instead |

### Step 2 — Storage

<p align="center">
  <a href="images/config-step2-storage.png"><img width="420" alt="Step 2 — Storage" src="images/config-step2-storage.png"></a>
</p>

| Field | Default | Description |
|---|---|---|
| Enable history storage | On | Toggle the built-in store on/off |
| Keep history for (days) | 0 | `0` keeps everything. A positive value prunes older rows daily to save space. Seasonal dampening works best with ≥ ~400 days |

The store lives at `config/solcast_solar_enhanced.db`. To browse it, point the [sqlite-web add-on](https://github.com/hassio-addons/addon-sqlite-web) at that path.

### Step 3 — Weather & Irradiance

<p align="center">
  <a href="images/config-step3-weather.png"><img width="420" alt="Step 3 — Weather & Irradiance" src="images/config-step3-weather.png"></a>
</p>

Open-Meteo (keyless) is on by default and powers tuning & dampening (see [§4 above](#4-weather--irradiance-open-meteo--keyless-on-by-default)). OpenWeatherMap is an optional legacy alternative for cloud/temperature.

| Field | Default | Description |
|---|---|---|
| Enable Open-Meteo | **On** | Keyless irradiance (GHI/DNI/DHI) + cloud/temperature |
| Enable OWM | **Off** | Optional legacy cloud/temperature source; needs a key |
| OWM API key | — | Free key from openweathermap.org (only if OWM enabled) |

### Step 4 — Battery Storage

<p align="center">
  <a href="images/config-step4-battery.png"><img width="420" alt="Step 4 — Battery Storage" src="images/config-step4-battery.png"></a>
</p>

A fallback for systems without a battery sensor mapped in Step 1.

| Field | Description |
|---|---|
| Enable raw battery fallback | Toggle |
| Mode | `net` (signed power sensor) or `separate` (charge-only sensor) |
| Net battery sensor | Signed power entity (positive = charging) |
| Charge battery sensor | Charge-only power entity |

### Step 5 — PV Tuning & Dampening

<p align="center">
  <a href="images/config-step5-tuning.png"><img width="420" alt="Step 5 — PV Tuning & Dampening" src="images/config-step5-tuning.png"></a>
</p>

| Field | Default | Description |
|---|---|---|
| Auto PV tuning | On | Run tilt/azimuth optimisation daily |
| Auto dampening | On | Recalculate and push dampening every 6 hours |
| Cloud threshold % | 20 | OWM-cloud clear-sky gate: records below this count as clear-sky (used only when Open-Meteo is off) |
| Max cloud % to include | 60 | Records above this are excluded |
| Clearness index threshold | 0.75 | Clear-sky gate when Open-Meteo is on (the default): a half-hour counts as clear when `Kt = GHI ÷ clear-sky GHI` is at or above this. More reliable than total cloud %, which over-rejects clear slots with harmless high/mid cloud |
| Clipping threshold | 0.95 | Fraction of capacity at which clipping is assumed |
| Grid export limit (kW) | 0 | Exclude records pegged at this ceiling; 0 = disabled. Read automatically from the base integration if set |

### Step 6 — Per-site sensor mapping (multi-site only)

<a href="images/config-step6-sites.png"><img align="right" width="340" alt="Step 6 — Per-site sensor mapping" src="images/config-step6-sites.png"></a>

Shown when more than one Solcast site is detected. Sites are auto-discovered from the base integration (orientation and capacity come from Solcast). For each site you map its generation sensor, and optionally its per-string DC sensors.

This page appears only for multi-array systems — a single-array system relies on the system-wide sensors from Step 1 and never sees it. It opens by asking **how your arrays are measured**, then shows only the fields that topology needs:

- **Each array has its own generation sensor** (microinverters, e.g. Enphase, or one inverter per array): map each array's own AC/generation sensor; there's no DC field. The per-site **generation sensor** is pre-filled with Step 1's system-wide PV Generation sensor — pick the array's own sensor when arrays are separately metered.
- **One shared inverter, split by DC** (a single multi-string inverter, e.g. Fronius): put the *same* whole-system AC sensor on every array and give each its **DC/MPPT sensor**, so the shared AC is split between arrays by DC share. Leaving a DC sensor off an array, or using different AC sensors, is flagged with an error rather than silently dropped.
- The per-site **MPPT voltage/current** fields are the per-array home for MPPT trackers (diagnostics). For multi-array systems they live *here only* — Step 1 hides its MPPT fields. If you're upgrading from an older version that had MPPT entities on Step 1, they're suggested on the first two arrays here for you to confirm (and cleared from Step 1 on save).

See [Multi-site](#multi-site) for how shared inverters are split between arrays.

> **Heads up:** the base integration's own **automatic dampening** must be **disabled** (Solcast PV Forecast → Configure). While it's on, the base rejects manual dampening, so this integration can't apply its factors — it detects this, skips the push, and logs a warning.

---

<br clear="all">

## How it works

- **PV tuning** runs daily: it searches for the panel tilt and azimuth that best explain your clear-sky generation, and reports them on the **Tuned Panel Tilt/Azimuth** sensors. Needs at least ~10 clear-sky, non-clipped records. Clear-sky half-hours are selected by a measured **clearness index** (`Kt = GHI ÷ clear-sky GHI`) when Open-Meteo is on — avoiding total cloud %'s habit of rejecting genuinely clear slots that had harmless high/mid cloud (in cloudy winters that gate can reject *every* clear record, starving the optimiser).
- **Adaptive dampening** compares your actual output to the forecast across a ±14-day seasonal window, weighting each record by how clear the sky was and how close the sun was to the same position. It starts at a neutral no-op and ramps toward the measured correction as data builds, then pushes 24 hourly factors to Solcast via `set_dampening`. The base integration's own dampening factors are never read into this — the correction is learned purely from your history.
- **Curtailment** — when your inverter is export-limited, that capped output is detected and handled so it doesn't look like shading: tuning excludes it, and dampening clips it to the achievable ceiling so a curtailed clear day stays neutral.

Full detail — the confidence model, the weighting maths, convergence timelines by climate, and design decisions — lives in the [design document](DESIGN_DOCUMENT.md).

### Multi-site

When the base integration has more than one rooftop array, each is stored, tuned and dampened separately (keyed by its Solcast `resource_id`) alongside the property-wide aggregate. Single-site behaviour is unchanged.

The per-site step asks which of these two topologies you have, then shows only the matching fields:

- **Dedicated AC per array (simplest).** If every array is independently metered — microinverters (e.g. Enphase) or one string inverter per array — map each site's own AC/generation sensor. There's no DC field in this mode; each site reports its own AC directly, no apportionment needed.
- **Shared inverter AC.** If several arrays share one AC sensor (a single multi-string inverter, e.g. Fronius), put that same AC sensor on every array and give each its per-string DC sensor; the integration splits the measured AC between them by each string's share of DC current (`ac × dcᵢ / Σ dc`), so each array can still be tuned individually. Every array in this mode needs a DC sensor and they must share one AC sensor — otherwise the wizard shows an error rather than silently dropping an array.

---

## Sensors

| Sensor | Unit | Description |
|---|---|---|
| Forecast Now | kW | Current 30-min PV forecast (from base integration) |
| Forecast Today | kWh | Total forecast for today (from base integration) |
| Tuned Panel Tilt | ° | Optimised tilt from PV tuning (carries `mae_kw`, `capacity_scale`, and a `per_site` attribute in multi-site mode) |
| Tuned Panel Azimuth | ° | Your configured azimuth — **not tuned** (azimuth is non-identifiable from this data; `azimuth_tuned: false`). Reported for reference only |
| Tuning RMSE | kW | Goodness of fit for the tuned tilt |
| Tuning Export Limited Excluded | — | Records dropped from the last tuning run by the export-limit filter |
| Database Records | — | Total records in the store |
| MPPT DC Voltage (max) | V | Diagnostic — highest captured string voltage this cycle (per-tracker detail in attributes). Unavailable until per-string DC sensors are configured |
| Dampening Hours with DB Data | — | Hours where DB-derived factors are active (per-hour diagnostics in attributes) |
| Weather Temperature | °C | Current temperature (Open-Meteo, or OWM if configured) |
| Cloud Cover | % | Cloud cover (Open-Meteo, or OWM if configured) |
| Battery Charge 30min Average | kW | From the configured battery sensor (restored across restarts) |
| PV Power 30min Average | kW | Average generation for the period (restored across restarts) |
| PV Export 30min Average | kW | Average export for the period (restored across restarts) |
| PV Forecast Confidence | 0–100 | Short-horizon load-scheduling decision aid — how well recent output is tracking the forecast (`rating` high/medium/low + `recent_bias` in attributes). A decision aid, not a forecast; never pushed to the base |
| Base Integration Status | — | `connected` or `not_detected` |

### Per-site sensors (multi-site only)

When you configure more than one array, each array gets **its own HA device** (grouped on its own card, nested under the main integration device), carrying these entities:

| Sensor | Unit | Description |
|---|---|---|
| `<array>` PV Power 30min Average | kW | That array's measured generation for the period (DC-share apportioned for shared-inverter setups; `pv_estimate` + `capacity_kw` in attributes) |
| `<array>` Shading | — | Average daytime dampening factor (1.0 = no shading, < 1 = measured structural shading), with orientation, `shading_pct`, confidence and clear-sky basis in attributes |
| `<array>` Tuned Tilt | ° | Optimised tilt from that array's last PV tuning run (fit RMSE, record count and configured tilt/orientation in attributes) |

Each array's display name comes from the **sites** config step (defaults to its Solcast site name).

---

## Services

| Service | Description |
|---|---|
| `solcast_solar_enhanced.run_pv_tuning` | Force immediate PV tuning |
| `solcast_solar_enhanced.run_dampening_update` | Force immediate dampening recalculation and push |
| `solcast_solar_enhanced.fetch_weather` | Force immediate weather fetch (Open-Meteo / OWM) |

---

## Standalone tools

`tools/standalone_tuning.py` runs the same tilt optimisation outside Home Assistant, against the SQLite store or a CSV export — handy for experimenting without waiting for the daily run.

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

### Backfill irradiance

`tools/backfill_irradiance.py` fills the `ghi`/`dni`/`dhi` columns on existing rows from Open-Meteo's free historical archive, so transposition-based tilt tuning is useful immediately instead of waiting months for fresh data to accumulate. Stdlib-only; safe to re-run (fills only rows still missing irradiance).

```bash
python tools/backfill_irradiance.py --sqlite config/solcast_solar_enhanced.db \
    --lat -37.9046 --lon 145.0362
```

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
