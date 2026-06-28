# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Home Assistant (HA) Integration Development Standards

## 1. Context7 Documentation Rules
- Always use the Context7 MCP server to fetch version-accurate API references and code snippets before generating or modifying code that uses external libraries. Do not rely on base training data for fast-evolving frameworks.
- If asked to implement or modify features using frameworks like Home Assistant Core or auxiliary dependencies, always precede your response by invoking the Context7 tools.
- Append "use context7" to your planning steps if you need to research the latest documentation. 

## 2. Architectural Guardrails
- **The Async Iron Law:** Never allow blocking code (e.g., `requests`, `time.sleep`, or synchronous file reads) in the main thread. Always wrap synchronous device calls in `await hass.async_add_executor_job()` or rewrite them natively using `aiohttp` or `asyncio`.
- **Data Coordination:** Always scaffold the integration using a central `DataUpdateCoordinator`. Individual entities must inherit from `CoordinatorEntity` and pull states from the coordinator's cached data, rather than querying the API directly to prevent rate-limiting.
- **UI-Driven Configuration:** Do not write YAML parsing routines. Generate UI-driven `ConfigFlow` components (`config_flow.py`) for initial setup and an `OptionsFlow` for changing parameters later without restarting Home Assistant.
- **Client Library Separation:** All raw API-specific network code, parsing, and authentication handling must live in a separate third-party Python client library (declared in the `manifest.json` `requirements` array). The custom component code should only orchestrate state translation.
- **HACS Layout Compliance:** Ensure the repository follows HACS layout standards. The `manifest.json` must explicitly contain a valid `"version"` key and a `"codeowners"` list. Generate a `hacs.json` file automatically in the project root.

## 3. Python Coding & Style Guidelines
- **Formatting:** Code must pass Ruff styling defaults with a 120-character line limit. Run `ruff format` and `ruff check --fix` before completing any file modification.
- **Import Conventions:** Order imports strictly by standard library, third-party, and then local modules. Ensure constants and dictionary keys are sorted alphabetically. Adhere strictly to Home Assistant's mandatory custom framework shortcut module bindings (e.g., import `homeassistant.util.dt` as `dt_util`).
- **Type Checking:** Every function signature must be fully typed (arguments and return types). Prefer concrete types over `Any` (some idiomatic HA `Any` remains — config-flow `user_input`, voluptuous schema dicts, raw-JSON payloads). Must pass strict (`mypy`-compliant) analysis; use `assert` to narrow types when Core context is ambiguous. Import and use structural types from the core (`HomeAssistant`, `ConfigEntry`, `DiscoveryInfoType`). Include a `py.typed` file in the package root to satisfy PEP-561 compliance.
- **Documentation:** Public methods must use Google-style docstrings. Comments must be complete sentences ending in a period.
- **Logging Restrictions:** Do not include the platform or domain name manually inside log strings (e.g., write `_LOGGER.error("Failed to connect")`, not `_LOGGER.error("[MyDomain] Failed to connect")`). Never log sensitive strings like API keys, tokens, or local passwords. Use `_LOGGER.debug` for developer diagnostics.
- **Native Constants:** Never hardcode states like `'on'`, `'off'`, `'unavailable'`, or metrics like `'C'`. Always import and use native constants from `homeassistant.const` (e.g., `STATE_ON`, `STATE_OFF`, `STATE_UNAVAILABLE`, `UnitOfTemperature.CELSIUS`).
- **Entity Naming:** Do not assign a raw string to the `_attr_name` property of an entity. Set `_attr_has_entity_name = True` and use localized device naming keys via translation strings inside the `strings.json` file.
- **Exception Handling:** Wrap external Python client calls in `homeassistant.exceptions.HomeAssistantError` variations (like `ConfigEntryNotReady`) to trigger safe auto-retries and elegant user-facing UI dialogs.

## What this project is

