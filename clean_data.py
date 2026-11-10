#!/usr/bin/env python3
"""
Clean the "Sales Warehouse" sample database for a text-to-SQL project.

Usage:
    python clean_data.py sales_warehouse.sql
    python clean_data.py sales_warehouse.sql --out-dir . --end-date 2026-11-09

Outputs (in --out-dir):
    shop.db             cleaned SQLite database
    schema_prompt.txt   schema + rules to paste into your LLM prompt

What it fixes:
    1. Dates shifted forward so the newest sale is yesterday (so "last month" works)
    2. date_dim rebuilt: one row per calendar day, month/year/weekday computed from the date
    3. Rows with quantity <= 0 dropped
    4. total_sales recomputed as quantity * price - discount (never negative)
    5. discount_amount: NULL -> 0, capped at 30% of the gross amount
    6. Duplicate product names made unique (second "Smart Charger" -> "Smart Charger II")
    7. Product categories derived from the product type (Jacket -> Clothing, ...)
    8. Duplicate customer emails made unique; join_date never later than first purchase
    9. Adds indexes and a flat view `sales_view` so the LLM needs fewer JOINs
"""
import argparse
import os
import sqlite3
from datetime import date, timedelta

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

# Product type (last words of product_name) -> category
NOUN_CATEGORY = {
    "Monitor": "Electronics", "Speaker": "Electronics", "Keyboard": "Electronics",
    "Camera": "Electronics", "Charger": "Electronics", "Headphones": "Electronics",
    "Router": "Electronics", "Tripod": "Electronics",
    "Jacket": "Clothing", "Backpack": "Clothing", "Watch": "Clothing",
    "Notebook": "Home", "Lantern": "Home", "Pan": "Home", "Blender": "Home",
    "Chair": "Home", "Kettle": "Home", "Mug": "Home", "Desk Lamp": "Home",
    "Planter": "Home", "Toolkit": "Home", "Umbrella": "Home",
    "Tent": "Sports", "Bottle": "Sports",
}

SCHEMA = """
CREATE TABLE customer_dim (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    join_date TEXT NOT NULL
);
CREATE TABLE date_dim (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,
    day_of_week TEXT NOT NULL,
    month INTEGER NOT NULL,
    month_name TEXT NOT NULL,
    year INTEGER NOT NULL,
    quarter INTEGER NOT NULL,
    is_weekend INTEGER NOT NULL
);
CREATE TABLE product_dim (
    id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0)
);
CREATE TABLE store_dim (
    id INTEGER PRIMARY KEY,
    store_name TEXT NOT NULL,
    location TEXT NOT NULL,
    manager TEXT NOT NULL
);
CREATE TABLE sales_fact (
    id INTEGER PRIMARY KEY,
    date_id INTEGER NOT NULL REFERENCES date_dim(id),
    product_id INTEGER NOT NULL REFERENCES product_dim(id),
    store_id INTEGER NOT NULL REFERENCES store_dim(id),
    customer_id INTEGER REFERENCES customer_dim(id),
    quantity_sold INTEGER NOT NULL CHECK (quantity_sold > 0),
    total_sales REAL NOT NULL CHECK (total_sales >= 0),
    discount_amount REAL NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    payment_method TEXT NOT NULL,
    customer_feedback INTEGER
);
CREATE INDEX idx_sales_date ON sales_fact(date_id);
CREATE INDEX idx_sales_product ON sales_fact(product_id);
CREATE INDEX idx_sales_store ON sales_fact(store_id);
CREATE INDEX idx_sales_customer ON sales_fact(customer_id);

CREATE VIEW sales_view AS
SELECT
    s.id                AS sale_id,
    d.date              AS sale_date,
    d.year              AS year,
    d.month             AS month,
    d.month_name        AS month_name,
    d.day_of_week       AS day_of_week,
    p.id                AS product_id,
    p.product_name      AS product_name,
    p.category          AS category,
    p.price             AS unit_price,
    st.id               AS store_id,
    st.store_name       AS store_name,
    st.location         AS store_location,
    st.manager          AS store_manager,
    s.customer_id       AS customer_id,
    COALESCE(c.first_name || ' ' || c.last_name, 'Guest') AS customer_name,
    s.quantity_sold     AS quantity_sold,
    s.discount_amount   AS discount_amount,
    s.total_sales       AS total_sales,
    s.payment_method    AS payment_method,
    s.customer_feedback AS customer_feedback
FROM sales_fact s
JOIN date_dim d     ON d.id = s.date_id
JOIN product_dim p  ON p.id = s.product_id
JOIN store_dim st   ON st.id = s.store_id
LEFT JOIN customer_dim c ON c.id = s.customer_id;
"""

