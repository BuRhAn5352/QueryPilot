"""Step 2: make sure the LLM's SQL is safe before it touches the database."""
import re

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {
    "sales_view", "sales_fact", "date_dim", "product_dim", "store_dim", "customer_dim",
}

# Any of these node types anywhere in the query means "not a plain read".
FORBIDDEN = tuple(
    getattr(exp, name)
    for name in ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command",
                 "Pragma", "Attach", "Merge", "Transaction", "Commit", "Rollback")
    if hasattr(exp, name)
)
QUERY_TYPES = (getattr(exp, "Query", exp.Select), exp.Select)


class UnsafeSQL(ValueError):
    pass


def strip_fences(text: str) -> str:
    """The LLM sometimes wraps SQL in ```sql ... ```; remove that."""
    text = text.strip()
    text = re.sub(r"^```(?:sql)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip().rstrip(";").strip()


def validate_sql(sql: str, max_rows: int = 100) -> str:
    """Return a safe, LIMIT-ed SQLite query or raise UnsafeSQL."""
    sql = strip_fences(sql)
    if not sql:
        raise UnsafeSQL("Empty query.")

    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.SqlglotError as e:
        raise UnsafeSQL(f"Could not parse SQL: {e}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQL("Exactly one statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, QUERY_TYPES):
        raise UnsafeSQL("Only SELECT queries are allowed.")
    if any(tree.find(node) for node in FORBIDDEN):
        raise UnsafeSQL("Query contains a forbidden operation.")

    cte_names = {c.alias for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.name not in ALLOWED_TABLES and table.name not in cte_names:
            raise UnsafeSQL(f"Table not allowed: {table.name}")

    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(max_rows)
    return tree.sql(dialect="sqlite")
