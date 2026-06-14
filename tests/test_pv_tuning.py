"""Test PV tuning: solar position calculation and numpy grid-search optimisation."""
from __future__ import annotations

import math
import pytest

from custom_components.solcast_solar_enhanced.pv_tuning import (
    TUNING_AVAILABLE,
    _cos_incidence,
    _minimize_tilt,
    panel_azimuth_to_internal,
    panel_azimuth_to_solcast,
    run_tuning,
    solar_position,
)


# ---------------------------------------------------------------------------
# Panel-azimuth convention conversion (Solcast West-positive <-> internal East-positive)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("solcast, internal", [
    (0.0, 0.0),       # North
    (-90.0, 90.0),    # Solcast East -> internal East
    (90.0, -90.0),    # Solcast West -> internal West
    (6.0, -6.0),      # 6 deg West of North
    (-30.0, 30.0),    # 30 deg East of North
])
def test_panel_azimuth_to_internal(solcast, internal):
    assert panel_azimuth_to_internal(solcast) == pytest.approx(internal)


def test_panel_azimuth_conversion_is_involution():
    for a in (-179.0, -90.0, -6.0, 0.0, 13.5, 90.0, 170.0):
        assert panel_azimuth_to_solcast(panel_azimuth_to_internal(a)) == pytest.approx(a)


def test_panel_azimuth_south_maps_to_pm180():
    # +180 and -180 both mean South; conversion stays on that axis.
    assert abs(panel_azimuth_to_internal(180.0)) == pytest.approx(180.0)

# ---------------------------------------------------------------------------
# solar_position
# ---------------------------------------------------------------------------

def test_solar_above_horizon_at_melbourne_midday():
    """Sun is above horizon at Melbourne solar noon on summer solstice."""
    # 2024-12-21 02:00 UTC ≈ solar noon at Melbourne (lat -37.9, lon 145.0)
    epoch = 1734746400  # 2024-12-21 02:00 UTC
    az, zen = solar_position(epoch, -37.9, 145.0)
    assert zen < 90.0, f"Expected sun above horizon, got zenith={zen:.1f}°"


def test_solar_below_horizon_at_utc_midnight():
    """Sun is below horizon at 00:00 UTC for a site on the Greenwich meridian."""
    # 2024-06-21 00:00 UTC = midnight for lon=0; sun is well below horizon at London
    epoch = 1718928000
    az, zen = solar_position(epoch, 51.5, 0.0)
    assert zen >= 90.0, f"Expected sun below horizon, got zenith={zen:.1f}°"


def test_solar_zenith_range():
    """Zenith is always in [0°, 180°]."""
    for hour in range(0, 24, 3):
        epoch = 1734739200 + hour * 3600
        az, zen = solar_position(epoch, -37.9, 145.0)
        assert 0.0 <= zen <= 180.0


def test_solar_azimuth_range():
    """Azimuth is always in [0°, 360°]."""
    for hour in range(0, 24, 3):
        epoch = 1734739200 + hour * 3600
        az, zen = solar_position(epoch, -37.9, 145.0)
        assert 0.0 <= az <= 360.0


def test_solar_position_northern_hemisphere():
    """Sun is above horizon at solar noon in London on summer solstice."""
    # 2024-06-21 12:00 UTC ≈ noon at London (lat 51.5, lon 0.0)
    epoch = 1718964000  # 2024-06-21 12:00 UTC
    az, zen = solar_position(epoch, 51.5, 0.0)
    assert zen < 90.0


def _reference_azimuth(epoch: int, latitude: float, longitude: float) -> float:
    """Independent azimuth-from-north via atan2, for cross-checking solar_position.

    Uses the same declination/EOT model but an atan2 formulation that is immune to
    the morning/afternoon sign-branch bug, so it validates the quadrant logic.
    """
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    doy = dt.timetuple().tm_yday
    hour_utc = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (doy - 81))))
    B = math.radians(360 / 365 * (doy - 81))
    eot = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    ha = math.radians(15 * (hour_utc - (12 - longitude / 15 - eot / 60)))
    lat_r = math.radians(latitude)
    az = math.atan2(
        math.sin(ha),
        math.cos(ha) * math.sin(lat_r) - math.tan(decl) * math.cos(lat_r),
    )
    return (math.degrees(az) + 180) % 360  # atan2 is from south; +180 -> from north


