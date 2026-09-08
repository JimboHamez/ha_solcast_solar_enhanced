"""Tests for the geometric shading advisory.

Inputs here are deliberately asymmetric — shading confined to one side of the sky,
sky-view and capacity set to different non-unit values, trackers given different
behaviour. A uniform or all-ones fixture would pass against several wrong
implementations (see the project's degenerate-input lesson), so each fixture is
built so that a plausible mistake produces a *different* answer, not the same one.
"""

from __future__ import annotations

import math

import pytest

from custom_components.solcast_solar_enhanced.const import (
    SHADING_BEAM_FRAC_HIGH,
    SHADING_CLEAN_ELEV_MIN,
    SHADING_MIN_RECORDS,
)
from custom_components.solcast_solar_enhanced.shading_geometry import (
    analyse_shading,
    classify_mechanism,
    compass_point,
    evaluate,
    poa_components,
    wrap_azimuth,
)

# A north-facing array, matching the southern-hemisphere reference site.
TILT = 25.0
PANEL_AZ = 0.0


def _record(
    elevation: float,
    azimuth: float,
    *,
    transmission: float = 1.0,
    beam_frac: float = 0.9,
    capacity_ratio: float = 1.0,
    sky_view: float = 1.0,
    epoch: int = 1_750_000_000,
    vmed: tuple[float, float] = (0.0, 0.0),
    imed: tuple[float, float] = (0.0, 0.0),
) -> dict[str, float]:
    """Synthesise one store row with a known transmission at a known sun position.

    The irradiance is chosen so the transposed beam fraction comes out at
    ``beam_frac``, and ``pv_actual`` is then set to exactly what the forward model
    predicts, so a correct inversion recovers ``transmission``.
    """
    zenith = 90.0 - elevation
    doy = 172
    # Solve for the DNI/DHI pair that yields the requested beam fraction.
    unit_beam, unit_diffuse, _ = poa_components(TILT, PANEL_AZ, zenith, azimuth, 0.0, 1.0, 0.0, doy)
    _, diffuse_per_dhi, _ = poa_components(TILT, PANEL_AZ, zenith, azimuth, 0.0, 0.0, 1.0, doy)
    assert unit_beam > 0, "fixture requires the sun in front of the panel"
    dni = 800.0
    beam = dni * unit_beam
    # beam / (beam + dhi * diffuse_per_dhi) == beam_frac
    dhi = (beam * (1.0 - beam_frac) / beam_frac) / max(diffuse_per_dhi, 1e-9)
    beam_poa, diffuse_poa, ground_poa = poa_components(TILT, PANEL_AZ, zenith, azimuth, 0.0, dni, dhi, doy)
    total = beam_poa + diffuse_poa + ground_poa
    actual_beam_frac = beam_poa / total
    expected = 4.0 * total / 1000.0
    observed = expected * capacity_ratio * (transmission * actual_beam_frac + sky_view * (1.0 - actual_beam_frac))
    return {
        "period_end_epoch": epoch,
        "pv_actual": observed,
        "pv_estimate": expected,
        "pv_estimate_undampened": expected,
        "azimuth": azimuth % 360.0,
        "zenith": zenith,
        "ghi": 0.0,
        "dni": dni,
        "dhi": dhi,
        "clouds": 0,
        "temp": 15.0,
        "dc_vmed1": vmed[0],
        "dc_vmed2": vmed[1],
        "dc_imed1": imed[0],
        "dc_imed2": imed[1],
    }


