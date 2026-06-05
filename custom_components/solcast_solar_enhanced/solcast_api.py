"""OpenWeatherMap client for Solcast Solar Enhanced."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import OWM_URL

_LOGGER = logging.getLogger(__name__)


class OWMClient:
    """Thin async OWM current-weather client."""

    def __init__(
        self,
        api_key: str,
        latitude: float,
        longitude: float,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
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
    async def _get(
        session: aiohttp.ClientSession, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Issue the GET and return parsed JSON (15s total timeout)."""
        async with session.get(
            OWM_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            return await resp.json()
