# sparkwatch — DGX Spark forum monitor

Mirrors the NVIDIA DGX Spark / GB10 Discourse forums (categories 721 and 723) into SQLite. A local LLM extracts model "recipes" (model, quant, engine, topology, tok/s) from the threads. The tool ranks the strongest models that run on **2× DGX Spark** and exposes everything to agents through an MCP server.

**SPEC.md is the source of truth** for the architecture, schema and build phases. Update it when a design decision changes.

## Tooling
- Python 3.12+ with a `src/` layout (`src/sparkwatch/`).
- **uv only:** `uv add <pkg>` (`--dev` for dev dependencies) and `uv run …`. Never use pip, and never edit dependencies in `pyproject.toml` by hand.
- Commands:
  - `uv run pytest`: tests
  - `uv run ruff check` and `uv run ruff format`: lint and format
  - `uv run sparkwatch sync | extract | rank | report | repos check | backfill | serve-mcp`
  - `uv run mcp dev src/sparkwatch/mcp_server.py`: MCP Inspector

## Scraping rules
- The forum is **Discourse**. Add `.json` to URLs: `/c/<path>/<id>.json?order=activity&page=N`, `/t/<id>.json`, `/t/<id>/posts.json?post_ids[]=…` and `/raw/<topic>/<post_number>`. Don't add a headless browser unless the JSON API stops working.
- Be polite:
  - at most 1 request per second;
  - a generic `User-Agent: sparkwatch/0.1` with no personal details;
  - honour 429 / `Retry-After`, with backoff via tenacity.
- During development, don't hammer the live forum. Record responses once into `tests/fixtures/` and test against them with `respx`. Live runs should use `--max-pages` limits.
- AI-provider fetch tools may be blocked for this site. Use `curl`/`httpx` directly, or Brave Search (`BRAVE_API_KEY`) for web lookups.
- The default initial lookback is **60 days** (`sync.lookback_days`). Widening it moves `forums.backfill_floor` back, so it never re-crawls from scratch.

## Data rules
- **SQLite is the single source of truth**, LLM output included: recipes, repo summaries, leaderboard snapshots and reports. Markdown reports are exports from it.
- Processing is incremental **per post**. Select posts where `extracted_at IS NULL OR extracted_hash != content_hash`. Never reprocess unchanged content unless `--reprocess` is passed (e.g. after bumping the prompt version).
- All upserts are idempotent and keyed on Discourse IDs.
- Schema changes go through numbered migrations in `db.py` using `PRAGMA user_version`. Never edit an applied migration.
- Use WAL mode, because the MCP server reads while sync writes.
- GitHub repos and Hugging Face models linked in posts are first-class entities (`repos`, `repo_mentions`, `repo_events`, `hf_models`). They are discovered with regex, not the LLM, and watched repos are monitored with ETag-cached GitHub API calls (`GITHUB_TOKEN` is optional).

## LLM rules
- Reach the local LLM through an **OpenAI-compatible** endpoint set in `config.toml` / `SPARKWATCH_LLM_*` (vLLM, SGLang, llama-server or Ollama). Don't hardcode a provider.
- Always ask for structured (JSON-schema) output and validate it with Pydantic, allowing one retry.
- Extract only what is stated. `evidence_quote` must appear verbatim in the source post, and tests check this.
- Ranking is deterministic code (in `rank.py`). The LLM only writes the narrative summary.
- Prompts live in `prompts/`. Bump `prompt_version` whenever a prompt changes.
