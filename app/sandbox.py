"""Strictly read-only SQLite execution sandbox with timeout and row caps."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.semantic import SchemaAllowlist, open_readonly_db
from app.validator import ValidationResult, validate_sql

DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_ROWS = 200


class SandboxError(Exception):
    """Base error raised by the read-only execution sandbox."""


class SandboxValidationError(SandboxError):
    """Raised when execution is attempted without a passing validation and dry-run."""


class SandboxTimeoutError(SandboxError):
    """Raised when a query exceeds the configured execution timeout."""


@dataclass(frozen=True)
class ExecutionResult:
    """Result returned by executing a validated SELECT query in the sandbox."""

    sql: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


def execute_query(
    sql: str,
    db_path: str | Path | None = None,
    allowlist: SchemaAllowlist | None = None,
    validation: ValidationResult | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ExecutionResult:
    """Execute a validated read-only SELECT query with timeout and row limit enforcement."""
    if max_rows <= 0:
        raise ValueError("max_rows must be a positive integer.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive.")

    if validation is None:
        effective_validation = validate_sql(sql, db_path=db_path, allowlist=allowlist)
    else:
        effective_validation = validation

    if not effective_validation.valid or not effective_validation.dry_run_ok:
        detail = "; ".join(effective_validation.errors) or "Validation and dry-run must succeed before execution."
        raise SandboxValidationError(detail)

    executable_sql = effective_validation.sql or sql
    if executable_sql.strip().rstrip(";") != sql.strip().rstrip(";"):
        raise SandboxValidationError("Executed SQL does not match the validated SQL statement.")

    conn: sqlite3.Connection | None = None
    try:
        conn = open_readonly_db(db_path)
        deadline = time.monotonic() + timeout_seconds

        def _progress_handler() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(_progress_handler, 1000)
        cursor = conn.execute(executable_sql)
        columns = [desc[0] for desc in (cursor.description or ())]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        limited_rows = [list(row) for row in fetched[:max_rows]]
        return ExecutionResult(
            sql=executable_sql,
            columns=columns,
            rows=limited_rows,
            row_count=len(limited_rows),
            truncated=truncated,
        )
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise SandboxTimeoutError(
                f"Query execution exceeded the {timeout_seconds:.2f}s timeout."
            ) from exc
        raise SandboxError(f"Database query error: {exc}") from exc
    except sqlite3.Error as exc:
        raise SandboxError(f"Database error: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
            except sqlite3.Error:
                pass
            conn.close()


def get_team_colors(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return English name, Persian name, and primary_color for all teams (read-only)."""
    conn = open_readonly_db(db_path)
    try:
        rows = conn.execute(
            "SELECT team_id, name, name_fa, primary_color FROM team ORDER BY name"
        ).fetchall()
        return [
            {
                "team_id": row[0],
                "name": row[1],
                "name_fa": row[2],
                "primary_color": row[3],
            }
            for row in rows
        ]
    finally:
        conn.close()
