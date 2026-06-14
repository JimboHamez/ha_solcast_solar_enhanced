"""Test the 5-step config flow and options flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockModule, mock_integration

from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MODE,
    CONF_CAPACITY_KW,
    CONF_CLIPPING_THRESHOLD,
    CONF_CLOUD_MAX_INCLUDE,
    CONF_CLOUD_THRESHOLD,
    CONF_DB_ENABLED,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_OWM_API_KEY,
    CONF_OPENMETEO_ENABLED,
    CONF_OWM_ENABLED,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_TILT,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_CLOUD_THRESHOLD,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def _mock_base_integration(hass):
    """Register a stub solcast_solar integration so the now-hard manifest
    dependency resolves during entry/flow setup in these tests."""
    mock_integration(hass, MockModule(BASE_DOMAIN), built_in=False)

STEP_SITE = {
    CONF_LATITUDE: -37.9,
    CONF_LONGITUDE: 145.0,
    CONF_CAPACITY_KW: 5.0,
    CONF_TILT: 20.0,
    CONF_AZIMUTH: 0.0,
    CONF_PV_ACTUAL_SENSOR: "sensor.pv_power_30min",
    CONF_PV_EXPORT_SENSOR: "sensor.pv_export_30min",
}

STEP_DATABASE = {
    CONF_DB_ENABLED: False,
}

STEP_WEATHER = {
    CONF_OPENMETEO_ENABLED: True,
    CONF_OWM_ENABLED: False,
    CONF_OWM_API_KEY: "",
}

STEP_BATTERY = {
    CONF_BATTERY_ENABLED: False,
    CONF_BATTERY_MODE: "net",
}

STEP_TUNING = {
    CONF_AUTO_TUNING: False,
    CONF_AUTO_DAMPENING: False,
    CONF_CLOUD_THRESHOLD: DEFAULT_CLOUD_THRESHOLD,
    CONF_CLOUD_MAX_INCLUDE: DEFAULT_CLOUD_MAX_INCLUDE,
    CONF_CLIPPING_THRESHOLD: DEFAULT_CLIPPING_THRESHOLD,
}


async def _run_full_flow(hass) -> dict:
    """Helper: walk through all 5 steps and return the final result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "site"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], STEP_SITE
    )
    assert result["step_id"] == "database"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], STEP_DATABASE
    )
    assert result["step_id"] == "weather"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], STEP_WEATHER
    )
    assert result["step_id"] == "battery"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], STEP_BATTERY
    )
    assert result["step_id"] == "tuning"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], STEP_TUNING
    )
    return result


async def test_step_site_shows_form(hass):
    """First step returns a FORM with step_id 'site'."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {}


async def test_single_config_entry_enforced(hass, mock_config_entry):
    """With single_config_entry set, a second add aborts — there is one base
    integration, one property and one shared database."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_full_flow_creates_entry(hass):
    """Completing all 5 steps creates a config entry."""
    with patch(
        "custom_components.solcast_solar_enhanced.async_setup_entry",
        return_value=True,
    ):
        result = await _run_full_flow(hass)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Solcast Solar Enhanced"
    data = result["data"]
    assert data[CONF_LATITUDE] == pytest.approx(-37.9)
    assert data[CONF_DB_ENABLED] is False
    assert data[CONF_AUTO_TUNING] is False


async def test_options_flow_shows_site_first(hass, mock_config_entry):
    """Options flow starts at the site step."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "site"


async def test_options_flow_completes(hass, mock_config_entry):
    """Options flow can be completed and saves updated options."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    for step_data in (STEP_SITE, STEP_DATABASE, STEP_WEATHER, STEP_BATTERY):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], step_data
        )

    updated_tuning = {**STEP_TUNING, CONF_CLOUD_THRESHOLD: 25}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], updated_tuning
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CLOUD_THRESHOLD] == 25
