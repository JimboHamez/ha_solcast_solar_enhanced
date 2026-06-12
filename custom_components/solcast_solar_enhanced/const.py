"""Constants for the Solcast Solar Enhanced integration."""
from __future__ import annotations

DOMAIN = "solcast_solar_enhanced"
BASE_DOMAIN = "solcast_solar"
PLATFORMS = ["sensor"]

# Base integration's "API last polled" timestamp sensor. Its state advances each
# time the base fetches a fresh Solcast forecast; we watch it to re-push the
# dampening factors right after a poll (so the latest curve lands on the freshly
# fetched forecast) rather than only on the periodic timer.
BASE_API_LAST_POLLED_SENSOR = "sensor.solcast_pv_forecast_api_last_polled"

# Config keys
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_CAPACITY_KW = "capacity_kw"
CONF_TILT = "tilt"
CONF_AZIMUTH = "azimuth"
CONF_PV_ACTUAL_SENSOR = "pv_actual_sensor"
CONF_PV_EXPORT_SENSOR = "pv_export_sensor"
CONF_PV_ACTUAL_INPUT_MODE = "pv_actual_input_mode"
CONF_PV_EXPORT_INPUT_MODE = "pv_export_input_mode"
CONF_BATTERY_STAT_SENSOR = "battery_stat_sensor"
# Phase-2 curtailment-detection capture: per-MPPT DC string telemetry. Up to
# MAX_MPPT_TRACKERS paired (voltage, current) trackers per site/inverter. Voltage
# rising toward Voc while current collapses is the off-MPP fingerprint of
# curtailment; pairs are kept per-tracker (not aggregated) so a later Vmp-band
# calibrator can learn each string. Captured now — it cannot be backfilled.
# Property-wide / single-inverter trackers use these flat keys on the site step;
# per-site (multi-site) trackers are stored as an ``mppts`` list inside each
# CONF_SITE_GROUPS single-site group or per-string entry.
MAX_MPPT_TRACKERS = 2
CONF_MPPT1_VOLTAGE_SENSOR = "mppt1_voltage_sensor"
CONF_MPPT1_CURRENT_SENSOR = "mppt1_current_sensor"
CONF_MPPT2_VOLTAGE_SENSOR = "mppt2_voltage_sensor"
CONF_MPPT2_CURRENT_SENSOR = "mppt2_current_sensor"

CONF_DB_ENABLED = "db_enabled"
# Optional history retention. 0 = keep everything (default, never prunes). When
# > 0, rows older than this many days are deleted on a daily timer to bound the
# table on long-lived / low-power (Raspberry Pi) installs. Seasonal dampening
# uses a cross-year day-of-year window, so a value below ~13 months degrades it —
# DB_RETENTION_MIN_RECOMMENDED_DAYS drives a warning, not a hard floor.
CONF_DB_RETENTION_DAYS = "db_retention_days"
DEFAULT_DB_RETENTION_DAYS = 0
DB_RETENTION_MIN_RECOMMENDED_DAYS = 400

CONF_OWM_ENABLED = "owm_enabled"
CONF_OWM_API_KEY = "owm_api_key"

CONF_BATTERY_ENABLED = "battery_enabled"
CONF_BATTERY_MODE = "battery_mode"
CONF_BATTERY_NET_SENSOR = "battery_net_sensor"
CONF_BATTERY_CHARGE_SENSOR = "battery_charge_sensor"

# Multi-site (multiple Solcast rooftop arrays on one property).
# DEFAULT_SITE_ID tags single-site / aggregate rows and back-filled legacy data;
# kept in sync with sqlite_store.DEFAULT_SITE.
DEFAULT_SITE_ID = "_total"
# Stored structured config: list of measurement groups mapping a generation
# sensor (and optional per-MPPT DC apportionment) to one or more sites.
CONF_SITES = "sites"
CONF_SITE_GROUPS = "site_groups"
# Auto-discover sites from the base integration's per-site RooftopSensors
# (attributes: resource_id, name, capacity, capacity_dc, azimuth, tilt, ...).
CONF_SITE_AUTODISCOVER = "site_autodiscover"
DEFAULT_SITE_AUTODISCOVER = True

CONF_AUTO_TUNING = "auto_tuning"
CONF_AUTO_DAMPENING = "auto_dampening"
CONF_CLOUD_THRESHOLD = "cloud_threshold"
CONF_CLOUD_MAX_INCLUDE = "cloud_max_include"
CONF_CLIPPING_THRESHOLD = "clipping_threshold"
CONF_EXPORT_LIMIT_KW = "export_limit_kw"
CONF_DAMPENING_GATE = "dampening_gate"

# Defaults
DEFAULT_LATITUDE = -37.9
DEFAULT_LONGITUDE = 145.0
DEFAULT_CAPACITY_KW = 5.0
DEFAULT_TILT = 20.0
DEFAULT_AZIMUTH = 0.0
DEFAULT_DB_ENABLED = True
# Built-in SQLite file, created in the HA config directory.
DEFAULT_DB_FILENAME = "solcast_solar_enhanced.db"
DEFAULT_AUTO_TUNING = True
DEFAULT_AUTO_DAMPENING = True
DEFAULT_CLOUD_THRESHOLD = 20
DEFAULT_CLOUD_MAX_INCLUDE = 60
DEFAULT_CLIPPING_THRESHOLD = 0.95
DEFAULT_EXPORT_LIMIT_KW = 0.0
DEFAULT_DAMPENING_GATE = True

