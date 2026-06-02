# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/releases/tag/v1.0.0
