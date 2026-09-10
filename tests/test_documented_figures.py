"""Numbers quoted in documentation must match the code, and each other.

Two failures this guards, both seen live:

* A **tuning constant** is changed in ``const.py`` and the prose describing it is
  not. The doc then states a threshold the integration does not use, and nothing
  disagrees with anything — the code is self-consistent and the prose reads fine.
* An **empirical figure** is measured once and quoted in several files, then
  re-measured and corrected in only some of them. This happened on 2026-09-10:
  the single-vs-differential shading agreement was updated in ``CLAUDE.md`` and
  ``shading_geometry.py`` but missed in ``DESIGN_DOCUMENT.md``, and when that was
  fixed the same number went in rounded two different ways (0.16 vs 0.162).

Empirical figures have no code home to check against, so the only available
invariant is that every copy agrees. That is exactly the invariant that broke.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from custom_components.solcast_solar_enhanced import const, pv_tuning, shading_geometry

_ROOT = Path(__file__).resolve().parents[1]
_COMPONENT = Path(shading_geometry.__file__).parent

CLAUDE_MD = "CLAUDE.md"
DESIGN_MD = "DESIGN_DOCUMENT.md"
SHADING_PY = "custom_components/solcast_solar_enhanced/shading_geometry.py"
TUNING_PY = "custom_components/solcast_solar_enhanced/pv_tuning.py"


def _text(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


# --- Documented constants: prose vs the value the code actually uses ------------
#
# Each entry anchors the number to the phrase around it. Searching for the bare
# value is useless — "0.8" occurs somewhere in any long document, so the check
# passes for the wrong reason and guards nothing (verified: a mutation changing
# DEFAULT_KT_THRESHOLD to 0.8 sailed through an unanchored version of this test).
#
# ``template`` carries a single ``{}`` where the value belongs. The test formats
# it with the live constant and requires that exact string, so changing the
# constant without touching the prose fails, and so does rewording the phrase
# without noticing it carries a checked number.
DOCUMENTED_CONSTANTS = [
    ("DEFAULT_KT_THRESHOLD", const.DEFAULT_KT_THRESHOLD, "(default `{}`", [CLAUDE_MD, DESIGN_MD]),
    ("_FIT_REL_ERROR_MAX", pv_tuning._FIT_REL_ERROR_MAX, "`_FIT_REL_ERROR_MAX`, {}", [CLAUDE_MD]),
    ("_FIT_REL_ERROR_MAX", pv_tuning._FIT_REL_ERROR_MAX, "`_FIT_REL_ERROR_MAX` ({})", [DESIGN_MD]),
    ("DAMPENING_WINDOW_BACK_DAYS", const.DAMPENING_WINDOW_BACK_DAYS, "`DAMPENING_WINDOW_BACK_DAYS` {} back", [CLAUDE_MD]),
    (
        "DAMPENING_WINDOW_FORWARD_DAYS",
        const.DAMPENING_WINDOW_FORWARD_DAYS,
        "`DAMPENING_WINDOW_FORWARD_DAYS` {} forward",
        [CLAUDE_MD],
    ),
    ("APPORTION_AZIMUTH_TOL", const.APPORTION_AZIMUTH_TOL, "APPORTION_AZIMUTH_TOL` ({}°", [DESIGN_MD]),
    ("SHADING_UNIFORM_VOLTAGE_MIN", const.SHADING_UNIFORM_VOLTAGE_MIN, "`SHADING_UNIFORM_VOLTAGE_MIN` ({})", [DESIGN_MD]),
    ("SHADING_BYPASS_VOLTAGE_MAX", const.SHADING_BYPASS_VOLTAGE_MAX, "`SHADING_BYPASS_VOLTAGE_MAX` ({})", [DESIGN_MD]),
    ("SHADING_ELEV_STEP x SHADING_AZIM_STEP", None, "{}°×{}° (elevation × azimuth) grid", [CLAUDE_MD]),
]


def _renderings(value: float | int) -> set[str]:
    """Every reasonable way prose might write ``value``.

    ``5.0`` may legitimately appear as "5" or "5.0"; ``0.8`` as "0.8" or "0.80".
    Accepting the set keeps the check about the *number* rather than about house
    style, which would make it a formatting linter and get it ignored.
    """
    out = {str(value)}
    if isinstance(value, float):
        if value.is_integer():
            out.add(str(int(value)))
        out.add(f"{value:.2f}")
        out.add(f"{value:g}")
    return out


@pytest.mark.parametrize(
    ("name", "value", "template", "files"),
    DOCUMENTED_CONSTANTS,
    ids=[f"{e[0]}-{e[3][0]}" for e in DOCUMENTED_CONSTANTS],
)
def test_documented_constant_matches_the_code(name, value, template, files):
    """Prose quoting a constant must quote the value the code actually uses."""
    if value is None:  # The grid entry needs two values in one phrase.
        candidates = {
            template.format(a, b)
            for a in _renderings(const.SHADING_ELEV_STEP)
            for b in _renderings(const.SHADING_AZIM_STEP)
        }
    else:
        candidates = {template.format(r) for r in _renderings(value)}

    for relative in files:
        body = _text(relative)
        assert any(c in body for c in candidates), (
            f"{relative} does not state {name} at its current value. Expected one of "
            f"{sorted(candidates)}. Either the constant changed and the prose was not "
            f"updated, or the phrase was reworded and this registry entry needs to follow."
        )


# --- Empirical figures: measured once, quoted in several places ----------------
#
# No code constant to check against, so the invariant is agreement. Each entry is
# the exact string every listed file must contain — re-measuring means updating
# the value here and in every file, which is the point.
SHARED_FIGURES = [
    ("shading agreement above 30 deg", "0.018", [CLAUDE_MD, DESIGN_MD, SHADING_PY]),
    ("shading agreement below 15 deg", "0.162", [CLAUDE_MD, DESIGN_MD, SHADING_PY]),
    ("shading agreement worst cell", "0.50", [CLAUDE_MD, DESIGN_MD, SHADING_PY]),
    ("capacity/sky-view cross-method agreement", "0.02", [CLAUDE_MD, DESIGN_MD, SHADING_PY]),
]


@pytest.mark.parametrize(("label", "figure", "files"), SHARED_FIGURES, ids=lambda a: a if isinstance(a, str) else "")
def test_shared_empirical_figure_agrees_everywhere(label, figure, files):
    """A measured figure quoted in several files must read identically in each.

    Catches both halves of the real failure: a file left behind on the old value,
    and a file carrying the new value rounded differently.
    """
    missing = [f for f in files if figure not in _text(f)]
    assert not missing, (
        f"{label}: {figure!r} is missing from {missing}. If it was re-measured, update "
        f"every file in the registry and the value here — a figure quoted in one place "
        f"and not another is how this drifted before."
    )


def test_registry_points_at_files_that_exist():
    """A typo'd path would make an entry vacuously unenforced."""
    for entry in DOCUMENTED_CONSTANTS + SHARED_FIGURES:
        for relative in entry[-1]:
            assert (_ROOT / relative).is_file(), f"registry references a missing file: {relative}"


def test_no_sensor_count_drift():
    """The docs state a property-wide sensor count; keep it true.

    Cheap to state, easy to forget, and wrong the moment a sensor is added —
    exactly the shape of claim this module exists for.
    """
    source = (_COMPONENT / "sensor.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'_attr_translation_key = "([^"]+)"', source))
    site_scoped = {k for k in keys if k.startswith("site_")}
    property_wide = len(keys) - len(site_scoped)

    assert f"({property_wide} property-wide)" in _text(DESIGN_MD), (
        f"DESIGN_DOCUMENT.md's sensor heading disagrees with sensor.py, which declares "
        f"{property_wide} property-wide sensors and {len(site_scoped)} per-array."
    )
    assert f"{property_wide} property-wide" in _text(CLAUDE_MD), (
        f"CLAUDE.md's sensor count disagrees with sensor.py ({property_wide} property-wide)."
    )
