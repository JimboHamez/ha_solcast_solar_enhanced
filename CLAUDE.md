# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Home Assistant (HA) custom integration (`custom_components/solcast_solar_enhanced`) that acts as a companion to [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar). It adds built-in SQLite historical storage (zero-config, stdlib `sqlite3`), automatic rooftop PV tilt/azimuth optimisation (scipy L-BFGS-B), and adaptive shading dampening computed purely from DB-collected actual-vs-forecast history (it never consumes the base integration's own dampening factors), ramping from a neutral no-op toward the measured ratio as data accumulates.

Development happens by installing the component into a running Home Assistant instance. There are no build steps. A `pytest` test suite lives in `tests/` (run `pytest` from the repo root; deps in `requirements_test.txt`, uses `pytest-homeassistant-custom-component`). A standalone PV-tuning CLI for running the optimiser against the DB/CSV outside HA lives in `tools/standalone_tuning.py`.

## Installation for development

Copy `custom_components/solcast_solar_enhanced/` into the HA `config/custom_components/` directory, then restart HA. Optional dependencies must be installed in the HA Python venv:

```bash
pip install numpy>=1.21.0 scipy>=1.7.0  # required for PV tuning
```

Storage uses stdlib `sqlite3` (no install). PV tuning is the only optional dep — a lazy import that disables tuning when absent; the integration still runs.

## Module responsibilities

| File | Role |
|---|---|
| `__init__.py` | Entry point — sets up coordinator, registers 3 services, handles load/unload |
| `coordinator.py` | `SolcastEnhancedCoordinator` (DataUpdateCoordinator) — half-hour-aligned update loop; orchestrates store writes, PV tuning (24 h), dampening push (6 h), OWM fetch |
| `sensor.py` | 13 `CoordinatorEntity` sensors; all read from coordinator data/properties |
| `config_flow.py` | UI wizard (`site → database → owm → battery → tuning`), plus mirrored options flow |
| `const.py` | All config keys, defaults, domain names, sensor keys, service names, timing constants |
| `sqlite_store.py` | `SqliteStore` — the built-in, zero-config stdlib `sqlite3` store (executor jobs, WAL, serialising lock); insert + 2 query methods + sites/count/lifecycle |
| `pv_tuning.py` | `run_tuning()` (called via `async_add_executor_job`) + pure-Python `solar_position()` |
| `shading_dampening.py` | `compute_dampening()` per half-hour slot + `average_slot_pairs()` |
| `solcast_api.py` | `OWMClient` — thin aiohttp wrapper for OWM current-weather endpoint |

## Key architecture patterns

**Data flow per 30-min update cycle** (`coordinator._do_update`):
1. Read `pv_actual` / `pv_export` via `_read_pv_value` (cumulative energy counter → avg kW over the actual interval, or averaged-power kW/W) and `battery_charge`; read per-site generation via `_read_site_actuals` (DC-ratio apportionment for shared-AC groups)
2. Fetch OWM weather (if enabled)
3. Compute solar position (pure Python, no external lib)
4. Read forecast data from base integration coordinator (`hass.data["solcast_solar"]`); per-site forecast via `_site_forecast_for_period` (`detailedForecast-<resource_id>`)
5. Write the property-wide `_total` row to the store (`INSERT [OR] IGNORE` on `(period_end_epoch, site)`), then one row per configured site
6. On 24 h timer: run `pv_tuning.run_tuning()` in a thread executor (aggregate `_total`, then per-site)
7. On 6 h timer: compute 48 half-hour dampening slots → average to 24 hourly → push via `solcast_solar.set_dampening` service call (per-site when multi-site groups are configured)

**Battery reading priority**: Statistics sensor (`CONF_BATTERY_STAT_SENSOR`) takes precedence; raw fallback (`CONF_BATTERY_ENABLED` with `net`/`separate` modes) is used only when the stat sensor reads zero.

