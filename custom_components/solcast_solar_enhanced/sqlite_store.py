"""Built-in SQLite storage for Solcast Solar Enhanced.

A zero-configuration store: a single file in the HA config directory, backed by
the Python standard-library ``sqlite3`` module (no third-party dependency).
Blocking calls run in HA's executor, serialised by a lock. The schema is created
fresh and complete, so there is no migration machinery and ``has_site_col`` /
``has_battery_col`` are always true.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any

from homeassistant.core import HomeAssistant

from .pv_tuning import solar_position

_LOGGER = logging.getLogger(__name__)

# Bumped when stored data needs a one-time, in-place repair. v1 recomputes the
# solar ``azimuth`` column for rows written before the hour-angle wrap fix (an
# east<->west mirror for sites whose local morning/afternoon fell on a different
# UTC day from solar noon). Tracked via SQLite's built-in PRAGMA user_version so
# the repair runs silently once and never re-scans on later starts.
SCHEMA_VERSION = 1

# Default site identifier for single-site / aggregate rows. Kept in sync with
# const.DEFAULT_SITE_ID (imported lazily-free to avoid a const import cycle here).
DEFAULT_SITE = "_total"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS solcast_data (
  "index"          INTEGER PRIMARY KEY AUTOINCREMENT,
  period_end       TEXT NOT NULL,
  period_end_epoch INTEGER NOT NULL,
  period_start     TEXT NOT NULL,
  site             TEXT NOT NULL DEFAULT '_total',
  pv_actual        REAL NOT NULL,
  pv_export        REAL NOT NULL DEFAULT 0,
  pv_estimate      REAL NOT NULL,
  pv_estimate10    REAL NOT NULL,
  pv_estimate90    REAL NOT NULL,
  azimuth          REAL NOT NULL,
  zenith           REAL NOT NULL,
  temp             REAL NOT NULL,
  clouds           INTEGER NOT NULL,
  description      TEXT NOT NULL,
  battery_charge   REAL NOT NULL DEFAULT 0,
  UNIQUE(period_end_epoch, site)
);
"""

# Columns written by an insert, in order. Shared by single and bulk inserts.
_INSERT_COLUMNS = (
    "period_end", "period_end_epoch", "period_start", "site",
    "pv_actual", "pv_export", "pv_estimate", "pv_estimate10", "pv_estimate90",
    "azimuth", "zenith", "temp", "clouds", "description", "battery_charge",
)
_INSERT_SQL = (
    "INSERT OR IGNORE INTO solcast_data ("
    + ", ".join(_INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _INSERT_COLUMNS)
    + ")"
)


