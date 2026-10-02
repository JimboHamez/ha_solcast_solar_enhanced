"""Defences against a base granular-dampening table that breaks our per-site push.

The base keys granular dampening by resource_id, but `dampen.py::get_factor` applies an
`all` entry in preference to *every* per-site entry, and `granular_data()` discards the
whole table if its sites disagree on factor count. Both failures are silent, so this
integration has to detect them itself. Also covers how the push is addressed: always
by site id, at 48 factors unless a site we don't manage holds 24.
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
    DEFAULT_SITE_ID,
    DOMAIN,
    HOURLY_FACTOR_COUNT,
    ISSUE_GRANULAR_CONFLICT,
    PUSH_FACTOR_COUNT,
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
    ("factors", "own_ids", "expected"),
    [
        (None, [RID_A], None),
        ({}, [RID_A], None),
        # Our own entries are about to be overwritten, so their count is irrelevant —
        # including the 24-factor entries every install holds before its first 48 push.
        ({RID_A: [1.0] * 24, RID_B: [0.9] * 24}, [RID_A, RID_B], None),
        ({RID_A: [1.0] * 24, RID_B: [0.9] * 48}, [RID_A, RID_B], None),
        # Base keys are lowercase and hyphenated; ours must match them that way.
        ({"AAAA_1111": [1.0] * 24}, [RID_A], None),
        # 'all' shadows every per-site entry, even when the per-site ones look right.
        ({"all": [1.0] * 48, RID_A: [0.9] * 24}, [RID_A], "all_key"),
        ({"all": [1.0] * 48}, [RID_A], "all_key"),
        # A single consistent foreign count is matched, not refused.
        ({"foreign": [1.0] * 48}, [RID_A], None),
        ({"foreign": [1.0] * 24}, [RID_A], None),
        # Foreign sites that disagree, or hold a count the base rejects, make the base
        # bin the table whatever we push.
        ({"f1": [1.0] * 24, "f2": [1.0] * 48}, [RID_A], "length_mismatch"),
        ({"foreign": [1.0] * 30}, [RID_A], "length_mismatch"),
    ],
)
async def test_granular_conflict_classification(coordinator, factors, own_ids, expected):
    assert coordinator._granular_conflict(factors, own_ids) == expected


@pytest.mark.parametrize(
    ("factors", "expected"),
    [
        (None, PUSH_FACTOR_COUNT),
        ({}, PUSH_FACTOR_COUNT),
        # Our own hourly entries from before the upgrade do not hold us at 24.
        ({RID_A: [1.0] * 24, RID_B: [1.0] * 24}, PUSH_FACTOR_COUNT),
        ({"foreign": [1.0] * 48}, PUSH_FACTOR_COUNT),
        # A site we don't manage at 24 forces 24, or the base would discard the table.
        ({RID_A: [1.0] * 24, "foreign": [1.0] * 24}, HOURLY_FACTOR_COUNT),
    ],
)
async def test_push_factor_count(coordinator, factors, expected):
    assert coordinator._push_factor_count(factors, [RID_A, RID_B]) == expected


async def test_granular_conflict_ignores_non_list_values(coordinator):
    """The table is another integration's data — a stray non-list must not raise."""
    assert coordinator._granular_conflict({RID_A: [1.0] * 24, "meta": "junk"}, [RID_A]) is None


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


async def _run(hass, coordinator, factors, *, multi_site=True, sites=None, traditional=None, push_ok=True):
    """Drive _run_dampening, returning ``(site, factors)`` for every push in order.

    ``multi_site`` configures two site groups; without it the property-wide curve is
    pushed to each discovered site in ``sites``. ``traditional`` stands in for the
    base's damp00..23 options.
    """
    opts = {CONF_DAMPENING_GATE: False}
    if multi_site:
        opts[CONF_SITE_GROUPS] = [
            {"ac_sensor": "sensor.inv", "site": RID_A},
            {"ac_sensor": "sensor.inv", "site": RID_B},
        ]
    coordinator._sites = [{"resource_id": rid} for rid in (sites or [])]
    # Distinct per-slot factors, so a dropped, duplicated or reordered slot shows.
    slots = [{"factor": round(0.5 + i / 100, 4)} for i in range(48)]
    coordinator._dampening_table = slots
    pushed: list[tuple[str | None, list[float]]] = []

    async def fake_push(factors_, site=None):
        pushed.append((site, list(factors_)))
        return push_ok

    with (
        patch.object(coordinator, "_compute_dampening_slots", AsyncMock(return_value=slots)),
        patch.object(coordinator, "_read_base_auto_dampen", return_value=False),
        patch.object(coordinator, "_read_base_granular_factors", return_value=factors),
        patch.object(coordinator, "_read_base_traditional_factors", return_value=traditional),
        patch.object(coordinator, "_push_dampening", side_effect=fake_push),
    ):
        await coordinator._run_dampening(opts, int(time.time()), -37.9, 145.0)
    return pushed


SLOTS = [round(0.5 + i / 100, 4) for i in range(48)]
HOURLY = [round((SLOTS[2 * h] + SLOTS[2 * h + 1]) / 2, 6) for h in range(24)]


def _sites(pushed):
    return [site for site, _ in pushed]


async def test_upgrade_from_our_own_hourly_entries_pushes_48(hass, coordinator):
    """The first cycle after upgrading finds our own 24-factor entries. Treating those
    as a clash would skip the push every cycle, freezing the last hourly curve."""
    pushed = await _run(hass, coordinator, {RID_A: [1.0] * 24, RID_B: [0.9] * 24})

    assert _sites(pushed) == [RID_A, RID_B]
    assert all(factors == SLOTS for _, factors in pushed)
    assert coordinator._pushed_factor_count == {RID_A: PUSH_FACTOR_COUNT, RID_B: PUSH_FACTOR_COUNT}
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_GRANULAR_CONFLICT) is None