@pytest.mark.parametrize(
    "label, lat, lon, iso",
    [
        # Regression: local morning/afternoon on a different UTC calendar day from
        # solar noon used to overflow the hour angle past ±180° and mirror the
        # azimuth east<->west. Covers far-east (UTC+10/+9 morning) and far-west
        # (UTC-10 afternoon), both hemispheres.
        ("melbourne_morning", -37.9, 145.0, "2026-06-04T23:15:00+00:00"),  # 09:15 AEST
        ("tokyo_morning", 35.7, 139.7, "2026-06-21T23:30:00+00:00"),       # 08:30 JST
        ("hawaii_afternoon", 21.3, -157.8, "2026-06-22T00:30:00+00:00"),   # 14:30 HST
        ("santiago_morning", -33.4, -70.6, "2026-06-21T13:00:00+00:00"),   # 09:00 CLT
    ],
)
def test_solar_azimuth_matches_reference_across_date_boundary(label, lat, lon, iso):
    """Azimuth matches an independent atan2 reference regardless of UTC-date offset."""
    from datetime import datetime

    epoch = int(datetime.fromisoformat(iso).timestamp())
    az, _ = solar_position(epoch, lat, lon)
    ref = _reference_azimuth(epoch, lat, lon)
    delta = (az - ref + 180) % 360 - 180  # signed shortest angular difference
    assert abs(delta) < 1.0, f"{label}: az={az:.1f}° vs ref={ref:.1f}° (Δ={delta:.1f}°)"


def test_solar_azimuth_morning_is_eastern_half():
    """Southern-hemisphere UTC+10 morning sun is in the eastern half (the old bug
    reported it at ~316°, the north-west)."""
    # 2026-06-04 23:15 UTC = 09:15 AEST at Melbourne
    epoch = int(__import__("datetime").datetime.fromisoformat(
        "2026-06-04T23:15:00+00:00").timestamp())
    az, _ = solar_position(epoch, -37.9, 145.0)
    assert 0.0 <= az <= 180.0, f"Expected eastern-half azimuth, got {az:.1f}°"


# ---------------------------------------------------------------------------
# _cos_incidence
# ---------------------------------------------------------------------------

def test_cos_incidence_panel_facing_sun():
    """Panel perfectly facing the sun returns cos_incidence ≈ 1."""
    # Horizontal panel (tilt=0) with sun directly overhead (zenith=0)
    val = _cos_incidence(0.0, 0.0, 0.0, 0.0)
    assert abs(val - 1.0) < 1e-6


def test_cos_incidence_panel_facing_away():
    """Panel facing away from sun clamps to 0."""
    # Sun at zenith=0 (overhead), panel tilted 90° facing away
    val = _cos_incidence(90.0, 180.0, 0.0, 0.0)
    assert val >= 0.0  # clamped, never negative


# ---------------------------------------------------------------------------
# run_tuning — transposition-based, tilt-only (azimuth fixed)
# ---------------------------------------------------------------------------

def _irr_record(**overrides):
    """A clear-sky, irradiance-bearing tuning record (the columns the tuner reads)."""
    rec = {
        "period_end_epoch": 1717200000,
        "pv_actual": 3.0, "pv_export": 0.5, "battery_charge": 0.0,
        "pv_estimate": 4.0, "clouds": 5, "zenith": 30.0, "azimuth": 180.0,
        "ghi": 600.0, "dni": 700.0, "dhi": 120.0,
    }
    rec.update(overrides)
    return rec


def test_run_tuning_returns_none_on_empty_records():
    assert run_tuning([], 5.0, 20, 0.95) is None


def test_run_tuning_returns_none_below_10_records():
    records = [_irr_record() for _ in range(9)]
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_filters_cloudy_records():
    """Records with clouds ≥ threshold are excluded; if none remain → None."""
    records = [_irr_record(clouds=50) for _ in range(20)]
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_skips_records_without_irradiance():
    """The transposition tuner needs GHI; rows with ghi=0 (night / not backfilled)
    are skipped, so an all-zero-irradiance set yields None even when otherwise clear."""
    records = [_irr_record(ghi=0.0, zenith=30.0 + i * 0.1) for i in range(20)]
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_returns_result_with_irradiance():
    """Enough clear-sky, irradiance-bearing, non-clipped records → a result dict."""
    records = [
        _irr_record(zenith=30.0 + i * 0.5, azimuth=170.0 + i) for i in range(20)
    ]
    result = run_tuning(records, 8.0, 20, 0.95, fixed_azimuth=0.0)
    if result is None:
        pytest.skip("numpy not available")
    assert set(result) >= {
        "tilt", "azimuth", "rmse_kw", "mae_kw", "capacity_scale",
        "n_records", "export_limited_excluded", "source",
    }
    assert result["n_records"] == 20
    assert 0.0 <= result["tilt"] <= 90.0
    assert result["capacity_scale"] > 0.0
    assert result["rmse_kw"] >= 0.0


def test_run_tuning_keeps_zero_cloud_records():
    """A genuine 0% cloud reading (clearest sky) must NOT be dropped (None=missing)."""
    if not TUNING_AVAILABLE:
        pytest.skip("numpy not available")
    records = [_irr_record(clouds=0, zenith=30.0 + i * 0.5) for i in range(20)]
    result = run_tuning(records, 8.0, 20, 0.95, fixed_azimuth=0.0)
    assert result is not None, "0% cloud records were wrongly excluded"
    assert result["n_records"] == 20


