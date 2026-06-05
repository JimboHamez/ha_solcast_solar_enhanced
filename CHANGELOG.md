# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **PV Power / PV Export / Battery Charge 30-min average sensors persist across
  restarts.** They now restore their last value on startup (HA `RestoreSensor`)
  instead of showing *unknown* until the first half-hour update cycle (up to
  ~30 min after a restart). The live value supersedes the restored one as soon as
  it arrives.

## [1.6.0] - 2026-06-05

### Changed
- **`solcast_solar` is now a hard dependency.** The manifest uses `dependencies`
  (was `after_dependencies`), so Home Assistant refuses to set up this
  integration when the base integration is absent — it cannot function without
  it.
- **Single config entry enforced.** Only one instance can be added (one base
  integration, one property, one shared database); a second add aborts.
- **Minimum Home Assistant aligned to 2026.5.4** in `hacs.json` (was 2025.3),
  matching the documented requirement.
- **License standardised on Apache-2.0.** Resolved a contradiction where the
  `LICENSE` file was Apache-2.0 but the README claimed MIT; renamed `LICENSE.md`
  → `LICENSE`, corrected the README, and added a `NOTICE` file.

### Security
- **OpenWeatherMap API key redacted from logs.** A failed fetch logged the
  aiohttp error verbatim, which embeds the request URL — and the key in its
  `appid` query param. The key is now scrubbed before logging.

### Fixed
- **Solar azimuth east↔west flip.** `solar_position()` did not normalise the
  hour angle to [-180, 180], so for sites whose local morning/afternoon falls on
  a different UTC calendar day from solar noon (e.g. UTC+10 mornings) the azimuth
  was mirrored east↔west. Zenith was unaffected (cosine is periodic). Now
  hemisphere- and longitude-agnostic, verified against an independent reference.
- **Existing databases are repaired in place.** A silent, one-time migration
  (gated by `PRAGMA user_version`) recomputes the stored `azimuth` for rows
  written before the wrap fix, reconstructed from each row's `period_end_epoch`
  and the site lat/lon. Only changed rows are rewritten.
- **Forecast columns no longer silently zero-filled.** The base integration
  stores `detailedForecast` `period_start` as `datetime` objects, which the slot
  parser rejected — zeroing `pv_estimate`/`10`/`90`. The parser now accepts
  `datetime`, ISO string and epoch.

### Performance
- **PV tuning objective vectorised** with numpy (≈59× faster on the objective;
  numerically identical) — meaningful on low-power CPUs.
- **Dampening records fetched once per run** instead of once per half-hour slot
  (the day-of-year window is identical across all 48 slots).
- **OpenWeatherMap uses Home Assistant's shared aiohttp session** instead of
  building a new connector every fetch.

### Added
- Debug-level logging at key data-flow checkpoints (per-cycle update summary,
  forecast estimate provenance, base-integration detection, tuning skips).
- CI: GitHub Actions for hassfest + HACS validation and the pytest suite.

## [1.5.2] - 2026-06-05

### Fixed
- Aligned the HACS display name (`hacs.json`) with the integration name —
  *Solcast Solar Enhanced* — so HACS no longer shows the stale *Solcast PV
  Forecast Enhanced* label.

## [1.5.1] - 2026-06-05

### Fixed
- **Wrong unit on the *Forecast Now* fallback.** When the base integration's
  in-memory coordinator data is unavailable, the *Forecast Now* (kW) sensor fell
  back to reading `forecast_remaining_today` — a kWh count-down — and surfaced it
  through a kilowatt-labelled sensor. It now derives the value from the current
  half-hour `detailedForecast` slot's `pv_estimate` (average kW over the slot),
  keeping the declared unit honest, and reads `0` when the attribute is absent.

### Docs
- Added a Home Assistant dashboard screenshot to the README installation section
  (`images/dashboard.png`).

## [1.5.0] - 2026-06-04

### Added
- **Built-in SQLite storage — zero configuration.** History is stored in a single
  file (`config/solcast_solar_enhanced.db`) using the Python standard-library
  `sqlite3` module — no server, no credentials and no extra dependency. It is
  enabled out of the box, so tuning and dampening work on a fresh install with
  nothing to set up. The store uses WAL mode and runs all calls in the executor;
  the schema is created complete on first run, so there are no migrations.
- **Storage diagnostics.** The *Database Records* sensor now exposes
  `latest_period_end`, `distinct_sites` and `sites` attributes, and the store logs
  its file path and row count at startup — handy for verifying that data is
  accumulating and for pointing tools like the sqlite-web add-on at the file.

