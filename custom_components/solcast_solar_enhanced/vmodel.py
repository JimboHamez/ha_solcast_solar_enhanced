"""Learned per-tracker MPP voltage, for detecting inverter-side limiting from DC telemetry.

Record-only (schema v2, the ``vexcess_s`` column). Pure Python, no Home Assistant
imports and no numpy, so it runs on any install.

Why a model: an inverter that limits its output walks the operating point up the
I-V curve, so the DC voltage rises above the maximum power point. A *fixed*
reference cannot see that reliably, because the MPP voltage itself moves with cell
temperature (cool afternoon panels sit 4-5% above a hot midday reference) and with
irradiance. The shipped ``dc_offmpp_min`` rule also needs current below 0.3x its
reference, which a ~20% export trim never reaches: it read 0 through two fully
limited hours on a Fronius GEN24.

The model, per tracker::

    V_mpp = a + b·T_amb + c·G + d·ln G

``T_amb`` is the slot's ambient temperature and ``G`` the plane-of-array
irradiance estimate; panel temperature is ``T_amb`` plus a term in ``G``, and
``ln G`` is the diode's irradiance dependence. String length, panel type and the
temperature coefficient all land in the four fitted numbers, so nothing about the
array is configured.

It is fitted by least squares on **running sums** (the 4x4 normal equations plus
``Σy²``), so no samples are stored and the state is a few dozen floats. The sums are
decayed once a day (``VMODEL_FORGET``), which follows the seasons and a changed
array within about two weeks.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict

from .const import (
    VMODEL_CLEAN_EXPORT_FRAC,
    VMODEL_CLEAN_EXPORT_WINDOW_S,
    VMODEL_EXCESS,
    VMODEL_GRID_S,
    VMODEL_MIN_CURRENT_A,
    VMODEL_MIN_G,
    VMODEL_MIN_SAMPLES,
    VMODEL_OUTLIER_SIGMA,
    VMODEL_SCAN_RATIO,
    VMODEL_SCAN_WINDOW_S,
    VMODEL_SHADE_SPREAD,
    VMODEL_SHARE_MIN_SAMPLES,
    VMODEL_SHARE_MIN_SLOTS,
    VMODEL_SHARE_SLOTS,
    VMODEL_STEADY_CV,
    VMODEL_STEADY_WINDOW_S,
)

Samples = Sequence[tuple[float, float]]
N_FEATURES = 4


def features(temp: float, g: float) -> tuple[float, float, float, float]:
    """The regressors, scaled to similar magnitudes so the 4x4 solve stays well conditioned."""
    g_kw = max(g, 10.0) / 1000.0
    return (1.0, temp / 10.0, g_kw, math.log(g_kw))


class SlotVoltage(TypedDict):
    """One slot's voltage-model result (``-1`` = not measurable)."""

    vexcess_s: float
    vratio_p50: float
    vratio_p90: float
    ready: bool