# PV input modes — how to interpret the configured pv_actual / pv_export sensors.
#   auto        : detect from state_class + unit_of_measurement
#   power_kw    : instantaneous/averaged power already in kW
#   power_w     : instantaneous/averaged power in W (divided by 1000)
#   energy_kwh  : cumulative energy counter in kWh (delta over the interval)
#   energy_wh   : cumulative energy counter in Wh (delta over the interval)
#   energy_mwh  : cumulative energy counter in MWh (delta over the interval)
PV_INPUT_MODES = [
    "auto",
    "power_kw",
    "power_w",
    "energy_kwh",
    "energy_wh",
    "energy_mwh",
]
DEFAULT_PV_INPUT_MODE = "auto"

# Energy-counter interval acceptance band, as a fraction of the expected update
# interval. A delta measured over a span outside this band (e.g. after a restart
# or a missed cycle) is excluded rather than attributed to a single half-hour slot.
ENERGY_DT_MIN_FRACTION = 0.5
ENERGY_DT_MAX_FRACTION = 2.0

# Internal
UPDATE_INTERVAL_MINUTES = 30
# Seconds past the :00/:30 boundary at which the wall-clock-aligned refresh fires.
# A small positive offset lets boundary energy-counter states post before the
# delta is read (counters update on their own cadence, not exactly on the minute).
HALF_HOUR_REFRESH_OFFSET_SECONDS = 30
DAMPENING_INTERVAL_HOURS = 6
TUNING_INTERVAL_HOURS = 24
# Upper bound on clear-sky rows pulled into a single tuning fit. Panel orientation
# is a fixed physical constant, so the fit should span the *full* history (every
# season cancels the seasonal sun-angle bias and the estimate converges), not a
# recent window — a recent-weighted window never settles and makes the tuned
# tilt/azimuth wander day to day. This cap is a memory guard only: ~20k clear-sky
# half-hours is several years for a typical site, so in practice the whole history
# is used. Raise if a very long-lived site ever brushes it.
TUNING_MAX_RECORDS = 20000
# Grace delay after the base "API last polled" sensor advances before we re-push
# dampening. The poll timestamp updates when the fetch fires, but the base then
# parses/stores the forecast detail we read; this lets that settle. Also debounces
# bursts of poll-sensor writes into a single dampening run.
DAMPENING_POLL_DELAY_SECONDS = 60
STORAGE_VERSION = 1
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

# Repair-issue id raised when cloud-driven features (tuning/dampening) are enabled
# but no OpenWeatherMap source is configured. Translation lives under `issues`.
ISSUE_OWM_REQUIRED = "owm_required"

# Dampening convergence gate: hold the dampening push at neutral (1.0) when the
# tuned orientation diverges materially from the configured (Solcast) one, so a
# mis-configured site can't bake orientation error into the dampening curve (the
# notebook 3.4b "tuned estimate" prerequisite). Per-site aware.
ISSUE_DAMPENING_GATED = "dampening_gated"
DAMPENING_GATE_MIN_RECORDS = 50      # tuning confidence before the gate may act
DAMPENING_GATE_TILT_TOL = 15.0       # ° tilt divergence that trips the gate
DAMPENING_GATE_AZIMUTH_TOL = 25.0    # ° azimuth divergence that trips the gate

# Orientation recommendation: the user-facing "should I update my Solcast site
# tilt/azimuth?" signal. Tighter tolerances than the dampening gate above — the
# gate guards against *gross* mis-config baking into dampening, whereas this is a
# gentle nudge to align Solcast with the converged fit. Confidence reuses
# DAMPENING_GATE_MIN_RECORDS. The status only flips to "update_suggested" when the
# converged fit differs by more than these tolerances; once the user applies the
# change the delta collapses back inside tolerance, so it nudges once rather than
# nagging every cycle.
RECOMMEND_TILT_TOL = 4.0             # ° tilt divergence to suggest updating Solcast
RECOMMEND_AZIMUTH_TOL = 6.0         # ° azimuth divergence to suggest updating Solcast

# Sensor keys
SENSOR_FORECAST_NOW = "forecast_now"
SENSOR_FORECAST_TODAY = "forecast_today"
SENSOR_TUNING_TILT = "tuning_tilt"
SENSOR_TUNING_AZIMUTH = "tuning_azimuth"
SENSOR_TUNING_RMSE = "tuning_rmse"
SENSOR_TUNING_EXPORT_EXCLUDED = "tuning_export_excluded"
SENSOR_ORIENTATION_RECOMMENDATION = "orientation_recommendation"
SENSOR_DB_RECORDS = "db_records"
SENSOR_DAMPENING = "dampening"
SENSOR_WEATHER_TEMP = "weather_temp"
SENSOR_WEATHER_CLOUDS = "weather_clouds"
SENSOR_BATTERY_CHARGE = "battery_charge"
SENSOR_PV_ACTUAL = "pv_actual"
SENSOR_PV_EXPORT = "pv_export"
SENSOR_BASE_STATUS = "base_status"
SENSOR_MPPT_DC = "mppt_dc"

# Services
SERVICE_RUN_PV_TUNING = "run_pv_tuning"
SERVICE_RUN_DAMPENING_UPDATE = "run_dampening_update"
SERVICE_FETCH_WEATHER = "fetch_weather"