def _population(
    shaded_azimuths: set[float],
    *,
    transmission: float = 0.4,
    capacity_ratio: float = 1.0,
    sky_view: float = 1.0,
    with_overcast: bool = False,
    low_sun_sky_view: float | None = None,
) -> list[dict[str, float]]:
    """Build a full sky of records, shaded only at ``shaded_azimuths``.

    Only the named azimuth bins carry the reduced transmission, so an implementation
    that applies one global factor cannot reproduce the result.

    ``low_sun_sky_view`` gives the *low* elevation overcast records a worse sky view
    than the high ones. That is the real behaviour — an obstruction that blocks the
    beam blocks part of the sky dome too — and without it a fit that wrongly pools
    every elevation gives the same answer as one that correctly restricts to high
    sun, so the fixture could not tell them apart.
    """
    records: list[dict[str, float]] = []
    for elevation in (7.5, 12.5, 17.5, 22.5, 27.5, 37.5, 42.5):
        for azimuth in (-67.5, -37.5, -7.5, 22.5, 52.5, 67.5):
            shaded = azimuth in shaded_azimuths and elevation < 20.0
            for _ in range(6):
                records.append(
                    _record(
                        elevation,
                        azimuth,
                        transmission=transmission if shaded else 1.0,
                        beam_frac=0.9,
                        capacity_ratio=capacity_ratio,
                        sky_view=sky_view,
                    )
                )
            if not with_overcast:
                continue
            high = elevation >= SHADING_CLEAN_ELEV_MIN
            if not high and low_sun_sky_view is None:
                continue
            for _ in range(6):
                records.append(
                    _record(
                        elevation,
                        azimuth,
                        transmission=1.0,
                        beam_frac=0.05,
                        capacity_ratio=capacity_ratio,
                        sky_view=sky_view if high else low_sun_sky_view,
                    )
                )
    return records


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0.0, 0.0), (90.0, 90.0), (270.0, -90.0), (359.0, -1.0), (180.0, -180.0)],
)
def test_wrap_azimuth_signs_east_positive(raw, expected):
    """0=N with East positive, so the western half comes back negative."""
    assert wrap_azimuth(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("azimuth", "point"),
    [(0.0, "N"), (45.0, "NE"), (90.0, "E"), (-90.0, "W"), (-45.0, "NW"), (67.5, "ENE")],
)
def test_compass_point_labels(azimuth, point):
    assert compass_point(azimuth) == point


def test_poa_beam_zero_when_sun_behind_panel():
    """A north-facing panel gets no beam from a sun due south of it."""
    beam, _, _ = poa_components(90.0, 0.0, 80.0, 180.0, 200.0, 800.0, 100.0, 172)
    assert beam == 0.0


def test_poa_beam_dominates_a_clear_sky():
    beam, diffuse, ground = poa_components(TILT, 0.0, 30.0, 0.0, 900.0, 850.0, 90.0, 172)
    assert beam > diffuse + ground


# ---------------------------------------------------------------------------
# Surface fit
# ---------------------------------------------------------------------------


def test_returns_none_below_the_record_floor():
    assert analyse_shading(_population(set())[:10], TILT, PANEL_AZ) is None


def test_unshaded_array_reports_no_loss():
    result = analyse_shading(_population(set()), TILT, PANEL_AZ)
    assert result is not None
    assert result["loss_pct"] == pytest.approx(0.0, abs=0.5)
    assert result["n_records"] >= SHADING_MIN_RECORDS


def test_recovers_the_shaded_bearing_and_leaves_the_rest_alone():
    """The mask must land in the shaded cells only — not be smeared across the sky.

    A global-factor implementation would report a loss but put the worst cell in an
    arbitrary direction, so the bearing assertion is what discriminates.
    """
    result = analyse_shading(_population({52.5, 67.5}, transmission=0.4), TILT, PANEL_AZ)
    assert result is not None
    assert result["worst_bearing"] in {"NE", "ENE"}
    assert result["worst_transmission"] == pytest.approx(0.4, abs=0.08)
    assert result["worst_elevation"] < 20.0
    assert result["loss_pct"] > 3.0
    # The unshaded western sky must come back clean.
    west = [v for k, v in result["surface"].items() if int(k.split(":")[1]) < 0]
    assert west and all(v > 0.9 for v in west)


def test_capacity_ratio_is_separated_from_shading():
    """A 0.8 capacity offset is not shading and must not be reported as any.

    This is the test that fails if ``k`` is dropped from the inversion: the whole
    20% offset would be attributed to the mask.
    """
    result = analyse_shading(_population(set(), capacity_ratio=0.8), TILT, PANEL_AZ)
    assert result is not None
    assert result["capacity_ratio"] == pytest.approx(0.8, abs=0.03)
    assert result["loss_pct"] == pytest.approx(0.0, abs=0.5)


