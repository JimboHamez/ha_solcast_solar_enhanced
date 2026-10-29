"""Typed shapes of the dicts passed between the store, the estimators and the coordinator.

These are ``TypedDict`` definitions only, so they are ordinary dicts at runtime and
change no behaviour. They exist so mypy checks every key name and value type at the
points where records, site configuration and results cross a module boundary.

Deliberately *not* modelled, because their shape is owned elsewhere:

- config-flow ``user_input`` and voluptuous schema dicts;
- the merged entry options (``{**entry.data, **entry.options}``);
- raw JSON from Open-Meteo / OpenWeatherMap and the base integration's internals;
- ``extra_state_attributes`` dicts, typed by Home Assistant core.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class StoreRecord(TypedDict, total=False):
    """One ``solcast_data`` row, as written to or read back from the store.

    Every key is optional because each read query selects only the columns its
    consumer needs, and the estimators read them with ``.get`` fallbacks. The
    columns are listed in ``CREATE_TABLE_SQL`` order. A new column must be added
    here as well as in the three places ``sqlite_store`` lists, and
    ``tests/test_sqlite_store.py`` pins this class against ``_INSERT_COLUMNS``.
    """

    period_end: str
    period_end_epoch: int
    period_start: str
    site: str
    pv_actual: float
    pv_export: float
    pv_estimate: float
    pv_estimate10: float
    pv_estimate90: float
    azimuth: float
    zenith: float
    temp: float
    clouds: int
    description: str
    battery_charge: float
    dc_voltage1: float
    dc_current1: float
    dc_voltage2: float
    dc_current2: float
    ghi: float
    dni: float
    dhi: float
    dc_vmed1: float
    dc_vmed2: float
    dc_imed1: float
    dc_imed2: float
    pv_estimate_undampened: float
    pv_export_max: float


class StoredIrradiance(TypedDict):
    """Open-Meteo irradiance components as written to a row (0 = unknown)."""

    dhi: float
    dni: float
    ghi: float


class DampeningResult(TypedDict):
    """One half-hour slot's dampening factor and the diagnostics behind it."""

    alpha: float
    avg_quality: float
    clear_sky_basis: str
    clipped_excluded: int
    # Only the sun-position estimator reports how much forecast energy covered the slot.
    coverage_kw: NotRequired[float]
    export_capped: int
    factor: float
    forecast_clipped: int
    quality_records: float
    source: str
    undampened_records: int


class TuningResult(TypedDict):
    """``pv_tuning.run_tuning`` output: the fitted tilt and how far to trust it."""

    azimuth: float
    capacity_scale: float
    export_limited_excluded: int
    fit_rel_error: float
    mae_kw: float
    n_records: int
    # Added by the coordinator to a per-site result, for the sensors' attributes.
    name: NotRequired[str | None]
    resource_id: NotRequired[str]
    rmse_kw: float
    source: str
    tilt: float
    tilt_identifiable: bool
    tilt_unidentifiable_reason: str | None


class ConfidenceResult(TypedDict):
    """``load_advisory.compute_confidence`` output."""

    confidence: int | None
    n_slots: int
    rating: str
    recent_bias: float | None


class DiscoveredSite(TypedDict, total=False):
    """A Solcast rooftop site read from the base integration's RooftopSensor.

    ``discover_sites`` always fills every key. The class is ``total=False`` because
    several lookups fall back to an empty dict for a site that was not discovered.
    """

    azimuth: float
    capacity: float
    capacity_dc: float
    compass_degrees: float
    entity_id: str
    name: str
    resource_id: str
    tilt: float


class MpptTracker(TypedDict, total=False):
    """One MPPT tracker's DC sensors in a site group (either may be unset)."""

    current_sensor: str | None
    voltage_sensor: str | None


class SiteString(TypedDict):
    """One array on a shared inverter, apportioned by its DC share (``dc_split``)."""

    # Read by the apportionment, but the config flow never writes it, so the DC
    # sensors always fall back to the default (auto-detected) input mode.
    dc_mode: NotRequired[str]
    # Entries written before multi-MPPT strings stored a single sensor here.
    dc_sensor: NotRequired[str]
    dc_sensors: NotRequired[list[str]]
    mppts: NotRequired[list[MpptTracker]]
    name: NotRequired[str]
    site: str


class SiteGroup(TypedDict):
    """``CONF_SITE_GROUPS`` entry: one generation sensor and the array(s) it covers.

    A ``direct`` group names one ``site``; a ``dc_split`` group lists ``strings``.
    """

    ac_mode: NotRequired[str]
    ac_sensor: str
    mppts: NotRequired[list[MpptTracker]]
    name: NotRequired[str]
    site: NotRequired[str]
    strings: NotRequired[list[SiteString]]


class DcPairs(TypedDict):
    """Latest per-tracker DC telemetry for the diagnostic sensor."""

    mppt1_current: float
    mppt1_voltage: float
    mppt2_current: float
    mppt2_voltage: float


class DcTelemetry(DcPairs):
    """Property-wide DC telemetry, plus the highest voltage and each array's trackers."""

    max_voltage: float
    sites: dict[str, DcPairs]


class CoordinatorData(TypedDict):
    """What ``SolcastEnhancedCoordinator`` publishes as ``coordinator.data`` each cycle."""

    base_status: str
    battery_charge: float
    dampening_table: list[DampeningResult]
    db_latest_period_end: str | None
    db_records: int
    db_sites: list[str]
    dc_telemetry: DcTelemetry | None
    forecast_now: float
    forecast_today: float
    orientation_diverged: bool
    pv_actual: float
    pv_export: float
    tuning: TuningResult | None
    # Normalised by the weather clients, which own its shape.
    weather: dict[str, Any]