class TrackerFit:
    """Running least-squares sums and the current fit for one tracker."""

    def __init__(self) -> None:
        """Start empty."""
        self.xtx = [[0.0] * N_FEATURES for _ in range(N_FEATURES)]
        self.xty = [0.0] * N_FEATURES
        self.yy = 0.0
        self.n = 0.0
        self.coef: list[float] | None = None
        self.sigma: float | None = None

    def add(self, x: Sequence[float], y: float) -> None:
        """Add one healthy sample."""
        for r in range(N_FEATURES):
            self.xty[r] += x[r] * y
            row = self.xtx[r]
            for c in range(N_FEATURES):
                row[c] += x[r] * x[c]
        self.yy += y * y
        self.n += 1.0

    def solve(self, min_samples: float = VMODEL_MIN_SAMPLES) -> bool:
        """Refit from the sums. True when the fit is usable."""
        if self.n < min_samples:
            return False
        coef = _solve(self.xtx, self.xty)
        if coef is None:
            return False
        self.coef = coef
        rss = self.yy - sum(c * b for c, b in zip(coef, self.xty, strict=True))
        self.sigma = math.sqrt(max(rss, 0.0) / max(self.n - N_FEATURES, 1.0))
        return True

    def decay(self, factor: float) -> None:
        """Fade old evidence so the fit follows the season."""
        self.xtx = [[v * factor for v in row] for row in self.xtx]
        self.xty = [v * factor for v in self.xty]
        self.yy *= factor
        self.n *= factor

    def predict(self, x: Sequence[float]) -> float | None:
        """Expected MPP voltage, or None before the fit is usable."""
        if self.coef is None:
            return None
        return sum(c * v for c, v in zip(self.coef, x, strict=True))

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe state, for HA ``Store``."""
        return {"xtx": self.xtx, "xty": self.xty, "yy": self.yy, "n": self.n, "coef": self.coef, "sigma": self.sigma}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrackerFit:
        """Restore from ``to_dict``; tolerates a missing or malformed entry by starting empty."""
        fit = cls()
        try:
            xtx = [[float(v) for v in row] for row in data["xtx"]]
            xty = [float(v) for v in data["xty"]]
            if len(xtx) != N_FEATURES or any(len(r) != N_FEATURES for r in xtx) or len(xty) != N_FEATURES:
                return cls()
            fit.xtx, fit.xty = xtx, xty
            fit.yy, fit.n = float(data["yy"]), float(data["n"])
            coef = data.get("coef")
            fit.coef = [float(v) for v in coef] if coef and len(coef) == N_FEATURES else None
            sigma = data.get("sigma")
            fit.sigma = float(sigma) if sigma is not None else None
        except (KeyError, TypeError, ValueError):
            return cls()
        return fit


class VoltageModel:
    """One ``TrackerFit`` per tracker, keyed by its voltage entity."""

    def __init__(self) -> None:
        """Start with no trackers."""
        self.trackers: dict[str, TrackerFit] = {}
        # Each tracker's recent share of the slot voltage (its median over the mean of
        # every tracker's median): the bypass-spread reference until the model is ready.
        # In memory only; after a restart training waits VMODEL_SHARE_MIN_SLOTS slots.
        self.shares: dict[str, deque[float]] = {}

    def share(self, tracker: str) -> float | None:
        """The tracker's usual share of the slot voltage, or ``None`` before enough slots."""
        recent = self.shares.get(tracker)
        if recent is None or len(recent) < VMODEL_SHARE_MIN_SLOTS:
            return None
        return statistics.median(recent)

    def add_shares(self, medians: Mapping[str, float]) -> None:
        """Record one slot's per-tracker median voltages as shares of their mean."""
        mean = sum(medians.values()) / len(medians)
        if mean <= 0:
            return
        for tracker, median in medians.items():
            self.shares.setdefault(tracker, deque(maxlen=VMODEL_SHARE_SLOTS)).append(median / mean)

    def fit(self, tracker: str) -> TrackerFit:
        """The fit for ``tracker``, created on first use."""
        return self.trackers.setdefault(tracker, TrackerFit())

    def ready(self, tracker: str) -> bool:
        """Whether ``tracker`` has a usable fit."""
        fit = self.trackers.get(tracker)
        return fit is not None and fit.coef is not None

    def decay(self, factor: float) -> None:
        """Decay every tracker's sums (called daily)."""
        for fit in self.trackers.values():
            fit.decay(factor)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe state, for HA ``Store``."""
        return {"version": 1, "trackers": {k: v.to_dict() for k, v in self.trackers.items()}}

    @classmethod
    def from_dict(cls, data: Any) -> VoltageModel:
        """Restore from ``to_dict``; anything unreadable starts a fresh model."""
        model = cls()
        if isinstance(data, Mapping) and data.get("version") == 1 and isinstance(data.get("trackers"), Mapping):
            model.trackers = {
                str(k): TrackerFit.from_dict(v) for k, v in data["trackers"].items() if isinstance(v, Mapping)
            }
        return model


def evaluate_slot(
    start: float,
    end: float,
    trackers: Mapping[str, tuple[Samples, Samples]],
    temp: float,
    g_at: Callable[[float], float],
    model: VoltageModel,
    export: Samples | None = None,
    export_limit_kw: float = 0.0,
) -> SlotVoltage:
    """Judge this slot's steady samples against the model, and train it on the healthy ones.

    Args:
        start: Slot start, epoch seconds.
        end: Slot end, epoch seconds.
        trackers: ``{voltage entity: (voltage samples, current samples)}`` for every
            tracker on the inverter.
        temp: The slot's ambient temperature, °C.
        g_at: Plane-of-array irradiance estimate at a time, W/m².
        model: The model to evaluate against and to train (updated in place).
        export: Export samples in kW, used to keep limited samples out of training.
        export_limit_kw: The limit in force; 0 or less means none.

    Returns:
        ``vexcess_s`` (steady seconds at least ``VMODEL_EXCESS`` above the model on
        every tracker) and the median and p90 of ``V / model`` for the lowest tracker
        over steady samples, all -1 unless every tracker had a usable fit beforehand
        (``ready``). The ratios are what lets the threshold be retuned later.
    """
    unknown: SlotVoltage = {"vexcess_s": -1.0, "vratio_p50": -1.0, "vratio_p90": -1.0, "ready": False}
    names = [n for n, (v, i) in trackers.items() if v and i]
    if not names or len(names) < len(trackers):
        return unknown
    grid = [start + k * VMODEL_GRID_S for k in range(int((end - start) // VMODEL_GRID_S))]
    if not grid:
        return unknown
    volts = {n: _held(trackers[n][0], grid) for n in names}
    amps = {n: _held(trackers[n][1], grid) for n in names}
    steady = {n: _steady(amps[n]) for n in names}
    scan_max = {n: _rolling_max(volts[n], int(VMODEL_SCAN_WINDOW_S // VMODEL_GRID_S)) for n in names}
    clean_export: list[bool] | None = None
    if export and export_limit_kw > 0:
        exp = _held(export, grid)
        w = int(VMODEL_CLEAN_EXPORT_WINDOW_S // VMODEL_GRID_S)
        peak = _rolling_max(exp, w)
        clean_export = [p is not None and p < VMODEL_CLEAN_EXPORT_FRAC * export_limit_kw for p in peak]

    ready_before = all(model.ready(n) for n in names)
    # Before the model is ready, the bypass-spread test needs each tracker's usual share
    # of the slot voltage; without one, a multi-tracker slot cannot be judged for shading,
    # so it is not trained on (its medians still count toward the shares).
    shares = {n: model.share(n) for n in names}
    spread_ref_known = ready_before or len(names) < 2 or all(v is not None for v in shares.values())
    excess_s = 0.0
    ratios: list[float] = []
    judged: dict[str, list[float]] = {n: [] for n in names}
    for k, t in enumerate(grid):
        vs = [volts[n][k] for n in names]
        cs = [amps[n][k] for n in names]
        if any(v is None or c is None for v, c in zip(vs, cs, strict=True)):
            continue
        v_ok = [v for v in vs if v is not None]
        c_ok = [c for c in cs if c is not None]
        if min(c_ok) < VMODEL_MIN_CURRENT_A or not all(steady[n][k] for n in names):
            continue
        if any(v < VMODEL_SCAN_RATIO * (scan_max[n][k] or v) for n, v in zip(names, v_ok, strict=True)):
            continue  # An MPPT scan sweeps the voltage down for a few seconds.
        g = g_at(t)
        if g < VMODEL_MIN_G:
            continue  # Outside the fitted range, and too dim for an export limit to bind.
        for n, v in zip(names, v_ok, strict=True):
            judged[n].append(v)
        x = features(temp, g)
        preds = [model.fit(n).predict(x) for n in names]
        if len(names) >= 2:
            if not spread_ref_known:
                continue
            refs = preds if ready_before else [shares[n] for n in names]
            rel = [v / r for v, r in zip(v_ok, refs, strict=True) if r]
            if len(rel) < len(names) or max(rel) > (1 + VMODEL_SHADE_SPREAD) * min(rel):
                continue  # Bypass diodes: shading lowers one tracker against its own normal. Never limiting.
        if ready_before:
            per_tracker = [v / p for v, p in zip(v_ok, preds, strict=True) if p]
            if len(per_tracker) < len(names):
                continue
            ratio = min(per_tracker)
            ratios.append(ratio)
            if ratio >= 1 + VMODEL_EXCESS:
                excess_s += VMODEL_GRID_S
                continue  # Above the model on every tracker: limited, so never trained on.
        if clean_export is not None and not clean_export[k]:
            continue
        for n, v, p in zip(names, v_ok, preds, strict=True):
            fit = model.fit(n)
            if (
                clean_export is None
                and p is not None
                and fit.sigma is not None
                and v > p + VMODEL_OUTLIER_SIGMA * fit.sigma
            ):
                continue  # No export series: a sample far above the fit is likely limited.
            fit.add(x, v)
    if len(names) >= 2 and all(len(judged[n]) >= VMODEL_SHARE_MIN_SAMPLES for n in names):
        model.add_shares({n: statistics.median(judged[n]) for n in names})
    for n in names:
        model.fit(n).solve()
    if not ready_before:
        return unknown
    ratios.sort()
    return {
        "vexcess_s": excess_s,
        "vratio_p50": round(ratios[len(ratios) // 2], 4) if ratios else -1.0,
        "vratio_p90": round(ratios[int(0.9 * (len(ratios) - 1))], 4) if ratios else -1.0,
        "ready": True,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _held(samples: Samples, grid: Sequence[float]) -> list[float | None]:
    times = [t for t, _ in samples]
    out: list[float | None] = []
    for t in grid:
        i = bisect.bisect_right(times, t) - 1
        out.append(samples[i][1] if i >= 0 else None)
    return out


def _rolling_max(values: Sequence[float | None], half: int) -> list[float | None]:
    out: list[float | None] = []
    for k in range(len(values)):
        window = [v for v in values[max(0, k - half) : k + half + 1] if v is not None]
        out.append(max(window) if window else None)
    return out


def _steady(values: Sequence[float | None]) -> list[bool]:
    """Coefficient of variation over ±VMODEL_STEADY_WINDOW_S below VMODEL_STEADY_CV (prefix sums)."""
    half = int(VMODEL_STEADY_WINDOW_S // VMODEL_GRID_S)
    n = len(values)
    s1 = [0.0] * (n + 1)
    s2 = [0.0] * (n + 1)
    cnt = [0] * (n + 1)
    for k, v in enumerate(values):
        s1[k + 1] = s1[k] + (v or 0.0)
        s2[k + 1] = s2[k] + (v or 0.0) ** 2
        cnt[k + 1] = cnt[k] + (v is not None)
    out = []
    for k in range(n):
        a, b = max(0, k - half), min(n, k + half + 1)
        m = cnt[b] - cnt[a]
        if m < 2:
            out.append(False)
            continue
        mean = (s1[b] - s1[a]) / m
        var = max((s2[b] - s2[a]) / m - mean * mean, 0.0)
        out.append(mean > 0 and math.sqrt(var) / mean < VMODEL_STEADY_CV)
    return out


def _solve(a: Sequence[Sequence[float]], b: Sequence[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x
