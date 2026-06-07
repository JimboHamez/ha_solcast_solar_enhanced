"""Tests for the built-in SQLite storage backend (real temp-file DB)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.solcast_solar_enhanced.sqlite_store import SqliteStore


def _record(epoch: int, site: str = "_total", **overrides):
    """Build a record dict for the given period_end epoch."""
    end = datetime.fromtimestamp(epoch, tz=timezone.utc)
    start = datetime.fromtimestamp(epoch - 1800, tz=timezone.utc)
    rec = {
        "period_end": end.isoformat(),
        "period_end_epoch": epoch,
        "period_start": start.isoformat(),
        "site": site,
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
    rec.update(overrides)
    return rec


# Epoch for 2024-06-01T12:00:00Z — day-of-year 153.
JUNE1 = int(datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp())
JUNE1_DOY = 153


@pytest.fixture
async def store(hass, tmp_path):
    s = SqliteStore(hass, str(tmp_path / "test.db"))
    assert await s.async_connect() is True
    yield s
    await s.async_close()


# ---------------------------------------------------------------------------
# connect / schema
# ---------------------------------------------------------------------------

async def test_connect_creates_schema_and_is_available(store):
    assert store.available is True
    assert await store.async_get_record_count() == 0


# ---------------------------------------------------------------------------
# Phase-2 per-MPPT DC telemetry capture
# ---------------------------------------------------------------------------

_DC_COLS = "dc_voltage1, dc_current1, dc_voltage2, dc_current2"


async def test_insert_and_read_dc_telemetry(store):
    await store.async_insert_record(_record(
        JUNE1, dc_voltage1=412.5, dc_current1=6.2, dc_voltage2=398.0, dc_current2=5.1,
    ))
    rows = store._query(f"SELECT {_DC_COLS} FROM solcast_data WHERE site = ?", ("_total",))
    assert rows == [{
        "dc_voltage1": 412.5, "dc_current1": 6.2,
        "dc_voltage2": 398.0, "dc_current2": 5.1,
    }]


async def test_insert_dc_telemetry_defaults_to_zero(store):
    # A record without DC fields stores 0 via NOT NULL DEFAULT (no crash).
    await store.async_insert_record(_record(JUNE1))
    rows = store._query(f"SELECT {_DC_COLS} FROM solcast_data", ())
    assert rows == [{
        "dc_voltage1": 0.0, "dc_current1": 0.0, "dc_voltage2": 0.0, "dc_current2": 0.0,
    }]


async def test_connect_adds_dc_columns_to_legacy_db(hass, tmp_path):
    """An existing DB created before the DC columns gets them ALTERed in, with
    legacy rows backfilled to 0, and accepts new DC-bearing inserts."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    legacy_sql = """
        CREATE TABLE solcast_data (
          "index" INTEGER PRIMARY KEY AUTOINCREMENT,
          period_end TEXT NOT NULL, period_end_epoch INTEGER NOT NULL,
          period_start TEXT NOT NULL, site TEXT NOT NULL DEFAULT '_total',
          pv_actual REAL NOT NULL, pv_export REAL NOT NULL DEFAULT 0,
          pv_estimate REAL NOT NULL, pv_estimate10 REAL NOT NULL,
          pv_estimate90 REAL NOT NULL, azimuth REAL NOT NULL, zenith REAL NOT NULL,
          temp REAL NOT NULL, clouds INTEGER NOT NULL, description TEXT NOT NULL,
          battery_charge REAL NOT NULL DEFAULT 0,
          UNIQUE(period_end_epoch, site)
        );
    """
    con = sqlite3.connect(path)
    con.executescript(legacy_sql)
    con.execute(
        'INSERT INTO solcast_data (period_end, period_end_epoch, period_start, site,'
        " pv_actual, pv_estimate, pv_estimate10, pv_estimate90, azimuth, zenith,"
        " temp, clouds, description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("x", JUNE1, "y", "_total", 3.0, 4.0, 3.0, 5.0, 180.0, 35.0, 20.0, 10, "clear"),
    )
    con.commit()
    con.close()

    s = SqliteStore(hass, path)
    assert await s.async_connect() is True
    # All four columns now present; the legacy row backfilled to 0.
    assert s._query(f"SELECT {_DC_COLS} FROM solcast_data", ()) == [{
        "dc_voltage1": 0.0, "dc_current1": 0.0, "dc_voltage2": 0.0, "dc_current2": 0.0,
    }]
    # And a new DC-bearing insert round-trips.
    await s.async_insert_record(_record(JUNE1 + 1800, dc_voltage1=400.0, dc_current1=5.0))
    assert s._query(
        "SELECT dc_voltage1 FROM solcast_data WHERE period_end_epoch = ?",
        (JUNE1 + 1800,),
    ) == [{"dc_voltage1": 400.0}]
    await s.async_close()


# ---------------------------------------------------------------------------
# azimuth repair migration
# ---------------------------------------------------------------------------

# 2026-06-04T23:30:00Z period end → midpoint 23:15Z = 09:15 AEST. The old bug
# stored ~316.46° (NW); the correct morning azimuth is ~43.5° (NE).
_MELB_LAT, _MELB_LON = -37.9, 145.0
_MORNING_EPOCH = 1780615800


async def _azimuth_of(store, epoch):
    recs = await store.async_get_records_for_tuning()
    for r in recs:
        # tuning query doesn't return epoch, so match on the single inserted row
        return r["azimuth"]
    return None


async def test_migrate_repairs_wrong_azimuth(store):
    await store.async_insert_record(_record(_MORNING_EPOCH, azimuth=316.46))
    changed = await store.async_migrate(_MELB_LAT, _MELB_LON)
    assert changed == 1
    fixed = await _azimuth_of(store, _MORNING_EPOCH)
    assert fixed == pytest.approx(43.5, abs=1.0)
    assert 0.0 <= fixed <= 180.0  # eastern half, not the mirrored ~316°


async def test_migrate_is_idempotent(store):
    await store.async_insert_record(_record(_MORNING_EPOCH, azimuth=316.46))
    assert await store.async_migrate(_MELB_LAT, _MELB_LON) == 1
    # Second run is gated by user_version → no rescan, nothing changed.
    assert await store.async_migrate(_MELB_LAT, _MELB_LON) == 0


async def test_migrate_leaves_correct_rows_untouched(store):
    from custom_components.solcast_solar_enhanced.pv_tuning import solar_position

    correct = round(solar_position(_MORNING_EPOCH - 900, _MELB_LAT, _MELB_LON)[0], 5)
    await store.async_insert_record(_record(_MORNING_EPOCH, azimuth=correct))
    assert await store.async_migrate(_MELB_LAT, _MELB_LON) == 0
    assert await _azimuth_of(store, _MORNING_EPOCH) == pytest.approx(correct)


async def test_connect_failure_returns_false(hass):
    # A path inside a non-existent directory cannot be opened.
    s = SqliteStore(hass, "/nonexistent-dir-xyz/sub/test.db")
    assert await s.async_connect() is False
    assert s.available is False


# ---------------------------------------------------------------------------
# insert + dedupe
# ---------------------------------------------------------------------------

async def test_insert_and_count(store):
    assert await store.async_insert_record(_record(JUNE1)) is True
    assert await store.async_get_record_count() == 1


async def test_duplicate_epoch_site_ignored(store):
    await store.async_insert_record(_record(JUNE1, pv_actual=3.5))
    # Same (epoch, site) — INSERT OR IGNORE keeps the first row.
    await store.async_insert_record(_record(JUNE1, pv_actual=9.9))
    assert await store.async_get_record_count() == 1
    rows = await store.async_get_records_for_tuning()
    assert rows[0]["pv_actual"] == pytest.approx(3.5)


async def test_same_epoch_different_site_both_stored(store):
    await store.async_insert_record(_record(JUNE1, site="_total"))
    await store.async_insert_record(_record(JUNE1, site="abcd-1234"))
    assert await store.async_get_record_count() == 2


async def test_insert_missing_field_returns_false(store):
    bad = _record(JUNE1)
    del bad["pv_estimate"]
    assert await store.async_insert_record(bad) is False
    assert await store.async_get_record_count() == 0


async def test_readonly_refuses_insert(hass, tmp_path):
    # Seed a row with a writable store first.
    path = str(tmp_path / "ro.db")
    writer = SqliteStore(hass, path)
    await writer.async_connect()
    await writer.async_insert_record(_record(JUNE1))
    await writer.async_close()

    ro = SqliteStore(hass, path, readonly=True)
    await ro.async_connect()
    assert await ro.async_insert_record(_record(JUNE1 + 1800)) is False
    assert await ro.async_get_record_count() == 1
    await ro.async_close()


# ---------------------------------------------------------------------------
# repeated inserts accumulate / re-run is idempotent
# ---------------------------------------------------------------------------

async def test_repeated_inserts_accumulate(store):
    for i in range(5):
        await store.async_insert_record(_record(JUNE1 + i * 1800))
    assert await store.async_get_record_count() == 5
    # Re-inserting the same slots is a no-op (INSERT OR IGNORE).
    for i in range(5):
        await store.async_insert_record(_record(JUNE1 + i * 1800))
    assert await store.async_get_record_count() == 5


# ---------------------------------------------------------------------------
# tuning query
# ---------------------------------------------------------------------------

async def test_tuning_orders_desc_and_limits(store):
    for i in range(5):
        await store.async_insert_record(_record(JUNE1 + i * 1800))
    rows = await store.async_get_records_for_tuning(limit=3)
    assert len(rows) == 3
    # Returned newest-first; tuning rows expose the consumed columns only.
    assert set(rows[0]) == {
        "pv_actual", "pv_export", "pv_estimate",
        "azimuth", "zenith", "clouds", "battery_charge",
    }


async def test_tuning_excludes_zero_actual(store):
    await store.async_insert_record(_record(JUNE1, pv_actual=0.0))
    await store.async_insert_record(_record(JUNE1 + 1800, pv_actual=2.0))
    rows = await store.async_get_records_for_tuning()
    assert len(rows) == 1
    assert rows[0]["pv_actual"] == pytest.approx(2.0)


async def test_tuning_site_filter(store):
    await store.async_insert_record(_record(JUNE1, site="_total"))
    await store.async_insert_record(_record(JUNE1, site="siteA"))
    rows = await store.async_get_records_for_tuning(site="siteA")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# dampening day-of-year window
# ---------------------------------------------------------------------------

async def test_dampening_doy_window_includes_and_excludes(store):
    # In-window: same day and +10 days. Out-of-window: +40 days.
    await store.async_insert_record(_record(JUNE1))
    await store.async_insert_record(_record(JUNE1 + 10 * 86400))
    await store.async_insert_record(_record(JUNE1 + 40 * 86400))
    rows = await store.async_get_records_for_dampening(JUNE1_DOY, window_days=14)
    assert len(rows) == 2


async def test_dampening_excludes_zero_actual_or_estimate(store):
    await store.async_insert_record(_record(JUNE1, pv_actual=0.0))
    await store.async_insert_record(_record(JUNE1 + 1800, pv_estimate=0.0))
    await store.async_insert_record(_record(JUNE1 + 3600, pv_actual=2.0, pv_estimate=3.0))
    rows = await store.async_get_records_for_dampening(JUNE1_DOY, window_days=14)
    assert len(rows) == 1


async def test_dampening_site_filter(store):
    await store.async_insert_record(_record(JUNE1, site="_total"))
    await store.async_insert_record(_record(JUNE1, site="siteB"))
    rows = await store.async_get_records_for_dampening(
        JUNE1_DOY, window_days=14, site="siteB"
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# sites
# ---------------------------------------------------------------------------

async def test_get_sites_distinct(store):
    await store.async_insert_record(_record(JUNE1, site="_total"))
    await store.async_insert_record(_record(JUNE1 + 1800, site="_total"))
    await store.async_insert_record(_record(JUNE1, site="siteC"))
    assert set(await store.async_get_sites()) == {"_total", "siteC"}


# ---------------------------------------------------------------------------
# retention / prune
# ---------------------------------------------------------------------------

import time as _time


async def test_prune_removes_only_old_rows(store):
    now = int(_time.time())
    old = now - 500 * 86400          # 500 days ago
    recent = now - 10 * 86400        # 10 days ago
    await store.async_insert_record(_record(old))
    await store.async_insert_record(_record(recent))
    removed = await store.async_prune(retention_days=400)
    assert removed == 1
    rows = await store.async_get_records_for_tuning()
    assert len(rows) == 1            # the recent row survives


async def test_prune_zero_is_noop(store):
    now = int(_time.time())
    await store.async_insert_record(_record(now - 9000 * 86400))
    assert await store.async_prune(retention_days=0) == 0
    assert await store.async_get_record_count() == 1


async def test_prune_negative_is_noop(store):
    now = int(_time.time())
    await store.async_insert_record(_record(now - 9000 * 86400))
    assert await store.async_prune(retention_days=-5) == 0
    assert await store.async_get_record_count() == 1


async def test_prune_nothing_to_remove(store):
    now = int(_time.time())
    await store.async_insert_record(_record(now - 10 * 86400))
    assert await store.async_prune(retention_days=400) == 0
    assert await store.async_get_record_count() == 1


async def test_prune_readonly_is_noop(hass, tmp_path):
    path = str(tmp_path / "ro.db")
    writer = SqliteStore(hass, path)
    assert await writer.async_connect() is True
    await writer.async_insert_record(_record(int(_time.time()) - 9000 * 86400))
    await writer.async_close()

    ro = SqliteStore(hass, path, readonly=True)
    assert await ro.async_connect() is True
    try:
        assert await ro.async_prune(retention_days=1) == 0
        assert await ro.async_get_record_count() == 1
    finally:
        await ro.async_close()


async def test_tuning_query_cloud_max_filters_in_sql(store):
    # Two clear rows + two cloudy rows; cloud_max should keep only clear ones,
    # applied before the LIMIT so a cloudy recent window can't crowd out clear data.
    await store.async_insert_record(_record(JUNE1 + 0,    clouds=5))
    await store.async_insert_record(_record(JUNE1 + 1800, clouds=10))
    await store.async_insert_record(_record(JUNE1 + 3600, clouds=80))
    await store.async_insert_record(_record(JUNE1 + 5400, clouds=100))
    rows = await store.async_get_records_for_tuning(cloud_max=20)
    assert len(rows) == 2
    assert all(r["clouds"] < 20 for r in rows)


async def test_tuning_query_clear_beats_recent_window(store):
    # 3 recent cloudy rows + 1 older clear row; with a small limit and cloud_max
    # the clear row survives (clear-sky filter happens before LIMIT).
    await store.async_insert_record(_record(JUNE1,          clouds=0))    # older, clear
    await store.async_insert_record(_record(JUNE1 + 86400,  clouds=90))   # newer, cloudy
    await store.async_insert_record(_record(JUNE1 + 172800, clouds=90))
    rows = await store.async_get_records_for_tuning(limit=2, cloud_max=20)
    assert [r["clouds"] for r in rows] == [0]


async def test_tuning_query_no_cloud_max_keeps_all_weather(store):
    await store.async_insert_record(_record(JUNE1, clouds=5))
    await store.async_insert_record(_record(JUNE1 + 1800, clouds=95))
    rows = await store.async_get_records_for_tuning()
    assert len(rows) == 2
