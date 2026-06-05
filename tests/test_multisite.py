"""Tests for multi-site support: discovery, energy-delta reads, DC apportionment,
per-site forecast matching, azimuth seeding and config-flow group derivation."""
from __future__ import annotations

import pytest

from custom_components.solcast_solar_enhanced.coordinator import (
    SolcastEnhancedCoordinator,
    discover_sites,
)
from custom_components.solcast_solar_enhanced.config_flow import (
    _derive_groups,
    _groups_to_assignments,
)
from custom_components.solcast_solar_enhanced.const import CONF_SITE_GROUPS


@pytest.fixture
def coordinator(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, mock_config_entry)


# ---------------------------------------------------------------------------
# discover_sites
# ---------------------------------------------------------------------------

def _set_site(hass, entity_id, rid, **attrs):
    base = {"resource_id": rid, "name": attrs.pop("name", rid)}
    base.update(attrs)
    hass.states.async_set(entity_id, "1.0", base)


async def test_discover_sites_finds_rooftops(hass):
    _set_site(
        hass, "sensor.solcast_pv_forecast_iris_ne", "b68d-c05a", name="Iris NE",
        capacity=5, capacity_dc=6.2, tilt=30, azimuth=-67.5, compass_degrees=67.5,
    )
    _set_site(hass, "sensor.solcast_pv_forecast_iris_nw", "aaaa-bbbb", name="Iris NW")
    hass.states.async_set("sensor.unrelated_power", "2.0")  # no resource_id

    sites = discover_sites(hass)
    by_id = {s["resource_id"]: s for s in sites}
    assert set(by_id) == {"b68d-c05a", "aaaa-bbbb"}
    assert by_id["b68d-c05a"]["name"] == "Iris NE"
    assert by_id["b68d-c05a"]["capacity_dc"] == pytest.approx(6.2)
    assert by_id["b68d-c05a"]["compass_degrees"] == pytest.approx(67.5)


async def test_discover_sites_ignores_non_solcast(hass):
    hass.states.async_set("sensor.foo", "1.0", {"resource_id": "x"})  # not solcast
    assert discover_sites(hass) == []


# ---------------------------------------------------------------------------
# _read_pv_value — power modes
# ---------------------------------------------------------------------------

async def test_read_pv_power_auto_watts(hass, coordinator):
    hass.states.async_set(
        "sensor.p", "3500",
        {"unit_of_measurement": "W", "state_class": "measurement"},
    )
    val, start = coordinator._read_pv_value("sensor.p", "auto", "k", 1_000_000)
    assert val == pytest.approx(3.5)
    assert start is None


async def test_read_pv_power_kw_explicit(hass, coordinator):
    hass.states.async_set("sensor.p", "3.5")
    val, _ = coordinator._read_pv_value("sensor.p", "power_kw", "k", 1_000_000)
    assert val == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# _read_pv_value — energy-counter modes (delta over actual interval)
# ---------------------------------------------------------------------------

