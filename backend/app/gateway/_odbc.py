"""Shared ODBC read helper for the Kiwiplan/Accura adapters.

The actual SQL is supplied per-site via config (INTEGRATIONS.md) so we never
hardcode a vendor schema. The query must return columns named
location / on_hand / allocated / on_order and use one `:item_ref` placeholder.
`map_rows` is pure and unit-tested; `read_stock` does the (untestable here) connect.
"""
from typing import Optional


def map_rows(columns: list[str], rows: list) -> list[dict]:
    """Map driver rows -> canonical stock rows by column name (case-insensitive)."""
    idx = {c.lower(): i for i, c in enumerate(columns)}

    def val(row, name, default=0):
        i = idx.get(name)
        return row[i] if i is not None and row[i] is not None else default

    out = []
    for row in rows:
        out.append({
            "location": val(row, "location", None),
            "on_hand": float(val(row, "on_hand", 0) or 0),
            "allocated": float(val(row, "allocated", 0) or 0),
            "on_order": float(val(row, "on_order", 0) or 0),
        })
    return out


def read_stock(dsn: str, sql: str, item_ref: Optional[str]) -> list[dict]:
    """Run the configured parameterized query and map the result.

    `sql` uses a named `:item_ref` placeholder; we convert it to the qmark
    paramstyle pyodbc expects. Imported lazily so the driver is only required
    when a live source is actually configured."""
    import pyodbc  # noqa: WPS433 (lazy: only needed in live mode)

    query = sql.replace(":item_ref", "?")
    n_params = sql.count(":item_ref")
    with pyodbc.connect(dsn, timeout=30) as conn:
        cur = conn.cursor()
        cur.execute(query, *([item_ref] * n_params))
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return map_rows(columns, rows)
