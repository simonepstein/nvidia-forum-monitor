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
- **Dependencies:** `httpx`, `tenacity`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `mcp<2` (FastMCP is `mcp.server.fastmcp` in 1.x; 2.x renamed it to `MCPServer`), `selectolax` (`selectolax.lexbor`).
- **Dev dependencies:** `pytest`, `respx`, `ruff`, `pyyaml` (eval cases only).
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
weights = { dual = 3.0, engine = 1.5, quant = 1.5, perf = 1.0, bench = 1.5, model_size = 1.0, family = 1.0, watch = 4.0 }
families = ["qwen", "llama", "deepseek", "gpt-oss", "glm", "kimi", "mistral", "gemma", "phi", "nemotron", "minimax", "hunyuan", "granite"]

[relevance.keywords]
dual = ["dual spark", "two sparks", "2x spark", "2 sparks", "stacked", "cluster", "connectx", "qsfp", "roce", "nccl", "tensor parallel", "tp=2", "tp 2", "pipeline parallel", "multi-node", "ray"]
engine = ["vllm", "sglang", "trt-llm", "tensorrt-llm", "llama.cpp", "llama-server", "ollama", "lm studio", "exllama", "nim"]
quant = ["nvfp4", "fp4", "fp8", "mxfp4", "int4", "awq", "gptq", "gguf", "q4_k_m", "q8_0", "exl2", "exl3"]
perf = ["tok/s", "tokens/s", "t/s", "tps", "prefill", "decode", "benchmark", "throughput", "latency"]
bench = ["humaneval", "mbpp", "livecodebench", "swe-bench", "aider", "bfcl", "tau-bench", "tau2", "gpqa", "mmlu", "aime", "ifeval", "ruler"]

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
- **Rate safety (revised 2026-11-08):** when `forum.base_url` is the live forum host, settings validation requires `0 < requests_per_second <= 1.0`, and sparkwatch refuses to start otherwise. Zero or a negative value never means "unlimited".
  - Tests and evals that need speed mock the network, or point `base_url` at a non-live host. They never just raise the rate.
  - The GitHub client paces requests at 1 per second as well.
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
     - on the first run, during a backfill, or **while `forums.listing_floor` is later than the window floor** (a coverage gap), it is the window floor.
   - Topics bumped before the floor are not upserted.
   - **Listing coverage (revised 2026-11-08):** `forums.listing_floor` is the oldest `bumped_at` back to which the listing has been read without interruption, starting from page 0.
     - **Reached the cutoff:** if the listing reaches its cutoff, or runs out of pages, `listing_floor` becomes the window floor (or stays earlier, if it already was).
     - **Stopped early:** if it stops first, because of `max_pages` or the time budget, `listing_floor` becomes the oldest non-pinned `bumped_at` actually listed, and **`last_synced_at` is not advanced**. The next sync carries on paging back until the gap is closed.
     - **Topics found while closing a gap** are queued by the normal rules in step 3: new or changed topics only. Closing a gap never re-queues topics that are already complete.
     - **The two floors:** `backfill_floor` means the window applied to *posts*, and changes only when `lookback_days` grows. `listing_floor` means the window the *listing* has covered.
   - **Repair is explicit.** `sparkwatch sync --repair-coverage --forum ID` sets that forum's `listing_floor` to the oldest non-pinned `bumped_at` stored for it, and reports the old and new values. It runs for the named forum only, never automatically. An automatic repair would invent gaps on fully synced forums: on 2026-11-08, forum 721's oldest stored topic was 75 minutes later than its true floor.
3. **Queue topics:** a topic is queued if it is new, if `highest_post_number` went up, if `bumped_at` changed, if this is a backfill, or if `topics.posts_complete` is still 0.
   - `posts_complete` is set to 1 when a topic's fetch ran to its natural end (every target batch older than the floor was skipped), and left at 0 when the time budget interrupted it.
   - That is what makes a partial run resume: the topic is not re-queued once it is complete, and posts outside the window never force a re-fetch.
