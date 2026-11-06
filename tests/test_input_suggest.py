"""Input suggestions for a first-time setup (Energy dashboard + known integrations).

The tariff-split fixture is shaped on a real install that splits solar and grid into
peak/off-peak utility meters (with made-up entity ids): it is the case where taking the
Energy dashboard's counters at face value would silently under-count.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.solcast_solar_enhanced import const
from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_GENERATION_POWER_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_MPPT1_CURRENT_SENSOR,
    CONF_MPPT1_VOLTAGE_SENSOR,
    CONF_MPPT2_CURRENT_SENSOR,
    CONF_MPPT2_VOLTAGE_SENSOR,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    DOMAIN,
)
from custom_components.solcast_solar_enhanced.input_suggest import (
    _resolve_counter,
    async_read_energy_prefs,
    async_suggest_inputs,
    suggest_from_energy,
    suggest_fronius_trackers,
)
from custom_components.solcast_solar_enhanced.models import InputSuggestions

SUGGEST = "custom_components.solcast_solar_enhanced.config_flow.async_suggest_inputs"


@pytest.fixture(autouse=True)
def _mock_base_integration(hass):
    mock_integration(hass, MockModule(BASE_DOMAIN), built_in=False)


def _identity(stat_id: str) -> str | None:
    return None if ":" in stat_id else stat_id


TYPICAL = {
    "energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.pv_lifetime_kwh", "stat_rate": "sensor.pv_power"},
        {
            "type": "grid",
            "stat_energy_from": "sensor.grid_import_kwh",
            "stat_energy_to": "sensor.grid_export_kwh",
            "stat_rate": "sensor.grid_power",
        },
    ]
}

# Peak/off-peak solar, three import tariffs, tiered feed-in; only one source has power.
TARIFF_SPLIT = {
    "energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.pv_offpeak", "stat_rate": "sensor.pv_power"},
        {"type": "solar", "stat_energy_from": "sensor.pv_peak"},
        {"type": "grid", "stat_energy_from": "sensor.imp_peak", "stat_energy_to": "sensor.exp_first10"},
        {
            "type": "grid",
            "stat_energy_from": "sensor.imp_offpeak",
            "stat_energy_to": "sensor.exp_after10",
            "power_config": {"stat_rate": "sensor.grid_power"},
            "stat_rate": "sensor.grid_power",
        },
        {"type": "grid", "stat_energy_from": "sensor.imp_ev_night", "stat_energy_to": None},
        {"type": "water", "stat_energy_from": "sew_water:usage"},
    ]
}
METERS = {
    "sensor.pv_offpeak": "sensor.pv_lifetime_kwh",
    "sensor.pv_peak": "sensor.pv_lifetime_kwh",
    "sensor.exp_first10": "sensor.grid_export_kwh",
    "sensor.exp_after10": "sensor.grid_export_kwh",
}


def test_suggestion_keys_are_the_config_keys() -> None:
    """Every ``InputSuggestions`` key is a ``CONF_*`` value, so a suggestion lands on its field."""
    conf_values = {v for k, v in vars(const).items() if k.startswith("CONF_") and isinstance(v, str)}
    assert set(InputSuggestions.__annotations__) <= conf_values


def test_typical_energy_dashboard_suggests_all_four() -> None:
    got = suggest_from_energy(TYPICAL, _identity, _identity)
    assert got == {
        CONF_PV_ACTUAL_SENSOR: "sensor.pv_lifetime_kwh",
        CONF_PV_EXPORT_SENSOR: "sensor.grid_export_kwh",
        CONF_GRID_POWER_SENSOR: "sensor.grid_power",
        CONF_GENERATION_POWER_SENSOR: "sensor.pv_power",
    }


def test_tariff_meters_trace_back_to_one_source() -> None:
    """Split meters that share a source are one candidate: the source, never a meter."""
    got = suggest_from_energy(TARIFF_SPLIT, lambda s: METERS.get(s, s), _identity)
    assert got[CONF_PV_ACTUAL_SENSOR] == "sensor.pv_lifetime_kwh"
    assert got[CONF_PV_EXPORT_SENSOR] == "sensor.grid_export_kwh"
    assert got[CONF_GRID_POWER_SENSOR] == "sensor.grid_power"
    assert got[CONF_GENERATION_POWER_SENSOR] == "sensor.pv_power"


def test_untraceable_meter_refuses_the_counter() -> None:
    """One meter that cannot be traced (a YAML meter) blocks the suggestion outright."""
    resolve = lambda s: None if s == "sensor.pv_peak" else METERS.get(s, s)  # noqa: E731
    got = suggest_from_energy(TARIFF_SPLIT, resolve, _identity)
    assert CONF_PV_ACTUAL_SENSOR not in got
    assert got[CONF_PV_EXPORT_SENSOR] == "sensor.grid_export_kwh"


def test_two_arrays_are_not_one_property_counter() -> None:
    """Two solar sources with different counters are two arrays: no property-wide guess."""
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.east_kwh", "stat_rate": "sensor.east_w"},
            {"type": "solar", "stat_energy_from": "sensor.west_kwh", "stat_rate": "sensor.west_w"},
        ]
    }
    assert suggest_from_energy(prefs, _identity, _identity) == {}


def test_external_statistics_are_ignored() -> None:
    prefs = {"energy_sources": [{"type": "solar", "stat_energy_from": "envoy:lifetime", "stat_rate": "envoy:power"}]}
    assert suggest_from_energy(prefs, _identity, _identity) == {}


def test_legacy_grid_lists_are_read() -> None:
    """The pre-2026 grid format kept export and power in lists."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.grid_import_kwh"}],
                "flow_to": [{"stat_energy_to": "sensor.grid_export_kwh"}],
                "power": [{"stat_rate": "sensor.grid_power"}],
            }
        ]
    }
    got = suggest_from_energy(prefs, _identity, _identity)
    assert got == {CONF_PV_EXPORT_SENSOR: "sensor.grid_export_kwh", CONF_GRID_POWER_SENSOR: "sensor.grid_power"}


