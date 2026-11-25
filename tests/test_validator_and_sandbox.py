"""Unit tests for semantic loading, SQL validation, dry-run, read-only sandbox, and FastAPI endpoints."""

from __future__ import annotations

from types import SimpleNamespace
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.llm import SQLPlan, generate_answer, plan_and_generate_sql
from app.main import app
from app.sandbox import (
    SandboxTimeoutError,
    SandboxValidationError,
    execute_query,
    get_team_colors,
)
from app.semantic import (
    extract_schema_allowlist,
    load_schema_sql,
    load_semantic_model,
    open_readonly_db,
)
from app.validator import ValidationResult, validate_sql


class TestSemanticAndValidator(unittest.TestCase):
    """Tests for schema extraction and strict read-only SQL validation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.allowlist = extract_schema_allowlist()

    def test_dynamic_semantic_and_schema_loading(self) -> None:
        semantic_text = load_semantic_model()
        schema_text = load_schema_sql()
        self.assertIn("Football DB", semantic_text)
        self.assertIn("CREATE VIEW v_standings", schema_text)
        self.assertIn("team", self.allowlist.tables)
        self.assertIn("match", self.allowlist.tables)
        self.assertIn("v_standings", self.allowlist.views)
        self.assertIn("v_team_match", self.allowlist.views)
        self.assertIn("v_match", self.allowlist.views)
        self.assertIn("v_player_minutes", self.allowlist.views)
        self.assertIn("points", self.allowlist.columns_by_relation["v_standings"])

    def test_valid_select_cte_union_and_aliases(self) -> None:
        sql_standings = (
            "SELECT team, points, goal_diff "
            "FROM v_standings "
            "ORDER BY points DESC, goal_diff DESC, goals_for DESC"
        )
        res = validate_sql(sql_standings, allowlist=self.allowlist)
        self.assertTrue(res.valid, res.errors)
        self.assertTrue(res.dry_run_ok)
        self.assertTrue(len(res.dry_run_plan) > 0)
        self.assertEqual(res.tables_used, ["v_standings"])
        self.assertIn("v_standings.team", res.columns_used)
        self.assertIn("v_standings.points", res.columns_used)

        sql_cte = (
            "WITH top_scorers AS ("
            "  SELECT g.player_id, COUNT(*) AS goals_scored "
            "  FROM goal g "
            "  WHERE g.is_own_goal = 0 "
            "  GROUP BY g.player_id"
            ") "
            "SELECT p.name_fa, ts.goals_scored "
            "FROM top_scorers ts "
            "JOIN player p ON p.player_id = ts.player_id "
            "ORDER BY goals_scored DESC LIMIT 5;"
        )
        res_cte = validate_sql(sql_cte, allowlist=self.allowlist)
        self.assertTrue(res_cte.valid, res_cte.errors)
        self.assertTrue(res_cte.dry_run_ok)
        self.assertNotIn("top_scorers", res_cte.tables_used)
        self.assertNotIn("ts", res_cte.tables_used)
        self.assertEqual(set(res_cte.tables_used), {"goal", "player"})
        self.assertNotIn("goals_scored", " ".join(res_cte.columns_used))

        sql_explicit_cte = (
            "WITH team_names(team_title) AS (SELECT name FROM team) "
            "SELECT team_title FROM team_names ORDER BY team_title"
        )
        res_explicit = validate_sql(sql_explicit_cte, allowlist=self.allowlist)
        self.assertTrue(res_explicit.valid, res_explicit.errors)
        self.assertEqual(res_explicit.tables_used, ["team"])
        self.assertEqual(res_explicit.columns_used, ["team.name"])

        sql_union = (
            "SELECT home_team_id AS tid FROM match "
            "UNION ALL "
            "SELECT away_team_id AS tid FROM match"
        )
        res_union = validate_sql(sql_union, allowlist=self.allowlist)
        self.assertTrue(res_union.valid, res_union.errors)
        self.assertEqual(res_union.tables_used, ["match"])
        self.assertEqual(
            set(res_union.columns_used),
            {"match.home_team_id", "match.away_team_id"},
        )

        sql_cte_union = (
            "WITH all_sides AS ("
            "  SELECT home_team_id AS tid FROM match "
            "  UNION ALL "
            "  SELECT away_team_id AS tid FROM match"
            "), "
            "counts AS ("
            "  SELECT tid, COUNT(*) AS appearances FROM all_sides GROUP BY tid"
            ") "
            "SELECT t.name, c.appearances "
            "FROM counts c JOIN team t ON t.team_id = c.tid "
            "ORDER BY appearances DESC"
        )
        res_cte_union = validate_sql(sql_cte_union, allowlist=self.allowlist)
        self.assertTrue(res_cte_union.valid, res_cte_union.errors)
        self.assertEqual(set(res_cte_union.tables_used), {"match", "team"})

        sql_correlated = (
            "SELECT m.match_id FROM match m "
            "WHERE EXISTS (SELECT 1 FROM goal g WHERE g.match_id = m.match_id)"
        )
        res_corr = validate_sql(sql_correlated, allowlist=self.allowlist)
        self.assertTrue(res_corr.valid, res_corr.errors)

        sql_literal_keyword = "SELECT name FROM team WHERE name = 'DELETE'"
        res_lit = validate_sql(sql_literal_keyword, allowlist=self.allowlist)
        self.assertTrue(res_lit.valid, res_lit.errors)

    def test_empty_and_comment_smuggling_rejected(self) -> None:
        bad_queries = [
            "",
            "   \n\t  ",
            "-- just a comment",
            "/* block comment only */",
            "SELECT name FROM team -- ; DROP TABLE team",
            "SELECT /* ; DELETE FROM team */ name FROM team",
            "/* header comment */ SELECT name FROM team",
            "SELECT name FROM team /* unclosed",
        ]
        for query in bad_queries:
            res = validate_sql(query, allowlist=self.allowlist)
            self.assertFalse(res.valid, f"Expected failure for: {query!r}")
            self.assertFalse(res.dry_run_ok)

    def test_multi_statements_rejected(self) -> None:
        bad_queries = [
            "SELECT name FROM team; SELECT match_id FROM match",
            "SELECT name FROM team; DROP TABLE team",
            "SELECT name FROM team;;",
            "; SELECT name FROM team",
            "SELECT 1; INSERT INTO team (team_id, name, name_fa) VALUES (999, 'X', 'Y')",
        ]
        for query in bad_queries:
            res = validate_sql(query, allowlist=self.allowlist)
            self.assertFalse(res.valid, f"Expected multi-statement failure for: {query!r}")
            self.assertFalse(res.dry_run_ok)

    def test_all_forbidden_write_and_admin_keywords_rejected(self) -> None:
        bad_queries = [
            "INSERT INTO team (team_id, name, name_fa) VALUES (999, 'Test', 'تست')",
            "UPDATE team SET name = 'Hacked' WHERE team_id = 1",
            "DELETE FROM team WHERE team_id = 1",
            "DROP TABLE team",
            "ALTER TABLE team ADD COLUMN extra TEXT",
            "CREATE TABLE evil (id INTEGER PRIMARY KEY)",
            "ATTACH DATABASE ':memory:' AS evil",
            "DETACH DATABASE main",
            "PRAGMA table_info(team)",
            "REPLACE INTO team (team_id, name, name_fa) VALUES (1, 'X', 'Y')",
            "VACUUM",
            "REINDEX",
            "WITH cte AS (SELECT 1) DELETE FROM team",
            "WITH cte AS (SELECT 1) UPDATE team SET name = 'X'",
            "SELECT * FROM pragma_table_info('team')",
        ]
        for query in bad_queries:
            res = validate_sql(query, allowlist=self.allowlist)
            self.assertFalse(res.valid, f"Expected write/admin failure for: {query!r}")
            self.assertFalse(res.dry_run_ok)

    def test_nonexistent_tables_and_columns_rejected(self) -> None:
        bad_queries = [
            "SELECT * FROM nonexistent_table",
            "SELECT name FROM sqlite_master",
            "SELECT * FROM main.team",
            "SELECT bogus_column FROM team",
            "SELECT t.home_goals FROM team t",
            "SELECT unknown_alias.name FROM team t",
            "WITH c AS (SELECT fake_col FROM team) SELECT * FROM c",
            "SELECT name FROM team WHERE team_id IN (SELECT bogus_col FROM match)",
            "SELECT t.name, COUNT(*) AS cnt FROM team t ORDER BY nonexistent_alias DESC",
        ]
        for query in bad_queries:
            res = validate_sql(query, allowlist=self.allowlist)
            self.assertFalse(res.valid, f"Expected schema failure for: {query!r}")
            self.assertFalse(res.dry_run_ok)


class TestSandboxAndReadOnlyDB(unittest.TestCase):
    """Tests for sandbox validation gating, limits, timeout, and direct DB write refusal."""

    def test_sandbox_refuses_unvalidated_or_failed_queries(self) -> None:
        with self.assertRaises(SandboxValidationError):
            execute_query("DROP TABLE team")

        with self.assertRaises(SandboxValidationError):
            execute_query("SELECT nonexistent_col FROM team")

        failed_val = ValidationResult(
            valid=False,
            errors=["Rejected"],
            dry_run_ok=False,
            sql="SELECT name FROM team",
        )
        with self.assertRaises(SandboxValidationError):
            execute_query("SELECT name FROM team", validation=failed_val)

        mismatched_val = ValidationResult(
            valid=True,
            errors=[],
            dry_run_ok=True,
            sql="SELECT name FROM team",
        )
        with self.assertRaises(SandboxValidationError):
            execute_query("DELETE FROM team", validation=mismatched_val)

    def test_sandbox_executes_valid_query_and_enforces_row_limit(self) -> None:
        res = execute_query("SELECT team, points FROM v_standings ORDER BY points DESC")
        self.assertEqual(res.columns, ["team", "points"])
        self.assertEqual(res.row_count, 18)
        self.assertFalse(res.truncated)

        capped = execute_query("SELECT name FROM team ORDER BY name", max_rows=5)
        self.assertEqual(capped.row_count, 5)
        self.assertTrue(capped.truncated)

        default_capped = execute_query("SELECT match_id, player_id FROM lineup")
        self.assertEqual(default_capped.row_count, 200)
        self.assertTrue(default_capped.truncated)

    def test_sandbox_enforces_timeout(self) -> None:
        slow_query = (
            "WITH RECURSIVE r(x) AS ("
            "  SELECT 1 UNION ALL SELECT x + 1 FROM r WHERE x < 50000000"
            ") SELECT COUNT(*) AS total FROM r"
        )
        with self.assertRaises(SandboxTimeoutError):
            execute_query(slow_query, timeout_seconds=0.05)

    def test_direct_write_attempts_on_readonly_db_fail(self) -> None:
        conn = open_readonly_db()
        try:
            write_statements = [
                "INSERT INTO team (team_id, name, name_fa) VALUES (9999, 'Evil FC', 'ایول')",
                "UPDATE team SET name = 'Evil FC' WHERE team_id = 1",
                "DELETE FROM team WHERE team_id = 1",
                "CREATE TABLE evil (id INTEGER PRIMARY KEY)",
                "DROP TABLE team",
            ]
            for stmt in write_statements:
                with self.assertRaises(sqlite3.OperationalError, msg=f"Write succeeded unexpectedly: {stmt}"):
                    conn.execute(stmt)
        finally:
            conn.close()

    def test_get_team_colors_readonly(self) -> None:
        teams = get_team_colors()
        self.assertEqual(len(teams), 18)
        self.assertIn("name", teams[0])
        self.assertIn("name_fa", teams[0])
        self.assertIn("primary_color", teams[0])


class TestFastAPIEndpoints(unittest.TestCase):
    """Integration tests for FastAPI routes with mocked LLM responses."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_and_teams_endpoints(self) -> None:
        health_resp = self.client.get("/health")
        self.assertEqual(health_resp.status_code, 200)
        self.assertEqual(health_resp.json(), {"status": "ok"})

        teams_resp = self.client.get("/api/teams")
        self.assertEqual(teams_resp.status_code, 200)
        teams_data = teams_resp.json()["teams"]
        self.assertEqual(len(teams_data), 18)

    def test_non_query_actions_do_not_touch_database(self) -> None:
        scenarios = [
            (
                "Delete all matches from the database",
                SQLPlan(
                    action="refuse_write",
                    intent="Request to delete match records",
                    refusal_reason="I only have read-only access and cannot modify or delete data.",
                ),
            ),
            (
                "How many fans attended the derby?",
                SQLPlan(
                    action="refuse_out_of_scope",
                    intent="Ask for stadium attendance figures",
                    refusal_reason="Attendance counts are not available in this database.",
                ),
            ),
            (
                "Who is the best player?",
                SQLPlan(
                    action="ask_clarifying_question",
                    intent="Ambiguous request for top player",
                    clarifying_question="Do you mean most goals scored, most minutes played, or something else?",
                ),
            ),
        ]

        for question, fake_plan in scenarios:
            with (
                patch("app.main.plan_and_generate_sql", return_value=fake_plan),
                patch("app.main.extract_schema_allowlist") as mock_allowlist,
                patch("app.main.execute_query") as mock_execute,
            ):
                resp = self.client.post("/api/ask", json={"question": question, "history": []})
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["action"], fake_plan.action)
                self.assertIsNone(body["sql"])
                self.assertEqual(body["row_count"], 0)
                self.assertFalse(body["executed"])
                mock_allowlist.assert_not_called()
                mock_execute.assert_not_called()

    def test_answer_from_query_and_self_correction_retry(self) -> None:
        bad_plan = SQLPlan(
            action="answer_from_query",
            intent="Get league leader",
            sql="SELECT nonexistent_col FROM v_standings LIMIT 1",
        )
        good_plan = SQLPlan(
            action="answer_from_query",
            intent="Get league leader",
            sql="SELECT team, points FROM v_standings ORDER BY points DESC, goal_diff DESC LIMIT 1",
        )

        with (
            patch("app.main.plan_and_generate_sql", side_effect=[bad_plan, good_plan]) as mock_plan,
            patch("app.main.generate_answer", return_value="Top team returned from query.") as mock_ans,
        ):
            resp = self.client.post(
                "/api/ask",
                json={"question": "Which team leads the standings?", "history": []},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(mock_plan.call_count, 2)
            self.assertEqual(mock_ans.call_count, 1)
            self.assertTrue(body["executed"])
            self.assertTrue(body["validation"]["valid"])
            self.assertTrue(body["dry_run"]["ok"])
            self.assertEqual(body["row_count"], 1)
            self.assertEqual(body["columns"], ["team", "points"])
            self.assertEqual(body["answer"], "Top team returned from query.")

    def test_double_validation_failure_returns_safe_error_without_executing(self) -> None:
        bad_plan_1 = SQLPlan(
            action="answer_from_query",
            intent="Invalid query",
            sql="SELECT nope FROM team",
        )
        bad_plan_2 = SQLPlan(
            action="answer_from_query",
            intent="Still invalid query",
            sql="DROP TABLE team",
        )

        with (
            patch("app.main.plan_and_generate_sql", side_effect=[bad_plan_1, bad_plan_2]) as mock_plan,
            patch("app.main.execute_query") as mock_execute,
        ):
            resp = self.client.post(
                "/api/ask",
                json={"question": "Show something", "history": []},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(mock_plan.call_count, 2)
            mock_execute.assert_not_called()
            self.assertFalse(body["executed"])
            self.assertFalse(body["validation"]["valid"])
            self.assertTrue(len(body["validation"]["errors"]) > 0)


class TestLLMContracts(unittest.TestCase):
    """Tests for strict 4-action LLM contracts and dynamic semantic loading (mocked client)."""

    def test_sql_plan_enforces_4_actions_and_fields(self) -> None:
        with self.assertRaises(Exception):
            SQLPlan(action="invalid_action", intent="bad")  # type: ignore[arg-type]
        with self.assertRaises(Exception):
            SQLPlan(action="answer_from_query", intent="missing sql", sql=None)
        with self.assertRaises(Exception):
            SQLPlan(action="ask_clarifying_question", intent="missing q", clarifying_question="")
        with self.assertRaises(Exception):
            SQLPlan(action="refuse_write", intent="missing reason", refusal_reason=None)

    def test_plan_and_generate_sql_loads_semantic_and_calls_json_mode(self) -> None:
        captured: dict[str, object] = {}

        class FakeCompletions:
            def create(self, **kwargs):  # type: ignore[no-untyped-def]
                captured.update(kwargs)
                payload = {
                    "action": "answer_from_query",
                    "intent": "List teams",
                    "sql": "SELECT name FROM team ORDER BY name",
                    "clarifying_question": None,
                    "refusal_reason": None,
                }
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
                )

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        plan = plan_and_generate_sql(
            question="List all teams",
            history=[{"question": "Hi", "answer": "Hello"}],
            client=fake_client,
            model="fake-model",
        )
        self.assertEqual(plan.action, "answer_from_query")
        self.assertEqual(plan.sql, "SELECT name FROM team ORDER BY name")
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        system_msg = captured["messages"][0]["content"]  # type: ignore[index]
        self.assertIn("Football DB", system_msg)
        self.assertIn("CREATE VIEW v_standings", system_msg)

    def test_generate_answer_receives_exact_rows(self) -> None:
        captured: dict[str, object] = {}

        class FakeCompletions:
            def create(self, **kwargs):  # type: ignore[no-untyped-def]
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="No matching data found."))]
                )

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        answer = generate_answer(
            question="Who scored 99 goals?",
            sql="SELECT p.name_fa FROM player p WHERE 1=0",
            columns=["name_fa"],
            rows=[],
            truncated=False,
            client=fake_client,
            model="fake-model",
        )
        self.assertIn("No matching data", answer)
        user_msg = captured["messages"][-1]["content"]  # type: ignore[index]
        self.assertIn('"row_count": 0', user_msg)


if __name__ == "__main__":
    unittest.main()