4. **Fetch each queued topic:**
   1. Call `get_topic`, which gives the first 20 posts plus `stream`.
   2. **Target IDs** = IDs in `stream` that aren't stored yet, plus stored posts created within the last `refetch_recent_days`. IDs in `missing_posts` are excluded, unless their re-check is due (see below).
   3. Fetch target batches **from newest to oldest**, and stop once a whole batch is older than the floor. The OP is always kept.
   4. Upsert posts with `content_hash = sha256(raw)`.
   5. **Tombstones:** a requested ID that `posts.json` doesn't return is upserted into `missing_posts` with `reason='absent_from_batch'`.
      - If a batch fails with 403, record `forbidden`. If the post is returned with `deleted_at` set or `hidden` true, record `deleted` or `hidden`.
      - **Posts that aren't content (revised 2026-11-08):** posts with `post_type` 3 or 4 (small actions and whispers, e.g. spam-scanner notices) are recorded as `not_content`, so they're never requested again. They're excluded from `tombstones_new` and from any count of missing content.
      - An ID that later comes back is removed from `missing_posts` and stored normally.
      - **Re-check policy:** tombstoned IDs are requested again only during a backfill, or when `last_checked_at` is more than 30 days old. Each re-check updates `last_checked_at`. Deleted posts are occasionally restored, so this keeps re-requests rare without losing anything permanently.
   6. Run `links.discover` and `watch.match` for each post that was inserted or changed, then recompute the topic's `relevance_score`.
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
  - Skip `gist.github.com` URLs entirely; they aren't repos.
  - When GitHub answers a repo request with `301 Moved Permanently`, follow it, and update the repo's `url`, `owner` and `name` to the new canonical names.
- **Hugging Face:** `huggingface\.co/([\w.-]+)/([\w.-]+)`. Skip first path segments `datasets|spaces|docs|blog|papers`.
  - HF links are **recorded only**, so the agent can see which quant repo a recipe uses.
  - HF is never polled. **The forum is the curator for new models.**
- **Mentions:** upsert into `repos` or `hf_models`, plus the matching `*_mentions` row. The context snippet is ±150 characters around the URL.
- **Repos are discovered from posts.** Discovery never watches a repo; you choose which repos to watch (§4).

### Watch matching (`watch.py`)
- For each active `watchlist` row, run `re.search(match_regex, raw, re.I)` against the title and raw text of each new or changed post.
- On a match, insert a row into `watch_hits(watch_id, post_id)`. These are unique, so re-matching an edited post is a no-op.
- The first hit for an entry sets `watchlist.first_seen_post_id` and `first_seen_at`.
- **Anticipated entries (revised 2026-11-08).** Speculation about a much-awaited model is constant, so a first *mention* says little. An anticipated entry may therefore have a `release_regex`, which is stricter than `match_regex`, e.g. a Hugging Face URL or phrases like "weights are out".
  - **With a `release_regex`:** only a post matching it raises the sighting alert (`sighting_post_id`, `sighting_at`). After `ack_watch_sighting`, the next release match raises a new alert.
  - **Without one:** the first hit raises the alert, as before.
  - **Every other hit is still recorded,** and appears in the digest only as a count.
  - **The judgement belongs to the agent:** `catch_up` tells it to classify anticipated-model hits as rumour or release, and to `flag` only releases.
- An alert appears at the top of every digest until the agent acknowledges it with `ack_watch_sighting`.
- When a watch entry is added or its regex changes, it is matched against all stored posts once. That's local only, with no fetching.

## 2. SQLite schema (`db.py`, migration 1)
Every write is an idempotent upsert keyed on natural or Discourse IDs.
- **Connection settings:** `PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000`.

