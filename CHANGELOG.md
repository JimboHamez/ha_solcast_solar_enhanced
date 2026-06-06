# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.6.4] - 2026-06-06

### Changed
- **PV tuning no longer depends on scipy — works on a Raspberry Pi out of the
  box.** The tilt/azimuth optimiser was `scipy.optimize.minimize` (L-BFGS-B);
  scipy has no prebuilt ARM/Pi wheel and its from-source build fails under Home
  Assistant's locked-down environment (the meson permission error that broke the
  base integration in BJReplay/ha-solcast-solar #85). It is replaced by a
  pure-**numpy** coarse-to-fine grid search (`_minimize_grid`: full 5° sweep,
  then ±5° at 1°, then ±1° at 0.25° around the running best) — which is in fact
  the method Solcast notebook 3.4 itself uses. numpy is a core Home Assistant
  dependency with Pi wheels, so tuning now runs everywhere with nothing to
  install. Same geometry, same RMSE objective, ≤0.25° resolution; recovers a
  known synthetic orientation exactly. The grid search is shaped for low-power
  CPUs: it sweeps azimuth-outer so the one expensive transcendental
  (`cos(sun_az − panel_az)` over all records) is computed once per azimuth and
  the tilt sweep is a single vectorised numpy multiply-add, with peak memory
  bounded to one tilts×records block (≈14 MB even at 20k records). On a typical
  per-site dataset this is ~2× faster than a naive per-point loop. The record
  pre-filtering (cloud / clipping / export-limit / geometry exclusions) is also
  vectorised as numpy boolean masks instead of a Python per-record loop. End to
  end, tuning a 2,000-record site dropped from ~143 ms to ~63 ms here.

### Added
- **Optional history retention (cull old SQLite rows).** A new *Keep history for
  (days)* setting on the Storage step (`CONF_DB_RETENTION_DAYS`, default `0` =
  keep everything, so existing installs are unchanged) prunes rows older than the
  window on a daily timer, bounding the database on long-lived / low-power
  (Raspberry Pi) installs. Uses a plain `DELETE` (no `VACUUM`) so freed pages are
  reused without a heavy SD-card rewrite. Runs independently of auto-tuning, so it
  applies to logging-only setups. Seasonal dampening needs a cross-year window, so
  a value below ~400 days (≈13 months) logs a warning but is still honoured. This
  lands the retention half of the roadmap's database-efficiency item.
- **Dampening convergence gate (per-site).** Adaptive dampening is now held neutral
  (`1.0`, nothing pushed) for any array whose tuner has *confidently* converged on
  a tilt/azimuth that diverges materially from the orientation its Solcast site is
  configured with. This enforces the notebook 3.4b "tuned estimate" prerequisite —
  while the Solcast forecast is built on the wrong geometry, its actual/estimate
  ratio mixes orientation error with shading, so dampening would bake that error
  in. Trips when tuning has ≥ 50 clear-sky records **and** `|Δtilt| > 15°` or
  shortest-circle `|Δazimuth| > 25°`. A `dampening_gated` repair issue prompts the
  user to apply the *Tuned Panel Tilt/Azimuth* sensor values in their Solcast
  account; dampening resumes automatically once they agree. The gate is per-site
  aware (one mis-configured array doesn't freeze the others) and on by default —
  toggle *"Gate dampening until tuning agrees with Solcast orientation"* in the
  tuning options to disable.
- **Translations for 10 additional languages.** German, Spanish, French, Italian,
  Japanese, Dutch, Polish, Portuguese, Slovak and Urdu (`translations/<lang>.json`),
  covering the full config/options flow, selector options, entity name and both
  repair issues. Each mirrors `en.json` key-for-key.

### Fixed
- **Multi-site crash when OpenWeatherMap is absent.** The 1.6.3 fail-safe weather
  coercion was applied to the aggregate `_total` DB row but not the per-site rows,
  so a multi-site setup without OWM wrote `round(None, 2)` (crash) / `None` into
  the NOT NULL `clouds` column. Both the aggregate and per-site writes now share a
  single `_weather_for_storage()` helper that coerces unknown weather to the
  excluded `0 °C / 100 %` sentinel.

## [1.6.3] - 2026-06-05

### Fixed
- **Clear-sky (0% cloud) records no longer dropped from adaptive dampening.**
  `compute_dampening` read cloud cover as `int(r.get("clouds", 100) or 100)` (the
  same falsy-`0` bug fixed for tuning in 1.6.2). A genuine 0% reading — the
  clearest sky, the best data for a shading ratio — was scored in `_cloud_weight`'s
  zero band and excluded. Now `None`-aware: real `0` is kept, missing stays
  overcast.

### Changed
- **OpenWeatherMap is now treated as required for tuning & dampening, and the
  no-cloud-data path is fail-safe.** Cloud cover is the only input that lets these
  features isolate clear-sky periods, and it comes solely from OWM. Previously,
  with OWM disabled or a failed fetch, every record was stored as `clouds = 0`
  (perfectly clear) — so the clear-sky filter excluded nothing and could push a
  cloud-contaminated dampening curve to Solcast. Now:
  - missing/failed weather is stored as *unknown* and coerced to the excluded
    `100` sentinel, so such records can never masquerade as clear sky — tuning
    returns no result and dampening stays neutral (nothing pushed);
  - the **Cloud Cover** and **Weather Temperature** sensors report *unavailable*
    instead of a misleading `0 % / 0 °C`;
  - a **repair issue** is raised when a cloud-driven feature is enabled but no OWM
    source is configured, and cleared once a key is added.
- **Docs:** README reframes OWM from "optional" to required (with the free
  Current Weather Data API access requirements); `DESIGN_DOCUMENT.md` adds the
  tuning↔dampening "tuned estimate" prerequisite (notebook 3.4 → 3.4b), the
  fail-safe cloud-sentinel rationale, and a roadmap note on gating dampening by
  tuning convergence.

## [1.6.2] - 2026-06-05

### Fixed
- **Clear-sky (0% cloud) records no longer dropped from PV tuning.** `run_tuning`
  read cloud cover as `int(r.get("clouds", 100) or 100)`; because `0` is falsy, a
  genuine 0% reading — the clearest sky, exactly the data tuning most wants — was
  coerced to `100` and excluded by the cloud-cover filter. Missing/`None` is now
  distinguished from a real `0`, so clear records are kept while a missing value
  still defaults to overcast (excluded).

## [1.6.1] - 2026-06-05

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

[Unreleased]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.6.4...HEAD
[1.6.4]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.6.3...v1.6.4
[1.6.3]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/JimboHamez/ha_solcast_solar_enhanced/compare/v1.6.0...v1.6.1
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
