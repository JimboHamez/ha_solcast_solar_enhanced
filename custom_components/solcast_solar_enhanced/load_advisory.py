"""Short-horizon load-scheduling decision aids (item 3).

Computes a **confidence** signal — how well the site's recently measured output
agrees with the base Solcast forecast — as a decision aid for scheduling heavy
loads (EV, pool pump, hot water). This is **not** a forecast and never overwrites
the base forecast; it only annotates how much the next few hours can be trusted at
this specific site, using measured ground truth the base integration never sees.
"""

from __future__ import annotations

import math
from typing import Any

# Confidence maps the recent bias to 0–100 via exp(−|ln(bias)| / scale). The scale
# is chosen so output tracking the forecast within ±18% reads "high", within ±40%
# reads "medium", and a larger divergence reads "low" (see the band thresholds).
CONFIDENCE_SCALE = 0.45
CONFIDENCE_HIGH = 67
CONFIDENCE_MEDIUM = 34

# How far back recent slots are gathered for the bias, and the forward horizon the
# resulting confidence is meant to inform (the window where local persistence skill
# beats the forecast). Both in their natural units.
RECENT_BIAS_LOOKBACK_S = 4 * 3600
CONFIDENCE_HORIZON_HOURS = 3


def compute_confidence(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    """Confidence that the next few hours' forecast can be trusted at this site.

    ``pairs`` are recent ``(pv_actual, pv_estimate)`` half-hour slots (daylight, with
    a non-zero estimate). The energy-weighted recent bias ``Σ actual / Σ estimate``
    is mapped to 0–100: ~100 when output tracks the forecast, falling as they diverge
    (local cloud, shading, or a bias the forecast hasn't caught).

    Returns ``confidence`` (int 0–100 or ``None``), ``rating``
    (``high``/``medium``/``low``/``unknown``), ``recent_bias`` and ``n_slots``.
    """
    usable = [(a, e) for a, e in pairs if e > 0 and a >= 0]
    sum_e = sum(e for _, e in usable)
    if not usable or sum_e <= 0:
        return {"confidence": None, "rating": "unknown", "recent_bias": None, "n_slots": 0}
    bias = sum(a for a, _ in usable) / sum_e
    conf = round(100 * math.exp(-abs(math.log(bias)) / CONFIDENCE_SCALE)) if bias > 0 else 0
    conf = max(0, min(100, conf))
    if conf >= CONFIDENCE_HIGH:
        rating = "high"
    elif conf >= CONFIDENCE_MEDIUM:
        rating = "medium"
    else:
        rating = "low"
    return {"confidence": conf, "rating": rating, "recent_bias": round(bias, 3), "n_slots": len(usable)}
