# sparkwatch Run A: merged issue list for the fix session

This list merges two independent test passes of the Run A build (`2f8342c`), both run on 2026-11-08 against the live 60-day mirror:
- **[C]** an outside review (Claude Code over an MCP stdio client): full detail in `docs/reviews/2026-11-08/ISSUES-claude.md`, items `ISSUE-n`;
- **[S]** the builder's own interactive pass (OpenFox, MCP tools): full detail in `docs/reviews/2026-11-08/REVIEW-self.md`, items `Fn`.

**[C+S]** means both passes found it independently, which is the strongest evidence. Repro steps, ground-truth queries and code locations are in the source documents. This file sets the order and merges duplicates.

**The spec has been revised for these fixes.** `SPEC.md` now defines the behaviour for every item marked **→ SPEC §…** below. Search for "revised 2026-11-08" (8 places) and read those sections before starting. **Where this file and the spec disagree, the spec wins.** Migration 3 (§6) gathers the schema changes, and Run B's triage migration is now number 4.

**Rules for the fix session:**
- Verify each item before fixing it.
- Every fix gets a regression test written against fixtures.
- The spec decisions are already made: implement them as written. If something in the spec looks wrong once you're in the code, stop and say so; don't work around it.
- Don't run any full re-sync of forum 721.

---

## P0: wrong results or broken core paths (fix first)

### 1. `sync_now()` with its defaults always returns `busy` (high) [S]: F1
Not a spec change. SPEC §5's `sync_now` already defines the behaviour.
- The sync lock is taken, then the repo-check lock is taken inside it. Both jobs use the same lock file, so the second `flock` fails within the same process.
- `catch_up` step 1 is therefore dead. `include_repos=false` works.
- **Fix:** take the lock once for the combined job, or allow the nested acquisition when it's from the same process.
- **Test:** `sync_now()` with `include_repos=True` against mocks returns the sync results plus the repo results.

### 2. `repo_status` reports ahead and behind the wrong way round (high) [S]: F2
**→ SPEC §4, "What it returns"**: the direction and names are defined there.
- An install 4 commits behind the branch head is reported as `status: "ahead", ahead_by: 4`. The digest says `commits_behind: 4` for the same repo.
- GitHub's `compare/{base}...{head}` fields are copied straight through, without translating them to the installer's point of view.
- **Fix:** report the result from the installed SHA's point of view (`behind_by` = commits it's missing), and make `repo_status` and the digest use the same names.
- **Test:** an install behind the head, one ahead of it, and one diverged.

### 3. `search_posts` fails on `.` and `/` (high) [S]: F4
**→ SPEC §5, `search_posts`, "Query handling"**: plain-text quoting by default, plus `raw_fts=True`.
- `search_posts("GLM 5.3 Flash")` and `"… tok/s …"` give an FTS5 syntax error. These are the queries an agent naturally types.
- **Fix:** turn free text into a safe FTS5 query. Quote each word, or each term containing punctuation (`"5.3"`, `"tok/s"`), and keep the documented FTS operators available through an explicit raw-query option.
- **Test:** queries with model versions, `tok/s`, quotes and hyphens.

### 4. The digest's `updated_topics.new_posts` counts a topic's whole history (high) [S]: F6
**→ SPEC §5, digest "Timing"**: the definitions of new, edited and backfilled posts, with a 24-hour grace period.
- Topic 381350 reports `new_posts: 570`, when there's actually 1.
- The query counts `first_fetched_at >= since OR changed_at >= since`, so after a backfill or a first sync of a bumped topic, every stored post matches.
- **Fix:** separate new posts (written after `since` and first seen after it) from posts that were only backfilled (seen after `since` but written earlier), and report backfilled posts as a count. This matches SPEC §5: "Backfilled posts are reported as a count only."

