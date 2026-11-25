# FootballSql — Read-Only Text-to-SQL Analytics

**Live Demo URL:** `<TBD — add Hamravesh deployment URL here>`

Bilingual (Persian / English) Text-to-SQL web application for the **Iran Persian Gulf Pro League 2026-27 (1405-06)** (`data/football.db`), featuring a modern `100dvh` chatbot UI, automatic Chart.js visualizations colored by each team's `primary_color`, and full SQL explainability & execution traces.

---

## Architecture & Safety Highlights

Pipeline: `Question -> Semantic Model -> SQL Generation -> Validation -> Dry-Run -> Read-Only Sandbox -> Grounded Answer`

- **Strict 4-Action Routing**: The LLM planner outputs strictly one of `answer_from_query`, `ask_clarifying_question`, `refuse_write`, or `refuse_out_of_scope`. Non-query actions never touch the database.
- **Static & AST Validation (`app/validator.py`)**: Rejects empty SQL, comments (`--`, `/* */`), multiple statements (`;`), and 12 forbidden write/admin keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `ATTACH`, `DETACH`, `PRAGMA`, `REPLACE`, `VACUUM`, `REINDEX`). Uses `sqlglot` (`read="sqlite"`) to verify a single `SELECT` / `WITH ... SELECT` / set-operation query and checks all tables, views, and columns against the live schema allowlist.
- **Dry-Run Verification**: Runs `EXPLAIN QUERY PLAN <sql>` on a read-only connection before execution.
- **Read-Only Sandbox (`app/sandbox.py` & `Dockerfile`)**: Opens SQLite strictly via `file:data/football.db?mode=ro` (`uri=True`), enforces a `2.0s` timeout (`set_progress_handler`) and a `200`-row cap (`fetchmany(201)`), and locks `data/football.db` with `chmod 444` inside the container.
- **Grounded Answers (`app/llm.py`)**: Synthesizes responses strictly from returned SQL rows in the user's language (Persian or English).

---

## Local Setup

1. **Create a virtual environment & install dependencies:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```
   Set your OpenAI-compatible credentials in `.env`:
   - `OPENAI_API_KEY` (or `LLM_API_KEY`)
   - `OPENAI_BASE_URL` (or `LLM_BASE_URL`)
   - `LLM_MODEL`

3. **Run the server:**
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```
   Open `http://127.0.0.1:8000` in your browser (`GET /health` returns `{"status": "ok"}`).

---

## Docker (Hamravesh / Darkube)

```bash
docker build -t footballsql .
docker run -p 8000:8000 --env-file .env footballsql
```

---

## Evaluation & Trace Capture

With the server running locally (or deployed):

```bash
# Run the 17-question evaluation suite (en, fa, or both)
python eval/run_eval.py --base-url http://127.0.0.1:8000 --lang en --delay 2.5

# Capture the 3 representative live traces (join query, refusal, clarifying question)
python eval/capture_traces.py --base-url http://127.0.0.1:8000
```

Reports are saved to `eval/results/eval_report.md`, `eval/results/eval_report.json`, `eval/results/traces.md`, and `eval/results/traces.json`.
