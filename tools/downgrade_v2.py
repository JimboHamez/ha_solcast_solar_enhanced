#!/usr/bin/env python3
"""Rebuild the legacy ``solcast_data`` table from a compacted (schema v3) store.

After ``compact_database`` the legacy table is gone, so 1.15.0b2 would start with an
empty history. This puts it back from the v2 tables, in real units (the view undoes
the scaled-integer storage), and returns the file to the soak state (schema v2,
both formats present). From there 1.15.0b2 sees its full history again, and the
newer version resumes writing both.

What cannot come back: ``description`` (the weather text, never read by any
calculation) is stored as ``restored``; ``period_start`` is rebuilt as the slot
start, not the measured interval start; and values carry the v2 rounding (1 W,
0.1 V, 0.01 A, 0.01°, 0.1 W/m²). Empty night slots the newer version never wrote
stay absent; 1.15.0b2 does not need them.

Stop the integration (or Home Assistant) first, and keep a copy of the file.

    python tools/downgrade_v2.py --sqlite /config/solcast_solar_enhanced.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
# The store module and what it imports are stdlib-only (numpy is optional), but the
# package __init__ imports Home Assistant. Register the package without running it, so
# this tool works on plain Python too. A package already imported is used as it is.
_PKG = "custom_components.solcast_solar_enhanced"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_ROOT / "custom_components" / "solcast_solar_enhanced")]
    sys.modules[_PKG] = _pkg
from custom_components.solcast_solar_enhanced.sqlite_store import (  # noqa: E402
    _INSERT_COLUMNS,
    CREATE_TABLE_SQL,
    SCHEMA_COMPACTED,
    SCHEMA_V2,
)

# Legacy columns the view does not carry, and how they are rebuilt.
REBUILT = {
    "period_start": "strftime('%Y-%m-%dT%H:%M:%S+00:00', period_end_epoch - 1800, 'unixepoch')",
    "description": "'restored'",
}


def downgrade(path: str) -> int:
    """Rebuild the legacy table in ``path``. Returns rows written."""
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        if "slot_rows" not in names:
            raise SystemExit("No v2 tables in this file: nothing to downgrade.")
        if version < SCHEMA_COMPACTED and "solcast_data" in names:
            raise SystemExit(f"Schema v{version} still has its legacy table: 1.15.0b2 can already use it.")
        conn.executescript(CREATE_TABLE_SQL)
        view_cols = {r[1] for r in conn.execute("PRAGMA table_info(slot_rows)")}
        select = ", ".join(REBUILT.get(c, c) if c not in view_cols else c for c in _INSERT_COLUMNS)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO solcast_data ({', '.join(_INSERT_COLUMNS)}) "
            f"SELECT {select} FROM slot_rows ORDER BY period_end_epoch, site"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_V2}")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", required=True, help="path to solcast_solar_enhanced.db (integration stopped)")
    args = ap.parse_args()
    n = downgrade(args.sqlite)
    print(f"Rebuilt solcast_data with {n} row(s); the store is back at schema v{SCHEMA_V2} (both formats).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