### 5. `whats_new_for(model, since=…)` ignores `since` for mentions (high) [S]: in the workflow notes under `serve_model`
**→ SPEC §5, `whats_new_for`**: `since` applies to every section, using the digest's timing definitions.
- With `since = note.as_of`, it returned posts from 17–26 Oct, so the second `serve_model` run re-reads old material.
- **Fix:** apply `since` to every section. Make clear whether it filters on when a post was written or when sparkwatch first saw it (the same split as item 4).
- **Test:** mentions on both sides of `since`.

### 6. `--max-pages` marks the whole window as covered, so the gap is never filled (high) [C]: ISSUE-10
**→ SPEC §1, "Listing coverage" and "Repair is explicit"**, plus migration 3: the new `forums.listing_floor` and `sync --repair-coverage --forum ID`. This replaces the "e.g. a migration that resets each forum's floor" suggestion below. **An automatic repair would wrongly re-list forum 721**, so the repair must stay explicit and per forum.
- Forum 723 has only 30 topics (25 Oct – 7 Nov), yet `backfill_floor` says 2026-09-09.
- Incremental syncs stop at `last_synced_at`, so the Projects topics from 9 Sep – 25 Oct are never fetched.
- **Fix:** a listing cut short by `max_pages` or the time budget mustn't advance the floor past the oldest `bumped_at` actually listed. It also mustn't advance the incremental cutoff in a way that skips the rest. The next sync carries on listing.
- **Repairing existing state is part of the fix.** The live database already records the wrong floor for forum 723, so a fix that only prevents future mistakes will trust it and fetch nothing. Add a repair step, e.g. a migration that resets each forum's floor to the oldest non-pinned `bumped_at` actually stored, or an explicit `sync --repair-coverage`. Report what it changed.
- **The gap has been left in the live database on purpose, as the acceptance test.** Once the fix and the repair pass on fixtures:
  1. Run `uv run sparkwatch sync --repair-coverage --forum 723 --apply` (dry run without `--apply`), then `uv run sparkwatch sync --forum 723` once, **with no `--max-pages`**. Stop and report if it goes over about 400 requests.
  2. **Forum 723 only.** Don't touch forum 721.
  3. Expect the topic count for 723 to rise from 30, with `bumped_at` reaching back towards 2026-09-09, the floor still at about 2026-09-09, and the run's `partial` set to false.
  4. A second immediate sync of 723 makes about 1–2 requests.
  5. **This also checks item 4.** `digest` should report the newly fetched old posts as a *backfilled count*, not as new topics or new posts. Record the digest output before and after.
  6. Report the request count, the topic and post counts before and after, and the digest output.

### 7. The rate limit can be switched off by config (high, safety) [C]: ISSUE-9, plus the process note
**→ SPEC §1, "Rate safety"**, which also requires 1-per-second pacing for GitHub.
- `requests_per_second <= 0` means no limit at all, and there's no upper clamp: `1000` is accepted and tested.
- The OpenFox registration left by the build used `1000` with the live forum URL. It has since been corrected by the user; it never actually ran fast.
- **Fix:** when `base_url` is the live forum host, refuse to start unless `0 < rps <= 1.0`. Tests and evals mock the network instead.
- **Related, [S] F19 (low):** GitHub requests aren't paced at all. Add the same 1 request per second.

### 8. Time windows and dates use when sparkwatch saw a post, not when it was written (high) [C+S]: ISSUE-1, F3, F8
**→ SPEC §5, `model_summary` mention counts, and digest "Timing"** (item dates).
- `list_watchlist` (`hits_7d == hits_30d`) and `model_summary.mentions` (`7d = 30d = 60d = 1163`) are affected.
- [S] also found that the count is of watch-hit rows (1,163), not posts (277); the true values are 13 / 83 / 245 for 7, 30 and 60 days.
- Digest watch hits carry `watch_hits.created_at` as their `date` (F8).
- **Fix:** activity windows and item dates use `posts.created_at`, and count distinct posts. "Novelty" (the digest cursor) keeps using `first_fetched_at` / `changed_at`.

