---
trigger: always_on
description: Strict workspace rules for the FootballSql text-to-SQL assignment
---

# Assignment Strict Rules

1. **No Eval / Source Leakage**: NEVER read, inspect, reference, or use anything inside `eval/` or `source/` (especially `eval/eval_questions.json` and `source/make_evals.py`). Gold evaluation SQL and questions must never enter prompts, code, tests, or the semantic model.
2. **Strictly Read-Only Database Access**: Always open `data/football.db` strictly read-only (`file:data/football.db?mode=ro`, `uri=True`). Never attempt write operations or open the database in read-write mode.
3. **Dynamic Runtime Loading**: Always load `data/semantic_model.md` (and `data/schema.sql`) dynamically from disk at runtime rather than hardcoding them inside prompts.
4. **Strict 4-Action Contract**: Allow strictly 4 actions from the planner:
   - `answer_from_query`
   - `ask_clarifying_question`
   - `refuse_write`
   - `refuse_out_of_scope`
5. **Strict Answer Grounding**: Final answers may only state numbers, names, scores, and dates that appear in the returned SQL rows; never extrapolate or use outside football knowledge.
