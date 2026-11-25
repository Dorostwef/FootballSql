"""Strict SQLite SELECT validator, schema allowlist verifier, and EXPLAIN QUERY PLAN dry-runner."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope
from sqlglot.tokens import TokenType

from app.semantic import SchemaAllowlist, extract_schema_allowlist, open_readonly_db

FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "REPLACE",
        "VACUUM",
        "REINDEX",
    }
)

_DISALLOWED_AST_NAMES: tuple[str, ...] = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Alter",
    "Create",
    "Attach",
    "Detach",
    "Pragma",
    "Command",
    "Transaction",
    "Commit",
    "Rollback",
    "Merge",
    "Grant",
    "Revoke",
    "Set",
    "Use",
    "LoadData",
    "Copy",
)
DISALLOWED_AST_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _DISALLOWED_AST_NAMES if hasattr(exp, name)
)

QUERY_ROOT_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in ("Select", "Union", "Intersect", "Except")
    if hasattr(exp, name)
)
SET_OPERATION_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in ("Union", "Intersect", "Except")
    if hasattr(exp, name)
)


@dataclass
class ValidationResult:
    """Structured result of SQL lexical/AST validation, schema checks, and dry-run."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    tables_used: list[str] = field(default_factory=list)
    columns_used: list[str] = field(default_factory=list)
    dry_run_ok: bool = False
    dry_run_plan: list[str] = field(default_factory=list)
    sql: str = ""


def _scan_comments_and_semicolons(sql: str) -> tuple[bool, list[int]]:
    """Scan SQL outside string literals and quoted identifiers for comments and semicolons."""
    has_comment = False
    semicolon_positions: list[int] = []

    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        # Single-quoted string literal ('...')
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Double-quoted identifier ("...")
        if ch == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Backtick-quoted identifier (`...`)
        if ch == "`":
            i += 1
            while i < n:
                if sql[i] == "`":
                    if i + 1 < n and sql[i + 1] == "`":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Bracket-quoted identifier ([...])
        if ch == "[":
            i += 1
            while i < n and sql[i] != "]":
                i += 1
            if i < n:
                i += 1
            continue

        # Line comment (--)
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            has_comment = True
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            continue

        # Block comment (/* ... */)
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            has_comment = True
            end = sql.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            continue

        if ch == ";":
            semicolon_positions.append(i)

        i += 1

    return has_comment, semicolon_positions


def _find_forbidden_keywords(sql: str) -> list[str]:
    """Tokenize SQL with sqlglot and detect forbidden write/admin keywords outside literals/identifiers."""
    try:
        tokens = sqlglot.tokenize(sql, read="sqlite")
    except TokenError as exc:
        return [f"TOKEN_ERROR: {exc}"]

    found: list[str] = []
    for idx, tok in enumerate(tokens):
        if tok.token_type in (TokenType.STRING, TokenType.IDENTIFIER, TokenType.COMMENT):
            continue
        upper = tok.text.upper()
        if upper not in FORBIDDEN_KEYWORDS:
            continue
        # Allow SQLite scalar string function REPLACE(str, old, new) when invoked as a function.
        if upper == "REPLACE" and idx + 1 < len(tokens) and tokens[idx + 1].token_type == TokenType.L_PAREN:
            continue
        if upper not in found:
            found.append(upper)
    return found


def _is_query_tree_safe(node: exp.Expression) -> str | None:
    """Verify the AST root and every sub-expression form a read-only SELECT query."""
    if not isinstance(node, QUERY_ROOT_NODES):
        return f"Only SELECT queries are allowed; found {type(node).__name__}."

    for sub in node.walk():
        if isinstance(sub, DISALLOWED_AST_NODES):
            return f"Forbidden SQL operation in AST: {type(sub).__name__}."
        if isinstance(sub, exp.Select) and sub.args.get("into") is not None:
            return "SELECT INTO is not allowed."
        if isinstance(sub, SET_OPERATION_NODES):
            if not isinstance(sub.left, (QUERY_ROOT_NODES + (exp.Subquery,))) or not isinstance(
                sub.right, (QUERY_ROOT_NODES + (exp.Subquery,))
            ):
                return f"Unsupported set-operation branch inside {type(sub).__name__}."
    return None


