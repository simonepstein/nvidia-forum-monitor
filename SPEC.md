# Spec: DGX Spark Forum Monitor ("sparkwatch")

## Context
The NVIDIA DGX Spark / GB10 forums are the main place people post which models, quants and serving stacks actually work on Spark hardware. The goal is a local tool that:
1. Keeps a SQLite mirror of two forums up to date:
   - `c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10/721` (general)
   - `c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10-projects/723` (projects)
2. Uses a **local LLM** to pull structured "recipes" out of threads (model + quant + serving engine + topology + measured perf).
3. Ranks the **strongest models that run on 2× DGX Spark** (2 × 128 GB unified memory ≈ 256 GB, linked by ConnectX-7) as of today.
4. Offers an **MCP server** so an agent (Claude Code etc.) can browse, search, fix up and ask questions about the scraped data.

Stack: Python 3.12+, **uv** for dependencies and running (`uv init`, `uv add`, `uv run`). 

## Key finding: no JS rendering needed
The forum runs on **Discourse**, which serves JSON from every page if you add `.json` to the URL. Checked 2026-11-08 with plain `curl` (HTTP 200, no auth):
- Category listing: `GET /c/<path>/<id>.json?order=activity&page=N`. It returns `topic_list.topics[]` with `id, slug, title, posts_count, highest_post_number, bumped_at, last_posted_at, tags, like_count, views, pinned, excerpt` and a `more_topics_url` for paging (30 per page).
- Topic: `GET /t/<id>.json`. It returns `post_stream.posts[]` (first ~20 posts) and `post_stream.stream[]` (all post IDs). The most active topic currently has 956 posts, so pagination is needed.
- Batches of posts: `GET /t/<id>/posts.json?post_ids[]=..&post_ids[]=..` (about 20 per call).
- Raw markdown of a post: `GET /raw/<topic_id>/<post_number>`. This is easier for the LLM to read than the `cooked` HTML.
- Post fields: `id, post_number, username, created_at, updated_at, cooked, reply_to_post_number, reactions, score, trust_level, staff, accepted_answer, version`.

**Decision:** use `httpx` against the JSON API. Leave Playwright out of v1. Only add it as an optional extra (`uv add --optional browser playwright`) if Cloudflare or bot protection starts blocking requests.

## Architecture
```
 scraper (httpx, Discourse JSON) ──► SQLite (raw tables + FTS5)
                                         │
 extractor (local LLM, OpenAI-compatible) ◄┘ ──► recipes / mentions tables
                                         │
 ranker (deterministic scoring + LLM summary) ──► leaderboard + daily report (md)
                                         │
 MCP server (FastMCP, stdio) ◄── agent: query / annotate / re-extract / trigger sync
 CLI (typer): sync | extract | rank | report | serve-mcp
```

## Project layout
```
pyproject.toml            # uv-managed; [project.scripts] sparkwatch = "sparkwatch.cli:app"
config.toml               # forums, keywords, LLM endpoint, rate limits, hardware budget
src/sparkwatch/
  config.py               # pydantic-settings; loads config.toml + env (SPARKWATCH_*)
  db.py                   # sqlite3 connection, migrations (PRAGMA user_version), FTS5 setup
  discourse.py            # Discourse JSON client: list_category, get_topic, get_posts, get_raw
  sync.py                 # incremental sync logic
  filters.py              # keyword / regex relevance scoring (cheap pre-filter)
  llm.py                  # OpenAI-compatible client wrapper, JSON-schema structured output
  extract.py              # per-thread recipe extraction, chunking, prompt templates
  normalize.py            # canonical model names, quant names, engine names (alias tables)
  repos.py                # GitHub/HF link discovery + GitHub API monitoring (ETag-cached)
  rank.py                 # scoring + leaderboard
  report.py               # markdown "state of the art today" report
  websearch.py            # optional Brave Search enrichment (model sizes, HF links)
  mcp_server.py           # FastMCP server
  cli.py                  # typer CLI
prompts/extract_recipe.md, prompts/summarize_leaderboard.md
data/sparkwatch.db        # gitignored
tests/ (pytest, with recorded JSON fixtures under tests/fixtures/)
```
Dependencies: `httpx`, `tenacity`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `openai` (used as a client for any OpenAI-compatible local server), `mcp` (official Python SDK, which includes FastMCP), `markdownify` or `selectolax` (fallback HTML→text), `python-dateutil`. Dev: `pytest`, `respx`, `ruff`.

