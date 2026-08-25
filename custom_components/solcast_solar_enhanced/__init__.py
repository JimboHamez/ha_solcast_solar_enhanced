"""Solcast Solar Enhanced integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

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
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers.device_registry import DeviceEntry
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


async def _run_on_each_entry(
    hass: HomeAssistant,
    method: Callable[[SolcastEnhancedCoordinator], Awaitable[None]],
    failure_key: str,
) -> None:
    """Run one coordinator method for every loaded entry, reporting failures.

    Args:
        hass: The Home Assistant instance.
        method: Coroutine function to await against each loaded coordinator.
        failure_key: ``exceptions`` translation key describing what did not happen,
            used when the coordinator raises something not already user-facing.

    Raises:
        ServiceValidationError: If no entry is loaded, or the coordinator rejects
            the call because the integration is not configured for it.
        HomeAssistantError: If the work itself fails, so the caller sees a dialog
            naming the failure instead of the action quietly logging and returning.
    """
    for coordinator in _loaded_coordinators(hass):
        try:
            await method(coordinator)
        except HomeAssistantError:
            # Already carries a translated, user-facing message (including
            # ServiceValidationError) — let it through untouched.
            raise
        except Exception as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=failure_key,
                translation_placeholders={"error": str(exc)},
            ) from exc


def _live_identifiers(
    entry: SolcastEnhancedConfigEntry, coordinator: SolcastEnhancedCoordinator
) -> set[tuple[str, str]]:
    """Return the device identifiers this entry should currently own.

    That is the main integration device plus one per configured array, matching
    the ``DeviceInfo`` built in :mod:`.entity`.
    """
    return {(DOMAIN, entry.entry_id)} | {
        (DOMAIN, f"{entry.entry_id}_{site_id}") for site_id, _ in coordinator.configured_sites_for_entities()
    }


def _async_remove_stale_devices(
    hass: HomeAssistant,
    entry: SolcastEnhancedConfigEntry,
    coordinator: SolcastEnhancedCoordinator,
) -> None:
    """Detach per-array devices for arrays the user has since unconfigured.

    Removing an array in the options flow drops its entities, but its device would
    otherwise linger in the registry as an empty card. The entry is detached rather
    than the device deleted outright, so a device another integration also claims
    survives.
    """
    device_registry = dr.async_get(hass)
    live = _live_identifiers(entry, coordinator)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if not device.identifiers & live:
            _LOGGER.debug("Removing stale array device %s", device.name)
            device_registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: SolcastEnhancedConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Allow the user to delete an array device that is no longer configured.

    Returns:
        True when the device is not one of the entry's current devices, which is
        what lets Home Assistant offer the delete button.
    """
    coordinator: SolcastEnhancedCoordinator | None = getattr(config_entry, "runtime_data", None)
    if coordinator is None:
        # Entry not loaded: nothing can vouch for the device, so allow removal.
        return True
    return not (device_entry.identifiers & _live_identifiers(config_entry, coordinator))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions.

    Registration happens here rather than in :func:`async_setup_entry` so that
    automations referencing these actions still validate while the config entry is
    unloaded; each handler resolves the entry at call time instead.
    """

    async def handle_run_pv_tuning(call: ServiceCall) -> None:
        await _run_on_each_entry(hass, lambda c: c.async_force_pv_tuning(), "pv_tuning_failed")

    async def handle_run_dampening_update(call: ServiceCall) -> None:
        await _run_on_each_entry(hass, lambda c: c.async_force_dampening_update(), "dampening_update_failed")

    async def handle_fetch_weather(call: ServiceCall) -> None:
        await _run_on_each_entry(hass, lambda c: c.async_force_fetch_weather(), "weather_fetch_failed")

    hass.services.async_register(DOMAIN, SERVICE_RUN_PV_TUNING, handle_run_pv_tuning)
    hass.services.async_register(DOMAIN, SERVICE_RUN_DAMPENING_UPDATE, handle_run_dampening_update)
    hass.services.async_register(DOMAIN, SERVICE_FETCH_WEATHER, handle_fetch_weather)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolcastEnhancedConfigEntry) -> bool:
    """Set up Solcast Solar Enhanced from a config entry."""
    # Verify the base integration is configured. Deliberately looser than
    # ``base_integration_available()``: a base entry that is merely mid-retry should
    # not stop local data collection, and the status sensor reports the live state.
    # (``hass.data`` is only consulted for pre-4.6.0 bases, which is all it means now.)
    if not hass.config_entries.async_entries(BASE_DOMAIN) and hass.data.get(BASE_DOMAIN) is None:
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

    _async_remove_stale_devices(hass, entry, coordinator)

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
