"""Config flow for Solcast Solar Enhanced — 5-step setup wizard."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

try:
    from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
    _ENTITY_SELECTOR_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ENTITY_SELECTOR_AVAILABLE = False

from .const import (
    CONF_AUTO_DAMPENING,
    CONF_DAMPENING_GATE,
    CONF_AUTO_TUNING,
    CONF_AZIMUTH,
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_ENABLED,
    CONF_BATTERY_MODE,
    CONF_BATTERY_NET_SENSOR,
    CONF_BATTERY_STAT_SENSOR,
    CONF_MPPT1_CURRENT_SENSOR,
    CONF_MPPT1_VOLTAGE_SENSOR,
    CONF_MPPT2_CURRENT_SENSOR,
    CONF_MPPT2_VOLTAGE_SENSOR,
    CONF_CAPACITY_KW,
    CONF_CLIPPING_THRESHOLD,
    CONF_CLOUD_MAX_INCLUDE,
    CONF_KT_THRESHOLD,
    CONF_CLOUD_THRESHOLD,
    CONF_EXPORT_LIMIT_KW,
    CONF_DB_ENABLED,
    CONF_DB_RETENTION_DAYS,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_OPENMETEO_ENABLED,
    CONF_OWM_API_KEY,
    CONF_OWM_ENABLED,
    CONF_PV_ACTUAL_INPUT_MODE,
    CONF_PV_ACTUAL_SENSOR,
    CONF_PV_EXPORT_INPUT_MODE,
    CONF_PV_EXPORT_SENSOR,
    CONF_SITE_GROUPS,
    CONF_TILT,
    DEFAULT_AUTO_DAMPENING,
    DEFAULT_DAMPENING_GATE,
    DEFAULT_AUTO_TUNING,
    DEFAULT_AZIMUTH,
    DEFAULT_CAPACITY_KW,
    DEFAULT_CLIPPING_THRESHOLD,
    DEFAULT_CLOUD_MAX_INCLUDE,
    DEFAULT_KT_THRESHOLD,
    DEFAULT_CLOUD_THRESHOLD,
    DEFAULT_DB_ENABLED,
    DEFAULT_OPENMETEO_ENABLED,
    DEFAULT_DB_RETENTION_DAYS,
    DEFAULT_EXPORT_LIMIT_KW,
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_PV_INPUT_MODE,
    DEFAULT_TILT,
    DOMAIN,
    PV_INPUT_MODES,
)

_LOGGER = logging.getLogger(__name__)


def _entity_selector(domain: str = "sensor") -> Any:
    if _ENTITY_SELECTOR_AVAILABLE:
        try:
            return EntitySelector(EntitySelectorConfig(domain=domain))
        except Exception:  # noqa: BLE001
            pass
    return TextSelector()


def _input_mode_selector() -> Any:
    """Dropdown for how a PV sensor should be interpreted (translated labels)."""
    return SelectSelector(
        SelectSelectorConfig(
            options=PV_INPUT_MODES,
            mode="dropdown",
            translation_key="pv_input_mode",
        )
    )


# Flat per-inverter MPPT keys — entered on Step 1 for single-array systems only.
_FLAT_MPPT_KEYS = (
    CONF_MPPT1_VOLTAGE_SENSOR,
    CONF_MPPT1_CURRENT_SENSOR,
    CONF_MPPT2_VOLTAGE_SENSOR,
    CONF_MPPT2_CURRENT_SENSOR,
)


def _build_site_schema(d: dict[str, Any], *, single_site: bool) -> vol.Schema:
    """Step 1 (Site & System) schema, shared by the config and options flows.

    ``d`` supplies current/suggested values (pass an empty dict for a fresh
    install — every field then falls back to its default). The flat per-inverter
    MPPT voltage/current fields are shown only for single-array systems; a
    multi-array system maps MPPT trackers per array in the per-site step, so they
    are omitted here to avoid duplicate entry.
    """
    fields: dict[Any, Any] = {
        vol.Required(CONF_LATITUDE, default=d.get(CONF_LATITUDE, DEFAULT_LATITUDE)):
            NumberSelector(NumberSelectorConfig(min=-90, max=90, step=0.001)),
        vol.Required(CONF_LONGITUDE, default=d.get(CONF_LONGITUDE, DEFAULT_LONGITUDE)):
            NumberSelector(NumberSelectorConfig(min=-180, max=180, step=0.001)),
        vol.Required(CONF_CAPACITY_KW, default=d.get(CONF_CAPACITY_KW, DEFAULT_CAPACITY_KW)):
            NumberSelector(NumberSelectorConfig(min=0.1, max=1000, step=0.1)),
        vol.Required(CONF_TILT, default=d.get(CONF_TILT, DEFAULT_TILT)):
            NumberSelector(NumberSelectorConfig(min=0, max=90, step=0.1)),
        vol.Required(CONF_AZIMUTH, default=d.get(CONF_AZIMUTH, DEFAULT_AZIMUTH)):
            NumberSelector(NumberSelectorConfig(min=-180, max=180, step=0.1)),
        vol.Optional(CONF_PV_ACTUAL_SENSOR, description={"suggested_value": d.get(CONF_PV_ACTUAL_SENSOR)}):
            _entity_selector(),
        vol.Required(CONF_PV_ACTUAL_INPUT_MODE, default=d.get(CONF_PV_ACTUAL_INPUT_MODE, DEFAULT_PV_INPUT_MODE)):
            _input_mode_selector(),
        vol.Optional(CONF_PV_EXPORT_SENSOR, description={"suggested_value": d.get(CONF_PV_EXPORT_SENSOR)}):
            _entity_selector(),
        vol.Required(CONF_PV_EXPORT_INPUT_MODE, default=d.get(CONF_PV_EXPORT_INPUT_MODE, DEFAULT_PV_INPUT_MODE)):
            _input_mode_selector(),
        vol.Optional(CONF_BATTERY_STAT_SENSOR, description={"suggested_value": d.get(CONF_BATTERY_STAT_SENSOR)}):
            _entity_selector(),
    }
    if single_site:
        for key in _FLAT_MPPT_KEYS:
            fields[vol.Optional(key, description={"suggested_value": d.get(key)})] = _entity_selector()
    return vol.Schema(fields)


# --- Multi-site mapping helpers -------------------------------------------------
# The UI collects, per discovered site, a generation sensor, an optional DC/MPPT
# sensor and an input mode. ``CONF_SITE_GROUPS`` is then *derived* by grouping
# sites that share the same AC sensor (those sharing one are DC-apportioned).

def _fields_to_mppts(
    v1: Any, i1: Any, v2: Any, i2: Any
) -> list[dict[str, Any]]:
    """Form values → compacted ``mppts`` list. A tracker is kept only when it has a
    voltage sensor (the off-MPP signal); current is the optional disambiguator."""
    out: list[dict[str, Any]] = []
    for v, c in ((v1, i1), (v2, i2)):
        if v:
            out.append({"voltage_sensor": v, "current_sensor": c or None})
    return out


def _groups_to_assignments(groups: Any) -> dict[str, dict[str, Any]]:
    """Reverse a stored CONF_SITE_GROUPS list into per-site assignments for prefill."""
    out: dict[str, dict[str, Any]] = {}

    def _mppts(src: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        """Carry the per-MPPT capture list when set (keeps the mapping lossless)."""
        if src.get("mppts"):
            base["mppts"] = src["mppts"]
        return base

    for group in groups or []:
        mode = group.get("ac_mode", DEFAULT_PV_INPUT_MODE)
        ac = group.get("ac_sensor")
        site = group.get("site")
        if site:
            out[site] = _mppts(group, {"ac": ac, "dc": None, "mode": mode})
        for s in group.get("strings") or []:
            sid = s.get("site")
            if sid:
                out[sid] = _mppts(s, {"ac": ac, "dc": s.get("dc_sensor"), "mode": mode})
    return out


def _derive_groups(assignments: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Build CONF_SITE_GROUPS from per-site assignments.

    Sites sharing an AC sensor become one DC-apportioned group (strings = those
    with a DC sensor); a site that owns its AC sensor alone becomes a single-site
    group. A shared AC sensor with no DC sensors at all cannot be split and is
    omitted (logged), since per-array generation isn't observable there.
    """
    by_ac: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for rid, a in assignments.items():
        ac = a.get("ac")
        if not ac:
            continue
        by_ac.setdefault(ac, []).append((rid, a))

    def _with_mppts(entry: dict[str, Any], a: dict[str, Any]) -> dict[str, Any]:
        """Attach the optional per-MPPT capture list to a group/string."""
        if a.get("mppts"):
            entry["mppts"] = a["mppts"]
        return entry

    groups: list[dict[str, Any]] = []
    for ac, members in by_ac.items():
        mode = members[0][1].get("mode", DEFAULT_PV_INPUT_MODE)
        if len(members) == 1:
            groups.append(_with_mppts(
                {"ac_sensor": ac, "ac_mode": mode, "site": members[0][0]},
                members[0][1],
            ))
            continue
        strings = [
            _with_mppts({"site": rid, "dc_sensor": a["dc"]}, a)
            for rid, a in members
            if a.get("dc")
        ]
        if strings:
            groups.append({"ac_sensor": ac, "ac_mode": mode, "strings": strings})
        else:
            _LOGGER.warning(
                "AC sensor %s is shared by %d sites with no DC sensors; cannot "
                "apportion — these sites are not mapped", ac, len(members)
            )
    return groups


