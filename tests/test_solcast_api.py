"""Tests for the OWM client, including shared-session reuse."""
from __future__ import annotations

import pytest

from custom_components.solcast_solar_enhanced.solcast_api import OWMClient

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
    assert result == {"temp": 0.0, "clouds": 0, "description": "unavailable"}