def test_run_tuning_treats_missing_cloud_as_overcast():
    """A missing/None cloud value is still treated as overcast (excluded)."""
    records = [_irr_record(clouds=None) for _ in range(20)]
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_excludes_no_owm_sentinel():
    """The no-OWM storage sentinel (clouds=100) is excluded as fully overcast."""
    records = [_irr_record(clouds=100) for _ in range(20)]
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_excludes_clipped_records():
    """Records where both total_pv and pv_estimate exceed clip threshold are excluded."""
    records = [_irr_record(pv_actual=4.8, pv_estimate=4.9) for _ in range(20)]
    assert run_tuning(records, 5.0, 20, 0.95) is None


def test_run_tuning_excludes_export_limited_records():
    """Records where pv_export >= export_limit * clipping_threshold are excluded."""
    records = [_irr_record(pv_actual=2.0, pv_export=2.9) for _ in range(20)]
    # pv_export 2.9 >= 3.0 * 0.95 = 2.85 → all records excluded → None
    assert run_tuning(records, 5.0, 20, 0.95, export_limit_kw=3.0) is None


def test_run_tuning_zero_export_limit_disables_filter():
    """export_limit_kw=0 (default) does not exclude any records based on export."""
    records = [
        _irr_record(pv_actual=2.0, pv_export=2.9, zenith=30.0 + i * 0.5)
        for i in range(20)
    ]
    result = run_tuning(records, 5.0, 20, 0.95, export_limit_kw=0.0, fixed_azimuth=0.0)
    if result is None:
        pytest.skip("numpy not available")
    assert result["n_records"] == 20


def test_run_tuning_azimuth_is_fixed_not_tuned():
    """Azimuth is echoed back as the (normalised) fixed input — never tuned."""
    if not TUNING_AVAILABLE:
        pytest.skip("numpy not available")
    records = [_irr_record(zenith=30.0 + i * 0.5, azimuth=170.0 + i) for i in range(20)]
    result = run_tuning(records, 8.0, 20, 0.95, fixed_azimuth=-6.0)
    assert result["azimuth"] == pytest.approx(-6.0)


# ---------------------------------------------------------------------------
# _minimize_tilt — pure-numpy 1-D grid search
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not TUNING_AVAILABLE, reason="numpy not available")
def test_minimize_tilt_finds_known_minimum():
    """The coarse-to-fine tilt search locates a known minimum within the finest
    step (0.25°), independent of the starting point (the full sweep has no seed)."""
    target = 33.0
    tilt, val = _minimize_tilt(lambda t: (t - target) ** 2, initial_tilt=5.0)
    assert tilt == pytest.approx(target, abs=0.25)
    assert val == pytest.approx(0.0, abs=0.1)


@pytest.mark.skipif(not TUNING_AVAILABLE, reason="numpy not available")
def test_minimize_tilt_clamps_to_bounds():
    """A minimum outside [0, 90] is clamped to the nearest in-bounds tilt."""
    tilt, _ = _minimize_tilt(lambda t: (t - 120.0) ** 2)
    assert tilt == pytest.approx(90.0, abs=0.25)


@pytest.mark.skipif(not TUNING_AVAILABLE, reason="numpy not available")
def test_run_tuning_recovers_synthetic_tilt():
    """End-to-end: records synthesised (isotropic transposition) from a known tilt
    are tuned back to it, with the fitted capacity scale recovered too."""
    true_tilt, fixed_az, true_scale, albedo = 35.0, -6.0, 0.006, 0.2

    def iso_poa(tilt, zen, sun_az, ghi, dni, dhi):
        tr, ar = math.radians(tilt), math.radians(fixed_az)
        z = math.radians(zen)
        caoi = max(
            0.0, math.cos(z) * math.cos(tr)
            + math.sin(z) * math.sin(tr) * math.cos(math.radians(sun_az) - ar)
        )
        return (dni * caoi + dhi * (1 + math.cos(tr)) / 2
                + ghi * albedo * (1 - math.cos(tr)) / 2)

    base_epoch = 1717200000
    records = []
    for i in range(200):
        ep = base_epoch + i * 1800
        az, zen = solar_position(ep, -37.9, 145.0)
        if zen >= 85:
            continue
        dni, dhi = 850.0, 90.0
        ghi = dni * math.cos(math.radians(zen)) + dhi
        obs = true_scale * iso_poa(true_tilt, zen, az, ghi, dni, dhi)
        records.append({
            "period_end_epoch": ep, "pv_actual": obs, "pv_export": 0.0,
            "pv_estimate": 0.0, "clouds": 0, "zenith": zen, "azimuth": az,
            "ghi": ghi, "dni": dni, "dhi": dhi,
        })
    result = run_tuning(
        records, 100.0, 20, 0.95, fixed_azimuth=fixed_az, albedo=albedo, model="isotropic"
    )
    assert result is not None
    assert result["tilt"] == pytest.approx(true_tilt, abs=1.0)
    assert result["capacity_scale"] == pytest.approx(true_scale, rel=0.02)
    assert result["mae_kw"] == pytest.approx(0.0, abs=0.01)
