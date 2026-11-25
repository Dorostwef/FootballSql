"""OpenAI-compatible LLM planner (SQL generation) and grounded answer synthesizer."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

from app.semantic import load_schema_sql, load_semantic_model

ActionType = Literal[
    "answer_from_query",
    "ask_clarifying_question",
    "refuse_write",
    "refuse_out_of_scope",
]


class LLMConfigurationError(RuntimeError):
    """Raised when required LLM configuration environment variables are missing."""


class SQLPlan(BaseModel):
    """Structured planner output enforcing the strict 4-action contract."""

    action: ActionType
    intent: str = Field(..., min_length=1)
    sql: str | None = None
    clarifying_question: str | None = None
    refusal_reason: str | None = None

    @model_validator(mode="after")
    def _enforce_action_fields(self) -> SQLPlan:
        if self.action == "answer_from_query":
            if not self.sql or not self.sql.strip():
                raise ValueError("action='answer_from_query' requires non-empty sql.")
            self.sql = self.sql.strip()
            self.clarifying_question = None
            self.refusal_reason = None
        elif self.action == "ask_clarifying_question":
            if not self.clarifying_question or not self.clarifying_question.strip():
                raise ValueError("action='ask_clarifying_question' requires clarifying_question.")
            self.clarifying_question = self.clarifying_question.strip()
            self.sql = None
            self.refusal_reason = None
        else:
            if not self.refusal_reason or not self.refusal_reason.strip():
                raise ValueError(f"action='{self.action}' requires refusal_reason.")
            self.refusal_reason = self.refusal_reason.strip()
            self.sql = None
            self.clarifying_question = None
        return self


def _get_llm_settings() -> tuple[str, str | None, str]:
    """Read LLM credentials and model name from environment / .env."""
    load_dotenv(override=False)
    api_key = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    base_url = (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip() or None
    model = (os.getenv("LLM_MODEL") or "").strip()
    if not api_key:
        raise LLMConfigurationError("LLM_API_KEY (or OPENAI_API_KEY) is not configured.")
    if not model:
        raise LLMConfigurationError("LLM_MODEL is not configured.")
    return api_key, base_url, model


def get_openai_client() -> tuple[OpenAI, str]:
    """Create an OpenAI-compatible SDK client using environment settings."""
    api_key, base_url, model = _get_llm_settings()
    return OpenAI(api_key=api_key, base_url=base_url), model


def _strip_json_fences(raw_text: str) -> str:
    """Remove optional markdown code fences around JSON output."""
    cleaned = raw_text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else cleaned


def _format_history_messages(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Normalize previous clarifying Q&A history into chat messages."""
    messages: list[dict[str, str]] = []
    if not history:
        return messages

    for turn in history:
        if not isinstance(turn, dict):
            continue
        if "role" in turn and "content" in turn:
            role = str(turn["role"]).strip().lower()
            content = str(turn["content"]).strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
            continue

        q_text = str(turn.get("question") or "").strip()
        a_text = str(turn.get("answer") or turn.get("clarifying_question") or "").strip()
        if q_text:
            messages.append({"role": "user", "content": q_text})
        if a_text:
            messages.append({"role": "assistant", "content": a_text})
    return messages


def _build_planner_system_prompt(semantic_model: str, schema_sql: str) -> str:
    """Construct the Step-1 planner system prompt using runtime-loaded semantic docs and schema."""
    return (
        "You are a football database Text-to-SQL planner for the Iran Persian Gulf Pro League (SQLite).\n"
        "You MUST reply with a single valid JSON object matching this exact schema:\n"
        "{\n"
        '  "action": "answer_from_query" | "ask_clarifying_question" | "refuse_write" | "refuse_out_of_scope",\n'
        '  "intent": "<concise summary of user intent>",\n'
        '  "sql": "<single read-only SQLite SELECT query or null>",\n'
        '  "clarifying_question": "<ONE short follow-up question in user language or null>",\n'
        '  "refusal_reason": "<polite explanation in user language or null>"\n'
        "}\n\n"
        "STRICT ACTION RULES:\n"
        "1. Allowed actions are strictly: answer_from_query, ask_clarifying_question, refuse_write, refuse_out_of_scope.\n"
        "2. Set sql ONLY when action == 'answer_from_query' (otherwise null).\n"
        "3. Set clarifying_question ONLY when action == 'ask_clarifying_question' (otherwise null).\n"
        "4. Set refusal_reason ONLY when action is 'refuse_write' or 'refuse_out_of_scope' (otherwise null).\n\n"
        "DECISION & SAFETY RULES:\n"
        "- Works in both English and Persian (Farsi). Write clarifying_question and refusal_reason in the same language as the user's question.\n"
        "- If the user asks to insert, update, delete, drop, alter, modify, add, or create data/tables, choose action='refuse_write' with sql=null.\n"
        "- If the question cannot be answered from the schema below (e.g., fan attendance/counts, transfers, market values, injuries, xG/possession/shots, other seasons/leagues, predictions), choose action='refuse_out_of_scope' with sql=null.\n"
        "- If the question is vague or ambiguous (e.g., 'Who is the best player?' or 'Best team?' where 'best' could mean goals, clean sheets, minutes, points, etc.), choose action='ask_clarifying_question' and ask ONE concise clarifying question WITHOUT picking a metric for the user. If conversation history resolves the ambiguity, proceed to 'answer_from_query'.\n\n"
        "SQL GENERATION RULES:\n"
        "- Write strictly ONE read-only SQLite SELECT statement (WITH ... SELECT CTEs and UNION ALL are allowed).\n"
        "- NEVER use SQL comments (-- or /* */), multiple statements (;), trailing semicolons, or any write/admin keyword (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, DETACH, PRAGMA, REPLACE, VACUUM, REINDEX).\n"
        "- Team names exist in team.name (English) and team.name_fa (Persian); player, coach, referee, and stadium names are Persian only (name_fa).\n"
        "- ALWAYS output English team.name in query results unless the user explicitly asks for Persian team names.\n"
        "- When filtering or aggregating by a team, match across BOTH home and away sides (e.g., via v_team_match, OR on home_team_id/away_team_id, or UNION ALL) — never only home_team_id.\n"
        "- Whenever a question asks for a ranking, standings, top-N, or 'most/best' (queries suitable for charts), ALWAYS include the relevant metric column(s) (e.g., points, played, goal_diff for standings, or the count/sum column for rankings) in the SELECT clause alongside the entity name — never select only the name alone.\n"
        "- Follow all join rules and quirks in the semantic model below (e.g., goal.scorer_team_id vs goal.team_id for own goals, excluding is_own_goal = 1 for top scorers, not double-counting penalties with goals, joining v_team_match to lineup on both match_id and team_id).\n\n"
        "=== SEMANTIC MODEL (loaded dynamically from disk) ===\n"
        f"{semantic_model}\n\n"
        "=== SQLITE SCHEMA DDL (loaded dynamically from disk) ===\n"
        f"{schema_sql}\n"
    )