def _seed_flat_mppt(
    discovered: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    src: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """One-time migration: pre-fill the per-site MPPT trackers from the old flat
    per-inverter keys (``src``), so a multi-site install that previously entered
    MPPT 1/2 on Step 1 sees those entities suggested on the per-site page rather
    than losing them. MPPT 1 → first discovered site, MPPT 2 → second — a
    suggestion the user confirms (only they know which tracker feeds which array).
    Skipped once any per-site tracker is already configured."""
    if any(a.get("mppts") for a in assignments.values()):
        return assignments
    flat = [
        {"voltage_sensor": src.get(CONF_MPPT1_VOLTAGE_SENSOR),
         "current_sensor": src.get(CONF_MPPT1_CURRENT_SENSOR)},
        {"voltage_sensor": src.get(CONF_MPPT2_VOLTAGE_SENSOR),
         "current_sensor": src.get(CONF_MPPT2_CURRENT_SENSOR)},
    ]
    flat = [m for m in flat if m["voltage_sensor"]]
    for site, m in zip([s["resource_id"] for s in discovered], flat):
        a = assignments.setdefault(
            site, {"ac": None, "dc": None, "mode": DEFAULT_PV_INPUT_MODE}
        )
        a["mppts"] = [m]
    return assignments


def _clear_flat_mppt(target: dict[str, Any]) -> None:
    """Retire the flat per-inverter MPPT keys for a multi-site config (their
    trackers now live per-site). Set to ``None`` rather than popped so the value
    overrides any stale entry it shadows when merged."""
    for key in _FLAT_MPPT_KEYS:
        target[key] = None


def _build_sites_schema(
    discovered: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    default_ac: str | None = None,
) -> tuple[vol.Schema, dict[str, dict[str, str]]]:
    """Build a per-site mapping form. Returns (schema, {rid: {ac,dc,mode field keys}}).

    Field keys embed the readable site name so HA renders them as labels without
    needing per-site translations. ``default_ac`` (the system-wide PV generation
    sensor) seeds each site's generation field when it has no assignment yet, so a
    shared-meter install confirms rather than re-types the same entity.
    """
    schema_dict: dict[Any, Any] = {}
    field_map: dict[str, dict[str, str]] = {}
    seen_names: dict[str, int] = {}
    for site in discovered:
        rid = site["resource_id"]
        name = site.get("name") or rid
        if name in seen_names:
            seen_names[name] += 1
            name = f"{name} ({rid[:4]})"
        else:
            seen_names[name] = 1
        a = assignments.get(rid, {})
        mppts = a.get("mppts") or []
        m0 = mppts[0] if len(mppts) > 0 else {}
        m1 = mppts[1] if len(mppts) > 1 else {}
        k_ac = f"{name} — generation sensor"
        k_dc = f"{name} — DC/MPPT sensor (optional)"
        k_mode = f"{name} — sensor type"
        k_v1 = f"{name} — MPPT 1 voltage (optional)"
        k_i1 = f"{name} — MPPT 1 current (optional)"
        k_v2 = f"{name} — MPPT 2 voltage (optional)"
        k_i2 = f"{name} — MPPT 2 current (optional)"
        field_map[rid] = {
            "ac": k_ac, "dc": k_dc, "mode": k_mode,
            "v1": k_v1, "i1": k_i1, "v2": k_v2, "i2": k_i2,
        }
        schema_dict[vol.Optional(k_ac, description={"suggested_value": a.get("ac") or default_ac})] = _entity_selector()
        schema_dict[vol.Optional(k_dc, description={"suggested_value": a.get("dc")})] = _entity_selector()
        schema_dict[vol.Required(k_mode, default=a.get("mode", DEFAULT_PV_INPUT_MODE))] = _input_mode_selector()
        schema_dict[vol.Optional(k_v1, description={"suggested_value": m0.get("voltage_sensor")})] = _entity_selector()
        schema_dict[vol.Optional(k_i1, description={"suggested_value": m0.get("current_sensor")})] = _entity_selector()
        schema_dict[vol.Optional(k_v2, description={"suggested_value": m1.get("voltage_sensor")})] = _entity_selector()
        schema_dict[vol.Optional(k_i2, description={"suggested_value": m1.get("current_sensor")})] = _entity_selector()
    return vol.Schema(schema_dict), field_map


def _parse_sites_input(
    user_input: dict[str, Any],
    field_map: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Collect per-site assignments from submitted form values (AC sensor required)."""
    assignments: dict[str, dict[str, Any]] = {}
    for rid, keys in field_map.items():
        ac = user_input.get(keys["ac"])
        if not ac:
            continue
        assignments[rid] = {
            "ac": ac,
            "dc": user_input.get(keys["dc"]) or None,
            "mode": user_input.get(keys["mode"], DEFAULT_PV_INPUT_MODE),
            "mppts": _fields_to_mppts(
                user_input.get(keys["v1"]), user_input.get(keys["i1"]),
                user_input.get(keys["v2"]), user_input.get(keys["i2"]),
            ),
        }
    return assignments


class SolcastEnhancedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """5-step setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._discovered: list[dict[str, Any]] | None = None

    def _is_single_site(self) -> bool:
        """Whether the property has one (or no) auto-discovered Solcast site.

        Drives field placement — single-array systems map MPPT trackers on Step 1
        and skip the per-site step; multi-array systems do the reverse. Cached so
        Step 1 and the per-site step agree within one flow."""
        from .coordinator import discover_sites

        if self._discovered is None:
            self._discovered = discover_sites(self.hass)
        return len(self._discovered) <= 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_site(user_input)

    async def async_step_site(self, user_input: dict[str, Any] | None = None):
        """Step 1 — Site & System."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_database()

        schema = _build_site_schema({}, single_site=self._is_single_site())
        return self.async_show_form(step_id="site", data_schema=schema, errors={})

    async def async_step_database(self, user_input: dict[str, Any] | None = None):
        """Step 2 — Storage. Built-in SQLite store, on by default; no setup needed."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_weather()

        schema = vol.Schema({
            vol.Required(CONF_DB_ENABLED, default=DEFAULT_DB_ENABLED): BooleanSelector(),
            vol.Required(CONF_DB_RETENTION_DAYS, default=DEFAULT_DB_RETENTION_DAYS): NumberSelector(
                NumberSelectorConfig(min=0, max=3650, step=1)
            ),
        })
        return self.async_show_form(step_id="database", data_schema=schema)

    async def async_step_weather(self, user_input: dict[str, Any] | None = None):
        """Step 3 — Weather & irradiance. Open-Meteo (keyless, default) supplies the
        irradiance for PV tuning plus cloud/temperature; OpenWeatherMap is an
        optional legacy alternative for cloud/temperature."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()

        schema = vol.Schema({
            vol.Required(
                CONF_OPENMETEO_ENABLED, default=DEFAULT_OPENMETEO_ENABLED
            ): BooleanSelector(),
            vol.Required(CONF_OWM_ENABLED, default=False): BooleanSelector(),
            vol.Optional(CONF_OWM_API_KEY, default=""): TextSelector(
                TextSelectorConfig(type="password")
            ),
        })
        return self.async_show_form(step_id="weather", data_schema=schema)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        """Step 4 — Battery Storage (optional)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_tuning()

        schema = vol.Schema({
            vol.Required(CONF_BATTERY_ENABLED, default=False): BooleanSelector(),
            vol.Optional(CONF_BATTERY_MODE, default="net"): SelectSelector(
                SelectSelectorConfig(options=["net", "separate"], mode="dropdown")
            ),
            vol.Optional(CONF_BATTERY_NET_SENSOR): _entity_selector(),
            vol.Optional(CONF_BATTERY_CHARGE_SENSOR): _entity_selector(),
        })
        return self.async_show_form(step_id="battery", data_schema=schema)

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None):
        """Step 5 — PV Tuning & Dampening."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sites()

        schema = vol.Schema({
            vol.Required(CONF_AUTO_TUNING, default=DEFAULT_AUTO_TUNING): BooleanSelector(),
            vol.Required(CONF_AUTO_DAMPENING, default=DEFAULT_AUTO_DAMPENING): BooleanSelector(),
            vol.Required(CONF_DAMPENING_GATE, default=DEFAULT_DAMPENING_GATE): BooleanSelector(),
            vol.Required(CONF_CLOUD_THRESHOLD, default=DEFAULT_CLOUD_THRESHOLD): NumberSelector(
                NumberSelectorConfig(min=10, max=50, step=1)
            ),
            vol.Required(CONF_CLOUD_MAX_INCLUDE, default=DEFAULT_CLOUD_MAX_INCLUDE): NumberSelector(
                NumberSelectorConfig(min=20, max=100, step=1)
            ),
            vol.Required(CONF_KT_THRESHOLD, default=DEFAULT_KT_THRESHOLD): NumberSelector(
                NumberSelectorConfig(min=0.5, max=1.0, step=0.05)
            ),
            vol.Required(CONF_CLIPPING_THRESHOLD, default=DEFAULT_CLIPPING_THRESHOLD): NumberSelector(
                NumberSelectorConfig(min=0.5, max=1.0, step=0.01)
            ),
            vol.Required(CONF_EXPORT_LIMIT_KW, default=DEFAULT_EXPORT_LIMIT_KW): NumberSelector(
                NumberSelectorConfig(min=0.0, max=100.0, step=0.1)
            ),
        })
        return self.async_show_form(step_id="tuning", data_schema=schema)

    async def async_step_sites(self, user_input: dict[str, Any] | None = None):
        """Step 6 — Per-site sensor mapping (multi-site). Skipped if ≤1 site."""
        if self._is_single_site():
            return self.async_create_entry(title="Solcast Solar Enhanced", data=self._data)
        discovered = self._discovered or []

        existing = _groups_to_assignments(self._data.get(CONF_SITE_GROUPS))
        existing = _seed_flat_mppt(discovered, existing, self._data)
        schema, field_map = _build_sites_schema(
            discovered, existing, default_ac=self._data.get(CONF_PV_ACTUAL_SENSOR)
        )
        if user_input is not None:
            assignments = _parse_sites_input(user_input, field_map)
            self._data[CONF_SITE_GROUPS] = _derive_groups(assignments)
            _clear_flat_mppt(self._data)
            return self.async_create_entry(title="Solcast Solar Enhanced", data=self._data)
        return self.async_show_form(
            step_id="sites",
            data_schema=schema,
            description_placeholders={"count": str(len(discovered))},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return SolcastEnhancedOptionsFlow()


class SolcastEnhancedOptionsFlow(config_entries.OptionsFlow):
    """Options flow — reconfigures all settings."""

    def __init__(self) -> None:
        self._opts: dict[str, Any] = {}
        self._discovered: list[dict[str, Any]] | None = None

    def _is_single_site(self) -> bool:
        """See ``SolcastEnhancedConfigFlow._is_single_site``."""
        from .coordinator import discover_sites

        if self._discovered is None:
            self._discovered = discover_sites(self.hass)
        return len(self._discovered) <= 1

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_site(user_input)

    async def async_step_site(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_database()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = _build_site_schema(current, single_site=self._is_single_site())
        return self.async_show_form(step_id="site", data_schema=schema)

    async def async_step_database(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_weather()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_DB_ENABLED, default=current.get(CONF_DB_ENABLED, DEFAULT_DB_ENABLED)): BooleanSelector(),
            vol.Required(
                CONF_DB_RETENTION_DAYS,
                default=current.get(CONF_DB_RETENTION_DAYS, DEFAULT_DB_RETENTION_DAYS),
            ): NumberSelector(NumberSelectorConfig(min=0, max=3650, step=1)),
        })
        return self.async_show_form(step_id="database", data_schema=schema)

    async def async_step_weather(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_battery()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(
                CONF_OPENMETEO_ENABLED,
                default=current.get(CONF_OPENMETEO_ENABLED, DEFAULT_OPENMETEO_ENABLED),
            ): BooleanSelector(),
            vol.Required(CONF_OWM_ENABLED, default=current.get(CONF_OWM_ENABLED, False)): BooleanSelector(),
            vol.Optional(CONF_OWM_API_KEY, default=current.get(CONF_OWM_API_KEY, "")): TextSelector(
                TextSelectorConfig(type="password")
            ),
        })
        return self.async_show_form(step_id="weather", data_schema=schema)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_tuning()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_BATTERY_ENABLED, default=current.get(CONF_BATTERY_ENABLED, False)): BooleanSelector(),
            vol.Optional(CONF_BATTERY_MODE, default=current.get(CONF_BATTERY_MODE, "net")): SelectSelector(
                SelectSelectorConfig(options=["net", "separate"], mode="dropdown")
            ),
            vol.Optional(CONF_BATTERY_NET_SENSOR, description={"suggested_value": current.get(CONF_BATTERY_NET_SENSOR)}): _entity_selector(),
            vol.Optional(CONF_BATTERY_CHARGE_SENSOR, description={"suggested_value": current.get(CONF_BATTERY_CHARGE_SENSOR)}): _entity_selector(),
        })
        return self.async_show_form(step_id="battery", data_schema=schema)

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._opts.update(user_input)
            return await self.async_step_sites()

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_AUTO_TUNING, default=current.get(CONF_AUTO_TUNING, DEFAULT_AUTO_TUNING)): BooleanSelector(),
            vol.Required(CONF_AUTO_DAMPENING, default=current.get(CONF_AUTO_DAMPENING, DEFAULT_AUTO_DAMPENING)): BooleanSelector(),
            vol.Required(CONF_DAMPENING_GATE, default=current.get(CONF_DAMPENING_GATE, DEFAULT_DAMPENING_GATE)): BooleanSelector(),
            vol.Required(CONF_CLOUD_THRESHOLD, default=current.get(CONF_CLOUD_THRESHOLD, DEFAULT_CLOUD_THRESHOLD)): NumberSelector(
                NumberSelectorConfig(min=10, max=50, step=1)
            ),
            vol.Required(CONF_CLOUD_MAX_INCLUDE, default=current.get(CONF_CLOUD_MAX_INCLUDE, DEFAULT_CLOUD_MAX_INCLUDE)): NumberSelector(
                NumberSelectorConfig(min=20, max=100, step=1)
            ),
            vol.Required(CONF_KT_THRESHOLD, default=current.get(CONF_KT_THRESHOLD, DEFAULT_KT_THRESHOLD)): NumberSelector(
                NumberSelectorConfig(min=0.5, max=1.0, step=0.05)
            ),
            vol.Required(CONF_CLIPPING_THRESHOLD, default=current.get(CONF_CLIPPING_THRESHOLD, DEFAULT_CLIPPING_THRESHOLD)): NumberSelector(
                NumberSelectorConfig(min=0.5, max=1.0, step=0.01)
            ),
            vol.Required(CONF_EXPORT_LIMIT_KW, default=current.get(CONF_EXPORT_LIMIT_KW, DEFAULT_EXPORT_LIMIT_KW)): NumberSelector(
                NumberSelectorConfig(min=0.0, max=100.0, step=0.1)
            ),
        })
        return self.async_show_form(step_id="tuning", data_schema=schema)

    async def async_step_sites(self, user_input: dict[str, Any] | None = None):
        """Per-site sensor mapping (multi-site). Skipped if ≤1 site discovered."""
        if self._is_single_site():
            return self.async_create_entry(data=self._opts)
        discovered = self._discovered or []

        current = {**self.config_entry.data, **self.config_entry.options}
        existing = _groups_to_assignments(
            self._opts.get(CONF_SITE_GROUPS) or current.get(CONF_SITE_GROUPS)
        )
        existing = _seed_flat_mppt(discovered, existing, current)
        schema, field_map = _build_sites_schema(
            discovered, existing,
            default_ac=self._opts.get(CONF_PV_ACTUAL_SENSOR)
            or current.get(CONF_PV_ACTUAL_SENSOR),
        )
        if user_input is not None:
            assignments = _parse_sites_input(user_input, field_map)
            self._opts[CONF_SITE_GROUPS] = _derive_groups(assignments)
            _clear_flat_mppt(self._opts)
            return self.async_create_entry(data=self._opts)
        return self.async_show_form(
            step_id="sites",
            data_schema=schema,
            description_placeholders={"count": str(len(discovered))},
        )