PROMPT_TEMPLATE = """\
You write SQLite queries for a retail sales database.

DATA RANGE: sales run from {min_date} to {max_date}. Dates are TEXT 'YYYY-MM-DD'.
Use date(), strftime() and date modifiers for date logic, e.g.:
  - last month:  sale_date >= date('{today}', 'start of month', '-1 month')
                 AND sale_date <  date('{today}', 'start of month')
  - this month:  sale_date >= date('{today}', 'start of month')
  - last 7 days: sale_date >= date('{today}', '-7 days')
TODAY is {today}. Never use date('now') in queries; use the literal date above.

PREFER THE VIEW sales_view (already joined, one row per sale):
  sales_view(sale_id, sale_date, year, month, month_name, day_of_week,
             product_id, product_name, category, unit_price,
             store_id, store_name, store_location, store_manager,
             customer_id, customer_name, quantity_sold, discount_amount,
             total_sales, payment_method, customer_feedback)

UNDERLYING TABLES (use only if the view cannot answer):
  sales_fact(id, date_id, product_id, store_id, customer_id, quantity_sold,
             total_sales, discount_amount, payment_method, customer_feedback)
  date_dim(id, date, day_of_week, month, month_name, year, quarter, is_weekend)
  product_dim(id, product_name, category, price)
  store_dim(id, store_name, location, manager)
  customer_dim(id, first_name, last_name, email, join_date)

COLUMN MEANINGS:
  - quantity_sold: units sold in that sale (always > 0).
  - total_sales: net revenue of the sale in currency units = quantity_sold * unit_price - discount_amount. Never negative.
  - discount_amount: discount in currency units, 0 if none.
  - "most sold product" = SUM(quantity_sold) grouped by product_name.
  - "revenue" / "sales amount" = SUM(total_sales).
  - customer_id is NULL for guest purchases (customer_name = 'Guest').
  - customer_feedback: rating 1-10, NULL if the customer left none. Use AVG() which ignores NULLs.
  - payment_method values: credit card, debit card, paypal, gift card.
  - category values: Electronics, Clothing, Home, Sports.
  - product_name is unique. store_name is unique per store; several stores can share a location.

RULES:
  - Return exactly ONE SELECT statement and nothing else. No explanations, no markdown.
  - Never modify data (no INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA).
  - Add LIMIT 20 to queries that list rows unless the question asks for a specific number.
  - For "top"/"most"/"highest" questions, ORDER BY the metric DESC and LIMIT 1 (or N if asked).
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sql_file", help="path to the original sales_warehouse .sql file")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--end-date", default=None,
                    help="newest sale date, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--today", default=None,
                    help="today's date, YYYY-MM-DD (default: system date)")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    end = date.fromisoformat(args.end_date) if args.end_date else today - timedelta(days=1)

    # ---- load the raw dump into memory ------------------------------------
    raw = sqlite3.connect(":memory:")
    with open(args.sql_file, encoding="utf-8") as f:
        raw.executescript(f.read())

    raw_dates = dict(raw.execute("SELECT id, date FROM date_dim"))
    used = [date.fromisoformat(raw_dates[r[0]])
            for r in raw.execute("SELECT date_id FROM sales_fact")
            if r[0] in raw_dates]
    raw_min, raw_max = min(used), max(used)
    offset = end - raw_max  # shift everything forward by this many days

    # ---- build the clean database -----------------------------------------
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "shop.db")
    if os.path.exists(out_path):
        os.remove(out_path)
    db = sqlite3.connect(out_path)
    db.executescript(SCHEMA)

    # date_dim: one row per calendar day, everything computed from the date
    date_id = {}
    rows, d, i = [], raw_min + offset, 1
    while d <= end:
        iso = d.isoformat()
        rows.append((i, iso, WEEKDAYS[d.weekday()], d.month, MONTHS[d.month - 1],
                     d.year, (d.month - 1) // 3 + 1, 1 if d.weekday() >= 5 else 0))
        date_id[iso] = i
        i += 1
        d += timedelta(days=1)
    db.executemany("INSERT INTO date_dim VALUES (?,?,?,?,?,?,?,?)", rows)

    # product_dim: unique names, category from product type
    seen, price_of, prods = {}, {}, []
    nouns = sorted(NOUN_CATEGORY, key=len, reverse=True)
    for pid, name, cat, price in raw.execute(
            "SELECT id, product_name, category, price FROM product_dim ORDER BY id"):
        seen[name] = seen.get(name, 0) + 1
        new_name = name if seen[name] == 1 else f"{name} {'I' * seen[name]}"
        for noun in nouns:
            if name.endswith(noun):
                cat = NOUN_CATEGORY[noun]
                break
        price = round(float(price), 2)
        price_of[pid] = price
        prods.append((pid, new_name, cat, price))
    db.executemany("INSERT INTO product_dim VALUES (?,?,?,?)", prods)

    # store_dim: unchanged
    db.executemany("INSERT INTO store_dim VALUES (?,?,?,?)",
                   raw.execute("SELECT id, store_name, location, manager FROM store_dim").fetchall())

    # sales_fact
    sales, first_sale, dropped = [], {}, 0
    for (sid, did, pid, stid, cid, qty, total, disc, pay, fb) in raw.execute(
            "SELECT id, date_id, product_id, store_id, customer_id, quantity_sold, "
            "total_sales, discount_amount, payment_method, customer_feedback "
            "FROM sales_fact ORDER BY id"):
        if qty is None or qty <= 0 or pid not in price_of or did not in raw_dates:
            dropped += 1
            continue
        sdate = (date.fromisoformat(raw_dates[did]) + offset).isoformat()
        gross = qty * price_of[pid]
        disc = round(min(disc or 0.0, 0.30 * gross), 2)
        net = round(gross - disc, 2)
        sales.append((sid, date_id[sdate], pid, stid, cid, qty, net, disc,
                      (pay or "unknown").strip().lower(), fb))
        if cid is not None and (cid not in first_sale or sdate < first_sale[cid]):
            first_sale[cid] = sdate

    # customer_dim: unique emails, join_date <= first purchase
    emails, custs = set(), []
    for cid, fn, ln, email, jd in raw.execute(
            "SELECT id, first_name, last_name, email, join_date FROM customer_dim ORDER BY id"):
        e = email.strip().lower()
        if e in emails:
            local, dom = e.split("@", 1)
            e = f"{local}{cid}@{dom}"
        emails.add(e)
        j = (date.fromisoformat(jd) + offset).isoformat()
        if cid in first_sale and first_sale[cid] < j:
            j = first_sale[cid]
        custs.append((cid, fn.strip(), ln.strip(), e, j))
    db.executemany("INSERT INTO customer_dim VALUES (?,?,?,?,?)", custs)

    db.executemany("INSERT INTO sales_fact VALUES (?,?,?,?,?,?,?,?,?,?)", sales)
    db.commit()

    # ---- checks -----------------------------------------------------------
    assert db.execute("PRAGMA foreign_key_check").fetchall() == [], "foreign key problem"
    assert db.execute("SELECT COUNT(*) FROM sales_fact "
                      "WHERE total_sales < 0 OR quantity_sold <= 0").fetchone()[0] == 0

    min_d, max_d = db.execute(
        "SELECT MIN(sale_date), MAX(sale_date) FROM sales_view").fetchone()

    with open(os.path.join(args.out_dir, "schema_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(PROMPT_TEMPLATE.format(min_date=min_d, max_date=max_d, today="{TODAY}"))

    # ---- summary ----------------------------------------------------------
    first_this = today.replace(day=1)
    lm_start = (first_this - timedelta(days=1)).replace(day=1)
    top = db.execute(
        "SELECT product_name, SUM(quantity_sold) q FROM sales_view "
        "WHERE sale_date >= ? AND sale_date < ? GROUP BY product_name "
        "ORDER BY q DESC LIMIT 3", (lm_start.isoformat(), first_this.isoformat())).fetchall()

    print(f"Wrote {out_path}")
    print(f"  date shift: +{offset.days} days  ->  sales from {min_d} to {max_d}")
    print(f"  customers={len(custs)}  products={len(prods)}  "
          f"stores={db.execute('SELECT COUNT(*) FROM store_dim').fetchone()[0]}  "
          f"sales={len(sales)}  (dropped {dropped} invalid rows)")
    print(f"  top products last month ({lm_start:%B %Y}): {top}")
    db.close()


if __name__ == "__main__":
    main()
