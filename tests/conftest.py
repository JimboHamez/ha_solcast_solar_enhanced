"""Global fixtures for Solcast Solar Enhanced tests."""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_CAPACITY_KW,
    CONF_CLIPPING_THRESHOLD,
    CONF_CLOUD_MAX_INCLUDE,
    CONF_CLOUD_THRESHOLD,
    CONF_DB_ENABLED,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_OWM_ENABLED,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_TILT,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_CLOUD_THRESHOLD,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"

MOCK_CONFIG = {
    CONF_LATITUDE: -37.9,
    CONF_LONGITUDE: 145.0,
    CONF_CAPACITY_KW: 5.0,
    CONF_TILT: 20.0,
    CONF_AZIMUTH: 0.0,
    CONF_PV_ACTUAL_SENSOR: "sensor.pv_power_30min",
    CONF_PV_EXPORT_SENSOR: "sensor.pv_export_30min",
    CONF_DB_ENABLED: False,
    CONF_OWM_ENABLED: False,
    CONF_AUTO_TUNING: False,
    CONF_AUTO_DAMPENING: False,
    CONF_CLOUD_THRESHOLD: DEFAULT_CLOUD_THRESHOLD,
    CONF_CLOUD_MAX_INCLUDE: DEFAULT_CLOUD_MAX_INCLUDE,
    CONF_CLIPPING_THRESHOLD: DEFAULT_CLIPPING_THRESHOLD,
}


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={},
        entry_id="test_solcast_enhanced",
        title="Solcast Solar Enhanced",
    )


@pytest.fixture
def mock_base_coordinator(hass):
    """Inject a minimal solcast_solar coordinator stub into hass.data."""
    coordinator = type("MockCoord", (), {
        "data": {
            "forecast_now": 2.5,
            "forecast_today": 18.0,
            "pv_estimate": 3.0,
            "pv_estimate10": 2.0,
            "pv_estimate90": 4.0,
        }
    })()
    hass.data.setdefault(BASE_DOMAIN, coordinator)
    return coordinator