async def test_read_pv_energy_first_read_seeds_baseline(hass, coordinator):
    hass.states.async_set(
        "sensor.e", "10.0",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    val, start = coordinator._read_pv_value("sensor.e", "auto", "pv", 1_000_000)
    assert val == 0.0 and start is None
    assert coordinator._energy_baselines["pv"]["value"] == pytest.approx(10.0)


async def test_read_pv_energy_delta_average_kw(hass, coordinator):
    coordinator._energy_baselines["pv"] = {"value": 10.0, "epoch": 1_000_000}
    hass.states.async_set(
        "sensor.e", "11.0",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    # 1.0 kWh over 1800 s = 2.0 kW average
    val, start = coordinator._read_pv_value("sensor.e", "auto", "pv", 1_000_000 + 1800)
    assert val == pytest.approx(2.0)
    assert start == 1_000_000


async def test_read_pv_energy_wh_units(hass, coordinator):
    coordinator._energy_baselines["pv"] = {"value": 10.0, "epoch": 1_000_000}  # kWh
    hass.states.async_set(
        "sensor.e", "11000",  # Wh
        {"unit_of_measurement": "Wh", "state_class": "total_increasing"},
    )
    val, _ = coordinator._read_pv_value("sensor.e", "auto", "pv", 1_000_000 + 1800)
    assert val == pytest.approx(2.0)


async def test_read_pv_energy_counter_reset_excluded(hass, coordinator):
    coordinator._energy_baselines["pv"] = {"value": 11.0, "epoch": 1_000_000}
    hass.states.async_set(
        "sensor.e", "1.0",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    val, _ = coordinator._read_pv_value("sensor.e", "auto", "pv", 1_000_000 + 1800)
    assert val == 0.0  # negative delta → reset, skipped


async def test_read_pv_energy_interval_out_of_bounds_excluded(hass, coordinator):
    coordinator._energy_baselines["pv"] = {"value": 10.0, "epoch": 1_000_000}
    hass.states.async_set(
        "sensor.e", "20.0",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    # dt = 7200 s > 2× the 1800 s expected interval → excluded
    val, _ = coordinator._read_pv_value("sensor.e", "auto", "pv", 1_000_000 + 7200)
    assert val == 0.0


# ---------------------------------------------------------------------------
# _read_site_actuals — DC apportionment
# ---------------------------------------------------------------------------

async def test_site_actuals_dc_apportionment(hass, coordinator):
    hass.states.async_set("sensor.ac", "4.0")        # 4 kW AC (auto → power_kw)
    hass.states.async_set("sensor.dc_a", "3.0")
    hass.states.async_set("sensor.dc_b", "1.0")
    opts = {CONF_SITE_GROUPS: [{
        "ac_sensor": "sensor.ac",
        "strings": [
            {"site": "A", "dc_sensor": "sensor.dc_a"},
            {"site": "B", "dc_sensor": "sensor.dc_b"},
        ],
    }]}
    out = coordinator._read_site_actuals(opts, 1_000_000)
    assert out["A"][0] == pytest.approx(3.0)   # 4 × 3/4
    assert out["B"][0] == pytest.approx(1.0)   # 4 × 1/4


async def test_site_actuals_single_site_group(hass, coordinator):
    hass.states.async_set("sensor.ac", "2.5")
    opts = {CONF_SITE_GROUPS: [{"ac_sensor": "sensor.ac", "site": "C"}]}
    out = coordinator._read_site_actuals(opts, 1_000_000)
    assert out["C"][0] == pytest.approx(2.5)


async def test_site_actuals_zero_dc_guarded(hass, coordinator):
    hass.states.async_set("sensor.ac", "4.0")
    hass.states.async_set("sensor.dc_a", "0")
    hass.states.async_set("sensor.dc_b", "0")
    opts = {CONF_SITE_GROUPS: [{
        "ac_sensor": "sensor.ac",
        "strings": [
            {"site": "A", "dc_sensor": "sensor.dc_a"},
            {"site": "B", "dc_sensor": "sensor.dc_b"},
        ],
    }]}
    out = coordinator._read_site_actuals(opts, 1_000_000)
    assert out["A"][0] == 0.0 and out["B"][0] == 0.0


async def test_site_actuals_empty_without_groups(hass, coordinator):
    assert coordinator._read_site_actuals({}, 1_000_000) == {}


# ---------------------------------------------------------------------------
# _site_forecast_for_period
# ---------------------------------------------------------------------------

async def test_site_forecast_matches_slot(hass, coordinator):
    rid = "b68d-c05a"
    hass.states.async_set(
        "sensor.solcast_pv_forecast_forecast_today", "10.0",
        {
            f"detailedForecast-{rid}": [
                {"period_start": "2024-09-10T06:00:00+00:00", "pv_estimate": 0.5,
                 "pv_estimate10": 0.3, "pv_estimate90": 0.7},
                {"period_start": "2024-09-10T06:30:00+00:00", "pv_estimate": 1.5,
                 "pv_estimate10": 1.0, "pv_estimate90": 2.0},
            ],
        },
    )
    import datetime as dt
    start = int(dt.datetime(2024, 9, 10, 6, 30, tzinfo=dt.timezone.utc).timestamp())
    est, e10, e90 = coordinator._site_forecast_for_period(rid, start)
    assert est == pytest.approx(1.5)
    assert e10 == pytest.approx(1.0)
    assert e90 == pytest.approx(2.0)


async def test_site_forecast_underscore_fallback(hass, coordinator):
    rid = "b68d-c05a"
    hass.states.async_set(
        "sensor.solcast_pv_forecast_forecast_today", "10.0",
        {f"detailedForecast_{rid}": [
            {"period_start": "2024-09-10T06:30:00+00:00", "pv_estimate": 1.5},
        ]},
    )
    import datetime as dt
    start = int(dt.datetime(2024, 9, 10, 6, 30, tzinfo=dt.timezone.utc).timestamp())
    est, _, _ = coordinator._site_forecast_for_period(rid, start)
    assert est == pytest.approx(1.5)


async def test_site_forecast_missing_returns_zeros(hass, coordinator):
    assert coordinator._site_forecast_for_period("nope", 1_000_000) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# _total_forecast_for_period (property-wide detailedForecast)
# ---------------------------------------------------------------------------

async def test_total_forecast_matches_slot(hass, coordinator):
    hass.states.async_set(
        "sensor.solcast_pv_forecast_forecast_today", "10.0",
        {
            "detailedForecast": [
                {"period_start": "2024-09-10T06:00:00+00:00", "pv_estimate": 0.5,
                 "pv_estimate10": 0.3, "pv_estimate90": 0.7},
                {"period_start": "2024-09-10T06:30:00+00:00", "pv_estimate": 4.2,
                 "pv_estimate10": 3.0, "pv_estimate90": 5.0},
            ],
        },
    )
    import datetime as dt
    slot = int(dt.datetime(2024, 9, 10, 6, 30, tzinfo=dt.timezone.utc).timestamp())
    est, e10, e90 = coordinator._total_forecast_for_period(slot)
    assert est == pytest.approx(4.2)
    assert e10 == pytest.approx(3.0)
    assert e90 == pytest.approx(5.0)


async def test_total_forecast_matches_slot_datetime_period_start(hass, coordinator):
    """Regression: the base integration stores period_start as datetime objects,
    not ISO strings. The old code did fromisoformat(datetime) -> TypeError ->
    silently zero-filled every forecast column."""
    import datetime as dt

    hass.states.async_set(
        "sensor.solcast_pv_forecast_forecast_today", "10.0",
        {
            "detailedForecast": [
                {"period_start": dt.datetime(2024, 9, 10, 6, 0, tzinfo=dt.timezone.utc),
                 "pv_estimate": 0.5, "pv_estimate10": 0.3, "pv_estimate90": 0.7},
                {"period_start": dt.datetime(2024, 9, 10, 6, 30, tzinfo=dt.timezone.utc),
                 "pv_estimate": 4.2, "pv_estimate10": 3.0, "pv_estimate90": 5.0},
            ],
        },
    )
    slot = int(dt.datetime(2024, 9, 10, 6, 30, tzinfo=dt.timezone.utc).timestamp())
    est, e10, e90 = coordinator._total_forecast_for_period(slot)
    assert est == pytest.approx(4.2)
    assert e10 == pytest.approx(3.0)
    assert e90 == pytest.approx(5.0)


async def test_total_forecast_missing_returns_zeros(hass, coordinator):
    assert coordinator._total_forecast_for_period(1_000_000) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# azimuth seed + configured site ids
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site,expected", [
    ({"compass_degrees": 67.5}, 67.5),     # ENE
    ({"compass_degrees": 337.5}, -22.5),   # NNW wraps to ±180
    ({"compass_degrees": 90.0}, 90.0),     # due east
    ({"azimuth": -67.5}, 67.5),            # derived from raw Solcast azimuth
])
def test_site_azimuth_seed(site, expected):
    assert SolcastEnhancedCoordinator._site_azimuth_seed(site, {}) == pytest.approx(expected)


def test_site_azimuth_seed_default_when_missing():
    from custom_components.solcast_solar_enhanced.const import CONF_AZIMUTH
    assert SolcastEnhancedCoordinator._site_azimuth_seed({}, {CONF_AZIMUTH: 12.0}) == 12.0


def test_configured_site_ids_dedup():
    groups = [
        {"ac_sensor": "a", "strings": [{"site": "A"}, {"site": "B"}]},
        {"ac_sensor": "b", "site": "C"},
        {"ac_sensor": "c", "site": "A"},  # duplicate
    ]
    assert SolcastEnhancedCoordinator._configured_site_ids(groups) == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# config-flow group derivation round-trip
# ---------------------------------------------------------------------------

def test_derive_groups_fronius_plus_enphase():
    assignments = {
        "A": {"ac": "sensor.fronius_ac", "dc": "sensor.mppt1", "mode": "auto"},
        "B": {"ac": "sensor.fronius_ac", "dc": "sensor.mppt2", "mode": "auto"},
        "C": {"ac": "sensor.enphase_c", "dc": None, "mode": "power_w"},
    }
    groups = _derive_groups(assignments)
    # round-trips losslessly back to the assignments
    assert _groups_to_assignments(groups) == assignments
    fronius = next(g for g in groups if g["ac_sensor"] == "sensor.fronius_ac")
    assert {s["site"] for s in fronius["strings"]} == {"A", "B"}
    enphase = next(g for g in groups if g["ac_sensor"] == "sensor.enphase_c")
    assert enphase["site"] == "C" and enphase["ac_mode"] == "power_w"


def test_derive_groups_shared_ac_no_dc_omitted():
    """Two sites sharing an AC sensor with no DC sensors cannot be split → omitted."""
    assignments = {
        "A": {"ac": "sensor.shared", "dc": None, "mode": "auto"},
        "B": {"ac": "sensor.shared", "dc": None, "mode": "auto"},
    }
    assert _derive_groups(assignments) == []


def test_derive_groups_blank_assignment_ignored():
    assert _derive_groups({"A": {"ac": None, "dc": None, "mode": "auto"}}) == []


# ---------------------------------------------------------------------------
# base auto_dampen guard
# ---------------------------------------------------------------------------

async def test_read_base_auto_dampen_false_when_absent(hass, coordinator):
    assert coordinator._read_base_auto_dampen() is False


async def test_run_dampening_skips_push_when_base_auto_dampen(hass, coordinator, monkeypatch):
    """When the base has auto_dampen on, no set_dampening call is made."""
    monkeypatch.setattr(coordinator, "_read_base_auto_dampen", lambda: True)

    async def _fake_slots(*a, **k):
        return [{"factor": 1.0} for _ in range(48)]

    monkeypatch.setattr(coordinator, "_compute_dampening_slots", _fake_slots)
    pushed = []
    monkeypatch.setattr(coordinator, "_push_dampening",
                        lambda *a, **k: pushed.append((a, k)))

    await coordinator._run_dampening({}, 1_000_000, -37.9, 145.0)
    assert pushed == []  # push skipped
    assert coordinator._auto_dampen_warned is True


# ---------------------------------------------------------------------------
# _resolve_input_mode — unit-first auto-detection
# ---------------------------------------------------------------------------

async def test_energy_counter_without_state_class_detected_as_energy(hass, coordinator):
    """A kWh counter missing state_class must take the energy (delta) path, not be
    misread as instantaneous kW."""
    coordinator._energy_baselines["pv"] = {"value": 10.0, "epoch": 1_000_000}
    hass.states.async_set("sensor.e", "11.0", {"unit_of_measurement": "kWh"})  # no state_class
    val, start = coordinator._read_pv_value("sensor.e", "auto", "pv", 1_000_000 + 1800)
    assert val == pytest.approx(2.0)   # 1 kWh / 0.5 h → 2 kW via delta, not raw 11.0
    assert start == 1_000_000


async def test_mwh_counter_without_state_class_detected_as_energy(hass, coordinator):
    coordinator._energy_baselines["pv"] = {"value": 10.0, "epoch": 1_000_000}  # kWh baseline
    hass.states.async_set("sensor.e", "0.011", {"unit_of_measurement": "MWh"})  # 11 kWh
    val, _ = coordinator._read_pv_value("sensor.e", "auto", "pv", 1_000_000 + 1800)
    assert val == pytest.approx(2.0)


def test_resolve_input_mode_unit_first():
    """Unit decides energy vs power; state_class is only a fallback."""
    def _state(unit, state_class=None):
        attrs = {"unit_of_measurement": unit}
        if state_class:
            attrs["state_class"] = state_class
        return type("S", (), {"attributes": attrs})()

    rm = SolcastEnhancedCoordinator._resolve_input_mode
    assert rm(_state("kWh"), "auto") == "energy_kwh"          # no state_class → still energy
    assert rm(_state("Wh"), "auto") == "energy_wh"
    assert rm(_state("MWh"), "auto") == "energy_mwh"
    assert rm(_state("W"), "auto") == "power_w"
    assert rm(_state("kW"), "auto") == "power_kw"
    assert rm(_state("", "total_increasing"), "auto") == "energy_kwh"  # unit missing → state_class
    assert rm(_state(""), "auto") == "power_kw"                        # nothing → power default
    assert rm(_state("kWh"), "energy_wh") == "energy_wh"               # explicit override wins
