# Spec: DGX Spark Forum Monitor ("sparkwatch")

## Context
The NVIDIA DGX Spark / GB10 forums are the main place people post which models, quants and serving stacks actually work on Spark hardware. This is especially true for **2× DGX Spark**: 2 × 128 GB unified memory, about 256 GB, linked by ConnectX-7.

The aim is to **stay up to date** and to answer questions such as:
- "What's the best way to serve GLM 5.3 Flash on two Sparks today?"
- "Pull the latest posts and flag anything interesting."

**sparkwatch is a local data layer behind an MCP server. The MCP server is the primary interface.**
- A frontier agent (e.g. Claude Code) does the reading, judging and answering.
- sparkwatch does the parts an agent is bad at or can't do:
  - fetching politely and incrementally from a site the agent's own web tools may be blocked from;
  - keeping a searchable mirror;
  - tracking what has changed since the last time the agent looked;
  - watching for specific models;
  - **remembering what was concluded before**: recipes, benchmarks, research notes and flags, all with links back to the source posts.

Why an agent instead of a precomputed leaderboard:
- The useful answer lives in the details: the engine build that works this week, the NCCL variables, the container image, the flag that fixes the out-of-memory error at long context, and the reply saying a quant breaks tool calling.
- Recent reports often contradict each other ("fails with 0.11, works with 0.12rc2").
- An agent reading the latest relevant posts handles both of these better than a fixed scoring formula does.

The tool is meant to be more bleeding-edge than curated recipe sites such as Spark Arena, which collects submitted recipes. It catches first reports of new models, quants and benchmark runs.

Two forum categories are monitored:
- **721** general: `https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10/721`
- **723** projects: `https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10-projects/723`

Models currently tracked:
- GLM 5.3 Flash
- DeepSeek V4.1 Flash
- Qwen3.8 Flash Next
- **anticipated:** Qwen4

Scope:
- **MVP (Run A) needs no local LLM.** Local-LLM triage is phase 2 (Run B). A deterministic leaderboard and scheduled reports are optional later work (Run C).
- **Stack:** Python 3.12+, with **uv** for dependencies and running.
- **Conventions:**
  - all timestamps are stored as ISO-8601 UTC `TEXT`;
  - JSON columns are `TEXT` containing JSON;
  - "forum" means a Discourse **category id**, either 721 or 723.

## Forum API (verified 2026-11-08 with plain curl, no auth, HTTP 200)
The forum runs on Discourse, so **no JS rendering is needed**. Base URL: `https://forums.developer.nvidia.com`.

| Purpose | Request | Notes |
|---|---|---|
| Category listing | `GET /c/<path>/<id>.json?order=activity&page=N` | Returns `topic_list.topics[]`, 30 per page. `topic_list.more_topics_url` is absent on the last page. Pinned topics come first, whatever their activity. |
| Topic | `GET /t/<id>.json?include_raw=true` | Returns `post_stream.posts[]` (the first 20 posts, each with `raw`) and `post_stream.stream[]` (**all** post IDs in order). |
| Post batch | `GET /t/<id>/posts.json?post_ids[]=..&...&include_raw=true` | **Verified with 50 IDs per call.** Returns `post_stream.posts[]` with `raw`. |
| Raw post | `GET /raw/<topic_id>/<post_number>` | Fallback only. |

- **Topic list fields used:** `id, slug, title, tags, created_at, bumped_at, last_posted_at, posts_count, highest_post_number, views, like_count, pinned, has_accepted_answer, excerpt`.
- **Post fields used:** `id, topic_id, post_number, post_type, username, trust_level, staff, created_at, updated_at, version, reply_to_post_number, reactions, accepted_answer, cooked, raw, link_counts[] {url, internal, reflection, title, clicks}`.
- **`post_type`:** 1 is regular, 2 is a moderator action, 3 is a small action, and 4 is a whisper. Store types 1 and 2.
- **`print=true` returns 422.** Don't use it.
- **No rate-limit headers.** Assume anonymous limits of about 60 requests/min.

**Decision:** use `httpx` against the JSON API. Playwright is not part of v1. Add it as an optional extra only if bot protection appears.

## Architecture
```
              ┌──────────── agent (Claude Code etc.) ────────────┐
              │  "catch me up"   "best way to serve X on 2 Sparks" │
              └───────────────▲──────────────┬────────────────────┘
                       read tools            write-back tools
                              │              ▼
 MCP server (FastMCP, stdio) ─┴── sync_now / digest / search / read / record / flag / notes
              │
              ▼
 SQLite (WAL) ── forums/topics/posts + FTS5 ── watchlist + watch_hits ── repos + installs
              │       recipes ── benchmark_results ── research_notes ── flags ── digest_cursors
              ▲
 sync (httpx → Discourse JSON, 1 req/s) + links (regex) + watch matcher (regex)
 repos check (GitHub API, ETag; watched repos only: has the branch moved? new release/tag?)
 systemd timer: background sync every 2h (optional; sync_now works without it)
 [Run B] triage (local LLM) → post_tags        [Run C] leaderboard / reports
```

## Project layout
```
pyproject.toml            # uv-managed; [project.scripts] sparkwatch = "sparkwatch.cli:app"
config.toml
seed/watchlist.toml  seed/model_aliases.toml  seed/models.toml  seed/benchmarks.toml
src/sparkwatch/
  config.py      # pydantic-settings: config.toml + env overrides (SPARKWATCH_ prefix, __ nesting)
  db.py          # connect(), WAL, foreign_keys=ON, busy_timeout, numbered migrations (PRAGMA user_version), seed loader
  discourse.py   # DiscourseClient: list_category_page, get_topic, get_posts (rate-limited, retrying)
  sync.py        # incremental sync, backfill, time budget
  links.py       # GitHub / HF link discovery from posts (HF links are only recorded, never monitored)
  watch.py       # watchlist regex matching → watch_hits
  filters.py     # keyword relevance score (used for ordering digests and search)
  normalize.py   # model/quant/engine/benchmark canonicalisation
  fit.py         # memory fit estimate (an advisory warning on recipes)
  evidence.py    # verify_quote(), shared by recipes and benchmarks
  repos.py       # GitHub monitoring of watched repos + compare against installed SHAs
  digest.py      # builds the "since last time" digest
  lock.py        # fcntl lock on data/sparkwatch.lock for write jobs (sync, repos check, triage)
  mcp_server.py  # FastMCP server: tools, resources, prompts
  cli.py         # typer CLI (thin wrappers over the same functions the MCP tools call)
  triage.py, llm.py      # Run B
  rank.py, report.py     # Run C
  websearch.py           # optional
scripts/record_fixtures.py   # one-off: records a small live sample into tests/fixtures/
deploy/sparkwatch-sync.service, deploy/sparkwatch-sync.timer
data/                        # gitignored
tests/
```
- **Dependencies:** `httpx`, `tenacity`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `mcp`, `selectolax`.
- **Dev dependencies:** `pytest`, `respx`, `ruff`.
- `openai` is added in Run B only.
- **One code path:** every MCP tool and CLI command calls the same function in the library modules.

