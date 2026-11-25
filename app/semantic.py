"""Dynamic loader for the semantic model, SQL schema, and DB schema allowlist."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_path(env_key: str, default_relative: str, override: str | Path | None = None) -> Path:
    """Resolve a configured file path from an override, environment variable, or project root."""
    load_dotenv(override=False)
    raw = str(override) if override is not None else (os.getenv(env_key) or default_relative)
    candidate = Path(raw)
    if not candidate.is_absolute() and not candidate.exists():
        root_candidate = PROJECT_ROOT / candidate
        if root_candidate.exists():
            return root_candidate
    return candidate


def get_db_path(db_path: str | Path | None = None) -> Path:
    """Return the configured path to the SQLite database."""
    return _resolve_path("DB_PATH", "data/football.db", db_path)


def get_semantic_model_path(semantic_model_path: str | Path | None = None) -> Path:
    """Return the configured path to semantic_model.md."""
    return _resolve_path("SEMANTIC_MODEL_PATH", "data/semantic_model.md", semantic_model_path)


def get_schema_path(schema_path: str | Path | None = None) -> Path:
    """Return the configured path to schema.sql."""
    return _resolve_path("SCHEMA_PATH", "data/schema.sql", schema_path)


def load_semantic_model(semantic_model_path: str | Path | None = None) -> str:
    """Read semantic_model.md dynamically from disk."""
    path = get_semantic_model_path(semantic_model_path)
    return path.read_text(encoding="utf-8")


def load_schema_sql(schema_path: str | Path | None = None) -> str:
    """Read schema.sql dynamically from disk."""
    path = get_schema_path(schema_path)
    return path.read_text(encoding="utf-8")


def open_readonly_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open the SQLite database strictly in read-only URI mode."""
    path = get_db_path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found at: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


@dataclass(frozen=True)
class SchemaAllowlist:
    """Authoritative schema allowlist of real SQLite tables, views, and columns."""

    tables: frozenset[str]
    views: frozenset[str]
    columns_by_relation: dict[str, frozenset[str]]
    ordered_columns: dict[str, tuple[str, ...]]
    _relation_lookup: dict[str, str] = field(init=False, repr=False)
    _column_lookup: dict[str, dict[str, str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        rel_map = {name.lower(): name for name in (self.tables | self.views)}
        col_map = {
            rel.lower(): {col.lower(): col for col in cols}
            for rel, cols in self.columns_by_relation.items()
        }
        object.__setattr__(self, "_relation_lookup", rel_map)
        object.__setattr__(self, "_column_lookup", col_map)

    @property
    def relations(self) -> frozenset[str]:
        """All allowed real tables and views."""
        return self.tables | self.views

    @property
    def all_columns(self) -> frozenset[str]:
        """Union of all column names across all allowed tables and views."""
        merged: set[str] = set()
        for cols in self.columns_by_relation.values():
            merged.update(cols)
        return frozenset(merged)

    def resolve_relation(self, name: str) -> str | None:
        """Return canonical relation name if allowed, else None (case-insensitive)."""
        return self._relation_lookup.get(name.lower())

    def resolve_column(self, relation: str, column: str) -> str | None:
        """Return canonical column name if it belongs to relation, else None."""
        rel_cols = self._column_lookup.get(relation.lower())
        if not rel_cols:
            return None
        return rel_cols.get(column.lower())


def _extract_allowlist_from_connection(conn: sqlite3.Connection) -> SchemaAllowlist:
    """Inspect sqlite_master and PRAGMA table_info on an open SQLite connection."""
    cursor = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    rows = cursor.fetchall()

    tables: set[str] = set()
    views: set[str] = set()
    columns_by_relation: dict[str, frozenset[str]] = {}
    ordered_columns: dict[str, tuple[str, ...]] = {}

    for name, rel_type in rows:
        if rel_type == "table":
            tables.add(name)
        elif rel_type == "view":
            views.add(name)

        escaped = name.replace('"', '""')
        col_rows = conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()
        col_names = tuple(col_row[1] for col_row in col_rows)
        ordered_columns[name] = col_names
        columns_by_relation[name] = frozenset(col_names)

    return SchemaAllowlist(
        tables=frozenset(tables),
        views=frozenset(views),
        columns_by_relation=columns_by_relation,
        ordered_columns=ordered_columns,
    )


def extract_allowlist_from_schema_sql(schema_sql: str) -> SchemaAllowlist:
    """Build a SchemaAllowlist by loading DDL statements into an isolated in-memory DB."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_sql)
        return _extract_allowlist_from_connection(conn)
    finally:
        conn.close()


def extract_schema_allowlist(
    db_path: str | Path | None = None,
    schema_path: str | Path | None = None,
) -> SchemaAllowlist:
    """Extract tables, views, and columns from sqlite_master + PRAGMA table_info on a read-only DB.

    Falls back to parsing schema.sql in an in-memory SQLite instance only when db_path
    is not explicitly supplied and the default DB file is absent.
    """
    resolved_db = get_db_path(db_path)
    if resolved_db.is_file():
        conn = open_readonly_db(resolved_db)
        try:
            return _extract_allowlist_from_connection(conn)
        finally:
            conn.close()

    if db_path is not None:
        raise FileNotFoundError(f"SQLite database not found at: {resolved_db}")

    schema_sql = load_schema_sql(schema_path)
    return extract_allowlist_from_schema_sql(schema_sql)
