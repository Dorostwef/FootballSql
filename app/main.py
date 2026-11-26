"""FastAPI application exposing the FootballSql read-only Text-to-SQL pipeline."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.llm import (
    ActionType,
    LLMConfigurationError,
    SQLPlan,
    check_sql_quality,
    generate_answer,
    plan_and_generate_sql,
    should_recheck_out_of_scope,
)
from app.sandbox import SandboxError, execute_query, get_team_colors
from app.semantic import extract_schema_allowlist, load_schema_sql, load_semantic_model
from app.validator import ValidationResult, validate_sql

_PERSIAN_CHAR_RE = re.compile(r"[\u0600-\u06FF]")


class AskRequest(BaseModel):
    """Input payload for POST /api/ask."""

    question: str
    history: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be empty.")
        return stripped


class ValidationPayload(BaseModel):
    """Validation status included in /api/ask responses."""

    checked: bool
    valid: bool
    errors: list[str] = Field(default_factory=list)


class DryRunPayload(BaseModel):
    """EXPLAIN QUERY PLAN dry-run status included in /api/ask responses."""

    checked: bool
    ok: bool
    plan: list[str] = Field(default_factory=list)


class AskResponse(BaseModel):
    """Full explainability payload returned by POST /api/ask."""

    action: ActionType
    intent: str
    answer: str
    sql: str | None = None
    tables_used: list[str] = Field(default_factory=list)
    columns_used: list[str] = Field(default_factory=list)
    validation: ValidationPayload
    dry_run: DryRunPayload
    row_count: int = 0
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    truncated: bool = False
    executed: bool = False


app = FastAPI(
    title="FootballSql Read-Only Text-to-SQL API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def serve_index() -> FileResponse:
    """Serve the single-page analytics frontend."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="Frontend index.html not found.")
    return FileResponse(index_path)


def _is_persian(text: str) -> bool:
    """Detect whether the input contains Persian/Arabic script characters."""
    return bool(_PERSIAN_CHAR_RE.search(text))


def _non_query_response(plan: SQLPlan) -> AskResponse:
    """Build an explainability payload without touching the database."""
    answer_text = (
        plan.clarifying_question
        if plan.action == "ask_clarifying_question"
        else (plan.refusal_reason or "")
    )
    return AskResponse(
        action=plan.action,
        intent=plan.intent,
        answer=answer_text or "",
        sql=None,
        tables_used=[],
        columns_used=[],
        validation=ValidationPayload(checked=False, valid=False, errors=[]),
        dry_run=DryRunPayload(checked=False, ok=False, plan=[]),
        row_count=0,
        columns=[],
        rows=[],
        truncated=False,
        executed=False,
    )


