"""Test the shared entity bases in entity.py.

These paths are hard to reach through the concrete sensors — the restore bridge
only runs while an entity is being added to hass, and the coordinator-update
callback is invoked by the coordinator rather than by any test that reads a
``native_value``. They are exercised here directly.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.solcast_solar_enhanced.const import DOMAIN
from custom_components.solcast_solar_enhanced.entity import (
    RestoringSensorEntity,
    SolcastEnhancedEntity,
    SolcastEnhancedSiteEntity,
)


def _entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "abc123"
    return entry


# ---------------------------------------------------------------------------
# SolcastEnhancedEntity
# ---------------------------------------------------------------------------

def test_unique_id_and_device_identify_the_integration():
    e = SolcastEnhancedEntity(MagicMock(), _entry(), "some_key")
    assert e.unique_id == f"{DOMAIN}_abc123_some_key"
    assert e.device_info["identifiers"] == {(DOMAIN, "abc123")}


def test_coordinator_update_refreshes_cache_then_writes_state():
    """The callback must refresh cached state *before* publishing it.

    Writing first would publish the previous value on every update, so the order
    is asserted rather than just the two calls happening.
    """
    calls: list[str] = []
    e = SolcastEnhancedEntity(MagicMock(), _entry(), "k")
    with (
        patch.object(
            SolcastEnhancedEntity, "_update_from_coordinator", side_effect=lambda: calls.append("refresh")
        ),
        patch.object(SolcastEnhancedEntity, "async_write_ha_state", side_effect=lambda: calls.append("write")),
    ):
        e._handle_coordinator_update()
    assert calls == ["refresh", "write"]


def test_base_update_from_coordinator_is_a_no_op():
    """The default hook exists for subclasses to override and must not raise."""
    assert SolcastEnhancedEntity(MagicMock(), _entry(), "k")._update_from_coordinator() is None


# ---------------------------------------------------------------------------
# RestoringSensorEntity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (SimpleNamespace(native_value=4.25), 4.25),
        (SimpleNamespace(native_value="3.5"), 3.5),  # recorder may hand back a string
        (SimpleNamespace(native_value=None), None),  # nothing usable stored
        (SimpleNamespace(native_value="unknown"), None),  # unparseable → ignored, not raised
        (None, None),  # recorder kept nothing at all
    ],
)
async def test_restore_bridges_the_gap_after_a_restart(stored, expected):
    """The last recorded value is adopted when it parses, and never crashes setup."""
    e = RestoringSensorEntity(MagicMock(), _entry(), "k")
    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()),
        patch.object(RestoringSensorEntity, "async_get_last_sensor_data", new=AsyncMock(return_value=stored)),
    ):
        await e.async_added_to_hass()
    assert e._restored_value == expected
    # With no live value the restored one is what the sensor reports.
    assert e.native_value == expected


def test_live_value_defaults_to_none():
    """Subclasses supply the live reading; the base has none of its own."""
    assert RestoringSensorEntity(MagicMock(), _entry(), "k")._live_value() is None


# ---------------------------------------------------------------------------
# SolcastEnhancedSiteEntity
# ---------------------------------------------------------------------------

def test_site_entity_gets_its_own_device_linked_to_the_main_one():
    e = SolcastEnhancedSiteEntity(MagicMock(), _entry(), "site-1", "Ground Array", "site_output")
    assert e.unique_id == f"{DOMAIN}_abc123_site_output_site-1"
    assert e.device_info["identifiers"] == {(DOMAIN, "abc123_site-1")}
    assert e.device_info["name"] == "Ground Array"
    assert e.device_info["via_device"] == (DOMAIN, "abc123")