### Removed
- **MySQL support is removed.** The integration is now SQLite-only: the MySQL
  backend, the `aiomysql` dependency, the `db_host`/`db_port`/`db_user`/
  `db_password`/`db_name`/`db_readonly` options and the storage-backend selector
  are all gone. The storage step in the setup/options flow is now a single
  *Enable history storage* toggle. To carry forward an existing MySQL history,
  export it to CSV (e.g. `mysqldump`/`SELECT ... INTO OUTFILE`) before upgrading;
  otherwise the built-in store starts fresh and rebuilds as data accumulates.

## [1.4.1] - 2026-06-04

### Fixed
- **`pv_estimate` is no longer written as zero.** The property-wide `_total` row
  sourced its forecast estimates from the base coordinator's in-memory
  `pv_estimate` key, which current `solcast_solar` versions don't expose, so the
  `pv_estimate`/`pv_estimate10`/`pv_estimate90` columns were stored as `0`. The
  `_total` row now reads the documented property-wide `detailedForecast`
  attribute off `sensor.solcast_pv_forecast_forecast_today` — the same source the
  per-site rows already use — and selects the slot matching the half-hour
  boundary. The base coordinator key remains a fallback when the attribute is
  unavailable.

### Changed
- **Update cycle now fires on the wall-clock half-hour grid.** The coordinator's
  free-running 30-minute interval (anchored to HA's boot time) is replaced with a
  listener that fires at `:00`/`:30` plus a small offset, so each energy-counter
  measurement window aligns to Solcast's half-hour slots instead of drifting. A
  small post-boundary offset lets boundary counter states post before the delta
  is read; the elapsed-time average-kW math and the 15–60 min acceptance guard
  are unchanged. Forecast slots are now matched on the snapped slot boundary
  rather than the drifting measured start.

## [1.4.0] - 2026-06-03

### Fixed
- **Dampening factors are now aligned to local time.** The 48-slot dampening
  array was built on UTC time-of-day, but the base `solcast_solar` integration
  applies `damp_factor[i]` to the i-th **local** half-hour. For non-UTC sites
  this shifted the whole curve by the UTC offset — e.g. for a UTC+10/+11 site,
  daytime periods received the night factor (~1.0), effectively disabling
  dampening during daylight. The slot grid is now built on local wall-clock time
  (each local slot converted to its UTC instant for the solar-position lookup).
- **Energy counters can no longer be misread as instantaneous power.** PV input
  auto-detection now decides energy vs power by the sensor's **unit**
  (`Wh`/`kWh`/`MWh` → energy counter; `W`/`kW` → averaged power), with
  `state_class` only as a fallback. Previously the decision keyed on
  `state_class`, so a `kWh` counter that omitted it was read as instantaneous
  power — a lifetime total interpreted as a huge `kW` value.

### Changed
- Standardised on **cumulative energy counters** as the recommended PV input.
  The power path remains for a rolling `mean_linear` statistics helper (a
  continuous sliding window with no `:00`/`:30` reset race) and for per-MPPT DC
  ratio sensors; the config-flow mode selector and docs were relabelled
  accordingly, replacing the legacy boundary-resetting Statistics-helper guidance.
- The dampening day-of-year window query now renders timestamps in UTC
  deterministically (DB session pinned to UTC), and solar position for stored
  rows and dampening slots is taken at the interval **midpoint** rather than the
  boundary.

## [1.3.0] - 2026-06-03

### Changed
- **Stored database timestamps now snap to the half-hour grid.** The 30-minute
  poll is free-running (anchored to setup/restart, not the wall clock), so
  `period_end_epoch` previously drifted off the `:00`/`:30` boundaries and the
  `(period_end_epoch, site)` unique key only coalesced exact-second collisions.
  Each stored `period_end` / `period_end_epoch` / derived `period_start` (for
  both the aggregate `_total` row and per-site rows) is now rounded to the
  nearest half-hour, aligning rows to Solcast's 48-slot UTC grid — the same grid
  the dampening calculation walks — so the unique key enforces one row per slot
  per site. The real wall-clock time still drives the energy-counter delta
  averaging and the tuning interval timer, so the average-kW math is unchanged.
  Side effect: two restarts within the same half-hour now collapse to one row
  (the second `INSERT IGNORE` is a no-op) rather than producing a near-duplicate.

### Documentation
- Documented the planned **Short-range Forecast Correction** design (purpose,
  activation conditions, `correction(n) = 1.0 + (recent_ratio − 1.0) × exp(−n/τ)`
  decay, stacking with dampening, planned `correction_tau` config). Still
  not implemented.
- Corrected the README "Adaptive dampening" section, which still described the
  pre-1.2.0 base-factor blend, to match the DB-only neutral-`1.0` calculation.

## [1.2.0] - 2026-06-03

### Changed
- **Shading dampening is now computed purely from database-collected history**
  and no longer consumes any dampening factors from the base `solcast_solar`
  integration. The confidence blend is anchored on a neutral `1.0`
  (`final = (1−α) × 1.0 + α × db_factor`) instead of the base factor, so the
  result ramps from a no-op toward the DB-measured actual/forecast ratio as
  data accumulates. When no usable DB data exists for a slot the factor stays
  at a neutral `1.0` rather than falling back to the base integration's values.
  Slot source labels changed accordingly: `base_fallback` → `no_data`,
  `blended` → `db_blended` (`db_history`, `night` unchanged). The
  `set_dampening` push and its skip-while-base-auto-dampening guard are
  unchanged.

### Fixed
- Corrected an unsatisfiable test dependency pin
  (`pytest-homeassistant-custom-component>=1.3`, which has no 1.x release) to
  `>=0.13,<0.14`.

## [1.1.1] - 2026-06-02

### Fixed
- Skip the dampening push (logging a single clear warning) when the base
  integration's **automatic dampening** (`auto_dampen`) is enabled — it rejects
  every manual `set_dampening` call with a `ServiceValidationError`, which
  previously spammed Home Assistant's core error log every 6 hours. The push is
  now also issued with `blocking=True` so any base-side service error is handled
  by the integration instead of leaking into the core log.

## [1.1.0] - 2026-06-02

### Added
- **Multi-site support** for multiple Solcast rooftop arrays on one property:
  - Auto-discovery of sites from the base integration's RooftopSensors
    (`resource_id`, name, capacity, capacity_dc, tilt, azimuth, compass_degrees).
  - Per-site storage: new `site` column and `(period_end_epoch, site)` unique key,
    with a safe, idempotent migration from the legacy single-column key; one row
    per site is written alongside the property-wide `_total` aggregate.
  - Per-site **PV tuning** (seeded from each array's Solcast orientation; results
    surfaced as a `per_site` attribute on the Tuned Panel Tilt sensor).
  - Per-site **dampening** pushed via `set_dampening` with the site `resource_id`.
  - **DC-ratio apportionment** for string inverters (e.g. Fronius) that share one
    AC output but expose per-MPPT DC: measured AC is split by each string's DC
    share (`ac × dcᵢ / Σ dc`).
  - Config-flow **per-site sensor mapping** step (shown automatically when more
    than one site is detected).
- **Flexible PV input modes** — each generation/export sensor can be read as a
  cumulative energy counter (`Wh`/`kWh`/`MWh`, `total_increasing`) or a power
  sensor (`W`/`kW`), with `Auto-detect`. Energy mode derives average kW from the
  energy delta over the *actual* elapsed interval (robust to polling drift),
  guards counter resets/rollovers and out-of-band gaps, and persists baselines
  across restarts via Home Assistant's `Store`.
- Property-wide export limit read automatically from the base integration's
  `entry.options["site_export_limit"]` (Watts → kW), preferred over the manual
  option.
- **Standalone PV tuning CLI** (`tools/standalone_tuning.py`) — runs the same
  optimisation against MySQL or a CSV export outside Home Assistant, per-site or
  aggregate.
- Expanded `pytest` suite (`tests/test_multisite.py` plus DB site-filter tests);
  117 tests passing.

### Changed
- Documentation (`README.md`, `DESIGN_DOCUMENT.md`, `CLAUDE.md`) now leads with the
  inverter **energy counter** as the recommended PV input; the HA Statistics
  `mean_linear` sensor is documented as an optional legacy approach rather than a
  prerequisite.

### Fixed
- Dampening push now calls the base integration's actual `set_dampening` service
  (comma-separated `damp_factor`, optional `site`) instead of the non-existent
  `set_dampening_factor`, repairing the dampening push for current base versions.

## [1.0.0] - 2026-06-01

### Added
- MySQL-backed historical storage of PV power, forecasts, solar position, weather
  and battery data.
- Automatic rooftop PV tuning — daily tilt/azimuth optimisation via scipy
  (L-BFGS-B).
- Adaptive shading dampening — quality-weighted factors blended with, and
  progressively replacing, the base integration's manual dampening.
- OpenWeatherMap cloud-cover integration for dampening quality weighting.
- Battery handling with Statistics-sensor priority and raw `net`/`separate`
  fallback.
- Grid export limit filter for PV tuning (`CONF_EXPORT_LIMIT_KW`) and the
  `TuningExportExcludedSensor` reporting records excluded by it.

### Fixed
- `total_pv` double-counting in tuning and dampening (`pv_actual` is the inverter
  AC output and already includes export and battery).
- Millisecond-epoch crash in `solar_position`, plus two config-flow bugs.
- `CREATE TABLE` permission error avoided by checking `information_schema` first.
- `NumberSelectorConfig` step rejected by HA 2026.x.

[Unreleased]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.5.2...v1.6.0
[1.5.2]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/tag/v1.0.0
