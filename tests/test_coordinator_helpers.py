"""Targeted tests for SolcastEnhancedCoordinator helper methods and sensor
properties — the pure/lightly-coupled paths that the orchestration-focused
suite leaves uncovered (forecast-slot matching, base-entry reads, energy-counter
edge cases, telemetry/tuning/dampening properties, force-* services)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.pv_tuning import panel_azimuth_to_solcast
from custom_components.solcast_solar_enhanced.const import (
    BASE_DOMAIN,
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MODE,
    CONF_BATTERY_NET_SENSOR,
    CONF_BATTERY_STAT_SENSOR,
    CONF_CAPACITY_KW,
    CONF_DAMPENING_GATE,
    CONF_DB_ENABLED,
    CONF_DB_RETENTION_DAYS,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_SITE_GROUPS,
    CONF_TILT,
    DAMPENING_GATE_MIN_RECORDS,
    DEFAULT_SITE_ID,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)

FORECAST_SENSOR = "sensor.solcast_pv_forecast_forecast_today"


@pytest.fixture
def coordinator(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, mock_config_entry)


def _base_entry(hass, options):
    MockConfigEntry(domain=BASE_DOMAIN, options=options, entry_id="base").add_to_hass(hass)


# ---------------------------------------------------------------------------
# _period_start_epoch — every accepted/rejected input form
# ---------------------------------------------------------------------------

def test_period_start_epoch_aware_datetime():
    dt = datetime(2026, 6, 9, 0, 0, tzinfo=timezone.utc)
    assert SolcastEnhancedCoordinator._period_start_epoch(dt) == dt.timestamp()


def test_period_start_epoch_naive_assumed_utc():
    naive = datetime(2026, 6, 9, 0, 0)
    expected = naive.replace(tzinfo=timezone.utc).timestamp()
    assert SolcastEnhancedCoordinator._period_start_epoch(naive) == expected


def test_period_start_epoch_iso_string():
    got = SolcastEnhancedCoordinator._period_start_epoch("2026-06-09T00:00:00+00:00")
    assert got == datetime(2026, 6, 9, tzinfo=timezone.utc).timestamp()


def test_period_start_epoch_epoch_number():
    assert SolcastEnhancedCoordinator._period_start_epoch(1_700_000_000) == 1_700_000_000.0
    assert SolcastEnhancedCoordinator._period_start_epoch(1700.5) == 1700.5


@pytest.mark.parametrize("bad", [None, "", "not-a-date", object(), ["list"]])
def test_period_start_epoch_rejects(bad):
    assert SolcastEnhancedCoordinator._period_start_epoch(bad) is None


# ---------------------------------------------------------------------------
# _forecast_slot / _total_/_site_forecast_for_period
# ---------------------------------------------------------------------------

def _set_forecast(hass, attr, entries):
    hass.states.async_set(FORECAST_SENSOR, "5.0", {attr: entries})


async def test_forecast_slot_no_sensor(hass, coordinator):
    assert coordinator._forecast_slot("detailedForecast", 1_700_000_000) == (0.0, 0.0, 0.0)


async def test_forecast_slot_missing_attr(hass, coordinator):
    hass.states.async_set(FORECAST_SENSOR, "5.0", {})
    assert coordinator._forecast_slot("detailedForecast", 1_700_000_000) == (0.0, 0.0, 0.0)


async def test_forecast_slot_matches_nearest(hass, coordinator):
    start = datetime(2026, 6, 9, 2, 0, tzinfo=timezone.utc)
    epoch = int(start.timestamp())
    _set_forecast(hass, "detailedForecast", [
        {"period_start": start - timedelta(minutes=30), "pv_estimate": 1.0},
        {"period_start": start, "pv_estimate": 3.0, "pv_estimate10": 2.0, "pv_estimate90": 4.0},
    ])
    assert coordinator._total_forecast_for_period(epoch) == (3.0, 2.0, 4.0)


async def test_forecast_slot_skips_unparseable_period(hass, coordinator):
    start = datetime(2026, 6, 9, 2, 0, tzinfo=timezone.utc)
    epoch = int(start.timestamp())
    _set_forecast(hass, "detailedForecast", [
        {"period_start": "garbage", "pv_estimate": 9.0},  # ts None → skipped
        {"period_start": start, "pv_estimate": 3.0},
    ])
    assert coordinator._total_forecast_for_period(epoch)[0] == 3.0


async def test_forecast_slot_out_of_window_zeroes(hass, coordinator):
    start = datetime(2026, 6, 9, 2, 0, tzinfo=timezone.utc)
    far = int(start.timestamp()) + 100_000  # >900s from any entry
    _set_forecast(hass, "detailedForecast", [{"period_start": start, "pv_estimate": 3.0}])
    assert coordinator._forecast_slot("detailedForecast", far) == (0.0, 0.0, 0.0)


async def test_forecast_slot_bad_value_coerces_zero(hass, coordinator):
    start = datetime(2026, 6, 9, 2, 0, tzinfo=timezone.utc)
    epoch = int(start.timestamp())
    _set_forecast(hass, "detailedForecast", [
        {"period_start": start, "pv_estimate": "oops", "pv_estimate10": None},
    ])
    assert coordinator._forecast_slot("detailedForecast", epoch) == (0.0, 0.0, 0.0)


async def test_site_forecast_underscore_fallback(hass, coordinator):
    start = datetime(2026, 6, 9, 2, 0, tzinfo=timezone.utc)
    epoch = int(start.timestamp())
    # Only the underscore variant present → fallback path is exercised.
    _set_forecast(hass, "detailedForecast_abcd-1234", [
        {"period_start": start, "pv_estimate": 2.5},
    ])
    assert coordinator._site_forecast_for_period("abcd-1234", epoch)[0] == 2.5


# ---------------------------------------------------------------------------
# Base config-entry reads
# ---------------------------------------------------------------------------

async def test_read_base_auto_dampen_true(hass, coordinator):
    _base_entry(hass, {"auto_dampen": True})
    assert coordinator._read_base_auto_dampen() is True


async def test_read_base_auto_dampen_false_when_unset(hass, coordinator):
    _base_entry(hass, {})
    assert coordinator._read_base_auto_dampen() is False


async def test_read_base_export_limit_watts_scaled(hass, coordinator):
    _base_entry(hass, {"site_export_limit": 5000})  # Watts → 5 kW
    assert coordinator._read_base_export_limit() == pytest.approx(5.0)


async def test_read_base_export_limit_kw_passthrough(hass, coordinator):
    _base_entry(hass, {"site_export_limit": 8})  # already kW (<=100)
    assert coordinator._read_base_export_limit() == pytest.approx(8.0)


async def test_read_base_export_limit_unset(hass, coordinator):
    _base_entry(hass, {"site_export_limit": ""})
    assert coordinator._read_base_export_limit() is None


# ---------------------------------------------------------------------------
# _read_battery — stat precedence + raw fallback modes
# ---------------------------------------------------------------------------

async def test_read_battery_stat_precedence(hass, coordinator):
    hass.states.async_set("sensor.batt_stat", "1.2")
    opts = {CONF_BATTERY_STAT_SENSOR: "sensor.batt_stat"}
    assert coordinator._read_battery(opts) == pytest.approx(1.2)


async def test_read_battery_net_fallback(hass, coordinator):
    hass.states.async_set("sensor.batt_net", "0.8")
    opts = {
        CONF_BATTERY_STAT_SENSOR: "",
        CONF_BATTERY_ENABLED: True,
        CONF_BATTERY_MODE: "net",
        CONF_BATTERY_NET_SENSOR: "sensor.batt_net",
    }
    assert coordinator._read_battery(opts) == pytest.approx(0.8)


async def test_read_battery_separate_charge_mode(hass, coordinator):
    hass.states.async_set("sensor.batt_charge", "0.5")
    opts = {
        CONF_BATTERY_STAT_SENSOR: "",
        CONF_BATTERY_ENABLED: True,
        CONF_BATTERY_MODE: "separate",
        CONF_BATTERY_CHARGE_SENSOR: "sensor.batt_charge",
    }
    assert coordinator._read_battery(opts) == pytest.approx(0.5)


async def test_read_battery_disabled_zero(hass, coordinator):
    assert coordinator._read_battery({CONF_BATTERY_STAT_SENSOR: ""}) == 0.0


# ---------------------------------------------------------------------------
# _read_forecast_from_base
# ---------------------------------------------------------------------------

async def test_read_forecast_from_base_coordinator(hass, coordinator):
    base = type("C", (), {"data": {
        "forecast_now": 2.5, "forecast_today": 18.0,
        "pv_estimate": 3.0, "pv_estimate10": 2.0, "pv_estimate90": 4.0,
    }})()
    assert coordinator._read_forecast_from_base(base) == (2.5, 18.0, 3.0, 2.0, 4.0)


async def test_read_forecast_from_base_fallback_to_sensor(hass, coordinator):
    # No base data → fallback reads the kWh sensor + detailedForecast slot.
    hass.states.async_set(FORECAST_SENSOR, "20.0", {})
    fn, ft, *rest = coordinator._read_forecast_from_base(None)
    assert ft == pytest.approx(20.0)
    assert rest == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# _read_pv_value — energy-counter edge cases
# ---------------------------------------------------------------------------

async def test_read_pv_value_no_entity(hass, coordinator):
    assert coordinator._read_pv_value("", "auto", "k", 1000) == (0.0, None)


async def test_read_pv_value_unavailable(hass, coordinator):
    hass.states.async_set("sensor.pv", "unavailable")
    assert coordinator._read_pv_value("sensor.pv", "auto", "k", 1000) == (0.0, None)


async def test_read_pv_value_non_numeric(hass, coordinator):
    hass.states.async_set("sensor.pv", "n/a", {"unit_of_measurement": "kWh"})
    assert coordinator._read_pv_value("sensor.pv", "auto", "k", 1000) == (0.0, None)


async def test_read_pv_value_first_read_seeds(hass, coordinator):
    hass.states.async_set("sensor.pv", "10.0", {"unit_of_measurement": "kWh"})
    val, start = coordinator._read_pv_value("sensor.pv", "auto", "k", 1000)
    assert (val, start) == (0.0, None)
    assert coordinator._energy_baselines["k"]["value"] == pytest.approx(10.0)


async def test_read_pv_value_energy_delta_avg_kw(hass, coordinator):
    now = 1_700_001_800
    prev = now - UPDATE_INTERVAL_MINUTES * 60  # exactly one interval earlier
    coordinator._energy_baselines["k"] = {"value": 10.0, "epoch": prev}
    hass.states.async_set("sensor.pv", "12.0", {"unit_of_measurement": "kWh"})
    val, start = coordinator._read_pv_value("sensor.pv", "auto", "k", now)
    # 2 kWh over 0.5 h → 4 kW
    assert val == pytest.approx(2.0 / (UPDATE_INTERVAL_MINUTES / 60.0))
    assert start == prev


async def test_read_pv_value_counter_reset_skipped(hass, coordinator):
    now = 1_700_001_800
    coordinator._energy_baselines["k"] = {"value": 50.0, "epoch": now - 1800}
    hass.states.async_set("sensor.pv", "5.0", {"unit_of_measurement": "kWh"})  # dropped
    assert coordinator._read_pv_value("sensor.pv", "auto", "k", now) == (0.0, None)


async def test_read_pv_value_zero_dt_skipped(hass, coordinator):
    now = 1_700_001_800
    coordinator._energy_baselines["k"] = {"value": 10.0, "epoch": now}  # dt == 0
    hass.states.async_set("sensor.pv", "11.0", {"unit_of_measurement": "kWh"})
    assert coordinator._read_pv_value("sensor.pv", "auto", "k", now) == (0.0, None)


async def test_read_pv_value_power_mode_direct(hass, coordinator):
    hass.states.async_set("sensor.pv", "3500", {"unit_of_measurement": "W"})
    val, start = coordinator._read_pv_value("sensor.pv", "auto", "k", 1000)
    assert val == pytest.approx(3.5)  # 3500 W → 3.5 kW
    assert start is None


async def test_save_baselines_clears_dirty(hass, coordinator):
    coordinator._baselines_dirty = True
    coordinator._store = type("S", (), {"async_save": AsyncMock()})()
    await coordinator._save_baselines()
    assert coordinator._baselines_dirty is False


# ---------------------------------------------------------------------------
# Orientation-divergence gate helpers
# ---------------------------------------------------------------------------

def test_angle_diff_wraps():
    assert SolcastEnhancedCoordinator._angle_diff(350, 10) == pytest.approx(-20)
    assert SolcastEnhancedCoordinator._angle_diff(10, 350) == pytest.approx(20)


async def test_orientation_diverged_none_when_no_result(coordinator):
    assert coordinator._orientation_diverged(None, 20.0, 0.0) is None


async def test_orientation_diverged_none_below_min_records(coordinator):
    res = {"tilt": 50.0, "azimuth": 90.0, "n_records": DAMPENING_GATE_MIN_RECORDS - 1}
    assert coordinator._orientation_diverged(res, 20.0, 0.0) is None


async def test_orientation_diverged_flags_large_delta(coordinator):
    res = {"tilt": 55.0, "azimuth": 80.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 10}
    div = coordinator._orientation_diverged(res, 20.0, 0.0)
    assert div is not None and div["tilt_delta"] == pytest.approx(35.0)


async def test_orientation_diverged_none_when_aligned(coordinator):
    res = {"tilt": 20.4, "azimuth": 0.3, "n_records": DAMPENING_GATE_MIN_RECORDS + 10}
    assert coordinator._orientation_diverged(res, 20.0, 0.0) is None


async def test_site_orientation_seed_unknown_site_uses_opts(coordinator):
    coordinator._sites = []
    tilt, az = coordinator._site_orientation_seed("missing", {CONF_TILT: 33.0, CONF_AZIMUTH: 12.0})
    assert tilt == pytest.approx(33.0)
    assert az == pytest.approx(12.0)


async def test_site_orientation_seed_uses_site_geometry(coordinator):
    coordinator._sites = [{"resource_id": "r1", "tilt": 25.0, "azimuth": 90.0}]
    tilt, az = coordinator._site_orientation_seed("r1", {})
    assert tilt == pytest.approx(25.0)
    # azimuth 90 (N-zero/E-neg) → compass 270 → internal -90
    assert az == pytest.approx(-90.0)


# ---------------------------------------------------------------------------
# _configured_site_ids
# ---------------------------------------------------------------------------

def test_configured_site_ids_dedupes_strings_and_group():
    groups = [
        {"site": "g1", "strings": [{"site": "s1"}, {"site": "s2"}]},
        {"site": "s1"},  # duplicate, dropped
        {"strings": [{"site": "s3"}]},
    ]
    assert SolcastEnhancedCoordinator._configured_site_ids(groups) == ["s1", "s2", "g1", "s3"]


# ---------------------------------------------------------------------------
# DC-telemetry shaping
# ---------------------------------------------------------------------------

def test_dc_telemetry_summary_shape():
    out = SolcastEnhancedCoordinator._dc_telemetry_summary(
        (414.0, 3.1, 410.0, 0.3), {"r1": (414.0, 3.1, 410.0, 0.3)}
    )
    assert out["mppt1_voltage"] == 414.0
    assert out["mppt2_current"] == 0.3
    assert out["max_voltage"] == 414.0
    assert out["sites"]["r1"]["mppt2_voltage"] == 410.0


async def test_read_site_dc_telemetry(hass, coordinator):
    hass.states.async_set("sensor.v1", "414")
    hass.states.async_set("sensor.i1", "3.1")
    opts = {CONF_SITE_GROUPS: [{
        "site": "r1",
        "mppts": [{"voltage_sensor": "sensor.v1", "current_sensor": "sensor.i1"}],
    }]}
    out = coordinator._read_site_dc_telemetry(opts, {})
    assert out["r1"][0] == pytest.approx(414.0)
    assert out["r1"][1] == pytest.approx(3.1)


# ---------------------------------------------------------------------------
# Tuning / dampening sensor properties
# ---------------------------------------------------------------------------

async def test_tuning_properties_none_without_result(coordinator):
    assert coordinator.tuning_tilt is None
    assert coordinator.tuning_azimuth is None
    assert coordinator.tuning_rmse is None
    assert coordinator.tuning_export_excluded == 0
    assert coordinator.tuning_extra == {}


async def test_tuning_properties_with_result(coordinator):
    coordinator._tuning_result = {
        "tilt": 22.0, "azimuth": -30.0, "rmse_kw": 0.42,
        "n_records": 120, "export_limited_excluded": 7,
    }
    assert coordinator.tuning_tilt == 22.0
    assert coordinator.tuning_rmse == 0.42
    assert coordinator.tuning_export_excluded == 7
    assert coordinator.tuning_azimuth == pytest.approx(panel_azimuth_to_solcast(-30.0))
    extra = coordinator.tuning_extra
    assert extra["n_records"] == 120
    assert extra["azimuth"] == pytest.approx(panel_azimuth_to_solcast(-30.0))


async def test_tuning_extra_includes_per_site(coordinator):
    coordinator._site_tuning_results = {
        "r1": {"name": "Home", "tilt": 24.75, "azimuth": -7.0, "rmse_kw": 0.3, "n_records": 90},
    }
    per_site = coordinator.tuning_extra["per_site"]
    assert per_site[0]["resource_id"] == "r1"
    assert per_site[0]["name"] == "Home"
    assert per_site[0]["tilt"] == pytest.approx(24.75)


async def test_dampening_properties(coordinator):
    def slot(factor, source):
        return {
            "factor": factor, "alpha": 0.5, "source": source,
            "quality_records": 4.0, "avg_quality": 0.9,
            "clipped_excluded": 1, "forecast_clipped": 0,
        }
    # 48 half-hour slots: 2 carrying real data, rest night. The property counts
    # qualifying *slots* (source not night/no_data), so 2 → 2.
    table = [slot(0.8, "db_blended"), slot(0.8, "db_blended")]
    table += [slot(1.0, "night") for _ in range(46)]
    coordinator._dampening_table = table

    assert coordinator.dampening_hours_with_db == 2
    attrs = coordinator.dampening_attributes
    assert attrs["hour_00_factor"] == pytest.approx(0.8)
    assert attrs["hour_00_source"] == "db_blended"
    assert attrs["hour_00_clipped_excluded"] == 2
    assert attrs["overall_source"] == "db_blended"
    assert attrs["gated"] is False


# ---------------------------------------------------------------------------
# force-* service entrypoints
# ---------------------------------------------------------------------------

async def test_async_force_pv_tuning_invokes_run(hass, coordinator):
    coordinator.data = {}
    with patch.object(coordinator, "_run_tuning", new=AsyncMock()) as run:
        await coordinator.async_force_pv_tuning()
        run.assert_awaited_once()


async def test_async_force_dampening_invokes_run(hass, coordinator):
    coordinator.data = {}
    with patch.object(coordinator, "_run_dampening", new=AsyncMock()) as run:
        await coordinator.async_force_dampening_update()
        run.assert_awaited_once()


async def test_async_force_fetch_weather_uses_owm(hass, coordinator):
    coordinator.data = {}
    coordinator._owm = type("O", (), {"async_fetch": AsyncMock(return_value={"temp": 9, "clouds": 50, "description": "x"})})()
    await coordinator.async_force_fetch_weather()
    assert coordinator._weather["clouds"] == 50


# ---------------------------------------------------------------------------
# misc static helpers
# ---------------------------------------------------------------------------

def test_snap_to_half_hour():
    assert SolcastEnhancedCoordinator._snap_to_half_hour(1800 + 901) == 1800 * 2
    assert SolcastEnhancedCoordinator._snap_to_half_hour(1800 + 899) == 1800


# ---------------------------------------------------------------------------
# _do_update — end-to-end orchestration (read → persist → return)
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal SqliteStore stand-in capturing inserted rows."""

    def __init__(self):
        self.records: list[dict] = []
        self.prune_calls = 0

    async def async_insert_record(self, rec):
        self.records.append(rec)

    async def async_get_record_count(self):
        return len(self.records)

    async def async_get_sites(self):
        return sorted({r["site"] for r in self.records})

    async def async_prune(self, days):
        self.prune_calls += 1
        return 0


