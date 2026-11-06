"""Step 1's grid/generation power inputs, and clearing an optional sensor in options and reconfigure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_BATTERY_STAT_SENSOR,
    CONF_GENERATION_POWER_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_GRID_VOLTAGE_SENSOR,
    DOMAIN,
)

SUGGEST = "custom_components.solcast_solar_enhanced.config_flow.async_suggest_inputs"
SITE = {"latitude": -37.9, "longitude": 145.0, "capacity_kw": 5.0, "tilt": 20.0, "azimuth": 0.0}
LATER = (
    {"db_enabled": False},
    {"openmeteo_enabled": True, "owm_enabled": False, "owm_api_key": ""},
)
BATTERY = {"battery_enabled": False, "battery_mode": "net"}
TUNING = {"auto_tuning": False, "auto_dampening": False, "cloud_threshold": 20.0, "cloud_max_include": 60.0, "clipping_threshold": 0.95}


@pytest.fixture(autouse=True)
def _mock_base_integration(hass):
    mock_integration(hass, MockModule(BASE_DOMAIN), built_in=False)


def _suggested(result, key: str) -> Any:
    for marker in result["data_schema"].schema:
        if str(marker.schema if hasattr(marker, "schema") else marker) == key:
            return (getattr(marker, "description", None) or {}).get("suggested_value")
    raise KeyError(key)


async def test_step1_offers_the_power_inputs_prefilled(hass) -> None:
    with patch(SUGGEST, return_value={CONF_GRID_POWER_SENSOR: "sensor.grid_w", CONF_GENERATION_POWER_SENSOR: "sensor.ac_w"}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert _suggested(result, CONF_GRID_POWER_SENSOR) == "sensor.grid_w"
    assert _suggested(result, CONF_GENERATION_POWER_SENSOR) == "sensor.ac_w"


@pytest.mark.parametrize("key", [CONF_GRID_POWER_SENSOR, CONF_GENERATION_POWER_SENSOR])
@pytest.mark.parametrize("unit", ["kWh", "Wh", "MWh"])
async def test_an_energy_counter_is_refused(hass, key: str, unit: str) -> None:
    hass.states.async_set("sensor.counter", "123", {"unit_of_measurement": unit})
    with patch(SUGGEST, return_value={}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SITE, key: "sensor.counter"})
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "site"
    assert result["errors"] == {key: "power_sensor_required"}
    assert _suggested(result, key) == "sensor.counter"  # What was typed stays on the form.


@pytest.mark.parametrize("unit", ["W", "kW", None])
async def test_a_power_sensor_is_accepted(hass, unit: str | None) -> None:
    hass.states.async_set("sensor.grid_w", "-1500", {"unit_of_measurement": unit} if unit else {})
    with patch(SUGGEST, return_value={}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SITE, CONF_GRID_POWER_SENSOR: "sensor.grid_w"})
    assert result["step_id"] == "database"


async def test_the_options_flow_refuses_a_counter_too(hass) -> None:
    hass.states.async_set("sensor.counter", "123", {"unit_of_measurement": "kWh"})
    entry = MockConfigEntry(domain=DOMAIN, data={**SITE, "db_enabled": False}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**SITE, CONF_GENERATION_POWER_SENSOR: "sensor.counter"}
    )
    assert result["errors"] == {CONF_GENERATION_POWER_SENSOR: "power_sensor_required"}


# --- clearing an optional sensor (pre-dev8 the old value silently stayed) ---------------


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **SITE,
            "db_enabled": False,
            CONF_GRID_VOLTAGE_SENSOR: "sensor.v",
            CONF_GRID_POWER_SENSOR: "sensor.grid_w",
            CONF_BATTERY_STAT_SENSOR: "sensor.batt",
        },
        options={},
    )
    entry.add_to_hass(hass)
    return entry


async def test_clearing_sensors_in_options_sticks(hass) -> None:
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in (SITE, *LATER, BATTERY, TUNING):
        result = await hass.config_entries.options.async_configure(result["flow_id"], step)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    merged = {**entry.data, **entry.options}
    assert merged[CONF_GRID_VOLTAGE_SENSOR] is None
    assert merged[CONF_GRID_POWER_SENSOR] is None
    assert merged[CONF_BATTERY_STAT_SENSOR] is None


async def test_a_kept_sensor_in_options_is_kept(hass) -> None:
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in ({**SITE, CONF_GRID_VOLTAGE_SENSOR: "sensor.v"}, *LATER, {**BATTERY, CONF_BATTERY_STAT_SENSOR: "sensor.batt"}, TUNING):
        result = await hass.config_entries.options.async_configure(result["flow_id"], step)
    merged = {**entry.data, **entry.options}
    assert merged[CONF_GRID_VOLTAGE_SENSOR] == "sensor.v"
    assert merged[CONF_BATTERY_STAT_SENSOR] == "sensor.batt"
    assert merged[CONF_GRID_POWER_SENSOR] is None


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


def test_the_new_fields_and_error_are_translated() -> None:
    root = Path(__file__).parents[1] / "custom_components" / DOMAIN
    for path in [root / "strings.json", *(root / "translations").glob("*.json")]:
        d = json.loads(path.read_text(encoding="utf-8"))
        for section in ("config", "options"):
            site = d[section]["step"]["site"]
            for key in (CONF_GRID_POWER_SENSOR, CONF_GENERATION_POWER_SENSOR):
                assert site["data"][key] and site["data_description"][key], (path.name, section, key)
            assert d[section]["error"]["power_sensor_required"], (path.name, section)
