"""DataUpdateCoordinator for Solcast Solar Enhanced."""

from __future__ import annotations

import logging
import statistics
import time
from collections import Counter, deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    APPORTION_AZIMUTH_TOL,
    BASE_DOMAIN,
    CONF_ALBEDO,
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MODE,
    CONF_BATTERY_NET_SENSOR,
    CONF_BATTERY_STAT_SENSOR,
    CONF_CAPACITY_KW,
    CONF_CLIPPING_THRESHOLD,
    CONF_CLOUD_MAX_INCLUDE,
    CONF_CLOUD_THRESHOLD,
    CONF_DAMPENING_GATE,
    CONF_DB_ENABLED,
    CONF_DB_RETENTION_DAYS,
    CONF_EXPORT_LIMIT_KW,
    CONF_KT_THRESHOLD,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MPPT1_CURRENT_SENSOR,
    CONF_MPPT1_VOLTAGE_SENSOR,
    CONF_MPPT2_CURRENT_SENSOR,
    CONF_MPPT2_VOLTAGE_SENSOR,
    CONF_OPENMETEO_ENABLED,
    CONF_OWM_API_KEY,
    CONF_OWM_ENABLED,
    CONF_PV_ACTUAL_INPUT_MODE,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_INPUT_MODE,
    CONF_PV_EXPORT_SENSOR,
    CONF_SITE_AUTODISCOVER,
    CONF_SITE_GROUPS,
    CONF_TILT,
    DAMPENING_GATE_AZIMUTH_TOL,
    DAMPENING_GATE_MIN_RECORDS,
    DAMPENING_GATE_TILT_TOL,
    DAMPENING_INTERVAL_HOURS,
    DB_RETENTION_MIN_RECOMMENDED_DAYS,
    DEFAULT_ALBEDO,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_CLOUD_THRESHOLD,
    DEFAULT_DAMPENING_GATE,
    DEFAULT_DB_ENABLED,
    DEFAULT_DB_FILENAME,
    DEFAULT_DB_RETENTION_DAYS,
    DEFAULT_EXPORT_LIMIT_KW,
    DEFAULT_KT_THRESHOLD,
    DEFAULT_OPENMETEO_ENABLED,
    DEFAULT_PV_INPUT_MODE,
    DEFAULT_SITE_AUTODISCOVER,
    DEFAULT_SITE_ID,
    DOMAIN,
    ENERGY_DT_MAX_FRACTION,
    ENERGY_DT_MIN_FRACTION,
    HALF_HOUR_REFRESH_OFFSET_SECONDS,
    ISSUE_DAMPENING_GATED,
    ISSUE_OWM_REQUIRED,
    KT_GHI_CS_FLOOR,
    KT_ZENITH_MAX,
    MAX_MPPT_TRACKERS,
    STORAGE_VERSION,
    TUNING_INTERVAL_HOURS,
    UPDATE_INTERVAL_MINUTES,
)
from .load_advisory import CONFIDENCE_HORIZON_HOURS, RECENT_BIAS_LOOKBACK_S, compute_confidence
from .pv_tuning import normalize_epoch, panel_azimuth_to_internal, panel_azimuth_to_solcast, run_tuning, solar_position
from .shading_dampening import average_slot_pairs, compute_dampening
from .solcast_api import OpenMeteoClient, OWMClient
from .sqlite_store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def _azimuth_spread(azimuths: list[float]) -> float:
    """Largest shortest-arc difference (degrees) between any pair of azimuths.

    Wrap-aware, so 350° and 10° read as 20° apart, not 340°.
    """
    spread = 0.0
    for i in range(len(azimuths)):
        for j in range(i + 1, len(azimuths)):
            d = abs(azimuths[i] - azimuths[j]) % 360.0
            spread = max(spread, min(d, 360.0 - d))
    return spread