def _collect_scope_projected_columns(
    scope: Scope,
    table_aliases: dict[str, str],
    scope_projections: dict[int, tuple[frozenset[str], bool]],
    allowlist: SchemaAllowlist,
) -> tuple[frozenset[str], bool]:
    """Compute the lowercase column names projected by a CTE, derived table, or set-op scope."""
    expr = scope.expression

    # Explicit CTE column header: WITH cte_name(col1, col2) AS (...)
    if scope.scope_type == ScopeType.CTE and isinstance(expr.parent, exp.CTE):
        alias_cols = expr.parent.alias_column_names
        if alias_cols:
            return frozenset(col.lower() for col in alias_cols), False

    if isinstance(expr, SET_OPERATION_NODES):
        set_scopes = getattr(scope, "set_operation_scopes", None) or ()
        if set_scopes:
            return scope_projections.get(id(set_scopes[0]), (frozenset(), True))
        return frozenset(), True

    if not isinstance(expr, exp.Select):
        return frozenset(), True

    projected: set[str] = set()
    has_unknown_star = False

    for item in expr.expressions:
        if isinstance(item, exp.Alias):
            if item.alias:
                projected.add(item.alias.lower())
        elif isinstance(item, exp.Star):
            expanded = _expand_star(None, scope, table_aliases, scope_projections, allowlist)
            if expanded is None:
                has_unknown_star = True
            else:
                projected.update(expanded)
        elif isinstance(item, exp.Column):
            if isinstance(item.this, exp.Star):
                tbl = item.table.lower() if item.table else None
                expanded = _expand_star(tbl, scope, table_aliases, scope_projections, allowlist)
                if expanded is None:
                    has_unknown_star = True
                else:
                    projected.update(expanded)
            elif item.name:
                projected.add(item.name.lower())
        else:
            output_name = item.output_name
            if output_name:
                projected.add(output_name.lower())

    return frozenset(projected), has_unknown_star


def _expand_star(
    qualifier: str | None,
    scope: Scope,
    table_aliases: dict[str, str],
    scope_projections: dict[int, tuple[frozenset[str], bool]],
    allowlist: SchemaAllowlist,
) -> set[str] | None:
    """Expand `*` or `alias.*` against known tables and child scopes in this scope."""
    cols: set[str] = set()
    if qualifier is not None:
        if qualifier in table_aliases:
            rel = table_aliases[qualifier]
            cols.update(c.lower() for c in allowlist.columns_by_relation.get(rel, ()))
            return cols
        for src_name, src_val in scope.sources.items():
            if src_name.lower() == qualifier and isinstance(src_val, Scope):
                child_cols, has_unk = scope_projections.get(id(src_val), (frozenset(), True))
                if has_unk:
                    return None
                cols.update(child_cols)
                return cols
        return None

    for rel in table_aliases.values():
        cols.update(c.lower() for c in allowlist.columns_by_relation.get(rel, ()))
    for src_val in scope.sources.values():
        if isinstance(src_val, Scope):
            child_cols, has_unk = scope_projections.get(id(src_val), (frozenset(), True))
            if has_unk:
                return None
            cols.update(child_cols)
    return cols


def _scope_select_aliases(scope: Scope) -> set[str]:
    """Return lowercase output aliases defined in the SELECT list of this scope."""
    aliases: set[str] = set()
    if isinstance(scope.expression, exp.Select):
        for item in scope.expression.expressions:
            if isinstance(item, exp.Alias) and item.alias:
                aliases.add(item.alias.lower())
    return aliases


def _is_output_clause_reference(col: exp.Column, scope: Scope) -> bool:
    """Return True if an unqualified column sits in ORDER BY, GROUP BY, or HAVING of this scope."""
    if col.table:
        return False
    parent = col.parent
    while parent is not None and parent is not scope.expression:
        if isinstance(parent, (exp.Order, exp.Group, exp.Having)):
            return True
        if isinstance(parent, (exp.Select, exp.Subquery, exp.CTE, exp.Where, exp.Join)):
            return False
        parent = parent.parent
    return False


