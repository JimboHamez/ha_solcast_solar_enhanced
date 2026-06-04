"""Sensor entities for Solcast Solar Enhanced."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SENSOR_BASE_STATUS,
    SENSOR_BATTERY_CHARGE,
    SENSOR_DAMPENING,
    SENSOR_DB_RECORDS,
    SENSOR_FORECAST_NOW,
    SENSOR_FORECAST_TODAY,
    SENSOR_PV_ACTUAL,
    SENSOR_PV_EXPORT,
    SENSOR_TUNING_AZIMUTH,
    SENSOR_TUNING_EXPORT_EXCLUDED,
    SENSOR_TUNING_RMSE,
    SENSOR_TUNING_TILT,
    SENSOR_WEATHER_CLOUDS,
    SENSOR_WEATHER_TEMP,
)
from .coordinator import SolcastEnhancedCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SolcastEnhancedCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ForecastNowSensor(coordinator, entry),
        ForecastTodaySensor(coordinator, entry),
        TuningTiltSensor(coordinator, entry),
        TuningAzimuthSensor(coordinator, entry),
        TuningRmseSensor(coordinator, entry),
        TuningExportExcludedSensor(coordinator, entry),
        DbRecordsSensor(coordinator, entry),
        DampeningSensor(coordinator, entry),
        WeatherTempSensor(coordinator, entry),
        WeatherCloudsSensor(coordinator, entry),
        BatteryChargeSensor(coordinator, entry),
        PvActualSensor(coordinator, entry),
        PvExportSensor(coordinator, entry),
        BaseIntegrationSensor(coordinator, entry),
    ]
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
            manufacturer="Solcast",
            model="Enhanced Integration",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_coordinator()
        self.async_write_ha_state()

    def _update_from_coordinator(self) -> None:
        pass


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
        return self.coordinator.data.get("db_records", 0)

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
        return self.coordinator.data.get("weather", {}).get("temp")


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
        return self.coordinator.data.get("weather", {}).get("clouds")


class BatteryChargeSensor(_EnhancedSensorBase):
    _attr_name = "Battery Charge 30min Average"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_BATTERY_CHARGE)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("battery_charge")


class PvActualSensor(_EnhancedSensorBase):
    _attr_name = "PV Power 30min Average"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-panel"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_PV_ACTUAL)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("pv_actual")


class PvExportSensor(_EnhancedSensorBase):
    _attr_name = "PV Export 30min Average"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator: SolcastEnhancedCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_PV_EXPORT)

    @property
    def native_value(self) -> float | None:
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
        return self.coordinator.data.get("base_status", "not_detected")
