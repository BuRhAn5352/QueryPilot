"""Step 3: run the validated SQL against a READ-ONLY connection with a time limit."""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "shop.db"


def run_sql(sql: str, max_rows: int = 100, timeout_s: float = 5.0):
    """Return (columns, rows). Raises sqlite3.Error on bad SQL or timeout."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # read-only file handle
    con.execute("PRAGMA query_only = ON")
    start = time.time()
    # SQLite calls this every 10k VM steps; returning 1 aborts the query.
    con.set_progress_handler(lambda: 1 if time.time() - start > timeout_s else 0, 10_000)
    try:
        cur = con.execute(sql)
        columns = [c[0] for c in cur.description]
        rows = cur.fetchmany(max_rows)
    finally:
        con.close()
    return columns, rows