### 9. Live drift tests can never run (high) [S]: F5
Not a spec change. SPEC's Testing approach already defines the behaviour.
- The test-wide `_block_network` fixture also applies to `tests/live/`, so `SPARKWATCH_LIVE=1 pytest -m live` fails 3/3 with "network access is blocked".
- **Fix:** skip the network block for tests marked `live`, when `SPARKWATCH_LIVE=1` is set.
- **Check:** keep the live tests themselves at 1 request per second, and limited to a handful of requests.

---

## P1: wasted tokens, misleading output, or features that never trigger

### 10. `model_summary` is mostly repo noise (medium) [C+S]: ISSUE-2, F9
**→ SPEC §5, "Size budget"; SPEC §1, Link discovery** (gists excluded).
- It's 35–52 KB, about 91% of which is `repos` (190–335 entries, e.g. `nvidia/cutlass`, `syllo/nvtop`).
- **Fix:**
  - rank repos by mentions within the model's topics, and cap the list at about 10;
  - put watched repos first, with install status;
  - report the total count;
  - aim for a few KB for the whole response.
- **Related, [S] F14 (low):** `gist.github.com/<user>/<id>` URLs are stored as GitHub repos. Exclude them in `links.py`.

### 11. The digest is too big, and the CLI output is unreadable (medium) [C+S]: ISSUE-7, F7
**→ SPEC §5, "Size budget", and digest "Size".**
- The MCP digest is 75 KB at `limit=30`; the CLI version is 42 KB of Python dict reprs.
- **Fix:**
  - give each section a smaller default and shorter snippets;
  - list repos first linked from posts as one line each;
  - make the CLI print readable lines, plus a `--json` flag;
  - aim for well under 10 KB by default.

### 12. `model_summary` leaves out flags on the model's topics (medium) [C]: ISSUE-3
**→ SPEC §5, "Size budget"** (the flag inclusion rule).
- The open flag on topic 382140 (the 60 tok/s claim against a ~26 tok/s report) doesn't appear.
- **Fix:** include open flags on the model, its recipes, and its topics.

### 13. Watch-hit snippets don't show the match (medium) [S]: F8
**→ SPEC §5, digest "Snippets".**
- The snippet is the first 300 characters of the post, so it often has nothing to do with the match. One example is a GLM hit whose snippet is about a Qwen SGLang setup.
- **Fix:** take the snippet from the text around the match.

### 14. `get_topic_overview.watch_hits` is a repeated bare list (medium) [S]: F10
- **Fix:** give distinct labels with counts, plus the latest post reference and URL for each.

### 15. Normalisation drops values, so the fit check never fires (medium) [S]: F11
- `fp4` and unrecognised engines become `other`. `models.params_total_b` is null for GLM 5.3 Flash, even though a thread title states "320B total / 18B active".
- **Fix:**
  - map `fp4`, falling back to `nvfp4` only if the context supports it; otherwise add a distinct `fp4` value;
  - keep the raw engine name searchable through `list_recipes(engine=…)`, matching the raw name as well as the canonical one;
  - when params are unknown, `record_recipe` returns a clear "fit unknown: set params with upsert_model" hint.
- Seeding params is a data task for the checkpoint, not a code fix.

### 16. Every missing post has `reason = 'hidden'` (medium) [C+S]: ISSUE-8, F12
**→ SPEC §1, Tombstones, "Posts that aren't content"**, plus migration 3: the CHECK rebuild and the reclassification. *The decision is made:* the reason is `not_content`.
- The [S] spot-check found they're `post_type 3` small actions (spam-scanner and moderator notices). `sync.py` labels every `post_type` outside {1, 2} as `hidden`.
- **Fix:** add the reason `not_content` for post types 3 and 4. Keep `hidden` for posts that really are hidden. Don't count `not_content` in `tombstones_new`, so the digest and summaries aren't inflated.

