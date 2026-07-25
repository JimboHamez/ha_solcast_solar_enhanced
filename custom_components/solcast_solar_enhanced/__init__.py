"""Solcast Solar Enhanced integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv

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
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers.typing import ConfigType

    from .coordinator import SolcastEnhancedConfigEntry

_LOGGER = logging.getLogger(__name__)

# Config-entry only: there is no YAML schema for this integration, but declaring
# `async_setup` obliges us to say so explicitly.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _loaded_coordinators(hass: HomeAssistant) -> list[SolcastEnhancedCoordinator]:
    """Return the coordinator of every loaded config entry.

    Raises:
        ServiceValidationError: If no entry is loaded, so an action called against
            a disabled or failed entry surfaces a dialog instead of silently
            doing nothing.
    """
    coordinators = [
        entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN) if entry.runtime_data
    ]
    if not coordinators:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded")
    return coordinators


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions.

    Registration happens here rather than in :func:`async_setup_entry` so that
    automations referencing these actions still validate while the config entry is
    unloaded; each handler resolves the entry at call time instead.
    """

    async def handle_run_pv_tuning(call: ServiceCall) -> None:
        for coordinator in _loaded_coordinators(hass):
            await coordinator.async_force_pv_tuning()

    async def handle_run_dampening_update(call: ServiceCall) -> None:
        for coordinator in _loaded_coordinators(hass):
            await coordinator.async_force_dampening_update()

    async def handle_fetch_weather(call: ServiceCall) -> None:
        for coordinator in _loaded_coordinators(hass):
            await coordinator.async_force_fetch_weather()

    hass.services.async_register(DOMAIN, SERVICE_RUN_PV_TUNING, handle_run_pv_tuning)
    hass.services.async_register(DOMAIN, SERVICE_RUN_DAMPENING_UPDATE, handle_run_dampening_update)
    hass.services.async_register(DOMAIN, SERVICE_FETCH_WEATHER, handle_fetch_weather)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolcastEnhancedConfigEntry) -> bool:
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

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolcastEnhancedConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_teardown()

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: SolcastEnhancedConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
