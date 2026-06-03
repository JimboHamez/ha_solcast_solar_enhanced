"""DataUpdateCoordinator for Solcast Solar Enhanced."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BASE_DOMAIN,
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
    CONF_EXPORT_LIMIT_KW,
    CONF_DB_ENABLED,
    CONF_DB_HOST,
    CONF_DB_NAME,
    CONF_DB_PASSWORD,
    CONF_DB_PORT,
    CONF_DB_READONLY,
    CONF_DB_USER,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_OWM_API_KEY,
    CONF_OWM_ENABLED,
    CONF_PV_ACTUAL_INPUT_MODE,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_INPUT_MODE,
    CONF_PV_EXPORT_SENSOR,
    CONF_TILT,
    DAMPENING_INTERVAL_HOURS,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_CLOUD_THRESHOLD,
    DEFAULT_EXPORT_LIMIT_KW,
    CONF_SITE_AUTODISCOVER,
    CONF_SITE_GROUPS,
    DEFAULT_PV_INPUT_MODE,
    DEFAULT_SITE_AUTODISCOVER,
    DEFAULT_SITE_ID,
    DOMAIN,
    ENERGY_DT_MAX_FRACTION,
    ENERGY_DT_MIN_FRACTION,
    STORAGE_VERSION,
    TUNING_INTERVAL_HOURS,
    UPDATE_INTERVAL_MINUTES,
)
from .db_manager import DbManager
from .pv_tuning import normalize_epoch, run_tuning, solar_position
from .shading_dampening import average_slot_pairs, compute_dampening
from .solcast_api import OWMClient

_LOGGER = logging.getLogger(__name__)


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

            def _f(key: str) -> float:
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
        super().__init__(
            hass,
            _LOGGER,
            name="solcast_solar_enhanced",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._entry = entry
        self._opts = {**entry.data, **entry.options}

        self._db: DbManager | None = None
        self._owm: OWMClient | None = None

        self._weather: dict[str, Any] = {"temp": 0.0, "clouds": 0, "description": ""}
        self._tuning_result: dict[str, Any] | None = None
        self._site_tuning_results: dict[str, dict[str, Any]] = {}
        self._dampening_table: list[dict[str, Any]] = []
        self._last_dampening_ts: float = 0.0
        self._last_tuning_ts: float = 0.0
        self._db_record_count: int = 0
        self._base_status: str = "not_detected"
        self._auto_dampen_warned: bool = False

        # Discovered Solcast sites (multiple arrays on one property), each:
        # {resource_id, name, capacity, capacity_dc, tilt, azimuth, entity_id}.
        self._sites: list[dict[str, Any]] = []

        # Energy-counter baselines: {key: {"value": kwh, "epoch": int}}.
        # Persisted across restarts so energy-delta readings survive a reload.
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_energy_baseline"
        )
        self._energy_baselines: dict[str, Any] = {}
        self._baselines_dirty: bool = False

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

        if opts.get(CONF_DB_ENABLED):
            self._db = DbManager(
                host=opts.get(CONF_DB_HOST, "localhost"),
                port=int(opts.get(CONF_DB_PORT, 3306)),
                user=opts.get(CONF_DB_USER, ""),
                password=opts.get(CONF_DB_PASSWORD, ""),
                db=opts.get(CONF_DB_NAME, "solcast"),
                readonly=bool(opts.get(CONF_DB_READONLY, False)),
            )
            ok = await self._db.async_connect()
            if not ok:
                _LOGGER.warning("DB connection failed — DB features disabled for this session")
                self._db = None

        if opts.get(CONF_OWM_ENABLED) and opts.get(CONF_OWM_API_KEY):
            self._owm = OWMClient(
                api_key=opts[CONF_OWM_API_KEY],
                latitude=float(opts.get(CONF_LATITUDE, -37.9)),
                longitude=float(opts.get(CONF_LONGITUDE, 145.0)),
            )

    async def async_teardown(self) -> None:
        """Close DB pool."""
        if self._db:
            await self._db.async_close()
            self._db = None

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._do_update()
        except Exception as exc:  # noqa: BLE001
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
        self._base_status = "connected" if base_coord is not None else "not_detected"

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
        if self._baselines_dirty:
            await self._save_baselines()
        battery_charge = self._read_battery(opts)

        # Fetch OWM weather
        if self._owm:
            self._weather = await self._owm.async_fetch()

        # Solar position
        lat = float(opts.get(CONF_LATITUDE, -37.9))
        lon = float(opts.get(CONF_LONGITUDE, 145.0))
        az, zen = solar_position(period_epoch, lat, lon)

        # Forecast data from base integration
        forecast_now, forecast_today, pv_estimate, pv_est10, pv_est90 = (
            self._read_forecast_from_base(base_coord)
        )

        # Persist to DB
        if self._db and opts.get(CONF_DB_ENABLED):
            period_end = datetime.fromtimestamp(period_epoch, tz=timezone.utc).isoformat()
            start_epoch = pv_actual_start if pv_actual_start else period_epoch - 1800
            period_start = datetime.fromtimestamp(
                start_epoch, tz=timezone.utc
            ).isoformat()
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
                "temp": round(self._weather["temp"], 2),
                "clouds": self._weather["clouds"],
                "description": self._weather["description"],
                "battery_charge": round(battery_charge, 4),
            }
            await self._db.async_insert_record(record)

            # Per-site rows (multi-site). The property-wide '_total' row above
            # remains the source for aggregate tuning/dampening; per-site rows are
            # additive and only ever read with an explicit site filter (never
            # summed), so the property-wide export is replicated here to drive each
            # site's export-limit clip exclusion. battery stays on '_total' only.
            for site_id, (site_kw, site_start) in site_actuals.items():
                s_start = site_start if site_start else period_epoch - 1800
                s_est, s_est10, s_est90 = self._site_forecast_for_period(
                    site_id, s_start
                )
                await self._db.async_insert_record({
                    "period_end": period_end,
                    "period_end_epoch": period_epoch,
                    "period_start": datetime.fromtimestamp(
                        s_start, tz=timezone.utc
                    ).isoformat(),
                    "site": site_id,
                    "pv_actual": round(site_kw, 4),
                    "pv_export": round(pv_export, 4),
                    "pv_estimate": round(s_est, 4),
                    "pv_estimate10": round(s_est10, 4),
                    "pv_estimate90": round(s_est90, 4),
                    "azimuth": round(az, 5),
                    "zenith": round(zen, 5),
                    "temp": round(self._weather["temp"], 2),
                    "clouds": self._weather["clouds"],
                    "description": self._weather["description"],
                    "battery_charge": 0.0,
                })

            self._db_record_count = await self._db.async_get_record_count()

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

        return {
            "pv_actual": pv_actual,
            "pv_export": pv_export,
            "battery_charge": battery_charge,
            "forecast_now": forecast_now,
            "forecast_today": forecast_today,
            "weather": self._weather,
            "tuning": self._tuning_result,
            "dampening_table": self._dampening_table,
            "db_records": self._db_record_count,
            "base_status": self._base_status,
        }

    # ------------------------------------------------------------------
    # PV Tuning
    # ------------------------------------------------------------------

    async def _run_tuning(self, opts: dict[str, Any]) -> None:
        if not self._db:
            return
        # Aggregate tuning operates on the property-wide '_total' rows so it never
        # double-counts the additive per-site rows.
        records = await self._db.async_get_records_for_tuning(site=DEFAULT_SITE_ID)
        if not records:
            return
        # Prefer the base integration's property-wide export limit; fall back to
        # the manual option when the base hasn't set one.
        export_limit = self._read_base_export_limit()
        if export_limit is None:
            export_limit = float(opts.get(CONF_EXPORT_LIMIT_KW, DEFAULT_EXPORT_LIMIT_KW))
        result = await self.hass.async_add_executor_job(
            run_tuning,
            records,
            float(opts.get(CONF_CAPACITY_KW, 5.0)),
            int(opts.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD)),
            float(opts.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD)),
            export_limit,
            float(opts.get(CONF_TILT, 20.0)),
            float(opts.get(CONF_AZIMUTH, 0.0)),
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
        cloud_threshold = int(opts.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD))
        clipping_threshold = float(
            opts.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD)
        )
        by_id = {s["resource_id"]: s for s in self._sites}
        results: dict[str, dict[str, Any]] = {}
        for site_id in site_ids:
            records = await self._db.async_get_records_for_tuning(site=site_id)
            if not records:
                continue
            site = by_id.get(site_id, {})
            capacity = site.get("capacity") or float(opts.get(CONF_CAPACITY_KW, 5.0))
            tilt_seed = site.get("tilt") or float(opts.get(CONF_TILT, 20.0))
            az_seed = self._site_azimuth_seed(site, opts)
            result = await self.hass.async_add_executor_job(
                run_tuning,
                records,
                float(capacity),
                cloud_threshold,
                clipping_threshold,
                export_limit,
                float(tilt_seed),
                float(az_seed),
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
            return float(opts.get(CONF_AZIMUTH, 0.0))
        compass = float(compass) % 360
        return compass - 360 if compass > 180 else compass

    # ------------------------------------------------------------------
    # Dampening
    # ------------------------------------------------------------------

    async def _run_dampening(
        self, opts: dict[str, Any], now_epoch: int, lat: float, lon: float
    ) -> None:
        # Aggregate table (drives the dampening sensors) — property-wide '_total' rows.
        self._dampening_table = await self._compute_dampening_slots(
            opts, now_epoch, lat, lon, DEFAULT_SITE_ID
        )

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

        site_ids = self._configured_site_ids(opts.get(CONF_SITE_GROUPS) or [])
        if site_ids:
            # Multi-site: push a dampening set per site (which overrides the base's
            # global dampening for that site). The conflicting global push is
            # skipped so per-site factors are not overwritten.
            for site_id in site_ids:
                slots = await self._compute_dampening_slots(
                    opts, now_epoch, lat, lon, site_id
                )
                hourly = average_slot_pairs([s["factor"] for s in slots])
                await self._push_dampening(hourly, site=site_id)
        else:
            hourly = average_slot_pairs([s["factor"] for s in self._dampening_table])
            await self._push_dampening(hourly)

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

        tz = dt_util.get_time_zone(self.hass.config.time_zone) or timezone.utc
        now_local = datetime.fromtimestamp(now_epoch, tz=tz)
        slot_results: list[dict[str, Any]] = []

        for slot in range(48):
            hour, minute = divmod(slot * 30, 60)
            slot_local = now_local.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            slot_epoch = int(slot_local.timestamp())
            slot_doy = slot_local.timetuple().tm_yday

            az_slot, zen_slot = solar_position(slot_epoch, lat, lon)

            # Night slots — factor = 1.0
            if zen_slot >= 90:
                slot_results.append({
                    "factor": 1.0,
                    "alpha": 0.0,
                    "source": "night",
                    "quality_records": 0.0,
                    "avg_quality": 0.0,
                    "clipped_excluded": 0,
                })
                continue

            records: list[dict[str, Any]] = []
            if self._db:
                records = await self._db.async_get_records_for_dampening(
                    slot_doy, site=site
                )

            slot_result = compute_dampening(
                records=records,
                capacity_kw=capacity_kw,
                cloud_threshold=cloud_threshold,
                cloud_max_include=cloud_max_include,
                clipping_threshold=clipping_threshold,
                target_zenith=zen_slot,
                target_azimuth=az_slot,
            )
            slot_results.append(slot_result)

        return slot_results

    async def _push_dampening(
        self, hourly_factors: list[float], site: str | None = None
    ) -> None:
        """Push factors to the base integration's ``set_dampening`` service.

        The base expects ``damp_factor`` as a comma-separated string of 24 (hourly)
        or 48 (half-hourly) values, with an optional ``site`` (resource_id) to
        target a single site.
        """
        try:
            damp_factor = ",".join(f"{round(f, 4)}" for f in hourly_factors)
            data: dict[str, Any] = {"damp_factor": damp_factor}
            if site:
                data["site"] = site
            # blocking=True so a base-side ServiceValidationError surfaces here
            # and is handled, rather than leaking into Home Assistant's core log.
            await self.hass.services.async_call(
                BASE_DOMAIN, "set_dampening", data, blocking=True
            )
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
        opts = {**self._entry.data, **self._entry.options}
        await self._run_tuning(opts)
        self.async_set_updated_data(self.data or {})

    async def async_force_dampening_update(self) -> None:
        opts = {**self._entry.data, **self._entry.options}
        now_epoch = normalize_epoch(time.time())
        lat = float(opts.get(CONF_LATITUDE, -37.9))
        lon = float(opts.get(CONF_LONGITUDE, 145.0))
        await self._run_dampening(opts, now_epoch, lat, lon)
        self.async_set_updated_data(self.data or {})

    async def async_force_fetch_weather(self) -> None:
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

    def _read_site_actuals(
        self, opts: dict[str, Any], now_epoch: int
    ) -> dict[str, tuple[float, int | None]]:
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

    def _site_forecast_for_period(
        self, resource_id: str, start_epoch: int
    ) -> tuple[float, float, float]:
        """Return (pv_estimate, pv_estimate10, pv_estimate90) for a site's slot.

        Reads the per-site ``detailedForecast-<resource_id>`` attribute off the base
        ``forecast_today`` sensor (falls back to the underscore variant used by newer
        base versions). Values are already average kW over the half-hour, matching
        ``pv_actual``. Picks the entry whose ``period_start`` is closest to the
        measured interval's start, within half a slot.
        """
        state = self.hass.states.get("sensor.solcast_pv_forecast_forecast_today")
        if state is None:
            return 0.0, 0.0, 0.0
        attrs = state.attributes
        series = attrs.get(f"detailedForecast-{resource_id}")
        if series is None:
            series = attrs.get(f"detailedForecast_{resource_id}")
        if not series:
            return 0.0, 0.0, 0.0

        best: dict[str, Any] | None = None
        best_delta: float | None = None
        for entry in series:
            period_start = entry.get("period_start")
            if not period_start:
                continue
            try:
                ts = datetime.fromisoformat(period_start).timestamp()
            except (ValueError, TypeError):
                continue
            delta = abs(ts - start_epoch)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = entry
        if best is None or best_delta is None or best_delta > 900:
            return 0.0, 0.0, 0.0

        def _f(key: str) -> float:
            try:
                return float(best.get(key, 0) or 0)
            except (ValueError, TypeError):
                return 0.0

        return _f("pv_estimate"), _f("pv_estimate10"), _f("pv_estimate90")

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
                _LOGGER.debug(
                    "Base site_export_limit=%s → %.3f kW", raw, limit
                )
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
    def _resolve_input_mode(state: Any, configured: str) -> str:
        """Resolve an explicit input mode, or auto-detect from the sensor."""
        if configured and configured != "auto":
            return configured
        attrs = state.attributes
        state_class = attrs.get("state_class")
        unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
        if state_class in ("total", "total_increasing"):
            if unit == "wh":
                return "energy_wh"
            if unit == "mwh":
                return "energy_mwh"
            return "energy_kwh"
        if unit == "w":
            return "power_w"
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

        Returns ``(value_kw, interval_start_epoch)``. For power-mode sensors the
        instantaneous reading is converted to kW and the start epoch is ``None``.
        For energy-counter sensors the value is the average power over the *actual*
        elapsed interval (``delta_kWh / hours``), making it robust to polling
        drift; the start epoch is the previous sample time. The first reading
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
            _LOGGER.debug(
                "Energy counter %s decreased (reset/rollover); interval skipped", key
            )
            return 0.0, None
        if dt < expected * ENERGY_DT_MIN_FRACTION or dt > expected * ENERGY_DT_MAX_FRACTION:
            _LOGGER.debug(
                "Energy interval for %s was %ss (expected ~%ss); excluded", key, dt, expected
            )
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

    def _read_forecast_from_base(
        self, base_coord: Any
    ) -> tuple[float, float, float, float, float]:
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

        # Fallback: read sensor states
        try:
            forecast_now = self._read_sensor_state_float("sensor.solcast_pv_forecast_forecast_remaining_today")
            forecast_today = self._read_sensor_state_float("sensor.solcast_pv_forecast_forecast_today")
        except Exception:  # noqa: BLE001
            forecast_now = 0.0
            forecast_today = 0.0
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
        return self._tuning_result["tilt"] if self._tuning_result else None

    @property
    def tuning_azimuth(self) -> float | None:
        return self._tuning_result["azimuth"] if self._tuning_result else None

    @property
    def tuning_rmse(self) -> float | None:
        return self._tuning_result["rmse_kw"] if self._tuning_result else None

    @property
    def tuning_export_excluded(self) -> int:
        return self._tuning_result.get("export_limited_excluded", 0) if self._tuning_result else 0

    @property
    def site_tuning(self) -> dict[str, dict[str, Any]]:
        """Per-site tuning results keyed by resource_id (empty in single-site mode)."""
        return self._site_tuning_results

    @property
    def tuning_extra(self) -> dict[str, Any]:
        if not self._tuning_result and not self._site_tuning_results:
            return {}
        extra: dict[str, Any] = {}
        if self._tuning_result:
            extra.update({
                "azimuth": self._tuning_result.get("azimuth"),
                "rmse_kw": self._tuning_result.get("rmse_kw"),
                "n_records": self._tuning_result.get("n_records"),
                "export_limited_excluded": self._tuning_result.get("export_limited_excluded", 0),
            })
        if self._site_tuning_results:
            extra["per_site"] = [
                {
                    "name": r.get("name"),
                    "resource_id": rid,
                    "tilt": round(r.get("tilt", 0.0), 2),
                    "azimuth": round(r.get("azimuth", 0.0), 2),
                    "rmse_kw": round(r.get("rmse_kw", 0.0), 4),
                    "n_records": r.get("n_records"),
                }
                for rid, r in self._site_tuning_results.items()
            ]
        return extra

    @property
    def dampening_hours_with_db(self) -> int:
        return sum(1 for s in self._dampening_table if s.get("source") not in ("no_data", "night"))

    @property
    def dampening_attributes(self) -> dict[str, Any]:
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
        sources = [s.get("source") for s in self._dampening_table if s.get("source") != "night"]
        if sources:
            from collections import Counter
            most_common = Counter(sources).most_common(1)
            attrs["overall_source"] = most_common[0][0] if most_common else "no_data"
        return attrs
