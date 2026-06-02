# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Home Assistant (HA) custom integration (`custom_components/solcast_solar_enhanced`) that acts as a companion to [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar). It adds MySQL-backed historical storage, automatic rooftop PV tilt/azimuth optimisation (scipy L-BFGS-B), and adaptive shading dampening that progressively replaces the base integration's manual dampening as data accumulates.

Development happens by installing the component into a running Home Assistant instance. There are no build steps. A `pytest` test suite lives in `tests/` (run `pytest` from the repo root; deps in `requirements_test.txt`, uses `pytest-homeassistant-custom-component`). A standalone PV-tuning CLI for running the optimiser against the DB/CSV outside HA lives in `tools/standalone_tuning.py`.

## Installation for development

Copy `custom_components/solcast_solar_enhanced/` into the HA `config/custom_components/` directory, then restart HA. Optional dependencies must be installed in the HA Python venv:

```bash
pip install aiomysql>=0.2.0           # required for DB features
pip install numpy>=1.21.0scipy>=1.7.0  # required for PV tuning
```

Both use lazy imports — the integration runs without them, disabling only the relevant feature.

## Module responsibilities

| File | Role |
|---|---|
| `__init__.py` | Entry point — sets up coordinator, registers 3 services, handles load/unload |
| `coordinator.py` | `SolcastEnhancedCoordinator` (DataUpdateCoordinator) — 30-min polling loop; orchestrates DB writes, PV tuning (24 h), dampening push (6 h), OWM fetch |
| `sensor.py` | 13 `CoordinatorEntity` sensors; all read from coordinator data/properties |
| `config_flow.py` | 5-step UI wizard (`site → database → owm → battery → tuning`), plus mirrored options flow |
| `const.py` | All config keys, defaults, domain names, sensor keys, service names, timing constants |
| `db_manager.py` | `DbManager` — async aiomysql pool, schema init/migration, 3 query methods |
| `pv_tuning.py` | `run_tuning()` (called via `async_add_executor_job`) + pure-Python `solar_position()` |
| `shading_dampening.py` | `compute_dampening()` per half-hour slot + `average_slot_pairs()` |
| `solcast_api.py` | `OWMClient` — thin aiohttp wrapper for OWM current-weather endpoint |

## Key architecture patterns

**Data flow per 30-min update cycle** (`coordinator._do_update`):
1. Read `pv_actual` / `pv_export` via `_read_pv_value` (power **or** cumulative energy counter → avg kW over the actual interval) and `battery_charge`; read per-site generation via `_read_site_actuals` (DC-ratio apportionment for shared-AC groups)
2. Fetch OWM weather (if enabled)
3. Compute solar position (pure Python, no external lib)
4. Read forecast data from base integration coordinator (`hass.data["solcast_solar"]`); per-site forecast via `_site_forecast_for_period` (`detailedForecast-<resource_id>`)
5. Write the property-wide `_total` row to MySQL (`INSERT IGNORE` on `(period_end_epoch, site)`), then one row per configured site
6. On 24 h timer: run `pv_tuning.run_tuning()` in a thread executor (aggregate `_total`, then per-site)
7. On 6 h timer: compute 48 half-hour dampening slots → average to 24 hourly → push via `solcast_solar.set_dampening` service call (per-site when multi-site groups are configured)

**Battery reading priority**: Statistics sensor (`CONF_BATTERY_STAT_SENSOR`) takes precedence; raw fallback (`CONF_BATTERY_ENABLED` with `net`/`separate` modes) is used only when the stat sensor reads zero.

**Dampening confidence blend**: `final = (1−α) × base_factor + α × db_factor`. α is a sigmoid over quality-weighted record count; clamped to ±15% of base when α < 0.5. Sources tagged as `night`, `base_fallback`, `blended`, or `db_history`.

**DB schema migration**: `_init_schema()` runs `CREATE TABLE IF NOT EXISTS`, then `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for `battery_charge`, `pv_export` and `site`, then `_migrate_unique_key()` swaps the legacy `uq_epoch (period_end_epoch)` for `uq_epoch_site (period_end_epoch, site)` (guarded by `information_schema` checks since MySQL lacks `DROP INDEX IF EXISTS`). All safe to re-run.

**Multi-site**: sites are auto-discovered from the base integration's RooftopSensors via `discover_sites(hass)` (module-level, shared with the config flow). The `CONF_SITE_GROUPS` config model maps a generation sensor (+ optional per-MPPT DC sensors) to one or more sites; the config-flow `sites` step authors it via per-site fields and derives the structure (`_derive_groups`). Each site is stored/tuned/dampened by its Solcast `resource_id`; the property-wide aggregate uses `site='_total'`, so aggregate queries pass `site=DEFAULT_SITE_ID` to avoid summing the additive per-site rows. Per-site `pv_actual` for shared-AC groups is apportioned by DC share (`ac × dcᵢ/Σdc`).

**Energy-counter reads**: `_read_pv_value` supports power sensors (kW/W) and cumulative energy counters (kWh/Wh/MWh, `state_class: total_increasing`), with `auto` detection. Energy mode computes avg kW from the energy delta over the *actual* elapsed time and guards resets/rollovers, first-read, and out-of-band intervals; baselines persist via HA `Store` (`{DOMAIN}_{entry_id}_energy_baseline`).

**Optional deps pattern**: `pv_tuning.py` and `db_manager.py` each guard their imports with `try/except ImportError` and set a `*_AVAILABLE` flag. Feature code checks this flag before executing.

## Base integration coupling

- Domain name: `BASE_DOMAIN = "solcast_solar"` (in `const.py`)
- Forecast data: read from `hass.data["solcast_solar"].data` (keys: `forecast_now`, `forecast_today`, `pv_estimate`, `pv_estimate10`, `pv_estimate90`). Falls back to reading named sensor states.
- Per-site forecast: `sensor.solcast_pv_forecast_forecast_today` attribute `detailedForecast-<resource_id>` (underscore variant fallback) — list of `{period_start, pv_estimate, pv_estimate10, pv_estimate90}`; `pv_estimate` is **average kW over the half-hour** (matches `pv_actual`).
- Site discovery + per-site export limit: per-site orientation/capacity from RooftopSensor attributes (`resource_id`, `capacity`, `capacity_dc`, `tilt`, `azimuth`, `compass_degrees`); property-wide export limit read from the base config entry `entry.options["site_export_limit"]` (Watts → kW), preferred over the manual option.
- Dampening push: `hass.services.async_call("solcast_solar", "set_dampening", {"damp_factor": "<csv>", "site": "<resource_id>"})` — `damp_factor` is a comma-separated string of 24 (hourly) or 48 (half-hourly) floats; `site` is optional and targets a single Solcast site (omit for global).
- Base dampening factors (read-back): inspects `entry.options["dampening"]` from the base config entry; falls back to scanning all state entities for `"solcast"` + `"dampening"` + `"hour_XX"` in the entity ID.

## Adding a new sensor

1. Add a `SENSOR_*` constant to `const.py`
2. Subclass `_EnhancedSensorBase` in `sensor.py`, set `_attr_name` and unit/device-class attributes, implement `native_value`
3. Instantiate it in `async_setup_entry` in `sensor.py`
4. Expose the backing value from `SolcastEnhancedCoordinator` (either via `coordinator.data` dict or a `@property`)

## Compatibility requirements

- Home Assistant 2026.5.4+, Python 3.12+
- MySQL 8.0+ (requires `ADD COLUMN IF NOT EXISTS` syntax)
- `manifest.json` declares `"dependencies": ["solcast_solar"]` — HA will refuse to load if the base integration is absent
