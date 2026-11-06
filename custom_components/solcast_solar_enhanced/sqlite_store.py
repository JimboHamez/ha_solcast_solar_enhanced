"""Built-in SQLite storage for Solcast Solar Enhanced.

A zero-configuration store: a single file in the HA config directory, backed by
the Python standard-library ``sqlite3`` module (no third-party dependency).
Blocking calls run in HA's executor, serialised by a lock. The schema is created
fresh and complete (``has_site_col`` / ``has_battery_col`` are always true).
Columns added in later versions are applied to existing databases in place via an
additive ``ALTER TABLE`` (see ``_ADDED_COLUMNS`` / ``_ensure_columns``); one-time
*data* repairs are gated separately on ``PRAGMA user_version`` (``async_migrate``).

A new column must be added in **three** places or the schema silently drifts:
``CREATE_TABLE_SQL`` (so a fresh database is complete without relying on the
ALTER pass), ``_ADDED_COLUMNS`` (so an existing database gains it) and
``_INSERT_COLUMNS`` + ``_row_values`` (so it is actually written). Appending it
last in ``CREATE_TABLE_SQL``, in ``_ADDED_COLUMNS`` order, also keeps a fresh
database's physical column order identical to an upgraded one's. The row's type,
``models.StoreRecord``, needs the key as well so mypy accepts it.
``tests/test_sqlite_store.py`` pins all of them against each other.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from .const import CURTAILMENT_UNKNOWN, DAMPENING_WINDOW_BACK_DAYS, DAMPENING_WINDOW_FORWARD_DAYS, SHADING_ZENITH_MAX
from .pv_tuning import clearsky_ghi, solar_position

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .models import StoreRecord

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
  dc_voltage1      REAL NOT NULL DEFAULT 0,
  dc_current1      REAL NOT NULL DEFAULT 0,
  dc_voltage2      REAL NOT NULL DEFAULT 0,
  dc_current2      REAL NOT NULL DEFAULT 0,
  ghi              REAL NOT NULL DEFAULT 0,
  dni              REAL NOT NULL DEFAULT 0,
  dhi              REAL NOT NULL DEFAULT 0,
  dc_vmed1         REAL NOT NULL DEFAULT 0,
  dc_vmed2         REAL NOT NULL DEFAULT 0,
  dc_imed1         REAL NOT NULL DEFAULT 0,
  dc_imed2         REAL NOT NULL DEFAULT 0,
  pv_estimate_undampened REAL NOT NULL DEFAULT 0,
  pv_export_max    REAL NOT NULL DEFAULT 0,
  grid_v_max       REAL NOT NULL DEFAULT 0,
  grid_v_med       REAL NOT NULL DEFAULT 0,
  inverter_temp_max REAL NOT NULL DEFAULT 0,
  inverter_limited REAL NOT NULL DEFAULT -1,
  export_limit     REAL NOT NULL DEFAULT -1,
  dc_offmpp_min    REAL NOT NULL DEFAULT -1,
  UNIQUE(period_end_epoch, site)
);
"""

# Columns introduced after the original schema. On an existing database they are
# absent, so they're added in place with an additive ALTER (safe + idempotent);
# the NOT NULL DEFAULT 0 backfills existing rows. Phase-2 per-MPPT DC telemetry
# (one voltage/current pair per tracker, up to MAX_MPPT_TRACKERS) is the first
# such addition — captured for later off-MPP curtailment detection.
_ADDED_COLUMNS = (
    ("dc_voltage1", "REAL NOT NULL DEFAULT 0"),
    ("dc_current1", "REAL NOT NULL DEFAULT 0"),
    ("dc_voltage2", "REAL NOT NULL DEFAULT 0"),
    ("dc_current2", "REAL NOT NULL DEFAULT 0"),
    # Open-Meteo plane-of-array irradiance components for transposition-based
    # tuning. Backfillable on existing rows via tools/backfill_irradiance.py.
    ("ghi", "REAL NOT NULL DEFAULT 0"),
    ("dni", "REAL NOT NULL DEFAULT 0"),
    ("dhi", "REAL NOT NULL DEFAULT 0"),
    # Per-tracker MEDIAN operating voltage over the slot (the "where the MPP sat"
    # point), distinct from the max-V curtailment capture above. Forward-only
    # groundwork for the per-site shading-mechanism classifier (uniform dimming vs
    # bypass/partial-shadow) and the Vmp-band calibrator; nothing consumes it yet.
    ("dc_vmed1", "REAL NOT NULL DEFAULT 0"),
    ("dc_vmed2", "REAL NOT NULL DEFAULT 0"),
    # Per-tracker MEDIAN operating current over the slot, the companion to
    # ``dc_vmed*``. ``dc_current*`` above is the interval MINIMUM, chosen to catch
    # the most-throttled instant for off-MPP curtailment detection; that statistic
    # is unusable as a shading measure because one passing cloud pins the slot near
    # zero, and it degrades worst at low sun where a shading signal is needed most.
    # The pair (vmed, imed) is the representative operating point: a shadow line
    # across every module drops the current while the voltage holds at Vmp, whereas
    # bypass-diode conduction collapses the voltage too. Forward-only, like
    # ``dc_vmed*`` — the minimum cannot be turned back into a median after the fact.
    ("dc_imed1", "REAL NOT NULL DEFAULT 0"),
    ("dc_imed2", "REAL NOT NULL DEFAULT 0"),
    # The base integration's forecast for this slot *before* it applied our pushed
    # dampening factors. ``pv_estimate`` is read from the base's detailedForecast,
    # which is already dampened, so using it as the shading ratio's denominator
    # measures output against a forecast we ourselves corrected (issue #50). Not
    # backfillable — the base retains only ~28 days of undampened data — so 0 means
    # "unknown" and the ratio falls back to ``pv_estimate``.
    ("pv_estimate_undampened", "REAL NOT NULL DEFAULT 0"),
    # Peak instantaneous export over the slot, in kW. ``pv_export`` is a half-hour
    # MEAN, and both curtailment gates compare it against an export limit that only
    # ever holds instantaneously, so a slot capped for part of the interval averages
    # out below the limit and reads as uncapped (issue #86). The max answers the
    # question the mean cannot: did export touch the ceiling at any point? Stored as
    # a max rather than an at-limit fraction so it stays limit-agnostic — a fraction
    # bakes today's limit into the row and breaks under a DNSP dynamic limit.
    # Forward-only: the recorder keeps ~10 days, so only a short backfill is possible.
    ("pv_export_max", "REAL NOT NULL DEFAULT 0"),
    # Inverter-side curtailment capture (record-only; nothing reads these yet).
    # Grid voltage max and median over the slot, and the inverter's peak
    # temperature: 0 = unknown, since neither is ever 0 while generating.
    ("grid_v_max", "REAL NOT NULL DEFAULT 0"),
    ("grid_v_med", "REAL NOT NULL DEFAULT 0"),
    ("inverter_temp_max", "REAL NOT NULL DEFAULT 0"),
    # Fraction of the slot the inverter reported holding its output down (0-1),
    # and the export limit in force in kW. Both have a real 0, so -1 = unknown,
    # which is also what every row written before these columns existed gets.
    ("inverter_limited", "REAL NOT NULL DEFAULT -1"),
    ("export_limit", "REAL NOT NULL DEFAULT -1"),
    # Minutes of the slot every DC tracker on the inverter spent off-MPP at the
    # same moment (see OFFMPP_* in const.py). 0 is a real "none", so -1 = unknown:
    # no paired voltage/current sensors, or no recorder history for them.
    ("dc_offmpp_min", "REAL NOT NULL DEFAULT -1"),
)

