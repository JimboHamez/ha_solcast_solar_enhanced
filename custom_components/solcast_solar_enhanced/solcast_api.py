"""Weather/irradiance clients for Solcast Solar Enhanced."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .const import OPENMETEO_ARCHIVE_URL, OPENMETEO_FORECAST_URL, OWM_URL

_LOGGER = logging.getLogger(__name__)

# Open-Meteo variable name -> our internal key. shortwave_radiation is GHI.
_OPENMETEO_VARS = {
    "shortwave_radiation": "ghi",
    "direct_normal_irradiance": "dni",
    "diffuse_radiation": "dhi",
    "cloud_cover": "clouds",
    "temperature_2m": "temp",
}


class OWMClient:
    """Thin async OWM current-weather client."""

    def __init__(
        self,
        api_key: str,
        latitude: float,
        longitude: float,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Store credentials/location and the optional shared aiohttp session."""
        self._api_key = api_key
        self._lat = latitude
        self._lon = longitude
        # Prefer Home Assistant's shared session (passed in) to avoid building a
        # new TCP/TLS connector on every 30-min fetch. Falls back to an owned
        # session when used standalone (e.g. the tuning CLI / tests).
        self._session = session

    async def async_fetch(self) -> dict[str, Any]:
        """Fetch current weather. Returns dict with temp, clouds, description."""
        params = {
            "lat": self._lat,
            "lon": self._lon,
            "appid": self._api_key,
            "units": "metric",
        }
        try:
            if self._session is not None:
                data = await self._get(self._session, params)
            else:
                async with aiohttp.ClientSession() as session:
                    data = await self._get(session, params)
            # Missing temp/clouds in a "successful" response are treated as
            # *unknown* (None), not 0. 0 is a real, valid reading (clear sky / 0°C)
            # that the clear-sky filters trust; coercing an absent value to 0 would
            # inject a false clear-sky record. None is the fail-safe sentinel —
            # downstream treats it as overcast/unknown and excludes it.
            temp_raw = data.get("main", {}).get("temp")
            clouds_raw = data.get("clouds", {}).get("all")
            return {
                "temp": float(temp_raw) if temp_raw is not None else None,
                "clouds": int(clouds_raw) if clouds_raw is not None else None,
                "description": str(data.get("weather", [{}])[0].get("description", "")),
            }
        except Exception as exc:  # noqa: BLE001
            # aiohttp errors embed the request URL, which carries the API key in
            # its `appid` query param — redact it so the key never reaches the log.
            detail = str(exc)
            if self._api_key:
                detail = detail.replace(self._api_key, "***")
            _LOGGER.warning("OWM fetch failed: %s: %s", type(exc).__name__, detail)
            # Unknown weather → None (fail-safe): the record is excluded from
            # tuning/dampening rather than trusted as a false clear-sky reading.
            return {"temp": None, "clouds": None, "description": "unavailable"}

    @staticmethod
    async def _get(session: aiohttp.ClientSession, params: dict[str, Any]) -> dict[str, Any]:
        """Issue the GET and return parsed JSON (15s total timeout)."""
        async with session.get(OWM_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()


class OpenMeteoClient:
    """Thin async Open-Meteo client for plane-of-array irradiance components.

    Keyless. ``async_get_interval`` reads the 15-minute forecast series (which spans
    the recent past) and returns the **half-hour mean** over a collection period —
    averaging the two preceding-mean samples that tile it — to match ``pv_actual``
    (itself a half-hour average). ``async_get_archive`` pulls a bulk hourly history
    for the one-pass backfill tool. Missing values come back as ``None`` (fail-safe),
    matching :class:`OWMClient`.
    """

    # A half-hour boundary must land within half a 15-min step of a real sample for
    # that sub-interval to count; a wider gap means the API didn't cover it (skip).
    _SAMPLE_TOLERANCE_S = 450

    def __init__(
        self,
        latitude: float,
        longitude: float,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Store location and the optional shared aiohttp session."""
        self._lat = latitude
        self._lon = longitude
        self._session = session

    async def async_get_interval(self, period_end_epoch: int) -> dict[str, Any]:
        """Half-hour-mean irradiance/weather for the period ending at ``period_end_epoch`` (UTC).

        Open-Meteo ``minutely_15`` radiation is a **preceding-15-minute mean** (the
        timestamp marks the *end* of its interval — confirmed against the docs and
        live data), so the half-hour ``[period_end − 30 min, period_end)`` is tiled
        exactly by the two samples timestamped at ``period_end − 15 min`` and
        ``period_end``. Averaging them yields a true half-hour mean that matches
        ``pv_actual`` (also a half-hour average), rather than a single point sample
        that represents only one 15-minute half of the period.

        Whichever of the two samples are present within tolerance are averaged; a
        value is ``None`` when neither is (fail-safe).
        """
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "minutely_15": ",".join(_OPENMETEO_VARS),
            "past_days": 1,
            "forecast_days": 1,
            "timezone": "UTC",
        }
        try:
            data = await self._fetch(params)
            series = data.get("minutely_15", {})
            times = series.get("time") or []
            if not times:
                return self._empty()
            epochs = [self._iso_to_epoch(t) for t in times]
            # The two preceding-mean samples that tile the half-hour period.
            idxs: list[int] = []
            for target in (period_end_epoch - 900, period_end_epoch):
                i = self._nearest_index(epochs, target)
                if i is not None and i not in idxs:
                    idxs.append(i)
            if not idxs:
                return self._empty()
            out: dict[str, Any] = {}
            for var, key in _OPENMETEO_VARS.items():
                col = series.get(var, [None] * len(times))
                vals = [v for v in (self._num(col[i]) for i in idxs) if v is not None]
                out[key] = sum(vals) / len(vals) if vals else None
            return out
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Open-Meteo fetch failed: %s: %s", type(exc).__name__, exc)
            return self._empty()

    async def async_get_archive(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Bulk hourly history (``YYYY-MM-DD`` bounds, inclusive, UTC).

        Returns one dict per hour with ``epoch`` plus the irradiance/weather keys;
        used by the backfill tool (which interpolates these to each row midpoint).
        """
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "hourly": ",".join(_OPENMETEO_VARS),
            "start_date": start_date,
            "end_date": end_date,
            "timezone": "UTC",
        }
        data = await self._fetch(params, archive=True)
        series = data.get("hourly", {})
        times = series.get("time") or []
        out: list[dict[str, Any]] = []
        for i, t in enumerate(times):
            row: dict[str, Any] = {"epoch": self._iso_to_epoch(t)}
            for var, key in _OPENMETEO_VARS.items():
                row[key] = self._num(series.get(var, [None] * len(times))[i])
            out.append(row)
        return out

    async def _fetch(self, params: dict[str, Any], archive: bool = False) -> dict[str, Any]:
        url = OPENMETEO_ARCHIVE_URL if archive else OPENMETEO_FORECAST_URL
        timeout = aiohttp.ClientTimeout(total=60 if archive else 15)
        if self._session is not None:
            async with self._session.get(url, params=params, timeout=timeout) as resp:
                resp.raise_for_status()
                return await resp.json()
        async with aiohttp.ClientSession() as session, session.get(url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json()

    @classmethod
    def _nearest_index(cls, epochs: list[int], target: int) -> int | None:
        """Index of the sample nearest ``target``, or ``None`` if none is within tolerance."""
        best_i, best_gap = None, None
        for i, e in enumerate(epochs):
            gap = abs(e - target)
            if best_gap is None or gap < best_gap:
                best_i, best_gap = i, gap
        if best_i is None or best_gap > cls._SAMPLE_TOLERANCE_S:
            return None
        return best_i

    @staticmethod
    def _iso_to_epoch(iso: str) -> int:
        """Open-Meteo timestamps are naive UTC (timezone=UTC requested)."""
        return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp())

    @staticmethod
    def _num(value: Any) -> float | None:
        return float(value) if value is not None else None

    @staticmethod
    def _empty() -> dict[str, Any]:
        return dict.fromkeys(_OPENMETEO_VARS.values())