### 17. The Qwen4 watch regex matches vLLM code names (medium) [C]: ISSUE-4, and the [S] workflow notes
**→ SPEC §2, `seed/watchlist.toml`**: the new `match_regex` and `release_regex` are written in. Update the seed and add the test strings. The release pattern still matches hopeful phrasing such as "hoping Qwen 4 is released soon". That's expected, and the agent makes the final call.
- `Qwen4ExpForConditionalGeneration`, `qwen4_exp` and `Qwen3.8-Flash-Next (Qwen4Exp)` all match.
- **Fix** (seed data plus tests): `qwen[\s._-]*4(?![\d.])(?![\s_-]*exp)(?!\s*b\b)`. Test strings are in ISSUE-4.

### 18. The first-sighting alert for anticipated models fires on speculation (medium, **SPEC**) [C+S]: ISSUE-5, and [S] "honest but noisy"
**→ SPEC §1, "Anticipated entries"; SPEC §5, `ack_watch_sighting` and `catch_up` step 4.** *The decision is made:* an optional `release_regex` per entry; the alert re-arms after an acknowledgement; other hits show as a count; the agent classifies.
- (The options that were considered are recorded in ISSUES-claude.md, ISSUE-5.)

### 19. The workflows can't be reached by agents whose client hides MCP prompts (medium, **SPEC**) [S]: "MCP prompts are not exposed by this client"
**→ SPEC §5, "Workflows: prompts plus tool and instruction fallbacks".** *The decision is made:* all four channels, built from one source.
- MCP prompts are controlled by the user, and client support varies. Even where they're shown, the model can't call them, so an agent can't find `catch_up` / `serve_model` / `should_i_update` / `frontier` by itself.
- **The settled approach** (detail in the spec):
  - set `FastMCP(instructions=…)` with a short summary of the workflows;
  - add `list_workflows()` and `get_workflow(name)` tools, which return the same text as the prompts from a single source;
  - point to `get_workflow` from the descriptions of `sync_now`, `digest`, `model_summary` and `repo_status`;
  - add a "Workflows" section to the README.
- Keep the prompts as they are.

---

## P2: polish

| # | Item | Source |
|---|---|---|
| 20 | Seeded watchlist entries have `canonical = null`; they should resolve through the alias table. | [C] ISSUE-6 |
| 21 | The `busy` payload often can't name the lock holder (`job: null`), because the holder record is written after the lock is taken. | [S] F13 |
| 22 | `get_posts`: `post_count` is the page count with no topic total; page breaks split words; every page re-renders the whole topic. Break pages at post or line boundaries. | [S] F15 |
| 23 | The digest mixes events with open state (sightings, flags), so it's never empty. Either document this, or split it into `events` and `open_state`. | [S] F16 |
| 24 | A free-text `model=` (e.g. on `save_note`) creates junk model rows. Validate it against models and aliases, or store null. | [S] F17 |
| 25 | A renamed GitHub repo keeps its old URL while commit URLs use the new name. Follow the 301 and update the canonical URL. | [S] F18 |

---

## Process notes (not code)
- **Use a branch.** The build went straight onto `main`, not `build/<model>` from the `spec` tag, and there are 5 commits not pushed to `origin`. One message is "test commit: scaffold project metadata". Do fixes on a branch.
- **Pause 1 wasn't recorded.** There's no report for review pause 1. Only `tests/evals/pause2_report.md` exists.
- **Keep evals off the live data.** Checkpoint and test runs wrote to the live `data/sparkwatch.db`. Evals should use `tests/evals/frozen.db`.
- **The builder's summary was wrong about the live tests.** It said they run three drift checks; they can't run (item 9).