def _safe_validation_failure_response(
    plan: SQLPlan,
    validation: ValidationResult,
    question: str,
) -> AskResponse:
    """Build a safe unexecuted response when SQL validation or dry-run fails after retry."""
    if _is_persian(question):
        message = (
            "امکان تولید کوئری امن و معتبر برای پاسخ به این پرسش فراهم نشد و هیچ کوئری روی پایگاه داده اجرا نشد."
        )
    else:
        message = (
            "I could not generate a valid, safe read-only SQL query for this question, so no query was executed."
        )
    return AskResponse(
        action=plan.action,
        intent=plan.intent,
        answer=message,
        sql=validation.sql or plan.sql,
        tables_used=validation.tables_used,
        columns_used=validation.columns_used,
        validation=ValidationPayload(
            checked=True,
            valid=validation.valid,
            errors=validation.errors,
        ),
        dry_run=DryRunPayload(
            checked=True,
            ok=validation.dry_run_ok,
            plan=validation.dry_run_plan,
        ),
        row_count=0,
        columns=[],
        rows=[],
        truncated=False,
        executed=False,
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness health check."""
    return {"status": "ok"}


@app.get("/api/teams")
def get_teams() -> dict[str, list[dict[str, Any]]]:
    """Return team English/Persian names and primary colors from the read-only DB."""
    try:
        return {"teams": get_team_colors()}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    """Run the full Text-to-SQL pipeline: Semantic -> SQL -> Validate -> Dry-run -> Execute -> Answer."""
    try:
        semantic_model = load_semantic_model()
        schema_sql = load_schema_sql()
        plan = plan_and_generate_sql(
            question=request.question,
            history=request.history,
            semantic_model=semantic_model,
            schema_sql=schema_sql,
        )
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM planning failed: {exc}") from exc

    if plan.action == "refuse_out_of_scope" and should_recheck_out_of_scope(request.question):
        try:
            plan = plan_and_generate_sql(
                question=request.question,
                history=request.history,
                semantic_model=semantic_model,
                schema_sql=schema_sql,
                scope_retry_note=(
                    "Wait: the entities in this question DO exist in the database schema! "
                    "Remember that referee_id and stadium_id live on 'match', so you MUST join event tables "
                    "(card, goal, penalty, substitution, var_decision) to 'match' on match_id and then to "
                    "'referee' or 'stadium'. Return action='answer_from_query' with the multi-hop JOIN SQL."
                ),
            )
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"LLM scope recheck failed: {exc}") from exc

    if plan.action != "answer_from_query":
        return _non_query_response(plan)

    allowlist = extract_schema_allowlist()
    validation = validate_sql(plan.sql or "", allowlist=allowlist)

    # 1. If validation/dry-run fails, allow 1 repair retry with validator feedback.
    if not (validation.valid and validation.dry_run_ok):
        try:
            retry_plan = plan_and_generate_sql(
                question=request.question,
                history=request.history,
                semantic_model=semantic_model,
                schema_sql=schema_sql,
                validation_error="; ".join(validation.errors),
                failed_sql=plan.sql,
            )
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"LLM retry failed: {exc}") from exc

        if retry_plan.action != "answer_from_query":
            return _non_query_response(retry_plan)

        plan = retry_plan
        validation = validate_sql(plan.sql or "", allowlist=allowlist)
        if not (validation.valid and validation.dry_run_ok):
            return _safe_validation_failure_response(plan, validation, request.question)

    # 2. If SQL is valid, run static quality checks (at most 1 quality repair + 1 validator repair if needed).
    quality_issues = check_sql_quality(validation.sql, question=request.question)
    if quality_issues:
        try:
            q_retry_plan = plan_and_generate_sql(
                question=request.question,
                history=request.history,
                semantic_model=semantic_model,
                schema_sql=schema_sql,
                validation_error="; ".join(quality_issues),
                failed_sql=plan.sql,
            )
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"LLM quality retry failed: {exc}") from exc

        if q_retry_plan.action != "answer_from_query":
            return _non_query_response(q_retry_plan)

        q_validation = validate_sql(q_retry_plan.sql or "", allowlist=allowlist)
        if q_validation.valid and q_validation.dry_run_ok:
            plan = q_retry_plan
            validation = q_validation
        else:
            # Quality repair introduced a syntax/schema error (e.g. missing JOIN); give 1 validator repair retry.
            try:
                fix_plan = plan_and_generate_sql(
                    question=request.question,
                    history=request.history,
                    semantic_model=semantic_model,
                    schema_sql=schema_sql,
                    validation_error="; ".join(q_validation.errors),
                    failed_sql=q_retry_plan.sql,
                )
            except LLMConfigurationError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"LLM fix retry failed: {exc}") from exc

            if fix_plan.action != "answer_from_query":
                return _non_query_response(fix_plan)

            fix_validation = validate_sql(fix_plan.sql or "", allowlist=allowlist)
            if not (fix_validation.valid and fix_validation.dry_run_ok):
                return _safe_validation_failure_response(fix_plan, fix_validation, request.question)
            plan = fix_plan
            validation = fix_validation

    try:
        exec_result = execute_query(
            validation.sql,
            allowlist=allowlist,
            validation=validation,
        )
    except SandboxError as exc:
        failed_validation = ValidationResult(
            valid=validation.valid,
            errors=[*validation.errors, str(exc)],
            tables_used=validation.tables_used,
            columns_used=validation.columns_used,
            dry_run_ok=validation.dry_run_ok,
            dry_run_plan=validation.dry_run_plan,
            sql=validation.sql,
        )
        return _safe_validation_failure_response(plan, failed_validation, request.question)

    try:
        answer_text = generate_answer(
            question=request.question,
            sql=exec_result.sql,
            columns=exec_result.columns,
            rows=exec_result.rows,
            truncated=exec_result.truncated,
            history=request.history,
        )
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM answer synthesis failed: {exc}") from exc

    return AskResponse(
        action=plan.action,
        intent=plan.intent,
        answer=answer_text,
        sql=exec_result.sql,
        tables_used=validation.tables_used,
        columns_used=validation.columns_used,
        validation=ValidationPayload(
            checked=True,
            valid=validation.valid,
            errors=validation.errors,
        ),
        dry_run=DryRunPayload(
            checked=True,
            ok=validation.dry_run_ok,
            plan=validation.dry_run_plan,
        ),
        row_count=exec_result.row_count,
        columns=exec_result.columns,
        rows=exec_result.rows,
        truncated=exec_result.truncated,
        executed=True,
    )
