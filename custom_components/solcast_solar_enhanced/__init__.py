"""Solcast Solar Enhanced integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv

from .const import (
    BASE_DOMAIN,
    CONF_DB_HOST,
    CONF_DB_NAME,
    CONF_DB_PASSWORD,
    CONF_DB_PORT,
    CONF_DB_USER,
    DOMAIN,
    PLATFORMS,
    SERVICE_FETCH_WEATHER,
    SERVICE_IMPORT_FROM_MYSQL,
    SERVICE_RUN_DAMPENING_UPDATE,
    SERVICE_RUN_PV_TUNING,
)

# All fields optional — omitted values fall back to the entry's stored MySQL
# config, so a user who is migrating off MySQL can call the service with no data.
IMPORT_FROM_MYSQL_SCHEMA = vol.Schema({
    vol.Optional(CONF_DB_HOST): cv.string,
    vol.Optional(CONF_DB_PORT): cv.port,
    vol.Optional(CONF_DB_USER): cv.string,
    vol.Optional(CONF_DB_PASSWORD): cv.string,
    vol.Optional(CONF_DB_NAME): cv.string,
})
from .coordinator import SolcastEnhancedCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solcast Solar Enhanced from a config entry."""
    # Verify base integration is loaded
    if BASE_DOMAIN not in hass.data and not hass.config_entries.async_entries(BASE_DOMAIN):
        raise ConfigEntryNotReady(
            f"Base integration '{BASE_DOMAIN}' is not loaded. "
            "Ensure solcast_solar is configured and running."
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

    async def handle_import_from_mysql(call: ServiceCall) -> None:
        mysql_opts = {k: v for k, v in call.data.items() if v is not None}
        total = 0
        for coord in hass.data[DOMAIN].values():
            total += await coord.async_import_from_mysql(mysql_opts)
        _LOGGER.info("import_from_mysql complete — %d record(s) imported", total)

    if not hass.services.has_service(DOMAIN, SERVICE_RUN_PV_TUNING):
        hass.services.async_register(DOMAIN, SERVICE_RUN_PV_TUNING, handle_run_pv_tuning)
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_DAMPENING_UPDATE):
        hass.services.async_register(DOMAIN, SERVICE_RUN_DAMPENING_UPDATE, handle_run_dampening_update)
    if not hass.services.has_service(DOMAIN, SERVICE_FETCH_WEATHER):
        hass.services.async_register(DOMAIN, SERVICE_FETCH_WEATHER, handle_fetch_weather)
    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_FROM_MYSQL):
        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT_FROM_MYSQL, handle_import_from_mysql,
            schema=IMPORT_FROM_MYSQL_SCHEMA,
        )

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
        hass.services.async_remove(DOMAIN, SERVICE_IMPORT_FROM_MYSQL)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