```sql
-- Mirror
CREATE TABLE forums (id INTEGER PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
  last_synced_at TEXT, backfill_floor TEXT);
  -- migration 3 adds: listing_floor TEXT (see §1, Listing coverage); it starts as a copy of backfill_floor
CREATE TABLE topics (
  id INTEGER PRIMARY KEY, forum_id INTEGER NOT NULL REFERENCES forums(id),
  slug TEXT, title TEXT NOT NULL, tags TEXT, excerpt TEXT,
  created_at TEXT, bumped_at TEXT, last_posted_at TEXT,
  posts_count INTEGER, highest_post_number INTEGER, views INTEGER, like_count INTEGER,
  pinned INTEGER DEFAULT 0, has_accepted_answer INTEGER DEFAULT 0,
  relevance_score REAL DEFAULT 0, relevance_override INTEGER,   -- NULL auto, 1 force-relevant, 0 force-irrelevant
  posts_complete INTEGER NOT NULL DEFAULT 0,   -- migration 2: 0 = fetch was interrupted, 1 = ran to its natural end
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
CREATE TABLE missing_posts (       -- tombstones: IDs in a topic's stream that couldn't be fetched
  topic_id INTEGER NOT NULL REFERENCES topics(id), post_id INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('absent_from_batch','deleted','hidden','forbidden','not_content')),  -- 'not_content' added in migration 3
  first_seen_at TEXT NOT NULL, last_checked_at TEXT NOT NULL,
  PRIMARY KEY (topic_id, post_id));
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
  -- migration 3 adds: release_regex TEXT, sighting_post_id INTEGER, sighting_at TEXT (see §1, Watch matching)
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

**Seeding:** `seed/*.toml` is loaded on every run. The loader reads before it writes and only writes when a seed row is missing or its seeded fields differ, so a steady-state open issues no writes at all — that is what keeps MCP read tools responsive while a sync writes. Rows the agent wrote are never overwritten. `watchlist.first_seen_*` and `sighting_acked_at` are never reset. The same read-before-write rule applies to `forums`.

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
match_regex = 'qwen[\s._-]*4(?![\d.])(?![\s_-]*exp)(?!\s*b\b)'   # excludes vLLM's Qwen4Exp / qwen4_exp (Qwen3.8 Flash Next's architecture) and "Qwen 4B"
release_regex = 'huggingface\.co/qwen/qwen[\s._-]*4(?![\d.])|qwen[\s._-]*4\b[^\n]{0,40}\b(released|weights (are )?(out|available)|now available|is out)'
```
- `seed/model_aliases.toml` and `seed/models.toml` start with these four models.
- Exact canonical names, HF repo IDs and variant naming are confirmed against real posts at the Run A checkpoint.
- `seed/benchmarks.toml` starts empty.

## 3. Normalisation and helpers (`normalize.py`, `fit.py`, `evidence.py`, `filters.py`)
These are deterministic helpers, used by the agent write tools and later by triage.

### Model names
1. Look up the alias in `model_aliases`, lowercased, with `-`, `_` and spaces collapsed to `-`. Dots are preserved in canonical names because they carry version numbers (`glm-5.3-flash`); a second lookup with dots also collapsed (`glm-5-3-flash`) is tried before giving up, which is what makes `GLM 5.3 Flash` and `glm_5_3_flash` resolve to the same model.
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
   - `status` (`identical` / `behind` / `ahead` / `diverged`), `behind_by` and `ahead_by`. **These are always from the installed (base) SHA's point of view:**
     - `behind_by` = commits on the branch head that the install doesn't have, which is GitHub compare's `ahead_by`;
     - `ahead_by` = commits in the install that aren't on the branch, which is GitHub's `behind_by`;
     - the digest and `list_installs` use the same names and direction.
   - the commit list: SHA, subject, author, date and URL, with the newest `max_compare_commits` listed and the total always given;
   - a summary of files changed, as the top changed directories with counts;
   - releases and tags published after the base commit's date, with their notes;
   - forum posts that mention this repo since the base date, from `repo_mentions` plus an FTS search for `owner/name`. **People reporting that a new commit breaks or fixes something is the most useful signal here.**
   - any open flags on the repo.
4. **Large comparisons:** GitHub's compare API lists at most 250 commits. Past that, the tool reports `behind_by` and the newest 50 commits, plus `truncated: true`.

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
  - Two tools keep literal defaults on purpose: `digest` caps each section at 30 and `sql_query` returns at most 200 rows. Those are per-call shape decisions, not list defaults, so `mcp.default_limit` does not apply to them.
- **Size budget (revised 2026-11-08):** a default call to any tool should return well under 10 KB.
  - Lists are ranked and capped, with a `total` count.
  - `model_summary` lists at most 10 repos, ranked by mentions in the model's topics, with watched repos first.
  - `model_summary` includes open flags on the model, its recipes and its topics.
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
  - **Timing (revised 2026-11-08).** Three kinds of post are distinguished:
    - **new:** `first_fetched_at >= since` and `created_at >= since - 24h`. The 24-hour grace covers posts written shortly before a sync and picked up by the next one.
    - **edited:** `changed_at >= since` and `first_fetched_at < since`.
    - **backfilled:** `first_fetched_at >= since` and `created_at < since - 24h`.
  - **How each kind is reported:** sections 2–4 count and show only *new* posts, with edits as a separate count. Backfilled posts appear as a single line, `backfilled: N posts in M topics`. A topic made up only of backfilled posts isn't listed as new or updated.
  - **Item dates:** every item's `date` is the post's `created_at`.
  - **Snippets:** watch-hit snippets are taken from around the matched text, not from the start of the post.
  - **Size:** the default digest should be well under 10 KB. Section caps and snippet lengths are chosen to meet that. "Repos first linked" is one line per repo.
  - **Anticipated models:** in section 2, hits for an anticipated model (`kind='anticipated'`) are shown as a count, plus the 3 newest hits.
  - **Cursor:** with `advance=True`, the cursor moves to the time the digest was built.
- `whats_new_for(model, since?)`: the same as the digest, but narrowed to one model. It uses watch hits, an alias regex and FTS. `since` applies to **every** section, using the new, edited and backfilled definitions above.
- `ack_watch_sighting(label)`: clears the current alert. An anticipated entry with a `release_regex` fires again on the next *release* match (see §1, Watch matching).

### Reading and research
- `search_posts(query, topic_id?, forum_id?, since?, username?, order="rank"|"recent", raw_fts=False, limit)`: FTS5 with `snippet()`.
  - **Query handling (revised 2026-11-08):** by default, `query` is plain text. Each whitespace-separated term is quoted as an FTS5 string, and the terms are joined with AND. So `GLM 5.3 Flash` and `tok/s` work as typed.
  - `raw_fts=True` passes the query through unchanged, for FTS5 operators. A syntax error then comes back as a tool error.
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
  - mention counts over 7, 30 and 60 days. These count **distinct posts by `posts.created_at`**, not watch-hit rows or when sparkwatch recorded them. The same rule applies to `list_watchlist`'s `hits_7d` and `hits_30d`.
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

### Workflows: prompts plus tool and instruction fallbacks (revised 2026-11-08)
MCP prompts are controlled by the user, and client support varies. Even where a client shows them, the model can't call them. OpenFox, for example, doesn't show them at all. So each workflow below is published through **four channels, all built from a single source** (e.g. `workflows.py`):
1. **MCP prompts**, as below.
2. **Tools:** `list_workflows()` returns each name with a one-line purpose and its arguments. `get_workflow(name, args?)` returns the step text, with the arguments filled in.
3. **Server instructions:** `FastMCP(instructions=…)` gets a short paragraph naming the four workflows and saying "call `get_workflow(name)` for the steps". Many clients put this in the model's context.
4. **Tool descriptions:** `sync_now`, `digest`, `model_summary` and `repo_status` mention the relevant workflow, e.g. "For a full catch-up, call `get_workflow('catch_up')`."

The README gets a "Workflows" section for people and for clients that show none of these.

### Prompts
MCP prompt templates encode the main workflows. They're short and instructional.
- **`catch_up`:**
  1. `sync_now` (repeat while `partial`)
  2. `digest`
  3. For promising items, read them with `get_posts(last_n=…)` / `get_post`.
  4. `flag` what matters: new models or quants, performance claims, breaking engine changes, new recipes, watch sightings. For anticipated models, classify each hit as rumour or release, and flag only releases.
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
- **Migration 2 (Run A):** `ALTER TABLE topics ADD COLUMN posts_complete INTEGER NOT NULL DEFAULT 0;` — the sync resume marker above.
- **Migration 3 (Run A fixes, 2026-11-08):**
  - `forums.listing_floor`, initialised from `backfill_floor`;
  - `watchlist.release_regex`, `sighting_post_id` and `sighting_at`;
  - rebuild `missing_posts` to add `'not_content'` to the CHECK, then reclassify existing rows whose post has `post_type` 3 or 4.

  The migration must not touch any coverage value other than initialising `listing_floor`, which keeps 721 out of any re-sync.
- **Migration 4 (Run B):**
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
- `scripts/record_fixtures.py` is run **once**, at the start of Stage 2. That's a single approved live session.
  - It goes through `DiscourseClient` itself, so it uses the real rate limiter and User-Agent, not ad-hoc curl.
  - GitHub requests are anonymous; no token is needed.
  - It saves a small live sample to `tests/fixtures/`:
    - one listing page from each category;
    - 3 topics with their `posts.json` batches. One is long: topic **381350**, which has about 956 posts and so would need about 20 batch requests. The recorder keeps the newest 3 batches of it (and the full `stream`), because the tests need the pagination shape, not the whole thread.
    - GitHub responses for one repo: commit SHA, releases/latest, tags, metadata, a compare between two SHAs, and commits?until=. Use a small repo linked from the recorded posts if there is one, otherwise `vllm-project/vllm`.
  - Fixtures are committed. It's all public forum content, but trim each topic to the posts the tests actually need, so the repo doesn't carry large JSON files. Keep the full `stream` array, so the pagination logic is still exercised.
  - The recorder is never run as part of the test suite.
- An `edited_post.json` variant has its `raw` changed and `version` + 1.
- **Hand-made fixtures for edge cases** (`tests/fixtures/synthetic/`), built by editing recorded responses:
  - a topic whose `stream` contains the ID of a deleted post, which `posts.json` leaves out;
  - a post edited in the middle of a thread, not just the newest one;
  - a deleted or private topic, returning 404 and 403;
  - a 429 response with `Retry-After`, followed by a 200;
  - a 5xx that still fails after `max_retries`;
  - for GitHub: a 304 response, a 404 for a branch that's gone, and a compare with more than 250 commits.
- Tests never touch the network by default. `respx` mocks Discourse and GitHub, and a `conftest.py` guard makes any request that hasn't been mocked fail the test.
- Most MCP tools are tested by calling the tool functions directly against a temporary database. A small set of **protocol tests** also run the real server (see Verification).
- **Opt-in live tests** (`tests/live/`, marked `@pytest.mark.live`) are skipped unless `SPARKWATCH_LIVE=1` is set.
  - They make a handful of polite requests (one listing page, one topic, one GitHub head check).
  - They check that the fields the code depends on still exist and have the expected types.
  - When they fail, the API has drifted. Re-record the fixtures with `record_fixtures.py`.
  - They are never run in CI.
- **Scripted agent evals:** a frozen database snapshot in `tests/evals/`, built from fixtures by `tests/evals/build_db.py`, with `tests/evals/cases.yaml` and `tests/evals/run.py`. Running them needs an agent and API access, so they're manual for the MVP. The harness is described under Verification.
- **CI:** GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request:
  1. `astral-sh/setup-uv`
  2. `uv sync --locked`
  3. `uv run ruff check`
  4. `uv run ruff format --check`
  5. `uv run pytest -m "not live"`

  It uses a single Python 3.12 job, with no secrets and no network access beyond installing dependencies.

## Build plan
**Run A (MVP, no LLM).** The stages below are numbered 1–7. **There are review pauses after Stage 2 and after Stage 5.** These are where rework is most likely. The build stops at each one and reports to the user before carrying on.
1. Scaffold:
   - `uv init --package --name sparkwatch`
   - dependencies, config, `db.py` with migration 1, the seed loader
   - the CLI skeleton, `lock.py`
2. `record_fixtures.py` (run once, live), `discourse.py`, `sync.py` (incremental, backfill, time budget, tombstones).
   - **⏸ Review pause 1:** report all of the following:
     - `uv run pytest` passing, including the full Discourse edge-case matrix;
     - a live `sparkwatch sync --forum 723 --max-pages 1`, with its request count;
     - an immediate second run making about 1–2 requests;
     - row counts for topics, posts and `missing_posts`.
3. FTS triggers, `links.py`, `watch.py`, `filters.py`, `normalize.py`, `fit.py`, `evidence.py`.
4. `repos.py`: head checks, releases and tags, installs, compare with caching.
5. `digest.py` and all MCP tools, resources and prompts in §5.
   - **⏸ Review pause 2:** report all of the following:
     - the `list_tools`, `list_prompts` and `list_resources` output;
     - the protocol tests passing;
     - a real Claude Code run of `catch_up` and `serve_model("GLM 5.3 Flash")` against a live 60-day sync.

     This is also the Run A checkpoint below: fix the watch regexes and canonical names here, before Stages 6–7.
6. `deploy/` systemd user units, and a README covering install, MCP registration and the timer.
7. `.github/workflows/ci.yml`, the MCP protocol tests and the live test suite. The eval harness can come at the checkpoint.

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
- Evals check cited URLs too: every recipe evidence row must map to a real post, the answer must contain a forum post URL, and it must cite the evidence URLs.
- Discourse edge cases:
  - a deleted post in `stream` is skipped without an error and recorded in `missing_posts`;
    - later syncs don't request it again;
    - a backfill, or a `last_checked_at` older than 30 days, does request it again;
    - if it comes back, it's removed from `missing_posts` and stored;
  - a 404 or 403 topic is logged, skipped, and doesn't stop the sync;
  - a 429 with `Retry-After` waits for the stated time, then succeeds;
  - a 5xx that persists past `max_retries` fails that topic only;
  - an edit in the middle of a thread is caught by the `refetch_recent_days` re-fetch when it's in the window. A test documents that edits older than the window are *not* caught.
- GitHub edge cases: a 304 creates no events, a branch that's gone creates a `branch_gone` event, and a compare with more than 250 commits returns `ahead_by` with `truncated: true`.
- Locking and concurrency:
  - while one process holds the write lock, `sync_now`, `check_repos_now` and the CLI `sync` return or exit `busy` straight away;
  - while a sync is writing (simulated with an open write transaction on a second connection), MCP read tools still return results, because WAL mode allows it, and `sql_query` doesn't block;
  - a process that crashes mid-sync leaves no stale lock, because `fcntl` locks are released when the process exits.
- MCP protocol (`tests/test_mcp_protocol.py`) starts `sparkwatch serve-mcp` over stdio with the `mcp` client SDK, against a temporary database. The committed snapshot is `tests/snapshots/tools.json`; regenerate it with `SPARKWATCH_WRITE_SNAPSHOT=1 uv run pytest tests/test_mcp_protocol.py`.
  - `list_tools` returns every tool in §5, and each input schema has the documented required and optional arguments. A snapshot file catches accidental renames.
  - `list_prompts` and `list_resources` return the documented items.
  - Calling `get_posts` with a small `max_chars` returns `truncated: true` and a `next` argument set. Passing `next` back returns the rest, with no overlap and no gaps.
  - Errors (an unknown topic ID, a bad SQL statement) come back as MCP tool errors (`isError: true`) with a clear message, not as a crashed server: `get_posts` and `sql_query` raise, and the SDK turns that into a tool error.

**Live smoke test:**
1. `uv run sparkwatch sync --forum 723 --max-pages 1`.
2. Run it again immediately; it should make only about 1–2 requests.
3. `uv run sparkwatch digest` shows the new topics.

**MCP smoke test:**
1. `uv run mcp dev src/sparkwatch/mcp_server.py`: call `sync_now`, `digest`, `find_model_mentions("GLM 5.3 Flash")` and `model_summary`.
2. In Claude Code, use the `catch_up` prompt, then ask: "What's the best way to serve GLM 5.3 Flash on two Sparks today?" Check that it records a recipe and a note with verified quotes.
3. Ask the same question again: the second answer should start from the saved note and only read posts added since.

**Scripted agent evals** (manual for the MVP, `uv run python tests/evals/run.py`):
- A frozen database built from fixtures, plus a YAML file of cases. Each case has a prompt, such as `catch_up` or `serve_model("GLM 5.3 Flash")`, and the facts expected in the answer and in the database afterwards. For example:
  - a recipe recorded for the model, with `topology='dual-spark'` and verified quotes;
  - a note saved;
  - the cited post URLs.
- The runner drives Claude Code headless (`claude -p`) with only the sparkwatch MCP server enabled. It then checks the database and the answer text against the expected facts.
- Any other agent can drive the same cases: register the server with `SPARKWATCH_GENERAL__DB_PATH` pointing at `tests/evals/frozen.db`, run the workflows by hand, then verify with `run.py --check-only --answers <file>`, which skips the agent run and checks the database plus the supplied answers. `tests/evals/pause2_report.md` records a checkpoint run done that way.
- Use it after changing tool descriptions or prompts, to catch regressions in how the agent uses the tools. It isn't run in CI because it needs API access.
