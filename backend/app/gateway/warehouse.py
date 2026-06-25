"""Azure SQL analytics warehouse writer (Phase 5).

Power BI reads spend / on-time-delivery / stock-turn from an Azure SQL warehouse.
This module is the guarded writer that pushes the computed figures there. It
mirrors mailer.notify's discipline: a guarded entry point (`push`) that NEVER
raises and returns a status string the caller records, so an analytics push can
never break the surrounding request.

Config-gated like every other integration (INTEGRATIONS.md): with no AZURE_SQL_DSN
set, `warehouse_enabled` is false and `push` logs + no-ops, returning
'skipped:not-configured'. Set the DSN to flip it live (pyodbc skeleton below).

The warehouse is a read-only analytics SINK; it is NOT canonical state. The gateway
remains the only writer of canonical tables — this just exports figures derived
from them.
"""
import logging

from ..config import settings

log = logging.getLogger("golden.procurement.warehouse")


def push(table: str, rows: list[dict]) -> str:
    """Push analytics rows to one warehouse table. Guarded; never raises.

    Returns a status string the caller records:
      - 'skipped:not-configured'   AZURE_SQL_DSN unset (demo mode)
      - 'skipped:no-rows'          nothing to write
      - 'written:<n>'              n rows written (live)
      - 'error:<msg>'              the live write failed (logged; flow continues)
    """
    if not settings.warehouse_enabled:
        log.info("warehouse push skipped: AZURE_SQL_DSN not configured (table=%r)", table)
        return "skipped:not-configured"
    if not rows:
        log.info("warehouse push skipped: no rows (table=%r)", table)
        return "skipped:no-rows"
    try:
        return _write(table, rows)
    except Exception as exc:  # never break the analytics flow on a warehouse error
        log.exception("warehouse push failed (table=%r)", table)
        return f"error:{exc}"


def _write(table: str, rows: list[dict]) -> str:
    """Live pyodbc write (skeleton). Imported lazily so the demo path needs no
    ODBC driver. Same lazy-import discipline as the BC/Kiwiplan/Accura adapters."""
    import pyodbc  # noqa: F401  (TODO: confirm the Azure SQL ODBC driver name)

    # TODO: confirm the warehouse table schema (one table per metric, or a single
    # tall fact table keyed by metric/as_of). The parameterized INSERT below is a
    # placeholder; columns derive from each row's keys. Wire the real DDL/MERGE
    # (upsert by as_of) once the Power BI model is fixed.
    conn = pyodbc.connect(settings.warehouse_dsn, timeout=30)
    try:
        cur = conn.cursor()
        for row in rows:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            cur.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
        conn.commit()
    finally:
        conn.close()
    return f"written:{len(rows)}"
