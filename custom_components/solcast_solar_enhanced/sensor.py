"""Sensor entities for Solcast Solar Enhanced."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SENSOR_BASE_STATUS,
    SENSOR_BATTERY_CHARGE,
    SENSOR_DAMPENING,
    SENSOR_DB_RECORDS,
    SENSOR_FORECAST_NOW,
    SENSOR_FORECAST_TODAY,
    SENSOR_MPPT_DC,
    SENSOR_PV_ACTUAL,
    SENSOR_PV_CONFIDENCE,
    SENSOR_PV_EXPORT,
    SENSOR_TUNING_AZIMUTH,
    SENSOR_TUNING_EXPORT_EXCLUDED,
    SENSOR_TUNING_RMSE,
    SENSOR_TUNING_TILT,
    SENSOR_WEATHER_CLOUDS,
    SENSOR_WEATHER_TEMP,
)
from .coordinator import SolcastEnhancedCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solcast Solar Enhanced sensors from a config entry."""
    coordinator: SolcastEnhancedCoordinator = hass.data[DOMAIN][entry.entry_id]
    site_pairs = coordinator.configured_sites_for_entities()
    is_multisite = bool(site_pairs)

    # Property-wide tuning sensors. In a multi-site setup the aggregate (`_total`)
    # blends arrays of different orientation, so the meaningful tilt/azimuth/RMSE live
    # on each array's own card — hide these from the main card by default there. A
    # single-site install keeps them visible, as that aggregate *is* the one site.
    tuning_tilt = TuningTiltSensor(coordinator, entry)
    tuning_azimuth = TuningAzimuthSensor(coordinator, entry)
    tuning_rmse = TuningRmseSensor(coordinator, entry)
    if is_multisite:
        for s in (tuning_tilt, tuning_azimuth, tuning_rmse):
            s._attr_entity_registry_visible_default = False

    entities = [
        ForecastNowSensor(coordinator, entry),
        ForecastTodaySensor(coordinator, entry),
        tuning_tilt,
        tuning_azimuth,
        tuning_rmse,
        TuningExportExcludedSensor(coordinator, entry),
        DbRecordsSensor(coordinator, entry),
        MpptDcSensor(coordinator, entry),
        DampeningSensor(coordinator, entry),
        WeatherTempSensor(coordinator, entry),
        WeatherCloudsSensor(coordinator, entry),
        BatteryChargeSensor(coordinator, entry),
        PvActualSensor(coordinator, entry),
        PvExportSensor(coordinator, entry),
        BaseIntegrationSensor(coordinator, entry),
        PvForecastConfidenceSensor(coordinator, entry),
    ]
    # Per-site sensors per configured array (multi-site only), each grouped onto its own
    # per-array device: measured PV Power, shading/visibility, tuned tilt, azimuth, and
    # the (diagnostic) tuning RMSE.
    for site_id, name in site_pairs:
        entities.append(SiteOutputSensor(coordinator, entry, site_id, name))
        entities.append(SiteShadingSensor(coordinator, entry, site_id, name))
        entities.append(SiteTunedTiltSensor(coordinator, entry, site_id, name))
        entities.append(SiteAzimuthSensor(coordinator, entry, site_id, name))
        entities.append(SiteTuningRmseSensor(coordinator, entry, site_id, name))
    async_add_entities(entities)


class _EnhancedSensorBase(CoordinatorEntity[SolcastEnhancedCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: SolcastEnhancedCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solcast Solar Enhanced",
            manufacturer="JimboHamez",
            model="Solcast Solar Enhanced Integration",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_coordinator()
        self.async_write_ha_state()

    def _update_from_coordinator(self) -> None:
        pass


class _RestoringSensorBase(_EnhancedSensorBase, RestoreSensor):
    """Sensor that restores its last value across restarts.

    The coordinator only produces data on the half-hour grid, so after a restart
    ``coordinator.data`` is empty for up to ~30 min, which would otherwise show
    the entity as *unknown* until the first update cycle. Restoring the last
    value bridges that gap. Subclasses implement ``_live_value()``; as soon as
    the coordinator yields a value it supersedes the restored one.
    """

    _restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                self._restored_value = None

    def _live_value(self) -> float | None:
        """Current value from the coordinator, or None if not yet available."""
        return None

    @property
    def native_value(self) -> float | None:
        live = self._live_value()
        return live if live is not None else self._restored_value


class ForecastNowSensor(_EnhancedSensorBase):
    _attr_name = "Forecast Now"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_FORECAST_NOW)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("forecast_now")


class ForecastTodaySensor(_EnhancedSensorBase):
    _attr_name = "Forecast Today"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_FORECAST_TODAY)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("forecast_today")


