"""A database written by a newer release (a downgrade) is opened read-only, flagged, and never pushed from."""

from __future__ import annotations

import os
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solcast_solar_enhanced.const import (
    CONF_DB_ENABLED,
    DEFAULT_DB_FILENAME,
    DOMAIN,
    ISSUE_SCHEMA_TOO_NEW,
)
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator
from custom_components.solcast_solar_enhanced.sqlite_store import SCHEMA_COMPACTED, SCHEMA_KNOWN, SqliteStore

from .test_store_v2 import T0, _open, _record

FUTURE = SCHEMA_KNOWN + 1


def _future_file(path: str, version: int = FUTURE) -> str:
    """A file a later release wrote: a layout this one has never seen."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE slots_v4 (period_end_epoch INTEGER PRIMARY KEY, payload BLOB)")
    conn.execute("INSERT INTO slots_v4 VALUES (1, x'00')")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return path


def _objects(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def _version(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_known_schema_is_the_compacted_one():
    """Raise SCHEMA_KNOWN with every new layout, or this release would write a file it can't read."""
    assert SCHEMA_KNOWN == SCHEMA_COMPACTED


async def test_a_newer_file_opens_read_only_and_is_left_untouched(hass, tmp_path):
    path = _future_file(str(tmp_path / "f.db"))
    before = _objects(path)
    s = SqliteStore(hass, path)
    assert await s.async_connect() is True
    assert s.newer_schema is True and s.schema_version == FUTURE

    assert await s.async_migrate(-37.9, 145.0) == 0
    assert await s.async_insert_record(_record(T0)) is False
    assert await s.async_prune(1) == 0
    assert (await s.async_compact(now_epoch=T0))["compacted"] is False
    await s.async_close()

    # No legacy table, no v2 tables, no ALTERs, no backup, no version change.
    assert _objects(path) == before
    assert _version(path) == FUTURE
    assert not os.path.exists(s.backup_path)


async def test_the_newest_known_layout_is_not_treated_as_newer(hass, tmp_path):
    """The boundary: a compacted (v3) file is ours, and stays writable."""
    path = str(tmp_path / "c.db")
    s = await _open(hass, path)
    await s.async_insert_record(_record(T0))
    await s.async_compact(now_epoch=T0)
    await s.async_close()
    assert _version(path) == SCHEMA_KNOWN

    s = SqliteStore(hass, path)
    assert await s.async_connect() is True
    assert s.newer_schema is False
    assert await s.async_insert_record(_record(T0 + 1800)) is True
    await s.async_close()


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _own_config_dir(hass, tmp_path):
    """The coordinator opens hass.config.path(DEFAULT_DB_FILENAME); keep each test's file in its own dir.

    The default config dir is shared by the whole run (inside the installed test
    package), so a file left there would leak into every later test.
    """
    hass.config.config_dir = str(tmp_path)


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": -37.9, "longitude": 145.0, "capacity_kw": 8.0},
        options={CONF_DB_ENABLED: True},
        entry_id="guard",
        title="guard",
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_raises_the_issue_and_teardown_clears_it(hass):
    _future_file(hass.config.path(DEFAULT_DB_FILENAME))
    coord = SolcastEnhancedCoordinator(hass, _entry(hass))
    await coord.async_setup()
    try:
        issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SCHEMA_TOO_NEW)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.ERROR
        assert issue.translation_placeholders == {"found": str(FUTURE), "known": str(SCHEMA_KNOWN)}
    finally:
        await coord.async_teardown()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SCHEMA_TOO_NEW) is None


async def test_a_current_file_raises_no_issue(hass):
    coord = SolcastEnhancedCoordinator(hass, _entry(hass))
    await coord.async_setup()
    try:
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SCHEMA_TOO_NEW) is None
    finally:
        await coord.async_teardown()


async def test_a_newer_file_never_pushes_dampening(hass):
    """Pushing from unreadable history sends a neutral curve and undoes the newer release's factors."""
    _future_file(hass.config.path(DEFAULT_DB_FILENAME))
    coord = SolcastEnhancedCoordinator(hass, _entry(hass))
    await coord.async_setup()
    try:
        push = AsyncMock(return_value=True)
        with (
            patch.object(coord, "_read_base_auto_dampen", return_value=False),
            patch.object(coord, "_read_base_granular_factors", return_value=None),
            patch.object(coord, "_push_dampening", push),
        ):
            await coord._run_dampening(dict(coord._opts), int(time.time()), -37.9, 145.0)
        push.assert_not_awaited()
    finally:
        await coord.async_teardown()


async def test_a_current_file_still_pushes(hass):
    """The control for the test above: the same call on a healthy store does push."""
    coord = SolcastEnhancedCoordinator(hass, _entry(hass))
    coord._sites = [{"resource_id": "aaaa-bbbb"}]
    await coord.async_setup()
    try:
        push = AsyncMock(return_value=True)
        with (
            patch.object(coord, "_read_base_auto_dampen", return_value=False),
            patch.object(coord, "_read_base_granular_factors", return_value=None),
            patch.object(coord, "_push_dampening", push),
            patch.object(coord, "_discover_sites", return_value=coord._sites, create=True),
        ):
            await coord._run_dampening(dict(coord._opts), int(time.time()), -37.9, 145.0)
        push.assert_awaited()
    finally:
        await coord.async_teardown()