@pytest.mark.parametrize(
    "prefs",
    [{}, {"energy_sources": None}, {"energy_sources": "x"}, {"energy_sources": [None, 3, {"type": "solar"}]}],
)
def test_malformed_preferences_suggest_nothing(prefs: dict[str, Any]) -> None:
    assert suggest_from_energy(prefs, _identity, _identity) == {}


# --- utility meter tracing against the real registries --------------------------


def _meter(hass, registry, entity_id: str, source: str | None) -> None:
    """A UI utility meter (config entry with ``source``), or a YAML one when ``source`` is None."""
    entry = None
    if source is not None:
        entry = MockConfigEntry(domain="utility_meter", options={"source": source})
        entry.add_to_hass(hass)
    registry.async_get_or_create(
        "sensor", "utility_meter", entity_id, suggested_object_id=entity_id.split(".", 1)[1], config_entry=entry
    )
    hass.states.async_set(entity_id, "1.0")


async def test_resolve_counter_follows_a_ui_meter(hass) -> None:
    registry = er.async_get(hass)
    hass.states.async_set("sensor.pv_lifetime_kwh", "1234.5")
    _meter(hass, registry, "sensor.pv_offpeak", "sensor.pv_lifetime_kwh")
    assert _resolve_counter(hass, registry, "sensor.pv_offpeak") == "sensor.pv_lifetime_kwh"
    assert _resolve_counter(hass, registry, "sensor.pv_lifetime_kwh") == "sensor.pv_lifetime_kwh"