class TuningTiltSensor(_EnhancedSensorBase):
    _attr_name = "Tuned Panel Tilt"
    _attr_native_unit_of_measurement = "°"
    _attr_icon = "mdi:angle-acute"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_TUNING_TILT)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.tuning_tilt

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.tuning_extra


class TuningAzimuthSensor(_EnhancedSensorBase):
    _attr_name = "Tuned Panel Azimuth"
    _attr_native_unit_of_measurement = "°"
    _attr_icon = "mdi:compass"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_TUNING_AZIMUTH)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.tuning_azimuth


class TuningRmseSensor(_EnhancedSensorBase):
    _attr_name = "Tuning RMSE"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_TUNING_RMSE)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.tuning_rmse


class TuningExportExcludedSensor(_EnhancedSensorBase):
    _attr_translation_key = "tuning_export_excluded"
    _attr_icon = "mdi:transmission-tower-off"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_TUNING_EXPORT_EXCLUDED)

    @property
    def native_value(self) -> int:
        return self.coordinator.tuning_export_excluded


class DbRecordsSensor(_EnhancedSensorBase):
    _attr_name = "Database Records"
    _attr_icon = "mdi:database"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_DB_RECORDS)

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return int(self.coordinator.data.get("db_records", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Freshness/coverage diagnostics for verifying data is accumulating."""
        data = self.coordinator.data
        if not data:
            return None
        sites = data.get("db_sites") or []
        return {
            "latest_period_end": data.get("db_latest_period_end"),
            "distinct_sites": len(sites),
            "sites": sites,
        }


class MpptDcSensor(_EnhancedSensorBase):
    """Diagnostic: latest captured per-MPPT DC telemetry (Phase 2).

    State is the highest string voltage seen this cycle (the off-MPP-relevant
    aggregate); attributes break out each tracker's voltage/current and any
    per-site values, so the user can confirm their string sensors are wired and
    data is landing. Unavailable (None) when no DC sensors are configured.
    """

    _attr_name = "MPPT DC Voltage (max)"
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:current-dc"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_MPPT_DC)

    @property
    def native_value(self) -> float | None:
        dc = (self.coordinator.data or {}).get("dc_telemetry")
        return dc.get("max_voltage") if dc else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        dc = (self.coordinator.data or {}).get("dc_telemetry")
        if not dc:
            return None
        return {k: v for k, v in dc.items() if k != "max_voltage"}


class DampeningSensor(_EnhancedSensorBase):
    _attr_name = "Dampening Hours with DB Data"
    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_DAMPENING)

    @property
    def native_value(self) -> int:
        return self.coordinator.dampening_hours_with_db

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.dampening_attributes


class PvForecastConfidenceSensor(_EnhancedSensorBase):
    """How well recent measured output agrees with the Solcast forecast (0–100).

    A decision aid for scheduling heavy loads, not a forecast: high means the next
    few hours can be trusted at this site; low means local conditions are diverging.
    """

    _attr_translation_key = "pv_forecast_confidence"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:check-decagram"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_PV_CONFIDENCE)

    @property
    def native_value(self) -> int | None:
        return self.coordinator.confidence

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.confidence_attributes


class _SiteSensorBase(_EnhancedSensorBase):
    """Base for per-array sensors, each attached to its own per-site HA device.

    A distinct ``DeviceInfo`` (keyed on ``entry_id + resource_id``, linked back to
    the main integration device via ``via_device``) groups every entity for one
    array onto its own card. Because ``_attr_has_entity_name`` is set, the device
    carries the array name and each entity name is just the bare metric (e.g.
    "Shading"), so HA renders "<Array> Shading" without duplicating the name.
    """

    def __init__(
        self,
        coordinator: SolcastEnhancedCoordinator,
        entry: ConfigEntry,
        site_id: str,
        name: str,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry, f"{key}_{site_id}")
        self._site_id = site_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{site_id}")},
            name=name,
            manufacturer="JimboHamez",
            model="Solcast Solar Enhanced Array",
            via_device=(DOMAIN, entry.entry_id),
            entry_type=DeviceEntryType.SERVICE,
        )


class SiteShadingSensor(_SiteSensorBase):
    """Per-array visibility: average daytime dampening (shading) plus tuning/confidence attrs.

    State is the array's average daytime dampening factor (1.0 = no shading correction,
    below 1.0 = the measured structural shading being applied to that array).
    """

    _attr_translation_key = "site_shading"
    _attr_icon = "mdi:home-roof"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry, site_id: str, name: str) -> None:
        super().__init__(coordinator, entry, site_id, name, "site_shading")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.site_shading(self._site_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.site_visibility_attributes(self._site_id)


class SiteOutputSensor(_SiteSensorBase):
    """Per-array measured generation: average kW over the just-completed half-hour.

    Unavailable until a multi-site cycle has produced a per-site reading.
    """

    _attr_translation_key = "site_output"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry, site_id: str, name: str) -> None:
        super().__init__(coordinator, entry, site_id, name, "site_output")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.site_output(self._site_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.site_output_attributes(self._site_id)


class SiteTunedTiltSensor(_SiteSensorBase):
    """Per-array tuned tilt: the optimised tilt from that array's last PV tuning run.

    Unavailable until the array has accumulated enough clear-sky history to tune.
    """

    _attr_translation_key = "site_tuned_tilt"
    _attr_native_unit_of_measurement = "°"
    _attr_icon = "mdi:angle-acute"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry, site_id: str, name: str) -> None:
        super().__init__(coordinator, entry, site_id, name, "site_tuned_tilt")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.site_tuned_tilt(self._site_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.site_tuned_tilt_attributes(self._site_id)


class SiteAzimuthSensor(_SiteSensorBase):
    """Per-array azimuth as configured in Solcast (held fixed, never tuned).

    Surfaces the discovered orientation on the array's own card alongside the tuned
    tilt; azimuth is deliberately not optimised, so this mirrors the Solcast value.
    """

    _attr_translation_key = "site_azimuth"
    _attr_native_unit_of_measurement = "°"
    _attr_icon = "mdi:compass"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry, site_id: str, name: str) -> None:
        super().__init__(coordinator, entry, site_id, name, "site_azimuth")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.site_azimuth(self._site_id)


class SiteTuningRmseSensor(_SiteSensorBase):
    """Per-array tuning fit error (RMSE, kW) — the trust signal for the tuned tilt.

    Lower means a tighter fit. Diagnostic-category, so it sits in the array device's
    Diagnostic section rather than on the main card face.
    """

    _attr_translation_key = "site_tuning_rmse"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry, site_id: str, name: str) -> None:
        super().__init__(coordinator, entry, site_id, name, "site_tuning_rmse")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.site_tuned_rmse(self._site_id)


class WeatherTempSensor(_EnhancedSensorBase):
    _attr_name = "Weather Temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_WEATHER_TEMP)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        temp = self.coordinator.data.get("weather", {}).get("temp")
        return float(temp) if temp is not None else None


class WeatherCloudsSensor(_EnhancedSensorBase):
    _attr_name = "Cloud Cover"
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:weather-cloudy"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_WEATHER_CLOUDS)

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        clouds = self.coordinator.data.get("weather", {}).get("clouds")
        return int(clouds) if clouds is not None else None


class BatteryChargeSensor(_RestoringSensorBase):
    _attr_name = "Battery Charge 30min Average"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_BATTERY_CHARGE)

    def _live_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("battery_charge")


class PvActualSensor(_RestoringSensorBase):
    _attr_name = "PV Power 30min Average"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-panel"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_PV_ACTUAL)

    def _live_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("pv_actual")


class PvExportSensor(_RestoringSensorBase):
    _attr_name = "PV Export 30min Average"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_PV_EXPORT)

    def _live_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("pv_export")


class BaseIntegrationSensor(_EnhancedSensorBase):
    _attr_name = "Base Integration Status"
    _attr_icon = "mdi:connection"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_BASE_STATUS)

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return str(self.coordinator.data.get("base_status", "not_detected"))
