"""Test sensor entity native values and attributes."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.solcast_solar_enhanced import sensor as sensor_mod
from custom_components.solcast_solar_enhanced.sensor import (
    BaseIntegrationSensor,
    BatteryChargeSensor,
    CurrentDampeningSensor,
    DampeningSensor,
    DbRecordsSensor,
    ForecastNowSensor,
    ForecastTodaySensor,
    MpptDcSensor,
    PvActualSensor,
    PvExportSensor,
    PvForecastConfidenceSensor,
    SiteAzimuthSensor,
    SiteCurrentDampeningSensor,
    SiteOutputSensor,
    SiteShadingSensor,
    SiteTunedTiltSensor,
    SiteTuningRmseSensor,
    TuningAzimuthSensor,
    TuningExportExcludedSensor,
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


def _make_site_sensor(cls, coordinator, site_id: str):
    """Build a per-array sensor bound to ``site_id`` (the id it must query with)."""
    sensor = _make_sensor(cls, coordinator)
    sensor._site_id = site_id
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


def test_forecast_today_returns_none_when_no_data():
    s = _make_sensor(ForecastTodaySensor, _make_coordinator(None))
    assert s.native_value is None


# ---------------------------------------------------------------------------
# MpptDcSensor (diagnostic DC telemetry)
# ---------------------------------------------------------------------------

def test_mppt_dc_state_is_max_voltage_with_breakdown_attrs():
    dc = {
        "mppt1_voltage": 412.0, "mppt1_current": 6.0,
        "mppt2_voltage": 398.0, "mppt2_current": 5.1,
        "max_voltage": 412.0,
        "sites": {"A": {"mppt1_voltage": 412.0, "mppt1_current": 6.0,
                        "mppt2_voltage": 398.0, "mppt2_current": 5.1}},
    }
    s = _make_sensor(MpptDcSensor, _make_coordinator({"dc_telemetry": dc}))
    assert s.native_value == pytest.approx(412.0)
    attrs = s.extra_state_attributes
    assert "max_voltage" not in attrs  # state already carries it
    assert attrs["mppt1_voltage"] == 412.0 and attrs["mppt2_current"] == 5.1
    assert attrs["sites"]["A"]["mppt1_voltage"] == 412.0


def test_mppt_dc_unavailable_when_not_configured():
    # dc_telemetry None (no DC sensors) → entity stays unavailable, not 0.
    s = _make_sensor(MpptDcSensor, _make_coordinator({"dc_telemetry": None}))
    assert s.native_value is None
    assert s.extra_state_attributes is None
    # And when coordinator has no data at all.
    s2 = _make_sensor(MpptDcSensor, _make_coordinator(None))
    assert s2.native_value is None


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


@pytest.mark.parametrize(
    "cls, key",
    [
        (PvActualSensor, "pv_actual"),
        (PvExportSensor, "pv_export"),
        (BatteryChargeSensor, "battery_charge"),
    ],
)
def test_restoring_sensor_uses_restored_value_until_coordinator_updates(cls, key):
    """After a restart the coordinator has no data for up to ~30 min; the sensor
    reports its restored value, then the live value once it arrives."""
    coord = _make_coordinator(None)  # no data yet (just restarted)
    s = _make_sensor(cls, coord)
    s._restored_value = 3.3
    assert s.native_value == pytest.approx(3.3)  # restored bridges the gap

    coord.data = {key: 4.2}  # first update cycle arrives
    assert s.native_value == pytest.approx(4.2)  # live supersedes restored


@pytest.mark.parametrize("cls", [PvActualSensor, PvExportSensor, BatteryChargeSensor])
def test_restoring_sensor_none_when_no_data_and_nothing_restored(cls):
    s = _make_sensor(cls, _make_coordinator(None))
    assert s.native_value is None


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


def test_tuning_azimuth_reports_the_configured_value_it_was_given():
    """Azimuth is never fitted — tuning echoes the configured value straight back."""
    coord = _make_coordinator({}, tuning_azimuth=-8.0)
    s = _make_sensor(TuningAzimuthSensor, coord)
    assert s.native_value == pytest.approx(-8.0)


def test_tuning_export_excluded_returns_count():
    coord = _make_coordinator({}, tuning_export_excluded=17)
    s = _make_sensor(TuningExportExcludedSensor, coord)
    assert s.native_value == 17


def test_tuning_export_excluded_returns_zero_before_first_run():
    coord = _make_coordinator({}, tuning_export_excluded=0)
    s = _make_sensor(TuningExportExcludedSensor, coord)
    assert s.native_value == 0


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


def test_db_records_none_before_the_first_cycle():
    """No data at all is unknown, not a count of zero — which would read as data loss."""
    s = _make_sensor(DbRecordsSensor, _make_coordinator(None))
    assert s.native_value is None


def test_db_records_attributes_expose_freshness_and_sites():
    coord = _make_coordinator({
        "db_records": 100,
        "db_latest_period_end": "2026-06-04T12:30:00+00:00",
        "db_sites": ["_total", "abcd-1234"],
    })
    s = _make_sensor(DbRecordsSensor, coord)
    attrs = s.extra_state_attributes
    assert attrs["latest_period_end"] == "2026-06-04T12:30:00+00:00"
    assert attrs["distinct_sites"] == 2
    assert attrs["sites"] == ["_total", "abcd-1234"]


def test_db_records_attributes_none_without_data():
    coord = _make_coordinator(None)
    s = _make_sensor(DbRecordsSensor, coord)
    assert s.extra_state_attributes is None


# ---------------------------------------------------------------------------
# DampeningSensor
# ---------------------------------------------------------------------------

def test_dampening_sensor_returns_count():
    coord = _make_coordinator({},
                              dampening_hours_with_db=8,
                              dampening_attributes={"hour_10_factor": 0.85, "overall_source": "db_blended"})
    s = _make_sensor(DampeningSensor, coord)
    assert s.native_value == 8
    assert s.extra_state_attributes["overall_source"] == "db_blended"


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


# ---------------------------------------------------------------------------
# Current dampening / forecast confidence
# ---------------------------------------------------------------------------

def test_current_dampening_reads_through_to_the_coordinator():
    coord = _make_coordinator(
        {}, current_dampening=0.82, current_dampening_attributes={"alpha": 0.4, "raw_factor": 0.815}
    )
    s = _make_sensor(CurrentDampeningSensor, coord)
    assert s.native_value == pytest.approx(0.82)
    assert s.extra_state_attributes["alpha"] == pytest.approx(0.4)


def test_pv_forecast_confidence_reads_through_to_the_coordinator():
    coord = _make_coordinator({}, confidence=87, confidence_attributes={"rating": "high", "n_slots": 6})
    s = _make_sensor(PvForecastConfidenceSensor, coord)
    assert s.native_value == 87
    assert s.extra_state_attributes["rating"] == "high"


# ---------------------------------------------------------------------------
# Per-array (multi-site) sensors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cls", "value_method", "attr_method", "value"),
    [
        (SiteShadingSensor, "site_shading", "site_visibility_attributes", 0.71),
        (SiteOutputSensor, "site_output", "site_output_attributes", 2.4),
        (SiteTunedTiltSensor, "site_tuned_tilt", "site_tuned_tilt_attributes", 27.5),
        (SiteAzimuthSensor, "site_azimuth", None, -12.0),
        (SiteTuningRmseSensor, "site_tuned_rmse", None, 0.33),
        (SiteCurrentDampeningSensor, "site_current_dampening", "site_current_dampening_attributes", 0.9),
    ],
)
def test_site_sensor_asks_the_coordinator_for_its_own_array(cls, value_method, attr_method, value):
    """Each per-array sensor must pass *its* site id, not read a property-wide value.

    A sensor that ignored ``self._site_id`` would still return a number, so the id
    the coordinator was called with is what is asserted.
    """
    coord = _make_coordinator({})
    getattr(coord, value_method).return_value = value
    s = _make_site_sensor(cls, coord, "site-b")

    assert s.native_value == pytest.approx(value)
    getattr(coord, value_method).assert_called_with("site-b")

    if attr_method is not None:
        getattr(coord, attr_method).return_value = {"marker": attr_method}
        assert s.extra_state_attributes == {"marker": attr_method}
        getattr(coord, attr_method).assert_called_with("site-b")


def test_site_tuned_tilt_stays_available_when_the_tilt_is_unidentifiable():
    """An unreportable tilt must not hide the attributes explaining why.

    Home Assistant drops the attributes of an unavailable entity, and those
    attributes carry the reason plus the raw (unreported) tilt.
    """
    coord = _make_coordinator({})
    coord.last_update_success = True
    coord.site_tuned_tilt.return_value = None
    coord.site_tuned_tilt_attributes.return_value = {"unidentified_tilt": 3.5, "reason": "fit_too_loose"}
    s = _make_site_sensor(SiteTunedTiltSensor, coord, "site-b")

    assert s.native_value is None
    assert s.available is True
    assert s.extra_state_attributes["unidentified_tilt"] == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# Availability (quality scale: entity-unavailable)
# ---------------------------------------------------------------------------

def test_parallel_updates_declares_no_limit():
    """Coordinator-fed, read-only entities do no I/O, so nothing needs serialising."""
    assert sensor_mod.PARALLEL_UPDATES == 0


@pytest.mark.parametrize("cls", [WeatherTempSensor, WeatherCloudsSensor])
@pytest.mark.parametrize(
    ("data", "available"),
    [
        ({"weather": {"temp": 22.5, "clouds": 45}}, True),
        # No source enabled, or a fetch that failed fail-safe: the coordinator's
        # placeholder leaves the fields empty. That is a missing source, not an
        # unknown value, so the entity goes unavailable.
        ({"weather": {"temp": None, "clouds": None, "description": "unavailable"}}, False),
        ({"weather": {}}, False),
        ({}, False),
        (None, False),
    ],
)
def test_weather_sensor_available_only_while_a_source_supplies_it(cls, data, available):
    assert _make_sensor(cls, _make_coordinator(data)).available is available


def test_weather_sensor_follows_the_coordinator_when_the_refresh_fails():
    """A failed coordinator refresh takes the entity down even with a cached reading."""
    coord = _make_coordinator({"weather": {"temp": 22.5, "clouds": 45}})
    coord.last_update_success = False
    assert _make_sensor(WeatherTempSensor, coord).available is False


@pytest.mark.parametrize(
    ("data", "available"),
    [
        ({"dc_telemetry": {"max_voltage": 412.0}}, True),
        ({"dc_telemetry": None}, False),
        ({"dc_telemetry": {}}, False),
        (None, False),
    ],
)
def test_mppt_dc_available_only_while_telemetry_is_landing(data, available):
    assert _make_sensor(MpptDcSensor, _make_coordinator(data)).available is available


# ---------------------------------------------------------------------------
# Entity naming (quality scale: has-entity-name + entity-translations)
# ---------------------------------------------------------------------------

_COMPONENT = Path(sensor_mod.__file__).parent
_SENSOR_SRC = (_COMPONENT / "sensor.py").read_text(encoding="utf-8")


def _strings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _declared_translation_keys() -> set[str]:
    """Every ``_attr_translation_key`` literal declared in sensor.py."""
    return set(re.findall(r'_attr_translation_key = "([^"]+)"', _SENSOR_SRC))


def test_no_sensor_names_itself_with_a_raw_string():
    """Entity names come from translations, never a hardcoded ``_attr_name``.

    A raw ``_attr_name`` is invisible to the translation files, so it would ship
    an English-only name in all 11 locales while looking perfectly fine in review.
    """
    assert "_attr_name" not in _SENSOR_SRC


def test_every_translation_key_has_a_name_in_strings_json():
    """No sensor can reference a key that strings.json does not define."""
    names = _strings(_COMPONENT / "strings.json")["entity"]["sensor"]
    missing = _declared_translation_keys() - set(names)
    assert not missing, f"translation keys with no name: {sorted(missing)}"


def test_strings_json_has_no_unused_entity_names():
    """And strings.json carries no names for keys no sensor uses any more."""
    names = _strings(_COMPONENT / "strings.json")["entity"]["sensor"]
    orphaned = set(names) - _declared_translation_keys()
    assert not orphaned, f"entity names with no sensor: {sorted(orphaned)}"


@pytest.mark.parametrize(
    "locale",
    sorted(p.name for p in (_COMPONENT / "translations").glob("*.json")),
)
def test_every_locale_translates_every_entity_name(locale):
    """All 11 locales define a non-empty name for every key — no silent English gaps."""
    expected = set(_strings(_COMPONENT / "strings.json")["entity"]["sensor"])
    names = _strings(_COMPONENT / "translations" / locale)["entity"]["sensor"]

    assert set(names) == expected, f"{locale} key set differs from strings.json"
    assert all(entry["name"].strip() for entry in names.values()), f"{locale} has a blank name"


# ---------------------------------------------------------------------------
# Entity icons (quality scale: icon-translations)
# ---------------------------------------------------------------------------


def _icons() -> dict:
    return _strings(_COMPONENT / "icons.json")


def test_no_sensor_sets_its_icon_in_code():
    """Icons live in icons.json, never as a hardcoded ``_attr_icon``.

    An `_attr_icon` wins over icons.json silently, so a stray one would leave
    the JSON entry looking correct while doing nothing.
    """
    assert "_attr_icon" not in _SENSOR_SRC


def test_every_icon_key_belongs_to_a_real_sensor():
    """icons.json cannot carry an icon for a translation key no sensor declares."""
    orphaned = set(_icons()["entity"]["sensor"]) - _declared_translation_keys()
    assert not orphaned, f"icons for no sensor: {sorted(orphaned)}"


def test_icons_cover_every_sensor_without_a_device_class():
    """A sensor with no device class needs an explicit icon, or it renders blank.

    Sensors that *do* carry a device class inherit an icon from it, so they are
    allowed to be absent from icons.json rather than forced to duplicate it.
    """
    icon_keys = set(_icons()["entity"]["sensor"])
    for block in _SENSOR_SRC.split("\nclass ")[1:]:
        key = re.search(r'_attr_translation_key = "([^"]+)"', block)
        if key and "_attr_device_class" not in block:
            assert key.group(1) in icon_keys, f"{key.group(1)} has neither a device class nor an icon"


def test_every_action_has_an_icon():
    """All three actions carry an icon, matching services.yaml."""
    import yaml

    services = yaml.safe_load((_COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    assert set(_icons()["services"]) == set(services)
    assert all(entry["service"].startswith("mdi:") for entry in _icons()["services"].values())