async def test_resolve_counter_accepts_a_registry_uuid_source(hass) -> None:
    """The meter's options may name its source by registry id rather than entity id."""
    registry = er.async_get(hass)
    src = registry.async_get_or_create("sensor", "fronius", "inv-energy", suggested_object_id="pv_lifetime_kwh")
    hass.states.async_set(src.entity_id, "1.0")
    _meter(hass, registry, "sensor.pv_offpeak", src.id)
    assert _resolve_counter(hass, registry, "sensor.pv_offpeak") == src.entity_id


async def test_resolve_counter_refuses_yaml_meters_and_missing_entities(hass) -> None:
    registry = er.async_get(hass)
    _meter(hass, registry, "sensor.pv_yaml", None)
    assert _resolve_counter(hass, registry, "sensor.pv_yaml") is None
    assert _resolve_counter(hass, registry, "sensor.does_not_exist") is None
    assert _resolve_counter(hass, registry, "external:stat") is None


async def test_resolve_counter_follows_a_meter_of_a_meter(hass) -> None:
    registry = er.async_get(hass)
    hass.states.async_set("sensor.pv_lifetime_kwh", "1.0")
    _meter(hass, registry, "sensor.pv_daily", "sensor.pv_lifetime_kwh")
    _meter(hass, registry, "sensor.pv_daily_peak", "sensor.pv_daily")
    assert _resolve_counter(hass, registry, "sensor.pv_daily_peak") == "sensor.pv_lifetime_kwh"


# --- Fronius DC trackers ------------------------------------------------------------


def _fronius(registry, uid: str, object_id: str) -> str:
    return registry.async_get_or_create("sensor", "fronius", uid, suggested_object_id=object_id).entity_id


async def test_fronius_trackers_map_by_unique_id(hass) -> None:
    """MPPT 1 is ``voltage_dc``/``current_dc`` and MPPT 2 the ``_2`` pair; Modbus twins are ignored."""
    registry = er.async_get(hass)
    v1 = _fronius(registry, "INV1-voltage_dc", "dc_v_a")
    i1 = _fronius(registry, "INV1-current_dc", "dc_i_a")
    v2 = _fronius(registry, "INV1-voltage_dc_2", "dc_v_b")
    i2 = _fronius(registry, "INV1-current_dc_2", "dc_i_b")
    _fronius(registry, "INV1-modbus-voltage_dc", "modbus_v")
    _fronius(registry, "INV1-power_ac", "ac_power")
    assert suggest_fronius_trackers(registry.entities.values()) == {
        CONF_MPPT1_VOLTAGE_SENSOR: v1,
        CONF_MPPT1_CURRENT_SENSOR: i1,
        CONF_MPPT2_VOLTAGE_SENSOR: v2,
        CONF_MPPT2_CURRENT_SENSOR: i2,
    }


async def test_fronius_tracker_needs_both_sensors(hass) -> None:
    registry = er.async_get(hass)
    _fronius(registry, "INV1-voltage_dc", "dc_v_a")
    _fronius(registry, "INV1-current_dc", "dc_i_a")
    _fronius(registry, "INV1-voltage_dc_2", "dc_v_b")
    got = suggest_fronius_trackers(registry.entities.values())
    assert set(got) == {CONF_MPPT1_VOLTAGE_SENSOR, CONF_MPPT1_CURRENT_SENSOR}


async def test_two_fronius_inverters_suggest_no_trackers(hass) -> None:
    registry = er.async_get(hass)
    for inv in ("INV1", "INV2"):
        _fronius(registry, f"{inv}-voltage_dc", f"{inv}_v")
        _fronius(registry, f"{inv}-current_dc", f"{inv}_i")
    assert suggest_fronius_trackers(registry.entities.values()) == {}


async def test_disabled_fronius_entities_are_ignored(hass) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "fronius", "INV1-voltage_dc", suggested_object_id="v", disabled_by=er.RegistryEntryDisabler.USER
    )
    _fronius(registry, "INV1-current_dc", "i")
    assert suggest_fronius_trackers(registry.entities.values()) == {}


