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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_TILT,
    DAMPENING_INTERVAL_HOURS,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_CLOUD_THRESHOLD,
    DEFAULT_EXPORT_LIMIT_KW,
    TUNING_INTERVAL_HOURS,
    UPDATE_INTERVAL_MINUTES,
)
from .db_manager import DbManager
from .pv_tuning import normalize_epoch, run_tuning, solar_position
from .shading_dampening import average_slot_pairs, compute_dampening
from .solcast_api import OWMClient

_LOGGER = logging.getLogger(__name__)


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
        self._dampening_table: list[dict[str, Any]] = []
        self._last_dampening_ts: float = 0.0
        self._last_tuning_ts: float = 0.0
        self._db_record_count: int = 0
        self._base_status: str = "not_detected"

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Initialise DB and OWM connections."""
        opts = self._opts
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

        # Detect base integration
        base_coord = self._get_base_coordinator()
        self._base_status = "connected" if base_coord is not None else "not_detected"

        # Read Statistics sensors
        pv_actual = self._safe_read_sensor(opts.get(CONF_PV_ACTUAL_SENSOR, ""))
        pv_export = self._safe_read_sensor(opts.get(CONF_PV_EXPORT_SENSOR, ""))
        battery_charge = self._read_battery(opts)

        # Fetch OWM weather
        if self._owm:
            self._weather = await self._owm.async_fetch()

        # Solar position
        lat = float(opts.get(CONF_LATITUDE, -37.9))
        lon = float(opts.get(CONF_LONGITUDE, 145.0))
        az, zen = solar_position(now_epoch, lat, lon)

        # Forecast data from base integration
        forecast_now, forecast_today, pv_estimate, pv_est10, pv_est90 = (
            self._read_forecast_from_base(base_coord)
        )

        # Persist to DB
        if self._db and opts.get(CONF_DB_ENABLED):
            period_end = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
            period_start = datetime.fromtimestamp(
                now_epoch - 1800, tz=timezone.utc
            ).isoformat()
            record = {
                "period_end": period_end,
                "period_end_epoch": now_epoch,
                "period_start": period_start,
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
        records = await self._db.async_get_records_for_tuning()
        if not records:
            return
        result = await self.hass.async_add_executor_job(
            run_tuning,
            records,
            float(opts.get(CONF_CAPACITY_KW, 5.0)),
            int(opts.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD)),
            float(opts.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD)),
            float(opts.get(CONF_EXPORT_LIMIT_KW, DEFAULT_EXPORT_LIMIT_KW)),
            float(opts.get(CONF_TILT, 20.0)),
            float(opts.get(CONF_AZIMUTH, 0.0)),
        )
        if result:
            self._tuning_result = result
            _LOGGER.debug("PV tuning result: %s", result)

    # ------------------------------------------------------------------
    # Dampening
    # ------------------------------------------------------------------

    async def _run_dampening(
        self, opts: dict[str, Any], now_epoch: int, lat: float, lon: float
    ) -> None:
        capacity_kw = float(opts.get(CONF_CAPACITY_KW, 5.0))
        cloud_threshold = int(opts.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD))
        cloud_max_include = int(opts.get(CONF_CLOUD_MAX_INCLUDE, DEFAULT_CLOUD_MAX_INCLUDE))
        clipping_threshold = float(opts.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD))

        base_factors = self._read_base_dampening_factors()
        dt_now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)

        slot_results: list[dict[str, Any]] = []

        for slot in range(48):
            slot_epoch = now_epoch - (now_epoch % 86400) + slot * 1800
            slot_dt = datetime.fromtimestamp(slot_epoch, tz=timezone.utc)
            slot_doy = slot_dt.timetuple().tm_yday

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
                records = await self._db.async_get_records_for_dampening(slot_doy)

            hour_idx = slot // 2
            base_val = base_factors[hour_idx] if hour_idx < len(base_factors) else 1.0

            slot_result = compute_dampening(
                records=records,
                capacity_kw=capacity_kw,
                cloud_threshold=cloud_threshold,
                cloud_max_include=cloud_max_include,
                clipping_threshold=clipping_threshold,
                base_factors=[base_val],
                target_zenith=zen_slot,
                target_azimuth=az_slot,
            )
            slot_results.append(slot_result)

        self._dampening_table = slot_results

        # Push to base integration
        hourly_factors = average_slot_pairs([s["factor"] for s in slot_results])
        await self._push_dampening(hourly_factors)

    async def _push_dampening(self, hourly_factors: list[float]) -> None:
        """Call base integration set_dampening_factor service."""
        try:
            dampening_payload = {str(h): round(f, 4) for h, f in enumerate(hourly_factors)}
            await self.hass.services.async_call(
                BASE_DOMAIN,
                "set_dampening_factor",
                {"dampening": dampening_payload},
                blocking=False,
            )
            _LOGGER.debug("Pushed dampening factors to base integration")
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

    def _read_base_dampening_factors(self) -> list[float]:
        """Read 24 hourly dampening factors from base integration."""
        # Try config entry options first
        try:
            for entry in self.hass.config_entries.async_entries(BASE_DOMAIN):
                dampening = entry.options.get("dampening", {})
                if dampening:
                    return [float(dampening.get(str(h), 1.0)) for h in range(24)]
        except Exception:  # noqa: BLE001
            pass

        # Try sensor states
        factors = []
        for h in range(24):
            found = False
            for state in self.hass.states.async_all():
                if "solcast" in state.entity_id and "dampening" in state.entity_id and f"hour_{h:02d}" in state.entity_id:
                    try:
                        factors.append(float(state.state))
                        found = True
                        break
                    except (ValueError, TypeError):
                        pass
            if not found:
                factors.append(1.0)
        return factors

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
    def tuning_extra(self) -> dict[str, Any]:
        if not self._tuning_result:
            return {}
        return {
            "azimuth": self._tuning_result.get("azimuth"),
            "rmse_kw": self._tuning_result.get("rmse_kw"),
            "n_records": self._tuning_result.get("n_records"),
        }

    @property
    def dampening_hours_with_db(self) -> int:
        return sum(1 for s in self._dampening_table if s.get("source") not in ("base_fallback", "night"))

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
            attrs["overall_source"] = most_common[0][0] if most_common else "base_fallback"
        return attrs
