"""MySQL database manager for Solcast Solar Enhanced."""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

try:
    import aiomysql
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    _LOGGER.info("aiomysql not installed — database features disabled")

# Default site identifier for single-site / aggregate rows (and back-fill of
# pre-multi-site data). Kept in sync with const.DEFAULT_SITE_ID.
DEFAULT_SITE = "_total"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS solcast_data (
  `index`          INT AUTO_INCREMENT PRIMARY KEY,
  period_end       TEXT NOT NULL,
  period_end_epoch BIGINT NOT NULL,
  period_start     TEXT NOT NULL,
  site             VARCHAR(64) NOT NULL DEFAULT '_total',
  pv_actual        DECIMAL(10,4) NOT NULL,
  pv_export        DECIMAL(10,4) NOT NULL DEFAULT 0.0000,
  pv_estimate      DECIMAL(10,4) NOT NULL,
  pv_estimate10    DECIMAL(10,4) NOT NULL,
  pv_estimate90    DECIMAL(10,4) NOT NULL,
  azimuth          DECIMAL(10,5) NOT NULL,
  zenith           DECIMAL(10,5) NOT NULL,
  temp             DECIMAL(10,2) NOT NULL,
  clouds           INT NOT NULL,
  description      TEXT NOT NULL,
  battery_charge   DECIMAL(10,4) NOT NULL DEFAULT 0.0000,
  UNIQUE KEY uq_epoch_site (period_end_epoch, site),
  INDEX idx_period_end ((CAST(period_end AS CHAR(25))))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
"""

ADD_BATTERY_COL_SQL = """
ALTER TABLE solcast_data
  ADD COLUMN IF NOT EXISTS battery_charge DECIMAL(10,4) NOT NULL DEFAULT 0.0000;
"""

ADD_PV_EXPORT_COL_SQL = """
ALTER TABLE solcast_data
  ADD COLUMN IF NOT EXISTS pv_export DECIMAL(10,4) NOT NULL DEFAULT 0.0000;
"""

ADD_SITE_COL_SQL = """
ALTER TABLE solcast_data
  ADD COLUMN IF NOT EXISTS site VARCHAR(64) NOT NULL DEFAULT '_total';
"""


class DbManager:
    """Manages async MySQL connection pool and schema."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        db: str,
        readonly: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._db = db
        self._readonly = readonly
        self._pool: Any = None
        self.has_battery_col = True
        self.has_site_col = True

    async def async_connect(self) -> bool:
        """Create pool and initialise schema. Returns True on success."""
        if not DB_AVAILABLE:
            _LOGGER.warning("aiomysql unavailable — DB features disabled")
            return False
        try:
            self._pool = await aiomysql.create_pool(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                db=self._db,
                autocommit=True,
                minsize=1,
                maxsize=5,
                # Pin each connection's session to UTC so FROM_UNIXTIME() renders
                # deterministically regardless of the server's local time zone
                # (the day-of-year window query relies on this).
                init_command="SET time_zone = '+00:00'",
            )
            if not self._readonly:
                await self._init_schema()
            await self._detect_columns()
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("DB connection failed: %s", exc)
            return False

    async def _init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'solcast_data'",
                    (self._db,),
                )
                row = await cur.fetchone()
                if not (row and row[0] > 0):
                    await cur.execute(CREATE_TABLE_SQL)
                try:
                    await cur.execute(ADD_BATTERY_COL_SQL)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await cur.execute(ADD_PV_EXPORT_COL_SQL)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await cur.execute(ADD_SITE_COL_SQL)
                except Exception:  # noqa: BLE001
                    pass
                await self._migrate_unique_key(cur)

    async def _migrate_unique_key(self, cur: Any) -> None:
        """Replace the legacy single-column unique key with (period_end_epoch, site).

        MySQL has no ``DROP INDEX IF EXISTS``, so existence is checked first.
        Back-filled rows all carry site '_total', so the composite key stays
        unique and the swap is safe on existing data.
        """
        try:
            await cur.execute(
                "SELECT COUNT(*) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'solcast_data' "
                "AND INDEX_NAME = 'uq_epoch_site'",
                (self._db,),
            )
            row = await cur.fetchone()
            if row and row[0] > 0:
                return  # already migrated
            # Add the new composite unique key, then drop the legacy one.
            await cur.execute(
                "ALTER TABLE solcast_data "
                "ADD UNIQUE KEY uq_epoch_site (period_end_epoch, site)"
            )
            await cur.execute(
                "SELECT COUNT(*) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'solcast_data' "
                "AND INDEX_NAME = 'uq_epoch'",
                (self._db,),
            )
            row = await cur.fetchone()
            if row and row[0] > 0:
                await cur.execute("ALTER TABLE solcast_data DROP INDEX uq_epoch")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Unique-key migration skipped: %s", exc)

    async def _detect_columns(self) -> None:
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'solcast_data' "
                        "AND COLUMN_NAME = 'battery_charge'",
                        (self._db,),
                    )
                    row = await cur.fetchone()
                    self.has_battery_col = bool(row and row[0] > 0)
                    await cur.execute(
                        "SELECT COUNT(*) FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'solcast_data' "
                        "AND COLUMN_NAME = 'site'",
                        (self._db,),
                    )
                    row = await cur.fetchone()
                    self.has_site_col = bool(row and row[0] > 0)
        except Exception:  # noqa: BLE001
            self.has_battery_col = False
            self.has_site_col = False

    async def async_insert_record(self, record: dict[str, Any]) -> bool:
        """Insert a single record. Ignores duplicate (epoch, site). Returns True on success."""
        if not self._pool or self._readonly:
            return False
        battery = record.get("battery_charge", 0.0) or 0.0
        site = record.get("site", DEFAULT_SITE) or DEFAULT_SITE
        # When the site column hasn't been migrated yet, fall back to the legacy
        # column set so inserts still succeed.
        if self.has_site_col:
            columns = (
                "(period_end, period_end_epoch, period_start, site, "
                " pv_actual, pv_export, pv_estimate, pv_estimate10, pv_estimate90, "
                " azimuth, zenith, temp, clouds, description, battery_charge)"
            )
            placeholders = "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            values: tuple[Any, ...] = (
                record["period_end"],
                record["period_end_epoch"],
                record["period_start"],
                site,
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
                battery,
            )
        else:
            columns = (
                "(period_end, period_end_epoch, period_start, "
                " pv_actual, pv_export, pv_estimate, pv_estimate10, pv_estimate90, "
                " azimuth, zenith, temp, clouds, description, battery_charge)"
            )
            placeholders = "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            values = (
                record["period_end"],
                record["period_end_epoch"],
                record["period_start"],
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
                battery,
            )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"INSERT IGNORE INTO solcast_data {columns} {placeholders}",
                        values,
                    )
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("DB insert failed: %s", exc)
            return False

    async def async_get_record_count(self) -> int:
        """Return total record count."""
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT COUNT(*) FROM solcast_data")
                    row = await cur.fetchone()
                    return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def _site_filter(self, site: str | None) -> tuple[str, tuple[Any, ...]]:
        """Build an optional ``AND site = %s`` clause.

        Returns ``("", ())`` when no site is requested or the column is absent,
        preserving the pre-multi-site aggregate behaviour.
        """
        if site is None or not self.has_site_col:
            return "", ()
        return " AND site = %s", (site,)

    async def async_get_records_for_dampening(
        self,
        slot_doy: int,
        window_days: int = 14,
        site: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch records within ±window_days calendar day-of-year across all years.

        When ``site`` is given, restrict to that site; otherwise aggregate across
        all rows (legacy behaviour).
        """
        if not self._pool:
            return []
        battery_col = (
            "COALESCE(battery_charge, 0.0) AS battery_charge"
            if self.has_battery_col
            else "0.0 AS battery_charge"
        )
        site_clause, site_params = self._site_filter(site)
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        f"SELECT pv_actual, pv_export, pv_estimate, pv_estimate10, "
                        f"pv_estimate90, azimuth, zenith, clouds, {battery_col} "
                        f"FROM solcast_data "
                        f"WHERE pv_actual > 0 AND pv_estimate > 0 "
                        # Session tz is pinned to UTC (see async_connect), so
                        # FROM_UNIXTIME renders in UTC — no CONVERT_TZ needed.
                        f"AND ABS(DAYOFYEAR(FROM_UNIXTIME(period_end_epoch)) - %s) <= %s"
                        f"{site_clause}",
                        (slot_doy, window_days, *site_params),
                    )
                    return await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("DB dampening query failed: %s", exc)
            return []

    async def async_get_sites(self) -> list[str]:
        """Return the distinct site identifiers present in the table."""
        if not self._pool or not self.has_site_col:
            return []
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT DISTINCT site FROM solcast_data")
                    rows = await cur.fetchall()
                    return [r[0] for r in rows if r and r[0] is not None]
        except Exception:  # noqa: BLE001
            return []

    async def async_get_records_for_tuning(
        self, limit: int = 2000, site: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch recent records for PV tuning.

        When ``site`` is given, restrict to that site; otherwise aggregate across
        all rows (legacy behaviour).
        """
        if not self._pool:
            return []
        battery_col = (
            "COALESCE(battery_charge, 0.0) AS battery_charge"
            if self.has_battery_col
            else "0.0 AS battery_charge"
        )
        site_clause, site_params = self._site_filter(site)
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        f"SELECT pv_actual, pv_export, pv_estimate, azimuth, zenith, clouds, "
                        f"{battery_col} "
                        f"FROM solcast_data "
                        f"WHERE pv_actual > 0{site_clause} "
                        f"ORDER BY period_end_epoch DESC LIMIT %s",
                        (*site_params, limit),
                    )
                    return await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("DB tuning query failed: %s", exc)
            return []

    async def async_get_all_records(self) -> list[dict[str, Any]]:
        """Return every row as a full record dict (used by the SQLite import).

        Columns are normalised to the record shape ``SqliteStore.async_insert_many``
        expects, filling ``site``/``battery_charge`` defaults on pre-migration data.
        """
        if not self._pool:
            return []
        site_col = "site" if self.has_site_col else "'_total' AS site"
        battery_col = (
            "COALESCE(battery_charge, 0.0) AS battery_charge"
            if self.has_battery_col
            else "0.0 AS battery_charge"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        f"SELECT period_end, period_end_epoch, period_start, "
                        f"{site_col}, pv_actual, pv_export, pv_estimate, "
                        f"pv_estimate10, pv_estimate90, azimuth, zenith, temp, "
                        f"clouds, description, {battery_col} "
                        f"FROM solcast_data ORDER BY period_end_epoch ASC"
                    )
                    return await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("DB export query failed: %s", exc)
            return []

    async def async_close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    @property
    def available(self) -> bool:
        return self._pool is not None