async def test_other_platforms_with_fronius_like_ids_are_ignored(hass) -> None:
    """An MQTT or Node-RED bridge may copy Fronius's unique-id shape; only the fronius platform counts."""
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "mqtt", "INV9-voltage_dc", suggested_object_id="mq_v")
    registry.async_get_or_create("sensor", "mqtt", "INV9-current_dc", suggested_object_id="mq_i")
    assert suggest_fronius_trackers(registry.entities.values()) == {}


# --- energy manager access -----------------------------------------------------------


class _Manager:
    def __init__(self, data: Any) -> None:
        self.data = data


async def test_energy_prefs_need_the_energy_integration(hass) -> None:
    """Without the Energy integration loaded, its manager is never touched (it would create one)."""
    assert "energy" not in hass.config.components
    with patch("homeassistant.components.energy.data.async_get_manager", return_value=_Manager(TYPICAL)) as get:
        assert await async_read_energy_prefs(hass) is None
    get.assert_not_called()


async def test_energy_prefs_survive_a_broken_manager(hass) -> None:
    hass.config.components.add("energy")
    with patch("homeassistant.components.energy.data.async_get_manager", side_effect=RuntimeError("changed")):
        assert await async_read_energy_prefs(hass) is None
    with patch("homeassistant.components.energy.data.async_get_manager", return_value=_Manager(["not", "a", "dict"])):
        assert await async_read_energy_prefs(hass) is None
    with patch("homeassistant.components.energy.data.async_get_manager", return_value=_Manager(TYPICAL)):
        assert await async_read_energy_prefs(hass) == TYPICAL


async def test_async_suggest_inputs_end_to_end(hass) -> None:
    """Real registry + patched Energy manager: tariff meters traced, Fronius trackers found."""
    registry = er.async_get(hass)
    for entity_id in ("sensor.pv_lifetime_kwh", "sensor.grid_export_kwh", "sensor.grid_power", "sensor.pv_power"):
        hass.states.async_set(entity_id, "1.0")
    for meter, source in METERS.items():
        _meter(hass, registry, meter, source)
    v1 = _fronius(registry, "INV1-voltage_dc", "dc_v_a")
    i1 = _fronius(registry, "INV1-current_dc", "dc_i_a")
    hass.config.components.add("energy")
    with patch("homeassistant.components.energy.data.async_get_manager", return_value=_Manager(TARIFF_SPLIT)):
        got = await async_suggest_inputs(hass)
    assert got == {
        CONF_PV_ACTUAL_SENSOR: "sensor.pv_lifetime_kwh",
        CONF_PV_EXPORT_SENSOR: "sensor.grid_export_kwh",
        CONF_GRID_POWER_SENSOR: "sensor.grid_power",
        CONF_GENERATION_POWER_SENSOR: "sensor.pv_power",
        CONF_MPPT1_VOLTAGE_SENSOR: v1,
        CONF_MPPT1_CURRENT_SENSOR: i1,
    }


async def test_async_suggest_inputs_never_raises(hass) -> None:
    with patch(
        "custom_components.solcast_solar_enhanced.input_suggest.suggest_fronius_trackers", side_effect=KeyError("x")
    ):
        assert await async_suggest_inputs(hass) == {}


# --- config flow wiring ----------------------------------------------------------------


def _suggested(result, key: str) -> Any:
    for marker in result["data_schema"].schema:
        if str(marker.schema if hasattr(marker, "schema") else marker) == key:
            return (getattr(marker, "description", None) or {}).get("suggested_value")
    raise KeyError(key)


SITE_INPUT = {
    "latitude": -37.9,
    "longitude": 145.0,
    "capacity_kw": 5.0,
    "tilt": 20.0,
    "azimuth": 0.0,
}