## Configuration (`config.toml`)
```toml
[general]
db_path = "data/sparkwatch.db"
log_level = "INFO"

[forum]
base_url = "https://forums.developer.nvidia.com"
user_agent = "sparkwatch/0.1"
requests_per_second = 1.0
max_retries = 5
timeout_s = 30
posts_batch_size = 50

[[forum.categories]]
id = 721
path = "accelerated-computing/dgx-spark-gb10/dgx-spark-gb10"
name = "DGX Spark / GB10"

[[forum.categories]]
id = 723
path = "accelerated-computing/dgx-spark-gb10/dgx-spark-gb10-projects"
name = "DGX Spark / GB10 Projects"

[sync]
lookback_days = 60
refetch_recent_days = 7          # re-fetch posts from the last N days in bumped topics, to catch edits
sync_now_time_budget_s = 90      # sync_now stops cleanly at this budget; the next call resumes (idempotent)

[relevance]
keywords = { dual = ["dual spark", "two sparks", "2x spark", "2 sparks", "stacked", "cluster", "connectx", "qsfp", "roce", "nccl", "tensor parallel", "tp=2", "tp 2", "pipeline parallel", "multi-node", "ray"],
             engine = ["vllm", "sglang", "trt-llm", "tensorrt-llm", "llama.cpp", "llama-server", "ollama", "lm studio", "exllama", "nim"],
             quant = ["nvfp4", "fp4", "fp8", "mxfp4", "int4", "awq", "gptq", "gguf", "q4_k_m", "q8_0", "exl2", "exl3"],
             perf = ["tok/s", "tokens/s", "t/s", "tps", "prefill", "decode", "benchmark", "throughput", "latency"],
             bench = ["humaneval", "mbpp", "livecodebench", "swe-bench", "aider", "bfcl", "tau-bench", "tau2", "gpqa", "mmlu", "aime", "ifeval", "ruler"] }
weights = { dual = 3.0, engine = 1.5, quant = 1.5, perf = 1.0, bench = 1.5, model_size = 1.0, family = 1.0, watch = 4.0 }
families = ["qwen", "llama", "deepseek", "gpt-oss", "glm", "kimi", "mistral", "gemma", "phi", "nemotron", "minimax", "hunyuan", "granite"]

[hardware]
dual_spark_usable_gb = 240
single_spark_usable_gb = 120
kv_overhead_frac = 0.10

[repos]
max_compare_commits = 50       # commits listed in a repo_status response (the total count is always given)

[mcp]
default_limit = 20
max_limit = 200
max_chars = 40000                # default cap on the text in one tool response

[llm]                            # Run B only
base_url = "http://localhost:8000/v1"
model = "local-model"
api_key = "none"
structured_output = "json_schema"   # or "json_object"
chunk_tokens = 10000
prompt_version = 1

[websearch]
enabled = false                  # needs BRAVE_API_KEY
```
- **Secrets** come only from the environment: `GITHUB_TOKEN`, `BRAVE_API_KEY` and `SPARKWATCH_LLM__API_KEY`.

## 1. Sync (`discourse.py`, `sync.py`, `links.py`, `watch.py`)

### Client behaviour
- Use a single `httpx.Client` with the configured User-Agent.
- A token-bucket limiter keeps requests to `requests_per_second` or fewer.
- Retry with `tenacity` (exponential backoff with jitter) on 429, 5xx and network errors. On a 429, honour `Retry-After`.
- Any other 4xx is logged, and the topic is skipped.

### `sync(forum_ids=None, max_pages=None, lookback_days=None, time_budget_s=None) -> SyncResult`
1. **Window floor:**
   - First run: `now - lookback_days`.
   - Afterwards: whichever is earlier of `forums.backfill_floor` and `now - lookback_days`.
     - If `lookback_days` has grown, this is a backfill: the floor moves back, and older posts in topics already stored are fetched too.
2. **List topics:** page through `order=activity`.
   - Upsert each topic. Pinned topics are always upserted.
   - Stop when there is no `more_topics_url`, when `max_pages` is reached, or when a page's last non-pinned topic has a `bumped_at` earlier than the cutoff:
     - normally the cutoff is `last_synced_at`;
     - on the first run or during a backfill, it is the floor.
   - Topics bumped before the floor are not upserted.
3. **Queue topics:** a topic is queued if it is new, if `highest_post_number` went up, if `bumped_at` changed, or if this is a backfill.
4. **Fetch each queued topic:**
   1. Call `get_topic`, which gives the first 20 posts plus `stream`.
   2. **Target IDs** = IDs in `stream` that aren't stored yet, plus stored posts created within the last `refetch_recent_days`.
   3. Fetch target batches **from newest to oldest**, and stop once a whole batch is older than the floor. The OP is always kept.
   4. Upsert posts with `content_hash = sha256(raw)`.
   5. Run `links.discover` and `watch.match` for each post that was inserted or changed, then recompute the topic's `relevance_score`.
5. **Time budget:** if `time_budget_s` runs out, finish the current topic, commit, and return `partial=True`.
   - `last_synced_at` is **not** advanced on a partial run, so the next call picks up the rest.
   - Upserts are idempotent, so nothing is lost or duplicated.
6. **Finish:**
   - Update `last_synced_at` and `backfill_floor`.
   - Write a `sync_runs` row, including `new_topic_ids` and `updated_topic_ids` (as JSON).
   - A failure on one topic is recorded in `errors` and the sync carries on.