class SqliteStore:
    """File-backed SQLite store (async API, all I/O via the executor)."""

    def __init__(self, hass: HomeAssistant, path: str, readonly: bool = False) -> None:
        self._hass = hass
        self._path = path
        self._readonly = readonly
        self._conn: sqlite3.Connection | None = None
        # sqlite3 connections aren't safe to share across threads without
        # serialisation; every executor call holds this lock.
        self._lock = threading.Lock()
        # Always present on a fresh schema (kept for query-builder symmetry).
        self.has_battery_col = True
        self.has_site_col = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_connect(self) -> bool:
        """Open the file and initialise the schema. Returns True on success."""
        return await self._hass.async_add_executor_job(self._connect)

    def _connect(self) -> bool:
        try:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            if not self._readonly:
                conn.executescript(CREATE_TABLE_SQL)
                conn.commit()
            self._conn = conn
            # Surface the file path + current row count so users know where the
            # store lives (e.g. to point sqlite-web at it) and that it loaded.
            try:
                row = conn.execute("SELECT COUNT(*) FROM solcast_data").fetchone()
                count = int(row[0]) if row else 0
            except Exception:  # noqa: BLE001 — table may be absent on a read-only first open
                count = 0
            _LOGGER.info("Built-in store ready at %s — %d row(s)", self._path, count)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SQLite open failed (%s): %s", self._path, exc)
            return False

    async def async_close(self) -> None:
        """Close the connection."""
        await self._hass.async_add_executor_job(self._close)

    def _close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @staticmethod
    def _row_values(record: dict[str, Any]) -> tuple[Any, ...]:
        return (
            record["period_end"],
            record["period_end_epoch"],
            record["period_start"],
            record.get("site", DEFAULT_SITE) or DEFAULT_SITE,
            record["pv_actual"],
            record.get("pv_export", 0.0) or 0.0,
            record["pv_estimate"],
            record["pv_estimate10"],
            record["pv_estimate90"],
            record["azimuth"],
            record["zenith"],
            record["temp"],
            record["clouds"],
            record["description"],
            record.get("battery_charge", 0.0) or 0.0,
        )

    async def async_insert_record(self, record: dict[str, Any]) -> bool:
        """Insert one record. Duplicate (epoch, site) is ignored. True on success."""
        if self._conn is None or self._readonly:
            return False
        return await self._hass.async_add_executor_job(self._insert_many, [record])

    def _insert_many(self, records: list[dict[str, Any]]) -> bool:
        try:
            rows = [self._row_values(r) for r in records]
        except KeyError as exc:
            _LOGGER.error("SQLite insert skipped — record missing field %s", exc)
            return False
        try:
            with self._lock:
                self._conn.executemany(_INSERT_SQL, rows)
                self._conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SQLite insert failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    async def async_prune(self, retention_days: int) -> int:
        """Delete rows older than ``retention_days`` days. Returns rows removed.

        A no-op when ``retention_days <= 0`` (the default — keep everything) or in
        read-only mode. A plain ``DELETE`` (no ``VACUUM``) is intentional: in the
        steady state old rows are deleted as fast as new ones arrive, so SQLite
        reuses the freed pages and the file size stabilises without the heavy I/O
        of a rewrite — important on an SD-card-backed Raspberry Pi.
        """
        if self._conn is None or self._readonly or retention_days <= 0:
            return 0
        return await self._hass.async_add_executor_job(self._prune, retention_days)

    def _prune(self, retention_days: int) -> int:
        cutoff = int(time.time()) - retention_days * 86400
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM solcast_data WHERE period_end_epoch < ?", (cutoff,)
                )
                self._conn.commit()
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SQLite prune failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def async_migrate(self, latitude: float, longitude: float) -> int:
        """Run any pending one-time data repairs. Returns rows changed.

        Silent and idempotent: gated on PRAGMA user_version, so it scans once and
        is a no-op on every later start (and on a fresh, empty database).
        """
        if self._conn is None or self._readonly:
            return 0
        return await self._hass.async_add_executor_job(
            self._migrate_azimuth, latitude, longitude
        )

    def _migrate_azimuth(self, latitude: float, longitude: float) -> int:
        try:
            with self._lock:
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
                if version >= SCHEMA_VERSION:
                    return 0
                rows = self._conn.execute(
                    'SELECT "index", period_end_epoch, azimuth FROM solcast_data'
                ).fetchall()
                # Solar azimuth depends only on epoch + site lat/lon (it is the sun
                # position, shared by every site on the property), so recompute it
                # from each row's stored epoch at the interval midpoint (epoch-900),
                # matching how it was originally written. Only rewrite rows whose
                # value actually moved, to avoid needless SD-card writes.
                updates = [
                    (round(new_az, 5), idx)
                    for idx, epoch, old_az in rows
                    if abs(
                        (new_az := solar_position(int(epoch) - 900, latitude, longitude)[0])
                        - (old_az or 0.0)
                    )
                    > 0.01
                ]
                if updates:
                    self._conn.executemany(
                        'UPDATE solcast_data SET azimuth = ? WHERE "index" = ?', updates
                    )
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()
            if updates:
                _LOGGER.info(
                    "Repaired solar azimuth on %d of %d stored row(s)",
                    len(updates), len(rows),
                )
            return len(updates)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Azimuth repair failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def async_get_record_count(self) -> int:
        """Return total record count."""
        if self._conn is None:
            return 0
        return await self._hass.async_add_executor_job(self._record_count)

    def _record_count(self) -> int:
        try:
            with self._lock:
                cur = self._conn.execute("SELECT COUNT(*) FROM solcast_data")
                row = cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def _site_filter(self, site: str | None) -> tuple[str, tuple[Any, ...]]:
        """Build an optional ``AND site = ?`` clause for site-scoped queries."""
        if site is None:
            return "", ()
        return " AND site = ?", (site,)

    async def async_get_sites(self) -> list[str]:
        """Return the distinct site identifiers present in the table."""
        if self._conn is None:
            return []
        return await self._hass.async_add_executor_job(self._sites)

    def _sites(self) -> list[str]:
        try:
            with self._lock:
                cur = self._conn.execute("SELECT DISTINCT site FROM solcast_data")
                rows = cur.fetchall()
            return [r[0] for r in rows if r and r[0] is not None]
        except Exception:  # noqa: BLE001
            return []

    async def async_get_records_for_dampening(
        self,
        slot_doy: int,
        window_days: int = 14,
        site: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch records within ±window_days calendar day-of-year across all years."""
        if self._conn is None:
            return []
        site_clause, site_params = self._site_filter(site)
        sql = (
            "SELECT pv_actual, pv_export, pv_estimate, pv_estimate10, "
            "pv_estimate90, azimuth, zenith, clouds, "
            "COALESCE(battery_charge, 0.0) AS battery_charge "
            "FROM solcast_data "
            "WHERE pv_actual > 0 AND pv_estimate > 0 "
            # strftime('%j', epoch, 'unixepoch') renders day-of-year in UTC,
            # matching the MySQL backend's UTC-pinned FROM_UNIXTIME/DAYOFYEAR.
            "AND ABS(CAST(strftime('%j', period_end_epoch, 'unixepoch') AS INTEGER) - ?) <= ?"
            f"{site_clause}"
        )
        params = (slot_doy, window_days, *site_params)
        return await self._hass.async_add_executor_job(self._query, sql, params)

    async def async_get_records_for_tuning(
        self, limit: int = 2000, site: str | None = None, cloud_max: int | None = None
    ) -> list[dict[str, Any]]:
        """Fetch recent records for PV tuning.

        When ``cloud_max`` is given, the clear-sky filter (``clouds < cloud_max``)
        is applied **in SQL before the LIMIT**, so the result is the most recent
        ``limit`` *clear-sky* rows rather than the most recent rows of any weather
        (of which only a few may be clear in a cloudy season). This lets tuning use
        clear-sky data spanning all seasons — the sun-angle diversity that actually
        constrains tilt/azimuth — instead of a recent cloudy window.
        """
        if self._conn is None:
            return []
        site_clause, site_params = self._site_filter(site)
        cloud_clause, cloud_params = "", ()
        if cloud_max is not None:
            cloud_clause = " AND clouds < ?"
            cloud_params = (int(cloud_max),)
        sql = (
            "SELECT pv_actual, pv_export, pv_estimate, azimuth, zenith, clouds, "
            "COALESCE(battery_charge, 0.0) AS battery_charge "
            "FROM solcast_data "
            f"WHERE pv_actual > 0{site_clause}{cloud_clause} "
            "ORDER BY period_end_epoch DESC LIMIT ?"
        )
        params = (*site_params, *cloud_params, limit)
        return await self._hass.async_add_executor_job(self._query, sql, params)

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SQLite query failed: %s", exc)
            return []

    @property
    def available(self) -> bool:
        return self._conn is not None
