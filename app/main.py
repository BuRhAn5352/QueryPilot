"""Step 5: FastAPI endpoint that ties everything together."""
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import llm
from .db import run_sql
from .validator import UnsafeSQL, validate_sql

MAX_ATTEMPTS = 3  # first try + up to 2 self-corrections
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Ask your database")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.post("/ask")
def ask(body: Ask):
    question = body.question.strip()
    sql, error = None, None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw_sql = llm.generate_sql(question, prev_sql=sql, error=error)
            sql = validate_sql(raw_sql)
            columns, rows = run_sql(sql)
            break  # success
        except (UnsafeSQL, sqlite3.Error) as e:
            error = str(e)
            sql = sql or ""
            if attempt == MAX_ATTEMPTS:
                raise HTTPException(
                    status_code=422,
                    detail=f"Could not build a working query after {MAX_ATTEMPTS} tries: {error}",
                )

    answer = llm.generate_answer(question, sql, columns, rows)
    return {
        "answer": answer,
        "sql": sql,
        "columns": columns,
        "rows": [list(r) for r in rows],
        "attempts": attempt,
    }
