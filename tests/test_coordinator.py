"""Test SolcastEnhancedCoordinator helpers."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.const import (
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MODE,
    CONF_BATTERY_NET_SENSOR,
    CONF_BATTERY_STAT_SENSOR,
    CONF_AUTO_TUNING,
    CONF_DAMPENING_GATE,
    CONF_OWM_API_KEY,
    CONF_OWM_ENABLED,
    DAMPENING_GATE_MIN_RECORDS,
    DOMAIN,
    ISSUE_DAMPENING_GATED,
    ISSUE_OWM_REQUIRED,
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


# ---------------------------------------------------------------------------
# Phase-2 DC telemetry capture (read helpers)
# ---------------------------------------------------------------------------

async def test_read_numeric_state_variants(hass, coordinator):
    hass.states.async_set("sensor.dc_v", "412.5")
    hass.states.async_set("sensor.dc_bad", "n/a")
    hass.states.async_set("sensor.dc_unavail", "unavailable")
    assert coordinator._read_numeric_state("sensor.dc_v") == pytest.approx(412.5)
    # Unlike _safe_read_sensor, a non-numeric/absent reading is None (no telemetry),
    # not 0 — so the caller can distinguish "no sensor" from a real zero.
    assert coordinator._read_numeric_state("sensor.dc_bad") is None
    assert coordinator._read_numeric_state("sensor.dc_unavail") is None
    assert coordinator._read_numeric_state("sensor.missing") is None
    assert coordinator._read_numeric_state(None) is None
    assert coordinator._read_numeric_state("") is None


async def test_read_mppt_telemetry_pairs_and_padding(hass, coordinator):
    hass.states.async_set("sensor.v1", "412.0")
    hass.states.async_set("sensor.i1", "6.0")
    hass.states.async_set("sensor.v2", "398.0")
    # Two trackers, second has voltage only → current pads to 0.0. hist={} → the
    # aggregate falls back to the instantaneous reading.
    mppts = [
        {"voltage_sensor": "sensor.v1", "current_sensor": "sensor.i1"},
        {"voltage_sensor": "sensor.v2", "current_sensor": "sensor.i2_missing"},
    ]
    assert coordinator._read_mppt_telemetry(mppts, {}) == (412.0, 6.0, 398.0, 0.0)
    # One tracker → second pair zero-filled.
    assert coordinator._read_mppt_telemetry(
        [{"voltage_sensor": "sensor.v1", "current_sensor": "sensor.i1"}], {}
    ) == (412.0, 6.0, 0.0, 0.0)
    # Nothing configured → None (so the site stays absent, not a row of zeros).
    assert coordinator._read_mppt_telemetry([], {}) is None
    assert coordinator._read_mppt_telemetry(None, {}) is None
    assert coordinator._read_mppt_telemetry([{"voltage_sensor": None}], {}) is None


async def test_read_mppt_telemetry_uses_interval_max_v_min_i(hass, coordinator):
    """A mid-slot off-MPP excursion (voltage spike, current dip) is caught by the
    interval max-voltage / min-current even when the boundary sample looks normal."""
    hass.states.async_set("sensor.v1", "415.0")  # instantaneous (slot boundary)
    hass.states.async_set("sensor.i1", "5.5")
    hist = {
        "sensor.v1": [410.0, 450.0, 420.0],  # spiked to 450 mid-slot
        "sensor.i1": [6.0, 0.5, 5.0],        # dipped to 0.5 mid-slot
    }
    mppts = [{"voltage_sensor": "sensor.v1", "current_sensor": "sensor.i1"}]
    # max(410,450,420,415)=450 ; min(6.0,0.5,5.0,5.5)=0.5
    assert coordinator._read_mppt_telemetry(mppts, hist) == (450.0, 0.5, 0.0, 0.0)


async def test_interval_extreme_modes(hass, coordinator):
    hass.states.async_set("sensor.v", "415.0")
    hist = {"sensor.v": [410.0, 450.0]}
    assert coordinator._interval_extreme("sensor.v", "max", hist) == 450.0
    assert coordinator._interval_extreme("sensor.v", "min", hist) == 410.0
    # No history, no state → None; unset entity → None.
    assert coordinator._interval_extreme("sensor.absent", "max", {}) is None
    assert coordinator._interval_extreme(None, "max", hist) is None


async def test_dc_telemetry_summary_shape(coordinator):
    summary = coordinator._dc_telemetry_summary(
        (412.0, 6.0, 398.0, 5.1),
        {"A": (412.0, 6.0, 398.0, 5.1)},
    )
    assert summary["mppt1_voltage"] == 412.0
    assert summary["mppt2_current"] == 5.1
    assert summary["max_voltage"] == 412.0  # max(412, 398)
    assert summary["sites"]["A"]["mppt2_voltage"] == 398.0


async def test_interval_values_empty_without_entities(coordinator):
    # No configured entities → no recorder query attempted.
    assert await coordinator._interval_values(set(), 0, 100) == {}


async def test_collect_dc_entities(coordinator):
    opts = {
        "mppt1_voltage_sensor": "sensor.tv1", "mppt1_current_sensor": "sensor.ti1",
        "site_groups": [
            {"site": "A", "mppts": [{"voltage_sensor": "sensor.a_v"}]},
            {"strings": [{"site": "B", "mppts": [
                {"voltage_sensor": "sensor.b_v", "current_sensor": "sensor.b_i"}]}]},
        ],
    }
    assert coordinator._collect_dc_entities(opts) == {
        "sensor.tv1", "sensor.ti1", "sensor.a_v", "sensor.b_v", "sensor.b_i",
    }


async def test_read_site_dc_telemetry_single_site_and_strings(hass, coordinator):
    hass.states.async_set("sensor.a_v", "405.0")
    hass.states.async_set("sensor.a_i", "6.0")
    hass.states.async_set("sensor.m1_v", "398.0")
    opts = {
        "site_groups": [
            {"ac_sensor": "sensor.inv_a", "site": "A", "mppts": [
                {"voltage_sensor": "sensor.a_v", "current_sensor": "sensor.a_i"},
            ]},
            {"ac_sensor": "sensor.shared", "strings": [
                {"site": "M1", "dc_sensor": "sensor.m1", "mppts": [
                    {"voltage_sensor": "sensor.m1_v"},
                ]},
                {"site": "M2", "dc_sensor": "sensor.m2"},  # no MPPT telemetry → absent
            ]},
        ]
    }
    out = coordinator._read_site_dc_telemetry(opts, {})
    assert out == {"A": (405.0, 6.0, 0.0, 0.0), "M1": (398.0, 0.0, 0.0, 0.0)}
    assert "M2" not in out


async def test_read_site_dc_telemetry_empty_when_unconfigured(hass, coordinator):
    assert coordinator._read_site_dc_telemetry({}, {}) == {}
    assert coordinator._read_site_dc_telemetry({"site_groups": []}, {}) == {}


async def test_mppt_list_from_opts_builds_two_pairs(coordinator):
    opts = {
        "mppt1_voltage_sensor": "sensor.v1", "mppt1_current_sensor": "sensor.i1",
        "mppt2_voltage_sensor": "sensor.v2",
    }
    assert coordinator._mppt_list_from_opts(opts) == [
        {"voltage_sensor": "sensor.v1", "current_sensor": "sensor.i1"},
        {"voltage_sensor": "sensor.v2", "current_sensor": None},
    ]


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


# ---------------------------------------------------------------------------
# OpenWeatherMap requirement — repair issue + fail-safe weather default
# ---------------------------------------------------------------------------

async def test_weather_default_is_unknown_not_clear(coordinator):
    """Without OWM the in-memory weather is unknown (None), never a false 0 clear."""
    assert coordinator._weather["clouds"] is None
    assert coordinator._weather["temp"] is None


async def test_setup_raises_owm_issue_when_disabled(hass, mock_config_entry):
    """OWM off + a cloud-driven feature on → a repair issue is raised, cleared on teardown."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(mock_config_entry.data),
        options={CONF_OWM_ENABLED: False, CONF_AUTO_TUNING: True},
        entry_id="owm_off_tuning_on",
    )
    entry.add_to_hass(hass)
    coord = SolcastEnhancedCoordinator(hass, entry)
    await coord.async_setup()
    try:
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_OWM_REQUIRED) is not None
    finally:
        await coord.async_teardown()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_OWM_REQUIRED) is None


