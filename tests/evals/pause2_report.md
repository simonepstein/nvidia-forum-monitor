# Review pause 2 / Run A checkpoint — evidence

Recorded 2026-11-08. This is the substantiation for the second review pause (end of Stage 5,
which doubles as the Run A checkpoint).

## 1. MCP surface

`sparkwatch serve-mcp` is registered in this OpenFox session as a stdio MCP server:

```
name:      sparkwatch
command:   uv run --directory /home/simon/dev/nvidia-forum-monitor sparkwatch serve-mcp
env:       SPARKWATCH_GENERAL__DB_PATH=/home/simon/dev/nvidia-forum-monitor/tests/evals/frozen.db
```

`list_tools` → 39 tools, matching SPEC §5 exactly (snapshot: `tests/snapshots/tools.json`):

```
ack_watch_sighting, add_model_alias, add_watch, check_repos_now, clear_installed, digest,
find_model_mentions, flag, get_notes, get_post, get_posts, get_repo, get_topic_overview,
list_benchmarks, list_flags, list_hf_models, list_installs, list_recipes, list_repos,
list_topics, list_watchlist, mark_topic, model_summary, record_benchmark, record_recipe,
repo_events, repo_status, resolve_flag, review_proposed, save_note, search_posts,
set_installed, set_review, sql_query, sync_now, update_watch, upsert_model, watch_repo,
whats_new_for
```

`list_prompts` → `catch_up`, `frontier`, `serve_model`, `should_i_update`.
`list_resources` → `sparkwatch://watchlist`, `sparkwatch://flags/open`, `sparkwatch://notes/latest`.

Protocol tests (`uv run pytest tests/test_mcp_protocol.py`): **10 passed**, over real stdio with the
`mcp` client SDK — schema snapshot, truncation round-trip with no gaps or overlap, `isError` on an
unknown topic and on a rejected `sql_query`, server survives both.

## 2. Agent run

The checkpoint called for a real agent driving `catch_up` and `serve_model("GLM 5.3 Flash")`.
The `claude` CLI is not installed on this machine, so the run was done by the OpenFox agent over
the same stdio MCP server (`tests/evals/run.py --check-only` verifies the result). The tool calls
were exactly the ones the prompts prescribe:

| Workflow | Tools called |
|---|---|
| `serve_model("GLM 5.3 Flash")` | `model_summary` → `search_posts` (topic 381350) → `get_posts` / `get_post` → `record_recipe` → `save_note` |
| `catch_up` | `digest` → `flag` |
| `frontier("dual-spark")` | `list_watchlist` → `list_recipes` → `list_benchmarks` → `save_note` |

What the agent recorded (all quotes verified verbatim against the mirrored posts):

- recipe 1 — `glm-5.3-flash`, quant `exl` (EXL3), topology `dual-spark`, status `works`,
  2300 tok/s prefill / 50 tok/s decode, evidence posts 1875216 and 1877572, `review='accepted'`
- note 1 — "serving GLM 5.3 Flash on dual-spark", as_of 2026-10-23, sources 1875216 + 1877572
- note 2 — "frontier dual-spark"
- flag 1 — `perf-claim` on topic 381350 (priority 2)

Database state afterwards:

```
recipes = 1, notes = 2, flags = 1, verified_quotes = 4, watch_hits = 159
```

## 3. Eval result

```
$ uv run python tests/evals/build_db.py
built tests/evals/frozen.db with 60 topics and 187 posts

$ uv run python tests/evals/run.py --check-only --answers tests/evals/answers.openfox.json
[PASS] catch_up_flags_work: ok
[PASS] serve_glm_records_recipe: ok
[PASS] frontier_saves_note: ok
```

`serve_glm_records_recipe` checks the database (a recipe exists, `topology='dual-spark'`, at least
one note, at least one verified quote, and every recipe evidence row maps to a citable post URL)
and the answer text (mentions GLM, contains a forum post URL, and cites the evidence URLs).

## 4. Known gap

The `claude -p` driver in `tests/evals/run.py` is still the documented path and is untested here
because the CLI is absent. `--check-only --answers` exists so the same cases can be checked after
any agent drives the server, which is how this run was verified.

## 5. Live 60-day mirror run

The same workflows were also driven against the live mirror (`data/sparkwatch.db`: 30 topics,
669 posts, category 723 over the default 60-day window), through the same stdio MCP server
pointed at that database.

| Workflow | Tools called | Result |
|---|---|---|
| `serve_model("GLM 5.3 Flash")` | `find_model_mentions` → `record_recipe` → `save_note` | recipe 1 (single-spark, EXL3, `works`, evidence posts 1881645 + 1882072, both quotes verified), note 1 |
| `catch_up` | `flag` | flag 1 (`perf-claim` on topic 382140: the 60 tok/s title vs a ~26.4 tok/s report in the same thread) |

Live database state afterwards:

```
recipes = 1, notes = 1, flags = 1, verified_quotes = 4, topics = 30, posts = 669
```

The answer itself, for the record:

> The best documented GLM 5.3 Flash setup in the live mirror is single-Spark, not dual:
> GLM-5.3-Flash-EXL3-2.05 loads on one DGX Spark but only after freeing memory —
> https://forums.developer.nvidia.com/t/382140/20 and https://forums.developer.nvidia.com/t/382140/21.
> The thread title claims 60 tok/s; another user reports ~26.4 tok/s on real tasks, so treat the
> top figure as best case. Dual-Spark reports live in category 721, which this 60-day sync did not
> cover — widen with `backfill` before answering a dual-Spark question.

That last point is a real finding, not a defect: the default 60-day sync was run against
category 723 only (`--forum 723 --max-pages 1`), so category 721 was never mirrored.
