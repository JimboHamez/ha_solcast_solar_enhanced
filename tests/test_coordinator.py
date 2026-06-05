"""Test SolcastEnhancedCoordinator helpers."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.const import (
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MODE,
    CONF_BATTERY_NET_SENSOR,
    CONF_BATTERY_STAT_SENSOR,
)


@pytest.fixture
def coordinator(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, mock_config_entry)


# ---------------------------------------------------------------------------
# _safe_read_sensor
# ---------------------------------------------------------------------------

async def test_safe_read_sensor_normal(hass, coordinator):
    hass.states.async_set("sensor.pv_power_30min", "3.5")
    assert coordinator._safe_read_sensor("sensor.pv_power_30min") == pytest.approx(3.5)


async def test_safe_read_sensor_unavailable(hass, coordinator):
    hass.states.async_set("sensor.pv_power_30min", "unavailable")
    assert coordinator._safe_read_sensor("sensor.pv_power_30min") == 0.0


async def test_safe_read_sensor_unknown(hass, coordinator):
    hass.states.async_set("sensor.pv_power_30min", "unknown")
    assert coordinator._safe_read_sensor("sensor.pv_power_30min") == 0.0


async def test_safe_read_sensor_missing(hass, coordinator):
    assert coordinator._safe_read_sensor("sensor.does_not_exist") == 0.0


async def test_safe_read_sensor_negative_clamped(hass, coordinator):
    hass.states.async_set("sensor.pv_power_30min", "-1.0")
    assert coordinator._safe_read_sensor("sensor.pv_power_30min") == 0.0


async def test_safe_read_sensor_empty_entity(hass, coordinator):
    assert coordinator._safe_read_sensor("") == 0.0


# ---------------------------------------------------------------------------
# _read_battery
# ---------------------------------------------------------------------------

async def test_read_battery_prefers_stat_sensor(hass, coordinator):
    """Stat sensor takes priority over raw fallback."""
    hass.states.async_set("sensor.battery_stat", "2.0")
    opts = {CONF_BATTERY_STAT_SENSOR: "sensor.battery_stat"}
    assert coordinator._read_battery(opts) == pytest.approx(2.0)


async def test_read_battery_falls_back_to_net(hass, coordinator):
    """Falls back to net sensor when stat sensor reads zero."""
    hass.states.async_set("sensor.battery_net", "1.5")
    opts = {
        CONF_BATTERY_STAT_SENSOR: "",
        CONF_BATTERY_ENABLED: True,
        CONF_BATTERY_MODE: "net",
        CONF_BATTERY_NET_SENSOR: "sensor.battery_net",
    }
    assert coordinator._read_battery(opts) == pytest.approx(1.5)


async def test_read_battery_negative_net_clamped(hass, coordinator):
    """Negative net sensor (discharging) clamps to 0."""
    hass.states.async_set("sensor.battery_net", "-2.0")
    opts = {
        CONF_BATTERY_STAT_SENSOR: "",
        CONF_BATTERY_ENABLED: True,
        CONF_BATTERY_MODE: "net",
        CONF_BATTERY_NET_SENSOR: "sensor.battery_net",
    }
    assert coordinator._read_battery(opts) == 0.0


async def test_read_battery_disabled(hass, coordinator):
    """Returns 0 when battery fallback is disabled and no stat sensor."""
    opts = {CONF_BATTERY_STAT_SENSOR: "", CONF_BATTERY_ENABLED: False}
    assert coordinator._read_battery(opts) == 0.0


# ---------------------------------------------------------------------------
# _get_base_coordinator / _base_status
# ---------------------------------------------------------------------------

async def test_get_base_coordinator_present(hass, coordinator, mock_base_coordinator):
    assert coordinator._get_base_coordinator() is not None


async def test_get_base_coordinator_absent(hass, coordinator):
    assert coordinator._get_base_coordinator() is None


# ---------------------------------------------------------------------------
# _read_forecast_from_base
# ---------------------------------------------------------------------------

async def test_read_forecast_from_base_coordinator(hass, coordinator, mock_base_coordinator):
    now, today, est, est10, est90 = coordinator._read_forecast_from_base(mock_base_coordinator)
    assert now == pytest.approx(2.5)
    assert today == pytest.approx(18.0)
    assert est == pytest.approx(3.0)


async def test_read_forecast_from_base_none(hass, coordinator):
    """Falls back to sensor states when base coordinator is None.

    forecast_today reads the kWh daily-total sensor; forecast_now is derived from
    the current half-hour detailedForecast slot's pv_estimate (avg kW), not the
    kWh forecast_remaining_today count-down (which was the old wrong-unit bug).
    """
    now_epoch = int(time.time())
    slot_start = now_epoch - (now_epoch % 1800)
    slot_iso = datetime.fromtimestamp(slot_start, tz=timezone.utc).isoformat()
    hass.states.async_set(
        "sensor.solcast_pv_forecast_forecast_today",
        "18.0",
        {"detailedForecast": [{"period_start": slot_iso, "pv_estimate": 2.5}]},
    )
    now, today, est, est10, est90 = coordinator._read_forecast_from_base(None)
    assert now == pytest.approx(2.5)  # kW from the current slot, not kWh remaining
    assert today == pytest.approx(18.0)
    assert est == 0.0


# ---------------------------------------------------------------------------
# _snap_to_half_hour
# ---------------------------------------------------------------------------

# 2024-09-10 — :00 and :30 boundaries in UTC epoch seconds.
_T_1400 = 1725976800  # 2024-09-10T14:00:00+00:00
_T_1430 = _T_1400 + 1800
_T_1500 = _T_1400 + 3600


def test_snap_exact_boundary_unchanged():
    assert SolcastEnhancedCoordinator._snap_to_half_hour(_T_1400) == _T_1400
    assert SolcastEnhancedCoordinator._snap_to_half_hour(_T_1430) == _T_1430


def test_snap_rounds_down_within_first_half():
    # 14:07 → 14:00
    assert SolcastEnhancedCoordinator._snap_to_half_hour(_T_1400 + 7 * 60) == _T_1400


def test_snap_rounds_up_past_quarter():
    # 14:20 → 14:30, 14:50 → 15:00
    assert SolcastEnhancedCoordinator._snap_to_half_hour(_T_1400 + 20 * 60) == _T_1430
    assert SolcastEnhancedCoordinator._snap_to_half_hour(_T_1400 + 50 * 60) == _T_1500


def test_snap_two_polls_in_same_slot_collapse():
    # A scheduled poll and a post-restart poll within the same half-hour snap to
    # the same boundary, so the (period_end_epoch, site) key dedups them.
    assert (
        SolcastEnhancedCoordinator._snap_to_half_hour(_T_1400 + 3 * 60)
        == SolcastEnhancedCoordinator._snap_to_half_hour(_T_1400 + 11 * 60)
    )


# ---------------------------------------------------------------------------
# _compute_dampening_slots — local-time slot grid
# ---------------------------------------------------------------------------

async def test_dampening_slots_built_on_local_time_grid(hass, coordinator):
    """Slot index must map to LOCAL half-hour, not UTC.

    The base integration applies damp_factor[i] to the i-th local half-hour, so
    for a UTC+10/+11 site slot 0 must be local midnight (night) and slot 24 local
    noon (daytime). A UTC grid would invert this.
    """
    await hass.config.async_set_time_zone("Australia/Melbourne")
    coordinator._db = None  # no DB → day slots resolve to source "no_data"

    now_epoch = 1718452800  # 2024-06-15T12:00:00Z = Melbourne 22:00 (winter)
    slots = await coordinator._compute_dampening_slots(
        {}, now_epoch, -37.81, 144.96, "_total"
    )

    assert len(slots) == 48
    assert slots[0]["source"] == "night"       # local 00:00
    assert slots[24]["source"] != "night"      # local 12:00 — sun is up


async def test_dampening_slots_fetch_records_once(hass, coordinator):
    """The day-of-year window is identical for all 48 slots, so the DB scan must
    run once per call — not once per (daytime) slot."""
    from unittest.mock import AsyncMock, MagicMock

    await hass.config.async_set_time_zone("Australia/Melbourne")
    db = MagicMock()
    db.async_get_records_for_dampening = AsyncMock(return_value=[])
    coordinator._db = db

    now_epoch = 1718452800  # 2024-06-15T12:00:00Z
    await coordinator._compute_dampening_slots({}, now_epoch, -37.81, 144.96, "_total")

    assert db.async_get_records_for_dampening.call_count == 1
