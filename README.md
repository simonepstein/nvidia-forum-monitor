# sparkwatch

**Status: Run A (the MVP) is implemented.** See [SPEC.md](SPEC.md) for the design and the Run B/C plan.

sparkwatch keeps an AI agent up to date with the NVIDIA DGX Spark / GB10 developer forums. It helps answer questions like "what's the best way to serve GLM 5.3 Flash on two Sparks today?" or "pull the latest posts and flag anything interesting".

It is a local data layer behind an MCP server:

- **Forum mirror.** Incremental, polite sync of the [DGX Spark / GB10](https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10/721) and [Projects](https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10-projects/723) categories into SQLite, with full-text search. It uses Discourse's JSON API, so no browser is needed.
- **"Since I last asked" digest.** Shows new and updated threads, mentions of watchlist models, and first sightings of anticipated releases. It also carries open state — unacknowledged sightings and open flags — so a digest is never empty even when nothing changed.
- **Hot topics.** `global_digest` ranks what the whole forum is talking about across both categories by a deterministic score, with every term of the score returned, plus unanswered questions, repos and models first linked, and what the agent recorded in the window. It has its own cursor, so it never consumes your catch-up.
- **Agent memory.** Recipes (model, quant, engine, topology, tok/s, setup, gotchas), benchmarks, research notes and flags are written back by the agent. Every one cites the forum posts it came from, and the quoted text is checked against those posts.
- **Watched repos.** GitHub repos found in posts can be watched. sparkwatch tracks whether the branch has moved, new releases and tags, and how far your installed SHA is behind, so you know when to update your serving stack.

The MVP needs no local LLM. Local-LLM triage, to cut agent token use, is planned as phase 2.

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run sparkwatch sync                 # mirror the last 60 days (configurable in config.toml)
uv run sparkwatch digest               # what's new since the last digest
uv run sparkwatch global-digest        # what's hot across both categories, ranked
uv run sparkwatch global-digest --since 2026-11-01 --detail   # with the top posts
uv run sparkwatch global-digest --since 2026-10-25 --until 2026-11-08 --basis written   # a past period, backfill included
uv run sparkwatch sync --forum 723 --max-pages 1     # a small, polite live run
uv run sparkwatch backfill --days 120  # widen the window; never re-crawls from scratch
uv run sparkwatch repos check          # watched repos: head moved? new release or tag?
uv run sparkwatch sync --repair-coverage --forum 723            # dry run: show the floor change
uv run sparkwatch sync --repair-coverage --forum 723 --apply    # write it, then re-sync
uv run sparkwatch sync --refresh-engagement --days 14 --budget 180  # refresh recent likes/answers
```

`--repair-coverage` is explicit and per forum: it resets that forum's `listing_floor` to the
oldest topic it actually stored, which is what heals a listing that stopped early (say, after
`--max-pages`). It is a dry run unless you pass `--apply`, and it warns when the new floor is
*later* than the old one — on a fully covered forum that creates a gap the next sync will
re-page to close.

Configuration lives in `config.toml`; any value can be overridden with `SPARKWATCH_*`
environment variables using `__` for nesting, e.g.
`SPARKWATCH_GENERAL__DB_PATH=/tmp/sw.db` or `SPARKWATCH_FORUM__REQUESTS_PER_SECOND=0.5`.
`GITHUB_TOKEN` is optional but recommended: a `repos check` costs about 4–5 requests per watched repo, and anonymous GitHub allows only 60 an hour. A fine-grained token with "Public repositories" read access and no permissions raises that to 5000. Put it in `.env` at the project root (copy `.env.example`; `.env` is gitignored), or export it. An exported variable wins over `.env`. `repos check` reports `auth: token|anonymous` and never prints the token.

## Register the MCP server

```sh
claude mcp add sparkwatch -- uv run --directory /home/simon/dev/nvidia-forum-monitor sparkwatch serve-mcp
```

Then, in Claude Code: *"catch me up"*, *"what's the best way to serve DeepSeek V4.1 Flash on two Sparks?"* or *"should I update vLLM?"* — or use the `catch_up`, `serve_model`, `should_i_update` and `frontier` prompts.

To poke at the tools interactively: `uv run mcp dev src/sparkwatch/mcp_server.py`.

## Workflows

Four workflows are published through four channels, all built from one source
(`src/sparkwatch/workflows.py`), so an agent finds them whichever channel its client shows:

1. **MCP prompts** — `catch_up`, `serve_model`, `should_i_update`, `frontier`.
2. **Tools** — `list_workflows()` lists each name with its purpose and arguments;
   `get_workflow(name, args?)` returns the steps with the arguments filled in. These work
   even when the client hides prompts.
3. **Server instructions** — `FastMCP(instructions=…)` names the four workflows and says to
   call `get_workflow(name)` for the steps. Many clients put this in the model's context.
4. **Tool descriptions** — `sync_now`, `digest`, `model_summary` and `repo_status` point at
   the workflow they belong to.

| Workflow | Answers | Arguments |
|---|---|---|
| `catch_up` | "catch me up" — sync, digest, read, flag, record | — |
| `serve_model` | "best way to serve X on two Sparks?" | `model`, `topology` |
| `should_i_update` | "should I update this repo?" | `repo`, `label` |
| `frontier` | "strongest models on two Sparks right now?" | `topology` |

## Run it in the background

```sh
mkdir -p ~/.config/systemd/user
cp deploy/sparkwatch-sync.service deploy/sparkwatch-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sparkwatch-sync.timer
```

The timer runs `sparkwatch sync && sparkwatch repos check` every 2 hours. Long write jobs take
a lock, so an overlapping `sync_now` returns `{"busy": true, ...}` instead of blocking.

## Development

- `uv run pytest` — the full suite (no network; `respx` fixtures under `tests/fixtures/`).
- `uv run pytest -m "not live"` — what CI runs.
- `SPARKWATCH_LIVE=1 uv run pytest -m live` — opt-in drift checks against the real forum and GitHub.
- `uv run ruff check` and `uv run ruff format`.
- `uv run python scripts/record_fixtures.py` — re-record the live sample (run once; not part of the suite).
- `uv run python tests/evals/build_db.py` then `uv run python tests/evals/run.py` — manual, agent-driven evals.
  - With `claude` on PATH it drives `claude -p` against the frozen database.
  - With any other agent (OpenFox, or by hand), drive the tools yourself and then verify with
    `uv run python tests/evals/run.py --check-only --answers tests/evals/answers.openfox.json`.
  - `tests/evals/pause2_report.md` records a full checkpoint run done this way.

Contributor and agent guidance is in [CLAUDE.md](CLAUDE.md).
