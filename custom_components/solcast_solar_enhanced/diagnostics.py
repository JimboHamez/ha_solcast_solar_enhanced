"""Diagnostics for Solcast Solar Enhanced.

Everything a shading/tuning problem report needs — the effective configuration,
what the collector last read, the full half-hour dampening curve and the tuning
fit — gathered into one downloadable payload, so a user does not have to
screenshot a dozen entity attributes. The site coordinates and the
OpenWeatherMap key are redacted; the rest is measurement data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .const import CONF_LATITUDE, CONF_LONGITUDE, CONF_OWM_API_KEY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import SolcastEnhancedConfigEntry, SolcastEnhancedCoordinator

# The API key is a credential; the coordinates pin the user's home down to a few
# metres. Neither is needed to read a diagnostics dump.
TO_REDACT = {CONF_LATITUDE, CONF_LONGITUDE, CONF_OWM_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: SolcastEnhancedConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being diagnosed.

    Returns:
        A JSON-serialisable payload describing the entry's configuration and the
        coordinator's current state, with credentials and coordinates redacted.
    """
    data: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "source": entry.source,
            "state": str(entry.state),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }

    # Diagnostics can be requested for an entry that failed to set up, which never
    # reached the point of assigning runtime_data.
    coordinator: SolcastEnhancedCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is None:
        data["coordinator"] = None
        return data

    return data | coordinator.diagnostics_snapshot()
