"""Schema v2: migration, dual-write, revert to 1.15.0b2, resync and compaction.

The revert tests run the *released* 1.15.0b2 store module (a frozen copy in
``tests/fixtures``) against files this version has migrated, so "you can always
reinstall 1.15.0b2" is checked against the real thing, not a re-implementation.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.solcast_solar_enhanced import sqlite_store as store_mod
from custom_components.solcast_solar_enhanced.sqlite_store import (
    _INSERT_SQL,
    CREATE_TABLE_SQL,
    SCHEMA_COMPACTED,
    SCHEMA_V2,
    V2_PROPERTY_NAMES,
    V2_SCALES,
    V2_SITE_NAMES,
    SqliteStore,
)

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_DB = FIXTURES / "reference_ha.db"
T0 = int(datetime(2026, 10, 20, 2, 0, tzinfo=UTC).timestamp())  # 13:00 AEDT, daylight


def _b2_store_class():
    """The SqliteStore class exactly as released in 1.15.0b2."""
    spec = importlib.util.spec_from_file_location(
        "custom_components.solcast_solar_enhanced._sqlite_store_b2", FIXTURES / "sqlite_store_v1_15_0b2.py"
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "custom_components.solcast_solar_enhanced"
    spec.loader.exec_module(module)
    return module.SqliteStore


def _record(epoch: int, site: str = "_total", **overrides) -> dict:
    rec = {
        "period_end": datetime.fromtimestamp(epoch, tz=UTC).isoformat(),
        "period_end_epoch": epoch,
        "period_start": datetime.fromtimestamp(epoch - 1800, tz=UTC).isoformat(),
        "site": site,
        "pv_actual": 3.5,
        "pv_export": 1.2,
        "pv_estimate": 4.0,
        "pv_estimate10": 3.0,
        "pv_estimate90": 5.0,
        "azimuth": 20.0,
        "zenith": 35.0,
        "temp": 22.5,
        "clouds": 10,
        "description": "open-meteo",
        "battery_charge": 0.0,
    }
    rec.update(overrides)
    return rec


def _empty_night(epoch: int, site: str = "_total") -> dict:
    return _record(
        epoch, site, pv_actual=0.0, pv_export=0.0, pv_estimate=0.0, pv_estimate10=0.0, pv_estimate90=0.0, zenith=110.0
    )


def _legacy_file(path: Path, records: list[dict], user_version: int = 1) -> str:
    """A legacy-only store file, as 1.15.0b2 leaves it."""
    conn = sqlite3.connect(path)
    conn.executescript(CREATE_TABLE_SQL)
    conn.executemany(_INSERT_SQL, [SqliteStore._row_values(r) for r in records])
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    conn.close()
    return str(path)


def _upgraded_file(path: Path) -> str:
    """An install from before schema v2 with no rows yet: the legacy table exists but is empty.

    It dual-writes from its first start, exactly as a migrated store does, so the
    soak behaviours (compaction, revert, resync, prune of both schemas) are tested on
    it. A brand-new file starts compacted instead (``test_fresh_store_starts_compacted``).
    """
    return _legacy_file(path, [])


def _history() -> list[dict]:
    """Three slots: a daytime slot, an empty night slot, and a twilight slot where only one array produced."""
    return [
        _record(T0),
        _record(T0, "site-a", pv_actual=2.0),
        _record(T0, "site-b", pv_actual=1.5),
        _empty_night(T0 + 1800),
        _empty_night(T0 + 1800, "site-a"),
        _empty_night(T0 + 1800, "site-b"),
        _empty_night(T0 + 3600, "site-a") | {"pv_actual": 0.0},
        _record(T0 + 3600, pv_actual=0.05, pv_estimate=0.0, pv_estimate90=0.0, zenith=92.0),
        _empty_night(T0 + 3600, "site-b") | {"pv_actual": 0.05},
    ]


async def _open(hass, path: str, migrate: bool = True) -> SqliteStore:
    s = SqliteStore(hass, path)
    assert await s.async_connect() is True
    if migrate:
        await s.async_migrate(-37.9, 145.0)
    return s


def _count(path: str, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fresh store and dual-write
# ---------------------------------------------------------------------------


def _tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    finally:
        conn.close()


async def test_fresh_store_starts_compacted(hass, tmp_path):
    """A new install has no history to protect, so it never creates the legacy table."""
    path = str(tmp_path / "fresh.db")
    s = await _open(hass, path)
    assert s.schema_version == SCHEMA_COMPACTED
    assert not s.dual_writing
    assert await s.async_insert_record(_record(T0, sky_dip_s=420.0, gate_flags=5))
    assert await s.async_insert_record(_record(T0, "site-a", pv_actual=2.0, shade_dip_s=90.0))
    assert not os.path.exists(s.backup_path)  # Nothing was migrated, so nothing to back up.
    await s.async_close()

    assert "solcast_data" not in _tables(path)
    assert _count(path, "slot_site") == 2
    assert _count(path, "slot_property") == 1
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = {r["site"]: r for r in conn.execute("SELECT * FROM slot_rows")}
    conn.close()
    assert rows["_total"]["sky_dip_s"] == 420.0 and rows["_total"]["gate_flags"] == 5
    assert rows["site-a"]["shade_dip_s"] == 90.0 and rows["site-a"]["pv_actual"] == 2.0

    s = await _open(hass, path)  # Every later start leaves it compacted.
    assert s.schema_version == SCHEMA_COMPACTED and not s._has_legacy
    assert len(await s.async_get_records_for_tuning(limit=10, site="_total")) == 1
    await s.async_close()
    assert "solcast_data" not in _tables(path)


async def test_a_failed_create_leaves_a_new_file_that_starts_cleanly(hass, tmp_path):
    """Tables and version are one transaction: a half-built file would otherwise reopen as a legacy store."""
    path = str(tmp_path / "fresh.db")
    with patch.object(store_mod, "V2_VIEW_SQL", "THIS IS NOT SQL"):
        assert await SqliteStore(hass, path).async_connect() is False
    assert _tables(path) == set()
    s = await _open(hass, path)
    assert s.schema_version == SCHEMA_COMPACTED and not s._has_legacy
    await s.async_close()


async def test_an_empty_legacy_table_starts_on_v2_and_dual_writes(hass, tmp_path):
    """An earlier release created the table: that release may be reinstalled, so keep it complete."""
    path = _upgraded_file(tmp_path / "upgraded.db")
    s = await _open(hass, path)
    assert s.schema_version == SCHEMA_V2
    assert s.dual_writing
    assert await s.async_insert_record(_record(T0, sky_dip_s=420.0, gate_flags=5))
    assert await s.async_insert_record(_record(T0, "site-a", pv_actual=2.0, shade_dip_s=90.0))
    await s.async_close()

    assert _count(path, "solcast_data") == 2  # 1.15.0b2's table is complete.
    assert _count(path, "slot_site") == 2
    assert _count(path, "slot_property") == 1  # Property-wide values once per slot.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = {r["site"]: r for r in conn.execute("SELECT * FROM slot_rows")}
    conn.close()
    assert rows["_total"]["sky_dip_s"] == 420.0 and rows["_total"]["gate_flags"] == 5
    assert rows["site-a"]["sky_dip_s"] == 420.0  # Property-wide, shared by the array.
    assert rows["site-a"]["shade_dip_s"] == 90.0 and rows["_total"]["shade_dip_s"] == -1.0
    assert rows["site-a"]["pv_actual"] == 2.0


async def test_missing_gate_values_store_the_unknown_sentinel(hass, tmp_path):
    s = await _open(hass, str(tmp_path / "s.db"))
    await s.async_insert_record(_record(T0))
    row = s._query("SELECT * FROM slot_rows", ())[0]
    for name in ("sky_dip_s", "sky_dip_runs", "export_pinned_s", "vexcess_s", "sample_gap_s", "gate_flags"):
        assert row[name] == -1, name
    assert row["inverter_limited"] == -1 and row["dc_offmpp_min"] == -1
    await s.async_close()


async def test_a_failed_insert_writes_neither_schema(hass, tmp_path):
    path = _upgraded_file(tmp_path / "s.db")
    s = await _open(hass, path)
    with patch.object(store_mod, "_V2_SITE_INSERT", "INSERT INTO no_such_table VALUES (?)"):
        assert await s.async_insert_record(_record(T0)) is False
    assert await s.async_insert_record(_record(T0 + 1800)) is True
    await s.async_close()
    assert _count(path, "solcast_data") == 1
    assert _count(path, "slot_site") == 1


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def test_migration_copies_every_non_empty_slot_and_backs_up_first(hass, tmp_path):
    path = _legacy_file(tmp_path / "legacy.db", _history())
    s = await _open(hass, path)
    assert s.schema_version == SCHEMA_V2 and s.dual_writing
    sites = {r["site"]: r["n"] for r in s._query("SELECT site, COUNT(*) AS n FROM slot_rows GROUP BY site", ())}
    await s.async_close()

    # The daytime slot and the twilight slot (one array produced) are kept whole,
    # every array included; the empty night slot is dropped from all three.
    assert sites == {"_total": 2, "site-a": 2, "site-b": 2}
    assert _count(path, "solcast_data") == 9  # Legacy table untouched.
    backup = path + ".pre-v2.bak"
    assert os.path.exists(backup)
    assert _count(backup, "solcast_data") == 9


async def test_migrated_reads_match_the_legacy_reads(hass, tmp_path):
    """Every estimator query returns the same rows from the view as from the legacy table."""
    path = str(tmp_path / "ref.db")
    shutil.copy(REFERENCE_DB, path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    s = await _open(hass, path)
    assert s.schema_version == SCHEMA_V2

    async def all_reads():
        return (
            await s.async_get_records_for_dampening(152, site="_total"),
            await s.async_get_records_for_tuning(limit=500, site="_total", kt_threshold=0.75),
            await s.async_get_records_for_tuning(limit=500, site="_total", cloud_max=30),
            await s.async_get_records_for_shading(site="_total"),
        )

    v2 = await all_reads()
    s._has_v2 = False  # Force the legacy table.
    legacy = await all_reads()
    await s.async_close()

    for new, old in zip(v2, legacy, strict=True):
        # Same rows, same order; every value equal to within the scale it is stored at.
        assert len(new) == len(old)
        for n_row, o_row in zip(new, old, strict=True):
            assert n_row.keys() == o_row.keys()
            for key, o_val in o_row.items():
                tol = 0.5 / V2_SCALES.get(key, 1) + 1e-9 if isinstance(o_val, float) else 0
                assert n_row[key] == pytest.approx(o_val, abs=tol), key
    assert all(len(x) for x in v2)


async def test_failed_copy_rolls_back_and_stays_on_v1(hass, tmp_path):
    path = _legacy_file(tmp_path / "legacy.db", _history())

    def boom(*_args, **_kwargs):
        raise ValueError("simulated mismatch")

    with patch.object(SqliteStore, "_verify_copy", boom):
        s = await _open(hass, path)
    assert s.schema_version == 1
    assert not s.dual_writing
    assert len(await s.async_get_records_for_dampening(293, site="_total")) == 1  # Still serving from legacy.
    await s.async_close()
    conn = sqlite3.connect(path)
    objects = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert not objects & {"slot_site", "slot_property", "sites", "slot_rows"}


@pytest.mark.parametrize("failure", ["integrity", "disk"])
async def test_preflight_failure_stays_on_v1(hass, tmp_path, failure):
    path = _legacy_file(tmp_path / "legacy.db", _history())
    usage = namedtuple("usage", "total used free")
    patches = {
        "integrity": patch.object(SqliteStore, "_integrity_ok", staticmethod(lambda _conn: False)),
        "disk": patch.object(store_mod.shutil, "disk_usage", lambda _p: usage(10, 10, 0)),
    }
    with patches[failure]:
        s = await _open(hass, path)
    assert s.schema_version == 1
    assert not os.path.exists(path + ".pre-v2.bak")
    await s.async_close()


async def test_migration_runs_once(hass, tmp_path):
    path = _legacy_file(tmp_path / "legacy.db", _history())
    s = await _open(hass, path)
    await s.async_close()
    first = os.path.getmtime(path + ".pre-v2.bak")
    time.sleep(0.01)
    s = await _open(hass, path)
    await s.async_close()
    assert os.path.getmtime(path + ".pre-v2.bak") == first
    assert _count(path, "slot_site") == 6


# ---------------------------------------------------------------------------
# Revert to 1.15.0b2, and back
# ---------------------------------------------------------------------------


async def test_released_b2_store_works_on_a_migrated_file(hass, tmp_path):
    path = _legacy_file(tmp_path / "legacy.db", _history())
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0 + 7200))
    await s.async_close()

    b2 = _b2_store_class()(hass, path)
    assert await b2.async_connect() is True
    assert await b2.async_get_record_count() == 10  # 9 migrated-from + 1 dual-written.
    assert await b2.async_insert_record(_record(T0 + 9000)) is True
    assert await b2.async_migrate(-37.9, 145.0) == 0  # Its repair is skipped at v2.
    # T0, T0+7200 and T0+9000; the night and the zero-forecast twilight rows are excluded.
    assert len(await b2.async_get_records_for_dampening(293, site="_total")) == 3
    assert await b2.async_get_records_for_shading(site="_total")
    await b2.async_close()


async def test_coming_back_from_b2_carries_its_rows_over(hass, tmp_path):
    path = _legacy_file(tmp_path / "legacy.db", _history())
    s = await _open(hass, path)
    await s.async_close()

    b2 = _b2_store_class()(hass, path)
    assert await b2.async_connect() is True
    await b2.async_insert_record(_record(T0 + 7200))
    await b2.async_insert_record(_record(T0 + 7200, "site-a", pv_actual=2.2))
    await b2.async_insert_record(_empty_night(T0 + 9000))  # b2 writes night rows; they are not carried.
    await b2.async_close()

    s = await _open(hass, path)
    rows = s._query("SELECT site, pv_actual FROM slot_rows WHERE period_end_epoch = ?", (T0 + 7200,))
    night = s._query("SELECT COUNT(*) AS n FROM slot_rows WHERE period_end_epoch = ?", (T0 + 9000,))
    await s.async_close()
    assert {r["site"]: r["pv_actual"] for r in rows} == {"_total": 3.5, "site-a": 2.2}
    assert night[0]["n"] == 0


# ---------------------------------------------------------------------------
# Compaction (user-triggered) and after
# ---------------------------------------------------------------------------


async def test_compact_drops_legacy_and_shrinks_the_file(hass, tmp_path):
    path = _upgraded_file(tmp_path / "c.db")
    s = await _open(hass, path)
    for i in range(300):
        await s.async_insert_record(_record(T0 + 1800 * i))
        await s.async_insert_record(_record(T0 + 1800 * i, "site-a", pv_actual=1.0))
    result = await s.async_compact(now_epoch=T0 + 1800 * 300)
    assert result["compacted"] is True
    assert result["size_after_mb"] < result["size_before_mb"]
    assert s.schema_version == SCHEMA_COMPACTED and not s.dual_writing
    assert await s.async_insert_record(_record(T0 + 1800 * 301))
    assert len(await s.async_get_records_for_tuning(limit=1000, site="_total")) == 301
    await s.async_close()

    s = await _open(hass, path)  # Reopening must not bring the legacy table back.
    assert not s._has_legacy
    await s.async_close()
    conn = sqlite3.connect(path)
    assert "solcast_data" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()


async def test_compact_refuses_when_the_schemas_disagree(hass, tmp_path):
    path = _upgraded_file(tmp_path / "c.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0))
    s._conn.execute(_INSERT_SQL, SqliteStore._row_values(_record(T0 + 1800)))  # Legacy-only row.
    s._conn.commit()
    result = await s.async_compact(now_epoch=T0 + 3600)
    assert result["compacted"] is False and "differ" in result["reason"]
    assert s.dual_writing
    await s.async_close()


async def test_compact_twice_is_refused(hass, tmp_path):
    s = await _open(hass, _upgraded_file(tmp_path / "c.db"))
    await s.async_insert_record(_record(T0))
    assert (await s.async_compact(now_epoch=T0))["compacted"] is True
    assert (await s.async_compact(now_epoch=T0)) == {"compacted": False, "reason": "already compacted"}
    await s.async_close()


async def test_released_b2_still_starts_on_a_compacted_file(hass, tmp_path):
    """After compaction b2 loses the history (that is what the backup and tools/downgrade_v2.py are for), but it starts."""
    path = _upgraded_file(tmp_path / "c.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0))
    await s.async_compact(now_epoch=T0)
    await s.async_close()
    b2 = _b2_store_class()(hass, path)
    assert await b2.async_connect() is True
    assert await b2.async_insert_record(_record(T0 + 1800)) is True
    await b2.async_close()


@pytest.mark.parametrize(("age_days", "kept"), [(10, True), (40, False)])
async def test_backup_expires_only_after_compaction_and_30_days(hass, tmp_path, age_days, kept):
    path = _legacy_file(tmp_path / "legacy.db", _history())
    s = await _open(hass, path)
    backup = s.backup_path
    old = time.time() - age_days * 86400
    os.utime(backup, (old, old))
    await s.async_migrate(-37.9, 145.0)  # Not compacted yet: never expires.
    assert os.path.exists(backup)
    # Compaction checks the last 7 days; this history is older, so judge it as of then.
    assert (await s.async_compact(now_epoch=T0 + 3600))["compacted"] is True
    os.utime(backup, (old, old))
    await s.async_migrate(-37.9, 145.0)
    assert os.path.exists(backup) is kept
    await s.async_close()


# ---------------------------------------------------------------------------
# Schema evolution and retention
# ---------------------------------------------------------------------------


async def test_a_new_v2_column_is_added_and_the_view_names_it(hass, tmp_path):
    path = str(tmp_path / "s.db")
    s = await _open(hass, path)
    await s.async_close()
    conn = sqlite3.connect(path)
    conn.executescript("DROP VIEW slot_rows; ALTER TABLE slot_site DROP COLUMN shade_dip_s;")
    conn.commit()
    conn.close()
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0, shade_dip_s=12.0))
    assert s._query("SELECT shade_dip_s FROM slot_rows", ()) == [{"shade_dip_s": 12.0}]
    await s.async_close()


def test_view_exposes_every_v2_column_once():
    conn = sqlite3.connect(":memory:")
    conn.executescript(store_mod.V2_CREATE_SQL)
    conn.execute(store_mod.V2_VIEW_SQL)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(slot_rows)")]
    conn.close()
    assert cols == ["period_end_epoch", "site", "period_end", *V2_SITE_NAMES, *V2_PROPERTY_NAMES]


async def test_prune_removes_old_rows_from_both_schemas(hass, tmp_path):
    path = _upgraded_file(tmp_path / "s.db")
    s = await _open(hass, path)
    now = int(time.time())
    await s.async_insert_record(_record(now - 40 * 86400))
    await s.async_insert_record(_record(now - 40 * 86400, "site-a"))
    await s.async_insert_record(_record(now - 3600))
    assert await s.async_prune(30) == 2
    await s.async_close()
    assert _count(path, "solcast_data") == 1
    assert _count(path, "slot_site") == 1
    assert _count(path, "slot_property") == 1


# ---------------------------------------------------------------------------
# Scaled-integer storage
# ---------------------------------------------------------------------------


async def test_every_v2_value_is_stored_as_an_integer(hass, tmp_path):
    """The whole point of the scaling: no 8-byte REALs in the v2 tables."""
    path = str(tmp_path / "s.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0, pv_actual=3.5123, ghi=812.6, dc_voltage1=372.46, temp=-3.27))
    await s.async_close()
    conn = sqlite3.connect(path)
    for table, names in (("slot_property", V2_PROPERTY_NAMES), ("slot_site", V2_SITE_NAMES)):
        kinds = conn.execute(f"SELECT {', '.join(f'typeof({n})' for n in names)} FROM {table}").fetchone()
        assert set(kinds) == {"integer"}, (table, dict(zip(names, kinds, strict=True)))
    conn.close()


async def test_values_round_trip_to_the_stored_precision(hass, tmp_path):
    s = await _open(hass, str(tmp_path / "s.db"))
    await s.async_insert_record(
        _record(T0, pv_actual=3.5123, ghi=812.6, dc_voltage1=372.46, dc_current1=9.876, zenith=35.123, temp=21.37)
    )
    row = s._query("SELECT * FROM slot_rows", ())[0]
    await s.async_close()
    assert row["pv_actual"] == pytest.approx(3.512)
    assert row["ghi"] == pytest.approx(812.6)  # 0.1 W/m²: two-sample means stay exact.
    assert row["dc_voltage1"] == pytest.approx(372.5)
    assert row["dc_current1"] == pytest.approx(9.88)
    assert row["zenith"] == pytest.approx(35.12)
    assert row["temp"] == pytest.approx(21.4)


async def test_a_negative_value_never_reads_back_as_unknown(hass, tmp_path):
    """-0.1 °C stores as -1, the sentinel's integer. Temperature has no sentinel, so it
    must read back as -0.1; only the UNKNOWN-default columns map -1 to unknown."""
    s = await _open(hass, str(tmp_path / "s.db"))
    await s.async_insert_record(_record(T0, temp=-0.1))
    row = s._query("SELECT temp FROM slot_rows", ())[0]
    await s.async_close()
    assert row["temp"] == pytest.approx(-0.1)


async def test_unknown_survives_every_scale(hass, tmp_path):
    s = await _open(hass, str(tmp_path / "s.db"))
    await s.async_insert_record(_record(T0, export_limit=-1.0, inverter_limited=-1.0, dc_offmpp_min=-1.0))
    await s.async_insert_record(_record(T0 + 1800, export_limit=5.0, inverter_limited=0.0, dc_offmpp_min=0.0))
    rows = s._query("SELECT export_limit, inverter_limited, dc_offmpp_min FROM slot_rows ORDER BY period_end_epoch", ())
    await s.async_close()
    assert rows[0] == {"export_limit": -1.0, "inverter_limited": -1.0, "dc_offmpp_min": -1.0}
    assert rows[1] == {"export_limit": 5.0, "inverter_limited": 0.0, "dc_offmpp_min": 0.0}


@pytest.mark.parametrize("value", [0.0005, 0.0015, 2.5, -2.5, 3.4995, -0.25, 812.5, 0.1 + 0.2])
@pytest.mark.parametrize("scale", [1, 10, 100, 1000])
def test_rounding_matches_sqlite_round(value, scale):
    """Half away from zero, as SQLite's ROUND, so the migration SQL and the write path agree."""
    conn = sqlite3.connect(":memory:")
    expected = conn.execute("SELECT CAST(ROUND(? * ?) AS INTEGER)", (value, scale)).fetchone()[0]
    conn.close()
    assert store_mod.to_stored(value, 0.0, scale) == expected


