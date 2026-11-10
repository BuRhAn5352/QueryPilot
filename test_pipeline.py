"""Run with:  python test_pipeline.py   (no API key needed; the LLM is mocked)"""
import sqlite3

from fastapi.testclient import TestClient

from app import llm, main
from app.db import run_sql
from app.validator import UnsafeSQL, validate_sql

# ---- validator ------------------------------------------------------------
ok = validate_sql("SELECT product_name, SUM(quantity_sold) q FROM sales_view GROUP BY 1 ORDER BY q DESC")
assert "LIMIT 100" in ok.upper(), ok
assert validate_sql("```sql\nSELECT 1 FROM sales_view;\n```")
assert "LIMIT 5" in validate_sql("SELECT * FROM sales_view LIMIT 5").upper()
assert validate_sql("WITH t AS (SELECT * FROM sales_view) SELECT COUNT(*) FROM t")

bad = [
    "DROP TABLE sales_fact",
    "DELETE FROM sales_fact",
    "UPDATE sales_fact SET total_sales = 0",
    "INSERT INTO store_dim VALUES (99,'a','b','c')",
    "SELECT 1; DROP TABLE sales_fact",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM users",
    "PRAGMA table_info(sales_fact)",
    "ATTACH DATABASE 'x.db' AS x",
    "",
]
for q in bad:
    try:
        validate_sql(q)
    except UnsafeSQL:
        continue
    raise AssertionError(f"should have been blocked: {q!r}")
print("validator: ok")

# ---- db runner ------------------------------------------------------------
cols, rows = run_sql("SELECT COUNT(*) AS n FROM sales_fact")
assert cols == ["n"] and rows[0][0] > 0
try:
    run_sql("DELETE FROM sales_fact")
    raise AssertionError("write was allowed on read-only connection")
except sqlite3.Error:
    pass
try:  # runaway query hits the timeout
    run_sql("WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r) SELECT COUNT(*) FROM r",
            timeout_s=0.5)
    raise AssertionError("timeout did not fire")
except sqlite3.OperationalError:
    pass
print("db runner: ok")

# ---- endpoint with a mocked LLM (also exercises the retry loop) -----------
calls = {"n": 0}


def fake_generate_sql(question, prev_sql=None, error=None):
    calls["n"] += 1
    if calls["n"] == 1:
        return "SELECT product_nme FROM sales_view"  # typo -> SQL error -> retry
    return ("SELECT product_name, SUM(quantity_sold) AS units FROM sales_view "
            "WHERE sale_date >= date('2026-11-10','start of month','-1 month') "
            "AND sale_date < date('2026-11-10','start of month') "
            "GROUP BY product_name ORDER BY units DESC LIMIT 1")


llm.generate_sql = fake_generate_sql
llm.generate_answer = lambda q, sql, cols, rows: f"Top product: {rows[0][0]} ({rows[0][1]} units)."

client = TestClient(main.app)
r = client.post("/ask", json={"question": "most sold product last month?"})
assert r.status_code == 200, r.text
body = r.json()
assert body["attempts"] == 2 and body["rows"][0][0] == "Travel Notebook", body
assert client.get("/").status_code == 200

llm.generate_sql = lambda q, prev_sql=None, error=None: "DROP TABLE sales_fact"
r = client.post("/ask", json={"question": "delete everything please"})
assert r.status_code == 422, r.text
print("endpoint + retry + blocking: ok")
print("ALL TESTS PASSED")
