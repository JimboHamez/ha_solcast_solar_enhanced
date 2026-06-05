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
            return {
                "temp": float(data.get("main", {}).get("temp", 0.0)),
                "clouds": int(data.get("clouds", {}).get("all", 0)),
                "description": str(data.get("weather", [{}])[0].get("description", "")),
            }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("OWM fetch failed: %s", exc)
            return {"temp": 0.0, "clouds": 0, "description": "unavailable"}

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