`SyncResult` contains:
- the number of requests made;
- new and updated topic IDs;
- the number of new posts;
- new watch hits;
- the `partial` flag;
- errors.

**Callers:** the CLI `sparkwatch sync`, the MCP tool `sync_now`, and an optional systemd timer (`deploy/`) that runs `sparkwatch sync && sparkwatch repos check` every 2 hours. All of them take `lock.py`'s lock.

`sparkwatch backfill --days D` is `sync(lookback_days=D)`. You never need to start from scratch.

### Link discovery (`links.py`)
This is regex only.
- **Sources:** non-internal `link_counts[].url`, plus hrefs in `cooked`.
- **GitHub:** `github\.com/([\w.-]+)/([\w.-]+)`.
  - Normalise to the lowercase form `https://github.com/owner/name`.
  - Strip `.git`, `/tree|blob/…`, `#…` and `?…`.
  - Skip owners `orgs|features|sponsors|settings|marketplace`.
- **Hugging Face:** `huggingface\.co/([\w.-]+)/([\w.-]+)`. Skip first path segments `datasets|spaces|docs|blog|papers`.
  - HF links are **recorded only**, so the agent can see which quant repo a recipe uses.
  - HF is never polled. **The forum is the curator for new models.**
- **Mentions:** upsert into `repos` or `hf_models`, plus the matching `*_mentions` row. The context snippet is ±150 characters around the URL.
- **Repos are discovered from posts.** Discovery never watches a repo; you choose which repos to watch (§4).

### Watch matching (`watch.py`)
- For each active `watchlist` row, run `re.search(match_regex, raw, re.I)` against the title and raw text of each new or changed post.
- On a match, insert a row into `watch_hits(watch_id, post_id)`. These are unique, so re-matching an edited post is a no-op.
- The first hit for an entry sets `watchlist.first_seen_post_id` and `first_seen_at`. **For an `anticipated` entry this is a "first sighting" alert.** It appears at the top of every digest until the agent acknowledges it with `ack_watch_sighting`.
- When a watch entry is added or its regex changes, it is matched against all stored posts once. That's local only, with no fetching.

## 2. SQLite schema (`db.py`, migration 1)
Every write is an idempotent upsert keyed on natural or Discourse IDs.
- **Connection settings:** `PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000`.

