# sparkwatch — DGX Spark forum monitor

A local data layer plus an **MCP server** that keeps an agent up to date with the NVIDIA DGX Spark / GB10 forums (Discourse categories 721 and 723), and with the GitHub repos you've chosen to watch. The agent uses it to answer questions like "what's the best way to serve GLM 5.3 Flash on two Sparks today?"
- sparkwatch fetches the data, mirrors it, works out what has changed, watches for specific models, and stores the agent's findings.
- The agent does the reading and judging.

**SPEC.md is the source of truth.** It covers the architecture, schema, MCP tools and build plan.
- **Run A:** the MVP, with no LLM.
- **Run B:** local-LLM triage.
- **Run C:** optional extras.

Update SPEC.md when a design decision changes.

## Tooling
- Python 3.12+ with a `src/` layout (`src/sparkwatch/`).
- **uv only:** `uv add <pkg>` (`--dev` for dev dependencies) and `uv run …`. Never use pip, and never edit dependencies in `pyproject.toml` by hand.
- Commands:
  - `uv run pytest`: tests
  - `uv run ruff check` and `uv run ruff format`: lint and format
  - `uv run sparkwatch sync | backfill | repos check | digest | serve-mcp`
  - `uv run mcp dev src/sparkwatch/mcp_server.py`: MCP Inspector
- MCP tools and CLI commands call the same library functions. Don't duplicate logic between them.

## Scraping rules
- The forum is **Discourse**. Use the JSON endpoints, adding `.json` to URLs:
  - `/c/<path>/<id>.json?order=activity&page=N`
  - `/t/<id>.json?include_raw=true`
  - `/t/<id>/posts.json?post_ids[]=…&include_raw=true` (up to 50 IDs per call)
- There's no need for per-post `/raw/` calls or a headless browser.
- Be polite:
  - at most 1 request per second;
  - a generic `User-Agent: sparkwatch/0.1` with no personal details;
  - honour 429 / `Retry-After`, with backoff via tenacity.
- During development, don't hammer the live forum or GitHub.
  - Record a sample once, with `scripts/record_fixtures.py`, into `tests/fixtures/`.
  - Tests use `respx` and never touch the network.
  - Live runs use `--max-pages` limits.
- AI-provider fetch tools may be blocked for this site. Use `curl`/`httpx` directly.
- The default lookback is **60 days**. Widening it moves `forums.backfill_floor` back, so it never re-crawls from scratch.

## Repo monitoring
- Repos are **discovered from forum posts**, but discovery never watches a repo. You flag the few that matter with `watch_repo`, which also accepts a URL.
- **Purpose:** knowing when to update a serving stack you've settled on.
  - The cheap check is "has the branch head moved?", plus new releases and tags, using ETag conditional requests.
  - `set_installed` records the SHA you run. `repo_status` compares it with the branch head through GitHub's compare API, cached by SHA pair, and also finds forum posts mentioning the repo since the install.
- The first check of a newly watched repo records a **baseline**, not a backlog of events.
- **Hugging Face is never polled.** HF links in posts are recorded only, because the forum is the curator for new models.

## Data rules
- **SQLite is the single source of truth.** It holds the mirror, watch hits, repo events and installs, and everything the agent writes back: recipes, benchmarks, research notes and flags.
- Posts carry `content_hash`, `first_fetched_at` and `changed_at`. The digest keys on when sparkwatch *saw* a change, not on post `created_at`, so backfills aren't reported as news.
- All writes are idempotent upserts keyed on Discourse or GitHub IDs.
- Schema changes go through numbered migrations in `db.py` using `PRAGMA user_version`. Never edit an applied migration.
- Use WAL mode. Long write jobs take the `lock.py` lock, and return `busy` rather than blocking.
- Quotes behind recipes and benchmarks are checked against the post's `raw`. An unverified quote gives `review='proposed'`.
- Seed files never overwrite rows the agent has written.

## MCP design
- Tool responses are compact JSON, with a forum or GitHub URL and a date on every item.
- Long text is truncated with a `next` argument set the agent can pass back to continue.
- Tool descriptions tell the agent *when* to use each tool.
- `sql_query` uses a read-only connection, accepts a single SELECT/WITH statement, and has a timeout.

## LLM rules (Run B onward)
- Reach the local LLM only through an OpenAI-compatible endpoint set in config. Don't hardcode a provider.
- Ask for structured JSON-schema output, validate it with Pydantic, and allow one retry.
- Extract only what is stated, and never infer numbers.
- Prompts live in `prompts/`. Bump `prompt_version` whenever a prompt changes.
