"""The slot gate and voltage model, wired into the update cycle; and the compact_database action."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced import async_setup
from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_CAPACITY_KW,
    CONF_DB_ENABLED,
    CONF_EXPORT_LIMIT_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_SITE_GROUPS,
    CONF_TILT,
    DEFAULT_SITE_ID,
    DOMAIN,
    VMODEL_FORGET,
    GateFlag,
)
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator

TRACKERS = {
    "A": {"voltage_sensor": "sensor.v1", "current_sensor": "sensor.i1"},
    "B": {"voltage_sensor": "sensor.v2", "current_sensor": "sensor.i2"},
}
CONFIG = {
    CONF_LATITUDE: -37.9,
    CONF_LONGITUDE: 145.0,
    CONF_CAPACITY_KW: 8.0,
    CONF_TILT: 25.0,
    CONF_AZIMUTH: 0.0,
    CONF_PV_ACTUAL_SENSOR: "sensor.pv_power_30min",
    CONF_PV_EXPORT_SENSOR: "sensor.export_power",
    CONF_AUTO_TUNING: False,
    CONF_AUTO_DAMPENING: False,
}


class _FakeStore:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def async_insert_record(self, rec):
        self.records.append(rec)

    async def async_get_record_count(self):
        return len(self.records)

    async def async_get_sites(self):
        return sorted({r["site"] for r in self.records})

    async def async_prune(self, days):
        return 0

    async def async_get_records_for_shading(self, site=None, **kwargs):
        return []


def _coordinator(hass, groups=None, **options) -> SolcastEnhancedCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONFIG,
        options={CONF_DB_ENABLED: True, CONF_EXPORT_LIMIT_KW: 5.0, CONF_SITE_GROUPS: groups or [], **options},
        entry_id="gate",
        title="gate",
    )
    entry.add_to_hass(hass)
    coord = SolcastEnhancedCoordinator(hass, entry)
    coord._db = _FakeStore()
    hass.states.async_set("sensor.pv_power_30min", "6.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.export_power", "1.0", {"unit_of_measurement": "kW"})
    return coord


def _dc_groups():
    return [
        {"ac_sensor": "sensor.ac_a", "site": "A", "mppts": [TRACKERS["A"]]},
        {"ac_sensor": "sensor.ac_b", "site": "B", "mppts": [TRACKERS["B"]]},
    ]


def _samples_with_dips(start, end, *, dip_a, dip_b, export_kw=1.0):
    """1 Hz V/I for two trackers; current drops inside each array's dip windows."""

    def series(dips, value, low):
        return [(float(t), low if any(a <= t - start < b for a, b in dips) else value) for t in range(int(start), int(end))]

    return {
        "sensor.v1": series([], 370.0, 370.0),
        "sensor.i1": series(dip_a, 9.0, 3.0),
        "sensor.v2": series([], 368.0, 368.0),
        "sensor.i2": series(dip_b, 9.0, 3.0),
        "sensor.export_power": [(float(start), export_kw)],
    }


@pytest.fixture
def base_with_forecast(hass):
    hass.data[BASE_DOMAIN] = type(
        "Base",
        (),
        {"data": {"forecast_now": 5.0, "forecast_today": 30.0, "pv_estimate": 6.0, "pv_estimate10": 5.0, "pv_estimate90": 7.0}},
    )()


async def _update(coord, samples_fn, zenith=40.0):
    async def samples(entity_ids, start, end):
        data = samples_fn(start, end)
        return {k: v for k, v in data.items() if k in entity_ids}

    with (
        patch.object(coord, "_interval_samples", side_effect=samples),
        patch("custom_components.solcast_solar_enhanced.coordinator.solar_position", return_value=(0.0, zenith)),
        patch.object(coord, "_fetch_undampened_estimate", AsyncMock(return_value=0.0)),
    ):
        await coord._do_update()


# ---------------------------------------------------------------------------
# Measures reach the rows
# ---------------------------------------------------------------------------


async def test_gate_measures_land_on_the_total_and_site_rows(hass, base_with_forecast):
    coord = _coordinator(hass, _dc_groups())
    await _update(coord, lambda s, e: _samples_with_dips(s, e, dip_a=[(100, 400), (1000, 1200)], dip_b=[(100, 400)]))

    rows = {r["site"]: r for r in coord._db.records}
    total = rows[DEFAULT_SITE_ID]
    assert total["sky_dip_s"] == pytest.approx(300.0, abs=2)
    assert total["export_pinned_s"] == 0.0
    flags = GateFlag(int(total["gate_flags"]))
    assert GateFlag.DC_TRACKERS in flags and GateFlag.CROSS_REF in flags and GateFlag.EXPORT_SERIES in flags
    assert rows["A"]["shade_dip_s"] == pytest.approx(200.0, abs=2)
    assert rows["B"]["shade_dip_s"] == 0.0
    assert "sky_dip_s" not in rows["A"]  # Property-wide: carried once, on the '_total' row.
    assert coord.diagnostics_snapshot()["slot_gate"]["latest"]["sky_dip_s"] == total["sky_dip_s"]