```sql
-- Mirror
CREATE TABLE forums (id INTEGER PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
  last_synced_at TEXT, backfill_floor TEXT);
CREATE TABLE topics (
  id INTEGER PRIMARY KEY, forum_id INTEGER NOT NULL REFERENCES forums(id),
  slug TEXT, title TEXT NOT NULL, tags TEXT, excerpt TEXT,
  created_at TEXT, bumped_at TEXT, last_posted_at TEXT,
  posts_count INTEGER, highest_post_number INTEGER, views INTEGER, like_count INTEGER,
  pinned INTEGER DEFAULT 0, has_accepted_answer INTEGER DEFAULT 0,
  relevance_score REAL DEFAULT 0, relevance_override INTEGER,   -- NULL auto, 1 force-relevant, 0 force-irrelevant
  first_fetched_at TEXT, fetched_at TEXT);
CREATE TABLE posts (
  id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL REFERENCES topics(id),
  post_number INTEGER NOT NULL, post_type INTEGER NOT NULL,
  username TEXT, trust_level INTEGER, staff INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT, version INTEGER, reply_to_post_number INTEGER,
  like_count INTEGER DEFAULT 0, accepted_answer INTEGER DEFAULT 0,
  cooked TEXT, raw TEXT, content_hash TEXT NOT NULL,
  first_fetched_at TEXT NOT NULL,   -- when sparkwatch first saw it (the digest keys on this)
  changed_at TEXT NOT NULL,         -- last time content_hash changed
  fetched_at TEXT,
  UNIQUE(topic_id, post_number));
CREATE INDEX posts_changed ON posts(changed_at);
CREATE INDEX posts_topic ON posts(topic_id, post_number);
CREATE VIRTUAL TABLE posts_fts USING fts5(title, body, tokenize='porter unicode61');
-- A plain FTS5 table with rowid = posts.id and body = raw; title is copied from topics.
-- Triggers on posts handle INSERT/UPDATE/DELETE. A trigger on topics updates title for that topic's rows.
CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, trigger TEXT,  -- 'cli'|'mcp'|'timer'
  requests INTEGER, new_topic_ids TEXT, updated_topic_ids TEXT, posts_new INTEGER, posts_changed INTEGER,
  partial INTEGER, errors TEXT);

-- Watching
CREATE TABLE watchlist (id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE, canonical TEXT,
  match_regex TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('tracking','anticipated')),
  first_seen_post_id INTEGER, first_seen_at TEXT, sighting_acked_at TEXT,
  active INTEGER NOT NULL DEFAULT 1, note TEXT, created_at TEXT NOT NULL);
CREATE TABLE watch_hits (watch_id INTEGER REFERENCES watchlist(id), post_id INTEGER REFERENCES posts(id),
  topic_id INTEGER, matched_text TEXT, created_at TEXT NOT NULL, PRIMARY KEY (watch_id, post_id));
CREATE TABLE digest_cursors (name TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL);  -- 'default', or named per use

-- Links
CREATE TABLE repos (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
  description TEXT, default_branch TEXT, archived INTEGER, pushed_at TEXT,
  origin TEXT NOT NULL DEFAULT 'forum' CHECK (origin IN ('forum','agent')),   -- 'agent' = added by URL, not yet seen in a post
  watch INTEGER NOT NULL DEFAULT 0,  -- set explicitly by you/the agent; never automatic
  branch TEXT,                       -- branch to follow; NULL = default_branch
  head_sha TEXT, head_seen_at TEXT,  -- the last observed tip of the followed branch
  latest_release_tag TEXT, latest_tag TEXT,
  note TEXT,                         -- why it's watched, e.g. "vLLM fork we serve GLM 5.3 Flash from"
  first_seen_at TEXT NOT NULL, watched_at TEXT, last_checked_at TEXT, etags TEXT);
CREATE TABLE repo_mentions (repo_id INTEGER REFERENCES repos(id), post_id INTEGER REFERENCES posts(id),
  topic_id INTEGER, url TEXT, context_snippet TEXT, mentioned_at TEXT, PRIMARY KEY (repo_id, post_id));
-- What you have installed locally, so "should I update?" compares against your real deployment
CREATE TABLE repo_installs (id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL REFERENCES repos(id),
  label TEXT NOT NULL DEFAULT 'default',   -- e.g. 'spark-pair', if you run more than one deployment
  sha TEXT NOT NULL,                       -- the full 40-character SHA (short SHAs and tags are resolved when set)
  ref_given TEXT,                          -- what was passed in: a short SHA, tag or branch
  branch TEXT, installed_at TEXT NOT NULL, note TEXT, updated_at TEXT NOT NULL,
  UNIQUE (repo_id, label));
CREATE TABLE repo_events (id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL REFERENCES repos(id),
  kind TEXT NOT NULL CHECK (kind IN ('head_moved','release','tag','archived','branch_gone')),
  ref TEXT,                          -- the new head SHA, or the release/tag name
  prev_ref TEXT,                     -- the previous head SHA, for head_moved
  commits_ahead INTEGER,             -- for head_moved: the number of commits between prev_ref and ref
  title TEXT, body_excerpt TEXT, url TEXT,   -- release name/notes (cut to 2,000 characters), or the compare URL
  occurred_at TEXT, detected_at TEXT NOT NULL,
  UNIQUE (repo_id, kind, ref));
-- GitHub compare responses between two SHAs never change, so they're cached for good
CREATE TABLE compare_cache (repo_id INTEGER NOT NULL, base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
  response TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (repo_id, base_sha, head_sha));
CREATE TABLE hf_models (id INTEGER PRIMARY KEY, repo_id TEXT NOT NULL UNIQUE, first_seen_at TEXT NOT NULL);
CREATE TABLE hf_mentions (hf_model_id INTEGER REFERENCES hf_models(id), post_id INTEGER REFERENCES posts(id),
  topic_id INTEGER, url TEXT, mentioned_at TEXT, PRIMARY KEY (hf_model_id, post_id));

-- Knowledge (written mainly by the agent through MCP)
CREATE TABLE models (canonical TEXT PRIMARY KEY, family TEXT, params_total_b REAL, params_active_b REAL,
  released_at TEXT, hf_repo_id TEXT, note TEXT, updated_at TEXT);
CREATE TABLE model_aliases (alias TEXT PRIMARY KEY, canonical TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('seed','agent','triage')), reviewed INTEGER DEFAULT 0);
CREATE TABLE recipes (
  id INTEGER PRIMARY KEY,
  model_canonical TEXT NOT NULL, model_raw TEXT,
  quant_format TEXT, quant_raw TEXT, quant_repo TEXT,          -- e.g. the HF repo of the quant
  engine TEXT, engine_version TEXT, container_image TEXT,
  topology TEXT NOT NULL CHECK (topology IN ('single-spark','dual-spark','other','unknown')),
  tensor_parallel INTEGER, pipeline_parallel INTEGER,
  context_len INTEGER, tok_s_decode REAL, tok_s_prefill REAL, concurrency INTEGER, memory_gb REAL,
  status TEXT NOT NULL CHECK (status IN ('works','partial','fails','unknown')),
  setup TEXT,          -- commands, flags and env vars, verbatim where possible
  gotchas TEXT,        -- known issues and workarounds
  fit_estimate_gb REAL, fit_ok INTEGER,          -- advisory, from fit.py
  reported_at TEXT NOT NULL,                     -- the date of the source post(s)
  review TEXT NOT NULL DEFAULT 'accepted' CHECK (review IN ('proposed','accepted','rejected')),
  created_by TEXT NOT NULL CHECK (created_by IN ('agent','triage','seed')),
  created_at TEXT NOT NULL, superseded_by INTEGER REFERENCES recipes(id));
CREATE TABLE evidence (           -- the quotes behind recipes and benchmarks
  id INTEGER PRIMARY KEY, target_type TEXT NOT NULL CHECK (target_type IN ('recipe','benchmark','note')),
  target_id INTEGER NOT NULL, post_id INTEGER NOT NULL REFERENCES posts(id),
  quote TEXT, quote_verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE benchmark_results (
  id INTEGER PRIMARY KEY, model_canonical TEXT NOT NULL,
  quant_format TEXT,                 -- NULL means a vendor/full-precision result; set means measured on that quant
  benchmark TEXT NOT NULL,           -- canonical name, see normalize.BENCHMARKS
  score REAL NOT NULL, score_unit TEXT, higher_is_better INTEGER NOT NULL DEFAULT 1,
  source_type TEXT NOT NULL CHECK (source_type IN ('forum','model_card','leaderboard','paper','manual')),
  source_url TEXT, measured_on TEXT CHECK (measured_on IN ('spark','other','vendor','unknown')),
  engine TEXT, measured_at TEXT,
  review TEXT NOT NULL DEFAULT 'accepted' CHECK (review IN ('proposed','accepted','rejected')),
  created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE research_notes (      -- the agent's conclusions, so the next session starts from them
  id INTEGER PRIMARY KEY, subject TEXT NOT NULL,     -- e.g. "serving GLM-5.3-Flash on dual-spark"
  model_canonical TEXT, body TEXT NOT NULL,          -- markdown
  as_of TEXT NOT NULL,                               -- the newest source post considered
  created_at TEXT NOT NULL, superseded_by INTEGER REFERENCES research_notes(id));
CREATE TABLE flags (
  id INTEGER PRIMARY KEY, target_type TEXT NOT NULL CHECK (target_type IN ('topic','post','repo','hf_model','recipe','model')),
  target_id TEXT NOT NULL, label TEXT NOT NULL,      -- e.g. 'new-model','perf-claim','breaking-change','recipe','question-for-me'
  reason TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
  created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT);
CREATE TABLE web_cache (query TEXT PRIMARY KEY, response TEXT NOT NULL, fetched_at TEXT NOT NULL);
```

**Seeding:** `seed/*.toml` is loaded on every run, using upserts. Rows the agent wrote are never overwritten. `watchlist.first_seen_*` and `sighting_acked_at` are never reset.

