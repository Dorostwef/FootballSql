"""OpenAI-compatible LLM planner (SQL generation) and grounded answer synthesizer."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal

logger = logging.getLogger(__name__)

import sqlglot
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator
from sqlglot import exp

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
        "- Bilingual (English/Persian): write clarifying_question and refusal_reason in the user's language.\n"
        "- `refuse_write`: User asks to insert, update, delete, drop, alter, or modify data/tables (`sql: null`).\n"
        "- `refuse_out_of_scope`: Question asks for data absent from the schema (fan counts, attendance, budgets, salaries, transfers, market values, injuries, xG/possession, other seasons/leagues, predictions). NEVER use `refuse_out_of_scope` if the entities exist in the schema—check multi-hop joins via `match_id` (e.g., `card`/`goal`/`penalty` -> `match` -> `referee`/`stadium`) first. NEVER clarify out-of-scope questions (`sql: null`).\n"
        "- `ask_clarifying_question`: ONLY when genuinely subjective AND mapping to 2+ valid DB metrics (e.g., 'Who is the best player?'). NEVER clarify clear analytics questions that name a specific schema concept (cards, clean sheets, minutes, late goals, formations, coach changes, fixture scores, standings).\n"
        "- `answer_from_query`: Default whenever answerable from the schema.\n\n"
        "SQL GENERATION RULES:\n"
        "- Write ONE read-only SQLite SELECT (`WITH ... SELECT` and `UNION ALL` allowed). No comments, no semicolons, no write/admin keywords.\n"
        "- Team names: `team.name` (English) and `team.name_fa` (Persian); player/coach/referee/stadium names are Persian only (`name_fa`). ALWAYS select English `team.name` (never `team.name_fa`) in SQL output unless the user explicitly asks for Persian team names.\n"
        "- Human-readable names: ALWAYS join lookup tables (`team`, `player`, `coach`, `referee`, `stadium`) and project names instead of raw IDs (`team_id`, `player_id`, `coach_id`, `referee_id`, `stadium_id`).\n"
        "- Primary entity first & team context: For player/goalkeeper/coach queries, project `(primary_entity_name, team.name, metric...)` — entity name FIRST, then `team.name`, then metric(s). For team queries, project `(team.name, metric...)`.\n"
        "- Natural complete columns: Standings leader -> `(team, played, points, goal_diff)` ordered by `points DESC, goal_diff DESC, goals_for DESC`. Fixture score -> `(round, match_date, home_team, away_team, home_goals, away_goals)`. Coaching changes -> `GROUP BY team_id, coach_id` with `(team, coach, first_round, last_round, matches)`.\n"
        "- Concise event lists: When listing match events (goals, own goals, cards), select `(match_id, player name, team name(s), minute, added_time)` ordered by `match_id`; omit redundant `match_date_jalali` or `half` unless asked. For own goals, ALWAYS join `player` (`p.name_fa`) and join `team` twice to show both `scorer_team` (`scorer_team_id`) and `benefited_team` (`team_id`).\n"
        "- Deterministic tie-breaking & default top-N limits: Every ranking (`ORDER BY metric DESC`) MUST include a secondary ascending sort by primary entity name (`ORDER BY metric DESC, entity_name ASC`). For singular superlative questions without 'list all': use `LIMIT 1` for single league-table leader queries; use `LIMIT 3` for team, referee, or coach superlatives ('which team/referee/coach has the most...' or 'best home record'); use `LIMIT 5` for player or goalkeeper superlatives (or explicit 'top 5').\n"
        "- Played matches: Count played matches with `match WHERE status = 'finished'` (NEVER include `'awarded'`).\n"
        "- Goal & minute rules: Team goals scored = `goal.team_id` (player's club for scorer charts = `goal.scorer_team_id` with `is_own_goal = 0`). 'After the Nth minute' means `minute > N` (e.g. after 75th minute -> `half = 'second_half' AND minute >= 76`).\n"
        "- Multi-side & composite joins: Match teams across BOTH home & away (`v_team_match` or `OR`). Join `lineup` to `v_team_match` on BOTH `match_id` AND `team_id`. Follow all semantic model join rules (clean sheets, formations `CASE`, `v_player_minutes`).\n\n"
        "=== SEMANTIC MODEL (loaded dynamically from disk) ===\n"
        f"{semantic_model}\n\n"
        "=== SQLITE SCHEMA DDL (loaded dynamically from disk) ===\n"
        f"{schema_sql}\n"
    )


_ENTITY_ID_TO_TABLE = {
    "team_id": "team",
    "player_id": "player",
    "coach_id": "coach",
    "referee_id": "referee",
    "stadium_id": "stadium",
}


def check_sql_quality(sql: str, question: str = "") -> list[str]:
    """Run general static SQL quality checks on validated SQL before execution."""
    issues: list[str] = []
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return issues

    outer_select = tree if isinstance(tree, exp.Select) else None
    if outer_select is None:
        return issues

    outer_tables = {
        (t.alias or t.name).lower(): t.name.lower()
        for t in outer_select.find_all(exp.Table)
        if t.name and t.find_ancestor(exp.Select) is outer_select
    }
    outer_table_names = set(outer_tables.values())

    # 1. Catch ANY bare foreign-key / entity ID projected in the outer SELECT without its lookup name column.
    projected_col_refs: set[tuple[str, str]] = set()
    for expr in outer_select.expressions:
        target = expr.this if isinstance(expr, exp.Alias) else expr
        if isinstance(target, exp.Column):
            projected_col_refs.add(((target.table or "").lower(), target.name.lower()))

    for expr in outer_select.expressions:
        target = expr.this if isinstance(expr, exp.Alias) else expr
        if isinstance(target, exp.Column) and target.name.lower() in _ENTITY_ID_TO_TABLE:
            id_col = target.name.lower()
            lookup_tbl = _ENTITY_ID_TO_TABLE[id_col]
            expected_name_cols = {"name", "name_fa"} if lookup_tbl == "team" else {"name_fa"}
            lookup_aliases = {alias for alias, rel in outer_tables.items() if rel == lookup_tbl}
            has_lookup_name = (
                lookup_tbl in outer_table_names
                and any(
                    col in expected_name_cols and (tbl in lookup_aliases or not tbl)
                    for tbl, col in projected_col_refs
                )
            )
            if not has_lookup_name:
                issues.append(
                    f"Outer SELECT projects raw ID '{target.name}'; join '{lookup_tbl}' and select its "
                    f"human-readable name ({'name' if lookup_tbl == 'team' else 'name_fa'}) instead."
                )

    # 2. Detect composite-key join fan-out: lineup joined with v_team_match must link BOTH match_id and team_id.
    for select_node in tree.find_all(exp.Select):
        tables_in_sel = {
            t.name.lower(): (t.alias or t.name).lower()
            for t in select_node.find_all(exp.Table)
            if t.name and t.find_ancestor(exp.Select) is select_node
        }
        if "lineup" in tables_in_sel and "v_team_match" in tables_in_sel:
            eq_pairs: set[frozenset[tuple[str, str]]] = set()
            for eq in select_node.find_all(exp.EQ):
                if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column):
                    l_tbl = (eq.left.table or "").lower()
                    r_tbl = (eq.right.table or "").lower()
                    eq_pairs.add(frozenset({(l_tbl, eq.left.name.lower()), (r_tbl, eq.right.name.lower())}))
            l_alias = tables_in_sel["lineup"]
            tm_alias = tables_in_sel["v_team_match"]
            has_match_link = frozenset({(l_alias, "match_id"), (tm_alias, "match_id")}) in eq_pairs
            has_team_link = frozenset({(l_alias, "team_id"), (tm_alias, "team_id")}) in eq_pairs
            if not (has_match_link and has_team_link):
                issues.append(
                    "Joining 'lineup' with 'v_team_match' MUST match BOTH match_id AND team_id "
                    "(e.g. l.match_id = tm.match_id AND l.team_id = tm.team_id) to avoid row multiplication."
                )

    # 3. Catch per-match match_coach queries returning duplicate (team, coach) rows without GROUP BY/DISTINCT.
    if "match_coach" in outer_table_names and "team" in outer_table_names and "coach" in outer_table_names:
        if not outer_select.args.get("group") and not outer_select.args.get("distinct"):
            issues.append(
                "Query reads per-match table 'match_coach' without GROUP BY; group by (team_id, coach_id) "
                "and include summary aggregates (MIN(round), MAX(round), COUNT(*)) instead of duplicate rows."
            )

    # 4. Catch SQLite GROUP BY collapsing non-aggregated player/coach columns when grouping only by team_id.
    for select_node in tree.find_all(exp.Select):
        group = select_node.args.get("group")
        if not group:
            continue
        grouped_cols = {
            col.name.lower()
            for g_expr in group.expressions
            for col in g_expr.find_all(exp.Column)
        }
        if "team_id" not in grouped_cols:
            continue

        for proj in select_node.expressions:
            target = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(target, exp.AggFunc):
                continue
            for col in target.find_all(exp.Column):
                if col.find_ancestor(exp.AggFunc) is not None:
                    continue
                col_lower = col.name.lower()
                tbl_lower = col.table.lower() if col.table else ""
                if (col_lower == "coach_id" or tbl_lower in ("coach", "c", "mc")) and "coach_id" not in grouped_cols and col_lower not in grouped_cols:
                    issues.append(
                        "GROUP BY groups by team_id while selecting non-aggregated coach data; "
                        "include coach_id in GROUP BY (e.g., GROUP BY team_id, coach_id) or aggregate it."
                    )
                elif (col_lower == "player_id" or tbl_lower in ("player", "p")) and "player_id" not in grouped_cols and col_lower not in grouped_cols:
                    issues.append(
                        "GROUP BY groups by team_id while selecting non-aggregated player data; "
                        "include player_id in GROUP BY or aggregate it."
                    )

    # 5. Player/coach grouped analytics must include team context and put primary entity first.
    if outer_select.args.get("group") and ("player" in outer_table_names or "coach" in outer_table_names):
        if "team" not in outer_table_names:
            issues.append(
                "Player or coach analytics queries must JOIN 'team' and include the team name alongside the player/coach name."
            )
        elif "player" in outer_table_names:
            proj_aliases = [
                (
                    (e.this.table.lower() if isinstance(e.this, exp.Column) and e.this.table else ""),
                    (e.alias or (e.this.name if isinstance(e.this, exp.Column) else "")).lower(),
                )
                for e in outer_select.expressions
            ]
            player_pos = next(
                (i for i, (tbl, alias) in enumerate(proj_aliases) if tbl in ("p", "player") or alias in ("player", "goalkeeper", "scorer")),
                None,
            )
            team_pos = next(
                (i for i, (tbl, alias) in enumerate(proj_aliases) if tbl in ("t", "team") or alias == "team"),
                None,
            )
            if player_pos is not None and team_pos is not None and team_pos < player_pos:
                issues.append(
                    "For player rankings, project the player/goalkeeper name FIRST, followed by team name, then metric."
                )

    # 6. Grouped rankings with ORDER BY ... DESC must have deterministic tie-breaking and avoid blind LIMIT 1.
    order = outer_select.args.get("order")
    if outer_select.args.get("group") and order and order.expressions:
        has_desc = any(o.args.get("desc") for o in order.expressions)
        if has_desc and len(order.expressions) == 1 and "v_standings" not in outer_table_names:
            issues.append(
                "Grouped ranking ORDER BY ... DESC lacks a secondary tie-breaker; add an ascending primary entity name "
                "(e.g., ORDER BY metric DESC, entity_name ASC)."
            )
        limit_node = outer_select.args.get("limit")
        if has_desc and limit_node and "v_standings" not in outer_table_names:
            limit_expr = limit_node.expression
            is_team_coach_ref = (
                "coach" in outer_table_names
                or "referee" in outer_table_names
                or ("team" in outer_table_names and "player" not in outer_table_names)
            )
            q_lower_for_limit = (question or "").lower()
            asks_explicit_5 = any(tok in q_lower_for_limit for tok in ("top 5", "top five", "5 ", "۵"))
            if isinstance(limit_expr, exp.Literal) and limit_expr.name == "1":
                target_limit = "LIMIT 3" if is_team_coach_ref else "LIMIT 5"
                issues.append(
                    f"Do not use LIMIT 1 on grouped 'most/best' rankings because ties may exist; use {target_limit}."
                )
            elif (
                isinstance(limit_expr, exp.Literal)
                and limit_expr.name == "5"
                and is_team_coach_ref
                and not asks_explicit_5
            ):
                issues.append(
                    "For singular superlative rankings over teams, coaches, or referees (without 'top 5'), "
                    "use a concise podium of LIMIT 3 instead of LIMIT 5."
                )
        if has_desc and not limit_node and "player" in outer_table_names:
            issues.append(
                "Superlative player/goalkeeper ranking has no LIMIT; add LIMIT 5 while keeping deterministic tie-breaking."
            )
        elif has_desc and not limit_node and "referee" in outer_table_names:
            issues.append(
                "Superlative referee ranking has no LIMIT; add LIMIT 3 while keeping deterministic tie-breaking."
            )

    # 7. Team-level goal queries (no player table) must join team on goal.team_id, not goal.scorer_team_id, and output team.name.
    if "goal" in outer_table_names and "team" in outer_table_names and "player" not in outer_table_names:
        for eq in outer_select.find_all(exp.EQ):
            cols_in_eq = {
                c.name.lower()
                for c in (eq.left, eq.right)
                if isinstance(c, exp.Column)
            }
            if "scorer_team_id" in cols_in_eq:
                issues.append(
                    "For team-level goals-scored queries (without player), join 'team' on goal.team_id "
                    "(credited team), NOT goal.scorer_team_id (which counts own goals scored against the club)."
                )
                break

    # 8. Enforce English team.name unless the user explicitly asked for Persian team names.
    q_lower = (question or "").lower()
    wants_persian_team = "persian" in q_lower or "name_fa" in q_lower or "فارسی" in q_lower
    if not wants_persian_team and "team" in outer_table_names:
        team_aliases = {alias for alias, rel in outer_tables.items() if rel == "team"}
        for expr in outer_select.expressions:
            target = expr.this if isinstance(expr, exp.Alias) else expr
            if isinstance(target, exp.Column) and target.name.lower() == "name_fa":
                tbl_ref = (target.table or "").lower()
                if tbl_ref in team_aliases or (not tbl_ref and outer_table_names == {"team"}):
                    issues.append(
                        "Project English team.name instead of team.name_fa for team names in SQL output."
                    )
                    break

    # 9. Catch unsatisfiable HAVING COUNT(DISTINCT B) > 1 when B is already in GROUP BY (always 1 per group).
    for select_node in tree.find_all(exp.Select):
        group = select_node.args.get("group")
        having = select_node.args.get("having")
        if not group or not having:
            continue
        grouped_cols = {
            col.name.lower()
            for g_expr in group.expressions
            for col in g_expr.find_all(exp.Column)
        }
        if len(grouped_cols) < 2:
            continue
        for count_node in having.find_all(exp.Count):
            if not isinstance(count_node.this, exp.Distinct):
                continue
            distinct_cols = [
                col.name.lower()
                for d_expr in count_node.this.expressions
                for col in d_expr.find_all(exp.Column)
            ]
            if distinct_cols and all(c in grouped_cols for c in distinct_cols):
                cols_str = ", ".join(distinct_cols)
                issues.append(
                    f"HAVING COUNT(DISTINCT {cols_str}) > 1 is always false when ({cols_str}) is already in "
                    "GROUP BY (returns 0 rows). Move the HAVING COUNT(DISTINCT ...) > 1 filter into a CTE or "
                    "subquery grouped ONLY by the parent key (e.g., team_id), and keep GROUP BY (team_id, coach_id) "
                    "with MIN(m.round) AS first_round, MAX(m.round) AS last_round, COUNT(*) AS matches in the outer query."
                )
                break

    # 10. Goalkeeper questions using lineup must filter position = 'GK' (and starter status).
    q_lower_gk = (question or "").lower()
    asks_goalkeeper = any(tok in q_lower_gk for tok in ("goalkeeper", "keeper", "دروازه‌بان", "دروازه بان", "دروازهبان"))
    if asks_goalkeeper and "lineup" in outer_table_names:
        where_node = outer_select.args.get("where")
        where_sql = where_node.sql(dialect="sqlite").lower() if where_node else ""
        has_gk_filter = ("'gk'" in where_sql or "formation_line = 0" in where_sql)
        if not has_gk_filter:
            issues.append(
                "Question asks about goalkeepers using 'lineup', so WHERE must filter position = 'GK' "
                "(and is_starter = 1 for starting goalkeepers)."
            )

    return list(dict.fromkeys(issues))


_OUT_OF_SCOPE_HINTS = (
    "fan",
    "fans",
    "supporter",
    "attendance",
    "budget",
    "salary",
    "salaries",
    "wage",
    "transfer",
    "market value",
    "injury",
    "injuries",
    "possession",
    "expected goals",
    "xg",
    "هوادار",
    "تماشاگر",
    "بودجه",
    "دستمزد",
    "نقل و انتقال",
    "مصدوم",
)

_IN_SCHEMA_ENTITY_HINTS = (
    "referee",
    "coach",
    "manager",
    "card",
    "yellow",
    "red",
    "goal",
    "penalty",
    "penalties",
    "formation",
    "lineup",
    "minute",
    "minutes",
    "var",
    "clean sheet",
    "stadium",
    "standings",
    "match",
    "matches",
    "داور",
    "سرمربی",
    "کارت",
    "گل",
    "پنالتی",
    "آرایش",
    "ترکیب",
    "دقیقه",
    "کلین‌شیت",
    "کلین شیت",
    "ورزشگاه",
    "جدول",
)


def should_recheck_out_of_scope(question: str) -> bool:
    """Return True if a refused question references schema entities without clear out-of-scope concepts."""
    q_lower = (question or "").lower()
    if any(re.search(rf"\b{re.escape(tok)}\b", q_lower) if tok.isascii() else tok in q_lower for tok in _OUT_OF_SCOPE_HINTS):
        return False
    return any(
        re.search(rf"\b{re.escape(tok)}\b", q_lower) if tok.isascii() else tok in q_lower
        for tok in _IN_SCHEMA_ENTITY_HINTS
    )


def plan_and_generate_sql(
    question: str,
    history: list[dict[str, Any]] | None = None,
    semantic_model: str | None = None,
    schema_sql: str | None = None,
    validation_error: str | None = None,
    failed_sql: str | None = None,
    scope_retry_note: str | None = None,
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

    if scope_retry_note:
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "action": "refuse_out_of_scope",
                        "intent": "Initial out-of-scope check",
                        "sql": None,
                        "clarifying_question": None,
                        "refusal_reason": "Cannot answer from a single table.",
                    },
                    ensure_ascii=False,
                ),
            }
        )
        messages.append({"role": "user", "content": scope_retry_note})

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.0,
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
            temperature=0.0,
        )
    except Exception:
        logger.exception("generate_answer failed during LLM completion.")
        raise
    return (response.choices[0].message.content or "").strip()
