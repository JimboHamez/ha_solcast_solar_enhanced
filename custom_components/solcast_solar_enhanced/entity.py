"""Base entity classes for Solcast Solar Enhanced.

Every entity in the integration descends from :class:`SolcastEnhancedEntity`, which
owns the unique-id scheme and the main integration device. Two specialisations sit
on top: :class:`RestoringSensorEntity` for values that must survive a restart, and
:class:`SolcastEnhancedSiteEntity` for per-array entities that live on their own
per-site device.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import RestoreSensor, SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import SolcastEnhancedCoordinator

MANUFACTURER = "JimboHamez"


class SolcastEnhancedEntity(CoordinatorEntity["SolcastEnhancedCoordinator"], SensorEntity):
    """Base for every property-wide entity, attached to the main integration device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: SolcastEnhancedCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        """Set the unique id from ``key`` and attach to the integration device."""
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solcast Solar Enhanced",
            manufacturer=MANUFACTURER,
            model="Solcast Solar Enhanced Integration",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_coordinator()
        self.async_write_ha_state()

    def _update_from_coordinator(self) -> None:
        """Refresh any cached state from the coordinator. Overridden as needed."""


class RestoringSensorEntity(SolcastEnhancedEntity, RestoreSensor):
    """Sensor that restores its last value across restarts.

    The coordinator only produces data on the half-hour grid, so after a restart
    ``coordinator.data`` is empty for up to ~30 min, which would otherwise show
    the entity as *unknown* until the first update cycle. Restoring the last
    value bridges that gap. Subclasses implement ``_live_value()``; as soon as
    the coordinator yields a value it supersedes the restored one.
    """

    _restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous native value, if the recorder kept one."""
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
        """Live value when the coordinator has one, else the restored value."""
        live = self._live_value()
        return live if live is not None else self._restored_value


class SolcastEnhancedSiteEntity(SolcastEnhancedEntity):
    """Base for per-array entities, each attached to its own per-site HA device.

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
        """Set the per-site unique id and attach to that array's own device."""
        super().__init__(coordinator, entry, f"{key}_{site_id}")
        self._site_id = site_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{site_id}")},
            name=name,
            manufacturer=MANUFACTURER,
            model="Solcast Solar Enhanced Array",
            via_device=(DOMAIN, entry.entry_id),
            entry_type=DeviceEntryType.SERVICE,
        )