# Names only, for the optional-column SELECT guard below.
_ADDED_COLUMN_NAMES = frozenset(name for name, _ in _ADDED_COLUMNS)

# Columns written by an insert, in order. Shared by single and bulk inserts.
_INSERT_COLUMNS = (
    "period_end",
    "period_end_epoch",
    "period_start",
    "site",
    "pv_actual",
    "pv_export",
    "pv_estimate",
    "pv_estimate10",
    "pv_estimate90",
    "azimuth",
    "zenith",
    "temp",
    "clouds",
    "description",
    "battery_charge",
    "dc_voltage1",
    "dc_current1",
    "dc_voltage2",
    "dc_current2",
    "ghi",
    "dni",
    "dhi",
    "dc_vmed1",
    "dc_vmed2",
    "dc_imed1",
    "dc_imed2",
    "pv_estimate_undampened",
    "pv_export_max",
    "grid_v_max",
    "grid_v_med",
    "inverter_temp_max",
    "inverter_limited",
    "export_limit",
    "dc_offmpp_min",
)
_INSERT_SQL = (
    "INSERT OR IGNORE INTO solcast_data ("
    + ", ".join(_INSERT_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _INSERT_COLUMNS)
    + ")"
)

# ----------------------------------------------------------------------
# Schema v2 (1.15.0b3.dev2): normalised tables beside the legacy one.
#
# ``user_version`` 2 = the v2 tables exist and the legacy ``solcast_data`` is
# still dual-written, so 1.15.0b2 can be reinstalled at any time and finds a
# complete table. 3 = compacted: there is no legacy table, either because it was
# dropped on request (``async_compact``) or because the file was brand new
# (``_create_compacted``: a new install has no history to protect).
#
# Each v2 column is declared once below, as ``(name, read type, default, scale)``.
# The CREATE statements, the ALTER pass, the inserts, the migration copy and the
# ``slot_rows`` view are all generated from these lists, so a new column is one
# entry here plus its ``StoreRecord`` key.
#
# **Every v2 column is stored as a scaled integer** (``round(value × scale)``),
# which halves the file: SQLite spends 8 bytes on any non-integral REAL but only
# 1-3 on a small integer. Each scale is at or below the sensor's own resolution
# (W for kW, 0.1 V, 0.01 A, 0.01°, 0.1 °C). Irradiance is 0.1 W/m²: Open-Meteo's
# half-hour value is the mean of two integers, so x.5 is common and must stay exact,
# or rows on the Kt threshold flip verdict. The view divides on the way
# out, so every reader still sees the original units. Rounding is half away from
# zero, as SQLite's ROUND, so a dual-written row and a migrated row are identical.
#
# Columns whose default is ``UNKNOWN`` keep -1 as -1 at every scale. Those are all
# non-negative when known, so a real value can never round onto the sentinel; a
# column that can be negative (temperature) has a 0 default and no sentinel.
# ----------------------------------------------------------------------
SCHEMA_V2 = 2
SCHEMA_COMPACTED = 3
# The newest layout this release understands. A file above it was written by a newer
# release (HACS lets users pick any recent version, downgrades included), so it is
# opened read-only: writing it would mix layouts, and running a migration this release
# does not know about is not possible. Raise it with every new schema version.
SCHEMA_KNOWN = SCHEMA_COMPACTED
UNKNOWN = -1.0

# Everything that is the same for every array in a slot.
V2_PROPERTY_COLUMNS: tuple[tuple[str, str, float, int], ...] = (
    ("zenith", "REAL", 0.0, 100),
    ("azimuth", "REAL", 0.0, 100),
    ("temp", "REAL", 0.0, 10),
    ("clouds", "INTEGER", 100.0, 1),
    ("ghi", "REAL", 0.0, 10),
    ("dni", "REAL", 0.0, 10),
    ("dhi", "REAL", 0.0, 10),
    ("pv_export", "REAL", 0.0, 1000),
    ("pv_export_max", "REAL", 0.0, 1000),
    ("battery_charge", "REAL", 0.0, 1000),
    ("grid_v_max", "REAL", 0.0, 10),
    ("grid_v_med", "REAL", 0.0, 10),
    ("inverter_temp_max", "REAL", 0.0, 10),
    ("inverter_limited", "REAL", UNKNOWN, 1000),
    ("export_limit", "REAL", UNKNOWN, 1000),
    # Slot-gate measures (record-only). See slot_gate.py and GateFlag in const.py.
    ("sky_dip_s", "REAL", UNKNOWN, 1),
    ("sky_dip_runs", "INTEGER", UNKNOWN, 1),
    ("export_pinned_s", "REAL", UNKNOWN, 1),
    ("vexcess_s", "REAL", UNKNOWN, 1),
    # Median and p90 of V / model (lowest tracker) over steady samples: the raw evidence
    # behind vexcess_s, so its +5% threshold can be retuned later. A ratio near 1, not a
    # residual near 0, so no real value can round onto the -1 sentinel.
    ("vratio_p50", "REAL", UNKNOWN, 1000),
    ("vratio_p90", "REAL", UNKNOWN, 1000),
    ("sample_gap_s", "REAL", UNKNOWN, 100),
    ("gate_flags", "INTEGER", UNKNOWN, 1),
    # Export-hold step counts (record-only). See step_measures.py and STEP_* in const.py.
    ("gen_steps", "INTEGER", UNKNOWN, 1),
    ("held_steps", "INTEGER", UNKNOWN, 1),
    ("regulated_steps", "INTEGER", UNKNOWN, 1),
    ("cut_steps", "INTEGER", UNKNOWN, 1),
)
# Everything that belongs to one array (site_id 0 is the property total).
V2_SITE_COLUMNS: tuple[tuple[str, str, float, int], ...] = (
    ("pv_actual", "REAL", 0.0, 1000),
    ("pv_estimate", "REAL", 0.0, 1000),
    ("pv_estimate10", "REAL", 0.0, 1000),
    ("pv_estimate90", "REAL", 0.0, 1000),
    ("pv_estimate_undampened", "REAL", 0.0, 1000),
    ("dc_voltage1", "REAL", 0.0, 10),
    ("dc_current1", "REAL", 0.0, 100),
    ("dc_voltage2", "REAL", 0.0, 10),
    ("dc_current2", "REAL", 0.0, 100),
    ("dc_vmed1", "REAL", 0.0, 10),
    ("dc_vmed2", "REAL", 0.0, 10),
    ("dc_imed1", "REAL", 0.0, 100),
    ("dc_imed2", "REAL", 0.0, 100),
    ("dc_offmpp_min", "REAL", UNKNOWN, 100),
    ("shade_dip_s", "REAL", UNKNOWN, 1),
)
V2_PROPERTY_NAMES = tuple(c[0] for c in V2_PROPERTY_COLUMNS)
V2_SITE_NAMES = tuple(c[0] for c in V2_SITE_COLUMNS)
V2_SCALES = {c[0]: c[3] for c in (*V2_PROPERTY_COLUMNS, *V2_SITE_COLUMNS)}
V2_TABLES = ("sites", "slot_property", "slot_site")


