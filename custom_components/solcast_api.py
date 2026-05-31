"""OpenWeatherMap client for Solcast Solar Enhanced."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import OWM_URL

_LOGGER = logging.getLogger(__name__)


class OWMClient:
    """Thin async OWM current-weather client."""

    def __init__(self, api_key: str, latitude: float, longitude: float) -> None:
        self._api_key = api_key
        self._lat = latitude
        self._lon = longitude

    async def async_fetch(self) -> dict[str, Any]:
        """Fetch current weather. Returns dict with temp, clouds, description."""
        params = {
            "lat": self._lat,
            "lon": self._lon,
            "appid": self._api_key,
            "units": "metric",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(OWM_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            return {
                "temp": float(data.get("main", {}).get("temp", 0.0)),
                "clouds": int(data.get("clouds", {}).get("all", 0)),
                "description": str(data.get("weather", [{}])[0].get("description", "")),
            }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("OWM fetch failed: %s", exc)
            return {"temp": 0.0, "clouds": 0, "description": "unavailable"}