async def test_pinned_export_is_measured_against_the_limit(hass, base_with_forecast):
    coord = _coordinator(hass, _dc_groups())
    await _update(coord, lambda s, e: _samples_with_dips(s, e, dip_a=[], dip_b=[], export_kw=5.0))
    total = next(r for r in coord._db.records if r["site"] == DEFAULT_SITE_ID)
    assert total["export_pinned_s"] == pytest.approx(1800.0, abs=2)


async def test_no_gate_work_at_night(hass):
    hass.data[BASE_DOMAIN] = type("B", (), {"data": dict.fromkeys(("forecast_now", "forecast_today", "pv_estimate", "pv_estimate10", "pv_estimate90"), 0.0)})()
    coord = _coordinator(hass, _dc_groups())
    hass.states.async_set("sensor.pv_power_30min", "0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.export_power", "0", {"unit_of_measurement": "kW"})
    with patch.object(coord, "_async_slot_gate", AsyncMock(return_value=({}, {}))) as gate:
        await _update(coord, lambda s, e: {}, zenith=110.0)
    gate.assert_not_awaited()
    assert coord._db.records == []


async def test_a_gate_failure_never_costs_the_row(hass, base_with_forecast):
    coord = _coordinator(hass, _dc_groups())
    with patch("custom_components.solcast_solar_enhanced.coordinator.measure_slot", side_effect=RuntimeError("boom")):
        await _update(coord, lambda s, e: _samples_with_dips(s, e, dip_a=[], dip_b=[]))
    rows = {r["site"]: r for r in coord._db.records}
    assert set(rows) == {DEFAULT_SITE_ID, "A", "B"}
    assert "sky_dip_s" not in rows[DEFAULT_SITE_ID]  # The store writes its unknown default.
    assert rows["A"]["shade_dip_s"] == -1.0


async def test_the_model_trains_and_is_saved(hass, base_with_forecast):
    coord = _coordinator(hass, _dc_groups())
    coord._weather = {"temp": 22.0}
    coord._irradiance = {"ghi": 850.0, "dni": 800.0, "dhi": 90.0}
    with patch.object(coord._vmodel_store, "async_delay_save") as save:
        await _update(coord, lambda s, e: _samples_with_dips(s, e, dip_a=[], dip_b=[]))
    assert coord._vmodel.fit("sensor.v1").n > 0 and coord._vmodel.fit("sensor.v2").n > 0
    save.assert_called_once()
    assert save.call_args.args[0]()["model"]["version"] == 1


def test_the_model_decays_once_a_day():
    coord = SolcastEnhancedCoordinator.__new__(SolcastEnhancedCoordinator)
    from custom_components.solcast_solar_enhanced.vmodel import VoltageModel

    coord._vmodel = VoltageModel()
    coord._vmodel.fit("t").n = 100.0
    coord._vmodel_last_decay = 0
    coord._maybe_decay_vmodel(1_000_000)  # First run only starts the clock.
    assert coord._vmodel.fit("t").n == 100.0
    coord._maybe_decay_vmodel(1_000_000 + 3600)
    assert coord._vmodel.fit("t").n == 100.0
    coord._maybe_decay_vmodel(1_000_000 + 86400)
    assert coord._vmodel.fit("t").n == pytest.approx(100.0 * VMODEL_FORGET)


# ---------------------------------------------------------------------------
# Inputs by topology
# ---------------------------------------------------------------------------


def test_a_direct_group_without_dc_uses_its_own_generation_sensor(hass):
    coord = _coordinator(hass, [{"ac_sensor": "sensor.ac_a", "site": "A"}, {"ac_sensor": "sensor.ac_b", "site": "B"}])
    hass.states.async_set("sensor.ac_a", "2.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.ac_b", "100.0", {"unit_of_measurement": "kWh"})
    samples = {
        "sensor.ac_a": [(0.0, 2.0), (60.0, 2.1)],
        "sensor.ac_b": [(0.0, 100.0), (600.0, 100.5), (1200.0, 101.0)],
    }
    arrays, kinds = coord._gate_arrays(coord._opts, samples)
    assert kinds == {"A": "ac", "B": "counter"}
    assert arrays["A"] == [(0.0, 2.0), (60.0, 2.1)]
    assert arrays["B"][0][1] == pytest.approx(3.0)  # 0.5 kWh over 600 s.


