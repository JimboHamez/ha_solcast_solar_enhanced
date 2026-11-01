"""Night slots with nothing to record are not written to the store.

Every estimator query requires ``pv_actual > 0``, so an empty night row is never
read; night slots are over half of all rows. The tests pin that only the inserts are
skipped (and the undampened-forecast lookups that feed them), that the decision is
all-or-nothing across the '_total' and per-site rows, and that anything non-zero, or
any slot with the sun up, is still written.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_CAPACITY_KW,
    CONF_DB_ENABLED,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_SITE_GROUPS,
    CONF_TILT,
    DEFAULT_SITE_ID,
    DOMAIN,
    NIGHT_ZENITH_DEG,
)
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator, is_empty_night_slot

NIGHT = 105.0
DAY = 60.0
GROUPS = [{"ac_sensor": "sensor.ac_a", "site": "A"}, {"ac_sensor": "sensor.ac_b", "site": "B"}]
_CONFIG = {
    CONF_LATITUDE: -37.9,
    CONF_LONGITUDE: 145.0,
    CONF_CAPACITY_KW: 8.0,
    CONF_TILT: 25.0,
    CONF_AZIMUTH: 0.0,
    CONF_PV_ACTUAL_SENSOR: "sensor.pv_power_30min",
    CONF_PV_EXPORT_SENSOR: "sensor.pv_export_30min",
    CONF_AUTO_TUNING: False,
    CONF_AUTO_DAMPENING: False,
}


# ---------------------------------------------------------------------------
# is_empty_night_slot
# ---------------------------------------------------------------------------


def test_empty_night_slot_is_skipped():
    assert is_empty_night_slot(NIGHT, (0.0, 0.0, 0.0))


@pytest.mark.parametrize("position", range(4))
def test_any_single_non_zero_value_keeps_the_slot(position):
    """Twilight output, overnight battery export or charge, an early forecast: each
    one alone must keep the row. Catches an any()-for-all() inversion."""
    values = [0.0] * 4
    values[position] = 0.05
    assert not is_empty_night_slot(NIGHT, values)


def test_daytime_slot_is_kept_even_when_everything_is_zero():
    """A daytime outage (inverter off, sensor dropout) is a real record, not night."""
    assert not is_empty_night_slot(DAY, (0.0, 0.0, 0.0))


def test_zenith_boundary_is_inclusive():
    assert is_empty_night_slot(NIGHT_ZENITH_DEG, (0.0,))
    assert not is_empty_night_slot(NIGHT_ZENITH_DEG - 0.01, (0.0,))


# ---------------------------------------------------------------------------
# _do_update
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal SqliteStore stand-in capturing inserted rows."""

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


@pytest.fixture
def zero_forecast_base(hass):
    """A base stub whose forecast is zero, as it is at night."""
    stub = type(
        "NightCoord",
        (),
        {
            "data": {
                "forecast_now": 0.0,
                "forecast_today": 0.0,
                "pv_estimate": 0.0,
                "pv_estimate10": 0.0,
                "pv_estimate90": 0.0,
            }
        },
    )()
    hass.data[BASE_DOMAIN] = stub
    return stub


def _coordinator(hass, *, pv="0", export="0", ac_a="0", ac_b="0") -> SolcastEnhancedCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_CONFIG,
        options={CONF_DB_ENABLED: True, CONF_SITE_GROUPS: GROUPS},
        entry_id="night",
        title="night",
    )
    entry.add_to_hass(hass)
    coord = SolcastEnhancedCoordinator(hass, entry)
    coord._db = _FakeStore()
    hass.states.async_set("sensor.pv_power_30min", pv, {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.pv_export_30min", export, {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.ac_a", ac_a, {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.ac_b", ac_b, {"unit_of_measurement": "kW"})
    return coord


async def _run(coord, zenith):
    fetch = AsyncMock(return_value=0.0)
    with (
        patch("custom_components.solcast_solar_enhanced.coordinator.solar_position", return_value=(0.0, zenith)),
        patch.object(coord, "_fetch_undampened_estimate", fetch),
    ):
        await coord._do_update()
    return fetch


async def test_empty_night_writes_nothing_but_still_updates_state(hass, zero_forecast_base):
    coord = _coordinator(hass)
    coord._site_output["A"] = {"pv_actual": 1.0, "pv_estimate": 1.0}  # Yesterday evening's value.

    fetch = await _run(coord, NIGHT)

    assert coord._db.records == []
    fetch.assert_not_awaited()  # No base service call for a row that is not written.
    assert coord._db_latest_period_end is None
    # The per-site PV Power sensor still drops to zero, and the confidence advisory
    # still recomputes: only the write is skipped.
    assert coord._site_output["A"] == {"pv_actual": 0.0, "pv_estimate": 0.0}
    assert coord._site_output["B"] == {"pv_actual": 0.0, "pv_estimate": 0.0}
    assert set(coord._site_confidence) == {"A", "B"}


@pytest.mark.parametrize(
    "readings",
    [
        {"pv": "0.02"},  # Twilight generation on the system meter.
        {"export": "0.4"},  # Overnight battery export (e.g. a VPP event).
        {"ac_b": "0.03"},  # One array still producing.
    ],
)
async def test_any_night_value_writes_every_row(hass, zero_forecast_base, readings):
    """All-or-nothing: '_total' and every site are written together, never a subset."""
    coord = _coordinator(hass, **readings)

    fetch = await _run(coord, NIGHT)

    assert {r["site"] for r in coord._db.records} == {DEFAULT_SITE_ID, "A", "B"}
    assert fetch.await_count == 3
    assert coord._db_latest_period_end == coord._db.records[0]["period_end"]


async def test_night_forecast_alone_keeps_the_slot(hass, zero_forecast_base):
    """A forecast that starts while the sun is still below the horizon is kept."""
    zero_forecast_base.data["pv_estimate90"] = 0.1
    coord = _coordinator(hass)

    await _run(coord, NIGHT)

    assert {r["site"] for r in coord._db.records} == {DEFAULT_SITE_ID, "A", "B"}


async def test_daytime_zero_slot_is_still_written(hass, zero_forecast_base):
    coord = _coordinator(hass)

    await _run(coord, DAY)

    assert {r["site"] for r in coord._db.records} == {DEFAULT_SITE_ID, "A", "B"}


async def test_energy_baseline_advances_through_a_skipped_night(hass, zero_forecast_base):
    """The counter baseline must move on overnight even though nothing is written,
    or the first morning slot would average the whole night into one interval."""
    coord = _coordinator(hass)
    hass.states.async_set("sensor.pv_power_30min", "1234.5", {"unit_of_measurement": "kWh"})

    with patch("custom_components.solcast_solar_enhanced.coordinator.time.time", return_value=1_800_000_000):
        await _run(coord, NIGHT)
    first = coord._energy_baselines["pv_actual"]["epoch"]
    with patch("custom_components.solcast_solar_enhanced.coordinator.time.time", return_value=1_800_001_800):
        await _run(coord, NIGHT)

    assert coord._db.records == []
    assert coord._energy_baselines["pv_actual"]["epoch"] == first + 1800
