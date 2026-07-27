"""Test integration setup, teardown and action registration."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.solcast_solar_enhanced.const import BASE_DOMAIN, DOMAIN
from custom_components.solcast_solar_enhanced import (
    _async_remove_stale_devices,
    async_reload_entry,
    async_remove_config_entry_device,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)


async def test_setup_raises_when_base_missing(hass, mock_config_entry):
    """async_setup_entry raises ConfigEntryNotReady when solcast_solar is absent."""
    mock_config_entry.add_to_hass(hass)
    hass.data.pop(BASE_DOMAIN, None)

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, mock_config_entry)


async def test_setup_succeeds_with_base_present(hass, mock_config_entry, mock_base_coordinator):
    """async_setup_entry succeeds and parks the coordinator on entry.runtime_data."""
    mock_config_entry.add_to_hass(hass)

    mock_coordinator = MagicMock()
    mock_coordinator.async_setup = AsyncMock()
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch(
            "custom_components.solcast_solar_enhanced.SolcastEnhancedCoordinator",
            return_value=mock_coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await async_setup_entry(hass, mock_config_entry)

    assert result is True
    # The coordinator rides on the entry, not in hass.data (quality scale: runtime-data).
    assert mock_config_entry.runtime_data is mock_coordinator
    assert DOMAIN not in hass.data


async def test_services_registered_by_async_setup(hass):
    """The three actions register from async_setup, with no config entry involved.

    Registering at component setup (rather than entry setup) is what lets an
    automation referencing them validate while the entry is unloaded.
    """
    assert await async_setup(hass, {}) is True

    assert hass.services.has_service(DOMAIN, "run_pv_tuning")
    assert hass.services.has_service(DOMAIN, "run_dampening_update")
    assert hass.services.has_service(DOMAIN, "fetch_weather")


@pytest.mark.parametrize(
    "service",
    ["run_pv_tuning", "run_dampening_update", "fetch_weather"],
)
async def test_action_raises_when_no_entry_loaded(hass, service):
    """Calling an action with no loaded entry raises instead of silently no-opping."""
    await async_setup(hass, {})

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, service, {}, blocking=True)


@pytest.mark.parametrize(
    ("service", "method"),
    [
        ("run_pv_tuning", "async_force_pv_tuning"),
        ("run_dampening_update", "async_force_dampening_update"),
        ("fetch_weather", "async_force_fetch_weather"),
    ],
)
async def test_action_dispatches_to_loaded_entry(hass, service, method):
    """Each action resolves the loaded entry at call time and drives its coordinator."""
    await async_setup(hass, {})

    coordinator = MagicMock()
    setattr(coordinator, method, AsyncMock())
    entry = SimpleNamespace(runtime_data=coordinator)

    with patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]):
        await hass.services.async_call(DOMAIN, service, {}, blocking=True)

    getattr(coordinator, method).assert_awaited_once()


@pytest.mark.parametrize(
    ("service", "method"),
    [
        ("run_pv_tuning", "async_force_pv_tuning"),
        ("run_dampening_update", "async_force_dampening_update"),
        ("fetch_weather", "async_force_fetch_weather"),
    ],
)
async def test_action_reports_a_failure_instead_of_swallowing_it(hass, service, method):
    """A failure inside the coordinator surfaces as a user-facing error.

    Quality scale (action-exceptions): the caller must be told the work did not
    happen. Letting the raw exception through would show 'unknown error', so it is
    translated — and the original is kept as the cause.
    """
    await async_setup(hass, {})

    coordinator = MagicMock()
    boom = RuntimeError("database is locked")
    setattr(coordinator, method, AsyncMock(side_effect=boom))
    entry = SimpleNamespace(runtime_data=coordinator)

    with patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, service, {}, blocking=True)

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_placeholders == {"error": "database is locked"}
    assert err.value.__cause__ is boom


async def test_action_passes_a_user_facing_error_through_untouched(hass):
    """An error the coordinator already phrased for the user is not re-wrapped.

    ``async_force_fetch_weather`` raises ServiceValidationError when no weather
    source is enabled; re-wrapping it would replace that specific advice with the
    generic 'could not be completed' message, and downgrade it from a validation
    error to a plain failure.
    """
    await async_setup(hass, {})

    original = ServiceValidationError(translation_domain=DOMAIN, translation_key="no_weather_source")
    coordinator = MagicMock()
    coordinator.async_force_fetch_weather = AsyncMock(side_effect=original)
    entry = SimpleNamespace(runtime_data=coordinator)

    with patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, "fetch_weather", {}, blocking=True)

    assert err.value is original


async def test_setup_tears_the_coordinator_down_when_the_first_refresh_fails(hass, mock_config_entry):
    """A setup that never completes must not leave its store/listeners running.

    HA retries the entry after ConfigEntryNotReady, so skipping teardown would
    leak a database handle and a time listener on every attempt.
    """
    mock_config_entry.add_to_hass(hass)
    hass.data[BASE_DOMAIN] = MagicMock()

    coordinator = MagicMock()
    coordinator.async_setup = AsyncMock()
    coordinator.async_config_entry_first_refresh = AsyncMock(side_effect=ConfigEntryNotReady("not yet"))
    coordinator.async_teardown = AsyncMock()

    with patch(
        "custom_components.solcast_solar_enhanced.SolcastEnhancedCoordinator",
        return_value=coordinator,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, mock_config_entry)

    coordinator.async_teardown.assert_awaited_once()


async def test_options_change_reloads_the_entry(hass, mock_config_entry):
    """Options are read at setup, so a change only takes effect via a reload."""
    mock_config_entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload:
        await async_reload_entry(hass, mock_config_entry)

    reload.assert_awaited_once_with(mock_config_entry.entry_id)


async def test_unload_tears_down_coordinator(hass, mock_config_entry):
    """Unloading forwards to the platforms and tears the coordinator down."""
    mock_config_entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.async_teardown = AsyncMock()
    mock_config_entry.runtime_data = coordinator

    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, mock_config_entry) is True

    coordinator.async_teardown.assert_awaited_once()


# ---------------------------------------------------------------------------
# Stale per-array devices (quality scale: gold/stale-devices)
# ---------------------------------------------------------------------------


def _register_devices(hass, entry, site_ids):
    """Register the main device plus one per-array device for each site id."""
    registry = dr.async_get(hass)
    devices = {
        "main": registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solcast Solar Enhanced",
        )
    }
    for site_id in site_ids:
        devices[site_id] = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}_{site_id}")},
            name=f"Array {site_id}",
        )
    return registry, devices


def _coordinator_with_sites(*site_ids):
    """A coordinator stub reporting exactly these configured arrays."""
    coordinator = MagicMock()
    coordinator.configured_sites_for_entities.return_value = [(sid, f"Array {sid}") for sid in site_ids]
    return coordinator


async def test_stale_array_device_is_removed(hass, mock_config_entry):
    """An array dropped from the options flow loses its leftover device card.

    Its entities go on reload, but the device would otherwise linger as an empty
    card the user cannot get rid of.
    """
    mock_config_entry.add_to_hass(hass)
    registry, devices = _register_devices(hass, mock_config_entry, ["site_a", "site_gone"])

    # site_gone is no longer configured.
    _async_remove_stale_devices(hass, mock_config_entry, _coordinator_with_sites("site_a"))

    assert registry.async_get(devices["site_gone"].id) is None
    # The still-configured array and the main device are untouched.
    assert registry.async_get(devices["site_a"].id) is not None
    assert registry.async_get(devices["main"].id) is not None


async def test_no_devices_removed_when_all_configured(hass, mock_config_entry):
    """The cleanup is a no-op while every registered array is still configured."""
    mock_config_entry.add_to_hass(hass)
    registry, devices = _register_devices(hass, mock_config_entry, ["site_a", "site_b"])

    _async_remove_stale_devices(hass, mock_config_entry, _coordinator_with_sites("site_a", "site_b"))

    assert all(registry.async_get(device.id) is not None for device in devices.values())


async def test_manual_removal_refused_for_a_live_device(hass, mock_config_entry):
    """HA must not offer to delete a device the integration still owns."""
    mock_config_entry.add_to_hass(hass)
    _, devices = _register_devices(hass, mock_config_entry, ["site_a"])
    mock_config_entry.runtime_data = _coordinator_with_sites("site_a")

    assert await async_remove_config_entry_device(hass, mock_config_entry, devices["site_a"]) is False
    assert await async_remove_config_entry_device(hass, mock_config_entry, devices["main"]) is False


async def test_manual_removal_allowed_for_an_orphan_device(hass, mock_config_entry):
    """A device for an array we no longer configure can be deleted by hand."""
    mock_config_entry.add_to_hass(hass)
    _, devices = _register_devices(hass, mock_config_entry, ["site_gone"])
    mock_config_entry.runtime_data = _coordinator_with_sites("site_a")

    assert await async_remove_config_entry_device(hass, mock_config_entry, devices["site_gone"]) is True


async def test_manual_removal_allowed_when_entry_not_loaded(hass, mock_config_entry):
    """With no coordinator to vouch for it, the user is allowed to remove a device."""
    mock_config_entry.add_to_hass(hass)
    _, devices = _register_devices(hass, mock_config_entry, ["site_a"])

    assert await async_remove_config_entry_device(hass, mock_config_entry, devices["site_a"]) is True
