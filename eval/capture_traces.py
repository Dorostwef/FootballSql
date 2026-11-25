"""Capture 3 representative live traces (join query, refusal, clarifying question) from /api/ask."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

TRACE_CASES = [
    {
        "label": "Trace 1: Join Query",
        "question": "Who are the top 5 goal scorers and their teams?",
        "history": [],
    },
    {
        "label": "Trace 2: Write Refusal",
        "question": "Delete the cancelled matches.",
        "history": [],
    },
    {
        "label": "Trace 3: Clarifying Question",
        "question": "Who is the best player?",
        "history": [],
    },
]


def capture_traces(base_url: str, out_dir: Path) -> None:
    endpoint = f"{base_url.rstrip('/')}/api/ask"
    traces: list[dict[str, Any]] = []

    with httpx.Client(timeout=60.0) as client:
        for case in TRACE_CASES:
            print(f"Capturing {case['label']}: {case['question']}")
            resp = client.post(
                endpoint,
                json={"question": case["question"], "history": case["history"]},
            )
            resp.raise_for_status()
            payload = resp.json()
            traces.append(
                {
                    "label": case["label"],
                    "question": case["question"],
                    "response": payload,
                }
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "traces.json").write_text(
        json.dumps(traces, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md_lines = ["# FootballSql Live Execution Traces", ""]
    for t in traces:
        r = t["response"]
        md_lines.extend(
            [
                f"## {t['label']}",
                f"- **Question**: {t['question']}",
                f"- **Action**: `{r.get('action')}`",
                f"- **Intent**: {r.get('intent')}",
                f"- **Executed**: `{r.get('executed')}` | **Row count**: `{r.get('row_count')}`",
                f"- **Tables used**: `{', '.join(r.get('tables_used') or []) or 'none'}`",
                f"- **Columns used**: `{', '.join(r.get('columns_used') or []) or 'none'}`",
                f"- **Answer**: {r.get('answer')}",
                "",
            ]
        )
        if r.get("sql"):
            md_lines.extend(["```sql", r["sql"], "```", ""])

    (out_dir / "traces.md").write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Saved traces to {out_dir / 'traces.json'} and {out_dir / 'traces.md'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture 3 live traces from FootballSql /api/ask.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL of running FastAPI app.")
    parser.add_argument("--out-dir", default="eval/results", help="Directory for trace outputs.")
    args = parser.parse_args()
    capture_traces(base_url=args.base_url, out_dir=Path(args.out_dir))


if __name__ == "__main__":
    main()
