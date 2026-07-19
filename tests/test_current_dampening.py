"""Tests for the current-hour dampening sensors (property-wide and per-site).

The state must be the factor the base integration is *really* applying for the
current local hour: the mean of that hour's two half-hour slots, carrying the same
[0, 1] clamp and rounding as the push.
"""

from __future__ import annotations

import pytest
from freezegun import freeze_time

from custom_components.solcast_solar_enhanced.const import DEFAULT_SITE_ID
from custom_components.solcast_solar_enhanced.coordinator import SolcastEnhancedCoordinator

# 09:30 Melbourne (UTC+10 in July) — local hour 9, so slots 18/19.
MELBOURNE_0930 = "2026-07-14 23:30:00"


@pytest.fixture
def coordinator(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    return SolcastEnhancedCoordinator(hass, mock_config_entry)


@pytest.fixture
async def melbourne(hass):
    await hass.config.async_set_time_zone("Australia/Melbourne")


def _table(**overrides: dict) -> list[dict]:
    """48 slots, each with a distinct factor so a wrong index can't accidentally pass.

    Slot i gets factor 0.51 + i/100, i.e. every slot differs and no pair averages to
    another pair's value — a uniform table would let an off-by-one or a whole-day
    average masquerade as a correct current-hour lookup.
    """
    table = [
        {
            "factor": round(0.51 + i / 100, 4),
            "alpha": 0.4,
            "source": "db_blended",
            "quality_records": 4.0,
            "clear_sky_basis": "kt",
        }
        for i in range(48)
    ]
    for idx, patch in overrides.items():
        table[int(idx)].update(patch)
    return table


@freeze_time(MELBOURNE_0930)
async def test_current_dampening_uses_local_hour_slot_pair(coordinator, melbourne):
    coordinator._dampening_table = _table()
    coordinator._dampening_pushed = {DEFAULT_SITE_ID}

    # Local hour 9 → mean of slots 18 (0.69) and 19 (0.70).
    assert coordinator.current_dampening == pytest.approx(0.695)

    attrs = coordinator.current_dampening_attributes
    assert attrs["hour"] == 9
    assert attrs["factor_first_half"] == pytest.approx(0.69)
    assert attrs["factor_second_half"] == pytest.approx(0.70)
    assert attrs["raw_factor"] == pytest.approx(0.695)
    assert attrs["clear_sky_basis"] == "kt"
    assert attrs["orientation_diverged"] is False
    assert attrs["pushed"] is True


@freeze_time(MELBOURNE_0930)
async def test_orientation_divergence_is_advisory_only(coordinator, melbourne):
    """Divergence flags the orientation but must not change the reported factor.

    Before 1.10.0b8 a diverged target was held at a neutral 1.0; that suppressed a
    measured shading curve on the strength of a tuned tilt that is frequently
    non-identifiable, so the state must now track the real pushed value.
    """
    coordinator._dampening_table = _table()
    coordinator._orientation_advisory_targets = {DEFAULT_SITE_ID}

    assert coordinator.current_dampening == pytest.approx(0.695)
    attrs = coordinator.current_dampening_attributes
    assert attrs["orientation_diverged"] is True
    assert attrs["raw_factor"] == pytest.approx(0.695)


@freeze_time(MELBOURNE_0930)
async def test_current_dampening_clamps_like_the_push(coordinator, melbourne):
    """_push_dampening clamps to [0, 1]; the sensor must match the wire value."""
    coordinator._dampening_table = _table(**{"18": {"factor": 1.6}, "19": {"factor": 1.2}})

    assert coordinator.current_dampening == 1.0
    assert coordinator.current_dampening_attributes["raw_factor"] == pytest.approx(1.4)


@freeze_time(MELBOURNE_0930)
async def test_current_dampening_pushed_false_when_nothing_sent(coordinator, melbourne):
    """Multi-site skips the global push, and base auto-dampening skips every push."""
    coordinator._dampening_table = _table()
    coordinator._dampening_pushed = set()

    assert coordinator.current_dampening_attributes["pushed"] is False


async def test_current_dampening_none_without_table(coordinator):
    assert coordinator.current_dampening is None
    assert coordinator.current_dampening_attributes == {"pushed": False}


@freeze_time(MELBOURNE_0930)
async def test_current_dampening_none_when_table_too_short(coordinator, melbourne):
    """A partial table must read unavailable rather than index into a missing slot."""
    coordinator._dampening_table = _table()[:10]

    assert coordinator.current_dampening is None


@freeze_time(MELBOURNE_0930)
async def test_site_current_dampening_reads_that_sites_table(coordinator, melbourne):
    coordinator._sites = [{"resource_id": "a", "name": "Ground"}, {"resource_id": "b", "name": "Roof"}]
    coordinator._site_dampening_tables = {
        "a": _table(),
        "b": _table(**{"18": {"factor": 0.30}, "19": {"factor": 0.40}}),
    }
    coordinator._dampening_pushed = {"a", "b"}
    coordinator._orientation_advisory_targets = {"b"}

    assert coordinator.site_current_dampening("a") == pytest.approx(0.695)
    # The advisory on site b must not alter site b's factor nor leak onto site a.
    assert coordinator.site_current_dampening("b") == pytest.approx(0.35)

    attrs = coordinator.site_current_dampening_attributes("b")
    assert attrs["name"] == "Roof"
    assert attrs["resource_id"] == "b"
    assert attrs["orientation_diverged"] is True
    assert attrs["raw_factor"] == pytest.approx(0.35)
    assert coordinator.site_current_dampening_attributes("a")["orientation_diverged"] is False


async def test_site_current_dampening_none_for_unknown_site(coordinator):
    assert coordinator.site_current_dampening("missing") is None