## 1. Scraper (`discourse.py`, `sync.py`)
- Be polite: identify with `User-Agent: sparkwatch/0.1` (generic, with no personal contact details), allow at most 1 request per second (configurable), and use exponential backoff with `tenacity`. On HTTP 429, honour `Retry-After`. Anonymous Discourse rate limits are around 60 requests/min.
- **Incremental algorithm**, run for each category:
  1. Page through `?order=activity` until a page's oldest `bumped_at` is earlier than `forums.last_synced_at`, or there are no more pages.
     - The first run goes back only `sync.lookback_days` (default 60, set in config or with `--lookback-days`). It does not crawl the full history.
  2. For each topic, upsert the topic metadata. If `highest_post_number` is higher than the stored `max_post_number`, or `bumped_at` changed, queue the topic for fetching.
  3. Fetch `/t/<id>.json` → work out which post IDs from `stream[]` are missing → batch-fetch them through `posts.json`. Also re-fetch posts whose `version` or `updated_at` changed (edits).
  4. Store both `cooked` (HTML) and `raw` (markdown). Fetch `raw` lazily, only for relevant topics, to save requests.
  5. Mark the topic `needs_extraction = 1` whenever it gains posts.
- **The history window can be widened later without starting over.**
  - Each forum stores `backfill_floor`, the oldest date covered so far.
  - Raising `lookback_days` makes the next `sync` (or `sparkwatch backfill --days 180`) page further back through the listing. It only goes from the current floor to the new one, then moves the floor. Topics and posts already stored are skipped, because inserts are idempotent upserts keyed on Discourse IDs.
  - Within the window, a long-running topic that was bumped recently gets its OP plus every post after `backfill_floor`. Older posts in that topic are filled in automatically when the floor moves back.
  - Because extraction is tracked per post (below), backfilled posts are picked up by the next `extract` run, and nothing gets processed twice.
  - Older recipes have little effect on the ranking anyway, because of the recency decay.
- Run on a schedule with a systemd user timer or cron (`uv run sparkwatch sync && uv run sparkwatch extract && uv run sparkwatch report`), e.g. every 2 hours.

## 2. SQLite schema (`db.py`)
```sql
forums(id INTEGER PK, slug, url, name, last_synced_at)
topics(id INTEGER PK, forum_id, slug, title, tags JSON, created_at, bumped_at, last_posted_at,
       posts_count, max_post_number, views, like_count, pinned, has_accepted_answer,
       relevance_score REAL, needs_extraction INT, last_extracted_at, fetched_at)
posts(id INTEGER PK, topic_id, post_number, username, trust_level, staff, created_at, updated_at,
      version, reply_to_post_number, like_count, accepted_answer, cooked TEXT, raw TEXT, fetched_at)
posts_fts  -- FTS5 over (title, raw/cooked text), with content-sync triggers
recipes(id PK, topic_id, source_post_ids JSON, model_canonical, model_family, params_total_b,
        params_active_b, quant, quant_format, engine, engine_version, topology
        -- 'single'|'dual-spark'|'other', tensor_parallel, pipeline_parallel, context_len,
        tok_s_decode, tok_s_prefill, concurrency, memory_gb, status
        -- 'works'|'partial'|'fails'|'claimed', evidence_quote, confidence REAL,
        posted_at, extractor_model, extracted_at, human_override JSON, superseded_by)
-- GitHub repos as first-class entities
repos(id PK, owner, name, url UNIQUE, description, stars, default_branch, pushed_at,
      latest_release_tag, latest_release_at, archived, watch INT DEFAULT 1,
      relevance_note, first_seen_at, last_checked_at, etag)
repo_mentions(repo_id, post_id, topic_id, context_snippet, mentioned_at, PRIMARY KEY(repo_id, post_id))
repo_events(id PK, repo_id, kind  -- 'release'|'commits'|'readme_changed'|'archived',
            ref, title, body_excerpt, occurred_at, summary_llm, processed INT, detected_at)
model_aliases(alias TEXT PK, canonical TEXT)        -- e.g. "qwen3-235b-a22b-fp4" -> "Qwen3-235B-A22B"
annotations(id PK, target_type, target_id, author, note, created_at)  -- agent/human notes
extraction_runs(id PK, started_at, finished_at, llm_model, topics_processed, errors)
```
Use WAL mode so the MCP server can read while a sync is writing.