def test_sky_view_is_fitted_from_high_sun_overcast_only():
    """Sky view is fitted where the array is unshaded, so shading cannot leak into it.

    The fixture shades the low-sun east and sets a true sky view of 0.85. Pooling
    every beam fraction — the bug this guards — would drag the estimate well below
    0.85 by counting the shaded low-sun records as diffuse loss.
    """
    records = _population(
        {52.5, 67.5},
        transmission=0.3,
        sky_view=0.85,
        with_overcast=True,
        # Low-sun overcast really does read worse, because the obstruction blocks
        # sky as well as sun. Pooling every elevation would land near 0.7, not 0.85.
        low_sun_sky_view=0.45,
    )
    result = analyse_shading(records, TILT, PANEL_AZ)
    assert result is not None
    assert result["sky_view_source"] == "high_sun"
    assert result["sky_view"] == pytest.approx(0.85, abs=0.05)


def test_sky_view_unavailable_without_high_sun_overcast():
    result = analyse_shading(_population(set()), TILT, PANEL_AZ)
    assert result is not None
    assert result["sky_view_source"] == "unavailable"
    assert result["sky_view"] == 1.0


def test_loss_counts_the_beam_term_only():
    """A low sky view is forecast bias plus dome blocking and is not reported as shading.

    Both fixtures carry the same (absent) beam mask; only the diffuse behaviour
    differs. If the headline included the diffuse term the second would report a
    large loss.
    """
    clear = analyse_shading(_population(set(), with_overcast=True), TILT, PANEL_AZ)
    dim_sky = analyse_shading(_population(set(), sky_view=0.5, with_overcast=True), TILT, PANEL_AZ)
    assert clear is not None and dim_sky is not None
    assert dim_sky["sky_view"] < 0.6
    assert dim_sky["loss_pct"] == pytest.approx(clear["loss_pct"], abs=1.0)


def test_evaluate_returns_none_for_an_unfitted_cell():
    """An unvisited sky cell is unknown, not unshaded."""
    result = analyse_shading(_population({52.5}), TILT, PANEL_AZ)
    assert result is not None
    assert evaluate(result, 80.0, 170.0, 0.9) is None


def test_evaluate_reproduces_a_fitted_cell():
    result = analyse_shading(_population({52.5, 67.5}, transmission=0.4), TILT, PANEL_AZ)
    assert result is not None
    value = evaluate(result, 12.5, 67.5, 1.0)
    assert value is not None
    assert value == pytest.approx(0.4, abs=0.1)


# ---------------------------------------------------------------------------
# Mechanism classification — what the median DC columns are for
# ---------------------------------------------------------------------------


def _mechanism_records(low_voltage: float, low_current: float) -> list[dict[str, float]]:
    """Records whose tracker 1 reads ``low_*`` at low sun and a clean 400 V / 8 A high up.

    Current is scaled by plane-of-array irradiance inside the classifier, so the
    fixture states the *normalised* behaviour by holding irradiance constant across
    the two elevation bands.
    """
    prepared: list[dict[str, float]] = []
    for elevation, voltage, current in ((10.0, low_voltage, low_current), (40.0, 400.0, 8.0)):
        for _ in range(8):
            prepared.append(
                {
                    "elevation": elevation,
                    "azimuth": 0.0,
                    "beam_fraction": 0.9,
                    "poa": 500.0,
                    "ratio": 1.0,
                    "expected": 3.0,
                    "v1": voltage,
                    "i1": current,
                    "v2": 0.0,
                    "i2": 0.0,
                }
            )
    return prepared


def test_mechanism_uniform_when_voltage_holds_and_current_falls():
    """Flat Vmp with collapsing current is a shadow line across every module."""
    result = classify_mechanism(_mechanism_records(388.0, 3.5))
    assert result["mechanism"] == "uniform"
    assert result["model_valid"] is True
    assert result["current_ratio"] < 0.5


def test_mechanism_bypass_when_voltage_collapses():
    """A voltage collapse means bypass diodes, and no single factor can model it."""
    result = classify_mechanism(_mechanism_records(260.0, 3.5))
    assert result["mechanism"] == "bypass"
    assert result["model_valid"] is False


def test_mechanism_minimal_when_there_is_no_current_deficit():
    result = classify_mechanism(_mechanism_records(395.0, 7.6))
    assert result["mechanism"] == "minimal"


