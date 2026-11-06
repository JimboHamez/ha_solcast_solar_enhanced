"""Suggest input entities for a first-time setup from what Home Assistant already knows.

Two sources, in order of trust:

1. **The Energy dashboard preferences.** The user has already said which sensor is
   grid import, export and solar production there, and Home Assistant normalises the
   grid power sensor to "positive while importing" whatever its native polarity. Read
   through a private API (``homeassistant.components.energy.data``), so every access is
   defensive and any surprise yields no suggestion rather than an error.
2. **Core integrations' own entities**, matched on unique id, for the DC tracker
   sensors the Energy dashboard has no field for (Fronius only so far).

Entity names are never used: entity ids follow the install's language (issue #41) and
users rename them freely, so a name rule would make confident wrong guesses.

A suggestion only ever pre-fills an empty field on a first-time setup
(``suggested_value``, not ``default``), so a wrong one costs the user one click and
clearing the field leaves it empty.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_GENERATION_POWER_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_MPPT1_CURRENT_SENSOR,
    CONF_MPPT1_VOLTAGE_SENSOR,
    CONF_MPPT2_CURRENT_SENSOR,
    CONF_MPPT2_VOLTAGE_SENSOR,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_SENSOR,
)
from .models import InputSuggestions

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

ENERGY_DOMAIN = "energy"
FRONIUS_DOMAIN = "fronius"
UTILITY_METER_DOMAIN = "utility_meter"
# The option a UI-created utility meter keeps its source sensor under.
UTILITY_METER_SOURCE = "source"
# A utility meter may meter another one; follow at most this many hops.
_MAX_METER_HOPS = 3
# Fronius inverter sensors are ``<inverter uid>-<key>``. Trackers 2-4 share one
# translation key, so the key in the unique id is what tells them apart.
_FRONIUS_DC_KEYS: dict[str, str] = {
    "voltage_dc": CONF_MPPT1_VOLTAGE_SENSOR,
    "current_dc": CONF_MPPT1_CURRENT_SENSOR,
    "voltage_dc_2": CONF_MPPT2_VOLTAGE_SENSOR,
    "current_dc_2": CONF_MPPT2_CURRENT_SENSOR,
}

Resolver = Callable[[str], str | None]


def _single(candidates: Iterable[str | None]) -> str | None:
    """The one distinct candidate, or ``None`` if there are none, several, or any is unresolved.

    One unresolved candidate refuses the lot: if one of two solar counters cannot be
    traced, the other cannot be assumed to be the whole property.
    """
    values = list(candidates)
    if not values or any(v is None for v in values):
        return None
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else None


def _sources(prefs: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    sources = prefs.get("energy_sources")
    if not isinstance(sources, list):
        return []
    return [s for s in sources if isinstance(s, Mapping) and s.get("type") == kind]


def _stat_ids(sources: Iterable[Mapping[str, Any]], key: str, legacy_list: str | None = None) -> list[str]:
    """Every non-empty ``key`` on the sources, including the pre-2026 grid lists."""
    out: list[str] = []
    for source in sources:
        value = source.get(key)
        if isinstance(value, str) and value:
            out.append(value)
        if legacy_list and isinstance(items := source.get(legacy_list), list):
            out.extend(i[key] for i in items if isinstance(i, Mapping) and isinstance(i.get(key), str) and i[key])
    return out


def suggest_from_energy(
    prefs: Mapping[str, Any], resolve_counter: Resolver, resolve_power: Resolver
) -> InputSuggestions:
    """Map Energy dashboard preferences onto config keys.

    Args:
        prefs: The Energy manager's ``data`` (``EnergyPreferences``).
        resolve_counter: Turns an energy statistic id into the entity to read,
            tracing a utility meter back to its source; ``None`` when it cannot.
        resolve_power: Checks a power statistic id is an entity; ``None`` otherwise.

    Returns:
        A suggestion for each input with exactly one candidate. Tariff-split
        utility meters that trace back to one source count as one candidate.
    """
    solar = _sources(prefs, "solar")
    grid = _sources(prefs, "grid")
    candidates: dict[str, str | None] = {
        CONF_PV_ACTUAL_SENSOR: _single(resolve_counter(s) for s in _stat_ids(solar, "stat_energy_from")),
        CONF_PV_EXPORT_SENSOR: _single(resolve_counter(s) for s in _stat_ids(grid, "stat_energy_to", "flow_to")),
        CONF_GRID_POWER_SENSOR: _single(resolve_power(s) for s in _stat_ids(grid, "stat_rate", "power")),
        CONF_GENERATION_POWER_SENSOR: _single(resolve_power(s) for s in _stat_ids(solar, "stat_rate")),
    }
    return cast("InputSuggestions", {k: v for k, v in candidates.items() if v})


def suggest_fronius_trackers(entries: Iterable[er.RegistryEntry]) -> InputSuggestions:
    """MPPT 1/2 sensors of the property's Fronius inverter, if there is exactly one.

    A tracker is suggested only with both its voltage and current, and nothing is
    suggested when several inverters report DC data, since which feeds which array is
    then a guess.
    """
    inverters: dict[str, dict[str, str]] = {}
    for entry in entries:
        if entry.platform != FRONIUS_DOMAIN or entry.domain != "sensor" or entry.disabled_by:
            continue
        prefix, _, key = entry.unique_id.rpartition("-")
        if key in _FRONIUS_DC_KEYS and prefix and not prefix.endswith("-modbus"):
            inverters.setdefault(prefix, {})[_FRONIUS_DC_KEYS[key]] = entry.entity_id
    if len(inverters) != 1:
        return InputSuggestions()
    found = next(iter(inverters.values()))
    out: dict[str, str] = {}
    for voltage, current in (
        (CONF_MPPT1_VOLTAGE_SENSOR, CONF_MPPT1_CURRENT_SENSOR),
        (CONF_MPPT2_VOLTAGE_SENSOR, CONF_MPPT2_CURRENT_SENSOR),
    ):
        if voltage in found and current in found:
            out[voltage] = found[voltage]
            out[current] = found[current]
    return cast("InputSuggestions", out)


def _resolve_power(hass: HomeAssistant, registry: er.EntityRegistry, stat_id: str) -> str | None:
    """The entity behind a statistic id, or ``None`` for an external statistic or a missing entity."""
    if ":" in stat_id:
        return None
    if registry.async_get(stat_id) is None and hass.states.get(stat_id) is None:
        return None
    return stat_id


def _resolve_counter(hass: HomeAssistant, registry: er.EntityRegistry, stat_id: str) -> str | None:
    """The cumulative counter to read for an energy statistic id.

    A UI-created utility meter is traced back to its source sensor: a tariff or daily
    meter counts only part of the time, so it is never itself a valid input. A YAML
    meter has no config entry to trace, so it yields ``None``.
    """
    current = stat_id
    for _ in range(_MAX_METER_HOPS + 1):
        if _resolve_power(hass, registry, current) is None:
            return None
        entry = registry.async_get(current)
        if entry is None or entry.platform != UTILITY_METER_DOMAIN:
            return current
        config_entry = hass.config_entries.async_get_entry(entry.config_entry_id) if entry.config_entry_id else None
        source = config_entry.options.get(UTILITY_METER_SOURCE) if config_entry else None
        if not isinstance(source, str) or not source:
            return None
        resolved = er.async_resolve_entity_id(registry, source)
        if resolved is None:
            return None
        current = resolved
    return None


async def async_read_energy_prefs(hass: HomeAssistant) -> Mapping[str, Any] | None:
    """The Energy dashboard preferences, or ``None`` when they are unavailable."""
    if ENERGY_DOMAIN not in hass.config.components:
        return None
    try:
        from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415

        manager = await async_get_manager(hass)
        data = getattr(manager, "data", None)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Energy preferences unavailable: %s", exc)
        return None
    return data if isinstance(data, Mapping) else None


async def async_suggest_inputs(hass: HomeAssistant) -> InputSuggestions:
    """Suggestions for a first-time setup; empty when nothing is known with confidence.

    Never raises: a suggestion is a convenience, and failing to make one must not stop
    the user setting the integration up.
    """
    try:
        registry = er.async_get(hass)
        out = suggest_fronius_trackers(registry.entities.values())
        if prefs := await async_read_energy_prefs(hass):
            out.update(
                suggest_from_energy(
                    prefs,
                    lambda s: _resolve_counter(hass, registry, s),
                    lambda s: _resolve_power(hass, registry, s),
                )
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Input suggestions failed: %s", exc)
        return InputSuggestions()
    return out
