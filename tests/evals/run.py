"""Run the scripted agent evals against a frozen database.

    uv run python tests/evals/build_db.py
    uv run python tests/evals/run.py [--dry-run] [--db PATH] [--model MODEL]

Not part of CI: it needs an agent and API access.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES = HERE / "cases.yaml"
DEFAULT_DB = HERE / "frozen.db"


def check_database(db_path: Path, expect: dict[str, Any]) -> list[str]:
    import sqlite3

    failures: list[str] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if expect.get("recipes_min"):
            count = conn.execute("SELECT COUNT(*) FROM recipes WHERE review='accepted'").fetchone()[
                0
            ]
            if count < expect["recipes_min"]:
                failures.append(f"expected at least {expect['recipes_min']} recipes, found {count}")
        if expect.get("recipe_topology"):
            count = conn.execute(
                "SELECT COUNT(*) FROM recipes WHERE topology=?",
                (expect["recipe_topology"],),
            ).fetchone()[0]
            if count < 1:
                failures.append(f"no recipe with topology {expect['recipe_topology']}")
        if expect.get("notes_min"):
            count = conn.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0]
            if count < expect["notes_min"]:
                failures.append(f"expected at least {expect['notes_min']} notes, found {count}")
        if expect.get("flags_min"):
            count = conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
            if count < expect["flags_min"]:
                failures.append(f"expected at least {expect['flags_min']} flags, found {count}")
        if expect.get("evidence_verified"):
            rows = conn.execute(
                "SELECT quote_verified FROM evidence WHERE target_type='recipe'"
            ).fetchall()
            if not rows or not any(row[0] for row in rows):
                failures.append("no verified recipe quotes")
    finally:
        conn.close()
    return failures


def check_answer(answer: str, expect: dict[str, Any]) -> list[str]:
    return [
        f"answer is missing {needle!r}"
        for needle in expect.get("answer_contains", [])
        if needle.lower() not in answer.lower()
    ]


def run_case(case: dict[str, Any], db_path: Path, model: str | None) -> tuple[bool, str]:
    mcp_config = {
        "mcpServers": {
            "sparkwatch": {
                "command": "uv",
                "args": ["run", "--directory", str(ROOT), "sparkwatch", "serve-mcp"],
                "env": {"SPARKWATCH_GENERAL__DB_PATH": str(db_path)},
            }
        }
    }
    config_path = HERE / "mcp_config.json"
    config_path.write_text(json.dumps(mcp_config, indent=2))

    command = [
        "claude",
        "-p",
        case["prompt"],
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__sparkwatch",
    ]
    if model:
        command += ["--model", model]

    env = dict(os.environ)
    env["SPARKWATCH_GENERAL__DB_PATH"] = str(db_path)
    completed = subprocess.run(command, capture_output=True, text=True, env=env, cwd=ROOT)
    if completed.returncode != 0:
        return False, f"agent run failed: {completed.stderr.strip()[:400]}"

    answer = completed.stdout
    failures = check_answer(answer, case["expect"])
    failures += check_database(db_path, case["expect"].get("database", {}))
    return not failures, "; ".join(failures) or "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="run the scripted agent evals")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--model", default=None)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    cases = yaml.safe_load(arguments.cases.read_text())["cases"]
    if arguments.dry_run:
        for case in cases:
            print(f"{case['name']}: {case['prompt']}")
        return 0

    if not arguments.db.is_file():
        print(f"missing frozen database at {arguments.db}; run tests/evals/build_db.py first")
        return 2

    failed = 0
    for case in cases:
        ok, message = run_case(case, arguments.db, arguments.model)
        print(f"[{'PASS' if ok else 'FAIL'}] {case['name']}: {message}")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
