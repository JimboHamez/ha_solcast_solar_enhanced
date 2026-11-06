"""Clearing an optional sensor in the options flow or reconfigure must stick.

An empty optional field is absent from what the form submits, so merging the
submission with ``update`` kept the old value, and the coordinator (which reads
``{**data, **options}``) kept reading a sensor the user had just removed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_BATTERY_STAT_SENSOR,
    CONF_GRID_VOLTAGE_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    DOMAIN,
)

SITE = {"latitude": -37.9, "longitude": 145.0, "capacity_kw": 5.0, "tilt": 20.0, "azimuth": 0.0}
LATER = ({"db_enabled": False}, {"openmeteo_enabled": True, "owm_enabled": False, "owm_api_key": ""})
BATTERY = {"battery_enabled": False, "battery_mode": "net"}
TUNING = {"auto_tuning": False, "auto_dampening": False, "cloud_threshold": 20.0, "cloud_max_include": 60.0, "clipping_threshold": 0.95}


@pytest.fixture(autouse=True)
def _mock_base_integration(hass):
    mock_integration(hass, MockModule(BASE_DOMAIN), built_in=False)


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **SITE,
            "db_enabled": False,
            CONF_GRID_VOLTAGE_SENSOR: "sensor.v",
            CONF_PV_EXPORT_SENSOR: "sensor.export",
            CONF_BATTERY_STAT_SENSOR: "sensor.batt",
        },
        options={},
    )
    entry.add_to_hass(hass)
    return entry


async def _run_options(hass, entry, site, battery):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in (site, *LATER, battery, TUNING):
        result = await hass.config_entries.options.async_configure(result["flow_id"], step)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    return {**entry.data, **entry.options}


async def test_clearing_sensors_in_options_sticks(hass) -> None:
    merged = await _run_options(hass, _entry(hass), SITE, BATTERY)
    assert merged[CONF_GRID_VOLTAGE_SENSOR] is None
    assert merged[CONF_PV_EXPORT_SENSOR] is None
    assert merged[CONF_BATTERY_STAT_SENSOR] is None


async def test_kept_sensors_in_options_are_kept(hass) -> None:
    merged = await _run_options(
        hass,
        _entry(hass),
        {**SITE, CONF_GRID_VOLTAGE_SENSOR: "sensor.v"},
        {**BATTERY, CONF_BATTERY_STAT_SENSOR: "sensor.batt2"},
    )
    assert merged[CONF_GRID_VOLTAGE_SENSOR] == "sensor.v"
    assert merged[CONF_BATTERY_STAT_SENSOR] == "sensor.batt2"
    assert merged[CONF_PV_EXPORT_SENSOR] is None


async def test_clearing_sensors_in_reconfigure_sticks(hass) -> None:
    entry = _entry(hass)
    with patch("custom_components.solcast_solar_enhanced.async_setup_entry", return_value=True):
        result = await entry.start_reconfigure_flow(hass)
        for step in (SITE, *LATER, BATTERY, TUNING):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], step)
    assert result["type"] == FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    merged = {**entry.data, **entry.options}
    assert merged[CONF_GRID_VOLTAGE_SENSOR] is None
    assert merged[CONF_BATTERY_STAT_SENSOR] is None


async def test_non_entity_fields_are_not_blanked(hass) -> None:
    """Only entity pickers can be cleared; a field with a default keeps its submitted value."""
    merged = await _run_options(hass, _entry(hass), SITE, BATTERY)
    assert merged["latitude"] == pytest.approx(-37.9)
    assert merged["battery_mode"] == "net"
