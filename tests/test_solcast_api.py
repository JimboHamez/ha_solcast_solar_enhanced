"""Tests for the OWM client, including shared-session reuse."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from custom_components.solcast_solar_enhanced.solcast_api import (
    OpenMeteoClient,
    OWMClient,
)

_OWM_PAYLOAD = {
    "main": {"temp": 12.3},
    "clouds": {"all": 75},
    "weather": [{"description": "broken clouds"}],
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in that records calls."""

    def __init__(self, payload):
        self._payload = payload
        self.get_calls = 0
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.get_calls += 1
        self.last_params = params
        return _FakeResponse(self._payload)


async def test_owm_fetch_parses_payload():
    session = _FakeSession(_OWM_PAYLOAD)
    client = OWMClient("key", -37.9, 145.0, session=session)
    result = await client.async_fetch()
    assert result == {"temp": 12.3, "clouds": 75, "description": "broken clouds"}


async def test_owm_fetch_reuses_injected_session():
    """The shared HA session is used directly — no per-fetch session created."""
    session = _FakeSession(_OWM_PAYLOAD)
    client = OWMClient("key", -37.9, 145.0, session=session)
    await client.async_fetch()
    await client.async_fetch()
    assert session.get_calls == 2
    assert session.last_params["appid"] == "key"


async def test_owm_fetch_handles_errors_gracefully():
    class _Boom(_FakeSession):
        def get(self, url, params=None, timeout=None):
            raise RuntimeError("network down")

    client = OWMClient("key", -37.9, 145.0, session=_Boom(_OWM_PAYLOAD))
    result = await client.async_fetch()
    # Unknown weather → None (fail-safe), NOT 0. A 0 would read as clear sky and
    # be trusted by the tuning/dampening clear-sky filters.
    assert result == {"temp": None, "clouds": None, "description": "unavailable"}


async def test_owm_fetch_missing_clouds_is_unknown_not_zero():
    """A success response lacking a clouds field yields None, not a false 0%."""
    session = _FakeSession({"main": {"temp": 9.0}, "weather": [{"description": "x"}]})
    client = OWMClient("key", -37.9, 145.0, session=session)
    result = await client.async_fetch()
    assert result["clouds"] is None
    assert result["temp"] == 9.0


async def test_owm_fetch_redacts_api_key_in_logs(caplog):
    """An error whose message embeds the request URL must not leak the key."""
    secret = "SUPERSECRETKEY123"

    class _Leaky(_FakeSession):
        def get(self, url, params=None, timeout=None):
            raise RuntimeError(
                f"401, message='Unauthorized', url='https://api.openweathermap.org/"
                f"data/2.5/weather?lat=-37.9&appid={secret}'"
            )

    client = OWMClient(secret, -37.9, 145.0, session=_Leaky(_OWM_PAYLOAD))
    with caplog.at_level(logging.WARNING):
        await client.async_fetch()
    assert secret not in caplog.text
    assert "***" in caplog.text


# ---------------------------------------------------------------------------
# Open-Meteo irradiance client
# ---------------------------------------------------------------------------

_NOON = int(datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp())


def _minutely(payload):
    return {"minutely_15": payload}


# 15-min samples bracketing noon; the noon sample carries the distinctive values.
_OM_FORECAST = _minutely({
    "time": [
        "2024-06-01T11:30", "2024-06-01T11:45",
        "2024-06-01T12:00", "2024-06-01T12:15",
    ],
    "shortwave_radiation": [600.0, 650.0, 700.0, 680.0],
    "direct_normal_irradiance": [500.0, 520.0, 550.0, 540.0],
    "diffuse_radiation": [150.0, 155.0, 160.0, 158.0],
    "cloud_cover": [10, 12, 15, 18],
    "temperature_2m": [18.0, 18.5, 19.0, 19.2],
})


async def test_openmeteo_current_picks_nearest_sample():
    session = _FakeSession(_OM_FORECAST)
    client = OpenMeteoClient(-37.9, 145.0, session=session)
    result = await client.async_get_current(_NOON)
    assert result == {
        "ghi": 700.0, "dni": 550.0, "dhi": 160.0, "clouds": 15.0, "temp": 19.0,
    }


async def test_openmeteo_current_out_of_tolerance_is_none():
    # All samples a full day away from the target → beyond the match tolerance.
    session = _FakeSession(_OM_FORECAST)
    client = OpenMeteoClient(-37.9, 145.0, session=session)
    result = await client.async_get_current(_NOON + 86400)
    assert result == {"ghi": None, "dni": None, "dhi": None, "clouds": None, "temp": None}


async def test_openmeteo_current_handles_errors_gracefully():
    class _Boom(_FakeSession):
        def get(self, url, params=None, timeout=None):
            raise RuntimeError("network down")

    client = OpenMeteoClient(-37.9, 145.0, session=_Boom(_OM_FORECAST))
    result = await client.async_get_current(_NOON)
    assert result == {"ghi": None, "dni": None, "dhi": None, "clouds": None, "temp": None}


async def test_openmeteo_archive_parses_series():
    payload = {"hourly": {
        "time": ["2024-06-01T00:00", "2024-06-01T01:00"],
        "shortwave_radiation": [0.0, 5.0],
        "direct_normal_irradiance": [0.0, 3.0],
        "diffuse_radiation": [0.0, 2.0],
        "cloud_cover": [100, 90],
        "temperature_2m": [8.0, 8.5],
    }}
    client = OpenMeteoClient(-37.9, 145.0, session=_FakeSession(payload))
    rows = await client.async_get_archive("2024-06-01", "2024-06-01")
    assert len(rows) == 2
    assert rows[1] == {
        "epoch": int(datetime(2024, 6, 1, 1, 0, tzinfo=timezone.utc).timestamp()),
        "ghi": 5.0, "dni": 3.0, "dhi": 2.0, "clouds": 90.0, "temp": 8.5,
    }
