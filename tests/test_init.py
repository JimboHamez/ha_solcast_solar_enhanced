"""Test integration setup and teardown."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.solcast_solar_enhanced.const import BASE_DOMAIN, DOMAIN
from custom_components.solcast_solar_enhanced import async_setup_entry, async_unload_entry


async def test_setup_raises_when_base_missing(hass, mock_config_entry):
    """async_setup_entry raises ConfigEntryNotReady when solcast_solar is absent."""
    mock_config_entry.add_to_hass(hass)
    # Ensure BASE_DOMAIN is not in hass.data and has no config entries
    hass.data.pop(BASE_DOMAIN, None)

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, mock_config_entry)


async def test_setup_succeeds_with_base_present(hass, mock_config_entry, mock_base_coordinator):
    """async_setup_entry succeeds when solcast_solar coordinator is in hass.data."""
    mock_config_entry.add_to_hass(hass)

    mock_coordinator = MagicMock()
    mock_coordinator.async_setup = AsyncMock()
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch(
            "custom_components.solcast_solar_enhanced.SolcastEnhancedCoordinator",
            return_value=mock_coordinator,
        ),
        patch(
            "custom_components.solcast_solar_enhanced.hass.config_entries.async_forward_entry_setups",
            return_value=True,
            create=True,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=True),
    ):
        result = await async_setup_entry(hass, mock_config_entry)

    assert result is True
    assert mock_config_entry.entry_id in hass.data[DOMAIN]


async def test_services_registered_after_setup(hass, mock_config_entry, mock_base_coordinator):
    """The three services are registered after a successful setup."""
    mock_config_entry.add_to_hass(hass)

    mock_coordinator = MagicMock()
    mock_coordinator.async_setup = AsyncMock()
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()
    mock_coordinator.async_force_pv_tuning = AsyncMock()
    mock_coordinator.async_force_dampening_update = AsyncMock()
    mock_coordinator.async_force_fetch_weather = AsyncMock()

    with (
        patch(
            "custom_components.solcast_solar_enhanced.SolcastEnhancedCoordinator",
            return_value=mock_coordinator,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=True),
    ):
        await async_setup_entry(hass, mock_config_entry)

    assert hass.services.has_service(DOMAIN, "run_pv_tuning")
    assert hass.services.has_service(DOMAIN, "run_dampening_update")
    assert hass.services.has_service(DOMAIN, "fetch_weather")
