"""Inverter-side curtailment capture: the record-only columns behind issue #109.

The off-MPP tests are shaped on guiguan's 19 Oct 5 s trace. Each one is built so
that a plausible wrong implementation fails it: reducing voltage and current
separately (the 13:00 clipping + cloud slot), testing any tracker instead of every
tracker, counting MPPT scans, or judging a fully curtailed slot against its own
median.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced.config_flow import _build_site_schema
from custom_components.solcast_solar_enhanced.const import (
    CONF_AUTO_DAMPENING,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_CAPACITY_KW,
    CONF_DB_ENABLED,
    CONF_EXPORT_LIMIT_KW,
    CONF_GRID_VOLTAGE_SENSOR,
    CONF_INVERTER_LIMITED_SENSOR,
    CONF_INVERTER_TEMP_SENSOR,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MPPT1_CURRENT_SENSOR,
    CONF_MPPT1_VOLTAGE_SENSOR,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
    CONF_SITE_GROUPS,
    CONF_TILT,
    CURTAILMENT_UNKNOWN,
    DEFAULT_SITE_ID,
    DOMAIN,
    OFFMPP_REF_MAX_AGE_S,
)
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.sqlite_store import SqliteStore

SLOT = 1800
# Healthy operating point and the curtailed one from the 19 Oct trace (North string).
VMP, IMP = 280.0, 11.0
V_PARKED, I_PARKED = 328.0, 0.8

TRACKERS = [
    {"voltage_sensor": f"sensor.mppt{n}_v", "current_sensor": f"sensor.mppt{n}_a"} for n in (1, 2, 3)
]


@pytest.fixture
def coordinator(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, mock_config_entry)


def _series(segments: list[tuple[float, float]], start: float = 0.0, step: float = 10.0) -> list[tuple[float, float]]:
    """Recorder samples every ``step`` seconds through ``[(duration_s, value), ...]``."""
    out: list[tuple[float, float]] = []
    t = start
    for duration, value in segments:
        end = t + duration
        while t < end - 1e-9:
            out.append((t, value))
            t += step
    return out


def _tracker_samples(
    v_segments: list[tuple[float, float]],
    i_segments: list[tuple[float, float]],
    trackers: list[dict] = TRACKERS,
    start: float = 0.0,
) -> dict[str, list[tuple[float, float]]]:
    """The same voltage and current trace on every tracker."""
    out: dict[str, list[tuple[float, float]]] = {}
    for m in trackers:
        out[m["voltage_sensor"]] = _series(v_segments, start)
        out[m["current_sensor"]] = _series(i_segments, start)
    return out


def _curtailed(before: float, during: float, start: float = 0.0) -> dict[str, list[tuple[float, float]]]:
    after = SLOT - before - during
    return _tracker_samples(
        [(before, VMP), (during, V_PARKED), (after, VMP)],
        [(before, IMP), (during, I_PARKED), (after, IMP)],
        start=start,
    )


# ---------------------------------------------------------------------------
# _offmpp_minutes
# ---------------------------------------------------------------------------


def test_offmpp_counts_the_time_every_tracker_was_parked(coordinator):
    """The 19 Oct shape: 14 minutes parked near open circuit on all three trackers."""
    assert coordinator._offmpp_minutes(TRACKERS, _curtailed(600, 840), 0, SLOT) == pytest.approx(14.0)


def test_offmpp_ignores_cloud(coordinator):
    """Cloud drops the current at a held voltage, so the voltage test never passes."""
    samples = _tracker_samples([(SLOT, VMP)], [(600, IMP), (900, 2.0), (300, IMP)])
    assert coordinator._offmpp_minutes(TRACKERS, samples, 0, SLOT) == 0.0


def test_offmpp_ignores_clipping(coordinator):
    """Clipping raises the voltage at a held current, so the current test never passes."""
    samples = _tracker_samples([(600, VMP), (300, 306.0), (900, VMP)], [(SLOT, IMP)])
    assert coordinator._offmpp_minutes(TRACKERS, samples, 0, SLOT) == 0.0


def test_offmpp_does_not_pair_extremes_from_different_moments(coordinator):
    """The 19 Oct 13:00 slot: a clipping minute supplied the high voltage, a cloud
    stretch the low current. Reducing the two separately (the slot's max V and min I)
    reads that as curtailment; judging them in the same sample does not."""
    v = [(300, 272.0), (60, 306.0), (1440, 272.0)]
    i = [(300, 13.7), (60, 13.6), (240, 13.7), (600, 2.4), (600, 13.7)]
    samples = _tracker_samples(v, i)
    # Guard: the separate max/min reduction does flag this slot.
    volts = [x for _, x in samples["sensor.mppt1_v"]]
    amps = [x for _, x in samples["sensor.mppt1_a"]]
    assert max(volts) > 1.08 * statistics.median(volts)
    assert min(amps) < 0.3 * statistics.median(amps)
    assert coordinator._offmpp_minutes(TRACKERS, samples, 0, SLOT) == 0.0


def test_offmpp_needs_every_tracker_at_once(coordinator):
    """One parked tracker is a string-level event, not the inverter holding back."""
    samples = _curtailed(600, 840)
    healthy = _tracker_samples([(SLOT, VMP)], [(SLOT, IMP)])
    for key in ("sensor.mppt2_v", "sensor.mppt2_a"):
        samples[key] = healthy[key]
    assert coordinator._offmpp_minutes(TRACKERS, samples, 0, SLOT) == 0.0


def test_offmpp_drops_runs_shorter_than_a_minute(hass, mock_config_entry):
    """MPPT scan sweeps visit open circuit for seconds; a 50 s run is dropped, 70 s kept."""
    mock_config_entry.add_to_hass(hass)
    short = SolcastEnhancedCoordinator(hass, mock_config_entry)
    assert short._offmpp_minutes(TRACKERS, _curtailed(600, 50), 0, SLOT) == 0.0
    long_ = SolcastEnhancedCoordinator(hass, mock_config_entry)
    assert long_._offmpp_minutes(TRACKERS, _curtailed(600, 70), 0, SLOT) == pytest.approx(70 / 60, abs=0.01)


def test_offmpp_fully_curtailed_slot_is_judged_against_the_last_healthy_slot(coordinator):
    """A slot curtailed throughout has the parked point as its own median, so on its
    own it reads 0. After a healthy slot, the remembered operating point catches it."""
    parked = _tracker_samples([(SLOT, V_PARKED)], [(SLOT, I_PARKED)], start=SLOT)
    assert coordinator._offmpp_minutes(TRACKERS, parked, SLOT, 2 * SLOT) == 0.0

    healthy = _tracker_samples([(SLOT, VMP)], [(SLOT, IMP)])
    assert coordinator._offmpp_minutes(TRACKERS, healthy, 0, SLOT) == 0.0
    assert coordinator._offmpp_minutes(TRACKERS, parked, SLOT, 2 * SLOT) == pytest.approx(30.0)


def test_offmpp_curtailed_slot_does_not_replace_the_reference(coordinator):
    """Two fully curtailed slots in a row: the second must still be judged against the
    healthy slot, not against the parked medians of the first."""
    coordinator._offmpp_minutes(TRACKERS, _tracker_samples([(SLOT, VMP)], [(SLOT, IMP)]), 0, SLOT)
    for n in (1, 2):
        parked = _tracker_samples([(SLOT, V_PARKED)], [(SLOT, I_PARKED)], start=n * SLOT)
        assert coordinator._offmpp_minutes(TRACKERS, parked, n * SLOT, (n + 1) * SLOT) == pytest.approx(30.0)


def test_offmpp_reference_expires(coordinator):
    """Yesterday's midday operating point must not judge this morning's start-up."""
    coordinator._offmpp_minutes(TRACKERS, _tracker_samples([(SLOT, VMP)], [(SLOT, IMP)]), 0, SLOT)
    start = SLOT + OFFMPP_REF_MAX_AGE_S
    parked = _tracker_samples([(SLOT, V_PARKED)], [(SLOT, I_PARKED)], start=start)
    assert coordinator._offmpp_minutes(TRACKERS, parked, start, start + SLOT) == 0.0


def test_offmpp_unknown_without_history(coordinator):
    samples = _curtailed(600, 840)
    del samples["sensor.mppt3_a"]
    assert coordinator._offmpp_minutes(TRACKERS, samples, 0, SLOT) is None


def test_offmpp_zero_when_not_generating(coordinator):
    samples = _tracker_samples([(SLOT, 0.0)], [(SLOT, 0.0)])
    assert coordinator._offmpp_minutes(TRACKERS, samples, 0, SLOT) == 0.0


# ---------------------------------------------------------------------------
# _offmpp_tracker_sets
# ---------------------------------------------------------------------------


def _m(n: int, current: bool = True) -> dict:
    return {"voltage_sensor": f"sensor.v{n}", "current_sensor": f"sensor.a{n}" if current else None}


def test_tracker_sets_dc_split_group_is_one_inverter():
    """guiguan's layout: one inverter, three apportioned strings of one tracker each.
    All three trackers are judged together and the result feeds all three arrays."""
    group = {
        "ac_sensor": "sensor.inv_ac",
        "strings": [
            {"site": "east", "mppts": [_m(1)]},
            {"site": "north", "mppts": [_m(2)]},
            {"site": "west", "mppts": [_m(3), _m(4, current=False)]},
        ],
    }
    sets = SolcastEnhancedCoordinator._offmpp_tracker_sets({CONF_SITE_GROUPS: [group]})
    assert sets == [([_m(1), _m(2), _m(3)], ["east", "north", "west"])]


def test_tracker_sets_direct_groups_are_separate_inverters():
    groups = [
        {"ac_sensor": "sensor.ac_a", "site": "A", "mppts": [_m(1), _m(2)]},
        {"ac_sensor": "sensor.ac_b", "site": "B", "mppts": [_m(3)]},
    ]
    sets = SolcastEnhancedCoordinator._offmpp_tracker_sets({CONF_SITE_GROUPS: groups})
    assert sets == [([_m(1), _m(2)], ["A"]), ([_m(3)], ["B"])]


def test_tracker_sets_flat_keys_feed_a_group_without_trackers():
    opts = {
        CONF_MPPT1_VOLTAGE_SENSOR: "sensor.v1",
        CONF_MPPT1_CURRENT_SENSOR: "sensor.a1",
        CONF_SITE_GROUPS: [{"ac_sensor": "sensor.ac", "site": "only"}],
    }
    sets = SolcastEnhancedCoordinator._offmpp_tracker_sets(opts)
    assert sets == [([_m(1)], ["only"])]


# ---------------------------------------------------------------------------
# _interval_on_fraction
# ---------------------------------------------------------------------------


def _state(ts: float, state: str) -> SimpleNamespace:
    return SimpleNamespace(state=state, last_updated_timestamp=ts)


async def test_on_fraction_is_time_weighted_over_known_time(hass, coordinator):
    """On for 300 s; 300 s unavailable is left out, so 300 of 1500 known seconds."""
    states = {
        "binary_sensor.limited": [
            _state(-100.0, "off"),  # Held from before the slot: clamped to its start.
            _state(600.0, "on"),
            _state(900.0, "unavailable"),
            _state(1200.0, "off"),
        ]
    }

    async def _raw(entity_ids, start, end):
        return states

    with patch.object(coordinator, "_recorder_states", side_effect=_raw):
        assert await coordinator._interval_on_fraction("binary_sensor.limited", 0, SLOT) == pytest.approx(0.2)


async def test_on_fraction_falls_back_to_the_current_state(hass, coordinator):
    hass.states.async_set("binary_sensor.limited", "on")
    assert await coordinator._interval_on_fraction("binary_sensor.limited", 0, SLOT) == 1.0


async def test_on_fraction_unknown_sentinels(hass, coordinator):
    assert await coordinator._interval_on_fraction("", 0, SLOT) == CURTAILMENT_UNKNOWN
    hass.states.async_set("binary_sensor.limited", "unavailable")
    assert await coordinator._interval_on_fraction("binary_sensor.limited", 0, SLOT) == CURTAILMENT_UNKNOWN

    async def _raw(entity_ids, start, end):
        return {"binary_sensor.limited": [_state(0.0, "unavailable")]}

    with patch.object(coordinator, "_recorder_states", side_effect=_raw):
        assert await coordinator._interval_on_fraction("binary_sensor.limited", 0, SLOT) == CURTAILMENT_UNKNOWN


# ---------------------------------------------------------------------------
# _do_update — the columns land on every row
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


_CONFIG = {
    CONF_LATITUDE: -37.9,
    CONF_LONGITUDE: 145.0,
    CONF_CAPACITY_KW: 10.0,
    CONF_TILT: 22.0,
    CONF_AZIMUTH: 0.0,
    CONF_PV_ACTUAL_SENSOR: "sensor.pv_power_30min",
    CONF_PV_EXPORT_SENSOR: "sensor.pv_export_30min",
    CONF_AUTO_TUNING: False,
    CONF_AUTO_DAMPENING: False,
}


def _coordinator(hass, options: dict) -> SolcastEnhancedCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data=_CONFIG, options=options, entry_id="curtail", title="curtail")
    entry.add_to_hass(hass)
    coord = SolcastEnhancedCoordinator(hass, entry)
    coord._db = _FakeStore()
    hass.states.async_set("sensor.pv_power_30min", "3.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.pv_export_30min", "1.2", {"unit_of_measurement": "kW"})
    return coord


async def test_do_update_stores_the_curtailment_columns(hass, mock_base_coordinator):
    """Grid voltage, temperature, limited share and the export limit are property-wide
    and replicated; the off-MPP minutes come from the inverter each array feeds, so an
    array on an inverter without DC sensors reads unknown."""
    groups = [
        {"ac_sensor": "sensor.ac_a", "site": "A", "mppts": TRACKERS[:2]},
        {"ac_sensor": "sensor.ac_b", "site": "B"},
    ]
    coord = _coordinator(
        hass,
        {
            CONF_DB_ENABLED: True,
            CONF_SITE_GROUPS: groups,
            CONF_EXPORT_LIMIT_KW: 5.0,
            CONF_GRID_VOLTAGE_SENSOR: "sensor.grid_v",
            CONF_INVERTER_TEMP_SENSOR: "sensor.inv_temp",
            CONF_INVERTER_LIMITED_SENSOR: "binary_sensor.limited",
        },
    )
    hass.states.async_set("sensor.ac_a", "2.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.ac_b", "1.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("binary_sensor.limited", "on")  # No recorder: the current state stands.

    async def _samples(entity_ids, start, end):
        assert {"sensor.grid_v", "sensor.inv_temp"} <= set(entity_ids)
        dc = _curtailed(600, 840, start=start)
        dc = {k: [(t, x) for t, x in v] for k, v in dc.items() if k in entity_ids}
        return {
            **dc,
            "sensor.grid_v": [(float(start), 248.0), (start + 600.0, 257.5), (start + 900.0, 249.0)],
            "sensor.inv_temp": [(float(start), 51.0), (start + 600.0, 57.2)],
        }

    with patch.object(coord, "_interval_samples", side_effect=_samples):
        await coord._do_update()

    rows = {r["site"]: r for r in coord._db.records}
    assert set(rows) == {DEFAULT_SITE_ID, "A", "B"}
    for row in rows.values():
        assert row["grid_v_max"] == pytest.approx(257.5)
        assert row["grid_v_med"] == pytest.approx(249.0)
        assert row["inverter_temp_max"] == pytest.approx(57.2)
        assert row["inverter_limited"] == 1.0
        assert row["export_limit"] == pytest.approx(5.0)
    assert rows[DEFAULT_SITE_ID]["dc_offmpp_min"] == pytest.approx(14.0)
    assert rows["A"]["dc_offmpp_min"] == pytest.approx(14.0)
    assert rows["B"]["dc_offmpp_min"] == CURTAILMENT_UNKNOWN

    snapshot = coord.diagnostics_snapshot()["curtailment"]
    assert snapshot["inputs"][CONF_GRID_VOLTAGE_SENSOR] == "sensor.grid_v"
    assert snapshot["total"]["dc_offmpp_min"] == pytest.approx(14.0)
    assert snapshot["site_offmpp_min"] == {"A": pytest.approx(14.0)}


async def test_do_update_unconfigured_inputs_store_their_sentinels(hass, mock_base_coordinator):
    """Nothing configured and no export limit: 0 for voltage and temperature, -1 for
    the columns whose 0 is a real value."""
    coord = _coordinator(hass, {CONF_DB_ENABLED: True})

    await coord._do_update()

    row = coord._db.records[0]
    assert (row["grid_v_max"], row["grid_v_med"], row["inverter_temp_max"]) == (0.0, 0.0, 0.0)
    assert row["inverter_limited"] == CURTAILMENT_UNKNOWN
    assert row["export_limit"] == CURTAILMENT_UNKNOWN
    assert row["dc_offmpp_min"] == CURTAILMENT_UNKNOWN


# ---------------------------------------------------------------------------
# Store round trip
# ---------------------------------------------------------------------------


async def test_store_round_trips_the_columns_and_defaults_unknown(hass, tmp_path):
    path = str(tmp_path / "c.db")
    store = SqliteStore(hass, path)
    assert await store.async_connect() is True
    base = {
        "period_end": "2026-10-19T00:00:00+00:00",
        "period_start": "2026-10-18T23:30:00+00:00",
        "site": DEFAULT_SITE_ID,
        "pv_actual": 4.2,
        "pv_estimate": 6.7,
        "pv_estimate10": 5.0,
        "pv_estimate90": 7.0,
        "azimuth": 30.0,
        "zenith": 35.0,
        "temp": 20.0,
        "clouds": 0,
        "description": "clear",
    }
    try:
        await store.async_insert_record(
            {
                **base,
                "period_end_epoch": 1000,
                "grid_v_max": 257.5,
                "grid_v_med": 249.0,
                "inverter_temp_max": 57.2,
                "inverter_limited": 0.0,
                "export_limit": 0.0,
                "dc_offmpp_min": 0.0,
            }
        )
        await store.async_insert_record({**base, "period_end_epoch": 2800})
    finally:
        await store.async_close()
    cols = "grid_v_max, grid_v_med, inverter_temp_max, inverter_limited, export_limit, dc_offmpp_min"
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(f"SELECT {cols} FROM solcast_data ORDER BY period_end_epoch").fetchall()
    finally:
        conn.close()
    # A real 0 survives the write; a missing key reads back as unknown, not as 0.
    assert rows[0] == (257.5, 249.0, 57.2, 0.0, 0.0, 0.0)
    assert rows[1] == (0.0, 0.0, 0.0, -1.0, -1.0, -1.0)


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

_FIELDS = (CONF_GRID_VOLTAGE_SENSOR, CONF_INVERTER_TEMP_SENSOR, CONF_INVERTER_LIMITED_SENSOR)


@pytest.mark.parametrize("single_site", [True, False])
def test_site_step_offers_the_curtailment_inputs(single_site):
    """Property-wide inputs, so they show whatever the array topology."""
    keys = {str(k) for k in _build_site_schema({}, single_site=single_site).schema}
    assert set(_FIELDS) <= keys


def test_curtailment_inputs_are_labelled_and_described_in_every_locale():
    root = Path(__file__).resolve().parent.parent / "custom_components" / "solcast_solar_enhanced"
    files = [root / "strings.json", *sorted((root / "translations").glob("*.json"))]
    assert len(files) == 12
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for flow in ("config", "options"):
            site = data[flow]["step"]["site"]
            for field in _FIELDS:
                assert site["data"].get(field), f"{path.name} {flow}: no label for {field}"
                assert site["data_description"].get(field), f"{path.name} {flow}: no description for {field}"