**Dampening confidence blend**: `final = (1−α) × 1.0 + α × db_factor`, where `db_factor` is the quality-weighted actual/forecast ratio from DB records. The anchor is a neutral `1.0` (NOT the base integration's factors — those are never read into the calculation). α is a sigmoid over quality-weighted record count; clamped to ±15% of 1.0 when α < 0.5. Sources tagged as `night`, `no_data`, `db_blended`, or `db_history`.

**Storage** (`sqlite_store.py`): the coordinator instantiates `SqliteStore(hass, hass.config.path("solcast_solar_enhanced.db"))` in `async_setup` when `CONF_DB_ENABLED` is set (defaults **on**, `DEFAULT_DB_ENABLED=True`). It's a single file, stdlib `sqlite3`, WAL mode (`synchronous=NORMAL`), every call run via `async_add_executor_job` and serialised by a lock. The schema is created complete on first run — no migrations — so `has_site_col`/`has_battery_col` are always true. Writes use `INSERT OR IGNORE` on `(period_end_epoch, site)`; the seasonal day-of-year window uses `strftime('%j', period_end_epoch, 'unixepoch')` (UTC). The integration is SQLite-only as of v1.5.0 (MySQL was removed).

**Multi-site**: sites are auto-discovered from the base integration's RooftopSensors via `discover_sites(hass)` (module-level, shared with the config flow). The `CONF_SITE_GROUPS` config model maps a generation sensor (+ optional per-MPPT DC sensors) to one or more sites; the config-flow `sites` step authors it via per-site fields and derives the structure (`_derive_groups`). Each site is stored/tuned/dampened by its Solcast `resource_id`; the property-wide aggregate uses `site='_total'`, so aggregate queries pass `site=DEFAULT_SITE_ID` to avoid summing the additive per-site rows. Per-site `pv_actual` for shared-AC groups is apportioned by DC share (`ac × dcᵢ/Σdc`).

**Energy-counter reads**: `_read_pv_value` supports cumulative energy counters (kWh/Wh/MWh — the recommended input) and averaged-power readings (kW/W, intended for a rolling `mean_linear` helper, *not* a raw instantaneous sensor). Energy mode computes avg kW from the energy delta over the *actual* elapsed time and guards resets/rollovers, first-read, and out-of-band intervals; baselines persist via HA `Store` (`{DOMAIN}_{entry_id}_energy_baseline`). `_resolve_input_mode` auto-detection is **unit-first**: a `…wh` unit → energy counter, a `…w` unit → averaged power; `state_class` is only a fallback when the unit is absent (this prevents a counter that omits `state_class` from being read as instantaneous power). Power mode stays available for per-MPPT DC sensors, which feed only a `dcᵢ/Σdc` ratio.

**Optional deps pattern**: `pv_tuning.py` guards its imports with `try/except ImportError` and sets a `*_AVAILABLE` flag; feature code checks it before executing. `sqlite_store.py` has no optional dep (stdlib `sqlite3`), so storage always works.

## Base integration coupling

- Domain name: `BASE_DOMAIN = "solcast_solar"` (in `const.py`)
- Forecast data: read from `hass.data["solcast_solar"].data` (keys: `forecast_now`, `forecast_today`, `pv_estimate`, `pv_estimate10`, `pv_estimate90`). Falls back to reading named sensor states.
- Per-site forecast: `sensor.solcast_pv_forecast_forecast_today` attribute `detailedForecast-<resource_id>` (underscore variant fallback) — list of `{period_start, pv_estimate, pv_estimate10, pv_estimate90}`; `pv_estimate` is **average kW over the half-hour** (matches `pv_actual`).
- Site discovery + per-site export limit: per-site orientation/capacity from RooftopSensor attributes (`resource_id`, `capacity`, `capacity_dc`, `tilt`, `azimuth`, `compass_degrees`); property-wide export limit read from the base config entry `entry.options["site_export_limit"]` (Watts → kW), preferred over the manual option.
- Dampening push: `hass.services.async_call("solcast_solar", "set_dampening", {"damp_factor": "<csv>", "site": "<resource_id>"})` — `damp_factor` is a comma-separated string of 24 (hourly) or 48 (half-hourly) floats; `site` is optional and targets a single Solcast site (omit for global). The pushed factors are computed solely from DB-collected history; the base integration's own dampening factors are never read back into the calculation. The push is still gated by `_read_base_auto_dampen()` (skipped while the base's automatic dampening is on, since it rejects manual `set_dampening`).

## Adding a new sensor

1. Add a `SENSOR_*` constant to `const.py`
2. Subclass `_EnhancedSensorBase` in `sensor.py`, set `_attr_name` and unit/device-class attributes, implement `native_value`
3. Instantiate it in `async_setup_entry` in `sensor.py`
4. Expose the backing value from `SolcastEnhancedCoordinator` (either via `coordinator.data` dict or a `@property`)

## Compatibility requirements

- Home Assistant 2026.5.4+, Python 3.12+
- Storage: built-in SQLite via stdlib `sqlite3` (no install, no server)
- `manifest.json` declares `"dependencies": ["solcast_solar"]` — HA will refuse to load if the base integration is absent
