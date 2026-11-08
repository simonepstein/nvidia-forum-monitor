# sparkwatch

**Status: spec only; implementation hasn't started yet.** See [SPEC.md](SPEC.md).

sparkwatch keeps an AI agent up to date with the NVIDIA DGX Spark / GB10 developer forums. It helps answer questions like "what's the best way to serve GLM 5.3 Flash on two Sparks today?" or "pull the latest posts and flag anything interesting".

It is a local data layer behind an MCP server:

- **Forum mirror.** Incremental, polite sync of the [DGX Spark / GB10](https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10/721) and [Projects](https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/dgx-spark-gb10-projects/723) categories into SQLite, with full-text search. It uses Discourse's JSON API, so no browser is needed.
- **"Since I last asked" digest.** Shows new and updated threads, mentions of watchlist models, and first sightings of anticipated releases.
- **Agent memory.** Recipes (model, quant, engine, topology, tok/s, setup, gotchas), benchmarks, research notes and flags are written back by the agent. Every one cites the forum posts it came from, and the quoted text is checked against those posts.
- **Watched repos.** GitHub repos found in posts can be watched. sparkwatch tracks whether the branch has moved, new releases and tags, and how far your installed SHA is behind, so you know when to update your serving stack.

The MVP needs no local LLM. Local-LLM triage, to cut agent token use, is planned as phase 2.

## Planned usage

```sh
uv sync
uv run sparkwatch sync                 # mirror the last 60 days (configurable)
uv run sparkwatch digest               # what's new
claude mcp add sparkwatch -- uv run --directory "$PWD" sparkwatch serve-mcp
```

Then, in Claude Code: *"catch me up"*, *"what's the best way to serve DeepSeek V4.1 Flash on two Sparks?"* or *"should I update vLLM?"*

## Development

- Python 3.12+ and [uv](https://docs.astral.sh/uv/). Run the tests with `uv run pytest` and lint with `uv run ruff check`.
- Contributor and agent guidance is in [CLAUDE.md](CLAUDE.md).