Initial `seed/watchlist.toml`:
```toml
[[watch]]
label = "GLM 5.3 Flash"
kind = "tracking"
match_regex = 'glm[\s._-]*5\.3[\s._-]*flash'

[[watch]]
label = "DeepSeek V4.1 Flash"
kind = "tracking"
match_regex = 'deepseek[\s._-]*v?4\.1[\s._-]*flash'

[[watch]]
label = "Qwen3.8 Flash Next"
kind = "tracking"
match_regex = 'qwen[\s._-]*3\.8[\s._-]*(flash|next)'

[[watch]]
label = "Qwen4"
kind = "anticipated"
match_regex = 'qwen[\s._-]*4(?![\d.])'
```
- `seed/model_aliases.toml` and `seed/models.toml` start with these four models.
- Exact canonical names, HF repo IDs and variant naming are confirmed against real posts at the Run A checkpoint.
- `seed/benchmarks.toml` starts empty.

## 3. Normalisation and helpers (`normalize.py`, `fit.py`, `evidence.py`, `filters.py`)
These are deterministic helpers, used by the agent write tools and later by triage.

### Model names
1. Look up the alias in `model_aliases`, lowercased, with `-`, `_`, spaces and `.` collapsed.
2. If there's no match, strip quant or format suffixes (`-fp8`, `-nvfp4`, `-awq`, `-gguf`, `-instruct`, …) and try again.
3. If there's still no match, use the cleaned string as written. The write tool's response reports it as a new canonical name, so the agent can add an alias if it's really a variant.

### Quant formats
One of `bf16, fp16, fp8, nvfp4, mxfp4, int8, int4-awq, int4-gptq, gguf-q8, gguf-q6, gguf-q5, gguf-q4, gguf-q3, gguf-q2, exl, other`, matched with an ordered regex table. The quant is recorded on every recipe and benchmark.

### Engines
One of `vllm, sglang, trtllm, llamacpp, ollama, lmstudio, nim, exllama, transformers, other`.

### Benchmarks
- An alias table maps names to canonical ones, such as `humaneval`, `humaneval+`, `mbpp+`, `livecodebench`, `swe-bench-verified`, `aider-polyglot`, `bfcl`, `tau2-bench`, `gpqa-diamond`, `mmlu-pro`, `aime`, `ifeval`, `ruler`.
- Unknown names are kept as written.
- Quality is judged from benchmark evidence, which is partly subjective. The agent weighs it when answering, and v1 has no formula for it.

### `fit.py`
- Bits per weight: `bf16/fp16 16`, `fp8/int8 8.5`, `nvfp4/mxfp4/int4-* 4.5`, `gguf-q8 8.5`, `q6 6.6`, `q5 5.7`, `q4 4.8`, `q3 3.9`, `q2 3.0`.
- `fit_estimate_gb = params_total_b × bits / 8 × (1 + kv_overhead_frac)`.
- `fit_ok` compares the estimate with the usable memory for the topology. It's NULL if the params or quant are unknown, and it's 1 when a stated `memory_gb` comes with `status = works`.
- **The result is advisory:** the write tool returns a warning and doesn't reject the recipe.

### `evidence.verify_quote(post_id, quote)`
True if the whitespace-normalised quote is a substring of the post's whitespace-normalised `raw`.

### `filters.relevance_score(topic)`
- Each keyword group contributes `weight × min(distinct matches, 3)`.
- `model_size` is 0 or 1, depending on whether `\b\d{1,4}(\.\d)?\s?b\b` or `\ba\d{1,3}b\b` matches.
- `family` counts distinct families, up to 3.
- `watch` scores 1 if the topic has any watch hit.
- Add `log1p(likes) × 0.5 + log1p(posts_count) × 0.25`.
- **Use:** this score orders digests and topic lists, and decides `relevant_only` filtering (with `relevance_override`). It never hides data.

## 4. Watched repos (`repos.py`)
**Purpose:** once you've settled on a serving stack, tell you when you should update it.
- Repos are discovered from forum posts (§1).
- You flag the few that matter with `watch_repo`. A repo can also be watched by URL if it hasn't been posted about yet.
- The tool then answers two questions:
  1. **Has the branch moved?** This is cheap and checked on every cycle.
  2. **What's new compared with what I have installed, or since a date or SHA?** This is computed when asked, from GitHub's compare API, and the agent reads the commit messages and release notes to judge whether an update is worthwhile.

The number of watched repos is expected to be small, from a handful to about 20. There's no commit filtering, README tracking or automatic watching.

### `check_repos(repo=None)`
This covers watched repos only.
- **Requests:**
  - GitHub REST API, plus `Authorization: Bearer $GITHUB_TOKEN` if it's set. It's optional: a handful of repos fits easily in the anonymous 60 requests/hour.
  - Every request is conditional, using the ETag stored for that endpoint; a 304 costs nothing.
  - The check stops cleanly when `X-RateLimit-Remaining` drops below 5.
- **For each watched repo:**
  1. **Branch head:** `GET /repos/{o}/{n}/commits/{branch}` with `Accept: application/vnd.github.sha`, which returns only the SHA.
     - If the SHA differs from `head_sha`, create a `head_moved` event with `prev_ref = old`, `ref = new`, the compare URL, and `commits_ahead` from the cached compare (below).
     - Then update `head_sha` and `head_seen_at`.
     - A 404 or 422 means the branch has gone: create a `branch_gone` event.
  2. **Releases and tags:** `GET /repos/{o}/{n}/releases/latest` creates a `release` event, with the notes cut to 2,000 characters; a 404 means no releases. `GET /repos/{o}/{n}/tags?per_page=10` creates a `tag` event for each new tag.
  3. **Repo metadata:** `GET /repos/{o}/{n}`, at most once a day. It refreshes `default_branch`, `archived` and `description`. `archived` turning true creates an `archived` event.
- **Baseline:** the first check after `watch_repo` records the current head, latest release and tags. It creates no events.

### Comparing against what's installed (`repo_status`)
1. **Recording an install:**
   - `set_installed(repo, ref, label='default')` resolves `ref` (a full or short SHA, a tag, or a branch name) to a full SHA, using `GET /repos/{o}/{n}/commits/{ref}`.
   - `installed_at` defaults to now, but can be given.
   - If you run a local patch on top of upstream, record the upstream SHA it's based on.