async def test_migrated_and_dual_written_rows_store_identical_integers(hass, tmp_path):
    rec = _record(T0, pv_actual=3.5125, ghi=812.5, temp=-2.25, dc_voltage1=372.45, export_limit=5.0)
    legacy = _legacy_file(tmp_path / "legacy.db", [rec])
    migrated = await _open(hass, legacy)
    await migrated.async_close()
    fresh = await _open(hass, str(tmp_path / "fresh.db"))
    await fresh.async_insert_record(rec)
    await fresh.async_close()

    def raw(path):
        conn = sqlite3.connect(path)
        out = conn.execute("SELECT * FROM slot_property").fetchall(), conn.execute("SELECT * FROM slot_site").fetchall()
        conn.close()
        return out

    assert raw(legacy) == raw(str(tmp_path / "fresh.db"))


# ---------------------------------------------------------------------------
# Tools that write the legacy table during the soak
# ---------------------------------------------------------------------------


async def test_imported_older_history_reaches_v2_on_the_next_start(hass, tmp_path):
    """An import writes rows *older* than everything in v2; copying only newer rows would miss them."""
    path = _upgraded_file(tmp_path / "s.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0 + 86400))
    await s.async_close()
    conn = sqlite3.connect(path)
    conn.execute(_INSERT_SQL, SqliteStore._row_values(_record(T0 - 86400 * 30)))  # As import_history writes it.
    conn.commit()
    conn.close()
    s = await _open(hass, path)
    epochs = [r["period_end_epoch"] for r in s._query("SELECT period_end_epoch FROM slot_rows ORDER BY 1", ())]
    await s.async_close()
    assert epochs == [T0 - 86400 * 30, T0 + 86400]


async def test_a_backfill_rewrite_reaches_v2_scaled(hass, tmp_path):
    path = _upgraded_file(tmp_path / "s.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0))
    await s.async_close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE solcast_data SET ghi = 812.5, dni = 790.25 WHERE period_end_epoch = ?", (T0,))
    conn.commit()
    assert store_mod.sync_v2_from_legacy(conn, refresh=("ghi", "dni", "dhi")) == 0  # Nothing new to add...
    raw = conn.execute("SELECT ghi, dni FROM slot_property").fetchone()
    view = conn.execute("SELECT ghi, dni FROM slot_rows WHERE site = '_total'").fetchone()
    conn.close()
    assert raw == (8125, 7903)  # ...but the rewritten values arrived, at 0.1 W/m².
    assert view == (pytest.approx(812.5), pytest.approx(790.3))


def test_sync_refuses_a_compacted_store_and_ignores_a_legacy_one(tmp_path):
    legacy = sqlite3.connect(_legacy_file(tmp_path / "legacy.db", [_record(T0)]))
    assert store_mod.sync_v2_from_legacy(legacy) == 0  # No v2 tables: nothing to do.
    assert store_mod.read_source(legacy) == "solcast_data"
    legacy.close()
    compacted = sqlite3.connect(tmp_path / "compacted.db")
    compacted.executescript(store_mod.V2_CREATE_SQL)
    compacted.execute(store_mod.V2_VIEW_SQL)
    assert store_mod.read_source(compacted) == "slot_rows"
    with pytest.raises(ValueError, match="compacted"):
        store_mod.sync_v2_from_legacy(compacted)
    compacted.close()


# ---------------------------------------------------------------------------
# tools/downgrade_v2.py: back to 1.15.0b2 after compaction
# ---------------------------------------------------------------------------


def _downgrade_module():
    spec = importlib.util.spec_from_file_location("downgrade_v2", Path(__file__).parents[1] / "tools" / "downgrade_v2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_downgrade_gives_b2_its_history_back(hass, tmp_path):
    path = _upgraded_file(tmp_path / "c.db")
    s = await _open(hass, path)
    for i in range(5):
        await s.async_insert_record(_record(T0 + 1800 * i, pv_actual=3.5 + i / 10))
        await s.async_insert_record(_record(T0 + 1800 * i, "site-a", pv_actual=1.0 + i / 10))
    assert (await s.async_compact(now_epoch=T0 + 9000))["compacted"] is True
    await s.async_close()

    assert _downgrade_module().downgrade(path) == 10

    b2 = _b2_store_class()(hass, path)
    assert await b2.async_connect() is True
    assert await b2.async_get_record_count() == 10
    rows = await b2.async_get_records_for_tuning(limit=100, site="_total")
    await b2.async_close()
    assert sorted(r["pv_actual"] for r in rows) == pytest.approx([3.5, 3.6, 3.7, 3.8, 3.9])

    s = await _open(hass, path)  # The newer version resumes dual-writing.
    assert s.schema_version == SCHEMA_V2 and s.dual_writing
    await s.async_close()


async def test_downgrade_refuses_a_store_that_does_not_need_it(hass, tmp_path):
    path = _upgraded_file(tmp_path / "s.db")
    s = await _open(hass, path)  # v2, legacy table present.
    await s.async_close()
    with pytest.raises(SystemExit, match="already use it"):
        _downgrade_module().downgrade(path)


# ---------------------------------------------------------------------------
# A brand-new file (compacted from the start): b2, downgrade and the tools
# ---------------------------------------------------------------------------


async def test_b2_on_a_fresh_store_and_back_again(hass, tmp_path):
    """A new install that goes back to b2 and returns keeps what b2 recorded, and stays one copy."""
    path = str(tmp_path / "fresh.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0))
    await s.async_close()

    b2 = _b2_store_class()(hass, path)
    assert await b2.async_connect() is True  # It creates its own, empty, legacy table.
    assert await b2.async_insert_record(_record(T0 + 1800, pv_actual=2.8)) is True
    assert await b2.async_insert_record(_record(T0 + 1800, "site-a", pv_actual=1.4)) is True
    await b2.async_close()

    s = await _open(hass, path)
    rows = s._query("SELECT period_end_epoch, site, pv_actual FROM slot_rows ORDER BY 1, 2", ())
    assert [(r["period_end_epoch"], r["site"], r["pv_actual"]) for r in rows] == [
        (T0, "_total", 3.5),
        (T0 + 1800, "_total", 2.8),
        (T0 + 1800, "site-a", 1.4),
    ]
    assert await s.async_insert_record(_record(T0 + 3600))
    await s.async_close()
    assert _count(path, "slot_property") == 3


async def test_downgrade_of_a_fresh_store_gives_b2_its_history(hass, tmp_path):
    path = str(tmp_path / "fresh.db")
    s = await _open(hass, path)
    for i in range(3):
        await s.async_insert_record(_record(T0 + 1800 * i, pv_actual=3.5 + i / 10))
    await s.async_close()

    assert _downgrade_module().downgrade(path) == 3

    b2 = _b2_store_class()(hass, path)
    assert await b2.async_connect() is True
    rows = await b2.async_get_records_for_tuning(limit=100, site="_total")
    await b2.async_close()
    assert sorted(r["pv_actual"] for r in rows) == pytest.approx([3.5, 3.6, 3.7])

    s = await _open(hass, path)  # Downgraded, so this version dual-writes from here on.
    assert s.schema_version == SCHEMA_V2 and s.dual_writing
    await s.async_close()


def _tool_module(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _import_rows(module, records: list[dict]) -> list[dict]:
    """Records as ``import_history`` holds them after reading a CSV."""
    return [module._normalise({k: str(v) for k, v in r.items()}) for r in records]


@pytest.mark.parametrize("replace", [False, True])
async def test_import_into_a_compacted_store_writes_v2_only(hass, tmp_path, replace):
    path = str(tmp_path / "fresh.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0))
    await s.async_insert_record(_record(T0, "site-a", pv_actual=2.0))
    await s.async_close()

    imp = _tool_module("import_history")
    older = [
        _record(T0 - 86400, pv_export=0.83, temp=17.25),
        _record(T0 - 86400, "site-a", pv_actual=1.7),
        _empty_night(T0 - 86400 + 1800),
        _record(T0, pv_actual=9.9, temp=30.0),  # Same slot as a live row (live temp 22.5).
    ]
    read, inserted, _, deleted = imp._import(
        _import_rows(imp, older), path, -37.9, 145.0,
        recompute_azimuth=False, dry_run=False, replace=replace, sql_out=None,
    )
    assert read == 4
    assert "solcast_data" not in _tables(path)  # No legacy table reappears, so no dual-write restarts.

    s = await _open(hass, path)
    assert s.schema_version == SCHEMA_COMPACTED and not s.dual_writing
    rows = s._query(
        "SELECT period_end_epoch, site, pv_actual, pv_export, temp, ghi FROM slot_rows ORDER BY 1, 2", ()
    )
    await s.async_close()
    got = {(r["period_end_epoch"], r["site"]): r for r in rows}
    assert got[(T0 - 86400, "_total")]["pv_export"] == pytest.approx(0.83)
    assert got[(T0 - 86400, "_total")]["temp"] == pytest.approx(17.3)  # 0.1 °C, half away from zero, as live.
    assert got[(T0 - 86400, "_total")]["ghi"] == 0.0  # Not in a legacy export; tools/backfill_irradiance.py fills it.
    assert got[(T0 - 86400, "site-a")]["pv_actual"] == pytest.approx(1.7)
    assert (T0 - 86400 + 1800, "_total") not in got  # The empty night slot is not kept.
    if replace:
        assert deleted == 2
        assert set(got) == {(T0 - 86400, "_total"), (T0 - 86400, "site-a"), (T0, "_total")}
        assert got[(T0, "_total")]["pv_actual"] == pytest.approx(9.9)  # The import replaced it...
        assert got[(T0, "_total")]["temp"] == pytest.approx(30.0)  # ...the property-wide half too.
        assert inserted == 3
    else:
        assert deleted == 0
        assert got[(T0, "_total")]["pv_actual"] == pytest.approx(3.5)  # Live data wins: INSERT OR IGNORE.
        assert got[(T0, "_total")]["temp"] == pytest.approx(22.5)
        assert got[(T0, "site-a")]["pv_actual"] == pytest.approx(2.0)
        assert inserted == 2


async def test_backfill_into_a_compacted_store_writes_scaled_irradiance(hass, tmp_path, monkeypatch):
    path = str(tmp_path / "fresh.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0))  # No irradiance: backfilled.
    await s.async_insert_record(_record(T0, "site-a", pv_actual=2.0))
    await s.async_insert_record(_record(T0 + 1800, ghi=500.0, dni=400.0, dhi=100.0))  # Has it: left alone.
    await s.async_close()

    bf = _tool_module("backfill_irradiance")
    hour = T0 - 3600
    series = [{"epoch": hour + 3600 * i, "ghi": 800.0, "dni": 790.26, "dhi": 70.04} for i in range(4)]
    monkeypatch.setattr(bf, "fetch_archive", lambda *a: series)
    monkeypatch.setattr("sys.argv", ["backfill_irradiance", "--sqlite", path, "--lat", "-37.9", "--lon", "145.0"])
    assert bf.main() == 0

    assert "solcast_data" not in _tables(path)
    conn = sqlite3.connect(path)
    raw = conn.execute("SELECT ghi, dni, dhi FROM slot_property ORDER BY period_end_epoch").fetchall()
    conn.close()
    assert raw == [(8000, 7903, 700), (5000, 4000, 1000)]  # Scaled at 0.1 W/m², like the live write.
    s = await _open(hass, path)
    rows = {r["site"]: r for r in s._query("SELECT site, ghi FROM slot_rows WHERE period_end_epoch = ?", (T0,))}
    await s.async_close()
    assert rows["_total"]["ghi"] == rows["site-a"]["ghi"] == pytest.approx(800.0)  # Property-wide, on every array.


def test_the_design_document_column_reference_matches_the_code():
    """Every v2 column is documented with its table, scale and missing value, and nothing else is."""
    import re

    text = (Path(__file__).parents[1] / "DESIGN_DOCUMENT.md").read_text(encoding="utf-8")
    section = text.split("#### Column reference", 1)[1].split("####", 1)[0]
    documented = {
        name: (table, int(scale), -1.0 if missing.startswith("−1") else float(missing))
        for name, table, scale, missing in re.findall(
            r"^\| `(\w+)` \| (property|site) \| ×(\d+) \| ([^|]+?) \|", section, re.M
        )
    }
    expected = {name: ("property", scale, default) for name, _, default, scale in store_mod.V2_PROPERTY_COLUMNS}
    expected |= {name: ("site", scale, default) for name, _, default, scale in store_mod.V2_SITE_COLUMNS}
    assert documented == expected


# A subprocess that cannot import Home Assistant, as on a plain Python install.
_WITHOUT_HA = """
import runpy, sys
class _NoHomeAssistant:
    def find_spec(self, name, path=None, target=None):
        if name == "homeassistant" or name.startswith("homeassistant."):
            raise ImportError(f"Home Assistant is not installed: {name}")
sys.meta_path.insert(0, _NoHomeAssistant())
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _run_without_ha(tool: str, *args: str) -> subprocess.CompletedProcess:
    script = str(Path(__file__).parents[1] / "tools" / f"{tool}.py")
    return subprocess.run(
        [sys.executable, "-I", "-c", _WITHOUT_HA, script, *args], capture_output=True, text=True, timeout=60
    )


async def test_the_user_facing_tools_run_without_home_assistant(hass, tmp_path):
    """Users run these against a copied database, often with no Home Assistant installed."""
    path = str(tmp_path / "fresh.db")
    s = await _open(hass, path)
    await s.async_close()
    columns = ["period_end_epoch", "site", "pv_actual", "pv_estimate", "pv_estimate10", "pv_estimate90", "zenith"]
    csv_path = tmp_path / "history.csv"
    csv_path.write_text(",".join(columns) + f"\n{T0},_total,3.5,4.0,3.0,5.0,35.0\n")

    imported = _run_without_ha("import_history", "--sqlite", path, "--csv", str(csv_path), "--no-recompute-azimuth")
    assert imported.returncode == 0, imported.stderr
    assert _count(path, "slot_site") == 1

    empty = str(tmp_path / "empty.db")
    s = await _open(hass, empty)
    await s.async_close()
    backfill = _run_without_ha("backfill_irradiance", "--sqlite", empty, "--lat", "-37.9", "--lon", "145.0")
    assert backfill.returncode == 0, backfill.stderr
    assert "nothing to backfill" in backfill.stdout

    downgraded = _run_without_ha("downgrade_v2", "--sqlite", path)
    assert downgraded.returncode == 0, downgraded.stderr
    assert _count(path, "solcast_data") == 1
