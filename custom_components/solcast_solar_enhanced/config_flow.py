"""Config flow for Solcast Solar Enhanced — 5-step setup wizard."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

try:
    from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
    _ENTITY_SELECTOR_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ENTITY_SELECTOR_AVAILABLE = False

from .const import (
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MODE,
    CONF_BATTERY_NET_SENSOR,
    CONF_BATTERY_STAT_SENSOR,
    CONF_CAPACITY_KW,
    CONF_CLIPPING_THRESHOLD,
    CONF_CLOUD_MAX_INCLUDE,
    CONF_CLOUD_THRESHOLD,
    CONF_DB_ENABLED,
    CONF_DB_HOST,
    CONF_DB_NAME,
    CONF_DB_PASSWORD,
    CONF_DB_PORT,
    CONF_DB_READONLY,
    CONF_DB_USER,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_OWM_API_KEY,
    CONF_OWM_ENABLED,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_TILT,
    DEFAULT_AUTO_DAMPENING,
    DEFAULT_AUTO_TUNING,
    DEFAULT_AZIMUTH,
    DEFAULT_CAPACITY_KW,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_CLOUD_THRESHOLD,
    DEFAULT_DB_HOST,
    DEFAULT_DB_NAME,
    DEFAULT_DB_PORT,
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_TILT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _entity_selector(domain: str = "sensor") -> Any:
    if _ENTITY_SELECTOR_AVAILABLE:
        try:
            return EntitySelector(EntitySelectorConfig(domain=domain))
        except Exception:  # noqa: BLE001
            pass
    return TextSelector()


class SolcastEnhancedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """5-step setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_site(user_input)

    async def async_step_site(self, user_input: dict[str, Any] | None = None):
        """Step 1 — Site & System."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_database()

        schema = vol.Schema({
            vol.Required(CONF_LATITUDE, default=DEFAULT_LATITUDE): NumberSelector(
                NumberSelectorConfig(min=-90, max=90, step=0.001)
            ),
            vol.Required(CONF_LONGITUDE, default=DEFAULT_LONGITUDE): NumberSelector(
                NumberSelectorConfig(min=-180, max=180, step=0.001)
            ),
            vol.Required(CONF_CAPACITY_KW, default=DEFAULT_CAPACITY_KW): NumberSelector(
                NumberSelectorConfig(min=0.1, max=1000, step=0.1)
            ),
            vol.Required(CONF_TILT, default=DEFAULT_TILT): NumberSelector(
                NumberSelectorConfig(min=0, max=90, step=0.1)
            ),
            vol.Required(CONF_AZIMUTH, default=DEFAULT_AZIMUTH): NumberSelector(
                NumberSelectorConfig(min=-180, max=180, step=0.1)
            ),
            vol.Optional(CONF_PV_ACTUAL_SENSOR): _entity_selector(),
            vol.Optional(CONF_PV_EXPORT_SENSOR): _entity_selector(),
            vol.Optional(CONF_BATTERY_STAT_SENSOR): _entity_selector(),
        })
        return self.async_show_form(step_id="site", data_schema=schema, errors={})

    async def async_step_database(self, user_input: dict[str, Any] | None = None):
        """Step 2 — MySQL Database (optional)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_owm()

        schema = vol.Schema({
            vol.Required(CONF_DB_ENABLED, default=False): BooleanSelector(),
            vol.Optional(CONF_DB_HOST, default=DEFAULT_DB_HOST): TextSelector(),
            vol.Optional(CONF_DB_PORT, default=DEFAULT_DB_PORT): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, step=1)
            ),
            vol.Optional(CONF_DB_USER, default=""): TextSelector(),
            vol.Optional(CONF_DB_PASSWORD, default=""): TextSelector(
                TextSelectorConfig(type="password")
            ),
            vol.Optional(CONF_DB_NAME, default=DEFAULT_DB_NAME): TextSelector(),
            vol.Required(CONF_DB_READONLY, default=False): BooleanSelector(),
        })
        return self.async_show_form(step_id="database", data_schema=schema)

    async def async_step_owm(self, user_input: dict[str, Any] | None = None):
        """Step 3 — OpenWeatherMap (optional)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()

        schema = vol.Schema({
            vol.Required(CONF_OWM_ENABLED, default=False): BooleanSelector(),
            vol.Optional(CONF_OWM_API_KEY, default=""): TextSelector(
                TextSelectorConfig(type="password")
            ),
        })
        return self.async_show_form(step_id="owm", data_schema=schema)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        """Step 4 — Battery Storage (optional)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_tuning()

        schema = vol.Schema({
            vol.Required(CONF_BATTERY_ENABLED, default=False): BooleanSelector(),
            vol.Optional(CONF_BATTERY_MODE, default="net"): SelectSelector(
                SelectSelectorConfig(options=["net", "separate"], mode="dropdown")
            ),
            vol.Optional(CONF_BATTERY_NET_SENSOR): _entity_selector(),
            vol.Optional(CONF_BATTERY_CHARGE_SENSOR): _entity_selector(),
        })
        return self.async_show_form(step_id="battery", data_schema=schema)

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None):
        """Step 5 — PV Tuning & Dampening."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Solcast Solar Enhanced", data=self._data)

        schema = vol.Schema({
            vol.Required(CONF_AUTO_TUNING, default=DEFAULT_AUTO_TUNING): BooleanSelector(),
            vol.Required(CONF_AUTO_DAMPENING, default=DEFAULT_AUTO_DAMPENING): BooleanSelector(),
            vol.Required(CONF_CLOUD_THRESHOLD, default=DEFAULT_CLOUD_THRESHOLD): NumberSelector(
                NumberSelectorConfig(min=10, max=50, step=1)
            ),
            vol.Required(CONF_CLOUD_MAX_INCLUDE, default=DEFAULT_CLOUD_MAX_INCLUDE): NumberSelector(
                NumberSelectorConfig(min=20, max=100, step=1)
            ),
            vol.Required(CONF_CLIPPING_THRESHOLD, default=DEFAULT_CLIPPING_THRESHOLD): NumberSelector(
                NumberSelectorConfig(min=0.5, max=1.0, step=0.01)
            ),
        })
        return self.async_show_form(step_id="tuning", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return SolcastEnhancedOptionsFlow()


class SolcastEnhancedOptionsFlow(config_entries.OptionsFlow):
    """Options flow — reconfigures all settings."""

    def __init__(self) -> None:
        self._opts: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_site(user_input)

    async def async_step_site(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_database()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_LATITUDE, default=current.get(CONF_LATITUDE, DEFAULT_LATITUDE)): NumberSelector(
                NumberSelectorConfig(min=-90, max=90, step=0.001)
            ),
            vol.Required(CONF_LONGITUDE, default=current.get(CONF_LONGITUDE, DEFAULT_LONGITUDE)): NumberSelector(
                NumberSelectorConfig(min=-180, max=180, step=0.001)
            ),
            vol.Required(CONF_CAPACITY_KW, default=current.get(CONF_CAPACITY_KW, DEFAULT_CAPACITY_KW)): NumberSelector(
                NumberSelectorConfig(min=0.1, max=1000, step=0.1)
            ),
            vol.Required(CONF_TILT, default=current.get(CONF_TILT, DEFAULT_TILT)): NumberSelector(
                NumberSelectorConfig(min=0, max=90, step=0.1)
            ),
            vol.Required(CONF_AZIMUTH, default=current.get(CONF_AZIMUTH, DEFAULT_AZIMUTH)): NumberSelector(
                NumberSelectorConfig(min=-180, max=180, step=0.1)
            ),
            vol.Optional(CONF_PV_ACTUAL_SENSOR, description={"suggested_value": current.get(CONF_PV_ACTUAL_SENSOR)}): _entity_selector(),
            vol.Optional(CONF_PV_EXPORT_SENSOR, description={"suggested_value": current.get(CONF_PV_EXPORT_SENSOR)}): _entity_selector(),
            vol.Optional(CONF_BATTERY_STAT_SENSOR, description={"suggested_value": current.get(CONF_BATTERY_STAT_SENSOR)}): _entity_selector(),
        })
        return self.async_show_form(step_id="site", data_schema=schema)

    async def async_step_database(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_owm()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_DB_ENABLED, default=current.get(CONF_DB_ENABLED, False)): BooleanSelector(),
            vol.Optional(CONF_DB_HOST, default=current.get(CONF_DB_HOST, DEFAULT_DB_HOST)): TextSelector(),
            vol.Optional(CONF_DB_PORT, default=current.get(CONF_DB_PORT, DEFAULT_DB_PORT)): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, step=1)
            ),
            vol.Optional(CONF_DB_USER, default=current.get(CONF_DB_USER, "")): TextSelector(),
            vol.Optional(CONF_DB_PASSWORD, default=current.get(CONF_DB_PASSWORD, "")): TextSelector(
                TextSelectorConfig(type="password")
            ),
            vol.Optional(CONF_DB_NAME, default=current.get(CONF_DB_NAME, DEFAULT_DB_NAME)): TextSelector(),
            vol.Required(CONF_DB_READONLY, default=current.get(CONF_DB_READONLY, False)): BooleanSelector(),
        })
        return self.async_show_form(step_id="database", data_schema=schema)

    async def async_step_owm(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_battery()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_OWM_ENABLED, default=current.get(CONF_OWM_ENABLED, False)): BooleanSelector(),
            vol.Optional(CONF_OWM_API_KEY, default=current.get(CONF_OWM_API_KEY, "")): TextSelector(
                TextSelectorConfig(type="password")
            ),
        })
        return self.async_show_form(step_id="owm", data_schema=schema)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_tuning()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_BATTERY_ENABLED, default=current.get(CONF_BATTERY_ENABLED, False)): BooleanSelector(),
            vol.Optional(CONF_BATTERY_MODE, default=current.get(CONF_BATTERY_MODE, "net")): SelectSelector(
                SelectSelectorConfig(options=["net", "separate"], mode="dropdown")
            ),
            vol.Optional(CONF_BATTERY_NET_SENSOR, description={"suggested_value": current.get(CONF_BATTERY_NET_SENSOR)}): _entity_selector(),
            vol.Optional(CONF_BATTERY_CHARGE_SENSOR, description={"suggested_value": current.get(CONF_BATTERY_CHARGE_SENSOR)}): _entity_selector(),
        })
        return self.async_show_form(step_id="battery", data_schema=schema)

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return self.async_create_entry(data=self._opts)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_AUTO_TUNING, default=current.get(CONF_AUTO_TUNING, DEFAULT_AUTO_TUNING)): BooleanSelector(),
            vol.Required(CONF_AUTO_DAMPENING, default=current.get(CONF_AUTO_DAMPENING, DEFAULT_AUTO_DAMPENING)): BooleanSelector(),
            vol.Required(CONF_CLOUD_THRESHOLD, default=current.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD)): NumberSelector(
                NumberSelectorConfig(min=10, max=50, step=1)
            ),
            vol.Required(CONF_CLOUD_MAX_INCLUDE, default=current.get(CONF_CLOUD_MAX_INCLUDE, DEFAULT_CLOUD_MAX_INCLUDE)): NumberSelector(
                NumberSelectorConfig(min=20, max=100, step=1)
            ),
            vol.Required(CONF_CLIPPING_THRESHOLD, default=current.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD)): NumberSelector(
                NumberSelectorConfig(min=0.5, max=1.0, step=0.01)
            ),
        })
        return self.async_show_form(step_id="tuning", data_schema=schema)