2. **Status:** `repo_status(repo, label='default', since?)` works out a **base**: the installed SHA, or `since` if it's given (a date or a SHA).
   - A date is resolved to the last commit before that date on the branch, using `GET /commits?sha=<branch>&until=<date>&per_page=1`.
   - The tool then calls `GET /repos/{o}/{n}/compare/{base}...{head_sha}`, and the response is cached in `compare_cache`.
3. **What it returns:**
   - `status` (`identical` / `ahead` / `behind` / `diverged`), `ahead_by` and `behind_by`;
   - the commit list: SHA, subject, author, date and URL, with the newest `max_compare_commits` listed and the total always given;
   - a summary of files changed, as the top changed directories with counts;
   - releases and tags published after the base commit's date, with their notes;
   - forum posts that mention this repo since the base date, from `repo_mentions` plus an FTS search for `owner/name`. **People reporting that a new commit breaks or fixes something is the most useful signal here.**
   - any open flags on the repo.
4. **Large comparisons:** GitHub's compare API lists at most 250 commits. Past that, the tool reports `ahead_by` and the newest 50 commits, plus `truncated: true`.

### Scheduling
- `sparkwatch repos check` runs under the lock.
- The timer runs it on every cycle.
- `sync_now(include_repos=True)` also runs it.

There's no LLM here. The agent judges whether an update is worthwhile (§5, `should_i_update`).

## 5. MCP server (`mcp_server.py`) — the primary interface
- Built with the official `mcp` SDK's `FastMCP`, over stdio.
- Started with `sparkwatch serve-mcp`.
- To register: `claude mcp add sparkwatch -- uv run --directory /home/simon/dev/nvidia-forum-monitor sparkwatch serve-mcp`.

### Design rules
- **Responses are compact and built for an agent:**
  - JSON with stable field names;
  - a forum `url` on every post or topic, as `https://forums.developer.nvidia.com/t/<topic_id>/<post_number>`;
  - dates on everything;
  - text truncated to `max_chars`, with `truncated: true` and a `next` argument set the agent can pass back to continue.
- `limit` defaults to `mcp.default_limit` and is capped at `max_limit`.
- **Tool descriptions are written for the agent.** Each one says when to use the tool. For example: "Call `get_notes` and `list_recipes` before researching a model, so you build on earlier conclusions."
- Writes are short transactions. Long jobs (sync, repos check) take the lock. If it's already held, they return `{"busy": true, "job": ..., "started_at": ...}` rather than blocking.

### Keeping up to date
- `sync_now(forum_id?, include_repos=True, time_budget_s=90)`: runs `sync()` in the server process, then `check_repos()` within the time that's left. It returns the `SyncResult`, with titles for new and updated topics, plus a list of watched repos whose head moved or that got a release. If it returns `partial: true`, the agent calls it again.
- `digest(since="cursor", cursor="default", relevant_only=False, limit=30, advance=True)`. This is the "since I last asked" view. `since` is `"cursor"` or an ISO date.
  - **Sections, in order:**
    1. Unacknowledged **watch sightings**, i.e. first sightings of an anticipated model.
    2. **Watchlist activity**: per tracked model, new hits since the cursor, each with a topic, post URL and a 300-character snippet.
    3. **New topics** (created since the cursor), by relevance: title, URL, OP excerpt, author.
    4. **Updated topics**: the number of new posts and the newest 3 post snippets. Topics are ordered by relevance × number of new posts.
    5. **Watched repos:** for each repo with events since the cursor, whether or not anyone has posted about it:
       - head moved, with the number of commits and the 5 newest commit subjects;
       - new releases or tags;
       - archived, or branch gone.
       If an install is recorded, it also shows "installed `abc1234`, now N commits behind; releases since then: …".
    6. **Repos first linked from posts** since the cursor, with their topic and context snippet. These are candidates for `watch_repo`.
    7. **Open flags.**
  - **Timing:** "since" is based on `posts.first_fetched_at` and `changed_at`, not post `created_at`, so a backfill doesn't flood the digest with old posts as if they were news. Backfilled posts are reported as a count only.
  - **Cursor:** with `advance=True`, the cursor moves to the time the digest was built.
- `whats_new_for(model, since?)`: the same as the digest, but narrowed to one model. It uses watch hits, an alias regex and FTS.
- `ack_watch_sighting(label)`

### Reading and research
- `search_posts(query, topic_id?, forum_id?, since?, username?, order="rank"|"recent", limit)`: FTS5 with `snippet()`.
- `find_model_mentions(model, since?, limit)`: resolves aliases and the watch regex, then returns matching posts grouped by topic, newest first, with snippets.
- `list_topics(forum_id?, since?, relevant_only=False, order="bumped"|"relevance"|"likes"|"created", limit)`
- `get_topic_overview(topic_id)`:
  - metadata, the OP (truncated), the date range and participant count;
  - `has_accepted_answer`;
  - linked repos and HF models;
  - watch hits;
  - recipes, benchmarks, notes and flags that cite this topic.
- `get_posts(topic_id, from_post?, to_post?, since?, last_n?, username?, max_chars?)`: the raw text of the selected posts.
  - Use `last_n` for "what's the current state of this thread".
  - Use `since` for catching up.
- `get_post(post_id, with_parent=True, with_replies=True)`
- `list_repos(watched?, order="mentions"|"recent_mention"|"first_seen", limit)`: repos linked from posts, with mention counts and the latest mention. Use it to find repos worth watching.
- `get_repo(repo)`: metadata, watch settings, installs, forum mentions with snippets, and recent events. `repo` is `owner/name` or a URL.
- `repo_status(repo?, label="default", since?)`: see §4. Without `repo`, it covers every watched repo, giving a one-line status each plus details for any with changes. **This answers "anything new since …?" and "should I update?"**
- `repo_events(since?, repo?)`: the raw event log.
- `check_repos_now(repo?)`
- `list_hf_models(limit)`: HF repos linked from posts, with mention counts. They're recorded only, never polled.
- `sql_query(sql, limit=200)`:
  - runs on a separate `file:…?mode=ro` connection;
  - only a single `SELECT` or `WITH` statement is accepted;
  - a 2-second progress-handler timeout applies;
  - results are capped at `limit` rows.

