# Ask your database (text to SQL)

## Run it
```bash
python -m venv venv
venv\Scripts\activate            # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt

set ANTHROPIC_API_KEY=sk-ant-...      # Windows cmd   (Mac/Linux: export ANTHROPIC_API_KEY=...)
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000 and ask a question.

## Check it works (no API key needed)
```bash
python test_pipeline.py
```

## How a question flows
1. `app/llm.py  generate_sql`   question + schema_prompt.txt + today's date -> SQL
2. `app/validator.py`           one SELECT only, known tables only, adds LIMIT 100
3. `app/db.py   run_sql`        read-only connection, 5 second timeout
4. `app/main.py`                if SQL fails, the error goes back to the LLM (max 3 tries)
5. `app/llm.py  generate_answer` question + rows -> plain-English answer

## Files
- `shop.db`            cleaned sample database (rebuild with clean_data.py)
- `schema_prompt.txt`  schema and rules for the LLM; edit this to improve accuracy
- `test_pipeline.py`   tests for validator, DB runner, retry loop (LLM mocked)

## When answers are wrong
Look at the "SQL used" box first. Fix it by adding a rule or a description to
`schema_prompt.txt`, then re-ask. Most errors are a missing rule, not a code bug.
Model name can be changed with the CLAUDE_MODEL environment variable.