A Home Assistant (HA) custom integration (`custom_components/solcast_solar_enhanced`) that acts as a companion to [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar). It adds built-in SQLite historical storage (zero-config, stdlib `sqlite3`), automatic rooftop PV **tilt** optimisation (numpy coarse-to-fine grid search over a physical irradiance transposition — no scipy, so it works on a Raspberry Pi; azimuth is held fixed at the configured value, deliberately not tuned), and adaptive shading dampening computed purely from DB-collected actual-vs-forecast history (it never consumes the base integration's own dampening factors), ramping from a neutral no-op toward the measured ratio as data accumulates. Plane-of-array irradiance for tuning comes from **Open-Meteo** (keyless GHI/DNI/DHI, default-on), with OpenWeatherMap an optional legacy cloud/temperature source.

Development happens by installing the component into a running Home Assistant instance. There are no build steps. A `pytest` test suite lives in `tests/` (run `pytest` from the repo root; deps in `requirements_test.txt`, uses `pytest-homeassistant-custom-component`). A standalone PV-tuning CLI for running the optimiser against the DB/CSV outside HA lives in `tools/standalone_tuning.py` (fetches irradiance and supports a `--kt-threshold` clear-sky gate); `tools/backfill_irradiance.py` backfills the Open-Meteo `ghi`/`dni`/`dhi` columns onto pre-existing DB rows.

## Installation for development

Copy `custom_components/solcast_solar_enhanced/` into the HA `config/custom_components/` directory, then restart HA. PV tuning needs numpy, which Home Assistant already ships (and which has Raspberry Pi wheels), so a normal HA install needs nothing extra:

```bash
pip install numpy>=1.21.0  # already present in Home Assistant; no scipy
```

Storage uses stdlib `sqlite3` (no install). PV tuning uses a **numpy grid search, not scipy** — scipy has no Pi wheel and fails to build under HA (BJReplay/ha-solcast-solar #85). numpy is imported lazily, so tuning disables itself (integration still runs) in the unlikely event numpy is absent.

## Commands

Work in the repo virtualenv: `source venv/bin/activate`. There is no build step.

- **Run the suite:** `pytest` (or `python -m pytest`) from the repo root. Deps in `requirements_test.txt`; uses `pytest-homeassistant-custom-component` (the `hass` fixture, `MockConfigEntry`).
- **Run one file / test / pattern:** `pytest tests/test_multisite.py`, `pytest "tests/test_config_flow.py::test_sites_step_dc_split_valid_derives_strings"`, `pytest -k topology`.
- **Lint + format — component only:** `ruff check custom_components/solcast_solar_enhanced/` and `ruff format custom_components/solcast_solar_enhanced/`. The strict `pyproject.toml` ruleset and CI are scoped to the component; `tests/`, `tools/`, and `analysis/` are **intentionally not** kept ruff-clean, so always pass the component path — a bare `ruff check` at the repo root reports hundreds of expected errors in those dirs. `pyproject.toml`: line length 120, target py313, Google docstrings, with a `sensor.py` per-file-ignore for entity-boilerplate docstrings (`D101/D102/D107`).
- **CI gates (must stay green before merge):** `pytest`, `hassfest`, `HACS`, plus security/SBOM scanners (Semgrep, Gitleaks, Grype, Trivy, Syft). Use `gh pr checks <n>` to watch.

## Module responsibilities

| File | Role |
|---|---|
| `__init__.py` | Entry point — sets up coordinator, registers 3 services, handles load/unload |
| `coordinator.py` | `SolcastEnhancedCoordinator` (DataUpdateCoordinator) — half-hour-aligned update loop; orchestrates store writes, PV tuning (24 h), dampening push (6 h), OWM + Open-Meteo irradiance fetch |
| `sensor.py` | 15 `CoordinatorEntity` sensors; all read from coordinator data/properties. The three 30-min average sensors (PV Power/Export, Battery Charge) also extend `RestoreSensor` via `_RestoringSensorBase` to survive restarts. The diagnostic `MpptDcSensor` surfaces the latest captured per-MPPT DC telemetry |
| `config_flow.py` | UI wizard (`site → database → weather → battery → tuning → sites`), plus mirrored options flow. The `weather` step toggles Open-Meteo (default-on) and optional OWM |
| `const.py` | All config keys, defaults, domain names, sensor keys, service names, timing constants |
| `sqlite_store.py` | `SqliteStore` — the built-in, zero-config stdlib `sqlite3` store (executor jobs, WAL, serialising lock); insert + 2 query methods + sites/count/lifecycle + `async_migrate` (one-time data repairs, `PRAGMA user_version`-gated) |
| `pv_tuning.py` | `run_tuning()` (transposition-based tilt optimisation at fixed azimuth, called via `async_add_executor_job`) + pure-Python `solar_position()` and `clearsky_ghi()` (Haurwitz, for the Kt clear-sky gate) |
| `shading_dampening.py` | `compute_dampening()` per half-hour slot + `average_slot_pairs()` |
| `solcast_api.py` | `OWMClient` (OWM current-weather) + `OpenMeteoClient` (keyless plane-of-array GHI/DNI/DHI irradiance: current + archive backfill) — thin aiohttp wrappers |

## Key architecture patterns

**Data flow per 30-min update cycle** (`coordinator._do_update`):
1. Read `pv_actual` / `pv_export` via `_read_pv_value` (cumulative energy counter → avg kW over the actual interval, or averaged-power kW/W) and `battery_charge`; read per-site generation via `_read_site_actuals` (DC-ratio apportionment for shared-AC groups). When no `CONF_PV_ACTUAL_SENSOR` is configured (pure-microinverter install), the `_total` `pv_actual` is derived by summing the per-site reads so aggregate tuning/dampening aren't starved; a configured system sensor takes precedence
2. Fetch OWM weather (if enabled) and Open-Meteo plane-of-array irradiance (GHI/DNI/DHI, default-on). Open-Meteo `minutely_15` radiation is a **preceding-15-min mean**, so the value is collected as the **half-hour mean** over `[period_start, period_end)` — `async_get_interval(period_epoch)` averages the two samples timestamped `period_end − 15 min` and `period_end` that tile the period — matching `pv_actual` (also a half-hour average). When OWM is not configured, Open-Meteo's `clouds` doubles as the weather/cloud source (keyless)
3. Compute solar position (pure Python, no external lib)
4. Read forecast data from base integration coordinator (`hass.data["solcast_solar"]`); per-site forecast via `_site_forecast_for_period` (`detailedForecast-<resource_id>`)
5. Write the property-wide `_total` row to the store (`INSERT [OR] IGNORE` on `(period_end_epoch, site)`), including the `ghi`/`dni`/`dhi` irradiance columns, then one row per configured site (irradiance is property-wide, same on every site)
6. On 24 h timer: run `pv_tuning.run_tuning()` in a thread executor (aggregate `_total`, then per-site). Clear-sky tuning rows are selected by the Kt gate (see below); azimuth is passed in fixed (`panel_azimuth_to_internal(CONF_AZIMUTH)`), only tilt is fitted
7. On 6 h timer: compute 48 half-hour dampening slots → average to 24 hourly → push via `solcast_solar.set_dampening` service call (per-site when multi-site groups are configured)

**Battery reading priority**: Statistics sensor (`CONF_BATTERY_STAT_SENSOR`) takes precedence; raw fallback (`CONF_BATTERY_ENABLED` with `net`/`separate` modes) is used only when the stat sensor reads zero.

**PV tuning (transposition + Kt gate)** (`pv_tuning.py`, `sqlite_store.py`): tuning fits **tilt only** at a fixed azimuth. For each candidate tilt the stored Open-Meteo GHI/DNI/DHI are transposed to the panel plane (Hay-Davies anisotropic sky by default, isotropic otherwise), a single capacity scale is fitted by least squares, and the lowest mean-absolute-error tilt over a coarse-to-fine grid wins (`_minimize_tilt`). Azimuth is **non-identifiable** from this data (degenerate with the irradiance↔power time offset, biased by morning shading) so `run_tuning` echoes the configured `fixed_azimuth` straight back rather than tuning it. Clear-sky rows are chosen by a **clearness-index gate** applied in SQL before the `LIMIT` (`async_get_records_for_tuning`): when Open-Meteo is enabled, `Kt = ghi / clearsky_ghi(zenith) ≥ CONF_KT_THRESHOLD` (default `0.75`), judged only where the sun is up (`zenith < KT_ZENITH_MAX`) and the clear-sky reference is meaningful (`clearsky_ghi(zenith) ≥ KT_GHI_CS_FLOOR`); the pure-Python Haurwitz `clearsky_ghi` is registered as a SQLite function so the gate runs in-query. When OWM is absent the Kt gate replaces the OWM total-cloud gate, which over-rejects clear slots that carry harmless high/mid cloud. The in-tuning cloud re-filter is disabled (threshold `101`) when the SQL Kt gate already ran. Falls back to `cloud_max` (OWM total-cloud) only when Open-Meteo is disabled.

**Dampening confidence blend**: `final = (1−α) × 1.0 + α × db_factor`, where `db_factor` is the quality-weighted actual/forecast ratio from DB records. The anchor is a neutral `1.0` (NOT the base integration's factors — those are never read into the calculation). α is a sigmoid over quality-weighted record count; clamped to ±15% of 1.0 when α < 0.5. Sources tagged as `night`, `no_data`, `db_blended`, or `db_history`. Each record's quality weight grades how clear its sky was: as of v1.10.0b1 the basis is the **measured clearness index** `Kt = ghi / clearsky_ghi(zenith)` when Open-Meteo is enabled (`_kt_weight`, graded down from `CONF_KT_THRESHOLD`; records below `KT_GHI_CS_FLOOR` clear-sky reference are dropped), falling back to the legacy three-band cloud weight (`_cloud_weight`) only when Open-Meteo is off — the model cloud field is biased high and false-overcasts clear days, over-rejecting the clear records a shading ratio needs. The active basis is surfaced per slot and as a `clear_sky_basis` sensor attribute (`kt`/`cloud`). This mirrors the tuning Kt gate; the dampening query (`async_get_records_for_dampening`) returns `ghi` for it.

**Storage** (`sqlite_store.py`): the coordinator instantiates `SqliteStore(hass, hass.config.path("solcast_solar_enhanced.db"))` in `async_setup` when `CONF_DB_ENABLED` is set (defaults **on**, `DEFAULT_DB_ENABLED=True`). It's a single file, stdlib `sqlite3`, WAL mode (`synchronous=NORMAL`), every call run via `async_add_executor_job` and serialised by a lock. The base schema is created complete on first run (`has_site_col`/`has_battery_col` are always true), but columns introduced *after* the original schema are added on existing DBs with an additive, idempotent `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT 0` (`_ensure_columns` over `_ADDED_COLUMNS`: the per-MPPT DC telemetry pairs `dc_voltage1/2`/`dc_current1/2`, and the Open-Meteo `ghi`/`dni`/`dhi` irradiance columns — backfillable on old rows via `tools/backfill_irradiance.py`). Separately, one-time *data* repairs exist via `async_migrate(lat, lon)`, gated by `PRAGMA user_version` (`SCHEMA_VERSION`) so they run silently once; v1 recomputes the solar `azimuth` column for rows written before the hour-angle wrap fix (reconstructable from each row's `period_end_epoch` + lat/lon, rewriting only changed rows). Writes use `INSERT OR IGNORE` on `(period_end_epoch, site)`; the seasonal day-of-year window uses `strftime('%j', period_end_epoch, 'unixepoch')` (UTC) — a full scan (computed expression, no index; see the [roadmap](DESIGN_DOCUMENT.md#roadmap) for the retention/indexed-doy plan). The integration is SQLite-only as of v1.5.0 (MySQL was removed).

**Multi-site**: sites are auto-discovered from the base integration's RooftopSensors via `discover_sites(hass)` (module-level, shared with the config flow). The `CONF_SITE_GROUPS` config model maps a generation sensor (+ optional per-MPPT DC sensors) to one or more sites; the config-flow `sites` step authors it via per-site fields and derives the structure (`_derive_groups`). Each site is stored/tuned/dampened by its Solcast `resource_id`; the property-wide aggregate uses `site='_total'`, so aggregate queries pass `site=DEFAULT_SITE_ID` to avoid summing the additive per-site rows. Per-site `pv_actual` for shared-AC groups is apportioned by DC share (`ac × dcᵢ/Σdc`). The `sites` step leads with an explicit measurement-topology selector (`CONF_SITE_TOPOLOGY`: `direct` = each array its own generation sensor, no DC field; `dc_split` = one shared inverter apportioned by per-array DC); `_build_sites_schema(..., mode=)` renders the DC field only in `dc_split`, `_derive_groups(..., mode=)` builds single-site groups (`direct`) or `strings` (`dc_split`), and `_validate_dc_split` rejects a missing per-array DC sensor or mismatched AC sensors with a form error instead of silently dropping the array. The mode is persisted; `_infer_topology` defaults it for pre-selector entries (`strings` ⇒ `dc_split`, else `direct`). Config-flow fields are also placed by inverter-count topology to avoid duplicate entry (`_build_site_schema(..., single_site=)`): the flat per-inverter MPPT V/I fields (`CONF_MPPT*`) show on Step 1 only for single-array systems; multi-array systems map MPPT trackers per array in the `sites` step instead. Site discovery runs in Step 1 (`_is_single_site`, cached) so the topology is known before the step renders. The `sites` step prefills each array's generation field from `CONF_PV_ACTUAL_SENSOR` (`default_ac`, for shared-meter installs) and migrates any pre-existing flat MPPT keys into per-array suggestions (`_seed_flat_mppt`), clearing them on save (`_clear_flat_mppt`). `MpptDcSensor`'s `max_voltage` spans the property-wide and per-site trackers, so it stays populated when the flat keys are absent.

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
