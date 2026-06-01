"""Constants for the Solcast Solar Enhanced integration."""
from __future__ import annotations

DOMAIN = "solcast_solar_enhanced"
BASE_DOMAIN = "solcast_solar"
PLATFORMS = ["sensor"]

# Config keys
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_CAPACITY_KW = "capacity_kw"
CONF_TILT = "tilt"
CONF_AZIMUTH = "azimuth"
CONF_PV_ACTUAL_SENSOR = "pv_actual_sensor"
CONF_PV_EXPORT_SENSOR = "pv_export_sensor"
CONF_BATTERY_STAT_SENSOR = "battery_stat_sensor"

CONF_DB_ENABLED = "db_enabled"
CONF_DB_HOST = "db_host"
CONF_DB_PORT = "db_port"
CONF_DB_USER = "db_user"
CONF_DB_PASSWORD = "db_password"
CONF_DB_NAME = "db_name"
CONF_DB_READONLY = "db_readonly"

CONF_OWM_ENABLED = "owm_enabled"
CONF_OWM_API_KEY = "owm_api_key"

CONF_BATTERY_ENABLED = "battery_enabled"
CONF_BATTERY_MODE = "battery_mode"
CONF_BATTERY_NET_SENSOR = "battery_net_sensor"
CONF_BATTERY_CHARGE_SENSOR = "battery_charge_sensor"

CONF_AUTO_TUNING = "auto_tuning"
CONF_AUTO_DAMPENING = "auto_dampening"
CONF_CLOUD_THRESHOLD = "cloud_threshold"
CONF_CLOUD_MAX_INCLUDE = "cloud_max_include"
CONF_CLIPPING_THRESHOLD = "clipping_threshold"
CONF_EXPORT_LIMIT_KW = "export_limit_kw"

# Defaults
DEFAULT_LATITUDE = -37.9
DEFAULT_LONGITUDE = 145.0
DEFAULT_CAPACITY_KW = 5.0
DEFAULT_TILT = 20.0
DEFAULT_AZIMUTH = 0.0
DEFAULT_DB_HOST = "localhost"
DEFAULT_DB_PORT = 3306
DEFAULT_DB_NAME = "solcast"
DEFAULT_AUTO_TUNING = True
DEFAULT_AUTO_DAMPENING = True
DEFAULT_CLOUD_THRESHOLD = 20
DEFAULT_CLOUD_MAX_INCLUDE = 60
DEFAULT_CLIPPING_THRESHOLD = 0.95
DEFAULT_EXPORT_LIMIT_KW = 0.0

# Internal
UPDATE_INTERVAL_MINUTES = 30
DAMPENING_INTERVAL_HOURS = 6
TUNING_INTERVAL_HOURS = 24
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

# Sensor keys
SENSOR_FORECAST_NOW = "forecast_now"
SENSOR_FORECAST_TODAY = "forecast_today"
SENSOR_TUNING_TILT = "tuning_tilt"
SENSOR_TUNING_AZIMUTH = "tuning_azimuth"
SENSOR_TUNING_RMSE = "tuning_rmse"
SENSOR_TUNING_EXPORT_EXCLUDED = "tuning_export_excluded"
SENSOR_DB_RECORDS = "db_records"
SENSOR_DAMPENING = "dampening"
SENSOR_WEATHER_TEMP = "weather_temp"
SENSOR_WEATHER_CLOUDS = "weather_clouds"
SENSOR_BATTERY_CHARGE = "battery_charge"
SENSOR_PV_ACTUAL = "pv_actual"
SENSOR_PV_EXPORT = "pv_export"
SENSOR_BASE_STATUS = "base_status"

# Services
SERVICE_RUN_PV_TUNING = "run_pv_tuning"
SERVICE_RUN_DAMPENING_UPDATE = "run_dampening_update"
SERVICE_FETCH_WEATHER = "fetch_weather"
