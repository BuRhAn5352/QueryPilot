"""Steps 1 and 4: the two LLM calls (question -> SQL, results -> answer)."""
import os
from datetime import date
from pathlib import Path

import anthropic

from .validator import strip_fences

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
PROMPT_FILE = Path(__file__).resolve().parent.parent / "schema_prompt.txt"

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def system_prompt() -> str:
    # Re-read the date on every call so "last month" is always relative to today.
    return PROMPT_FILE.read_text(encoding="utf-8").replace("{TODAY}", date.today().isoformat())


def generate_sql(question: str, prev_sql: str | None = None, error: str | None = None) -> str:
    messages = [{"role": "user", "content": question}]
    if prev_sql and error:  # retry: show the model its own failed query + the error
        messages += [
            {"role": "assistant", "content": prev_sql},
            {"role": "user", "content": f"That query failed: {error}\n"
                                        "Return a corrected single SELECT statement only."},
        ]
    resp = client().messages.create(
        model=MODEL, max_tokens=600,
        system=system_prompt(), messages=messages,
    )
    return strip_fences(resp.content[0].text)


def generate_answer(question: str, sql: str, columns: list[str], rows: list) -> str:
    shown = rows[:50]
    table = "\n".join([" | ".join(columns)] + [" | ".join(str(v) for v in r) for r in shown])
    note = f"\n(showing first 50 of {len(rows)} rows)" if len(rows) > 50 else ""
    prompt = (
        f"Question: {question}\n\nSQL used:\n{sql}\n\nResult:\n{table}{note}\n\n"
        "Answer the question in 1-3 plain sentences using ONLY the result above. "
        "Include the key numbers. If the result is empty, say no matching data was found. "
        "Do not mention SQL or tables."
    )
    resp = client().messages.create(
        model=MODEL, max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()