def test_a_dc_split_group_sums_each_arrays_dc_sensors(hass):
    groups = [{"ac_sensor": "sensor.ac", "strings": [{"site": "A", "dc_sensors": ["sensor.dc_a1", "sensor.dc_a2"]}, {"site": "B", "dc_sensors": ["sensor.dc_b"]}]}]
    coord = _coordinator(hass, groups)
    for e in ("sensor.dc_a1", "sensor.dc_a2", "sensor.dc_b"):
        hass.states.async_set(e, "1000", {"unit_of_measurement": "W"})
    samples = {"sensor.dc_a1": [(0.0, 1000.0)], "sensor.dc_a2": [(0.0, 500.0), (10.0, 600.0)], "sensor.dc_b": [(0.0, 800.0)]}
    arrays, kinds = coord._gate_arrays(coord._opts, samples)
    assert arrays["A"] == [(0.0, pytest.approx(1.5)), (10.0, pytest.approx(1.6))]
    assert kinds == {"A": "dc", "B": "dc"}


def test_no_arrays_falls_back_to_the_system_sensor(hass):
    coord = _coordinator(hass)
    arrays, kinds = coord._gate_arrays(coord._opts, {"sensor.pv_power_30min": [(0.0, 6.0)]})
    assert arrays == {DEFAULT_SITE_ID: [(0.0, 6.0)]} and kinds == {DEFAULT_SITE_ID: "ac"}


# ---------------------------------------------------------------------------
# compact_database
# ---------------------------------------------------------------------------


async def test_compact_action_returns_each_entrys_result(hass):
    await async_setup(hass, {})
    coordinator = MagicMock()
    coordinator.config_entry = SimpleNamespace(entry_id="abc")
    coordinator.async_compact_database = AsyncMock(return_value={"compacted": True, "size_before_mb": 4.8, "size_after_mb": 0.6})
    entry = SimpleNamespace(runtime_data=coordinator)
    with patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]):
        response = await hass.services.async_call(DOMAIN, "compact_database", {}, blocking=True, return_response=True)
    assert response == {"entries": [{"entry_id": "abc", "compacted": True, "size_before_mb": 4.8, "size_after_mb": 0.6}]}


async def test_compact_action_needs_a_loaded_entry(hass):
    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "compact_database", {}, blocking=True, return_response=True)


async def test_compact_is_refused_with_the_stores_reason(hass):
    coord = _coordinator(hass)
    coord._db = SimpleNamespace(async_compact=AsyncMock(return_value={"compacted": False, "reason": "already compacted"}))
    with pytest.raises(ServiceValidationError) as err:
        await coord.async_compact_database()
    assert err.value.translation_key == "compact_refused"
    assert err.value.translation_placeholders == {"reason": "already compacted"}


async def test_compact_with_the_database_off_says_so(hass):
    coord = _coordinator(hass)
    coord._db = None
    with pytest.raises(ServiceValidationError) as err:
        await coord.async_compact_database()
    assert err.value.translation_key == "database_disabled"


def test_the_gate_inputs_join_the_batched_recorder_query(hass):
    """Without these, a direct or dc_split install would silently get no gate input."""
    groups = [
        {"ac_sensor": "sensor.ac_a", "site": "A"},
        {"ac_sensor": "sensor.ac_shared", "strings": [{"site": "B", "dc_sensors": ["sensor.dc_b"]}, {"site": "C", "dc_sensor": "sensor.dc_c"}]},
    ]
    coord = _coordinator(hass, groups)
    assert coord._gate_entities(coord._opts) == {"sensor.ac_a", "sensor.ac_shared", "sensor.dc_b", "sensor.dc_c", "sensor.pv_power_30min"}


async def test_the_query_asks_for_the_gate_inputs(hass, base_with_forecast):
    coord = _coordinator(hass, [{"ac_sensor": "sensor.ac_a", "site": "A"}])
    asked: set[str] = set()

    async def samples(entity_ids, start, end):
        asked.update(entity_ids)
        return {}

    with (
        patch.object(coord, "_interval_samples", side_effect=samples),
        patch("custom_components.solcast_solar_enhanced.coordinator.solar_position", return_value=(0.0, 40.0)),
        patch.object(coord, "_fetch_undampened_estimate", AsyncMock(return_value=0.0)),
    ):
        await coord._do_update()
    assert "sensor.ac_a" in asked
