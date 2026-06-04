"""Tests for storage backend selection and the MySQL→SQLite import."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.solcast_solar_enhanced.const import (
    CONF_DB_BACKEND,
    CONF_DB_USER,
    DB_BACKEND_BUILTIN,
    DB_BACKEND_MYSQL,
)
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.db_manager import DbManager
from custom_components.solcast_solar_enhanced.sqlite_store import SqliteStore
from custom_components.solcast_solar_enhanced.storage import (
    build_storage,
    resolve_backend,
)


# ---------------------------------------------------------------------------
# resolve_backend
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("opts,expected", [
    ({CONF_DB_BACKEND: DB_BACKEND_BUILTIN}, DB_BACKEND_BUILTIN),
    ({CONF_DB_BACKEND: DB_BACKEND_MYSQL}, DB_BACKEND_MYSQL),
    # Explicit backend wins even if a user is also present.
    ({CONF_DB_BACKEND: DB_BACKEND_BUILTIN, CONF_DB_USER: "solcast"}, DB_BACKEND_BUILTIN),
    # No explicit backend: infer from a configured MySQL user (legacy entry).
    ({CONF_DB_USER: "solcast"}, DB_BACKEND_MYSQL),
    ({CONF_DB_USER: "  "}, DB_BACKEND_BUILTIN),
    ({CONF_DB_USER: ""}, DB_BACKEND_BUILTIN),
    ({}, DB_BACKEND_BUILTIN),
])
def test_resolve_backend(opts, expected):
    assert resolve_backend(opts) == expected


# ---------------------------------------------------------------------------
# build_storage
# ---------------------------------------------------------------------------

def test_build_storage_builtin(hass):
    store = build_storage(hass, {CONF_DB_BACKEND: DB_BACKEND_BUILTIN})
    assert isinstance(store, SqliteStore)


def test_build_storage_mysql(hass):
    store = build_storage(hass, {CONF_DB_BACKEND: DB_BACKEND_MYSQL, CONF_DB_USER: "u"})
    assert isinstance(store, DbManager)


def test_build_storage_infers_mysql_for_legacy_entry(hass):
    store = build_storage(hass, {CONF_DB_USER: "solcast"})
    assert isinstance(store, DbManager)


# ---------------------------------------------------------------------------
# coordinator MySQL → SQLite import
# ---------------------------------------------------------------------------

def _sample(epoch: int) -> dict:
    return {
        "period_end": "2024-06-01T12:00:00+00:00",
        "period_end_epoch": epoch,
        "period_start": "2024-06-01T11:30:00+00:00",
        "site": "_total",
        "pv_actual": 3.5,
        "pv_export": 0.5,
        "pv_estimate": 4.0,
        "pv_estimate10": 3.0,
        "pv_estimate90": 5.0,
        "azimuth": 180.0,
        "zenith": 35.0,
        "temp": 22.5,
        "clouds": 10,
        "description": "clear sky",
        "battery_charge": 0.0,
    }


@pytest.fixture
async def sqlite_coordinator(hass, mock_config_entry, tmp_path):
    mock_config_entry.add_to_hass(hass)
    coord = SolcastEnhancedCoordinator(hass, mock_config_entry)
    store = SqliteStore(hass, str(tmp_path / "import.db"))
    await store.async_connect()
    coord._db = store
    yield coord
    await store.async_close()


async def test_import_from_mysql_copies_rows(hass, sqlite_coordinator):
    source = AsyncMock()
    source.async_connect = AsyncMock(return_value=True)
    source.async_get_all_records = AsyncMock(
        return_value=[_sample(1717243200), _sample(1717245000)]
    )
    source.async_close = AsyncMock()

    with patch(
        "custom_components.solcast_solar_enhanced.coordinator.build_mysql_manager",
        return_value=source,
    ):
        imported = await sqlite_coordinator.async_import_from_mysql(
            {"db_host": "10.0.0.1", "db_user": "solcast"}
        )

    assert imported == 2
    assert await sqlite_coordinator._db.async_get_record_count() == 2
    source.async_close.assert_awaited_once()


async def test_import_from_mysql_skips_when_source_unreachable(hass, sqlite_coordinator):
    source = AsyncMock()
    source.async_connect = AsyncMock(return_value=False)
    with patch(
        "custom_components.solcast_solar_enhanced.coordinator.build_mysql_manager",
        return_value=source,
    ):
        imported = await sqlite_coordinator.async_import_from_mysql({})
    assert imported == 0


async def test_import_from_mysql_noop_when_active_store_is_mysql(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    coord = SolcastEnhancedCoordinator(hass, mock_config_entry)
    coord._db = DbManager("localhost", 3306, "u", "p", "solcast")  # not SqliteStore
    imported = await coord.async_import_from_mysql({})
    assert imported == 0
