#!/usr/bin/env python3
"""Import legacy history into the built-in SQLite store.

Migrates rows from the old MySQL backend (removed in v1.5.0) into the current
zero-config SQLite store (`config/solcast_solar_enhanced.db`), mapping the
identical `solcast_data` schema 1:1.

Runs standalone — no Home Assistant required. Reads from a CSV export (no extra
dependency) or directly from MySQL (needs `PyMySQL`). Safe to re-run: rows are
inserted with INSERT OR IGNORE on the (period_end_epoch, site) unique key, so
duplicates are skipped and existing data is never overwritten.

By default it **recomputes `azimuth` and `zenith` from each row's epoch** (+ your
site lat/lon), at the interval midpoint (period_end_epoch - 900) using the same
solar_position() the integration uses live. This both corrects legacy rows
written before the hour-angle fix (east<->west mirrored azimuth) and normalises
rows whose sun position came from a *different* library or sampling time (e.g.
node-red-contrib-sun-position sampled at the boundary, which is ~15 min and a few
degrees off the midpoint the tuner expects). Pass --no-recompute-azimuth to keep
the source azimuth/zenith verbatim.

Examples
--------
    # CSV export, recompute azimuth with your coordinates
    python tools/import_history.py --sqlite config/solcast_solar_enhanced.db \\
        --csv solcast_data.csv --lat -37.9 --lon 145.0

    # Directly from MySQL (password from the MYSQL_PWD env var or a prompt)
    python tools/import_history.py --sqlite config/solcast_solar_enhanced.db \\
        --mysql-host 192.168.1.10 --mysql-db solcast --mysql-user ha

    # See what would happen without writing
    python tools/import_history.py --sqlite out.db --csv solcast_data.csv --dry-run
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable


# --- Self-contained solar position (copied verbatim from the integration's
# pv_tuning.py, including the hour-angle wrap fix) so this file is fully
# standalone and needs neither Home Assistant nor the repo on sys.path. ---

def normalize_epoch(epoch: float) -> int:
    """Coerce a Unix epoch to seconds (accepts s, ms or µs)."""
    value = float(epoch)
    while value >= 1e11:
        value /= 1000.0
    return int(value)


def solar_position(epoch: int, latitude: float, longitude: float) -> tuple[float, float]:
    """Return (azimuth_deg, zenith_deg) for a Unix epoch. Accurate to ±1°."""
    dt = datetime.fromtimestamp(normalize_epoch(epoch), tz=timezone.utc)
    doy = dt.timetuple().tm_yday
    hour_utc = dt.hour + dt.minute / 60.0 + dt.second / 3600.0

    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (doy - 81))))
    B = math.radians(360 / 365 * (doy - 81))
    eot = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    solar_noon = 12 - longitude / 15 - eot / 60
    # Normalise the hour angle to [-180, 180] so a UTC timestamp on a different
    # calendar day from solar noon yields the correct morning/afternoon sign.
    hour_angle_deg = ((15 * (hour_utc - solar_noon)) + 180) % 360 - 180
    hour_angle = math.radians(hour_angle_deg)

    lat_r = math.radians(latitude)
    cos_zenith = (
        math.sin(lat_r) * math.sin(decl)
        + math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.degrees(math.acos(cos_zenith))

    sin_zenith = math.sin(math.acos(cos_zenith))
    if sin_zenith < 1e-6:
        azimuth = 0.0
    else:
        cos_az = (math.sin(decl) - math.sin(lat_r) * cos_zenith) / (
            math.cos(lat_r) * sin_zenith
        )
        cos_az = max(-1.0, min(1.0, cos_az))
        azimuth = math.degrees(math.acos(cos_az))
        if hour_angle > 0:
            azimuth = 360 - azimuth
    return azimuth, zenith

# Data columns in schema order, excluding the autoincrement "index" PK (the
# destination assigns its own). Identical between the old MySQL table and the
# current SQLite store.
COLUMNS = [
    "period_end", "period_end_epoch", "period_start", "site",
    "pv_actual", "pv_export", "pv_estimate", "pv_estimate10", "pv_estimate90",
    "azimuth", "zenith", "temp", "clouds", "description", "battery_charge",
]
DEFAULT_SITE = "_total"

# Mirrors sqlite_store.CREATE_TABLE_SQL so a fresh destination is schema-complete.
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

_INSERT_SQL = (
    "INSERT OR IGNORE INTO solcast_data ("
    + ", ".join(COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in COLUMNS)
    + ")"
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value if value not in (None, "") else default))
    except (TypeError, ValueError):
        return default


def _to_iso_utc(value: Any, fallback_epoch: int) -> str:
    """Return an ISO-8601 UTC string for a timestamp in any of the legacy forms.

    Handles ISO-8601 with offset (e.g. ``2025-09-21T23:30:00+10:00``) and the
    RFC-2822 form the old store used (e.g. ``Sun, 21 Sep 2025 14:00:00 GMT``),
    normalising both to UTC so imported rows match the current store's format.
    Falls back to the (already seconds-normalised) epoch when unparseable.
    """
    from email.utils import parsedate_to_datetime  # noqa: PLC0415

    s = str(value or "").strip()
    dt = None
    if s:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = parsedate_to_datetime(s)
            except (TypeError, ValueError):
                dt = None
    if dt is None:
        dt = datetime.fromtimestamp(fallback_epoch, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce one source row (CSV strings or MySQL Decimals) to the schema types.

    Also repairs the two legacy timestamp quirks: ``period_end_epoch`` stored in
    milliseconds (normalised to seconds), and ``period_end`` in RFC-2822 / GMT
    form (regenerated as ISO-8601 UTC). Missing ``site`` / ``battery_charge``
    columns (older exports) default to ``_total`` / ``0.0``.
    """
    # Milliseconds -> seconds (normalize_epoch divides any value >= 1e11 down).
    epoch_s = normalize_epoch(_to_float(raw.get("period_end_epoch")))
    return {
        "period_end": _to_iso_utc(raw.get("period_end"), epoch_s),
        "period_end_epoch": epoch_s,
        "period_start": _to_iso_utc(raw.get("period_start"), epoch_s - 1800),
        "site": str(raw.get("site") or DEFAULT_SITE),
        "pv_actual": _to_float(raw.get("pv_actual")),
        "pv_export": _to_float(raw.get("pv_export")),
        "pv_estimate": _to_float(raw.get("pv_estimate")),
        "pv_estimate10": _to_float(raw.get("pv_estimate10")),
        "pv_estimate90": _to_float(raw.get("pv_estimate90")),
        "azimuth": _to_float(raw.get("azimuth")),
        "zenith": _to_float(raw.get("zenith")),
        "temp": _to_float(raw.get("temp")),
        "clouds": _to_int(raw.get("clouds"), 100),
        "description": str(raw.get("description", "")),
        "battery_charge": _to_float(raw.get("battery_charge")),
    }