async def test_setup_no_owm_issue_when_configured(hass, mock_config_entry):
    """A configured OWM key clears/avoids the repair issue."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(mock_config_entry.data),
        options={CONF_OWM_ENABLED: True, CONF_OWM_API_KEY: "k"},
        entry_id="owm_on",
    )
    entry.add_to_hass(hass)
    coord = SolcastEnhancedCoordinator(hass, entry)
    await coord.async_setup()
    try:
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_OWM_REQUIRED) is None
    finally:
        await coord.async_teardown()


# ---------------------------------------------------------------------------
# _weather_for_storage — NOT NULL DB coercion (single-site AND per-site rows)
# ---------------------------------------------------------------------------

async def test_weather_for_storage_coerces_unknown_to_excluded(coordinator):
    """Unknown weather (no OWM / failed fetch) → 0 °C and the 100% excluded sentinel."""
    coordinator._weather = {"temp": None, "clouds": None, "description": "unavailable"}
    temp, clouds, desc = coordinator._weather_for_storage()
    assert temp == 0.0
    assert clouds == 100  # excluded side — can never pass the clear-sky filter
    assert desc == "unavailable"


async def test_weather_for_storage_passes_real_values(coordinator):
    """Real OWM values (including a genuine 0% clear sky) are preserved/rounded."""
    coordinator._weather = {"temp": 18.456, "clouds": 0, "description": "clear sky"}
    temp, clouds, desc = coordinator._weather_for_storage()
    assert temp == pytest.approx(18.46)
    assert clouds == 0  # genuine clear sky is NOT coerced to the sentinel
    assert desc == "clear sky"


# ---------------------------------------------------------------------------
# Dampening convergence gate — _angle_diff / _orientation_diverged
# ---------------------------------------------------------------------------

def test_angle_diff_wraps_shortest_path():
    assert SolcastEnhancedCoordinator._angle_diff(10.0, 350.0) == pytest.approx(20.0)
    assert SolcastEnhancedCoordinator._angle_diff(350.0, 10.0) == pytest.approx(-20.0)
    assert SolcastEnhancedCoordinator._angle_diff(90.0, 270.0) == pytest.approx(-180.0)
    assert abs(SolcastEnhancedCoordinator._angle_diff(45.0, 50.0)) == pytest.approx(5.0)


async def test_orientation_diverged_none_without_result(coordinator):
    assert coordinator._orientation_diverged(None, 20.0, 0.0) is None


async def test_orientation_diverged_none_when_low_confidence(coordinator):
    """A big divergence is ignored until tuning has enough clear-sky records."""
    result = {"tilt": 60.0, "azimuth": 90.0, "n_records": DAMPENING_GATE_MIN_RECORDS - 1}
    assert coordinator._orientation_diverged(result, 20.0, 0.0) is None


async def test_orientation_diverged_none_when_aligned(coordinator):
    """Confident tuning that agrees with the configured orientation does not gate."""
    result = {"tilt": 22.0, "azimuth": 5.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 50}
    assert coordinator._orientation_diverged(result, 20.0, 0.0) is None


async def test_orientation_diverged_on_tilt(coordinator):
    result = {"tilt": 45.0, "azimuth": 2.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 50}
    div = coordinator._orientation_diverged(result, 20.0, 0.0)
    assert div is not None
    assert div["tilt_delta"] == pytest.approx(25.0)


async def test_orientation_diverged_on_azimuth_wraparound(coordinator):
    """Azimuth divergence uses the shortest circular distance, e.g. 350° vs 10°."""
    result = {"tilt": 21.0, "azimuth": 350.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 50}
    div = coordinator._orientation_diverged(result, 20.0, 40.0)
    assert div is not None
    # 350 vs 40 → 50° apart the short way, above the 25° tolerance
    assert div["azimuth_delta"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# _run_dampening — gate holds neutral + raises/clears the repair issue (single-site)
# ---------------------------------------------------------------------------

async def _run_gate_dampening(hass, coordinator, tuning_result, gate_on=True):
    """Drive _run_dampening with the DB/push/auto-dampen dependencies stubbed,
    returning the hourly factors that were pushed."""
    opts = {CONF_DAMPENING_GATE: gate_on}
    coordinator._tuning_result = tuning_result
    # A non-neutral slot table so a non-gated push is clearly distinguishable from 1.0.
    slots = [{"factor": 0.8} for _ in range(48)]
    pushed: dict[str, list[float]] = {}

    async def _fake_push(hourly, site=None):
        pushed["hourly"] = hourly
        pushed["site"] = site

    with patch.object(
        coordinator, "_compute_dampening_slots", AsyncMock(return_value=slots)
    ), patch.object(
        coordinator, "_read_base_auto_dampen", return_value=False
    ), patch.object(coordinator, "_push_dampening", side_effect=_fake_push):
        await coordinator._run_dampening(opts, int(time.time()), -37.9, 145.0)
    return pushed


async def test_run_dampening_gate_holds_neutral_and_raises_issue(hass, coordinator):
    diverged = {"tilt": 50.0, "azimuth": 0.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 50}
    pushed = await _run_gate_dampening(hass, coordinator, diverged)
    assert all(f == 1.0 for f in pushed["hourly"])  # held neutral
    assert coordinator._dampening_gated is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_DAMPENING_GATED) is not None


async def test_run_dampening_pushes_curve_when_aligned(hass, coordinator):
    aligned = {"tilt": 21.0, "azimuth": 2.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 50}
    pushed = await _run_gate_dampening(hass, coordinator, aligned)
    assert any(f != 1.0 for f in pushed["hourly"])  # real curve pushed
    assert coordinator._dampening_gated is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_DAMPENING_GATED) is None


async def test_run_dampening_gate_disabled_pushes_despite_divergence(hass, coordinator):
    diverged = {"tilt": 50.0, "azimuth": 0.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 50}
    pushed = await _run_gate_dampening(hass, coordinator, diverged, gate_on=False)
    assert any(f != 1.0 for f in pushed["hourly"])  # gate off → no neutralising
    assert coordinator._dampening_gated is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_DAMPENING_GATED) is None


# ---------------------------------------------------------------------------
# _push_dampening — clamp factors to [0,1] for the base set_dampening service
# ---------------------------------------------------------------------------

async def test_push_dampening_clamps_factors_to_unit_range(hass, coordinator):
    """The base set_dampening only accepts [0,1]; factors >1 (forecast
    under-predicts) clamp to 1.0 and negatives to 0.0 before the push."""
    captured: dict[str, object] = {}

    async def _handler(call):
        captured["damp"] = call.data["damp_factor"]

    hass.services.async_register("solcast_solar", "set_dampening", _handler)
    await coordinator._push_dampening([0.8, 1.3, 1.0, -0.2, 1.15])

    values = [float(x) for x in captured["damp"].split(",")]
    assert values == [0.8, 1.0, 1.0, 0.0, 1.0]
    assert all(0.0 <= v <= 1.0 for v in values)
