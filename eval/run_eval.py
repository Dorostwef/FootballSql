"""Standalone evaluation runner for FootballSql against POST /api/ask."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

RANKING_KEYWORDS_EN = ("top", "most", "best")
RANKING_KEYWORDS_FA = ("برتر", "بیشترین", "بهترین", "صدرنشین")


def _normalize_cell(val: Any) -> Any:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if isinstance(val, float) and val.is_integer():
            return int(val)
        return round(float(val), 4) if isinstance(val, float) else val
    if isinstance(val, str):
        return re.sub(r"\s+", " ", val.strip())
    return val


def _normalize_rows(rows: list[list[Any]]) -> list[tuple[Any, ...]]:
    return [tuple(_normalize_cell(cell) for cell in row) for row in rows]


def _requires_order(item: dict[str, Any]) -> bool:
    q_en = (item.get("question_en") or "").lower()
    q_fa = item.get("question_fa") or ""
    if any(re.search(rf"\b{kw}\b", q_en) for kw in RANKING_KEYWORDS_EN):
        return True
    if any(kw in q_fa for kw in RANKING_KEYWORDS_FA):
        return True
    return False


def _grade_item(
    item: dict[str, Any],
    resp_data: dict[str, Any],
) -> tuple[bool, str]:
    qid = item["id"]
    action = resp_data.get("action")
    sql = resp_data.get("sql")
    executed = resp_data.get("executed")

    if qid == "q16":
        if action == "refuse_out_of_scope" and sql is None and executed is False:
            return True, "Correctly refused out-of-scope question"
        return (
            False,
            f"Trap q16 failed: expected action='refuse_out_of_scope', sql=null, executed=false; "
            f"got action={action!r}, sql={sql!r}, executed={executed!r}",
        )

    if qid == "q17":
        if action == "refuse_write" and sql is None and executed is False:
            return True, "Correctly refused write request"
        return (
            False,
            f"Trap q17 failed: expected action='refuse_write', sql=null, executed=false; "
            f"got action={action!r}, sql={sql!r}, executed={executed!r}",
        )

    if action != "answer_from_query" or not executed:
        errors = (resp_data.get("validation") or {}).get("errors") or []
        err_note = f" ({'; '.join(errors)})" if errors else ""
        return False, f"Did not execute query: action={action!r}, executed={executed!r}{err_note}"

    expected_rows = _normalize_rows(item.get("expected_rows") or [])
    actual_rows = _normalize_rows(resp_data.get("rows") or [])

    if _requires_order(item):
        if actual_rows == expected_rows:
            return True, "Exact ordered row match"
        if Counter(actual_rows) == Counter(expected_rows):
            return False, f"Wrong row order (same multiset of {len(actual_rows)} rows)"
        missing = list((Counter(expected_rows) - Counter(actual_rows)).elements())
        extra = list((Counter(actual_rows) - Counter(expected_rows)).elements())
        return (
            False,
            f"Row mismatch (ordered): expected {len(expected_rows)} rows, got {len(actual_rows)}; "
            f"missing={missing[:3]}, extra={extra[:3]}",
        )

    if Counter(actual_rows) == Counter(expected_rows):
        return True, "Exact unordered multiset match"

    missing = list((Counter(expected_rows) - Counter(actual_rows)).elements())
    extra = list((Counter(actual_rows) - Counter(expected_rows)).elements())
    return (
        False,
        f"Row mismatch: expected {len(expected_rows)} rows, got {len(actual_rows)}; "
        f"missing={missing[:3]}, extra={extra[:3]}",
    )


def run_evaluation(
    base_url: str,
    lang: str,
    out_dir: Path,
    delay: float,
    questions_path: Path,
) -> int:
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    langs = ["en", "fa"] if lang == "both" else [lang]
    endpoint = f"{base_url.rstrip('/')}/api/ask"

    results: list[dict[str, Any]] = []
    passed_count = 0

    with httpx.Client(timeout=60.0) as client:
        total = len(questions) * len(langs)
        idx = 0
        for item in questions:
            for cur_lang in langs:
                idx += 1
                q_text = item[f"question_{cur_lang}"]
                print(f"[{idx}/{total}] {item['id']} ({cur_lang}): {q_text}")

                try:
                    resp = client.post(endpoint, json={"question": q_text, "history": []})
                    if resp.status_code != 200:
                        ok = False
                        reason = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        payload: dict[str, Any] = {}
                    else:
                        payload = resp.json()
                        ok, reason = _grade_item(item, payload)
                except Exception as exc:
                    ok = False
                    reason = f"Request error: {exc}"
                    payload = {}

                if ok:
                    passed_count += 1
                print(f"  -> {'PASS' if ok else 'FAIL'}: {reason}")

                results.append(
                    {
                        "id": item["id"],
                        "lang": cur_lang,
                        "difficulty": item.get("difficulty", ""),
                        "question": q_text,
                        "passed": ok,
                        "reason": reason,
                        "action": payload.get("action"),
                        "executed": payload.get("executed"),
                        "row_count": payload.get("row_count", 0),
                        "sql": payload.get("sql"),
                        "rows": payload.get("rows", []),
                        "answer": payload.get("answer", ""),
                    }
                )

                if idx < total and delay > 0:
                    time.sleep(delay)

    out_dir.mkdir(parents=True, exist_ok=True)
    report_json = {
        "base_url": base_url,
        "lang": lang,
        "passed": passed_count,
        "total": len(results),
        "pass_rate": round(passed_count / len(results), 4) if results else 0.0,
        "results": results,
    }
    (out_dir / "eval_report.json").write_text(
        json.dumps(report_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md_lines = [
        "# FootballSql Evaluation Report",
        "",
        f"- **Target**: `{base_url}`",
        f"- **Language**: `{lang}`",
        f"- **Score**: **{passed_count} / {len(results)}** ({report_json['pass_rate'] * 100:.1f}%)",
        "",
        "| ID | Lang | Difficulty | Status | Action | Rows | Notes / Failure Reason |",
        "|---|---|---|---|---|---:|---|",
    ]
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        reason_clean = str(r["reason"]).replace("|", "\\|").replace("\n", " ")
        md_lines.append(
            f"| {r['id']} | {r['lang']} | {r['difficulty']} | {status} | "
            f"`{r['action']}` | {r['row_count']} | {reason_clean} |"
        )
    (out_dir / "eval_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"\nSaved reports to {out_dir / 'eval_report.md'} and {out_dir / 'eval_report.json'}")
    print(f"Final score: {passed_count}/{len(results)}")
    return 0 if passed_count == len(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run FootballSql evaluation suite against /api/ask.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL of running FastAPI app.")
    parser.add_argument("--lang", choices=["en", "fa", "both"], default="en", help="Question language.")
    parser.add_argument("--out-dir", default="eval/results", help="Directory for evaluation outputs.")
    parser.add_argument("--delay", type=float, default=2.5, help="Delay in seconds between requests.")
    args = parser.parse_args()

    questions_path = Path(__file__).resolve().parent / "eval_questions.json"
    raise SystemExit(
        run_evaluation(
            base_url=args.base_url,
            lang=args.lang,
            out_dir=Path(args.out_dir),
            delay=args.delay,
            questions_path=questions_path,
        )
    )


if __name__ == "__main__":
    main()
