"""Test the diagnostics download (quality scale: gold/diagnostics).

Two things have to hold: the payload must never carry the OpenWeatherMap key or
the site coordinates, and it must actually reflect coordinator state rather than
a hardcoded skeleton — so every assertion here uses a distinctive value that a
stubbed-out implementation could not produce by accident.
"""
from __future__ import annotations

import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced.const import (
    CONF_AZIMUTH,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_OWM_API_KEY,
    CONF_OWM_ENABLED,
    CONF_SITE_GROUPS,
    CONF_SITE_TOPOLOGY,
    CONF_TILT,
    DOMAIN,
    SITE_TOPOLOGY_DC_SPLIT,
)
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.diagnostics import async_get_config_entry_diagnostics

SECRET_KEY = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def entry_with_secrets(hass) -> MockConfigEntry:
    """An entry carrying a real-looking OWM key, coordinates and two arrays."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LATITUDE: -37.8136,
            CONF_LONGITUDE: 144.9631,
            CONF_TILT: 27.5,
            CONF_AZIMUTH: -14.0,
            CONF_OWM_ENABLED: True,
            CONF_OWM_API_KEY: SECRET_KEY,
        },
        options={
            CONF_SITE_TOPOLOGY: SITE_TOPOLOGY_DC_SPLIT,
            CONF_SITE_GROUPS: [
                {
                    "ac_sensor": "sensor.inverter_ac",
                    "strings": [
                        {"site": "aaaa-1111", "dc_sensor": "sensor.dc_roof", "name": "Roof"},
                        {"site": "bbbb-2222", "dc_sensor": "sensor.dc_ground", "name": "Ground"},
                    ],
                }
            ],
        },
        entry_id="diag_entry",
        title="Solcast Solar Enhanced",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def loaded_entry(hass, entry_with_secrets) -> MockConfigEntry:
    """The same entry with a coordinator carrying distinctive live state."""
    coordinator = SolcastEnhancedCoordinator(hass, entry_with_secrets)
    coordinator.data = {"pv_actual": 4.25, "pv_estimate": 5.5}
    coordinator._weather = {"temp": 18.5, "clouds": 42, "description": "scattered clouds"}
    coordinator._irradiance = {"ghi": 611.0, "dni": 780.0, "dhi": 95.0}
    coordinator._db_record_count = 1234
    coordinator._db_latest_period_end = "2026-07-27T04:30:00+00:00"
    coordinator._db_sites = ["_total", "aaaa-1111", "bbbb-2222"]
    coordinator._tuning_result = {
        "tilt": 23.75,
        "azimuth": 14.0,
        "rmse_kw": 0.317,
        "mae_kw": 0.244,
        "n_records": 88,
        "tilt_identifiable": True,
    }
    coordinator._dampening_table = [{"factor": 0.62, "source": "db_history", "alpha": 0.8} for _ in range(48)]
    coordinator._dampening_pushed = {"aaaa-1111"}
    coordinator._orientation_advisory = True
    coordinator._orientation_advisory_targets = {"bbbb-2222"}
    coordinator._dc_telemetry = {"max_voltage": 412.5}
    entry_with_secrets.runtime_data = coordinator
    return entry_with_secrets


async def test_secrets_are_redacted(hass, loaded_entry):
    """The API key and the coordinates never leave in the payload."""
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert diag["entry"]["data"][CONF_OWM_API_KEY] != SECRET_KEY
    assert diag["entry"]["data"][CONF_LATITUDE] != -37.8136
    assert diag["entry"]["data"][CONF_LONGITUDE] != 144.9631
    # Belt and braces: the secret must not survive anywhere in the payload, not
    # just at the key we happened to check.
    assert SECRET_KEY not in json.dumps(diag, default=str)
    assert "144.9631" not in json.dumps(diag, default=str)


async def test_non_secret_config_is_kept(hass, loaded_entry):
    """Redaction is targeted: the settings needed to debug a report survive."""
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert diag["entry"]["data"][CONF_TILT] == 27.5
    assert diag["entry"]["data"][CONF_OWM_ENABLED] is True
    assert diag["entry"]["options"][CONF_SITE_TOPOLOGY] == SITE_TOPOLOGY_DC_SPLIT
    assert len(diag["entry"]["options"][CONF_SITE_GROUPS][0]["strings"]) == 2


async def test_coordinator_state_is_reported(hass, loaded_entry):
    """The snapshot carries live coordinator state, not a fixed skeleton."""
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)

    coord = diag["coordinator"]
    assert coord["data"] == {"pv_actual": 4.25, "pv_estimate": 5.5}
    assert coord["weather"]["clouds"] == 42
    assert coord["irradiance"]["ghi"] == 611.0
    assert coord["configured_orientation"] == {"tilt": 27.5, "azimuth": -14.0}
    assert coord["site_topology"] == SITE_TOPOLOGY_DC_SPLIT
    # Names come from the config groups, in configured order.
    assert coord["configured_sites"] == [
        {"resource_id": "aaaa-1111", "name": "Roof"},
        {"resource_id": "bbbb-2222", "name": "Ground"},
    ]

    assert diag["storage"]["record_count"] == 1234
    assert diag["storage"]["latest_period_end"] == "2026-07-27T04:30:00+00:00"
    assert diag["storage"]["sites_in_db"] == ["_total", "aaaa-1111", "bbbb-2222"]

    assert diag["tuning"]["tilt"] == 23.75
    assert diag["tuning"]["rmse_kw"] == 0.317
    assert diag["tuning"]["extra"]["n_records"] == 88

    assert diag["dc_telemetry"] == {"max_voltage": 412.5}


async def test_dampening_reports_the_half_hour_curve(hass, loaded_entry):
    """The raw 48-slot curve is included — that is what gets averaged and pushed.

    The hour-averaged sensor attributes would hide a slot-level asymmetry, which
    is exactly what a "dampening looks wrong" report needs to show.
    """
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)

    damp = diag["dampening"]
    assert len(damp["slots"]) == 48
    assert damp["slots"][0]["factor"] == 0.62
    assert damp["hours_with_db"] == 48
    assert damp["pushed_targets"] == ["aaaa-1111"]
    assert damp["orientation_advisory"] is True
    assert damp["orientation_advisory_targets"] == ["bbbb-2222"]


async def test_diagnostics_without_runtime_data(hass, entry_with_secrets):
    """An entry that failed to set up still yields a redacted payload, not a crash."""
    diag = await async_get_config_entry_diagnostics(hass, entry_with_secrets)

    assert diag["coordinator"] is None
    assert SECRET_KEY not in json.dumps(diag, default=str)
    # No coordinator means no snapshot sections at all.
    assert "dampening" not in diag


async def test_payload_is_json_serialisable(hass, loaded_entry):
    """Home Assistant serialises the payload to JSON — sets etc. must not leak in."""
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)

    json.dumps(diag)