async def test_first_setup_prefills_from_suggestions(hass) -> None:
    with patch(SUGGEST, return_value={CONF_PV_ACTUAL_SENSOR: "sensor.pv_lifetime_kwh", CONF_MPPT1_VOLTAGE_SENSOR: "sensor.v1"}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "site"
    assert _suggested(result, CONF_PV_ACTUAL_SENSOR) == "sensor.pv_lifetime_kwh"
    assert _suggested(result, CONF_MPPT1_VOLTAGE_SENSOR) == "sensor.v1"
    assert _suggested(result, CONF_PV_EXPORT_SENSOR) is None


async def test_a_cleared_suggestion_is_not_saved(hass) -> None:
    """Submitting Step 1 without the suggested field must not keep the suggestion."""
    with patch(SUGGEST, return_value={CONF_PV_EXPORT_SENSOR: "sensor.grid_export_kwh"}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SITE_INPUT)
    assert result["step_id"] == "database"
    flow = hass.config_entries.flow._progress[result["flow_id"]]  # noqa: SLF001
    assert flow._data.get(CONF_PV_EXPORT_SENSOR) is None  # noqa: SLF001


async def test_a_submitted_value_beats_the_suggestion(hass) -> None:
    with patch(SUGGEST, return_value={CONF_PV_EXPORT_SENSOR: "sensor.grid_export_kwh"}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SITE_INPUT, CONF_PV_EXPORT_SENSOR: "sensor.mine"}
    )
    flow = hass.config_entries.flow._progress[result["flow_id"]]  # noqa: SLF001
    assert flow._data[CONF_PV_EXPORT_SENSOR] == "sensor.mine"  # noqa: SLF001


async def test_reconfigure_does_not_suggest(hass) -> None:
    """An empty field on an existing entry may be deliberate: reconfigure never pre-fills it."""
    entry = MockConfigEntry(domain=DOMAIN, data={**SITE_INPUT, "db_enabled": False}, options={})
    entry.add_to_hass(hass)
    with patch(SUGGEST, return_value={CONF_PV_EXPORT_SENSOR: "sensor.grid_export_kwh"}) as suggest:
        result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert _suggested(result, CONF_PV_EXPORT_SENSOR) is None
    suggest.assert_not_called()


async def test_options_flow_does_not_suggest(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={**SITE_INPUT, "db_enabled": False}, options={})
    entry.add_to_hass(hass)
    with patch(SUGGEST, return_value={CONF_PV_EXPORT_SENSOR: "sensor.grid_export_kwh"}) as suggest:
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _suggested(result, CONF_PV_EXPORT_SENSOR) is None
    suggest.assert_not_called()


async def test_multi_site_trackers_are_seeded_on_the_first_array(hass) -> None:
    """With two arrays the flat MPPT fields are not on Step 1; the suggestion moves to the sites step."""
    hass.states.async_set("sensor.solcast_pv_forecast_a", "1.0", {"resource_id": "AAAA", "name": "Array A"})
    hass.states.async_set("sensor.solcast_pv_forecast_b", "1.0", {"resource_id": "BBBB", "name": "Array B"})
    suggestions = {CONF_MPPT1_VOLTAGE_SENSOR: "sensor.v1", CONF_MPPT1_CURRENT_SENSOR: "sensor.i1"}
    with patch(SUGGEST, return_value=suggestions):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    for step in (
        {**SITE_INPUT, CONF_PV_ACTUAL_SENSOR: "sensor.pv"},
        {"db_enabled": False},
        {"openmeteo_enabled": True, "owm_enabled": False, "owm_api_key": ""},
        {"battery_enabled": False, "battery_mode": "net"},
        {"auto_tuning": False, "auto_dampening": False, "cloud_threshold": 20.0, "cloud_max_include": 60.0, "clipping_threshold": 0.95},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], step)
    assert result["step_id"] == "sites"
    assert _suggested(result, "Array A — MPPT 1 voltage (optional)") == "sensor.v1"
    assert _suggested(result, "Array A — MPPT 1 current (optional)") == "sensor.i1"
    assert _suggested(result, "Array B — MPPT 1 voltage (optional)") is None