## Both passes verified as working
- the offline suite (192 passed, 1 skipped);
- lock contention between CLI processes (exit code 2);
- reads during a sync;
- digest cursor movement;
- `sql_query` rejecting writes;
- `get_posts` paging (no gaps or overlaps);
- quote verification;
- `ack_watch_sighting`;
- `watch_repo` creating a baseline with no events;
- flags and notes saving and reading back;
- forum politeness (every sparkwatch forum request at 1 per second or slower).

---

## P0 re-test (2026-11-08 15:10, `fix/run-a-issues` @ 24a9aab)
All nine P0 fixes held up when checked against the data:
- `sync_now` returns results, not `busy` (2 requests).
- `repo_status` says behind 6 / ahead 0, and the digest agrees.
- `search_posts` handles `.`, `/` and `tok/s`.
- The digest shows no history as new, plus a single backfilled line. A fresh cursor's first page is 8.7 KB and the second is 1.5 KB.
- `whats_new_for` applies `since` everywhere.
- The 723 gap is closed: 95 topics, and a re-sync makes 1 request.
- rps values of 0 and 1000 are rejected with a one-line error and exit code 1.
- Live tests: 3 passed in about 3 s. Offline: 241 passed, 1 skipped. Ruff is clean.
- `sync_runs` shows that nothing re-listed 721. Runs 13 and 15 are incremental `sync_now` calls (8 and 6 requests).

New findings, all P2:

| # | Issue | Evidence |
|---|---|---|
| 26 | `--repair-coverage` on a fully covered forum moves `listing_floor` *forward*, to the oldest stored topic, and reports `changed: true`. That creates a phantom gap, which the next sync re-pages to heal. It's harmless but surprising, and the fix agent's own test plan says to run it now. **Fix options:** a `--dry-run` default, a warning when the new floor is later than the old one, or refusing unless `--force` is given. | On a scratch copy: 723 went 12:27→21:05 and 721 went 13:03→14:19. The next 723 sync made 4 requests and restored the floor. |
| 27 | Mixed timestamp formats in comparable columns: Discourse `…T21:05:16.859Z` alongside sparkwatch `…T12:27:53+00:00` (e.g. `listing_floor` after a repair). String comparisons in SQL are only correct to the second, and `changed` compares strings. Normalise on write. | Repair output above |
| 28 | `list_installs` only reads `compare_cache`. After the head moves, `behind_by`/`ahead_by` stay `null` until `repo_status` or the digest fetches the compare. **Fix options:** compute on a miss, or show `"unknown (run repo_status)"`. | Calling it alongside the first `repo_status` returned null; afterwards it returned 6/0. |
| 29 | `search_posts("vllm or sglang")` silently ANDs `"or"` as a term, and no hint is shown. The checklist expected a hint. Detect bare `or`/`and`/`not`/`near` and give the hint. | `match: "vllm" AND "or" AND "sglang"`, with no `hint` key |
| 30 | A quoted phrase in plain mode gets split into separate terms (`"tensor parallel"` → `"tensor" AND "parallel"`). Keep user-quoted phrases as FTS phrases. | s5 test |
| 31 | `raw_fts=True` with a bad query returns a normal JSON result `{"error": …}`, not an MCP tool error (`isError`). The checklist said "expect a tool error". Pick one and make the checklist match. | s4 test |
| 32 | The 94 legacy tombstones stay `hidden` until `missing_post_recheck_days` (30) has passed, so #16 isn't visibly fixed for a month. Have migration 3 mark the old `hidden` rows as due for recheck so the next sync reclassifies them, at a few requests per topic. | `select reason,count(*) from missing_posts` → `hidden\|94` |
| 33 | `sync_runs` has no forum column, so you can't tell from the table which forum a run covered. | Schema |

Side effects of this re-test:
- a digest cursor named `claude-review` now exists in the DB;
- `compare_cache` has a b4c93e→f651fa entry;
- I made one 723 sync and one `sync_now`, both incremental.
- A backup taken before the re-test is at `~/dev/sparkwatch-review/sparkwatch-postfix.db`.