**Everything lives in SQLite, including what the LLM produces.** Raw posts, recipes, repo summaries, repo events, the leaderboard, reports, annotations and overrides all go in tables. Reports are also exported as markdown files. The database is the single source of truth, and processing is incremental:
- Extraction is tracked **per post**, not with a single high-water mark per topic, so backfilled older posts are handled correctly. Each post has `posts.extracted_at` and `posts.extracted_hash`. Extraction selects posts where `extracted_at IS NULL OR extracted_hash != content_hash`, groups them by topic, and adds the OP and neighbouring posts as read-only context.
- `posts.content_hash` is a hash of `raw`. If a post is edited, its hash changes, and its old recipes are marked `superseded_by` and extracted again.
- `extraction_runs` records each run: prompt version, LLM model, and the topics/posts it covered. When `prompt_version` changes, `sparkwatch extract --reprocess` re-runs everything on purpose. Otherwise nothing is ever processed twice.
- `repo_events.processed` works the same way for repo updates.
- `leaderboard_snapshots(date, target, rows JSON)` and `reports(date, markdown, diff_from)` keep history, so "what changed" is just a query.

Schema additions: add `backfill_floor` to forums; add `content_hash`, `extracted_at` and `extracted_hash` to posts; and add `prompt_version` to recipes and extraction_runs.

## 3. Relevance pre-filter (`filters.py`)
This is cheap and runs before the LLM. Keywords come from `config.toml` (you can extend the list). Starting set: `dual spark, two sparks, 2x spark, cluster, stacked, connectx, qsfp, RoCE, NCCL, tensor parallel, TP=2, pipeline parallel, vllm, sglang, trt-llm, tensorrt-llm, llama.cpp, ollama, nvfp4, fp4, fp8, mxfp4, awq, gptq, gguf, exl, tok/s, tokens/s, benchmark`, plus regexes for model sizes (`\b\d{2,3}B\b`, `A\d+B`) and known model families.

Score = keyword hits weighted by type (dual-node terms weigh most) + engagement (likes, reply count). Topics above a threshold get LLM extraction; the rest still go into FTS so they can be searched.

## 4. Local LLM extraction (`llm.py`, `extract.py`)
- **Endpoint:** any OpenAI-compatible server (vLLM, SGLang, llama.cpp `llama-server`, or Ollama), set by `llm.base_url`, `llm.model`, `llm.api_key`. It can run on the Sparks themselves. Default: a mid-size instruct model good at JSON, such as a Qwen3-30B-A3B-class model. Use structured output (`response_format` with a JSON schema, or guided JSON in vLLM/SGLang), and validate against Pydantic with one retry on failure.
- **Chunking:** go through each topic in windows of roughly 8–12k tokens (OP plus consecutive posts, with the OP repeated as context). Large threads like the 956-post one are processed incrementally: only posts that haven't been extracted yet or whose content has changed (per-post `extracted_hash` ≠ `content_hash`), with the OP and neighbouring posts added as context.
- **Per-chunk output** (Pydantic `RecipeMention`): model, quant, engine plus version, topology (single or dual Spark, TP/PP), context length, decode and prefill tok/s, concurrency, memory used, status (works/partial/fails), a verbatim `evidence_quote`, `source_post_numbers`, and confidence. The prompt tells the LLM to extract **only what is stated**, never to infer numbers, and to label each finding as a report, a claim, or a question.
- **Normalisation** (`normalize.py`): deterministic alias tables plus regex for model and quant names. The LLM proposes a canonical name only when the alias table doesn't have one, and new aliases are saved for a human or agent to review.
- **Fit check:** using estimated weight size from params × bits, plus a KV-cache margin, flag recipes claimed as dual-Spark that won't fit in about 240 GB usable, and single-Spark recipes that won't fit in about 120 GB.

## 4b. GitHub repos (`repos.py`)
- **Discovery** is deterministic and doesn't use the LLM. A regex finds `github.com/<owner>/<repo>` in each post's `cooked` hrefs and in its `link_counts`. The URL is normalised (strip `/tree/...`, `.git`, and anchors) and upserted into `repos`, with a row in `repo_mentions`.
  - Gists and `huggingface.co/<org>/<model>` links go into a similar `hf_models` table. This is the same pattern; HF repos are useful for tracking quant uploads.
- **Monitoring** (`sparkwatch repos check`) uses the GitHub REST API. `GITHUB_TOKEN` is optional; without one the limit is 60 requests/hour, with a PAT it is 5000.
  - For each watched repo it fetches the repo metadata (stars, `pushed_at`, archived), `releases/latest`, commits since `last_checked_at`, and README changes.
  - ETag / `If-None-Match` is used, so unchanged repos cost no rate limit.
  - Each change becomes a row in `repo_events`.
- **LLM step:** for new repos, summarise the README into `relevance_note` (what it does for Spark: serving recipe, cluster setup, quant tooling). For new release or commit events, write `summary_llm` and flag whether the change affects a recipe (e.g. "vLLM fork adds NVFP4 MoE kernels for GB10").
- **Ranking link:** recipes reference repos through `recipe_repos(recipe_id, repo_id)`. A recent release in a linked repo is noted on the leaderboard ("recipe may be stale or improved").
- **Watch policy:** a repo is auto-watched once it is mentioned in at least N relevant topics, or when set manually through MCP.

