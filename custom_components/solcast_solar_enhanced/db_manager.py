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

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS solcast_data (
  `index`          INT AUTO_INCREMENT PRIMARY KEY,
  period_end       TEXT NOT NULL,
  period_end_epoch BIGINT NOT NULL,
  period_start     TEXT NOT NULL,
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
  UNIQUE KEY uq_epoch (period_end_epoch),
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
        except Exception:  # noqa: BLE001
            self.has_battery_col = False

    async def async_insert_record(self, record: dict[str, Any]) -> bool:
        """Insert a single record. Ignores duplicate epoch. Returns True on success."""
        if not self._pool or self._readonly:
            return False
        battery = record.get("battery_charge", 0.0) or 0.0
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT IGNORE INTO solcast_data "
                        "(period_end, period_end_epoch, period_start, "
                        " pv_actual, pv_export, pv_estimate, pv_estimate10, pv_estimate90, "
                        " azimuth, zenith, temp, clouds, description, battery_charge) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
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
                        ),
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

    async def async_get_records_for_dampening(
        self,
        slot_doy: int,
        window_days: int = 14,
    ) -> list[dict[str, Any]]:
        """Fetch records within ±window_days calendar day-of-year across all years."""
        if not self._pool:
            return []
        battery_col = (
            "COALESCE(battery_charge, 0.0) AS battery_charge"
            if self.has_battery_col
            else "0.0 AS battery_charge"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        f"SELECT pv_actual, pv_export, pv_estimate, pv_estimate10, "
                        f"pv_estimate90, azimuth, zenith, clouds, {battery_col} "
                        f"FROM solcast_data "
                        f"WHERE pv_actual > 0 AND pv_estimate > 0 "
                        f"AND ABS(DAYOFYEAR(CONVERT_TZ(FROM_UNIXTIME(period_end_epoch), '+00:00', '+00:00')) - %s) <= %s",
                        (slot_doy, window_days),
                    )
                    return await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("DB dampening query failed: %s", exc)
            return []

    async def async_get_records_for_tuning(self, limit: int = 2000) -> list[dict[str, Any]]:
        """Fetch recent records for PV tuning."""
        if not self._pool:
            return []
        battery_col = (
            "COALESCE(battery_charge, 0.0) AS battery_charge"
            if self.has_battery_col
            else "0.0 AS battery_charge"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        f"SELECT pv_actual, pv_export, pv_estimate, azimuth, zenith, clouds, "
                        f"{battery_col} "
                        f"FROM solcast_data "
                        f"WHERE pv_actual > 0 ORDER BY period_end_epoch DESC LIMIT %s",
                        (limit,),
                    )
                    return await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("DB tuning query failed: %s", exc)
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