def to_stored(value: float, default: float, scale: int) -> int:
    """A value in its original units, as the scaled integer v2 stores.

    Half away from zero, matching SQLite's ``ROUND``, so a row written here and one
    copied by the migration SQL hold the same integer.
    """
    if default == UNKNOWN and value == UNKNOWN:
        return -1
    scaled = math.floor(abs(float(value)) * scale + 0.5)
    return int(-scaled if value < 0 else scaled)


def _stored_default(default: float, scale: int) -> int:
    return -1 if default == UNKNOWN else to_stored(default, default, scale)


def _col_sql(cols: tuple[tuple[str, str, float, int], ...]) -> str:
    return ", ".join(f"{n} INTEGER NOT NULL DEFAULT {_stored_default(d, sc)}" for n, _, d, sc in cols)


def _read_expr(prefix: str, name: str, kind: str, default: float, scale: int) -> str:
    """The view expression that turns a stored integer back into its original units."""
    col = f"{prefix}.{name}"
    unscaled = col if kind == "INTEGER" else f"({col} * 1.0)"
    value = unscaled if scale == 1 else f"({col} / {scale}.0)"
    if default == UNKNOWN and scale != 1:
        value = f"CASE WHEN {col} = -1 THEN -1.0 ELSE {value} END"
    return f"{value} AS {name}"


def _legacy_expr(name: str, default: float, scale: int) -> str:
    """The migration expression that scales a legacy REAL column into v2's integer."""
    scaled = f"CAST(ROUND({name} * {scale}) AS INTEGER)"
    return f"CASE WHEN {name} = -1 THEN -1 ELSE {scaled} END" if default == UNKNOWN else scaled