### Knowledge (agent write-back)
- `record_recipe(model, topology, status, evidence: [{post_id, quote}], quant?, quant_repo?, engine?, engine_version?, container_image?, tensor_parallel?, pipeline_parallel?, context_len?, tok_s_decode?, tok_s_prefill?, concurrency?, memory_gb?, setup?, gotchas?, supersedes?: recipe_id)`. On each call it:
  1. normalises the model, quant and engine;
  2. verifies every quote;
  3. computes the fit estimate;
  4. sets `reported_at` to the newest evidence post date.
  - If any quote fails verification, the recipe is still stored, but with `review='proposed'`.
  - The response includes the normalised values, the fit warning, which quotes were verified, and any similar existing recipes (same model, quant, engine and topology), so the agent can supersede instead of duplicating.
- `record_benchmark(model, benchmark, score, source_type, quant?, score_unit?, measured_on?, engine?, source_url?, evidence?: [{post_id, quote}], measured_at?)`: quotes are verified the same way when `source_type='forum'`.
- `list_recipes(model?, engine?, topology?, status?, since?, include_superseded=False, review="accepted", limit)`. Each recipe includes its evidence URLs.
- `list_benchmarks(model?, benchmark?, quant?, limit)`
- `model_summary(model)`. This is the starting point for a "best way to serve X" question. It returns:
  - the model's metadata;
  - its aliases;
  - mention counts over 7, 30 and 60 days;
  - its active recipes, newest first;
  - its benchmarks;
  - its latest research notes;
  - open flags;
  - repos mentioned in the model's topics (marking the watched ones, with their install status);
  - topics with the most mentions.
- `save_note(subject, body, model?, as_of, source_post_ids: [int], supersedes?: note_id)` and `get_notes(model?, subject_query?, include_superseded=False)`
- `flag(target_type, target_id, label, reason, priority=2)`, `list_flags(open_only=True)`, `resolve_flag(id, resolution)`
- `review_proposed(kind="recipe"|"benchmark"|"alias")`, `set_review(kind, id, review)`
- `add_model_alias(alias, canonical)`, `upsert_model(canonical, family?, params_total_b?, params_active_b?, released_at?, hf_repo_id?, note?)`
- `list_watchlist()` (with hit counts over 7 and 30 days, first-seen dates and the latest hit), `add_watch(label, match_regex, kind, canonical?)`, `update_watch(label, …)`
- `watch_repo(repo, watch=True, branch?, note?)`. `repo` is `owner/name` or a URL.
  - A repo that hasn't been seen yet is created with `origin='agent'`.
  - Watching a repo runs a baseline check straight away.
  - `watch=False` stops the checks but keeps the history.
- `set_installed(repo, ref, label="default", installed_at?, note?)`: records what you're running. `ref` can be a SHA (full or short), a tag or a branch, and it's resolved to a full SHA.
  - The response confirms the resolved SHA, the commit subject and date, and the current status against the branch head.
  - `clear_installed(repo, label)`
- `list_installs()`: every recorded install, with its current `ahead_by` against the branch head (from the cache, or computed).
- `mark_topic(topic_id, relevant: bool | None)`

### Resources
- `sparkwatch://watchlist`
- `sparkwatch://flags/open`
- `sparkwatch://notes/latest`

### Prompts
MCP prompt templates encode the two main workflows. They're short and instructional.
- **`catch_up`:**
  1. `sync_now` (repeat while `partial`)
  2. `digest`
  3. For promising items, read them with `get_posts(last_n=…)` / `get_post`.
  4. `flag` what matters: new models or quants, performance claims, breaking engine changes, new recipes, watch sightings.
  5. Record clear recipes and benchmarks with quotes.
  6. Reply with a short briefing, with links.
- **`serve_model(model, topology="dual-spark")`:**
  1. `model_summary`
  2. `get_notes`
  3. `whats_new_for(model, since=latest note's as_of)`
  4. Read the newest relevant posts first. Prefer recent reports, multiple users and accepted answers. Watch for contradictions and for which engine versions were used.
  5. Answer with the recommended quant, engine and version, container, launch command and flags, expected tok/s, gotchas, alternatives, and a source link and date for each claim.
  6. Then `record_recipe` / `record_benchmark` and `save_note` (superseding the previous note).
- **`should_i_update(repo?, label="default")`:**
  1. `repo_status` (for all watched repos if none is given).
  2. For each repo that's behind, read the commit subjects and release notes. Pick out fixes and features relevant to GB10/Spark, your models (watchlist and recipes) and your engine. Also note breaking changes, dependency or CUDA bumps, and reverts.
  3. Read the forum posts that mention the repo since the installed date. Look for reports that newer commits fix or break things.
  4. Recommend one of: update now to `<sha or tag>`, wait for `<reason>`, or no relevant changes. Give the reasoning and links.
  5. `flag` anything urgent, and `save_note(subject="update check <repo>")`.
- **`frontier(topology="dual-spark")`:** "what are the strongest models people run on two Sparks right now?"
  1. `list_watchlist`
  2. `list_recipes(status=works, since=60d)`
  3. `list_benchmarks`
  4. `get_notes`
  5. Fill gaps with `find_model_mentions` for heavily mentioned models that have no recipes yet.
  6. Answer with a ranked list and its reasoning, showing quant and benchmark evidence.
  7. `save_note(subject="frontier dual-spark")`.

## 6. Run B: local-LLM triage (`triage.py`, `llm.py`), phase 2
**Purpose:** lower agent token use and pre-flag items without supervision. Everything works without it.
- **Migration 2:**
  - `post_tags(post_id PK, content_hash, models JSON, has_perf INT, has_benchmark INT, has_recipe INT, is_question INT, is_solution INT, summary TEXT, triage_model, prompt_version, tagged_at)`
  - `triage_runs`
- **`sparkwatch triage`:**
  - covers posts in relevant topics, or with watch hits, where `post_tags` is missing or `post_tags.content_hash != posts.content_hash` (`--reprocess` after a prompt change);
  - chunks run by topic up to `chunk_tokens`, with the OP as context;
  - uses an OpenAI-compatible endpoint with structured JSON-schema output and Pydantic validation, plus one retry.