def discover_sites(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Discover Solcast sites from the base integration's RooftopSensors.

    Each site sensor exposes ``resource_id`` plus orientation/capacity attributes.
    Returns a list of normalised site dicts; empty if none found. Shared by the
    coordinator and the config flow.
    """
    sites: list[dict[str, Any]] = []
    try:
        for state in hass.states.async_all("sensor"):
            attrs = state.attributes
            resource_id = attrs.get("resource_id")
            if not resource_id or "solcast" not in state.entity_id:
                continue

            def _f(key: str, attrs: Mapping[str, Any] = attrs) -> float:
                try:
                    return float(attrs.get(key, 0) or 0)
                except (ValueError, TypeError):
                    return 0.0

            sites.append(
                {
                    "resource_id": str(resource_id),
                    "name": attrs.get("name") or state.name,
                    "capacity": _f("capacity"),
                    "capacity_dc": _f("capacity_dc"),
                    "tilt": _f("tilt"),
                    "azimuth": _f("azimuth"),
                    "compass_degrees": _f("compass_degrees"),
                    "entity_id": state.entity_id,
                }
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Site discovery failed: %s", exc)
    return sites


class SolcastEnhancedCoordinator(DataUpdateCoordinator):
    """Coordinator that orchestrates all enhanced features."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator (refreshes are wall-clock driven, not interval)."""
        # No free-running interval: refreshes are driven by a wall-clock listener
        # (see async_setup) so each cycle fires on the :00/:30 half-hour grid
        # rather than drifting from HA's boot time. This keeps the energy-counter
        # measurement window aligned with Solcast's half-hour slots.
        super().__init__(
            hass,
            _LOGGER,
            name="solcast_solar_enhanced",
            update_interval=None,
        )
        self._entry = entry
        self._opts = {**entry.data, **entry.options}

        self._db: SqliteStore | None = None
        self._owm: OWMClient | None = None
        self._openmeteo: OpenMeteoClient | None = None
        # Latest plane-of-array irradiance components (GHI/DNI/DHI), stored with
        # each DB row to feed transposition-based tuning. None until first fetch /
        # when Open-Meteo is disabled or unreachable.
        self._irradiance: dict[str, Any] = {"ghi": None, "dni": None, "dhi": None}

        # Weather defaults to *unknown* (None), not 0. Without OWM there is no
        # cloud data; a 0 here would read as perfectly clear sky and be trusted by
        # the tuning/dampening clear-sky filters. None is fail-safe: the sensors
        # show "unavailable" and the stored record is excluded (see the DB-write
        # coercion below and the OWM-required repair issue in async_setup).
        self._weather: dict[str, Any] = {"temp": None, "clouds": None, "description": "unavailable"}
        self._tuning_result: dict[str, Any] | None = None
        self._site_tuning_results: dict[str, dict[str, Any]] = {}
        self._dampening_table: list[dict[str, Any]] = []
        # Recent (epoch, pv_actual, pv_estimate) daylight slots driving the
        # short-horizon forecast-confidence advisory (item 3). Bounded; older than
        # the lookback window is ignored at compute time.
        self._recent_bias: deque[tuple[int, float, float]] = deque(maxlen=16)
        self._confidence: dict[str, Any] = compute_confidence([])
        # Per-site visibility state (multi-site): each configured array's retained
        # dampening curve and its own recent-bias confidence, surfaced on per-site
        # sensors. Keyed by Solcast resource_id.
        self._site_dampening_tables: dict[str, list[dict[str, Any]]] = {}
        self._site_recent_bias: dict[str, deque[tuple[int, float, float]]] = {}
        self._site_confidence: dict[str, dict[str, Any]] = {}
        # Latest measured generation per array (avg kW over the just-completed
        # half-hour) plus its forecast, surfaced on a per-site PV Power sensor.
        # Keyed by Solcast resource_id; empty until a multi-site cycle runs.
        self._site_output: dict[str, dict[str, float]] = {}
        self._last_dampening_ts: float = 0.0
        self._last_tuning_ts: float = 0.0
        self._last_prune_ts: float = 0.0
        self._db_record_count: int = 0
        # Freshness/coverage diagnostics surfaced on the Database Records sensor.
        self._db_latest_period_end: str | None = None
        self._db_sites: list[str] = []
        self._base_status: str = "not_detected"
        self._auto_dampen_warned: bool = False
        # Latest captured per-MPPT DC telemetry (Phase 2), surfaced on a diagnostic
        # sensor so users can confirm their string sensors are wired and data is
        # landing. None until a cycle with DC sensors configured runs.
        self._dc_telemetry: dict[str, Any] | None = None
        # True while the dampening push is held neutral because a tuned orientation
        # diverges materially from the configured (Solcast) one. Per-site aware:
        # set if *any* target is gated this cycle. Surfaced on the Dampening sensor.
        self._dampening_gated: bool = False

        # Discovered Solcast sites (multiple arrays on one property), each:
        # {resource_id, name, capacity, capacity_dc, tilt, azimuth, entity_id}.
        self._sites: list[dict[str, Any]] = []

        # Energy-counter baselines: {key: {"value": kwh, "epoch": int}}.
        # Persisted across restarts so energy-delta readings survive a reload.
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_energy_baseline")
        self._energy_baselines: dict[str, Any] = {}
        self._baselines_dirty: bool = False

        # Unsubscribe handle for the half-hour wall-clock refresh listener.
        self._unsub_timer: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Initialise DB and OWM connections."""
        opts = self._opts

        # Restore energy-counter baselines from disk (if any).
        try:
            stored = await self._store.async_load()
            if isinstance(stored, dict):
                self._energy_baselines = stored
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not load energy baselines: %s", exc)

        if opts.get(CONF_DB_ENABLED, DEFAULT_DB_ENABLED):
            self._db = SqliteStore(self.hass, self.hass.config.path(DEFAULT_DB_FILENAME))
            ok = await self._db.async_connect()
            if not ok:
                _LOGGER.warning("DB connection failed — DB features disabled for this session")
                self._db = None
            else:
                # One-time, silent repair of azimuth values written before the
                # hour-angle wrap fix; gated by PRAGMA user_version so it runs once.
                await self._db.async_migrate(
                    float(opts.get(CONF_LATITUDE, -37.9)),
                    float(opts.get(CONF_LONGITUDE, 145.0)),
                )

        if opts.get(CONF_OWM_ENABLED) and opts.get(CONF_OWM_API_KEY):
            self._owm = OWMClient(
                api_key=opts[CONF_OWM_API_KEY],
                latitude=float(opts.get(CONF_LATITUDE, -37.9)),
                longitude=float(opts.get(CONF_LONGITUDE, 145.0)),
                session=async_get_clientsession(self.hass),
            )

        # Open-Meteo irradiance collection (keyless, additive). Stores GHI/DNI/DHI
        # alongside each row for transposition-based tuning; does not yet replace
        # OWM as the cloud source, so it raises no repair issue of its own.
        if opts.get(CONF_OPENMETEO_ENABLED, DEFAULT_OPENMETEO_ENABLED):
            self._openmeteo = OpenMeteoClient(
                latitude=float(opts.get(CONF_LATITUDE, -37.9)),
                longitude=float(opts.get(CONF_LONGITUDE, 145.0)),
                session=async_get_clientsession(self.hass),
            )

        # Surface a repair issue when the cloud-driven features are enabled but NO
        # weather source is available — neither Open-Meteo nor OWM. Open-Meteo is
        # keyless and on by default, so this normally never fires; it only triggers
        # if the user has disabled Open-Meteo and not configured OWM. Without a
        # cloud source every record's cover is unknown (excluded) and
        # tuning/dampening stay inert — fail loud, not silent. Re-evaluated on every
        # reload, so enabling either source clears the issue.
        if (
            not self._owm
            and not self._openmeteo
            and (opts.get(CONF_AUTO_TUNING, True) or opts.get(CONF_AUTO_DAMPENING, True))
        ):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_OWM_REQUIRED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_OWM_REQUIRED,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_OWM_REQUIRED)

        # Drive refreshes from the wall clock at :00/:30 + a small offset, so the
        # measurement window aligns to Solcast's half-hour grid instead of drifting
        # from HA's boot time. The offset lets boundary energy-counter states post
        # before we read the delta (counters update on their own cadence).
        self._unsub_timer = async_track_time_change(
            self.hass,
            self._handle_timed_refresh,
            minute=(0, 30),
            second=HALF_HOUR_REFRESH_OFFSET_SECONDS,
        )

    @callback
    def _handle_timed_refresh(self, now: datetime) -> None:
        """Wall-clock-aligned refresh trigger (fires at :00/:30 each hour)."""
        self.hass.async_create_task(self.async_request_refresh())

    async def async_teardown(self) -> None:
        """Close DB pool and cancel the refresh timer."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        if self._db:
            await self._db.async_close()
            self._db = None
        # Clear repair issues on unload (a reload re-creates them if still
        # applicable via async_setup / the next dampening run).
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_OWM_REQUIRED)
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_DAMPENING_GATED)

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._do_update()
        except Exception as exc:
            raise UpdateFailed(f"Update failed: {exc}") from exc

    async def _do_update(self) -> dict[str, Any]:
        opts = self._opts = {**self._entry.data, **self._entry.options}
        now_epoch = normalize_epoch(time.time())
        # Slot timestamp for the stored row, snapped to the nearest :00/:30
        # boundary so rows align to Solcast's half-hour grid and the
        # (period_end_epoch, site) unique key coalesces repeated writes within one
        # slot. Real wall-clock ``now_epoch`` is still used for energy-counter
        # delta timing and the tuning/dampening interval timers.
        period_epoch = self._snap_to_half_hour(now_epoch)

        # Detect base integration
        base_coord = self._get_base_coordinator()
        new_status = "connected" if base_coord is not None else "not_detected"
        if new_status != self._base_status:
            _LOGGER.debug("Base integration status: %s -> %s", self._base_status, new_status)
        self._base_status = new_status

        # Discover Solcast sites (multiple arrays on one property).
        if opts.get(CONF_SITE_AUTODISCOVER, DEFAULT_SITE_AUTODISCOVER):
            self._sites = self._discover_sites()

        # Read PV sensors. Supports both averaged-power sensors and cumulative
        # energy counters (delta over the actual elapsed interval → average kW).
        pv_actual, pv_actual_start = self._read_pv_value(
            opts.get(CONF_PV_ACTUAL_SENSOR, ""),
            opts.get(CONF_PV_ACTUAL_INPUT_MODE, DEFAULT_PV_INPUT_MODE),
            "pv_actual",
            now_epoch,
        )
        pv_export, _ = self._read_pv_value(
            opts.get(CONF_PV_EXPORT_SENSOR, ""),
            opts.get(CONF_PV_EXPORT_INPUT_MODE, DEFAULT_PV_INPUT_MODE),
            "pv_export",
            now_epoch,
        )
        # Per-site measured generation (multi-site). Empty unless groups are
        # configured. Reads here so energy baselines advance in one save below.
        site_actuals = self._read_site_actuals(opts, now_epoch)
        # When no whole-system generation sensor is configured (e.g. a pure
        # microinverter setup with only per-array sensors), derive the property
        # total by summing the per-site measured generation, so the aggregate
        # '_total' row — which drives aggregate tuning and dampening — is not left
        # at zero. A configured page-1 sensor always takes precedence.
        if not opts.get(CONF_PV_ACTUAL_SENSOR) and site_actuals:
            pv_actual, summed_start = self._sum_site_actuals(site_actuals)
            if summed_start:
                pv_actual_start = summed_start
        if self._baselines_dirty:
            await self._save_baselines()
        battery_charge = self._read_battery(opts)

        # Fetch OWM weather
        if self._owm:
            self._weather = await self._owm.async_fetch()

        # Fetch Open-Meteo irradiance as the half-hour mean over [period_start,
        # period_end) — the average of the two 15-min preceding-mean samples that
        # tile the period — so it matches pv_actual (also a half-hour average)
        # rather than a single point sample. Stored as-is; None on miss (fail-safe).
        if self._openmeteo:
            fetched = await self._openmeteo.async_get_interval(period_epoch)
            self._irradiance = {k: fetched.get(k) for k in ("ghi", "dni", "dhi")}
            # Open-Meteo also supplies cloud cover + temperature. Use it as the
            # weather source when OWM is not configured (the keyless default), so
            # tuning/dampening get clear-sky data without an API key. OWM, when
            # configured, keeps precedence (set just above) for back-compatibility.
            if self._owm is None and fetched.get("clouds") is not None:
                self._weather = {
                    "temp": fetched.get("temp"),
                    "clouds": int(fetched["clouds"]),
                    "description": "open-meteo",
                }

        # Solar position at the interval *midpoint* (period_end − 15 min), the
        # representative sun position for a value averaged across the half-hour —
        # matched geometrically against the dampening slots, which also use their
        # midpoint.
        lat = float(opts.get(CONF_LATITUDE, -37.9))
        lon = float(opts.get(CONF_LONGITUDE, 145.0))
        az, zen = solar_position(period_epoch - 900, lat, lon)

        # Forecast data from base integration. forecast_now/today drive the
        # sensors; the in-memory pv_estimate keys are only a fallback for the DB
        # row (see below) since newer base versions don't expose them.
        forecast_now, forecast_today, pv_estimate, pv_est10, pv_est90 = self._read_forecast_from_base(base_coord)

        # Persist to DB
        if self._db and opts.get(CONF_DB_ENABLED, DEFAULT_DB_ENABLED):
            period_end = datetime.fromtimestamp(period_epoch, tz=UTC).isoformat()
            start_epoch = pv_actual_start if pv_actual_start else period_epoch - 1800
            period_start = datetime.fromtimestamp(start_epoch, tz=UTC).isoformat()
            # Forecast slots are bucketed on clean half-hour boundaries, so the
            # lookup keys off the snapped slot start (period_end − 30 min), not the
            # drifting measured start used for the avg-kW math. Prefer the
            # documented property-wide detailedForecast slot; fall back to the base
            # coordinator's in-memory estimate only when the attribute is absent.
            slot_start_epoch = period_epoch - 1800

            # Phase-2 per-MPPT DC telemetry capture (off-MPP curtailment detection
            # groundwork). Aggregated over the just-completed slot from recorder
            # history — max voltage (most off-MPP) / min current (most throttled) —
            # so curtailment that happened mid-slot, not just at the boundary, is
            # caught; falls back to the instantaneous read when no history exists.
            # Up to MAX_MPPT_TRACKERS paired trackers, kept per-tracker (not
            # aggregated across trackers) for a later Vmp-band calibrator. The
            # '_total' row uses the property-wide trackers; per-site rows use their
            # own. Banked now (cannot be backfilled); nothing acts on it yet.
            dc_entities = self._collect_dc_entities(opts)
            dc_hist = await self._interval_values(dc_entities, slot_start_epoch, period_epoch)
            site_dc = self._read_site_dc_telemetry(opts, dc_hist)
            total_dc = self._read_mppt_telemetry(self._mppt_list_from_opts(opts), dc_hist) or (0.0, 0.0, 0.0, 0.0)
            # Median operating voltage per tracker (reduction of the same per-sec
            # series) — the shading-mechanism ingredient; forward-only, unconsumed.
            total_vmed = self._read_mppt_vmed(self._mppt_list_from_opts(opts), dc_hist)
            site_vmed = self._read_site_vmed(opts, dc_hist)
            # Surface the latest reading on the diagnostic sensor (None when no DC
            # sensors are configured, so the entity stays unavailable rather than
            # reporting a misleading 0).
            self._dc_telemetry = self._dc_telemetry_summary(total_dc, site_dc) if dc_entities else None

            t_est, t_est10, t_est90 = self._total_forecast_for_period(slot_start_epoch)
            if (t_est, t_est10, t_est90) != (0.0, 0.0, 0.0):
                pv_estimate, pv_est10, pv_est90 = t_est, t_est10, t_est90
                _LOGGER.debug("Forecast estimate from detailedForecast slot: %s", t_est)
            elif pv_estimate:
                _LOGGER.debug("Forecast estimate from base coordinator: %s", pv_estimate)
            elif zen < 90:
                # Daylight slot with no forecast from either source — the symptom
                # of an empty/unparsed detailedForecast attribute.
                _LOGGER.debug(
                    "No forecast estimate for daylight slot %s (zenith %.1f) from "
                    "either detailedForecast or base coordinator",
                    period_end,
                    zen,
                )
            # Coerce unknown weather to the excluded sentinel for the NOT NULL
            # columns (used by both the aggregate and per-site rows below).
            temp_db, clouds_db, desc_db = self._weather_for_storage()
            record = {
                "period_end": period_end,
                "period_end_epoch": period_epoch,
                "period_start": period_start,
                # Phase 1: still one aggregate row per cycle, tagged with the
                # default site. Per-site rows arrive once per-site measurement +
                # forecast mapping lands (phase 2).
                "site": DEFAULT_SITE_ID,
                "pv_actual": round(pv_actual, 4),
                "pv_export": round(pv_export, 4),
                "pv_estimate": round(pv_estimate, 4),
                "pv_estimate10": round(pv_est10, 4),
                "pv_estimate90": round(pv_est90, 4),
                "azimuth": round(az, 5),
                "zenith": round(zen, 5),
                "temp": temp_db,
                "clouds": clouds_db,
                "description": desc_db,
                "battery_charge": round(battery_charge, 4),
                "dc_voltage1": total_dc[0],
                "dc_current1": total_dc[1],
                "dc_voltage2": total_dc[2],
                "dc_current2": total_dc[3],
                "dc_vmed1": total_vmed[0],
                "dc_vmed2": total_vmed[1],
                **self._irradiance_for_storage(),
            }
            await self._db.async_insert_record(record)

            # Feed the short-horizon confidence advisory (item 3): record this
            # completed daylight slot's measured-vs-forecast pair and recompute how
            # well recent output is tracking the forecast. Daylight only (a non-zero
            # estimate); night/no-forecast slots carry no signal.
            if pv_estimate > 0:
                self._recent_bias.append((period_epoch, round(pv_actual, 4), round(pv_estimate, 4)))
            self._confidence = compute_confidence(
                [(a, e) for (ep, a, e) in self._recent_bias if period_epoch - ep <= RECENT_BIAS_LOOKBACK_S]
            )

            # Per-site rows (multi-site). The property-wide '_total' row above
            # remains the source for aggregate tuning/dampening; per-site rows are
            # additive and only ever read with an explicit site filter (never
            # summed), so the property-wide export is replicated here to drive each
            # site's export-limit clip exclusion. battery stays on '_total' only.
            for site_id, (site_kw, site_start) in site_actuals.items():
                s_start = site_start if site_start else period_epoch - 1800
                s_dc = site_dc.get(site_id, (0.0, 0.0, 0.0, 0.0))
                s_vmed = site_vmed.get(site_id, (0.0, 0.0))
                # Match the forecast on the snapped slot boundary (as above), while
                # period_start below keeps the real per-site measurement window.
                s_est, s_est10, s_est90 = self._site_forecast_for_period(site_id, slot_start_epoch)
                await self._db.async_insert_record(
                    {
                        "period_end": period_end,
                        "period_end_epoch": period_epoch,
                        "period_start": datetime.fromtimestamp(s_start, tz=UTC).isoformat(),
                        "site": site_id,
                        "pv_actual": round(site_kw, 4),
                        "pv_export": round(pv_export, 4),
                        "pv_estimate": round(s_est, 4),
                        "pv_estimate10": round(s_est10, 4),
                        "pv_estimate90": round(s_est90, 4),
                        "azimuth": round(az, 5),
                        "zenith": round(zen, 5),
                        "temp": temp_db,
                        "clouds": clouds_db,
                        "description": desc_db,
                        "battery_charge": 0.0,
                        "dc_voltage1": s_dc[0],
                        "dc_current1": s_dc[1],
                        "dc_voltage2": s_dc[2],
                        "dc_current2": s_dc[3],
                        "dc_vmed1": s_vmed[0],
                        "dc_vmed2": s_vmed[1],
                        # Irradiance is property-wide weather: same values on every site.
                        **self._irradiance_for_storage(),
                    }
                )

                # Surface this array's measured generation (and its forecast) for
                # the per-site PV Power sensor.
                self._site_output[site_id] = {
                    "pv_actual": round(site_kw, 4),
                    "pv_estimate": round(s_est, 4),
                }

                # Per-site confidence advisory: track this array's measured-vs-
                # forecast bias the same way as the property total. Needs the
                # per-site forecast that apportionment now supplies.
                buf = self._site_recent_bias.setdefault(site_id, deque(maxlen=16))
                if s_est > 0:
                    buf.append((period_epoch, round(site_kw, 4), round(s_est, 4)))
                self._site_confidence[site_id] = compute_confidence(
                    [(a, e) for (ep, a, e) in buf if period_epoch - ep <= RECENT_BIAS_LOOKBACK_S]
                )

            self._db_record_count = await self._db.async_get_record_count()
            # Diagnostics: newest slot written this cycle + sites seen in the store.
            self._db_latest_period_end = period_end
            self._db_sites = await self._db.async_get_sites()

        # History retention (daily) — independent of auto-tuning, so it still
        # bounds the table when only logging is enabled.
        retention_days = int(opts.get(CONF_DB_RETENTION_DAYS, DEFAULT_DB_RETENTION_DAYS) or 0)
        if self._db and retention_days > 0 and now_epoch - self._last_prune_ts >= TUNING_INTERVAL_HOURS * 3600:
            if retention_days < DB_RETENTION_MIN_RECOMMENDED_DAYS:
                _LOGGER.warning(
                    "History retention is set to %d days — seasonal dampening uses a "
                    "cross-year window and works best with at least ~%d days of history.",
                    retention_days,
                    DB_RETENTION_MIN_RECOMMENDED_DAYS,
                )
            removed = await self._db.async_prune(retention_days)
            self._last_prune_ts = float(now_epoch)
            if removed:
                _LOGGER.info(
                    "Pruned %d record(s) older than %d days from history.",
                    removed,
                    retention_days,
                )

        # PV tuning (daily)
        if opts.get(CONF_AUTO_TUNING, True):
            elapsed_tuning = now_epoch - self._last_tuning_ts
            if elapsed_tuning >= TUNING_INTERVAL_HOURS * 3600:
                await self._run_tuning(opts)
                self._last_tuning_ts = float(now_epoch)

        # Dampening (every 6 hours)
        if opts.get(CONF_AUTO_DAMPENING, True):
            elapsed_damp = now_epoch - self._last_dampening_ts
            if elapsed_damp >= DAMPENING_INTERVAL_HOURS * 3600:
                await self._run_dampening(opts, now_epoch, lat, lon)
                self._last_dampening_ts = float(now_epoch)

        # Per-cycle summary — the at-a-glance "is it working?" line. One row per
        # half-hour update when debug logging is enabled for the component.
        _LOGGER.debug(
            "Update %s: base=%s pv_actual=%.3fkW pv_export=%.3fkW est=%.3fkW "
            "clouds=%s%% battery=%.3f sites=%d db_rows=%s",
            datetime.fromtimestamp(period_epoch, tz=UTC).isoformat(),
            self._base_status,
            pv_actual,
            pv_export,
            pv_estimate,
            self._weather.get("clouds"),
            battery_charge,
            len(self._sites),
            self._db_record_count,
        )

        return {
            "pv_actual": pv_actual,
            "pv_export": pv_export,
            "battery_charge": battery_charge,
            "forecast_now": forecast_now,
            "forecast_today": forecast_today,
            "weather": self._weather,
            "tuning": self._tuning_result,
            "dampening_table": self._dampening_table,
            "dampening_gated": self._dampening_gated,
            "db_records": self._db_record_count,
            "db_latest_period_end": self._db_latest_period_end,
            "db_sites": self._db_sites,
            "base_status": self._base_status,
            "dc_telemetry": self._dc_telemetry,
        }

    # ------------------------------------------------------------------
    # PV Tuning
    # ------------------------------------------------------------------

    def _clearsky_gate_kwargs(self, opts: dict[str, Any]) -> dict[str, Any]:
        """Clear-sky gate kwargs for ``async_get_records_for_tuning``.

        Prefer the measured clearness index (Kt) when Open-Meteo irradiance is
        enabled; otherwise fall back to the OWM total-cloud gate.
        """
        if opts.get(CONF_OPENMETEO_ENABLED, DEFAULT_OPENMETEO_ENABLED):
            return {
                "kt_threshold": float(opts.get(CONF_KT_THRESHOLD, DEFAULT_KT_THRESHOLD)),
                "kt_zenith_max": KT_ZENITH_MAX,
                "kt_ghi_cs_floor": KT_GHI_CS_FLOOR,
            }
        return {"cloud_max": int(opts.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD))}

    def _tuning_cloud_threshold(self, opts: dict[str, Any]) -> int:
        """Cloud threshold passed to ``run_tuning``'s internal cloud filter.

        When the SQL Kt gate already selected clear-sky rows, the in-tuning cloud
        re-filter is redundant and would wrongly drop them when OWM is absent (then
        ``clouds`` is the 100% sentinel). A value above 100 disables it.
        """
        if opts.get(CONF_OPENMETEO_ENABLED, DEFAULT_OPENMETEO_ENABLED):
            return 101
        return int(opts.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD))

    async def _run_tuning(self, opts: dict[str, Any]) -> None:
        if not self._db:
            return
        # Aggregate tuning operates on the property-wide '_total' rows so it never
        # double-counts the additive per-site rows. Pull the most recent *clear-sky*
        # rows (filter in SQL before the LIMIT) so tuning fits orientation-relevant
        # data spanning all seasons, not just a recent cloudy window.
        records = await self._db.async_get_records_for_tuning(site=DEFAULT_SITE_ID, **self._clearsky_gate_kwargs(opts))
        if not records:
            _LOGGER.debug("PV tuning skipped: no usable records yet")
            return
        # Prefer the base integration's property-wide export limit; fall back to
        # the manual option when the base hasn't set one.
        export_limit = self._read_base_export_limit()
        if export_limit is None:
            export_limit = float(opts.get(CONF_EXPORT_LIMIT_KW, DEFAULT_EXPORT_LIMIT_KW))
        albedo = float(opts.get(CONF_ALBEDO, DEFAULT_ALBEDO))
        result = await self.hass.async_add_executor_job(
            run_tuning,
            records,
            float(opts.get(CONF_CAPACITY_KW, 5.0)),
            self._tuning_cloud_threshold(opts),
            float(opts.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD)),
            export_limit,
            # Azimuth is held fixed at the configured value (not tuned — it is
            # non-identifiable here). CONF_AZIMUTH is in the Solcast/base convention
            # (West-positive); convert to the internal solar frame the tuner uses.
            panel_azimuth_to_internal(opts.get(CONF_AZIMUTH, 0.0)),
            albedo,
        )
        if result:
            self._tuning_result = result
            _LOGGER.debug("PV tuning result: %s", result)

        # Per-site tuning (multi-site): tune each individually-measured array.
        await self._run_site_tuning(opts, export_limit)

    async def _run_site_tuning(self, opts: dict[str, Any], export_limit: float) -> None:
        """Tune tilt/azimuth per individually-measured site.

        Each configured site (single-site group, or a DC-apportioned string) is
        tuned against its own ``site``-filtered rows, seeded from the Solcast
        orientation discovered for that site. Results are keyed by resource_id and
        surfaced via ``tuning_extra['per_site']``.
        """
        groups = opts.get(CONF_SITE_GROUPS) or []
        site_ids = self._configured_site_ids(groups)
        if not self._db or not site_ids:
            return
        cloud_threshold = self._tuning_cloud_threshold(opts)
        gate_kwargs = self._clearsky_gate_kwargs(opts)
        clipping_threshold = float(opts.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD))
        by_id = {s["resource_id"]: s for s in self._sites}
        results: dict[str, dict[str, Any]] = {}
        for site_id in site_ids:
            records = await self._db.async_get_records_for_tuning(site=site_id, **gate_kwargs)
            if not records:
                continue
            site = by_id.get(site_id, {})
            capacity = site.get("capacity") or float(opts.get(CONF_CAPACITY_KW, 5.0))
            # Azimuth fixed at this site's configured orientation (not tuned).
            fixed_az = self._site_azimuth_seed(site, opts)
            albedo = float(opts.get(CONF_ALBEDO, DEFAULT_ALBEDO))
            result = await self.hass.async_add_executor_job(
                run_tuning,
                records,
                float(capacity),
                cloud_threshold,
                clipping_threshold,
                export_limit,
                float(fixed_az),
                albedo,
            )
            if result:
                result["resource_id"] = site_id
                result["name"] = site.get("name")
                results[site_id] = result
        if results:
            self._site_tuning_results = results
            _LOGGER.debug("Per-site tuning results: %s", results)

    @staticmethod
    def _configured_site_ids(groups: list[dict[str, Any]]) -> list[str]:
        """All resource_ids that are individually measured (thus tunable)."""
        ids: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for s in group.get("strings") or []:
                sid = s.get("site")
                if sid and sid not in seen:
                    seen.add(sid)
                    ids.append(sid)
            sid = group.get("site")
            if sid and sid not in seen:
                seen.add(sid)
                ids.append(sid)
        return ids

    @staticmethod
    def _site_azimuth_seed(site: dict[str, Any], opts: dict[str, Any]) -> float:
        """Panel-azimuth seed in the tuner's frame (0=N, 90=E), mapped to ±180.

        Solcast ``compass_degrees`` is already 0=N/90=E (0–360); the raw Solcast
        ``azimuth`` is north-zero/east-negative, so compass = (−azimuth) mod 360.
        """
        compass = site.get("compass_degrees")
        if not compass:
            az = site.get("azimuth")
            compass = (-float(az)) % 360 if az not in (None, 0, 0.0) else None
        if compass is None:
            # Manual CONF_AZIMUTH is in the Solcast convention — convert to the
            # internal frame, matching the base-derived branch above.
            return panel_azimuth_to_internal(opts.get(CONF_AZIMUTH, 0.0))
        compass = float(compass) % 360
        return compass - 360 if compass > 180 else compass

    # ------------------------------------------------------------------
    # Dampening convergence gate
    # ------------------------------------------------------------------

    def _weather_for_storage(self) -> tuple[float, int, str]:
        """Coerce weather for the NOT NULL DB columns.

        Unknown (``None`` — no OWM, or a failed fetch) becomes the *excluded*
        100%-cloud / 0 °C sentinel so a record written without cloud data can never
        pass the clear-sky filter as clear. Used by both the aggregate ``_total``
        and per-site rows.
        """
        w_temp = self._weather.get("temp")
        w_clouds = self._weather.get("clouds")
        temp = round(w_temp, 2) if w_temp is not None else 0.0
        clouds = 100 if w_clouds is None else int(w_clouds)
        return temp, clouds, self._weather.get("description") or "unavailable"

    def _irradiance_for_storage(self) -> dict[str, float]:
        """Round GHI/DNI/DHI for the NOT NULL columns.

        Unknown (no Open-Meteo or a failed fetch) is stored as 0 — for a daytime row
        that reads as "no irradiance" and is simply skipped by the transposition tuner.
        """
        return {k: round(v, 2) if v is not None else 0.0 for k, v in self._irradiance.items()}

    @staticmethod
    def _angle_diff(a: float, b: float) -> float:
        """Smallest signed difference a−b on the circle, in (−180, 180]."""
        return ((a - b + 180.0) % 360.0) - 180.0

    def _orientation_diverged(
        self, tuning_result: dict[str, Any] | None, seed_tilt: float, seed_az: float
    ) -> dict[str, float] | None:
        """Return divergence info when confident tuning disagrees with the configured orientation.

        Confident here means the tuned tilt/azimuth differs materially from the
        configured (Solcast) one; otherwise returns ``None``.
        This is the dampening gate's trigger: a confident tuned tilt/azimuth that
        disagrees with the configured site means the Solcast forecast is built on
        the wrong geometry, so its actual/estimate ratio mixes orientation error
        with shading. Holding dampening neutral until they agree keeps the curve
        meaning "shading" (the notebook 3.4b tuned-estimate prerequisite).
        """
        if not tuning_result:
            return None
        if int(tuning_result.get("n_records", 0)) < DAMPENING_GATE_MIN_RECORDS:
            return None  # not enough clear-sky data to trust the divergence
        d_tilt = abs(float(tuning_result["tilt"]) - seed_tilt)
        d_az = abs(self._angle_diff(float(tuning_result["azimuth"]), seed_az))
        if d_tilt > DAMPENING_GATE_TILT_TOL or d_az > DAMPENING_GATE_AZIMUTH_TOL:
            return {"tilt_delta": round(d_tilt, 1), "azimuth_delta": round(d_az, 1)}
        return None

    def _site_orientation_seed(self, site_id: str, opts: dict[str, Any]) -> tuple[float, float]:
        """Return a site's (tilt, azimuth) seed in the tuner frame.

        Matches the seeds used by ``_run_site_tuning`` so the gate compares like with like.
        """
        site = next((s for s in self._sites if s.get("resource_id") == site_id), None)
        if site is None:
            return float(opts.get(CONF_TILT, 20.0)), float(opts.get(CONF_AZIMUTH, 0.0))
        tilt = site.get("tilt") or float(opts.get(CONF_TILT, 20.0))
        return float(tilt), self._site_azimuth_seed(site, opts)

    # ------------------------------------------------------------------
    # Dampening
    # ------------------------------------------------------------------

    async def _run_dampening(self, opts: dict[str, Any], now_epoch: int, lat: float, lon: float) -> None:
        # Aggregate table (drives the dampening sensors) — property-wide '_total' rows.
        self._dampening_table = await self._compute_dampening_slots(opts, now_epoch, lat, lon, DEFAULT_SITE_ID)

        # The base integration rejects manual set_dampening while its own
        # automatic dampening is enabled (ServiceValidationError). Skip the push
        # in that case — this integration can't drive dampening until the base's
        # auto-dampening is turned off.
        if self._read_base_auto_dampen():
            if not self._auto_dampen_warned:
                _LOGGER.warning(
                    "Base integration has automatic dampening enabled — skipping "
                    "dampening push. Turn off 'automatic dampening' in the Solcast "
                    "PV Forecast integration to let Solcast Solar Enhanced apply its "
                    "factors (or disable auto dampening here)."
                )
                self._auto_dampen_warned = True
            return
        self._auto_dampen_warned = False

        # Convergence gate: when a tuned orientation diverges materially from the
        # configured one, hold that target's dampening at neutral 1.0 rather than
        # push an orientation-contaminated curve. Per-site aware. Disable with
        # CONF_DAMPENING_GATE.
        gate_on = opts.get(CONF_DAMPENING_GATE, DEFAULT_DAMPENING_GATE)
        any_gated = False

        site_ids = self._configured_site_ids(opts.get(CONF_SITE_GROUPS) or [])
        if site_ids:
            # Multi-site: push a dampening set per site (which overrides the base's
            # global dampening for that site). The conflicting global push is
            # skipped so per-site factors are not overwritten.
            for site_id in site_ids:
                slots = await self._compute_dampening_slots(opts, now_epoch, lat, lon, site_id)
                self._site_dampening_tables[site_id] = slots
                hourly = average_slot_pairs([s["factor"] for s in slots])
                if gate_on:
                    seed_tilt, seed_az = self._site_orientation_seed(site_id, opts)
                    div = self._orientation_diverged(self._site_tuning_results.get(site_id), seed_tilt, seed_az)
                    if div:
                        any_gated = True
                        _LOGGER.warning(
                            "Dampening gated for site %s: tuned tilt diverges from "
                            "configured (Δtilt %.0f°) — pushing neutral 1.0. Apply the "
                            "Tuned Panel Tilt value in your Solcast account.",
                            site_id,
                            div["tilt_delta"],
                        )
                        hourly = [1.0] * len(hourly)
                await self._push_dampening(hourly, site=site_id)
        else:
            hourly = average_slot_pairs([s["factor"] for s in self._dampening_table])
            if gate_on:
                div = self._orientation_diverged(
                    self._tuning_result,
                    float(opts.get(CONF_TILT, 20.0)),
                    # Compare in the internal frame the tuned result is stored in.
                    panel_azimuth_to_internal(opts.get(CONF_AZIMUTH, 0.0)),
                )
                if div:
                    any_gated = True
                    _LOGGER.warning(
                        "Dampening gated: tuned tilt diverges from configured "
                        "(Δtilt %.0f°) — pushing neutral 1.0. Apply the Tuned Panel "
                        "Tilt value in your Solcast account.",
                        div["tilt_delta"],
                    )
                    hourly = [1.0] * len(hourly)
            await self._push_dampening(hourly)

        self._dampening_gated = any_gated
        if any_gated:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_DAMPENING_GATED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_DAMPENING_GATED,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_DAMPENING_GATED)

    async def _compute_dampening_slots(
        self,
        opts: dict[str, Any],
        now_epoch: int,
        lat: float,
        lon: float,
        site: str,
    ) -> list[dict[str, Any]]:
        """Compute the 48 half-hour dampening slots for one site (or '_total').

        The slot grid is built on **local** time-of-day so slot index ``i`` maps to
        the local half-hour the base integration applies ``damp_factor[i]`` to (its
        ``dampen.py`` converts each forecast period to the site timezone before
        indexing). Each local slot time is converted to its UTC instant for
        ``solar_position``. Building the array on UTC instead would shift the whole
        dampening curve by the site's UTC offset for non-UTC users.
        """
        capacity_kw = float(opts.get(CONF_CAPACITY_KW, 5.0))
        cloud_threshold = int(opts.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD))
        cloud_max_include = int(opts.get(CONF_CLOUD_MAX_INCLUDE, DEFAULT_CLOUD_MAX_INCLUDE))
        clipping_threshold = float(opts.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD))
        # Clear-sky basis for the per-record quality weight. Prefer measured Kt when
        # Open-Meteo irradiance is on (the cloud field is unreliable); fall back to
        # the OWM cloud bands otherwise. Mirrors the tuning gate's preference.
        use_kt = bool(opts.get(CONF_OPENMETEO_ENABLED, DEFAULT_OPENMETEO_ENABLED))
        kt_threshold = float(opts.get(CONF_KT_THRESHOLD, DEFAULT_KT_THRESHOLD)) if use_kt else None
        clear_sky_basis = "kt" if use_kt else "cloud"
        # Export limit for curtailment-aware forecast clipping — prefer the base's
        # site_export_limit, fall back to the manual option (0 = disabled). Same
        # source the tuner uses, so dampening and tuning agree on the ceiling.
        export_limit = self._read_base_export_limit()
        if export_limit is None:
            export_limit = float(opts.get(CONF_EXPORT_LIMIT_KW, DEFAULT_EXPORT_LIMIT_KW))

        tz = dt_util.get_time_zone(self.hass.config.time_zone) or UTC
        now_local = datetime.fromtimestamp(now_epoch, tz=tz)
        slot_results: list[dict[str, Any]] = []

        # All 48 slots share the same calendar day (only the time-of-day varies),
        # so the day-of-year window query is identical across them. Fetch the
        # records once instead of re-running the full-table strftime scan per slot.
        slot_doy = now_local.timetuple().tm_yday
        records: list[dict[str, Any]] = []
        if self._db:
            records = await self._db.async_get_records_for_dampening(slot_doy, site=site)

        for slot in range(48):
            hour, minute = divmod(slot * 30, 60)
            slot_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            slot_epoch = int(slot_local.timestamp())

            # Sun position at the slot midpoint (+15 min), matching the stored
            # records' midpoint convention for geometric weighting.
            az_slot, zen_slot = solar_position(slot_epoch + 900, lat, lon)

            # Night slots — factor = 1.0
            if zen_slot >= 90:
                slot_results.append(
                    {
                        "factor": 1.0,
                        "alpha": 0.0,
                        "source": "night",
                        "clear_sky_basis": clear_sky_basis,
                        "quality_records": 0.0,
                        "avg_quality": 0.0,
                        "clipped_excluded": 0,
                        "forecast_clipped": 0,
                    }
                )
                continue

            slot_result = compute_dampening(
                records=records,
                capacity_kw=capacity_kw,
                cloud_threshold=cloud_threshold,
                cloud_max_include=cloud_max_include,
                clipping_threshold=clipping_threshold,
                target_zenith=zen_slot,
                target_azimuth=az_slot,
                export_limit_kw=export_limit,
                kt_threshold=kt_threshold,
            )
            slot_results.append(slot_result)

        return slot_results

    async def _push_dampening(self, hourly_factors: list[float], site: str | None = None) -> None:
        """Push factors to the base integration's ``set_dampening`` service.

        The base expects ``damp_factor`` as a comma-separated string of 24 (hourly)
        or 48 (half-hourly) values, with an optional ``site`` (resource_id) to
        target a single site.
        """
        try:
            # The base integration's set_dampening only accepts factors in
            # [0.0, 1.0]: dampening can attenuate a forecast, never boost it. A
            # computed factor > 1.0 means the measured output exceeds the Solcast
            # forecast for that hour (the forecast under-predicts) — we cannot ask
            # Solcast to boost, so clamp to 1.0 (no dampening). The unclamped value
            # is kept in the dampening sensor attributes for diagnostics.
            clamped = [min(1.0, max(0.0, f)) for f in hourly_factors]
            n_clamped = sum(1 for c, f in zip(clamped, hourly_factors, strict=True) if c != f)
            if n_clamped:
                _LOGGER.debug(
                    "Clamped %d dampening factor(s) outside [0,1] before push%s "
                    "(forecast under-/over-shoots those hours)",
                    n_clamped,
                    f" for site {site}" if site else "",
                )
            damp_factor = ",".join(f"{round(c, 4)}" for c in clamped)
            data: dict[str, Any] = {"damp_factor": damp_factor}
            if site:
                data["site"] = site
            # blocking=True so a base-side ServiceValidationError surfaces here
            # and is handled, rather than leaking into Home Assistant's core log.
            await self.hass.services.async_call(BASE_DOMAIN, "set_dampening", data, blocking=True)
            _LOGGER.debug(
                "Pushed %d dampening factors%s",
                len(hourly_factors),
                f" for site {site}" if site else " (global)",
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Failed to push dampening factors: %s", exc)

    # ------------------------------------------------------------------
    # Forced service methods
    # ------------------------------------------------------------------

    async def async_force_pv_tuning(self) -> None:
        """Run PV tuning immediately (service handler)."""
        opts = {**self._entry.data, **self._entry.options}
        await self._run_tuning(opts)
        self.async_set_updated_data(self.data or {})

    async def async_force_dampening_update(self) -> None:
        """Recompute and push dampening immediately (service handler)."""
        opts = {**self._entry.data, **self._entry.options}
        now_epoch = normalize_epoch(time.time())
        lat = float(opts.get(CONF_LATITUDE, -37.9))
        lon = float(opts.get(CONF_LONGITUDE, 145.0))
        await self._run_dampening(opts, now_epoch, lat, lon)
        self.async_set_updated_data(self.data or {})

    async def async_force_fetch_weather(self) -> None:
        """Fetch weather immediately (service handler)."""
        if self._owm:
            self._weather = await self._owm.async_fetch()
        self.async_set_updated_data(self.data or {})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_base_coordinator(self) -> Any | None:
        return self.hass.data.get(BASE_DOMAIN)

    @staticmethod
    def _snap_to_half_hour(epoch: int) -> int:
        """Round a Unix epoch (UTC) to the nearest :00/:30 half-hour boundary.

        Solcast forecasts are bucketed on half-hour boundaries (in UTC), so
        snapping the stored ``period_end`` aligns each row to a clean slot and
        lets the ``(period_end_epoch, site)`` unique key coalesce repeated writes
        that fall in the same slot (e.g. a poll shortly after a restart).
        """
        return ((int(epoch) + 900) // 1800) * 1800

    def _discover_sites(self) -> list[dict[str, Any]]:
        """Discover Solcast sites from the base integration's RooftopSensors."""
        return discover_sites(self.hass)

    @property
    def sites(self) -> list[dict[str, Any]]:
        """Discovered Solcast sites (empty when single-site / not detected)."""
        return self._sites

    def _read_site_actuals(self, opts: dict[str, Any], now_epoch: int) -> dict[str, tuple[float, int | None]]:
        """Compute each site's measured generation (average kW) from the group config.

        Config model — ``opts[CONF_SITE_GROUPS]`` is a list of measurement groups::

            {
              "ac_sensor": "sensor.inverter_ac_power",
              "ac_mode": "auto",                 # optional power/energy mode
              "site": "<resource_id>",           # single-site group (no DC split)
              "strings": [                        # optional: DC-ratio apportionment
                {"site": "<rid>", "dc_sensor": "sensor.mppt1", "dc_mode": "auto"},
                {"site": "<rid>", "dc_sensor": "sensor.mppt2"},
              ],
            }

        For an apportioned group the measured AC is split across its sites by each
        string's share of total DC: ``ac_kw × dc_i / Σ dc``. Returns a mapping of
        ``resource_id → (pv_actual_kw, interval_start_epoch)``; empty when no groups
        are configured.
        """
        out: dict[str, tuple[float, int | None]] = {}
        groups = opts.get(CONF_SITE_GROUPS) or []
        for gi, group in enumerate(groups):
            ac_sensor = group.get("ac_sensor")
            if not ac_sensor:
                continue
            ac_kw, ac_start = self._read_pv_value(
                ac_sensor,
                group.get("ac_mode", DEFAULT_PV_INPUT_MODE),
                f"group{gi}:ac",
                now_epoch,
            )
            strings = group.get("strings") or []
            if strings:
                dc_vals: dict[str, float] = {}
                for s in strings:
                    site = s.get("site")
                    dc_sensor = s.get("dc_sensor")
                    if not site or not dc_sensor:
                        continue
                    val, _ = self._read_pv_value(
                        dc_sensor,
                        s.get("dc_mode", DEFAULT_PV_INPUT_MODE),
                        f"group{gi}:dc:{site}",
                        now_epoch,
                    )
                    dc_vals[site] = val
                total_dc = sum(dc_vals.values())
                for site, val in dc_vals.items():
                    frac = (val / total_dc) if total_dc > 0 else 0.0
                    out[site] = (ac_kw * frac, ac_start)
            else:
                site = group.get("site")
                if site:
                    out[site] = (ac_kw, ac_start)
        return out

    @staticmethod
    def _sum_site_actuals(site_actuals: dict[str, tuple[float, int | None]]) -> tuple[float, int | None]:
        """Sum per-site generation into a property total for the ``_total`` row.

        Returns ``(total_kw, earliest_start_epoch)``; the start is the earliest
        non-``None`` per-site interval start (``None`` when every site read is an
        averaged-power reading, which carries no start). Used to populate the
        aggregate row when no whole-system generation sensor is configured.
        """
        total = sum(kw for kw, _ in site_actuals.values())
        starts = [s for _, s in site_actuals.values() if s]
        return total, (min(starts) if starts else None)

    def _read_numeric_state(self, entity_id: str | None) -> float | None:
        """Read a plain numeric sensor state (e.g. DC volts / amps).

        Returns ``None`` when the entity is unset, missing, or non-numeric — the
        caller treats that as "no telemetry" rather than a zero reading.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _mppt_list_from_opts(opts: dict[str, Any]) -> list[dict[str, Any]]:
        """Property-wide / single-inverter MPPT pairs from the flat site-step keys."""
        return [
            {
                "voltage_sensor": opts.get(CONF_MPPT1_VOLTAGE_SENSOR),
                "current_sensor": opts.get(CONF_MPPT1_CURRENT_SENSOR),
            },
            {
                "voltage_sensor": opts.get(CONF_MPPT2_VOLTAGE_SENSOR),
                "current_sensor": opts.get(CONF_MPPT2_CURRENT_SENSOR),
            },
        ]

    def _collect_dc_entities(self, opts: dict[str, Any]) -> set[str]:
        """Return every configured MPPT voltage/current entity.

        Collected for one batched history query per cycle.
        """
        ids: set[str] = set()

        def _add(mppts: list[dict[str, Any]] | None) -> None:
            for m in mppts or []:
                for k in ("voltage_sensor", "current_sensor"):
                    if m.get(k):
                        ids.add(m[k])

        _add(self._mppt_list_from_opts(opts))
        for group in opts.get(CONF_SITE_GROUPS) or []:
            _add(group.get("mppts"))
            for s in group.get("strings") or []:
                _add(s.get("mppts"))
        return ids

    async def _interval_values(self, entity_ids: set[str], start_epoch: int, end_epoch: int) -> dict[str, list[float]]:
        """Recorded numeric values per entity over ``[start, end]`` from the recorder.

        One batched ``get_significant_states`` (all states, no attributes) run on
        the recorder executor. Returns ``{}`` when the recorder is unavailable or
        errors — callers then fall back to the instantaneous state, so capture
        degrades gracefully rather than failing.
        """
        ids = [e for e in entity_ids if e]
        if not ids:
            return {}
        try:
            from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        except ImportError:
            return {}
        start = datetime.fromtimestamp(start_epoch, tz=UTC)
        end = datetime.fromtimestamp(end_epoch, tz=UTC)

        def _job() -> dict[str, Any]:
            return history.get_significant_states(
                self.hass,
                start,
                end,
                entity_ids=ids,
                significant_changes_only=False,
                no_attributes=True,
            )

        try:
            raw = await get_instance(self.hass).async_add_executor_job(_job)
        except Exception as exc:  # noqa: BLE001 — recorder may be disabled/not ready
            _LOGGER.debug("DC interval history unavailable: %s", exc)
            return {}
        out: dict[str, list[float]] = {}
        for eid, states in (raw or {}).items():
            vals: list[float] = []
            for st in states:
                try:
                    vals.append(float(st.state))
                except (TypeError, ValueError):
                    continue  # 'unknown'/'unavailable' between real readings
            if vals:
                out[eid] = vals
        return out

    def _interval_extreme(self, entity_id: str | None, mode: str, hist: dict[str, list[float]]) -> float | None:
        """Return the extreme reading over the interval, or ``None`` if unreadable.

        ``max`` (voltage) or ``min`` (current) over the interval's recorded values
        plus the current instantaneous reading. Max-voltage / min-current catch a
        mid-slot off-MPP excursion that a single boundary sample would miss.
        """
        if not entity_id:
            return None
        vals = list(hist.get(entity_id, ()))
        inst = self._read_numeric_state(entity_id)
        if inst is not None:
            vals.append(inst)
        if not vals:
            return None
        return max(vals) if mode == "max" else min(vals)

    def _interval_median(self, entity_id: str | None, hist: dict[str, list[float]]) -> float | None:
        """Median recorded value over the interval — the representative operating point.

        Unlike ``_interval_extreme``'s max-V/min-I (off-MPP curtailment capture), the
        median is the value the tracker held for most of the slot — the operating
        voltage that separates uniform dimming (holds near Vmp) from a bypass/partial
        shadow (collapses). Falls back to the instantaneous read when no history exists.
        """
        if not entity_id:
            return None
        vals = hist.get(entity_id)
        if vals:
            return statistics.median(vals)
        return self._read_numeric_state(entity_id)

    def _read_mppt_vmed(self, mppts: list[dict[str, Any]] | None, hist: dict[str, list[float]]) -> tuple[float, float]:
        """Per-tracker median operating voltage ``(vmed1, vmed2)`` over the slot, zero-filled.

        Forward-only groundwork for the shading-mechanism classifier; a tracker with
        no voltage sensor (or none configured) reads 0.0.
        """
        pairs = list(mppts or [])[:MAX_MPPT_TRACKERS]
        out: list[float] = []
        for i in range(MAX_MPPT_TRACKERS):
            m = pairs[i] if i < len(pairs) else {}
            v = self._interval_median(m.get("voltage_sensor"), hist)
            out.append(round(v or 0.0, 3))
        return out[0], out[1]

    def _read_site_vmed(self, opts: dict[str, Any], hist: dict[str, list[float]]) -> dict[str, tuple[float, float]]:
        """Per-site median operating voltage ``site → (vmed1, vmed2)``; sites with no DC V absent."""
        out: dict[str, tuple[float, float]] = {}

        def _capture(site: str | None, cfg: dict[str, Any]) -> None:
            if not site:
                return
            vm = self._read_mppt_vmed(cfg.get("mppts"), hist)
            if any(vm):
                out[site] = vm

        for group in opts.get(CONF_SITE_GROUPS) or []:
            _capture(group.get("site"), group)
            for s in group.get("strings") or []:
                _capture(s.get("site"), s)
        return out

    def _read_mppt_telemetry(
        self, mppts: list[dict[str, Any]] | None, hist: dict[str, list[float]]
    ) -> tuple[float, float, float, float] | None:
        """Aggregate up to ``MAX_MPPT_TRACKERS`` paired (voltage, current) trackers.

        Aggregated over the interval (max V / min I per ``hist``).
        Returns a flat ``(v1, i1, v2, i2)`` tuple, zero-filled and padded to
        ``MAX_MPPT_TRACKERS`` pairs, or ``None`` when no tracker sensor is
        *configured* at all (so a site without DC telemetry stays absent). A
        configured-but-unreadable sensor yields 0.0 (e.g. amps at night), a real
        value — pairs are kept per-tracker (not aggregated across trackers) so a
        later Vmp-band calibrator can learn each string.
        """
        pairs = list(mppts or [])[:MAX_MPPT_TRACKERS]
        if not any(m.get("voltage_sensor") or m.get("current_sensor") for m in pairs):
            return None
        flat: list[float] = []
        for i in range(MAX_MPPT_TRACKERS):
            m = pairs[i] if i < len(pairs) else {}
            v = self._interval_extreme(m.get("voltage_sensor"), "max", hist)
            c = self._interval_extreme(m.get("current_sensor"), "min", hist)
            flat.extend([round(v or 0.0, 3), round(c or 0.0, 3)])
        return tuple(flat)  # type: ignore[return-value]

    @staticmethod
    def _dc_telemetry_summary(
        total: tuple[float, float, float, float],
        sites: dict[str, tuple[float, float, float, float]],
    ) -> dict[str, Any]:
        """Shape the captured DC telemetry for the diagnostic sensor."""

        def _pairs(t: tuple[float, float, float, float]) -> dict[str, float]:
            return {
                "mppt1_voltage": t[0],
                "mppt1_current": t[1],
                "mppt2_voltage": t[2],
                "mppt2_current": t[3],
            }

        # max_voltage spans the property-wide trackers AND every per-site tracker,
        # so the diagnostic stays meaningful for multi-site systems (where the flat
        # property-wide MPPT fields are unset — each array maps its own trackers).
        voltages = [total[0], total[2]]
        for t in sites.values():
            voltages += [t[0], t[2]]
        return {
            **_pairs(total),
            "max_voltage": max(voltages),
            "sites": {s: _pairs(t) for s, t in sites.items()},
        }

    def _read_site_dc_telemetry(
        self, opts: dict[str, Any], hist: dict[str, list[float]]
    ) -> dict[str, tuple[float, float, float, float]]:
        """Per-site MPPT DC telemetry for curtailment-detection capture.

        Aggregates each site's ``mppts`` list (paired voltage/current trackers,
        from its single-site group or apportioned string in ``CONF_SITE_GROUPS``)
        over the interval via ``hist``. Returns ``site → (v1, i1, v2, i2)``; sites
        with no configured tracker are absent. Banked now for a later off-MPP
        detector.
        """
        out: dict[str, tuple[float, float, float, float]] = {}

        def _capture(site: str | None, cfg: dict[str, Any]) -> None:
            if not site:
                return
            t = self._read_mppt_telemetry(cfg.get("mppts"), hist)
            if t is not None:
                out[site] = t

        for group in opts.get(CONF_SITE_GROUPS) or []:
            _capture(group.get("site"), group)  # single-site group
            for s in group.get("strings") or []:  # apportioned per-MPPT strings
                _capture(s.get("site"), s)
        return out

    def _total_forecast_for_period(self, start_epoch: int) -> tuple[float, float, float]:
        """Return (pv_estimate, pv_estimate10, pv_estimate90) for the property total.

        Reads the property-wide ``detailedForecast`` attribute off the base
        ``forecast_today`` sensor and picks the slot matching the measured
        interval's start — the same documented source the per-site path uses, just
        without a site suffix. Preferred over the base coordinator's in-memory
        ``pv_estimate`` key (which newer base versions don't expose), so the
        ``_total`` row gets a real forecast instead of zeros.
        """
        return self._forecast_slot("detailedForecast", start_epoch)

    def _site_forecast_for_period(self, resource_id: str, start_epoch: int) -> tuple[float, float, float]:
        """Return (pv_estimate, pv_estimate10, pv_estimate90) for a site's slot.

        Reads the per-site ``detailedForecast-<resource_id>`` attribute off the base
        ``forecast_today`` sensor. Base versions vary in how they key the attribute, so
        three forms are tried in order: the hyphenated id, an underscore separator with
        the hyphenated id, and — what current base versions actually emit — an
        underscore separator with the id's own hyphens replaced by underscores
        (e.g. ``detailedForecast_8be0_533e_baad_4841``).
        """
        est = self._forecast_slot(f"detailedForecast-{resource_id}", start_epoch)
        if est == (0.0, 0.0, 0.0):
            est = self._forecast_slot(f"detailedForecast_{resource_id}", start_epoch)
        if est == (0.0, 0.0, 0.0):
            rid_underscored = resource_id.replace("-", "_")
            if rid_underscored != resource_id:
                est = self._forecast_slot(f"detailedForecast_{rid_underscored}", start_epoch)
        if est != (0.0, 0.0, 0.0):
            return est
        # The base integration exposes no per-site detailedForecast on this install,
        # so per-site pv_estimate would be zero and per-site dampening could never
        # form a ratio. Fall back to apportioning the property-wide forecast by this
        # site's capacity share (item 1 P0) — valid only at a shared orientation.
        return self._apportion_total_forecast(resource_id, start_epoch)

    def _apportion_total_forecast(self, resource_id: str, start_epoch: int) -> tuple[float, float, float]:
        """Per-site forecast from the property total, split by capacity share.

        Capacity-share apportionment of a half-hourly forecast assumes the same
        forecast-per-kW shape across arrays, which holds only when they share an
        azimuth. When azimuths diverge beyond ``APPORTION_AZIMUTH_TOL`` the arrays
        peak at different times, so a per-slot split would invent phantom timing
        differences and corrupt per-site dampening — apportionment is skipped and
        the per-site forecast stays unset (zeros), the pre-existing behaviour.

        Returns the apportioned ``(pv_estimate, pv_estimate10, pv_estimate90)`` (avg
        kW over the half-hour), or zeros when apportionment is unavailable or unsafe.
        """
        sites = self._sites
        if len(sites) < 2:
            return 0.0, 0.0, 0.0
        site = next((s for s in sites if s.get("resource_id") == resource_id), None)
        if site is None:
            return 0.0, 0.0, 0.0
        azimuths = [float(s.get("azimuth") or 0.0) for s in sites]
        if _azimuth_spread(azimuths) > APPORTION_AZIMUTH_TOL:
            _LOGGER.debug(
                "Per-site forecast apportionment skipped: array azimuths diverge by %.0f° (> %.0f°)",
                _azimuth_spread(azimuths),
                APPORTION_AZIMUTH_TOL,
            )
            return 0.0, 0.0, 0.0
        total_cap = sum(float(s.get("capacity") or 0.0) for s in sites)
        site_cap = float(site.get("capacity") or 0.0)
        if total_cap <= 0.0 or site_cap <= 0.0:
            return 0.0, 0.0, 0.0
        total = self._total_forecast_for_period(start_epoch)
        if total == (0.0, 0.0, 0.0):
            return 0.0, 0.0, 0.0
        share = site_cap / total_cap
        return total[0] * share, total[1] * share, total[2] * share

    def _forecast_slot(self, attr_name: str, start_epoch: int) -> tuple[float, float, float]:
        """Pick the ``detailedForecast`` slot closest to ``start_epoch``.

        ``attr_name`` selects the property-wide (``detailedForecast``) or per-site
        (``detailedForecast-<resource_id>``) series on the base ``forecast_today``
        sensor. Values are already average kW over the half-hour, matching
        ``pv_actual``. Returns zeros if the attribute is absent or no slot falls
        within half a slot (900 s) of the measured interval's start.
        """
        state = self.hass.states.get("sensor.solcast_pv_forecast_forecast_today")
        if state is None:
            return 0.0, 0.0, 0.0
        series = state.attributes.get(attr_name)
        if not series:
            return 0.0, 0.0, 0.0

        best: dict[str, Any] | None = None
        best_delta: float | None = None
        for entry in series:
            ts = self._period_start_epoch(entry.get("period_start"))
            if ts is None:
                continue
            delta = abs(ts - start_epoch)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = entry
        if best is None or best_delta is None or best_delta > 900:
            # The base integration stores period_start as datetime objects, not
            # ISO strings, in the in-memory attribute; a parse miss here silently
            # zero-fills the forecast columns, so log it loudly when a populated
            # series produces no usable slot.
            _LOGGER.debug(
                "%s: no forecast slot within 900s of %s (%d entries)",
                attr_name,
                start_epoch,
                len(series),
            )
            return 0.0, 0.0, 0.0

        def _f(key: str) -> float:
            try:
                return float(best.get(key, 0) or 0)
            except (ValueError, TypeError):
                return 0.0

        return _f("pv_estimate"), _f("pv_estimate10"), _f("pv_estimate90")

    @staticmethod
    def _period_start_epoch(period_start: Any) -> float | None:
        """Epoch seconds for a detailedForecast ``period_start``, or None.

        The base integration stores ``period_start`` as a timezone-aware
        ``datetime`` in the in-memory attribute (HA only stringifies attributes at
        the API/recorder boundary), but it can also arrive as an ISO 8601 string or
        a raw epoch. Handle all three; a naive datetime/string is assumed UTC.
        """
        if period_start in (None, ""):
            return None
        if isinstance(period_start, datetime):
            dt = period_start
        elif isinstance(period_start, (int, float)):
            return float(period_start)
        elif isinstance(period_start, str):
            try:
                dt = datetime.fromisoformat(period_start)
            except ValueError:
                return None
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()

    def _read_base_auto_dampen(self) -> bool:
        """True if the base integration's automatic dampening is enabled.

        While on, the base rejects manual `set_dampening` calls, so the enhanced
        integration must not push.
        """
        try:
            for entry in self.hass.config_entries.async_entries(BASE_DOMAIN):
                if entry.options.get("auto_dampen"):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _read_base_export_limit(self) -> float | None:
        """Property-wide export limit in kW from the base config entry, or None.

        Base stores ``site_export_limit`` (historically in Watts). Values above 100
        are treated as Watts and scaled to kW; realistic kW limits are well under
        that. Returns None when unset so the manual option can take over.
        """
        try:
            for entry in self.hass.config_entries.async_entries(BASE_DOMAIN):
                raw = entry.options.get("site_export_limit")
                if raw in (None, ""):
                    continue
                limit = float(raw)
                if limit > 100:
                    limit = limit / 1000.0
                _LOGGER.debug("Base site_export_limit=%s → %.3f kW", raw, limit)
                return limit
        except Exception:  # noqa: BLE001
            pass
        return None

    def _safe_read_sensor(self, entity_id: str) -> float:
        if not entity_id:
            return 0.0
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return 0.0
        try:
            val = float(state.state)
            return max(0.0, val)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _resolve_input_mode(state: State, configured: str) -> str:
        """Resolve an explicit input mode, or auto-detect it from the sensor.

        Detection is **unit-first**: a ``Wh``/``kWh``/``MWh`` unit is a cumulative
        energy counter (read as a delta over the interval — the recommended input),
        and a ``W``/``kW`` unit is an averaged-power reading (a rolling
        ``mean_linear`` helper, read directly). The unit is authoritative because a
        counter that omits ``state_class`` must not be mistaken for instantaneous
        power — that was the previous behaviour and it silently read a lifetime
        ``kWh`` total as a giant ``kW`` value. ``state_class`` is only consulted as a
        fallback when the unit is missing or unrecognised.
        """
        if configured and configured != "auto":
            return configured
        attrs = state.attributes
        unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
        if unit.endswith("wh"):  # wh / kwh / mwh → cumulative energy counter
            if unit == "wh":
                return "energy_wh"
            if unit == "mwh":
                return "energy_mwh"
            return "energy_kwh"
        if unit.endswith("w"):  # w / kw → averaged power (rolling mean_linear)
            return "power_w" if unit == "w" else "power_kw"
        # Unit missing/unrecognised — fall back to the state_class hint.
        state_class = attrs.get("state_class")
        if state_class in ("total", "total_increasing"):
            return "energy_kwh"
        return "power_kw"

    @staticmethod
    def _to_kwh(value: float, mode: str) -> float:
        if mode == "energy_wh":
            return value / 1000.0
        if mode == "energy_mwh":
            return value * 1000.0
        return value  # energy_kwh

    @staticmethod
    def _to_kw(value: float, mode: str) -> float:
        if mode == "power_w":
            return value / 1000.0
        return value  # power_kw

    def _read_pv_value(
        self,
        entity_id: str,
        configured_mode: str,
        key: str,
        now_epoch: int,
    ) -> tuple[float, int | None]:
        """Read a PV sensor as average kW.

        Returns ``(value_kw, interval_start_epoch)``. Power-mode sensors are
        expected to be **pre-averaged** (a rolling ``mean_linear`` statistics
        helper, not a raw instantaneous reading); the value is converted to kW and
        read directly, with start epoch ``None``. For energy-counter sensors —
        the recommended input — the value is the average power over the *actual*
        elapsed interval (``delta_kWh / hours``), making it robust to polling
        drift and to the :00/:30 reset race of boundary-windowed helpers; the
        start epoch is the previous sample time. The first reading
        after setup/restart, a counter reset, or an out-of-band interval yields
        ``0.0`` so it is naturally excluded from tuning/dampening (which filter
        ``pv_actual > 0``).
        """
        if not entity_id:
            return 0.0, None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return 0.0, None
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            return 0.0, None

        mode = self._resolve_input_mode(state, configured_mode)

        if mode.startswith("power"):
            return max(0.0, self._to_kw(raw, mode)), None

        # Energy-counter mode: difference against the stored baseline.
        counter_kwh = self._to_kwh(max(0.0, raw), mode)
        prev = self._energy_baselines.get(key)
        self._energy_baselines[key] = {"value": counter_kwh, "epoch": int(now_epoch)}
        self._baselines_dirty = True

        if not isinstance(prev, dict):
            _LOGGER.debug("Energy baseline seeded for %s; first interval skipped", key)
            return 0.0, None

        prev_epoch = int(prev.get("epoch", 0))
        dt = now_epoch - prev_epoch
        delta = counter_kwh - float(prev.get("value", 0.0))
        expected = UPDATE_INTERVAL_MINUTES * 60

        if dt <= 0:
            return 0.0, None
        if delta < 0:
            _LOGGER.debug("Energy counter %s decreased (reset/rollover); interval skipped", key)
            return 0.0, None
        if dt < expected * ENERGY_DT_MIN_FRACTION or dt > expected * ENERGY_DT_MAX_FRACTION:
            _LOGGER.debug("Energy interval for %s was %ss (expected ~%ss); excluded", key, dt, expected)
            return 0.0, None

        avg_kw = delta / (dt / 3600.0)
        return max(0.0, avg_kw), prev_epoch

    async def _save_baselines(self) -> None:
        """Persist energy-counter baselines to disk."""
        try:
            await self._store.async_save(self._energy_baselines)
            self._baselines_dirty = False
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed to persist energy baselines: %s", exc)

    def _read_battery(self, opts: dict[str, Any]) -> float:
        # Prefer Statistics sensor
        stat = self._safe_read_sensor(opts.get(CONF_BATTERY_STAT_SENSOR, ""))
        if stat > 0:
            return stat

        # Raw battery fallback
        if opts.get(CONF_BATTERY_ENABLED):
            mode = opts.get(CONF_BATTERY_MODE, "net")
            if mode == "net":
                raw = self._safe_read_sensor(opts.get(CONF_BATTERY_NET_SENSOR, ""))
                return max(0.0, raw)
            raw = self._safe_read_sensor(opts.get(CONF_BATTERY_CHARGE_SENSOR, ""))
            return max(0.0, raw)
        return 0.0

    def _read_forecast_from_base(self, base_coord: Any) -> tuple[float, float, float, float, float]:
        """Return (forecast_now_kw, forecast_today_kwh, pv_estimate, pv_est10, pv_est90)."""
        try:
            if base_coord is not None and hasattr(base_coord, "data") and base_coord.data:
                data = base_coord.data
                forecast_now = float(data.get("forecast_now", 0) or 0)
                forecast_today = float(data.get("forecast_today", 0) or 0)
                pv_estimate = float(data.get("pv_estimate", 0) or 0)
                pv_est10 = float(data.get("pv_estimate10", 0) or 0)
                pv_est90 = float(data.get("pv_estimate90", 0) or 0)
                return forecast_now, forecast_today, pv_estimate, pv_est10, pv_est90
        except Exception:  # noqa: BLE001
            pass

        # Fallback: base coordinator data is unavailable. forecast_today is a
        # genuine kWh daily total, so read it straight off the kWh sensor. But
        # forecast_now is a kW *power* figure — the old fallback read
        # forecast_remaining_today (a kWh count-down), which is the wrong unit.
        # Derive it instead from the current half-hour detailedForecast slot's
        # pv_estimate (already average kW over the slot), keeping the sensor's
        # declared kW unit honest. Returns 0.0 if the attribute is absent.
        try:
            forecast_today = self._read_sensor_state_float("sensor.solcast_pv_forecast_forecast_today")
        except Exception:  # noqa: BLE001
            forecast_today = 0.0
        now_epoch = int(time.time())
        slot_start = now_epoch - (now_epoch % 1800)
        forecast_now = self._total_forecast_for_period(slot_start)[0]
        return forecast_now, forecast_today, 0.0, 0.0, 0.0

    def _read_sensor_state_float(self, entity_id: str) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return 0.0
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return 0.0

    # ------------------------------------------------------------------
    # Properties for sensors
    # ------------------------------------------------------------------

    @property
    def tuning_tilt(self) -> float | None:
        """Latest tuned panel tilt in degrees, or None before the first run."""
        return self._tuning_result["tilt"] if self._tuning_result else None

    @property
    def tuning_azimuth(self) -> float | None:
        """Configured azimuth echoed back (not tuned), in the Solcast convention."""
        # Stored internally East-positive; report in the Solcast/base convention
        # (West-positive) so it matches the configured Panel Azimuth and Solcast.
        if not self._tuning_result:
            return None
        return panel_azimuth_to_solcast(self._tuning_result["azimuth"])

    @property
    def tuning_rmse(self) -> float | None:
        """RMSE (kW) of the latest tuning fit, or None before the first run."""
        return self._tuning_result["rmse_kw"] if self._tuning_result else None

    @property
    def tuning_export_excluded(self) -> int:
        """Count of records dropped by the export-limit filter in the last run."""
        return self._tuning_result.get("export_limited_excluded", 0) if self._tuning_result else 0

    @property
    def site_tuning(self) -> dict[str, dict[str, Any]]:
        """Per-site tuning results keyed by resource_id (empty in single-site mode)."""
        return self._site_tuning_results

    @property
    def tuning_extra(self) -> dict[str, Any]:
        """Extra tuning attributes (fit quality, per-site results) for the sensor."""
        if not self._tuning_result and not self._site_tuning_results:
            return {}
        extra: dict[str, Any] = {}
        if self._tuning_result:
            extra.update(
                {
                    # Azimuth is fixed at the configured value (not tuned); reported in
                    # the Solcast/base convention (West-positive) for reference.
                    "azimuth": panel_azimuth_to_solcast(self._tuning_result.get("azimuth", 0.0)),
                    "azimuth_tuned": False,
                    "rmse_kw": self._tuning_result.get("rmse_kw"),
                    "mae_kw": self._tuning_result.get("mae_kw"),
                    "capacity_scale": self._tuning_result.get("capacity_scale"),
                    "n_records": self._tuning_result.get("n_records"),
                    "export_limited_excluded": self._tuning_result.get("export_limited_excluded", 0),
                }
            )
        if self._site_tuning_results:
            extra["per_site"] = [
                {
                    "name": r.get("name"),
                    "resource_id": rid,
                    "tilt": round(r.get("tilt", 0.0), 2),
                    "azimuth": round(panel_azimuth_to_solcast(r.get("azimuth", 0.0)), 2),
                    "rmse_kw": round(r.get("rmse_kw", 0.0), 4),
                    "n_records": r.get("n_records"),
                }
                for rid, r in self._site_tuning_results.items()
            ]
        return extra

    @property
    def dampening_hours_with_db(self) -> int:
        """Number of half-hour slots whose dampening is backed by DB history."""
        return sum(1 for s in self._dampening_table if s.get("source") not in ("no_data", "night"))

    @property
    def confidence(self) -> int | None:
        """Short-horizon forecast-confidence score (0–100), or None until there's data."""
        return self._confidence.get("confidence")

    @property
    def confidence_attributes(self) -> dict[str, Any]:
        """Diagnostics for the confidence sensor: rating, recent bias, what it's based on."""
        return {
            "rating": self._confidence.get("rating", "unknown"),
            "recent_bias": self._confidence.get("recent_bias"),
            "n_slots": self._confidence.get("n_slots", 0),
            "horizon_hours": CONFIDENCE_HORIZON_HOURS,
            "based_on": "recent measured output vs Solcast forecast",
        }

    def configured_sites_for_entities(self) -> list[tuple[str, str]]:
        """``(resource_id, display_name)`` for each configured per-site array, for entity setup.

        Name precedence: the user-entered per-array name (config flow) → the Solcast
        site name (discovered) → a short ``Site <id>`` fallback.
        """
        groups = self._opts.get(CONF_SITE_GROUPS) or []
        ids = self._configured_site_ids(groups)
        user_names = self._site_names_from_groups(groups)
        by_id = {s["resource_id"]: s for s in self._sites}
        return [(sid, user_names.get(sid) or (by_id.get(sid) or {}).get("name") or f"Site {sid[:4]}") for sid in ids]

    @staticmethod
    def _site_names_from_groups(groups: list[dict[str, Any]]) -> dict[str, str]:
        """Map ``resource_id → user-entered display name`` from the configured groups."""
        names: dict[str, str] = {}
        for g in groups:
            if g.get("site") and g.get("name"):
                names[g["site"]] = g["name"]
            for s in g.get("strings") or []:
                if s.get("name"):
                    names[s["site"]] = s["name"]
        return names

    def site_shading(self, site_id: str) -> float | None:
        """Average daytime dampening factor for a site (1.0 = no shading, < 1 = shaded)."""
        table = self._site_dampening_tables.get(site_id)
        if not table:
            return None
        factors = [s["factor"] for s in table if s.get("source") != "night"]
        return round(sum(factors) / len(factors), 4) if factors else None

    def site_output(self, site_id: str) -> float | None:
        """Latest measured generation for a site (average kW over the half-hour).

        ``None`` until a multi-site cycle has produced a per-site reading, so the
        entity stays unavailable rather than reporting a misleading 0.
        """
        out = self._site_output.get(site_id)
        return out["pv_actual"] if out else None

    def site_output_attributes(self, site_id: str) -> dict[str, Any]:
        """Per-array generation diagnostics: forecast for the same slot and name."""
        site = next((s for s in self._sites if s.get("resource_id") == site_id), {})
        out = self._site_output.get(site_id) or {}
        return {
            "name": site.get("name"),
            "resource_id": site_id,
            "pv_estimate": out.get("pv_estimate"),
            "capacity_kw": site.get("capacity"),
        }

    def site_tuned_tilt(self, site_id: str) -> float | None:
        """Latest tuned tilt for a site, or ``None`` until that array has been tuned."""
        tuning = self._site_tuning_results.get(site_id) or {}
        tilt = tuning.get("tilt")
        return round(tilt, 1) if tilt is not None else None

    def site_azimuth(self, site_id: str) -> float | None:
        """Configured azimuth for a site (Solcast convention), from site discovery.

        Azimuth is held fixed at the Solcast value and never tuned, so this reflects
        the discovered orientation rather than a fitted one — the per-site counterpart
        to the property-wide tuned-azimuth sensor.
        """
        site = next((s for s in self._sites if s.get("resource_id") == site_id), {})
        az = site.get("azimuth")
        return round(float(az), 1) if az is not None else None

    def site_tuned_rmse(self, site_id: str) -> float | None:
        """Fit error (RMSE, kW) of a site's last tuning run, or ``None`` if untuned.

        The trust signal for that array's tuned tilt: lower is a tighter fit. Tracked
        per-site because differently-oriented arrays are each tuned independently
        against their own records and azimuth, so the property-wide aggregate RMSE
        blurs them together.
        """
        tuning = self._site_tuning_results.get(site_id) or {}
        rmse = tuning.get("rmse_kw")
        return round(rmse, 4) if rmse is not None else None

    def site_tuned_tilt_attributes(self, site_id: str) -> dict[str, Any]:
        """Per-array tuning diagnostics: fit quality, record count and configured orientation."""
        site = next((s for s in self._sites if s.get("resource_id") == site_id), {})
        tuning = self._site_tuning_results.get(site_id) or {}
        return {
            "name": site.get("name"),
            "resource_id": site_id,
            "rmse_kw": round(tuning["rmse_kw"], 4) if tuning.get("rmse_kw") is not None else None,
            "tuning_records": tuning.get("n_records"),
            "configured_tilt": site.get("tilt"),
            "azimuth_compass": site.get("compass_degrees"),
        }

    def site_visibility_attributes(self, site_id: str) -> dict[str, Any]:
        """Per-array diagnostics: discovered orientation, dampening, tuning and confidence."""
        site = next((s for s in self._sites if s.get("resource_id") == site_id), {})
        table = self._site_dampening_tables.get(site_id) or []
        day = [s for s in table if s.get("source") != "night"]
        factors = [s["factor"] for s in day]
        avg = sum(factors) / len(factors) if factors else None
        tuning = self._site_tuning_results.get(site_id) or {}
        conf = self._site_confidence.get(site_id) or {}
        return {
            "name": site.get("name"),
            "resource_id": site_id,
            # 0° (due north) / 0° tilt are valid, so don't coerce them to None.
            "azimuth_compass": site.get("compass_degrees"),
            "tilt": site.get("tilt"),
            "capacity_kw": site.get("capacity"),
            "shading_pct": round((1.0 - avg) * 100, 1) if avg is not None else None,
            "min_factor": round(min(factors), 4) if factors else None,
            "hours_with_db": sum(1 for s in table if s.get("source") not in ("no_data", "night")),
            "clear_sky_basis": day[0].get("clear_sky_basis") if day else None,
            "confidence": conf.get("confidence"),
            "confidence_rating": conf.get("rating", "unknown"),
            "recent_bias": conf.get("recent_bias"),
            "tuned_tilt": round(tuning["tilt"], 1) if tuning.get("tilt") is not None else None,
            "tuning_rmse_kw": round(tuning["rmse_kw"], 4) if tuning.get("rmse_kw") is not None else None,
            "tuning_records": tuning.get("n_records"),
        }

    @property
    def dampening_attributes(self) -> dict[str, Any]:
        """Per-hour dampening diagnostics (factor + source) for the sensor."""
        attrs: dict[str, Any] = {}
        for h in range(24):
            slot_a = self._dampening_table[h * 2] if h * 2 < len(self._dampening_table) else {}
            slot_b = self._dampening_table[h * 2 + 1] if h * 2 + 1 < len(self._dampening_table) else {}
            if slot_a or slot_b:
                key = f"hour_{h:02d}"
                f_a = slot_a.get("factor", 1.0)
                f_b = slot_b.get("factor", 1.0)
                attrs[f"{key}_factor"] = round((f_a + f_b) / 2, 4)
                attrs[f"{key}_alpha"] = round((slot_a.get("alpha", 0.0) + slot_b.get("alpha", 0.0)) / 2, 4)
                attrs[f"{key}_source"] = slot_a.get("source", "night")
                attrs[f"{key}_quality_records"] = round(
                    (slot_a.get("quality_records", 0.0) + slot_b.get("quality_records", 0.0)) / 2, 2
                )
                attrs[f"{key}_avg_quality"] = round(
                    (slot_a.get("avg_quality", 0.0) + slot_b.get("avg_quality", 0.0)) / 2, 3
                )
                clipped = slot_a.get("clipped_excluded", 0) + slot_b.get("clipped_excluded", 0)
                if clipped:
                    attrs[f"{key}_clipped_excluded"] = clipped
                fclip = slot_a.get("forecast_clipped", 0) + slot_b.get("forecast_clipped", 0)
                if fclip:
                    attrs[f"{key}_forecast_clipped"] = fclip
        sources = [s.get("source") for s in self._dampening_table if s.get("source") != "night"]
        if sources:
            most_common = Counter(sources).most_common(1)
            attrs["overall_source"] = most_common[0][0] if most_common else "no_data"
        # Which clear-sky signal weighted the records: measured Kt (Open-Meteo
        # irradiance) or the legacy OWM cloud bands. Uniform across all slots.
        if self._dampening_table:
            attrs["clear_sky_basis"] = self._dampening_table[0].get("clear_sky_basis", "cloud")
        # Gate state: when true, the push was held at neutral 1.0 because a tuned
        # orientation diverges from the configured Solcast value (see repair issue).
        attrs["gated"] = self._dampening_gated
        return attrs
