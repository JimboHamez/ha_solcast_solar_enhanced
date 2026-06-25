"""Solcast Solar Enhanced integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    BASE_DOMAIN,
    DOMAIN,
    PLATFORMS,
    SERVICE_FETCH_WEATHER,
    SERVICE_RUN_DAMPENING_UPDATE,
    SERVICE_RUN_PV_TUNING,
)
from .coordinator import SolcastEnhancedCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solcast Solar Enhanced from a config entry."""
    # Verify base integration is loaded
    if BASE_DOMAIN not in hass.data and not hass.config_entries.async_entries(BASE_DOMAIN):
        raise ConfigEntryNotReady(
            f"Base integration '{BASE_DOMAIN}' is not loaded. Ensure solcast_solar is configured and running."
        )

    coordinator = SolcastEnhancedCoordinator(hass, entry)

    try:
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        await coordinator.async_teardown()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def handle_run_pv_tuning(call: ServiceCall) -> None:
        for coord in hass.data[DOMAIN].values():
            await coord.async_force_pv_tuning()

    async def handle_run_dampening_update(call: ServiceCall) -> None:
        for coord in hass.data[DOMAIN].values():
            await coord.async_force_dampening_update()

    async def handle_fetch_weather(call: ServiceCall) -> None:
        for coord in hass.data[DOMAIN].values():
            await coord.async_force_fetch_weather()

    if not hass.services.has_service(DOMAIN, SERVICE_RUN_PV_TUNING):
        hass.services.async_register(DOMAIN, SERVICE_RUN_PV_TUNING, handle_run_pv_tuning)
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_DAMPENING_UPDATE):
        hass.services.async_register(DOMAIN, SERVICE_RUN_DAMPENING_UPDATE, handle_run_dampening_update)
    if not hass.services.has_service(DOMAIN, SERVICE_FETCH_WEATHER):
        hass.services.async_register(DOMAIN, SERVICE_FETCH_WEATHER, handle_fetch_weather)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: SolcastEnhancedCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_teardown()

    # Remove services if no entries remain
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RUN_PV_TUNING)
        hass.services.async_remove(DOMAIN, SERVICE_RUN_DAMPENING_UPDATE)
        hass.services.async_remove(DOMAIN, SERVICE_FETCH_WEATHER)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