- **Optional:** candidate recipes and benchmarks go in with `created_by='triage', review='proposed'`, and quotes are verified the same way. The agent confirms them with `review_proposed`.
- **MCP integration:** `digest`, `get_posts` and `search_posts` include `summary` and tags when present, and accept filters such as `has_perf=True`.
- **Rules:**
  - extract only what is stated, never infer numbers;
  - prompts live in `prompts/`;
  - bump `prompt_version` when a prompt changes.
- **Scheduling:** the timer runs `sync → repos check → triage`.

## 7. Run C: optional extras
- **Leaderboard** (`rank.py`): a deterministic score over accepted recipes and benchmarks. Design it only after Run A/B usage shows the agent's `frontier` answers need a stable baseline. The quant penalty and benchmark normalisation are decided then.
- **Scheduled report** (`report.py`): a timer runs a headless agent, or the triage LLM, through the `catch_up` workflow, and writes markdown to `reports/`.
- **Web search** (`websearch.py`): the Brave Search API (`X-Subscription-Token: $BRAVE_API_KEY`), cached in `web_cache` for 7 days. Exposed as MCP `web_search(query)` for finding model cards and benchmark tables when the agent's own web tools are blocked. Results feed into `record_benchmark(source_type='model_card'|'leaderboard')`.
- **Other sources:** if Spark Arena or similar sites have an API or export, ingest them as an additional `source_type`. Check this during Run C, and don't assume an endpoint exists.

## Testing approach
- `scripts/record_fixtures.py` is run **once**. It saves a small live sample to `tests/fixtures/`:
  - one listing page from each category;
  - 3 topics, including a long one, with their `posts.json` batches;
  - GitHub responses for one repo: commit SHA, releases/latest, tags, metadata, a compare between two SHAs, and commits?until=.
- An `edited_post.json` variant has its `raw` changed and `version` + 1.
- Tests never touch the network. `respx` mocks Discourse and GitHub.
- MCP tools are tested by calling the tool functions directly against a temporary database.

## Build plan
**Run A (MVP, no LLM):**
1. Scaffold:
   - `uv init --package --name sparkwatch`
   - dependencies, config, `db.py` with migration 1, the seed loader
   - the CLI skeleton, `lock.py`
2. `discourse.py`, `sync.py` (incremental, backfill, time budget), `record_fixtures.py`.
3. FTS triggers, `links.py`, `watch.py`, `filters.py`, `normalize.py`, `fit.py`, `evidence.py`.
4. `repos.py`: head checks, releases and tags, installs, compare with caching.
5. `digest.py` and all MCP tools, resources and prompts in §5.
6. `deploy/` systemd user units, and a README covering install, MCP registration and the timer.

**Checkpoint:**
1. Sync the default 60-day window live.
2. Use it for real through Claude Code:
   - run `catch_up`;
   - run `serve_model("GLM 5.3 Flash")`;
   - fix the watch regexes and canonical names;
   - `list_repos` → `watch_repo` the stack you run;
   - `set_installed` with your current SHAs;
   - run `should_i_update`.
3. Note where agent token use hurts. That decides how much triage is worth.

**Run B:** triage (§6).

**Run C:** optional extras (§7), as usage shows they're needed.

## Verification
**Unit tests** (`uv run pytest`):
- Sync:
  - listing pagination stops at the cutoff, and pinned topics don't stop it early;
  - a second sync with unchanged fixtures adds no posts;
  - the edited-post fixture updates `content_hash` and `changed_at`;
  - a time-budget stop returns `partial` without advancing `last_synced_at`, and a second call completes the sync;
  - backfilling from 30 to 60 days fetches only posts between the old and new floors, and they appear in the digest as a count, not as news.
- FTS: an inserted or updated post becomes searchable.
- Links: normalisation table tests, e.g. `github.com/Foo/Bar.git/tree/main#x` → `https://github.com/foo/bar`.
- Watch:
  - the seed regexes match sample strings (`GLM-5.3-Flash`, `glm 5.3 flash`, `DeepSeek-V4.1-Flash`, `Qwen3.8-Flash-Next`, `Qwen4-72B`);
  - `qwen4` doesn't match `Qwen3.8` or `qwen 4.5`;
  - the first hit for an anticipated model sets `first_seen` and appears at the top of the digest until it's acknowledged.
- Digest: the cursor advances; a digest run immediately after another returns empty sections.
- `record_recipe`:
  - a fabricated quote gives `review='proposed'`;
  - a similar existing recipe is returned;
  - `supersedes` sets `superseded_by`.
- Fit check: Qwen3-235B-A22B at nvfp4 is about 145 GB, so it gets a warning on single-spark and none on dual-spark.
- Repos:
  - link discovery never sets `watch`;
  - the first check after `watch_repo` is a baseline with no events;
  - a changed head SHA creates exactly one `head_moved` event, with `prev_ref`;
  - an unchanged repo returns 304s and creates no events;
  - a watched repo with no forum mentions still appears in the digest when its head moves.
- Installs:
  - `set_installed` with a short SHA or a tag resolves to the full SHA;
  - `repo_status` reports `ahead_by`, commits and releases since the install, and forum mentions since `installed_at`;
  - a second call is served from `compare_cache`, with no compare request;
  - `since=<date>` resolves to the right base commit.
- `sql_query` rejects `DELETE`, multiple statements and `PRAGMA`.

**Live smoke test:**
1. `uv run sparkwatch sync --forum 723 --max-pages 1`.
2. Run it again immediately; it should make only about 1–2 requests.
3. `uv run sparkwatch digest` shows the new topics.

**MCP smoke test:**
1. `uv run mcp dev src/sparkwatch/mcp_server.py`: call `sync_now`, `digest`, `find_model_mentions("GLM 5.3 Flash")` and `model_summary`.
2. In Claude Code, use the `catch_up` prompt, then ask: "What's the best way to serve GLM 5.3 Flash on two Sparks today?" Check that it records a recipe and a note with verified quotes.
3. Ask the same question again: the second answer should start from the saved note and only read posts added since.
