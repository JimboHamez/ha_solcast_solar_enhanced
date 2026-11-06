"""Export-hold step counts wired into the update cycle, and the "updates too slowly" repair issue."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.solcast_solar_enhanced.const import (
    CONF_GENERATION_POWER_SENSOR,
    CONF_GRID_POWER_SENSOR,
    DEFAULT_SITE_ID,
    DOMAIN,
    ISSUE_STEP_INPUT_SLOW,
    STEP_INPUT_CHECK_SLOTS,
    GateFlag,
)
from custom_components.solcast_solar_enhanced.step_measures import StepMeasures

from .test_slot_gate_wiring import _coordinator, _dc_groups, _update, base_with_forecast  # noqa: F401

GRID = "sensor.grid_power"
GEN = "sensor.inverter_ac_power"


def _held_slot(start: float, end: float, *, gen_sensor_kw: bool = False) -> dict[str, list[tuple[float, float]]]:
    """A slot under a hold: a load step at +600 s lifts generation 1.5 kW, net stays at 0 W.

    The trackers' voltage falls 3% as current rises, so the step is also regulated.
    """

    def ser(before: float, after: float) -> list[tuple[float, float]]:
        return [(float(t), before if t - start < 600 else after) for t in range(int(start), int(end))]

    out = {
        "sensor.v1": ser(440.0, 427.0),
        "sensor.i1": ser(2.0, 3.8),
        "sensor.v2": ser(438.0, 425.0),
        "sensor.i2": ser(2.0, 3.8),
        "sensor.export_power": [(float(start), 0.0)],
        GRID: ser(0.0, 0.0),
    }
    if gen_sensor_kw:
        out[GEN] = ser(1.7, 3.2)
    return out


def _total(coord) -> dict[str, Any]:
    return next(r for r in coord._db.records if r["site"] == DEFAULT_SITE_ID)


async def test_steps_land_on_the_total_row_from_dc(hass, base_with_forecast) -> None:
    hass.states.async_set(GRID, "0", {"unit_of_measurement": "W"})
    coord = _coordinator(hass, _dc_groups(), **{CONF_GRID_POWER_SENSOR: GRID})
    await _update(coord, _held_slot)
    total = _total(coord)
    assert (total["gen_steps"], total["held_steps"], total["regulated_steps"], total["cut_steps"]) == (1, 1, 1, 0)
    flags = GateFlag(int(total["gate_flags"]))
    assert GateFlag.NET_SERIES in flags and GateFlag.GEN_SENSOR not in flags
    assert "gen_steps" not in next(r for r in coord._db.records if r["site"] == "A")
    latest = coord.diagnostics_snapshot()["slot_gate"]["latest"]
    assert latest["held_steps"] == 1 and latest["step_input_gap_s"]["grid"] == pytest.approx(1.0)


async def test_the_generation_sensor_is_preferred_over_dc(hass, base_with_forecast) -> None:
    hass.states.async_set(GRID, "0", {"unit_of_measurement": "W"})
    hass.states.async_set(GEN, "1.7", {"unit_of_measurement": "kW"})
    coord = _coordinator(hass, _dc_groups(), **{CONF_GRID_POWER_SENSOR: GRID, CONF_GENERATION_POWER_SENSOR: GEN})

    def slot(start: float, end: float) -> dict[str, list[tuple[float, float]]]:
        data = _held_slot(start, end, gen_sensor_kw=True)
        for key in ("sensor.i1", "sensor.i2"):  # DC flat: only the sensor shows the step.
            data[key] = [(t, 2.0) for t, _ in data[key]]
        return data

    await _update(coord, slot)
    total = _total(coord)
    assert (total["gen_steps"], total["held_steps"]) == (1, 1)
    assert GateFlag.GEN_SENSOR in GateFlag(int(total["gate_flags"]))


async def test_an_energy_counter_grid_input_yields_no_steps(hass, base_with_forecast) -> None:
    hass.states.async_set(GRID, "1234.5", {"unit_of_measurement": "kWh"})
    coord = _coordinator(hass, _dc_groups(), **{CONF_GRID_POWER_SENSOR: GRID})
    await _update(coord, _held_slot)
    total = _total(coord)
    assert total["gen_steps"] == -1 and total["held_steps"] == -1
    assert GateFlag.NET_SERIES not in GateFlag(int(total["gate_flags"]))


async def test_without_a_grid_input_the_steps_stay_unknown(hass, base_with_forecast) -> None:
    coord = _coordinator(hass, _dc_groups())
    await _update(coord, _held_slot)
    total = _total(coord)
    assert (total["gen_steps"], total["held_steps"], total["regulated_steps"], total["cut_steps"]) == (-1, -1, -1, -1)


async def test_the_query_asks_for_the_step_inputs(hass) -> None:
    coord = _coordinator(hass, _dc_groups(), **{CONF_GRID_POWER_SENSOR: GRID, CONF_GENERATION_POWER_SENSOR: GEN})
    assert {GRID, GEN} <= coord._gate_entities({**coord.config_entry.data, **coord.config_entry.options})


# --- the repair issue ------------------------------------------------------------------


def _steps(net_gap: float | None, gen_gap: float | None = None) -> StepMeasures:
    return {
        "gen_steps": 0,
        "held_steps": 0,
        "regulated_steps": -1,
        "cut_steps": -1,
        "net_gap_s": net_gap,
        "gen_gap_s": gen_gap,
    }


def _issue(hass, key: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_STEP_INPUT_SLOW}_{key}")


async def test_a_slow_grid_input_raises_the_issue_only_once_enough_slots_agree(hass) -> None:
    coord = _coordinator(hass, _dc_groups(), **{CONF_GRID_POWER_SENSOR: GRID})
    opts = {CONF_GRID_POWER_SENSOR: GRID}
    for _ in range(STEP_INPUT_CHECK_SLOTS - 1):
        coord._check_step_inputs(opts, _steps(300.0), gen_from_sensor=False)
    assert _issue(hass, CONF_GRID_POWER_SENSOR) is None
    coord._check_step_inputs(opts, _steps(300.0), gen_from_sensor=False)
    issue = _issue(hass, CONF_GRID_POWER_SENSOR)
    assert issue is not None
    assert issue.translation_key == ISSUE_STEP_INPUT_SLOW
    assert issue.translation_placeholders == {"entity": GRID, "interval": "300"}
    for _ in range(STEP_INPUT_CHECK_SLOTS):
        coord._check_step_inputs(opts, _steps(1.0), gen_from_sensor=False)
    assert _issue(hass, CONF_GRID_POWER_SENSOR) is None


async def test_a_quiet_slot_neither_raises_nor_clears(hass) -> None:
    coord = _coordinator(hass, _dc_groups(), **{CONF_GRID_POWER_SENSOR: GRID})
    opts = {CONF_GRID_POWER_SENSOR: GRID}
    for _ in range(STEP_INPUT_CHECK_SLOTS):
        coord._check_step_inputs(opts, _steps(300.0), gen_from_sensor=False)
    for _ in range(3 * STEP_INPUT_CHECK_SLOTS):
        coord._check_step_inputs(opts, _steps(None), gen_from_sensor=False)
    assert _issue(hass, CONF_GRID_POWER_SENSOR) is not None


async def test_removing_the_input_clears_its_issue(hass) -> None:
    coord = _coordinator(hass, _dc_groups(), **{CONF_GRID_POWER_SENSOR: GRID})
    for _ in range(STEP_INPUT_CHECK_SLOTS):
        coord._check_step_inputs({CONF_GRID_POWER_SENSOR: GRID}, _steps(300.0), gen_from_sensor=False)
    coord._check_step_inputs({CONF_GRID_POWER_SENSOR: None}, _steps(None), gen_from_sensor=False)
    assert _issue(hass, CONF_GRID_POWER_SENSOR) is None


async def test_the_generation_input_is_judged_only_when_it_was_used(hass) -> None:
    """Generation from DC: the configured-but-unused sensor's cadence is not the problem."""
    coord = _coordinator(hass, _dc_groups())
    opts = {CONF_GENERATION_POWER_SENSOR: GEN}
    for _ in range(STEP_INPUT_CHECK_SLOTS):
        coord._check_step_inputs(opts, _steps(None, 300.0), gen_from_sensor=False)
    assert _issue(hass, CONF_GENERATION_POWER_SENSOR) is None
    for _ in range(STEP_INPUT_CHECK_SLOTS):
        coord._check_step_inputs(opts, _steps(None, 300.0), gen_from_sensor=True)
    assert _issue(hass, CONF_GENERATION_POWER_SENSOR) is not None


async def test_the_issue_is_translated() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / DOMAIN
    for path in [root / "strings.json", *(root / "translations").glob("*.json")]:
        text = json.loads(path.read_text(encoding="utf-8"))["issues"][ISSUE_STEP_INPUT_SLOW]
        assert "{entity}" in text["title"] and "{interval}" in text["description"], path.name