def plan_and_generate_sql(
    question: str,
    history: list[dict[str, Any]] | None = None,
    semantic_model: str | None = None,
    schema_sql: str | None = None,
    validation_error: str | None = None,
    failed_sql: str | None = None,
    client: Any | None = None,
    model: str | None = None,
) -> SQLPlan:
    """Step 1: Classify intent into one of 4 strict actions and generate read-only SQL if needed."""
    loaded_semantic = semantic_model if semantic_model is not None else load_semantic_model()
    loaded_schema = schema_sql if schema_sql is not None else load_schema_sql()

    if client is None or model is None:
        resolved_client, resolved_model = get_openai_client()
        client = client or resolved_client
        model = model or resolved_model

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _build_planner_system_prompt(loaded_semantic, loaded_schema)}
    ]
    messages.extend(_format_history_messages(history))
    messages.append({"role": "user", "content": question})

    if validation_error and failed_sql:
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "action": "answer_from_query",
                        "intent": "Initial SQL attempt",
                        "sql": failed_sql,
                        "clarifying_question": None,
                        "refusal_reason": None,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your previous SQL failed safety/schema validation with this error:\n"
                    f"{validation_error}\n\n"
                    "Fix the query so it uses ONLY valid tables/views/columns from the schema and passes "
                    "read-only SQLite validation (single SELECT statement, no comments, no trailing semicolon, "
                    "no write/admin keywords). If the question actually cannot be answered from the schema, "
                    "return refuse_out_of_scope."
                ),
            }
        )

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0,
    )
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(_strip_json_fences(content))
    return SQLPlan.model_validate(parsed)


def generate_answer(
    question: str,
    sql: str,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
    history: list[dict[str, Any]] | None = None,
    client: Any | None = None,
    model: str | None = None,
) -> str:
    """Step 2: Format returned SQL rows into a strictly grounded Persian/English analyst answer."""
    if client is None or model is None:
        resolved_client, resolved_model = get_openai_client()
        client = client or resolved_client
        model = model or resolved_model

    system_prompt = (
        "You are a factual football data analyst reporting ONLY from SQL query results.\n"
        "STRICT RULES:\n"
        "1. ALWAYS write a short, complete, natural sentence in the exact same language as the user's question "
        "(Persian if Persian, English if English). NEVER output raw `column: value` pairs or bare numbers.\n"
        "2. When answering in Persian, write clean, natural, pure Persian using standard football terms "
        "(e.g., «امتیاز»، «تفاضل گل»، «گل زده»، «تعداد بازی») and NEVER mix in non-Persian/foreign "
        "characters or broken words (such as 'róż').\n"
        "   Example: for «تراکتور چند گل زده» with goals_for=12, reply: «تراکتور تا اینجای فصل ۱۲ گل زده است.»\n"
        "3. State ONLY numbers, names, scores, teams, and dates that explicitly appear in the provided SQL rows.\n"
        "4. NEVER invent, guess, extrapolate, or add any outside football knowledge.\n"
        "5. If rows is empty ([]), explicitly state that no matching data was found in the database.\n"
        "6. If truncated is true, explicitly note that the displayed results were capped at 200 rows."
    )

    payload = {
        "question": question,
        "sql": sql,
        "columns": columns,
        "row_count": len(rows),
        "truncated": truncated,
        "rows": rows,
    }

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(_format_history_messages(history))
    messages.append(
        {
            "role": "user",
            "content": (
                "Write the final grounded answer from these exact query results:\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
        }
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
        )
    except Exception:
        logger.exception("generate_answer failed during LLM completion.")
        raise
    return (response.choices[0].message.content or "").strip()
