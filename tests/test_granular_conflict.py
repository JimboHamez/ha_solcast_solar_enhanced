"""Defences against a base granular-dampening table that breaks our per-site push.

The base keys granular dampening by resource_id, but `dampen.py::get_factor` applies an
`all` entry in preference to *every* per-site entry, and `granular_data()` discards the
whole table if its sites disagree on factor count. Both failures are silent, so this
integration has to detect them itself.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.solcast_solar_enhanced.const import (
    CONF_DAMPENING_GATE,
    CONF_SITE_GROUPS,
    DOMAIN,
    ISSUE_GRANULAR_CONFLICT,
)
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator

RID_A = "aaaa-1111"
RID_B = "bbbb-2222"


@pytest.fixture
def coordinator(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, mock_config_entry)


# ---------------------------------------------------------------------------
# _granular_conflict — pure classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("factors", "expected"),
    [
        (None, None),
        ({}, None),
        # Healthy: our own two sites, both at the 24 we push.
        ({RID_A: [1.0] * 24, RID_B: [0.9] * 24}, None),
        # 'all' shadows every per-site entry, even when the per-site ones look right.
        ({"all": [1.0] * 48, RID_A: [0.9] * 24}, "all_key"),
        ({"all": [1.0] * 48}, "all_key"),
        # A foreign site at 48 makes the base reject the table once we add our 24.
        ({RID_A: [1.0] * 48}, "length_mismatch"),
        ({RID_A: [1.0] * 24, RID_B: [1.0] * 48}, "length_mismatch"),
    ],
)
async def test_granular_conflict_classification(coordinator, factors, expected):
    assert coordinator._granular_conflict(factors) == expected


async def test_granular_conflict_ignores_non_list_values(coordinator):
    """The table is another integration's data — a stray non-list must not raise."""
    assert coordinator._granular_conflict({RID_A: [1.0] * 24, "meta": "junk"}) is None


# ---------------------------------------------------------------------------
# _read_base_granular_factors — defensive traversal of base internals
# ---------------------------------------------------------------------------


async def test_read_base_granular_factors_none_when_base_absent(coordinator):
    assert coordinator._read_base_granular_factors() is None


async def test_read_base_granular_factors_survives_missing_attrs(hass, coordinator):
    """Every hop is a getattr on another integration's internals; a shape change must
    degrade to 'no check' rather than break the dampening push."""
    with patch.object(hass.config_entries, "async_entries", return_value=[SimpleNamespace(runtime_data=None)]):
        assert coordinator._read_base_granular_factors() is None


# ---------------------------------------------------------------------------
# _run_dampening — what the conflicts actually do to the push
# ---------------------------------------------------------------------------


async def _run(hass, coordinator, factors):
    """Drive _run_dampening for a two-site setup, returning the sites pushed."""
    opts = {
        CONF_SITE_GROUPS: [
            {"ac_sensor": "sensor.inv", "site": RID_A},
            {"ac_sensor": "sensor.inv", "site": RID_B},
        ],
        CONF_DAMPENING_GATE: False,
    }
    pushed: list[str | None] = []

    async def fake_push(hourly, site=None):
        pushed.append(site)

    with (
        patch.object(coordinator, "_compute_dampening_slots", AsyncMock(return_value=[{"factor": 0.8}] * 48)),
        patch.object(coordinator, "_read_base_auto_dampen", return_value=False),
        patch.object(coordinator, "_read_base_granular_factors", return_value=factors),
        patch.object(coordinator, "_push_dampening", side_effect=fake_push),
    ):
        await coordinator._run_dampening(opts, int(time.time()), -37.9, 145.0)
    return pushed


async def test_length_mismatch_skips_push_and_raises_issue(hass, coordinator):
    """Pushing 24 into a 48-factor table makes the base bin the lot, disabling
    dampening for every site — including ones we don't manage. So don't push."""
    pushed = await _run(hass, coordinator, {"foreign-site": [1.0] * 48})

    assert pushed == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_GRANULAR_CONFLICT) is not None


async def test_all_key_still_pushes_but_raises_issue(hass, coordinator):
    """An 'all' entry makes our factors inert, but pushing is harmless and means they
    are already correct the moment it is removed — so warn without withholding."""
    pushed = await _run(hass, coordinator, {"all": [1.0] * 48})

    assert pushed == [RID_A, RID_B]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_GRANULAR_CONFLICT) is not None


async def test_healthy_table_pushes_and_clears_issue(hass, coordinator):
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_GRANULAR_CONFLICT,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_GRANULAR_CONFLICT,
    )
    pushed = await _run(hass, coordinator, {RID_A: [1.0] * 24, RID_B: [0.9] * 24})

    assert pushed == [RID_A, RID_B]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_GRANULAR_CONFLICT) is None


async def test_empty_table_pushes_normally(hass, coordinator):
    """A fresh install has no granular file at all; that is not a conflict."""
    assert await _run(hass, coordinator, {}) == [RID_A, RID_B]
