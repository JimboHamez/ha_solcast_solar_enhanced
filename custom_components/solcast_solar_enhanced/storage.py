"""Storage backend selection for Solcast Solar Enhanced.

Two backends implement the same async API:

- :class:`sqlite_store.SqliteStore` — the built-in, zero-config default (stdlib
  ``sqlite3``, a single file in the HA config dir).
- :class:`db_manager.DbManager` — legacy MySQL, kept for existing users and to
  power the one-time import; slated for removal once users have migrated.

``build_storage`` picks one from the entry options; the coordinator depends only
on the shared :class:`StorageBackend` surface, so its insert/query call sites are
backend-agnostic.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from homeassistant.core import HomeAssistant

from .const import (
    CONF_DB_BACKEND,
    CONF_DB_HOST,
    CONF_DB_NAME,
    CONF_DB_PASSWORD,
    CONF_DB_PORT,
    CONF_DB_READONLY,
    CONF_DB_USER,
    DB_BACKEND_BUILTIN,
    DB_BACKEND_MYSQL,
    DEFAULT_DB_FILENAME,
)
from .db_manager import DbManager
from .sqlite_store import SqliteStore

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class StorageBackend(Protocol):
    """The storage surface the coordinator relies on (both backends satisfy it)."""

    has_site_col: bool
    has_battery_col: bool

    async def async_connect(self) -> bool: ...
    async def async_close(self) -> None: ...
    async def async_insert_record(self, record: dict[str, Any]) -> bool: ...
    async def async_get_record_count(self) -> int: ...
    async def async_get_sites(self) -> list[str]: ...
    async def async_get_records_for_dampening(
        self, slot_doy: int, window_days: int = 14, site: str | None = None
    ) -> list[dict[str, Any]]: ...
    async def async_get_records_for_tuning(
        self, limit: int = 2000, site: str | None = None
    ) -> list[dict[str, Any]]: ...

    @property
    def available(self) -> bool: ...


def resolve_backend(opts: dict[str, Any]) -> str:
    """Return the storage backend id for these options.

    An explicit ``CONF_DB_BACKEND`` always wins. For entries saved before the
    option existed, infer MySQL when a non-empty ``db_user`` is configured (a real
    MySQL setup), otherwise the built-in SQLite store — so an upgrade never
    silently switches a MySQL user onto an empty local file.
    """
    backend = opts.get(CONF_DB_BACKEND)
    if backend in (DB_BACKEND_BUILTIN, DB_BACKEND_MYSQL):
        return backend
    if str(opts.get(CONF_DB_USER) or "").strip():
        return DB_BACKEND_MYSQL
    return DB_BACKEND_BUILTIN


def build_mysql_manager(opts: dict[str, Any], readonly: bool | None = None) -> DbManager:
    """Construct a MySQL :class:`DbManager` from option values (not connected)."""
    return DbManager(
        host=opts.get(CONF_DB_HOST, "localhost"),
        port=int(opts.get(CONF_DB_PORT, 3306)),
        user=opts.get(CONF_DB_USER, ""),
        password=opts.get(CONF_DB_PASSWORD, ""),
        db=opts.get(CONF_DB_NAME, "solcast"),
        readonly=bool(opts.get(CONF_DB_READONLY, False)) if readonly is None else readonly,
    )


def build_storage(hass: HomeAssistant, opts: dict[str, Any]) -> StorageBackend:
    """Construct the configured storage backend (caller still awaits async_connect)."""
    backend = resolve_backend(opts)
    if backend == DB_BACKEND_MYSQL:
        _LOGGER.debug("Using MySQL storage backend (legacy)")
        return build_mysql_manager(opts)
    _LOGGER.debug("Using built-in SQLite storage backend")
    return SqliteStore(
        hass,
        hass.config.path(DEFAULT_DB_FILENAME),
        readonly=bool(opts.get(CONF_DB_READONLY, False)),
    )
