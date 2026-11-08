# sparkwatch Run A: interactive test brief

You built Run A of sparkwatch, as described in SPEC.md. This session is an **interactive test pass**: use the MCP server as an agent would, against real synced data, and write up what you find.

**Don't fix anything in this session.** Don't edit code, tests, the spec or config. Your only output is `REVIEW-self.md` in the repo root. Fixes come in a later session.

## Setup and safety

- The `sparkwatch` MCP server is registered in this client. Use its tools directly. If a tool isn't visible, stop and say so; don't register the server yourself.
- **Before anything else, confirm the server is reading the synced database, not the eval fixtures.**
  - Call `sql_query` with `select (select count(*) from topics) t, (select count(*) from posts) p, (select count(*) from missing_posts) m`.
  - Expect about **369 topics, 7,810 posts and 84 missing posts**. That's a 60-day mirror of categories 721 and 723, synced on 2026-11-08.
  - If you get the small `tests/evals/frozen.db` counts instead, stop and report it.
- **Be polite to the live forum and GitHub:**
  - Incremental `sync_now` is fine. Both categories are already synced, so each call should make only a few requests.
  - **Never backfill.** Don't pass `--days` or `lookback_days`, and don't do anything that would re-crawl forum 721.
  - CLI syncs: forum 723 only, with `--max-pages 1`.
  - If any sync reports far more requests than expected, stop and report it.
- The database is backed up outside the repo, so writes from this session (recipes, notes, flags, digest cursor, watched repos) are fine.

## 1. Quick checks

- **CLI sync:** run `uv run sparkwatch sync --forum 723 --max-pages 1`. Expect about 1 request and 0 new posts.
- **Lock:** run two of those CLI syncs at the same time. The second should print `busy` and exit with code 2.
- **CLI digest:**
  - `uv run sparkwatch digest --no-advance --cursor tester`
  - then `uv run sparkwatch digest --cursor tester`
  - then the second command again. The last run should show empty sections.
- **Tests:** run `uv run pytest -m "not live"`, then `SPARKWATCH_LIVE=1 uv run pytest -m live`.
- **MCP:**
  - `sql_query("DELETE FROM posts")` should be rejected as a tool error.
  - `get_posts` on topic 381350 with a tiny `max_chars` should return `truncated` and `next`. Keep following `next` for a few pages, and check that nothing overlaps and nothing is skipped.
  - Look at `missing_posts` through `sql_query`. Are the recorded reasons believable? Spot-check one or two posts.

## 2. Workflows, run end to end as an agent

Use the MCP prompts (`catch_up`, `serve_model`, `should_i_update`, `frontier`) where the client offers them. Otherwise follow their steps by hand.

1. **`catch_up`**: sync, read the digest, investigate the promising items, flag at least one, and write a briefing with a link for every claim.
2. **`serve_model("GLM 5.3 Flash", topology="dual-spark")`**: answer "what's the best way to serve GLM 5.3 Flash on two Sparks today?" Record a recipe with verified quotes, and save a note. Then **ask the same question again**: the second answer should start from the note and read only what's new.
3. **Watchlist reality check**: are any of the Qwen4 watch hits an actual release, or are they all rumours? Does the first-sighting alert help? Do the hits for the three tracked models look right?
4. **`should_i_update`**:
   - `watch_repo` one repo that's linked from the GLM 5.3 Flash threads;
   - `set_installed` an older SHA or tag of it;
   - judge whether `repo_status` gives you enough to decide whether to update.
5. **`frontier("dual-spark")`**: what are the strongest models people run on two Sparks right now?

## What to judge

For every tool response, ask:
- **Correctness:** do the numbers and dates match the underlying posts? Check that time-window counts (7, 30 and 60 days) reflect when posts were written.
- **Size:** is it compact enough for an agent? Note any response over about 10 KB, and what's taking up the space.
- **Usefulness:** does it include what the next step needs, or did you have to work around it with extra calls or `sql_query`?
- **Tool descriptions:** did they tell you which tool to use, and when?

## Output: `REVIEW-self.md`

For each finding, give:
- an ID and a severity: **high** (wrong results), **medium** (wasted tokens or misleading output) or **low** (polish);
- the tool or command;
- a repro;
- observed versus expected;
- evidence (topic and post IDs, URLs, sizes).

End with a short "verified OK" table, and the request counts from every sync you ran. Then stop.