async def test_foreign_hourly_entry_forces_hourly_push(hass, coordinator):
    """A hand-kept 24-factor site we don't manage: match it rather than refuse."""
    pushed = await _run(hass, coordinator, {"foreign-site": [1.0] * 24})

    assert _sites(pushed) == [RID_A, RID_B]
    assert all(factors == pytest.approx(HOURLY) for _, factors in pushed)
    assert coordinator._pushed_factor_count[RID_A] == HOURLY_FACTOR_COUNT


async def test_length_mismatch_skips_push_and_raises_issue(hass, coordinator):
    """Foreign sites that already disagree make the base bin the whole table, so
    adding ours cannot help — don't push."""
    pushed = await _run(hass, coordinator, {"f1": [1.0] * 24, "f2": [1.0] * 48})

    assert pushed == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_GRANULAR_CONFLICT) is not None


async def test_all_key_still_pushes_but_raises_issue(hass, coordinator):
    """An 'all' entry makes our factors inert, but pushing is harmless and means they
    are already correct the moment it is removed — so warn without withholding."""
    pushed = await _run(hass, coordinator, {"all": [1.0] * 48})

    assert _sites(pushed) == [RID_A, RID_B]
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
    pushed = await _run(hass, coordinator, {RID_A: [1.0] * 48, RID_B: [0.9] * 48})

    assert _sites(pushed) == [RID_A, RID_B]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_GRANULAR_CONFLICT) is None


async def test_empty_table_pushes_normally(hass, coordinator):
    """A fresh install has no granular file at all; that is not a conflict."""
    assert _sites(await _run(hass, coordinator, {})) == [RID_A, RID_B]


# ---------------------------------------------------------------------------
# No site groups — the property-wide curve, sent to each discovered site
# ---------------------------------------------------------------------------


async def test_no_groups_pushes_property_curve_to_every_discovered_site(hass, coordinator):
    """Single-site, or several Solcast sites behind one meter: every push names a site
    and carries all 48 slots. A site-less push would land in damp00..23 (hourly) or
    'all' (which shadows per-site entries if arrays are configured later)."""
    pushed = await _run(hass, coordinator, {}, multi_site=False, sites=[RID_A, RID_B], traditional=[1.0] * 24)

    assert _sites(pushed) == [RID_A, RID_B]
    assert all(factors == SLOTS for _, factors in pushed)
    assert coordinator._dampening_pushed == {DEFAULT_SITE_ID}
    assert coordinator._pushed_factor_count == {DEFAULT_SITE_ID: PUSH_FACTOR_COUNT}


async def test_no_groups_without_discovered_sites_falls_back_to_global_hourly(hass, coordinator):
    """No site to name (auto-discovery off): keep the base's hourly global factors."""
    pushed = await _run(hass, coordinator, {}, multi_site=False, sites=[])

    assert _sites(pushed) == [None]
    assert pushed[0][1] == pytest.approx(HOURLY)
    assert coordinator._pushed_factor_count == {DEFAULT_SITE_ID: HOURLY_FACTOR_COUNT}


async def test_failed_push_is_not_reported_as_pushed(hass, coordinator):
    """The Current Hour Dampening sensors' ``pushed`` must mean the base accepted it."""
    await _run(hass, coordinator, {}, push_ok=False)

    assert coordinator._dampening_pushed == set()
    assert coordinator._pushed_factor_count == {}


# ---------------------------------------------------------------------------
# Resetting the base's leftover hourly options before the first per-site push
# ---------------------------------------------------------------------------


async def test_single_site_upgrade_neutralises_leftover_hourly_options_first(hass, coordinator):
    """A single-site install's old curve sits in damp00..23. It is ignored once
    granular dampening is on, but returns if that is ever cleared — so reset it, and
    before the per-site push, since the global push is what clears granular."""
    old_curve = [1.0] * 7 + [0.5] * 10 + [1.0] * 7
    pushed = await _run(hass, coordinator, {}, multi_site=False, sites=[RID_A], traditional=old_curve)

    assert _sites(pushed) == [None, RID_A]
    assert pushed[0][1] == [1.0] * 24


@pytest.mark.parametrize(
    ("table", "traditional"),
    [
        # Already neutral — nothing to do.
        ({}, [1.0] * 24),
        # Populated table: a global push would clear it, and the base deletes the
        # file from a listener that can land after our per-site push. Leave it.
        ({RID_A: [0.9] * 24}, [0.5] * 24),
        # Base internals unreadable — no basis to act on.
        (None, [0.5] * 24),
        # Options unreadable.
        ({}, None),
    ],
)
async def test_leftover_hourly_options_left_alone(hass, coordinator, table, traditional):
    pushed = await _run(hass, coordinator, table, multi_site=False, sites=[RID_A], traditional=traditional)

    assert _sites(pushed) == [RID_A]


async def test_read_base_traditional_factors(hass, coordinator):
    entry = SimpleNamespace(options={f"damp{h:02d}": 0.5 + h / 100 for h in range(24)})
    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        assert coordinator._read_base_traditional_factors() == [0.5 + h / 100 for h in range(24)]
    partial = SimpleNamespace(options={"damp00": 0.5})
    with patch.object(hass.config_entries, "async_entries", return_value=[partial]):
        assert coordinator._read_base_traditional_factors() is None