def _read_csv(path: str) -> list[dict[str, Any]]:
    with open(path, newline="") as f:
        return [_normalise(row) for row in csv.DictReader(f)]


def _read_mysql(args: argparse.Namespace) -> list[dict[str, Any]]:
    try:
        import pymysql  # noqa: PLC0415
    except ImportError:
        raise SystemExit(
            "Direct MySQL import needs PyMySQL — `pip install PyMySQL`, or export "
            "to CSV and use --csv instead."
        )
    password = os.environ.get("MYSQL_PWD")
    if password is None:
        import getpass  # noqa: PLC0415
        password = getpass.getpass(f"MySQL password for {args.mysql_user}: ")
    conn = pymysql.connect(
        host=args.mysql_host, port=args.mysql_port, user=args.mysql_user,
        password=password, database=args.mysql_db, cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(COLUMNS)} FROM {args.mysql_table}")
            return [_normalise(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _sql_literal(value: Any) -> str:
    """Render a Python value as a SQLite literal for an emitted .sql script."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _insert_statement(row: dict[str, Any]) -> str:
    values = ", ".join(_sql_literal(row[c]) for c in COLUMNS)
    return (
        f"INSERT OR IGNORE INTO solcast_data ({', '.join(COLUMNS)}) "
        f"VALUES ({values});"
    )


def _emit_sql(rows: list[dict[str, Any]], replace: bool, path: str) -> None:
    """Write a runnable .sql script (DELETE + INSERTs) instead of touching a DB."""
    with open(path, "w") as f:
        f.write("BEGIN TRANSACTION;\n")
        if replace:
            f.write("DELETE FROM solcast_data;\n")
        for r in rows:
            f.write(_insert_statement(r) + "\n")
        f.write("COMMIT;\n")


def _import(
    rows: Iterable[dict[str, Any]], sqlite_path: str, lat: float, lon: float,
    recompute_azimuth: bool, dry_run: bool, replace: bool, sql_out: str | None,
) -> tuple[int, int, int, int]:
    """Load rows into the store. Returns (read, inserted, fixed_azimuth, deleted).

    ``replace`` clears existing rows (DELETE FROM solcast_data) before inserting,
    atomically in one transaction. ``sql_out`` writes the DELETE/INSERT script to
    that path instead of executing anything.
    """
    rows = list(rows)
    fixed = 0
    if recompute_azimuth:
        for r in rows:
            # Both azimuth and zenith are stored at the interval *midpoint*
            # (period_end_epoch - 900) by the live integration; recompute them the
            # same way and from the same solar_position() so imported rows are
            # byte-for-byte consistent with rows the running integration writes.
            # (Source columns produced by a different sun-position library — e.g.
            # node-red-contrib-sun-position sampled at the boundary — would
            # otherwise be ~15 min / a few degrees off the tuner's own geometry.)
            new_az, new_zen = solar_position(r["period_end_epoch"] - 900, lat, lon)
            new_az = round(new_az, 5)
            new_zen = round(new_zen, 5)
            if abs(new_az - r["azimuth"]) > 0.01 or abs(new_zen - r["zenith"]) > 0.01:
                fixed += 1
            r["azimuth"] = new_az
            r["zenith"] = new_zen

    if sql_out:
        _emit_sql(rows, replace, sql_out)
        return len(rows), 0, fixed, 0

    if dry_run:
        return len(rows), 0, fixed, 0

    # Create the destination directory if needed — sqlite3.connect raises a
    # cryptic "unable to open database file" when the parent dir is missing.
    parent = os.path.dirname(os.path.abspath(sqlite_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(CREATE_TABLE_SQL)  # ensure table; commits any pending tx
        before = conn.execute("SELECT COUNT(*) FROM solcast_data").fetchone()[0]
        deleted = 0
        if replace:
            # DELETE + INSERT share one implicit transaction, committed together,
            # so the table is never left empty if the insert fails.
            deleted = conn.execute("DELETE FROM solcast_data").rowcount
        conn.executemany(_INSERT_SQL, [tuple(r[c] for c in COLUMNS) for r in rows])
        after = conn.execute("SELECT COUNT(*) FROM solcast_data").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    inserted = after - (before - deleted)
    return len(rows), inserted, fixed, deleted


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--sqlite", required=True,
        help="Destination SQLite store (created if absent), e.g. config/solcast_solar_enhanced.db",
    )
    src = p.add_argument_group("source (choose one)")
    src.add_argument("--csv", help="CSV export of the old solcast_data table")
    src.add_argument("--mysql-host", help="MySQL host (enables direct import)")
    src.add_argument("--mysql-port", type=int, default=3306)
    src.add_argument("--mysql-db", help="MySQL database name")
    src.add_argument("--mysql-user", help="MySQL user")
    src.add_argument("--mysql-table", default="solcast_data", help="Source table (default solcast_data)")
    az = p.add_argument_group("azimuth repair")
    az.add_argument("--lat", type=float, default=-37.9, help="Site latitude for azimuth recompute")
    az.add_argument("--lon", type=float, default=145.0, help="Site longitude for azimuth recompute")
    az.add_argument(
        "--no-recompute-azimuth", action="store_true",
        help="Keep source azimuth/zenith verbatim (default: recompute both from the "
        "epoch midpoint so they match the integration's own solar_position)",
    )
    mode = p.add_argument_group("write mode")
    mode.add_argument(
        "--replace", action="store_true",
        help="DROP all existing rows (DELETE FROM solcast_data) then insert the CSV — "
        "a full replace. Without this, rows are added with INSERT OR IGNORE.",
    )
    mode.add_argument(
        "--sql-out", metavar="FILE",
        help="Write the DELETE/INSERT statements to FILE instead of touching the DB.",
    )
    mode.add_argument("--dry-run", action="store_true", help="Read and transform, but do not write")
    mode.add_argument("--yes", action="store_true", help="Skip the --replace confirmation prompt")
    args = p.parse_args()

    if args.csv:
        rows = _read_csv(args.csv)
        source = f"CSV {args.csv}"
    elif args.mysql_host:
        if not (args.mysql_db and args.mysql_user):
            raise SystemExit("--mysql-db and --mysql-user are required for direct MySQL import")
        rows = _read_mysql(args)
        source = f"MySQL {args.mysql_user}@{args.mysql_host}:{args.mysql_port}/{args.mysql_db}"
    else:
        raise SystemExit("Provide a source: --csv <file> or --mysql-host <host> ...")

    # Confirm the destructive replace when actually writing to the DB.
    if args.replace and not args.dry_run and not args.sql_out and not args.yes:
        reply = input(
            f"--replace will DELETE every existing row in {args.sqlite} and load "
            f"{len(list(rows))} row(s) from the source. Continue? [y/N] "
        )
        if reply.strip().lower() not in ("y", "yes"):
            raise SystemExit("Aborted.")

    read, inserted, fixed, deleted = _import(
        rows, args.sqlite, args.lat, args.lon,
        recompute_azimuth=not args.no_recompute_azimuth,
        dry_run=args.dry_run, replace=args.replace, sql_out=args.sql_out,
    )

    print(f"Source            : {source}")
    print(f"Rows read         : {read}")
    if not args.no_recompute_azimuth:
        print(f"Sun pos corrected : {fixed} (azimuth+zenith recomputed at lat={args.lat}, lon={args.lon})")
    if args.sql_out:
        print(f"SQL written       : {args.sql_out}")
        print(f"  statements      : {'DELETE FROM solcast_data; + ' if args.replace else ''}"
              f"{read} × INSERT OR IGNORE")
    elif args.dry_run:
        print(f"Dry run           : no rows written ({'replace' if args.replace else 'append'} mode)")
    elif args.replace:
        print(f"Deleted (DROP)    : {deleted}  (ran: DELETE FROM solcast_data;)")
        print(f"Inserted          : {inserted}")
        print(f"Destination       : {args.sqlite}")
    else:
        print(f"Inserted          : {inserted}  (skipped {read - inserted} duplicate/existing)")
        print(f"Destination       : {args.sqlite}")


if __name__ == "__main__":
    main()