V2_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS sites (
  site_id     INTEGER PRIMARY KEY,
  resource_id TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO sites (site_id, resource_id) VALUES (0, '{DEFAULT_SITE}');
CREATE TABLE IF NOT EXISTS slot_property (
  period_end_epoch INTEGER PRIMARY KEY,
  {_col_sql(V2_PROPERTY_COLUMNS)}
);
CREATE TABLE IF NOT EXISTS slot_site (
  period_end_epoch INTEGER NOT NULL,
  site_id          INTEGER NOT NULL,
  {_col_sql(V2_SITE_COLUMNS)},
  PRIMARY KEY (period_end_epoch, site_id)
) WITHOUT ROWID;
"""

# The read view: legacy column names and units, so every query below works on either schema.
V2_VIEW_SQL = (
    "CREATE VIEW slot_rows AS SELECT ss.period_end_epoch AS period_end_epoch, st.resource_id AS site, "
    "strftime('%Y-%m-%dT%H:%M:%S+00:00', ss.period_end_epoch, 'unixepoch') AS period_end, "
    + ", ".join(_read_expr("ss", *c) for c in V2_SITE_COLUMNS)
    + ", "
    + ", ".join(_read_expr("sp", *c) for c in V2_PROPERTY_COLUMNS)
    + " FROM slot_site ss JOIN slot_property sp USING (period_end_epoch) JOIN sites st USING (site_id)"
)

_V2_PROPERTY_INSERT = (
    f"INSERT OR IGNORE INTO slot_property (period_end_epoch, {', '.join(V2_PROPERTY_NAMES)}) "
    f"VALUES ({', '.join('?' for _ in range(len(V2_PROPERTY_NAMES) + 1))})"
)
_V2_SITE_INSERT = (
    f"INSERT OR IGNORE INTO slot_site (period_end_epoch, site_id, {', '.join(V2_SITE_NAMES)}) "
    f"VALUES ({', '.join('?' for _ in range(len(V2_SITE_NAMES) + 2))})"
)

# A legacy slot with nothing in it, the same rule the coordinator applies before
# writing (``coordinator.is_empty_night_slot``). Empty slots are not carried over.
_LEGACY_EMPTY_ROW = (
    "zenith >= 90 AND pv_actual <= 0 AND pv_export <= 0 AND pv_export_max <= 0 "
    "AND pv_estimate <= 0 AND pv_estimate90 <= 0 AND battery_charge <= 0"
)
# Slots in which at least one row (total or any array) has something in it.
_LEGACY_KEPT_SLOTS = (
    f"SELECT period_end_epoch FROM solcast_data GROUP BY period_end_epoch HAVING SUM(NOT ({_LEGACY_EMPTY_ROW})) > 0"
)
# Days compared before the legacy table may be dropped.
COMPACT_VERIFY_DAYS = 7
# How long the pre-migration backup is kept once the store is compacted.
BACKUP_KEEP_DAYS = 30
BACKUP_SUFFIX = ".pre-v2.bak"


def read_source(conn: sqlite3.Connection) -> str:
    """The table or view a reader should query: the v2 view when present, else the legacy table.

    For the ``tools/`` CLIs, which open the file directly; after compaction only the view exists.
    """
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    return "slot_rows" if "slot_rows" in names else "solcast_data"


def copy_legacy_slots(conn: sqlite3.Connection, after_epoch: int | None = None) -> int:
    """Copy legacy rows of every non-empty slot into the v2 tables. Returns site rows written.

    ``INSERT OR IGNORE`` throughout, so a slot already in v2 is left as it is. The
    property row comes from the slot's '_total' row where there is one, and otherwise
    from any of its rows. Gate measures did not exist, so they keep their unknown
    default. Values are scaled exactly as the write path scales them.
    """
    newer = "" if after_epoch is None else " AND period_end_epoch > ?"
    params: tuple[Any, ...] = () if after_epoch is None else (after_epoch,)
    kept = f"period_end_epoch IN ({_LEGACY_KEPT_SLOTS}){newer}"
    conn.execute(
        f"INSERT OR IGNORE INTO sites (resource_id) SELECT DISTINCT site FROM solcast_data WHERE {kept} ORDER BY site",
        params,
    )
    present = {row[1] for row in conn.execute("PRAGMA table_info(solcast_data)")}
    prop = [c for c in V2_PROPERTY_COLUMNS if c[0] in present]
    cols = ", ".join(c[0] for c in prop)
    exprs = ", ".join(_legacy_expr(n, d, sc) for n, _, d, sc in prop)
    for site_clause, site_params in (("site = ?", (DEFAULT_SITE,)), ("1 = 1", ())):
        conn.execute(
            f"INSERT OR IGNORE INTO slot_property (period_end_epoch, {cols}) "
            f"SELECT period_end_epoch, {exprs} FROM solcast_data WHERE {site_clause} AND {kept}",
            (*site_params, *params),
        )
    site_cols = [c for c in V2_SITE_COLUMNS if c[0] in present]
    scols = ", ".join(c[0] for c in site_cols)
    sexprs = ", ".join(_legacy_expr(f"d.{n}", d, sc) for n, _, d, sc in site_cols)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO slot_site (period_end_epoch, site_id, {scols}) "
        f"SELECT d.period_end_epoch, s.site_id, {sexprs} "
        f"FROM solcast_data d JOIN sites s ON s.resource_id = d.site WHERE d.{kept}",
        params,
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def sync_v2_from_legacy(conn: sqlite3.Connection, refresh: tuple[str, ...] = ()) -> int:
    """Bring the v2 tables in line with legacy rows a ``tools/`` CLI just wrote. Returns rows added.

    During the soak a tool that writes the legacy table (an import, a backfill) must
    reach v2 too, or every reader, which goes through the view, would not see it.
    Missing slots are copied; ``refresh`` names property columns to overwrite on
    slots v2 already holds (a backfill rewrites ``ghi``/``dni``/``dhi`` in place).
    A no-op on a store without v2 tables. Commits.

    Raises:
        ValueError: If the store is compacted (no legacy table to sync from), or a
            ``refresh`` column is not a v2 property column.
    """
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    if not all(t in names for t in V2_TABLES):
        return 0
    if "solcast_data" not in names:
        raise ValueError("store is compacted; write through the integration or run tools/downgrade_v2.py first")
    added = copy_legacy_slots(conn)
    spec = {c[0]: c for c in V2_PROPERTY_COLUMNS}
    for name in refresh:
        if name not in spec:
            raise ValueError(f"{name} is not a v2 property column")
        _, _, default, scale = spec[name]
        conn.execute(
            f"UPDATE slot_property SET {name} = (SELECT {_legacy_expr('d.' + name, default, scale)} "
            "FROM solcast_data d WHERE d.period_end_epoch = slot_property.period_end_epoch AND d.site = ?) "
            "WHERE EXISTS (SELECT 1 FROM solcast_data d WHERE d.period_end_epoch = slot_property.period_end_epoch "
            "AND d.site = ?)",
            (DEFAULT_SITE, DEFAULT_SITE),
        )
    conn.commit()
    return added


class SqliteStore:
    """File-backed SQLite store (async API, all I/O via the executor)."""

    def __init__(self, hass: HomeAssistant, path: str, readonly: bool = False) -> None:
        """Initialise the store for ``path`` (no connection is opened yet)."""
        self._hass = hass
        self._path = path
        self._readonly = readonly
        # The columns actually present in the open file. A read-only open skips the
        # additive ALTER pass, so an older database keeps an older column set and a
        # query naming a newer column fails outright rather than returning zeros —
        # see ``_optional_column``.
        self._columns: frozenset[str] = frozenset()
        self._conn: sqlite3.Connection | None = None
        # sqlite3 connections aren't safe to share across threads without
        # serialisation; every executor call holds this lock.
        self._lock = threading.Lock()
        # Always present on a fresh schema (kept for query-builder symmetry).
        self.has_battery_col = True
        self.has_site_col = True
        # Which schemas the open file holds (see SCHEMA_V2). Reads go to the v2
        # ``slot_rows`` view whenever it exists; writes go to every schema present.
        self._version = 0
        self._has_legacy = False
        self._has_v2 = False
        self._site_ids: dict[str, int] = {}
        # Set when the file's schema is newer than SCHEMA_KNOWN (see _connect).
        self.newer_schema = False

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
            # Expose the pure-Python Haurwitz clear-sky GHI to SQL so the clearness-
            # index gate can filter in-query (SQLite has no exp()). Registered on
            # read-only opens too, since the gate runs against reference DBs.
            conn.create_function("clearsky_ghi", 1, clearsky_ghi)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if self._version > SCHEMA_KNOWN:
                # A newer release wrote this file. Every write path checks _readonly,
                # so nothing is created, altered, migrated, pruned or recorded.
                self.newer_schema = True
                self._readonly = True
                _LOGGER.warning(
                    "Database %s is schema v%d, newer than this version understands (v%d); "
                    "opened read-only and nothing will be recorded until a newer version is installed",
                    self._path,
                    self._version,
                    SCHEMA_KNOWN,
                )
            if not self._readonly:
                if self._version == 0 and not self._objects(conn):
                    self._create_compacted(conn)
                # A compacted store has dropped the legacy table on purpose; recreating
                # it would restart the dual-write the user switched off.
                elif self._version < SCHEMA_COMPACTED:
                    conn.executescript(CREATE_TABLE_SQL)
                    conn.commit()
                    self._ensure_columns(conn)
                # An older release put a legacy table back on a compacted store, so the
                # dual-write resumes. 1.14.0 and earlier create it without the columns
                # added since, and every insert (v2 half included) would roll back.
                elif "solcast_data" in self._objects(conn):
                    self._ensure_columns(conn)
                self._init_v2(conn)
            self._conn = conn
            self._detect_schema()
            # Surface the file path + current row count so users know where the
            # store lives (e.g. to point sqlite-web at it) and that it loaded.
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {self._source}").fetchone()
                count = int(row[0]) if row else 0
            except Exception:  # noqa: BLE001 — table may be absent on a read-only first open
                count = 0
            _LOGGER.info(
                "Built-in store ready at %s — %d row(s), schema v%d%s",
                self._path,
                count,
                self._version,
                " (legacy table still written)" if self._has_v2 and self._has_legacy else "",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SQLite open failed (%s): %s", self._path, exc)
            return False

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """Additively add any post-original columns missing on an existing DB.

        ``CREATE TABLE IF NOT EXISTS`` leaves an older table untouched, so columns
        introduced later (``_ADDED_COLUMNS``) must be ALTERed in. The ALTER is
        idempotent (guarded by the live column set) and cheap; ``DEFAULT 0``
        backfills existing rows so the NOT NULL constraint holds.
        """
        existing = {row[1] for row in conn.execute("PRAGMA table_info(solcast_data)")}
        added: list[str] = []
        for name, decl in _ADDED_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE solcast_data ADD COLUMN {name} {decl}")
                added.append(name)
        if added:
            conn.commit()
            _LOGGER.info("Added column(s) to store schema: %s", ", ".join(added))

    @staticmethod
    def _objects(conn: sqlite3.Connection) -> set[str]:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}

    def _create_compacted(self, conn: sqlite3.Connection) -> None:
        """Start a brand-new file directly on the compacted layout (schema v3).

        A new install has no history to protect and no earlier release to go back
        to, so dual-writing the legacy table would only multiply the file's size
        until someone ran ``compact_database``. Going back to 1.15.0b2 still works:
        it creates its own empty legacy table, and ``_resync_from_legacy`` carries
        what it wrote into the v2 tables on the next start. The tables, the view and
        the version are written in one transaction (``user_version`` is
        transactional), so a crash part-way leaves an empty file that is created
        again on the next start.
        """
        try:
            conn.executescript(
                f"BEGIN;\n{V2_CREATE_SQL}\n{V2_VIEW_SQL};\nPRAGMA user_version = {SCHEMA_COMPACTED};\nCOMMIT;"
            )
        except sqlite3.Error:
            # Release the write lock now rather than when the connection is collected.
            conn.rollback()
            raise
        self._version = SCHEMA_COMPACTED
        _LOGGER.info("Created a new store at %s on the compacted schema v%d", self._path, SCHEMA_COMPACTED)

    def _init_v2(self, conn: sqlite3.Connection) -> None:
        """Bring the v2 tables up to date, or create them beside an empty legacy table.

        A store that already has v2 tables gains any column added to the
        ``V2_*_COLUMNS`` lists since, and its view is rebuilt so it names them. A
        store whose legacy table exists but holds no rows (an earlier release
        created it) starts on v2 dual-writing: there is nothing to migrate, but that
        release may still be reinstalled. A brand-new file never gets here below v3
        (``_create_compacted``). A legacy store with data is left for
        ``async_migrate``, which takes a backup first.
        """
        objects = self._objects(conn)
        if all(t in objects for t in V2_TABLES):
            self._ensure_v2_columns(conn)
            self._rebuild_view(conn)
            return
        legacy_rows = 0
        if "solcast_data" in objects:
            legacy_rows = int(conn.execute("SELECT COUNT(*) FROM solcast_data").fetchone()[0])
        if legacy_rows == 0:
            conn.executescript(V2_CREATE_SQL)
            self._rebuild_view(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_V2}")
            conn.commit()
            self._version = SCHEMA_V2

    @staticmethod
    def _ensure_v2_columns(conn: sqlite3.Connection) -> None:
        added: list[str] = []
        for table, cols in (("slot_property", V2_PROPERTY_COLUMNS), ("slot_site", V2_SITE_COLUMNS)):
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, _kind, default, scale in cols:
                if name not in existing:
                    stored = _stored_default(default, scale)
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} INTEGER NOT NULL DEFAULT {stored}")
                    added.append(f"{table}.{name}")
        if added:
            conn.commit()
            _LOGGER.info("Added column(s) to store schema: %s", ", ".join(added))

    @staticmethod
    def _rebuild_view(conn: sqlite3.Connection) -> None:
        conn.execute("DROP VIEW IF EXISTS slot_rows")
        conn.execute(V2_VIEW_SQL)
        conn.commit()

    def _detect_schema(self) -> None:
        """Record which schemas the open file holds, and the columns reads can name."""
        assert self._conn is not None
        objects = self._objects(self._conn)
        self._has_legacy = "solcast_data" in objects
        self._has_v2 = "slot_rows" in objects and all(t in objects for t in V2_TABLES)
        try:
            self._columns = frozenset(row[1] for row in self._conn.execute(f"PRAGMA table_info({self._source})"))
        except Exception:  # noqa: BLE001 — nothing to read on a read-only first open
            self._columns = frozenset()
        self._site_ids = {}
        if self._has_v2:
            self._site_ids = {r[1]: int(r[0]) for r in self._conn.execute("SELECT site_id, resource_id FROM sites")}

    @property
    def _source(self) -> str:
        """The table or view every read goes to."""
        return "slot_rows" if self._has_v2 else "solcast_data"

    @property
    def schema_version(self) -> int:
        """``PRAGMA user_version`` of the open file (see ``SCHEMA_V2``)."""
        return self._version

    @property
    def dual_writing(self) -> bool:
        """Whether the legacy table is still written alongside the v2 tables."""
        return self._has_v2 and self._has_legacy

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
    def _row_values(record: StoreRecord) -> tuple[Any, ...]:
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
            record.get("dc_voltage1", 0.0) or 0.0,
            record.get("dc_current1", 0.0) or 0.0,
            record.get("dc_voltage2", 0.0) or 0.0,
            record.get("dc_current2", 0.0) or 0.0,
            record.get("ghi", 0.0) or 0.0,
            record.get("dni", 0.0) or 0.0,
            record.get("dhi", 0.0) or 0.0,
            record.get("dc_vmed1", 0.0) or 0.0,
            record.get("dc_vmed2", 0.0) or 0.0,
            record.get("dc_imed1", 0.0) or 0.0,
            record.get("dc_imed2", 0.0) or 0.0,
            record.get("pv_estimate_undampened", 0.0) or 0.0,
            record.get("pv_export_max", 0.0) or 0.0,
            record.get("grid_v_max", 0.0) or 0.0,
            record.get("grid_v_med", 0.0) or 0.0,
            record.get("inverter_temp_max", 0.0) or 0.0,
            # Not `or`: 0.0 is a real value here, so only a missing key falls back.
            record.get("inverter_limited", CURTAILMENT_UNKNOWN),
            record.get("export_limit", CURTAILMENT_UNKNOWN),
            record.get("dc_offmpp_min", CURTAILMENT_UNKNOWN),
        )

    async def async_insert_record(self, record: StoreRecord) -> bool:
        """Insert one record. Duplicate (epoch, site) is ignored. True on success."""
        if self._conn is None or self._readonly:
            return False
        return await self._hass.async_add_executor_job(self._insert_many, [record])

    def _insert_many(self, records: list[StoreRecord]) -> bool:
        assert self._conn is not None  # Narrowed by async_insert_record's guard.
        try:
            legacy_rows = [self._row_values(r) for r in records] if self._has_legacy else []
            v2_rows = [self._v2_values(r) for r in records] if self._has_v2 else []
        except KeyError as exc:
            _LOGGER.error("SQLite insert skipped — record missing field %s", exc)
            return False
        try:
            with self._lock:
                # One transaction for both schemas, so a slot is in both or neither.
                if legacy_rows:
                    self._conn.executemany(_INSERT_SQL, legacy_rows)
                for site, prop, site_vals in v2_rows:
                    sid = self._site_id(site)
                    self._conn.execute(_V2_PROPERTY_INSERT, prop)
                    self._conn.execute(_V2_SITE_INSERT, (site_vals[0], sid, *site_vals[1:]))
                self._conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            self._conn.rollback()
            self._site_ids = {}  # An id assigned inside the rolled-back transaction is gone.
            _LOGGER.error("SQLite insert failed: %s", exc)
            return False

    @staticmethod
    def _v2_values(record: StoreRecord) -> tuple[str, tuple[Any, ...], tuple[Any, ...]]:
        """Split one record into its site, property-row values and site-row values, scaled.

        The property row is written ``OR IGNORE``, so the first record of a slot
        sets it. The coordinator writes the '_total' record first, which is also
        the only one that carries the property-wide gate measures.
        """
        raw = cast("dict[str, Any]", record)
        epoch = int(raw["period_end_epoch"])
        # Required, as for a legacy row: a record without these is malformed.
        for required in ("pv_actual", "pv_estimate"):
            if required not in raw:
                raise KeyError(required)

        def val(name: str, default: float, scale: int) -> int:
            value = raw.get(name)
            return to_stored(default if value is None else value, default, scale)

        prop = (epoch, *(val(n, d, sc) for n, _, d, sc in V2_PROPERTY_COLUMNS))
        site_vals = (epoch, *(val(n, d, sc) for n, _, d, sc in V2_SITE_COLUMNS))
        return str(raw.get("site") or DEFAULT_SITE), prop, site_vals

    def _site_id(self, resource_id: str) -> int:
        """The integer id for a site, assigned on first sight. Caller holds the lock."""
        assert self._conn is not None
        sid = self._site_ids.get(resource_id)
        if sid is None:
            self._conn.execute("INSERT OR IGNORE INTO sites (resource_id) VALUES (?)", (resource_id,))
            sid = int(
                self._conn.execute("SELECT site_id FROM sites WHERE resource_id = ?", (resource_id,)).fetchone()[0]
            )
            self._site_ids[resource_id] = sid
        return sid

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
        assert self._conn is not None  # Narrowed by async_prune's guard.
        cutoff = int(time.time()) - retention_days * 86400
        try:
            with self._lock:
                removed = 0
                tables = (("solcast_data",) if self._has_legacy else ()) + (
                    ("slot_site", "slot_property") if self._has_v2 else ()
                )
                for table in tables:
                    cur = self._conn.execute(f"DELETE FROM {table} WHERE period_end_epoch < ?", (cutoff,))
                    # Report rows in the schema reads use, so the count means the same either way.
                    if table == ("slot_site" if self._has_v2 else "solcast_data"):
                        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                self._conn.commit()
                return removed
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
        changed = await self._hass.async_add_executor_job(self._migrate_azimuth, latitude, longitude)
        await self._hass.async_add_executor_job(self._migrate_v2)
        await self._hass.async_add_executor_job(self._resync_from_legacy)
        await self._hass.async_add_executor_job(self._expire_backup)
        return changed

    @property
    def backup_path(self) -> str:
        """Where the pre-migration backup is written."""
        return self._path + BACKUP_SUFFIX

    def _migrate_v2(self) -> bool:
        """Move a legacy store's data into the v2 tables. True when it ran.

        Runs once, when the file is at v1 or below and holds legacy rows (a fresh
        store starts on v2 in ``_init_v2``). The legacy table is left in place and
        keeps being written, so reinstalling 1.15.0b2 finds a complete table.

        1. Pre-flight: ``PRAGMA quick_check`` and free disk of at least twice the
           file. On failure the store stays on v1 and runs exactly as before.
        2. ``VACUUM INTO`` a backup: a consistent, compacted copy even under WAL.
        3. One transaction: create, copy every non-empty slot, check that each
           site's row count and output total match the legacy table, and only then
           set ``user_version``. Any error rolls the whole thing back.
        """
        assert self._conn is not None
        if self._version >= SCHEMA_V2 or self._has_v2 or not self._has_legacy:
            return False
        conn = self._conn
        try:
            with self._lock:
                if not self._integrity_ok(conn):
                    _LOGGER.error("Store integrity check failed; staying on the legacy schema")
                    return False
                size = os.path.getsize(self._path)
                free = shutil.disk_usage(os.path.dirname(os.path.abspath(self._path))).free
                if free < 2 * size:
                    _LOGGER.error(
                        "Not enough free disk to migrate the store (%d MB free, %d MB needed); "
                        "staying on the legacy schema",
                        free // 1_000_000,
                        2 * size // 1_000_000,
                    )
                    return False
                conn.commit()
                if os.path.exists(self.backup_path):
                    os.remove(self.backup_path)
                conn.execute("VACUUM INTO ?", (self.backup_path,))
                started = time.monotonic()
                conn.execute("BEGIN")
                try:
                    for statement in V2_CREATE_SQL.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    copied = self._copy_legacy_slots(conn, after_epoch=None)
                    conn.execute("DROP VIEW IF EXISTS slot_rows")
                    conn.execute(V2_VIEW_SQL)
                    self._verify_copy(conn, since_epoch=None)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_V2}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                self._version = SCHEMA_V2
                self._detect_schema()
            _LOGGER.info(
                "Store migrated to schema v%d: %d row(s) copied in %.1f s; backup at %s",
                SCHEMA_V2,
                copied,
                time.monotonic() - started,
                self.backup_path,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Store migration failed; staying on the legacy schema: %s", exc)
            return False

    def _copy_legacy_slots(self, conn: sqlite3.Connection, after_epoch: int | None) -> int:
        """Copy legacy slots into v2; see the module-level ``copy_legacy_slots``."""
        return copy_legacy_slots(conn, after_epoch)

    @staticmethod
    def _integrity_ok(conn: sqlite3.Connection) -> bool:
        return bool(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok")

    @staticmethod
    def _legacy_columns(conn: sqlite3.Connection) -> set[str]:
        return {row[1] for row in conn.execute("PRAGMA table_info(solcast_data)")}

    @staticmethod
    def _site_totals(
        conn: sqlite3.Connection, source: str, where: str, params: tuple[Any, ...]
    ) -> dict[str, tuple[int, float]]:
        rows = conn.execute(
            f"SELECT site, COUNT(*), SUM(pv_actual) FROM {source} WHERE {where} GROUP BY site", params
        ).fetchall()
        return {r[0]: (int(r[1]), float(r[2] or 0.0)) for r in rows}

    def _verify_copy(self, conn: sqlite3.Connection, since_epoch: int | None) -> None:
        """Raise unless both schemas hold the same rows and output, per site."""
        newer = "" if since_epoch is None else " AND period_end_epoch >= ?"
        params: tuple[Any, ...] = () if since_epoch is None else (since_epoch,)
        legacy = self._site_totals(conn, "solcast_data", f"period_end_epoch IN ({_LEGACY_KEPT_SLOTS}){newer}", params)
        v2 = self._site_totals(conn, "slot_rows", f"1 = 1{newer}", params)
        # Counts must match exactly; totals to within the scaled-integer rounding,
        # which is at most half a unit of the scale per row.
        half_unit = 0.5 / V2_SCALES["pv_actual"]
        agree = legacy.keys() == v2.keys() and all(
            legacy[k][0] == v2[k][0] and abs(legacy[k][1] - v2[k][1]) <= legacy[k][0] * half_unit + 1e-9 for k in legacy
        )
        if not agree:
            raise ValueError(f"row counts or output totals differ between schemas: legacy {legacy}, v2 {v2}")

    def _resync_from_legacy(self) -> int:
        """Copy every legacy slot the v2 tables lack. Returns site rows copied.

        Covers anything that wrote only the legacy table: a spell on 1.15.0b2, or a
        ``tools/import_history.py`` run (whose history is *older* than the newest v2
        row, so "copy what is newer" would miss it). ``INSERT OR IGNORE`` makes it
        idempotent; the cost is one scan of the legacy table per start.
        """
        assert self._conn is not None
        if not self.dual_writing:
            return 0
        try:
            with self._lock:
                copied = copy_legacy_slots(self._conn)
                self._conn.commit()
                self._site_ids = {}
            if copied:
                _LOGGER.info("Carried %d row(s) written by an older version or a tool into the v2 tables", copied)
            return copied
        except Exception as exc:  # noqa: BLE001
            self._conn.rollback()
            _LOGGER.error("Store resync failed: %s", exc)
            return 0

    def _expire_backup(self) -> bool:
        """Delete the pre-migration backup once compacted and older than ``BACKUP_KEEP_DAYS``."""
        if self._version < SCHEMA_COMPACTED or not os.path.exists(self.backup_path):
            return False
        if time.time() - os.path.getmtime(self.backup_path) < BACKUP_KEEP_DAYS * 86400:
            return False
        try:
            os.remove(self.backup_path)
        except OSError as exc:
            _LOGGER.warning("Could not delete the old store backup %s: %s", self.backup_path, exc)
            return False
        _LOGGER.info("Deleted the pre-migration store backup %s", self.backup_path)
        return True

    async def async_compact(self, now_epoch: int | None = None) -> dict[str, Any]:
        """Drop the legacy table and reclaim its space, once both schemas agree.

        User-triggered (the ``compact_database`` action), never automatic. Refuses
        unless the last ``COMPACT_VERIFY_DAYS`` days match per site in both schemas.
        After this, going back to 1.15.0b2 needs ``tools/downgrade_v2.py`` or the
        backup.
        """
        if self._conn is None or self._readonly:
            return {"compacted": False, "reason": "store not open for writing"}
        return await self._hass.async_add_executor_job(self._compact, now_epoch)

    def _compact(self, now_epoch: int | None) -> dict[str, Any]:
        assert self._conn is not None
        if not self.dual_writing:
            return {"compacted": False, "reason": "already compacted" if self._has_v2 else "not migrated yet"}
        since = int(now_epoch if now_epoch is not None else time.time()) - COMPACT_VERIFY_DAYS * 86400
        conn = self._conn
        with self._lock:
            try:
                self._verify_copy(conn, since_epoch=since)
            except ValueError as exc:
                return {"compacted": False, "reason": str(exc)}
            before = os.path.getsize(self._path)
            conn.execute("DROP TABLE solcast_data")
            conn.execute(f"PRAGMA user_version = {SCHEMA_COMPACTED}")
            conn.commit()
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._version = SCHEMA_COMPACTED
            self._detect_schema()
            after = os.path.getsize(self._path)
        _LOGGER.info("Store compacted: %.2f MB -> %.2f MB", before / 1e6, after / 1e6)
        return {"compacted": True, "size_before_mb": round(before / 1e6, 2), "size_after_mb": round(after / 1e6, 2)}

    def _migrate_azimuth(self, latitude: float, longitude: float) -> int:
        assert self._conn is not None  # Narrowed by async_migrate's guard.
        try:
            with self._lock:
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
                if version >= SCHEMA_VERSION:
                    return 0
                rows = self._conn.execute('SELECT "index", period_end_epoch, azimuth FROM solcast_data').fetchall()
                # Solar azimuth depends only on epoch + site lat/lon (it is the sun
                # position, shared by every site on the property), so recompute it
                # from each row's stored epoch at the interval midpoint (epoch-900),
                # matching how it was originally written. Only rewrite rows whose
                # value actually moved, to avoid needless SD-card writes.
                updates = [
                    (round(new_az, 5), idx)
                    for idx, epoch, old_az in rows
                    if abs((new_az := solar_position(int(epoch) - 900, latitude, longitude)[0]) - (old_az or 0.0))
                    > 0.01
                ]
                if updates:
                    self._conn.executemany('UPDATE solcast_data SET azimuth = ? WHERE "index" = ?', updates)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()
            if updates:
                _LOGGER.info(
                    "Repaired solar azimuth on %d of %d stored row(s)",
                    len(updates),
                    len(rows),
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
        assert self._conn is not None  # Narrowed by async_get_record_count's guard.
        try:
            with self._lock:
                cur = self._conn.execute(f"SELECT COUNT(*) FROM {self._source}")
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
        assert self._conn is not None  # Narrowed by async_get_sites' guard.
        try:
            with self._lock:
                cur = self._conn.execute(f"SELECT DISTINCT site FROM {self._source}")
                rows = cur.fetchall()
            return [r[0] for r in rows if r and r[0] is not None]
        except Exception:  # noqa: BLE001
            return []

    async def async_get_records_for_dampening(
        self,
        slot_doy: int,
        back_days: int = DAMPENING_WINDOW_BACK_DAYS,
        forward_days: int = DAMPENING_WINDOW_FORWARD_DAYS,
        site: str | None = None,
    ) -> list[StoreRecord]:
        """Fetch records in a seasonal day-of-year window around ``slot_doy``, across all years.

        The window spans ``back_days`` before the target day to ``forward_days`` after
        it. It is asymmetric by default because the live target is always *today*: the
        forward half can only be filled from a previous year, so a centred window is
        permanently half-empty on a first-year install and truncates alpha. See
        ``DAMPENING_WINDOW_BACK_DAYS`` in const.py.

        The day difference is computed **signed and wrapped** onto the year, so a
        window near New Year still reaches across the boundary. The former
        ``ABS(doy - slot_doy)`` form could not: for a 5 January target it scored
        29 December as 355 days away rather than 7, silently excluding the immediately
        preceding week — the most seasonally relevant data there is.
        """
        if self._conn is None:
            return []
        site_clause, site_params = self._site_filter(site)
        # Signed day-of-year difference wrapped to [-182, 183]. The +548 bias keeps the
        # dividend positive (doy is 1..366, so the raw difference is >= -365) because
        # SQLite's % truncates toward zero and would otherwise return a negative
        # remainder for dates before the target.
        doy_delta = "((((CAST(strftime('%j', period_end_epoch, 'unixepoch') AS INTEGER) - ?) + 548) % 366) - 182)"
        optional = ", ".join(self._optional_column(c) for c in ("ghi", "pv_export_max", "pv_estimate_undampened"))
        sql = (
            "SELECT pv_actual, pv_export, pv_estimate, pv_estimate10, "
            "pv_estimate90, azimuth, zenith, clouds, "
            f"{optional}, "
            "COALESCE(battery_charge, 0.0) AS battery_charge "
            f"FROM {self._source} "
            "WHERE pv_actual > 0 AND pv_estimate > 0 "
            # strftime('%j', epoch, 'unixepoch') renders day-of-year in UTC,
            # matching the MySQL backend's UTC-pinned FROM_UNIXTIME/DAYOFYEAR.
            f"AND {doy_delta} >= ? AND {doy_delta} <= ?"
            f"{site_clause}"
        )
        params = (slot_doy, -abs(back_days), slot_doy, abs(forward_days), *site_params)
        return await self._hass.async_add_executor_job(self._query, sql, params)

    async def async_get_records_for_tuning(
        self,
        limit: int = 2000,
        site: str | None = None,
        cloud_max: int | None = None,
        kt_threshold: float | None = None,
        kt_zenith_max: float = 85.0,
        kt_ghi_cs_floor: float = 40.0,
    ) -> list[StoreRecord]:
        """Fetch recent clear-sky records for PV tuning.

        Two mutually exclusive clear-sky gates, both applied **in SQL before the
        LIMIT** so the result is the most recent ``limit`` *clear-sky* rows (not the
        most recent rows of any weather, of which only a few may be clear in a
        cloudy season). This gives tuning the sun-angle diversity across seasons
        that actually constrains tilt.

        - ``kt_threshold`` (preferred): measured-irradiance clearness index
          ``Kt = ghi / clearsky_ghi(zenith) >= kt_threshold``, judged only where the
          sun is up (``zenith < kt_zenith_max``) and the clear-sky reference is
          meaningful (``clearsky_ghi(zenith) >= kt_ghi_cs_floor``). Independent of
          any model cloud %.
        - ``cloud_max`` (fallback): OWM total-cloud gate ``clouds < cloud_max``.

        If both are given, ``kt_threshold`` wins.
        """
        if self._conn is None:
            return []
        site_clause, site_params = self._site_filter(site)
        gate_clause = ""
        gate_params: tuple[Any, ...] = ()
        if kt_threshold is not None:
            # Multiply form avoids dividing by a near-zero clear-sky reference.
            gate_clause = (
                " AND ghi > 0 AND zenith < ? AND clearsky_ghi(zenith) >= ? AND ghi >= ? * clearsky_ghi(zenith)"
            )
            gate_params = (
                float(kt_zenith_max),
                float(kt_ghi_cs_floor),
                float(kt_threshold),
            )
        elif cloud_max is not None:
            gate_clause = " AND clouds < ?"
            gate_params = (int(cloud_max),)
        optional = ", ".join(self._optional_column(c) for c in ("pv_export_max", "ghi", "dni", "dhi"))
        sql = (
            "SELECT period_end_epoch, pv_actual, pv_export, pv_estimate, "
            "azimuth, zenith, clouds, "
            f"{optional}, "
            "COALESCE(battery_charge, 0.0) AS battery_charge "
            f"FROM {self._source} "
            f"WHERE pv_actual > 0{site_clause}{gate_clause} "
            "ORDER BY period_end_epoch DESC LIMIT ?"
        )
        params = (*site_params, *gate_params, limit)
        return await self._hass.async_add_executor_job(self._query, sql, params)

    async def async_get_records_for_shading(
        self,
        site: str | None = None,
        limit: int = 6000,
        zenith_max: float = SHADING_ZENITH_MAX,
    ) -> list[StoreRecord]:
        """Fetch daylight records for the geometric shading advisory.

        This is the **only** query that returns the per-tracker median DC telemetry
        (``dc_vmed*`` / ``dc_imed*``). Those columns exist to answer one question the
        AC-side ratio cannot: whether a low-sun loss is a *uniform* shadow line across
        every module (current falls, the tracker stays at Vmp, the loss is linear in
        unshaded area) or a *bypass-diode* event (voltage collapses, and no
        multiplicative transmission factor can represent it). See
        ``shading_geometry.classify_mechanism``.

        Deliberately **not** clear-sky gated. The advisory needs the overcast records
        too: they carry no beam to block, so they establish the unshaded baseline that
        the beam-shaded records are measured against. Gating them out is precisely the
        mistake that hides shading from the dampening path.

        ``pv_estimate_undampened`` is returned alongside ``pv_estimate`` because the
        advisory's expected output must be the forecast *before* our own pushed
        factors; using the dampened figure would measure our own correction back.
        """
        if self._conn is None:
            return []
        site_clause, site_params = self._site_filter(site)
        optional = ", ".join(
            self._optional_column(c)
            for c in ("pv_estimate_undampened", "ghi", "dni", "dhi", "dc_vmed1", "dc_vmed2", "dc_imed1", "dc_imed2")
        )
        sql = (
            "SELECT period_end_epoch, pv_actual, pv_export, pv_estimate, "
            "azimuth, zenith, clouds, temp, "
            f"{optional} "
            f"FROM {self._source} "
            f"WHERE pv_actual > 0 AND zenith < ?{site_clause} "
            "ORDER BY period_end_epoch DESC LIMIT ?"
        )
        params = (float(zenith_max), *site_params, limit)
        return await self._hass.async_add_executor_job(self._query, sql, params)

    def _optional_column(self, name: str) -> str:
        """SELECT fragment for a post-original column that may be absent.

        A writable open ALTERs every ``_ADDED_COLUMNS`` entry in, so the column is
        always there and this is a plain ``COALESCE``. A **read-only** open cannot
        ALTER, so an older database — a reference copy handed to one of the
        ``tools/`` CLIs, or a checked-in fixture — genuinely lacks the column, and
        naming it would fail the whole query instead of yielding the 0.0 "unknown"
        sentinel every consumer already handles. Substituting the literal keeps the
        result shape identical either way.

        ``name`` is asserted to be a known column so the f-string interpolation
        below can never carry caller-supplied text into SQL.
        """
        assert name in _ADDED_COLUMN_NAMES, name
        if self._columns and name not in self._columns:
            return f"0.0 AS {name}"
        return f"COALESCE({name}, 0.0) AS {name}"

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[StoreRecord]:
        assert self._conn is not None  # Narrowed by each async query wrapper's guard.
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall()
            # Each query aliases its columns to StoreRecord's keys, so the row dicts match it.
            return [cast("StoreRecord", dict(r)) for r in rows]
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SQLite query failed: %s", exc)
            return []

    @property
    def available(self) -> bool:
        """Whether the store has an open connection."""
        return self._conn is not None