## 5. Ranking (`rank.py`, `report.py`)
These are deterministic scores, so results can be reproduced; the LLM only writes the final narrative.
- **Strength of the model:** a configurable table `model_quality` (family → tier or benchmark score, maintained by hand or the agent, and optionally filled in through web search).
- **Evidence strength:** the number of distinct users reporting `works`, accepted answers, likes, staff posts, reproducibility (several posts agreeing), and the presence of measured tok/s.
- **Recency:** exponential decay (half-life about 30 days, configurable). Newer engine versions replace older recipes for the same model+quant+engine.
- **Usability:** a minimum decode tok/s threshold (e.g. ≥ 10 tok/s) and context length.
- **Output:**
  - Leaderboard per hardware target (`dual-spark` as the main one, `single-spark` alongside). Each row has the best recipe per model, with links to the source posts.
  - `reports/YYYY-MM-DD.md` written with `summarize_leaderboard.md`.
  - A "what changed since the last report" diff.

## 6. MCP server (`mcp_server.py`, FastMCP, stdio; register with `claude mcp add sparkwatch -- uv run sparkwatch serve-mcp`)
Read tools:
- `search_posts(query, forum?, since?, limit)` – FTS5 search returning snippets, links and topic IDs
- `list_topics(forum?, since?, min_relevance?, order)`
- `get_topic(topic_id, from_post?, to_post?)` – posts as markdown
- `list_recipes(model?, engine?, topology?, status?, since?)`
- `get_leaderboard(target="dual-spark", limit=20)`
- `get_report(date?)` / `whats_new(since)`
- `list_repos(watched?, mentioned_since?, order="mentions"|"pushed_at")`, `get_repo(repo)` (metadata, mentions, events), `repo_updates(since)`
- `sql_query(sql)` – **read-only** (`mode=ro` URI connection, SELECT only, row limit)

Write / curation tools:
- `annotate(target_type, target_id, note)`
- `override_recipe(recipe_id, fields)` – writes `human_override`; the ranker prefers override values
- `add_model_alias(alias, canonical)`, `set_model_quality(model, tier/score, source)`
- `watch_repo(repo, watch: bool, note?)`
- `mark_topic(topic_id, relevant: bool)` – forces the topic into or out of extraction
- `trigger_sync(forum?)` / `trigger_extract(topic_ids?)` – runs as a subprocess with a lock file, so two runs never overlap

Resources: `sparkwatch://report/latest` and `sparkwatch://leaderboard/dual-spark`.
With these tools, Claude Code (or a local agent) can answer questions like "what's the best >200B model people run across two Sparks with vLLM?" and fix bad extractions.

## 7. Optional web search (`websearch.py`)
- Brave Search API (`BRAVE_API_KEY`). It's off by default.
- Uses: resolving unknown model names to parameter counts or Hugging Face links, and filling in `model_quality`.
- Results are cached in a `web_cache` table.
- It's never needed for scraping, because the forum JSON is directly reachable.

## Build phases
1. `uv init --package sparkwatch`, config, db and migrations, the Discourse client, and `sync`. Verify against the live forums.
2. FTS plus the keyword filter, and the MCP read tools. This is useful even before any LLM work.
3. LLM extraction, normalisation and the recipes table.
4. Discovering repos and HF models from posts (deterministic, can happen alongside phase 2), then GitHub monitoring and repo summaries.
5. Ranking and the report; the MCP write and curation tools.
6. Scheduler (systemd timer) and the Brave enrichment.

## Verification
- `uv run pytest`: the Discourse client is tested against recorded JSON fixtures (mocked with `respx`), plus incremental sync (no refetch when nothing changed; edited posts are re-fetched), FTS triggers, and recipe schema validation.
- Live smoke test: `uv run sparkwatch sync --forum 723 --max-pages 1`, then check that row counts in `topics` and `posts` match `posts_count`. Running it a second time should make only listing requests.
- Extraction smoke test against a known dual-Spark thread: check that the recipes include an evidence quote that appears verbatim in `posts.raw`. This is checked automatically to catch hallucinations.
- Incremental check: run `extract` twice. The second run should process 0 posts. Then edit a fixture post (change its hash): only that topic should be re-extracted, and the old recipe should be marked superseded.
- Repos: fixture posts that contain GitHub links should produce normalised rows in `repos` and `repo_mentions`. `repos check` should get a 304 on the second run when the ETag is unchanged.
- MCP: `uv run mcp dev src/sparkwatch/mcp_server.py` (MCP Inspector), then add it to Claude Code and ask "top 5 dual-Spark models this month with tok/s".