def _resolve_column_in_scope(
    col: exp.Column,
    scope: Scope,
    scope_tables: dict[int, dict[str, str]],
    scope_projections: dict[int, tuple[frozenset[str], bool]],
    allowlist: SchemaAllowlist,
    allow_parent_lookup: bool = True,
) -> tuple[bool, str | None, str | None]:
    """Resolve a column reference against the current scope (and parent scopes if correlated).

    Returns (found, qualified_real_column_or_None, error_message_or_None).
    """
    col_name = col.name
    if not col_name or isinstance(col.this, exp.Star):
        # For `alias.*`, verify `alias` exists in scope.
        if col.table:
            tbl_key = col.table.lower()
            cur: Scope | None = scope
            while cur is not None:
                aliases = scope_tables.get(id(cur), {})
                if tbl_key in aliases or any(k.lower() == tbl_key for k in cur.sources):
                    return True, None, None
                if not allow_parent_lookup or cur.scope_type != ScopeType.SUBQUERY:
                    break
                cur = cur.parent
            return False, None, f"Unknown table or alias '{col.table}' in '{col.table}.*'."
        return True, None, None

    cur_scope: Scope | None = scope
    while cur_scope is not None:
        table_aliases = scope_tables.get(id(cur_scope), {})
        child_scopes = {
            name.lower(): src
            for name, src in cur_scope.sources.items()
            if isinstance(src, Scope)
        }

        if col.table:
            tbl_key = col.table.lower()
            if tbl_key in table_aliases:
                real_rel = table_aliases[tbl_key]
                real_col = allowlist.resolve_column(real_rel, col_name)
                if real_col is not None:
                    return True, f"{real_rel}.{real_col}", None
                return (
                    False,
                    None,
                    f"Column '{col_name}' does not exist in table/view '{real_rel}'.",
                )
            if tbl_key in child_scopes:
                proj_cols, has_unk = scope_projections.get(id(child_scopes[tbl_key]), (frozenset(), True))
                if col_name.lower() in proj_cols or has_unk:
                    return True, None, None
                return (
                    False,
                    None,
                    f"Column '{col_name}' does not exist in CTE/subquery '{col.table}'.",
                )
        else:
            # Check output aliases first when used in ORDER BY / GROUP BY / HAVING.
            if _is_output_clause_reference(col, cur_scope) and col_name.lower() in _scope_select_aliases(cur_scope):
                return True, None, None

            # Check real tables/views in this scope.
            matches: list[tuple[str, str]] = []
            for real_rel in dict.fromkeys(table_aliases.values()):
                real_col = allowlist.resolve_column(real_rel, col_name)
                if real_col is not None:
                    matches.append((real_rel, real_col))

            if len(matches) == 1:
                return True, f"{matches[0][0]}.{matches[0][1]}", None
            if len(matches) > 1:
                # Ambiguous across multiple tables in scope; let SQLite dry-run report ambiguity if invalid.
                return True, col_name.lower(), None

            # Check CTE / derived-table sources in this scope.
            for child_sc in child_scopes.values():
                proj_cols, has_unk = scope_projections.get(id(child_sc), (frozenset(), True))
                if col_name.lower() in proj_cols or has_unk:
                    return True, None, None

        # Only scalar/WHERE/EXISTS subqueries can correlate to outer scopes.
        if not allow_parent_lookup or cur_scope.scope_type != ScopeType.SUBQUERY:
            break
        cur_scope = cur_scope.parent

    if col.table:
        return False, None, f"Unknown table or alias '{col.table}' for column '{col.table}.{col_name}'."
    refs = sorted(set(scope_tables.get(id(scope), {}).values()))
    if refs:
        return False, None, f"Column '{col_name}' does not exist in referenced table(s): {', '.join(refs)}."
    return False, None, f"Column '{col_name}' does not exist in the current query scope."


def _extract_and_verify_schema_usage(
    tree: exp.Expression,
    allowlist: SchemaAllowlist,
) -> tuple[list[str], list[str], list[str]]:
    """Verify every referenced table/view/column exists and return (tables_used, columns_used, errors)."""
    errors: list[str] = []
    tables_used: list[str] = []
    columns_used: list[str] = []

    root_scope = build_scope(tree)
    if root_scope is None:
        return [], [], ["Unable to analyze query scope."]

    scopes = list(root_scope.traverse())
    scope_tables: dict[int, dict[str, str]] = {}
    scope_projections: dict[int, tuple[frozenset[str], bool]] = {}
    checked_col_ids: set[int] = set()

    # Pass 1 (bottom-up): validate direct Table sources and build projections for every scope.
    for scope in scopes:
        table_aliases: dict[str, str] = {}

        for alias_name, source_val in scope.sources.items():
            if not isinstance(source_val, exp.Table):
                continue

            if source_val.db or source_val.catalog:
                errors.append(
                    f"Cross-database or schema-qualified table reference '{source_val.sql(dialect='sqlite')}' is not allowed."
                )
                continue

            if not isinstance(source_val.this, exp.Identifier):
                errors.append(
                    f"Table-valued functions or non-identifier sources are not allowed: '{source_val.sql(dialect='sqlite')}'."
                )
                continue

            raw_table = source_val.name
            canonical_rel = allowlist.resolve_relation(raw_table)
            if canonical_rel is None:
                errors.append(f"Table or view '{raw_table}' does not exist in the schema allowlist.")
                continue

            alias_key = (alias_name or raw_table).lower()
            table_aliases[alias_key] = canonical_rel
            if canonical_rel not in tables_used:
                tables_used.append(canonical_rel)

        scope_tables[id(scope)] = table_aliases
        scope_projections[id(scope)] = _collect_scope_projected_columns(
            scope, table_aliases, scope_projections, allowlist
        )

    # Pass 2: validate columns from their innermost owning scope (correlated lookups see parent scopes).
    for scope in scopes:
        for col in scope.columns:
            if id(col) in checked_col_ids:
                continue
            checked_col_ids.add(id(col))

            found, qualified_col, err = _resolve_column_in_scope(
                col,
                scope,
                scope_tables,
                scope_projections,
                allowlist,
                allow_parent_lookup=(scope.scope_type == ScopeType.SUBQUERY),
            )
            if found:
                if qualified_col and qualified_col not in columns_used:
                    columns_used.append(qualified_col)
            else:
                if err and err not in errors:
                    errors.append(err)

    return tables_used, columns_used, errors


