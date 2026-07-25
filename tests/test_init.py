"""Test integration setup, teardown and action registration."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError

from custom_components.solcast_solar_enhanced.const import BASE_DOMAIN, DOMAIN
from custom_components.solcast_solar_enhanced import (
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
