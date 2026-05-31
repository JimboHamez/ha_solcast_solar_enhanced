"""Test sensor entity native values and attributes."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.solcast_solar_enhanced.sensor import (
    BaseIntegrationSensor,
    BatteryChargeSensor,
    DampeningSensor,
    DbRecordsSensor,
    ForecastNowSensor,
    ForecastTodaySensor,
    PvActualSensor,
    PvExportSensor,
    TuningAzimuthSensor,
    TuningRmseSensor,
    TuningTiltSensor,
    WeatherCloudsSensor,
    WeatherTempSensor,
)


def _make_coordinator(data: dict | None = None, **props) -> MagicMock:
    coord = MagicMock()
    coord.data = data
    for k, v in props.items():
        setattr(coord, k, v)
    return coord


def _make_sensor(cls, coordinator):
    entry = MagicMock()
    entry.entry_id = "test"
    sensor = cls.__new__(cls)
    sensor.coordinator = coordinator
    sensor._entry = entry
    return sensor


# ---------------------------------------------------------------------------
# ForecastNowSensor
# ---------------------------------------------------------------------------

def test_forecast_now_returns_value():
    coord = _make_coordinator({"forecast_now": 3.5})
    s = _make_sensor(ForecastNowSensor, coord)
    assert s.native_value == pytest.approx(3.5)


def test_forecast_now_returns_none_when_no_data():
    coord = _make_coordinator(None)
    s = _make_sensor(ForecastNowSensor, coord)
    assert s.native_value is None


# ---------------------------------------------------------------------------
# ForecastTodaySensor
# ---------------------------------------------------------------------------

def test_forecast_today_returns_value():
    coord = _make_coordinator({"forecast_today": 18.0})
    s = _make_sensor(ForecastTodaySensor, coord)
    assert s.native_value == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# PvActualSensor / PvExportSensor / BatteryChargeSensor
# ---------------------------------------------------------------------------

def test_pv_actual_returns_value():
    coord = _make_coordinator({"pv_actual": 4.2})
    s = _make_sensor(PvActualSensor, coord)
    assert s.native_value == pytest.approx(4.2)


def test_pv_export_returns_value():
    coord = _make_coordinator({"pv_export": 1.1})
    s = _make_sensor(PvExportSensor, coord)
    assert s.native_value == pytest.approx(1.1)


def test_battery_charge_returns_value():
    coord = _make_coordinator({"battery_charge": 0.5})
    s = _make_sensor(BatteryChargeSensor, coord)
    assert s.native_value == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Weather sensors
# ---------------------------------------------------------------------------

def test_weather_temp_returns_value():
    coord = _make_coordinator({"weather": {"temp": 22.5, "clouds": 10}})
    s = _make_sensor(WeatherTempSensor, coord)
    assert s.native_value == pytest.approx(22.5)


def test_weather_clouds_returns_value():
    coord = _make_coordinator({"weather": {"temp": 22.5, "clouds": 45}})
    s = _make_sensor(WeatherCloudsSensor, coord)
    assert s.native_value == 45


# ---------------------------------------------------------------------------
# Tuning sensors
# ---------------------------------------------------------------------------

def test_tuning_tilt_returns_none_before_first_run():
    coord = _make_coordinator({}, tuning_tilt=None, tuning_azimuth=None, tuning_rmse=None)
    s = _make_sensor(TuningTiltSensor, coord)
    assert s.native_value is None


def test_tuning_tilt_returns_value():
    coord = _make_coordinator({}, tuning_tilt=22.5, tuning_azimuth=5.0, tuning_rmse=0.12,
                              tuning_extra={"azimuth": 5.0, "rmse_kw": 0.12, "n_records": 25})
    s = _make_sensor(TuningTiltSensor, coord)
    assert s.native_value == pytest.approx(22.5)
    assert s.extra_state_attributes["n_records"] == 25


def test_tuning_rmse_returns_value():
    coord = _make_coordinator({}, tuning_rmse=0.08)
    s = _make_sensor(TuningRmseSensor, coord)
    assert s.native_value == pytest.approx(0.08)


# ---------------------------------------------------------------------------
# DbRecordsSensor
# ---------------------------------------------------------------------------

def test_db_records_returns_count():
    coord = _make_coordinator({"db_records": 142})
    s = _make_sensor(DbRecordsSensor, coord)
    assert s.native_value == 142


def test_db_records_defaults_zero():
    # Non-empty dict (so the `if not data` guard passes) but no db_records key
    coord = _make_coordinator({"pv_actual": 0.0})
    s = _make_sensor(DbRecordsSensor, coord)
    assert s.native_value == 0


# ---------------------------------------------------------------------------
# DampeningSensor
# ---------------------------------------------------------------------------

def test_dampening_sensor_returns_count():
    coord = _make_coordinator({},
                              dampening_hours_with_db=8,
                              dampening_attributes={"hour_10_factor": 0.85, "overall_source": "blended"})
    s = _make_sensor(DampeningSensor, coord)
    assert s.native_value == 8
    assert s.extra_state_attributes["overall_source"] == "blended"


# ---------------------------------------------------------------------------
# BaseIntegrationSensor
# ---------------------------------------------------------------------------

def test_base_status_connected():
    coord = _make_coordinator({"base_status": "connected"})
    s = _make_sensor(BaseIntegrationSensor, coord)
    assert s.native_value == "connected"


def test_base_status_not_detected():
    coord = _make_coordinator({"base_status": "not_detected"})
    s = _make_sensor(BaseIntegrationSensor, coord)
    assert s.native_value == "not_detected"


def test_base_status_none_when_no_data():
    coord = _make_coordinator(None)
    s = _make_sensor(BaseIntegrationSensor, coord)
    assert s.native_value is None