def test_mechanism_undetermined_without_dc_telemetry():
    """No DC columns (an install with no per-MPPT sensors) yields no verdict."""
    records = _mechanism_records(388.0, 3.5)
    for record in records:
        record["v1"] = 0.0
        record["i1"] = 0.0
    result = classify_mechanism(records)
    assert result["mechanism"] == "undetermined"
    assert result["model_valid"] is None
    assert result["trackers"] == []


def test_worst_tracker_drives_the_verdict():
    """One shaded string invalidates a single factor for the whole array.

    Tracker 1 is clean and tracker 2 is bypassing; a mean over trackers would
    average the collapse away and wrongly report the model as valid.
    """
    records = _mechanism_records(400.0, 8.0)
    for record in records:
        record["v2"] = 250.0 if record["elevation"] < 20.0 else 400.0
        record["i2"] = 3.0 if record["elevation"] < 20.0 else 8.0
    result = classify_mechanism(records)
    assert result["mechanism"] == "bypass"
    assert result["model_valid"] is False
    assert len(result["trackers"]) == 2


def test_mechanism_reaches_the_full_analysis():
    """``analyse_shading`` merges the classifier's verdict into its result."""
    records = _population({52.5, 67.5}, transmission=0.4)
    for record in records:
        elevation = 90.0 - record["zenith"]
        record["dc_vmed1"] = 390.0 if elevation < 20.0 else 400.0
        record["dc_imed1"] = 2.0 if elevation < 20.0 else 8.0
    result = analyse_shading(records, TILT, PANEL_AZ)
    assert result is not None
    assert result["mechanism"] == "uniform"
    assert result["voltage_ratio"] == pytest.approx(390.0 / 400.0, abs=0.01)


def test_low_sun_uncertainty_is_flagged():
    """A worst cell in the unreliable low-sun band must say so.

    Transmission is graded by elevation so the deepest cell is unambiguously the
    lowest one — a flat mask would let the worst cell tie anywhere in the band.
    """
    records: list[dict[str, float]] = []
    for elevation in (7.5, 12.5, 17.5, 22.5, 27.5, 37.5, 42.5):
        for azimuth in (-67.5, -37.5, -7.5, 22.5, 52.5, 67.5):
            graded = 0.2 if elevation < 10.0 else (0.6 if elevation < 20.0 else 1.0)
            for _ in range(6):
                records.append(_record(elevation, azimuth, transmission=graded))
    result = analyse_shading(records, TILT, PANEL_AZ)
    assert result is not None
    assert result["worst_elevation"] < 15.0
    assert result["low_sun_uncertain"] is True


def test_low_sun_uncertainty_clear_when_shading_is_high_in_the_sky():
    """The counterpart: a mask confined above the unreliable band is not flagged."""
    records: list[dict[str, float]] = []
    for elevation in (22.5, 27.5, 37.5, 42.5):
        for azimuth in (-67.5, -37.5, -7.5, 22.5, 52.5, 67.5):
            for _ in range(10):
                records.append(_record(elevation, azimuth, transmission=0.7 if azimuth > 40 else 1.0))
    result = analyse_shading(records, TILT, PANEL_AZ)
    assert result is not None
    assert result["worst_elevation"] >= 15.0
    assert result["low_sun_uncertain"] is False


def test_beam_fraction_threshold_governs_the_fit():
    """Records below the beam-fraction bar cannot contribute to the mask.

    Built entirely from diffuse-dominated records, the fit has nothing to invert.
    """
    records = [
        _record(elevation, azimuth, transmission=0.3, beam_frac=0.1)
        for elevation in (7.5, 12.5, 17.5, 22.5, 27.5, 37.5)
        for azimuth in (-67.5, -37.5, -7.5, 22.5, 52.5, 67.5)
        for _ in range(8)
    ]
    assert all(SHADING_BEAM_FRAC_HIGH > 0.1 for _ in (0,))
    assert analyse_shading(records, TILT, PANEL_AZ) is None


def test_azimuth_wrap_keeps_transit_off_a_cell_boundary():
    """Cells either side of due north must not collapse into one another."""
    east = _record(30.0, 7.5)
    west = _record(30.0, -7.5)
    assert math.floor(wrap_azimuth(east["azimuth"]) / 15.0) != math.floor(wrap_azimuth(west["azimuth"]) / 15.0)
