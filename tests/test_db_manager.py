"""Test DbManager with mocked aiomysql."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solcast_solar_enhanced.db_manager import DbManager

SAMPLE_RECORD = {
    "period_end": "2024-06-01T12:00:00+00:00",
    "period_end_epoch": 1717243200,
    "period_start": "2024-06-01T11:30:00+00:00",
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
def db():
    return DbManager("localhost", 3306, "user", "pass", "solcast")


@pytest.fixture
def db_readonly():
    return DbManager("localhost", 3306, "user", "pass", "solcast", readonly=True)


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

async def test_connect_returns_false_without_aiomysql(db):
    with patch("custom_components.solcast_solar_enhanced.db_manager.DB_AVAILABLE", False):
        result = await db.async_connect()
    assert result is False


async def test_connect_returns_false_on_exception(db):
    mock_aiomysql = MagicMock()
    mock_aiomysql.create_pool = AsyncMock(side_effect=Exception("connection refused"))
    with (
        patch("custom_components.solcast_solar_enhanced.db_manager.DB_AVAILABLE", True),
        patch("custom_components.solcast_solar_enhanced.db_manager.aiomysql", mock_aiomysql, create=True),
    ):
        result = await db.async_connect()
    assert result is False


async def test_connect_success(db):
    mock_cursor = AsyncMock()
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=(1,))

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_conn)

    mock_aiomysql = MagicMock()
    mock_aiomysql.create_pool = AsyncMock(return_value=mock_pool)

    with (
        patch("custom_components.solcast_solar_enhanced.db_manager.DB_AVAILABLE", True),
        patch("custom_components.solcast_solar_enhanced.db_manager.aiomysql", mock_aiomysql, create=True),
    ):
        result = await db.async_connect()

    assert result is True
    assert db.available is True


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------

async def test_insert_returns_false_when_no_pool(db):
    db._pool = None
    result = await db.async_insert_record(SAMPLE_RECORD)
    assert result is False


async def test_insert_returns_false_in_readonly_mode(db_readonly):
    db_readonly._pool = MagicMock()
    result = await db_readonly.async_insert_record(SAMPLE_RECORD)
    assert result is False


async def test_insert_success(db):
    mock_cursor = AsyncMock()
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_conn)
    db._pool = mock_pool

    result = await db.async_insert_record(SAMPLE_RECORD)
    assert result is True
    mock_cursor.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# record count
# ---------------------------------------------------------------------------

async def test_get_record_count_returns_zero_without_pool(db):
    db._pool = None
    count = await db.async_get_record_count()
    assert count == 0


async def test_get_record_count_returns_value(db):
    mock_cursor = AsyncMock()
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)
    mock_cursor.fetchone = AsyncMock(return_value=(42,))

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_conn)
    db._pool = mock_pool

    count = await db.async_get_record_count()
    assert count == 42


# ---------------------------------------------------------------------------
# available property
# ---------------------------------------------------------------------------

def test_available_false_without_pool(db):
    db._pool = None
    assert db.available is False


def test_available_true_with_pool(db):
    db._pool = MagicMock()
    assert db.available is True