def _run_dry_run(cleaned_sql: str, db_path: str | Path | None = None) -> tuple[bool, list[str], list[str]]:
    """Run EXPLAIN QUERY PLAN on a strictly read-only SQLite connection."""
    conn: sqlite3.Connection | None = None
    try:
        conn = open_readonly_db(db_path)
        rows = conn.execute(f"EXPLAIN QUERY PLAN {cleaned_sql}").fetchall()
        plan = [f"[{row[0]}->{row[1]}] {row[3]}" if len(row) >= 4 else str(row) for row in rows]
        return True, plan, []
    except (sqlite3.Error, FileNotFoundError) as exc:
        return False, [], [f"SQLite dry-run failed: {exc}"]
    finally:
        if conn is not None:
            conn.close()


def validate_sql(
    sql: str,
    db_path: str | Path | None = None,
    allowlist: SchemaAllowlist | None = None,
    schema_path: str | Path | None = None,
) -> ValidationResult:
    """Validate SQL safety, syntax, schema allowlist compliance, and EXPLAIN QUERY PLAN dry-run."""
    if not isinstance(sql, str) or not sql.strip():
        return ValidationResult(valid=False, errors=["SQL query must not be empty."])

    has_comment, semicolons = _scan_comments_and_semicolons(sql)
    if has_comment:
        return ValidationResult(
            valid=False,
            errors=["SQL comments (-- or /* */) are not allowed."],
            sql=sql,
        )

    if len(semicolons) > 1:
        return ValidationResult(
            valid=False,
            errors=["Multiple SQL statements (;) are not allowed."],
            sql=sql,
        )

    cleaned_sql = sql.strip()
    if len(semicolons) == 1:
        semi_idx = semicolons[0]
        if sql[semi_idx + 1 :].strip():
            return ValidationResult(
                valid=False,
                errors=["Multiple SQL statements (;) are not allowed."],
                sql=sql,
            )
        cleaned_sql = sql[:semi_idx].strip()
        if not cleaned_sql:
            return ValidationResult(
                valid=False,
                errors=["SQL query must not be empty."],
                sql=sql,
            )

    forbidden = _find_forbidden_keywords(cleaned_sql)
    if forbidden:
        return ValidationResult(
            valid=False,
            errors=[f"Forbidden SQL keyword(s) detected: {', '.join(forbidden)}."],
            sql=cleaned_sql,
        )

    try:
        statements = sqlglot.parse(cleaned_sql, read="sqlite")
    except (ParseError, TokenError) as exc:
        return ValidationResult(
            valid=False,
            errors=[f"SQL syntax parse error: {exc}"],
            sql=cleaned_sql,
        )

    non_empty = [stmt for stmt in statements if stmt is not None]
    if len(non_empty) != 1:
        return ValidationResult(
            valid=False,
            errors=["Exactly one SQL SELECT statement is required."],
            sql=cleaned_sql,
        )

    ast_root = non_empty[0]
    ast_err = _is_query_tree_safe(ast_root)
    if ast_err is not None:
        return ValidationResult(valid=False, errors=[ast_err], sql=cleaned_sql)

    resolved_allowlist = allowlist or extract_schema_allowlist(db_path=db_path, schema_path=schema_path)
    tables_used, columns_used, schema_errors = _extract_and_verify_schema_usage(
        ast_root, resolved_allowlist
    )
    if schema_errors:
        return ValidationResult(
            valid=False,
            errors=schema_errors,
            tables_used=tables_used,
            columns_used=columns_used,
            sql=cleaned_sql,
        )

    dry_ok, dry_plan, dry_errors = _run_dry_run(cleaned_sql, db_path=db_path)
    if not dry_ok:
        return ValidationResult(
            valid=False,
            errors=dry_errors,
            tables_used=tables_used,
            columns_used=columns_used,
            dry_run_ok=False,
            dry_run_plan=[],
            sql=cleaned_sql,
        )

    return ValidationResult(
        valid=True,
        errors=[],
        tables_used=tables_used,
        columns_used=columns_used,
        dry_run_ok=True,
        dry_run_plan=dry_plan,
        sql=cleaned_sql,
    )