_ORCH_CONFIG = {
    CONF_LATITUDE: -37.9,
    CONF_LONGITUDE: 145.0,
    CONF_CAPACITY_KW: 5.0,
    CONF_TILT: 20.0,
    CONF_AZIMUTH: 0.0,
    CONF_PV_ACTUAL_SENSOR: "sensor.pv_power_30min",
    CONF_PV_EXPORT_SENSOR: "sensor.pv_export_30min",
    CONF_AUTO_TUNING: False,
    CONF_AUTO_DAMPENING: False,
}


def _orch_coordinator(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ORCH_CONFIG,
        options=options or {},
        entry_id="orch",
        title="orch",
    )
    entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, entry)


def _set_pv(hass, actual="3.5", export="1.2"):
    hass.states.async_set("sensor.pv_power_30min", actual, {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.pv_export_30min", export, {"unit_of_measurement": "kW"})


async def test_do_update_persists_total_row_and_returns(hass, mock_base_coordinator):
    coord = _orch_coordinator(hass, {CONF_DB_ENABLED: True})
    coord._db = _FakeStore()
    _set_pv(hass)

    result = await coord._do_update()

    # Return payload reflects the reads.
    assert result["pv_actual"] == pytest.approx(3.5)
    assert result["pv_export"] == pytest.approx(1.2)
    assert result["forecast_now"] == pytest.approx(2.5)   # from base coordinator
    assert result["forecast_today"] == pytest.approx(18.0)
    assert result["base_status"] == "connected"
    assert result["db_records"] == 1
    assert result["dc_telemetry"] is None                 # no DC sensors configured

    # Exactly one aggregate '_total' row was written, carrying the base estimate.
    assert len(coord._db.records) == 1
    row = coord._db.records[0]
    assert row["site"] == DEFAULT_SITE_ID
    assert row["pv_actual"] == pytest.approx(3.5)
    assert row["pv_estimate"] == pytest.approx(3.0)       # base pv_estimate (no detailedForecast)
    assert coord._db_sites == [DEFAULT_SITE_ID]


async def test_do_update_no_base_no_db(hass):
    # No base integration in hass.data, DB disabled → no writes, status reflects it.
    coord = _orch_coordinator(hass, {CONF_DB_ENABLED: False})
    _set_pv(hass, actual="2.0", export="0.0")

    result = await coord._do_update()

    assert result["base_status"] == "not_detected"
    assert result["pv_actual"] == pytest.approx(2.0)
    assert result["forecast_now"] == 0.0                  # fallback, no forecast sensor
    assert result["db_records"] == 0


async def test_do_update_fires_tuning_and_dampening_timers(hass, mock_base_coordinator):
    # Auto tuning + dampening on with zeroed last-run timestamps → both fire.
    coord = _orch_coordinator(hass, {
        CONF_DB_ENABLED: False,
        CONF_AUTO_TUNING: True,
        CONF_AUTO_DAMPENING: True,
    })
    _set_pv(hass)

    with patch.object(coord, "_run_tuning", new=AsyncMock()) as tune, \
         patch.object(coord, "_run_dampening", new=AsyncMock()) as damp:
        await coord._do_update()
        tune.assert_awaited_once()
        damp.assert_awaited_once()

    assert coord._last_tuning_ts > 0
    assert coord._last_dampening_ts > 0


async def test_do_update_prefers_detailed_forecast_slot(hass, mock_base_coordinator):
    # A detailedForecast slot for the current period overrides the base estimate.
    coord = _orch_coordinator(hass, {CONF_DB_ENABLED: True})
    coord._db = _FakeStore()
    _set_pv(hass)

    # The DB forecast lookup keys off period_epoch − 1800; replicate the snap so
    # the entry lands exactly on the slot the update will query.
    slot_start = coord._snap_to_half_hour(int(time.time())) - 1800
    start_dt = datetime.fromtimestamp(slot_start, tz=timezone.utc)
    hass.states.async_set(FORECAST_SENSOR, "20.0", {
        "detailedForecast": [
            {"period_start": start_dt, "pv_estimate": 6.6,
             "pv_estimate10": 5.0, "pv_estimate90": 7.0},
        ],
    })

    await coord._do_update()
    row = coord._db.records[0]
    assert row["pv_estimate"] == pytest.approx(6.6)       # detailedForecast wins over base 3.0
    assert row["pv_estimate90"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# _read_site_actuals — multi-site measurement
# ---------------------------------------------------------------------------

async def test_read_site_actuals_single_site_group(hass, coordinator):
    hass.states.async_set("sensor.inv_ac", "4.0", {"unit_of_measurement": "kW"})
    opts = {CONF_SITE_GROUPS: [{"ac_sensor": "sensor.inv_ac", "site": "r1"}]}
    out = coordinator._read_site_actuals(opts, 1000)
    assert out["r1"][0] == pytest.approx(4.0)


async def test_read_site_actuals_dc_apportionment(hass, coordinator):
    # AC split across two strings by their DC share (3:1 → 0.75 / 0.25).
    hass.states.async_set("sensor.inv_ac", "5.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.mppt1", "3.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.mppt2", "1.0", {"unit_of_measurement": "kW"})
    opts = {CONF_SITE_GROUPS: [{
        "ac_sensor": "sensor.inv_ac",
        "strings": [
            {"site": "r1", "dc_sensor": "sensor.mppt1"},
            {"site": "r2", "dc_sensor": "sensor.mppt2"},
        ],
    }]}
    out = coordinator._read_site_actuals(opts, 1000)
    assert out["r1"][0] == pytest.approx(5.0 * 0.75)
    assert out["r2"][0] == pytest.approx(5.0 * 0.25)


async def test_read_site_actuals_zero_dc_yields_zero(hass, coordinator):
    # Σ dc == 0 → guarded, each string gets 0 (no divide-by-zero).
    hass.states.async_set("sensor.inv_ac", "5.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.mppt1", "0", {"unit_of_measurement": "kW"})
    opts = {CONF_SITE_GROUPS: [{
        "ac_sensor": "sensor.inv_ac",
        "strings": [{"site": "r1", "dc_sensor": "sensor.mppt1"}],
    }]}
    assert coordinator._read_site_actuals(opts, 1000)["r1"][0] == 0.0


async def test_read_site_actuals_no_groups(coordinator):
    assert coordinator._read_site_actuals({}, 1000) == {}


async def test_read_site_actuals_skips_group_without_ac(hass, coordinator):
    opts = {CONF_SITE_GROUPS: [{"site": "r1"}]}  # no ac_sensor
    assert coordinator._read_site_actuals(opts, 1000) == {}


# ---------------------------------------------------------------------------
# _do_update — per-site rows
# ---------------------------------------------------------------------------

async def test_do_update_writes_per_site_rows(hass, mock_base_coordinator):
    rid = "abcd-1234"
    groups = [{"ac_sensor": "sensor.inv_ac", "site": rid}]
    coord = _orch_coordinator(hass, {CONF_DB_ENABLED: True, CONF_SITE_GROUPS: groups})
    coord._db = _FakeStore()
    _set_pv(hass)
    hass.states.async_set("sensor.inv_ac", "4.0", {"unit_of_measurement": "kW"})

    slot_start = coord._snap_to_half_hour(int(time.time())) - 1800
    start_dt = datetime.fromtimestamp(slot_start, tz=timezone.utc)
    hass.states.async_set(FORECAST_SENSOR, "20.0", {
        f"detailedForecast-{rid}": [{"period_start": start_dt, "pv_estimate": 2.2}],
    })

    await coord._do_update()

    rows = {r["site"]: r for r in coord._db.records}
    assert set(rows) == {DEFAULT_SITE_ID, rid}
    # Per-site row carries the apportioned actual, its own forecast, and no battery.
    assert rows[rid]["pv_actual"] == pytest.approx(4.0)
    assert rows[rid]["pv_estimate"] == pytest.approx(2.2)
    assert rows[rid]["battery_charge"] == 0.0
    assert set(coord._db_sites) == {DEFAULT_SITE_ID, rid}


# ---------------------------------------------------------------------------
# _run_tuning / _run_site_tuning
# ---------------------------------------------------------------------------

class _TuningStore(_FakeStore):
    """Fake store that also serves tuning queries per site."""

    def __init__(self, records_by_site):
        super().__init__()
        self._by_site = records_by_site

    async def async_get_records_for_tuning(self, site, cloud_max):
        return self._by_site.get(site, [])


async def test_run_tuning_no_db_noop(hass, coordinator):
    coordinator._db = None
    await coordinator._run_tuning({**coordinator._entry.data})
    assert coordinator._tuning_result is None


async def test_run_tuning_no_records_skips(hass, coordinator):
    coordinator._db = _TuningStore({})  # no rows for any site
    await coordinator._run_tuning({**coordinator._entry.data})
    assert coordinator._tuning_result is None


async def test_run_tuning_sets_aggregate_and_per_site(hass):
    rid = "abcd-1234"
    groups = [{"ac_sensor": "sensor.inv_ac", "site": rid}]
    coord = _orch_coordinator(hass, {CONF_SITE_GROUPS: groups})
    coord._sites = [
        {"resource_id": rid, "name": "Home", "capacity": 8, "tilt": 24.75, "azimuth": 7},
    ]
    recs = [{"row": 1}]
    coord._db = _TuningStore({DEFAULT_SITE_ID: recs, rid: recs})

    fake = MagicMock(return_value={
        "tilt": 22.0, "azimuth": -10.0, "rmse_kw": 0.3, "n_records": 40,
    })
    opts = {**coord._entry.data, **coord._entry.options}
    with patch("custom_components.solcast_solar_enhanced.coordinator.run_tuning", fake):
        await coord._run_tuning(opts)

    assert coord._tuning_result["tilt"] == 22.0
    assert rid in coord._site_tuning_results
    assert coord._site_tuning_results[rid]["name"] == "Home"
    assert coord._site_tuning_results[rid]["resource_id"] == rid


async def test_run_site_tuning_no_site_ids_noop(hass, coordinator):
    coordinator._db = _TuningStore({DEFAULT_SITE_ID: [{"row": 1}]})
    await coordinator._run_site_tuning({}, export_limit=5.0)  # no CONF_SITE_GROUPS
    assert coordinator._site_tuning_results == {}


# ---------------------------------------------------------------------------
# _run_dampening — multi-site push + convergence gate
# ---------------------------------------------------------------------------

async def test_run_dampening_pushes_per_site(hass):
    # Configured site → a per-site push; the conflicting global push is skipped.
    rid = "abcd-1234"
    coord = _orch_coordinator(hass, {
        CONF_SITE_GROUPS: [{"ac_sensor": "sensor.inv_ac", "site": rid}],
        CONF_DAMPENING_GATE: False,
    })
    coord._db = _FakeStore()
    slots = [{"factor": 0.9} for _ in range(48)]
    pushed: list = []

    async def fake_compute(opts, now, lat, lon, site):
        return slots

    async def fake_push(hourly, site=None):
        pushed.append(site)

    with patch.object(coord, "_compute_dampening_slots", side_effect=fake_compute), \
         patch.object(coord, "_push_dampening", side_effect=fake_push):
        opts = {**coord._entry.data, **coord._entry.options}
        await coord._run_dampening(opts, 1000, -37.9, 145.0)

    assert pushed == [rid]
    assert coord._dampening_gated is False


async def test_run_dampening_gates_diverged_site(hass):
    # A confident tuned orientation that diverges from the configured site holds
    # that site's push at neutral 1.0 and sets the gated flag.
    rid = "abcd-1234"
    coord = _orch_coordinator(hass, {
        CONF_SITE_GROUPS: [{"ac_sensor": "sensor.inv_ac", "site": rid}],
        CONF_DAMPENING_GATE: True,
    })
    coord._db = _FakeStore()
    coord._sites = [{"resource_id": rid, "tilt": 20.0, "azimuth": 0.0}]
    coord._site_tuning_results = {
        rid: {"tilt": 55.0, "azimuth": 80.0, "n_records": DAMPENING_GATE_MIN_RECORDS + 10},
    }
    slots = [{"factor": 0.8} for _ in range(48)]
    pushed: dict = {}

    async def fake_compute(opts, now, lat, lon, site):
        return slots

    async def fake_push(hourly, site=None):
        pushed[site] = hourly

    with patch.object(coord, "_compute_dampening_slots", side_effect=fake_compute), \
         patch.object(coord, "_push_dampening", side_effect=fake_push):
        opts = {**coord._entry.data, **coord._entry.options}
        await coord._run_dampening(opts, 1000, -37.9, 145.0)

    assert coord._dampening_gated is True
    assert pushed[rid] and all(f == 1.0 for f in pushed[rid])


# ---------------------------------------------------------------------------
# _interval_values — recorder-backed DC history
# ---------------------------------------------------------------------------

async def test_interval_values_empty_ids(coordinator):
    assert await coordinator._interval_values(set(), 1000, 2000) == {}


async def test_interval_values_reads_recorder(hass, coordinator):
    states = {"sensor.v1": [
        SimpleNamespace(state="414.0"),
        SimpleNamespace(state="unknown"),  # non-numeric → skipped
        SimpleNamespace(state="410.5"),
    ]}
    rec = MagicMock()
    rec.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
    with patch("homeassistant.components.recorder.get_instance", return_value=rec), \
         patch("homeassistant.components.recorder.history.get_significant_states",
               return_value=states):
        out = await coordinator._interval_values({"sensor.v1"}, 1000, 2000)
    assert out == {"sensor.v1": [414.0, 410.5]}


async def test_interval_values_recorder_error_degrades(hass, coordinator):
    rec = MagicMock()
    rec.async_add_executor_job = AsyncMock(side_effect=RuntimeError("recorder down"))
    with patch("homeassistant.components.recorder.get_instance", return_value=rec), \
         patch("homeassistant.components.recorder.history.get_significant_states",
               return_value={}):
        assert await coordinator._interval_values({"sensor.v1"}, 1000, 2000) == {}


# ---------------------------------------------------------------------------
# _do_update — history retention prune
# ---------------------------------------------------------------------------

async def test_do_update_prunes_history(hass, mock_base_coordinator):
    coord = _orch_coordinator(hass, {CONF_DB_ENABLED: True, CONF_DB_RETENTION_DAYS: 7})
    store = _FakeStore()
    store.async_prune = AsyncMock(return_value=3)  # report 3 rows removed
    coord._db = store
    _set_pv(hass)

    await coord._do_update()

    store.async_prune.assert_awaited_once_with(7)
    assert coord._last_prune_ts > 0


async def test_do_update_no_prune_when_retention_zero(hass, mock_base_coordinator):
    coord = _orch_coordinator(hass, {CONF_DB_ENABLED: True, CONF_DB_RETENTION_DAYS: 0})
    store = _FakeStore()
    store.async_prune = AsyncMock(return_value=0)
    coord._db = store
    _set_pv(hass)

    await coord._do_update()

    store.async_prune.assert_not_awaited()
    assert coord._last_prune_ts == 0.0


async def test_run_site_tuning_skips_site_without_records(hass):
    # Two configured sites; only r1 has rows → r2 hits the `continue` and is absent.
    coord = _orch_coordinator(hass, {CONF_SITE_GROUPS: [
        {"ac_sensor": "sensor.a", "site": "r1"},
        {"ac_sensor": "sensor.b", "site": "r2"},
    ]})
    coord._sites = [{"resource_id": "r1", "name": "A"}]
    coord._db = _TuningStore({"r1": [{"row": 1}]})  # r2 absent

    fake = MagicMock(return_value={"tilt": 20.0, "azimuth": 0.0, "rmse_kw": 0.1, "n_records": 10})
    opts = {**coord._entry.data, **coord._entry.options}
    with patch("custom_components.solcast_solar_enhanced.coordinator.run_tuning", fake):
        await coord._run_site_tuning(opts, export_limit=5.0)

    assert "r1" in coord._site_tuning_results
    assert "r2" not in coord._site_tuning_results
