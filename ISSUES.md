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
  1. Run `uv run sparkwatch sync --repair-coverage --forum 723`, then `uv run sparkwatch sync --forum 723` once, **with no `--max-pages`**. Stop and report if it goes over about 400 requests.
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

---

## P1/P2 re-test (2026-11-08 16:30, `fix/run-a-issues` @ 9db2b11)
Verified against the data:
- **Migration 4,** replayed on a copy of the pre-migration backup: row counts unchanged apart from watch_hits, integrity ok, FTS 8,989/8,989. All 41 timestamp columns are in one format.
- **Qwen4 hits** went from 94 to 80. All 14 removals were code-name or Qwen3.8 false positives, checked by hand. The sighting was re-armed.
- **Tombstones:** all 94 are now `not_content`. That took one 721 sync of 64 requests at 1 per second.
- **Repair coverage:** a dry run by default, with `--apply` and a gap warning.
- **Search:** the `or` hint, phrase quoting, and `raw_fts` errors raised as tool errors.
- **`model_summary`:** 5.7 KB, 10 of 223 repos, flags shown, and the alias resolves.
- **Workflows:** reachable through tools, instructions and descriptions.
- **`list_installs`:** 6 behind, 0 ahead.
- **Tests:** 290 passed, 1 skipped. Ruff is clean.
- **#25 renames:** checked on a scratch copy with the metadata check forced. It adopted `gilesco/…-tensorfold-zig`. The real DB will rename at the next daily metadata check.

| # | Issue | Evidence |
|---|---|---|
| 34 | **P1.** A sighting re-raises on every reply in a topic whose *title* matches the release pattern. `_raise_sighting` checks `release_regex` against the topic title for every post, and an ack re-arms it, so in a topic titled e.g. "Qwen4 is out" each reply reopens the alert and the ack stops meaning anything. **Keep title matches as notification:** the user wants to hear when the Qwen4 topic gets replies. But those belong in watch *activity* ("Qwen4 is coming: N new replies"), and the agent judges them. **The sighting alert** should be raised by (a) a post body matching `release_regex`, or (b) the OP of a new topic whose title matches it. Re-arm only for a post created after the ack. Also, don't let re-fetching an edited old post raise a sighting. | `watch.py:73` `release.search(title) or release.search(raw)`. The rearm logic is at lines 76-78. |
| 35 | **P2.** Title matches inflate hit counts and break match-centred snippets. 41 of the 80 Qwen4 hits are replies in "Qwen4 is coming" that never mention Qwen4, and most of GLM's 1,267 hits come from the 977-post GLM thread title. Record `matched_in` (`title`\|`body`) on `watch_hits`. Count *mentions* from body hits only. Report title-only hits per topic as thread activity (new-reply count, plus the latest replies with ordinary snippets, not match-centred ones). Keep both visible, because the user wants the thread updates. | `select … where raw does not match` → 41/80. `watch.py:35` checks the title first. |

Notes:
- A forced metadata check on a scratch copy found the watched repo's head moved 7 more commits (f651fa→2778ac). The install will show 13 behind after the next real check.
- Testing quirk, not a bug: the MCP stdio client only passes a safe subset of env vars to the server, so `SPARKWATCH_GENERAL__DB_PATH=… mcpcall.py` still reads the real DB. Use the CLI for scratch-DB checks.

---

## P1/P2 fix: title matches are notifications (2026-11-08 17:15, `fix/run-a-issues` @ 6a28e85 + working tree)

#34 and #35 are fixed. Title matches are kept as notifications, but they now go to watch **activity**; they never raise the sighting alert and never count as mentions.

- **Where a hit matched** is recorded on `watch_hits.matched_in` (`title` | `body`), with `matched_text` from the same match. One matcher, `watch._match`, checks the body first and is shared by `match_post`, `rematch_watch` and `rematch_all`; both rematch paths run with `is_new=False`, so changing a regex can never raise a sighting.
- **Sighting (#34):** a release raises the alert only from a **post body**, or from the **OP** (`post_number = 1`) of a **newly stored topic** whose title matches. A reply that only matches the topic title is thread activity, so a thread titled "Qwen4 is out" can no longer reopen the alert. After an ack, only a post **created after** it re-arms the alert. An edit raises nothing (now a documented choice in SPEC §1, not a gap).
- **Thread activity (#35):** the digest's `watchlist_activity[].threads` and `find_model_mentions.threads` report title-only hits per topic with ordinary (start-of-post) snippets. Every reply is counted — the digest head line carries `replies` and `threads_total`, e.g. `GLM 5.3 Flash (tracking): 21 new mention(s), 31 new replies in 3 threads (+1 more)` — while `threads` lists at most 2 topics with their 2 newest replies. `model_summary` gains `threads` (3 at most) with `body_posts` / `title_posts`.
- **Mention counts are body-only:** `model_summary.mentions` and `top_topics`, `find_model_mentions.total_posts`, `list_watchlist.hits_7d` / `hits_30d` (beside new `replies_7d` / `replies_30d` and `latest_reply`), `get_topic_overview.watch_hits[].count` (beside `replies` and `latest_reply`).
- **Migration 5** adds `matched_in` and backfills it **in one transaction** (a callable migration step, because `executescript` would commit the ALTER on its own). A crash leaves v4 and the next open retries. Run B triage moves to migration 6 (SPEC §6).
- **Tests:** 306 passed, 1 skipped. Ruff clean.
- **Rehearsal:** `/tmp/swcopy4/sparkwatch.db` is a genuine **v4** copy of the live data (3,640 hits, taken 16:29). Migrating it produced exactly the numbers below.
- **Note:** by the time a fresh copy was taken, the live DB was **already v5** — a process using the new code had opened it after the edits (any `open_db` migrates). Its post-state matches the rehearsal exactly, and a one-page `sync --forum 723` afterwards changed nothing.

### What to test

| # | Check | Expected |
|---|---|---|
| 1 | `PRAGMA user_version` | `5` |
| 2 | `select matched_in, count(*) from watch_hits group by 1` | `body\|1142`, `title\|2498` |
| 3 | `select count(*) from watch_hits` | `3640` (plus any later posts) |
| 4 | Qwen4 split: `select h.matched_in, count(*) from watch_hits h join watchlist w on w.id=h.watch_id where w.label='Qwen4' group by 1` | `body\|39`, `title\|41` |
| 5 | `select count(*) from watch_hits where matched_in='body' and matched_text is null` | `0` |
| 6 | Qwen4 alert after the migration and a sync: `select sighting_post_id, sighting_acked_at from watchlist where label='Qwen4'` | `NULL` / `2026-11-08T13:28:37+00:00` |
| 7 | `uv run sparkwatch digest --since 2026-11-01 --no-advance --limit 6` | Qwen4: `9 new mention(s), 12 new replies in 1 thread`, then `thread Qwen4 — …/t/388002: 12 new replies`; GLM: `31 new replies in 3 threads (+1 more)`, listed under the thread, not the mentions |
| 8 | `uv run sparkwatch digest --no-advance --json \| wc -c` (default cursor) | ~8.5 KB, still under the 10 KB budget |
| 9 | `model_summary("GLM 5.3 Flash").top_topics[0].topic_id` | `381350` (158 body mentions), with `mentions` body-only (`~17 / 124 / 372` for 7/30/60 days) |
| 10 | `model_summary("Qwen4").mentions` (body-only) and its `threads` | `{7d: 9, 30d: 14, 60d: 37}`; threads show 383964 (`body_posts 2` / `title_posts 29`) and 388002 (`2` / `12`) |
| 11 | `list_watchlist` for GLM | `hits_7d 17`, `hits_30d 124`, `replies_7d 30`, `replies_30d 206` |
| 12 | `get_topic_overview(383964)` watch hits for Qwen4 | `count 2` (body) beside `replies 29` (title) |
| 13 | Migration atomicity (offline): `uv run pytest -q tests/test_db.py -k migration_five` | passes: a failure inside the backfill leaves `user_version = 4` and no `matched_in` column |


---

## #34/#35 re-test (2026-11-08 17:35, `fix/run-a-issues` @ dbd3d08)
Verified independently:
- Recomputing title versus body from the stored posts agrees with `matched_in` on all 3,640 hits (0 disagreements), and `matched_text` matches the body text. Body 1,142 / title 2,498.
- Migration 5, replayed on a v4 scratch copy: integrity ok, every table's row count identical, Qwen4 sighting still null.
- Live DB: v5, and Qwen4 is still quiet.
- Digest: the Qwen4 line is as expected, and GLM shows "(+1 more)". Compact JSON is 8.1 KB; the CLI's `--json` is pretty-printed, at 11.3 KB.
- `model_summary`: top_topics[0] is 381350, mentions 17/124/372.
- `list_watchlist` and `get_topic_overview(383964)` (count 2, replies 29) are as stated.
- A Kimi K2 anticipated watch with no release_regex, then a regex change (2→170 hits), raised no sighting.
- Tests: 306 passed, 1 skipped. Ruff is clean.

(Committed as dbd3d08.)

| # | Issue | Evidence |
|---|---|---|
| 36 | **P1.** The agent's "this is significant" records carry no links, which breaks CLAUDE.md's rule of a URL and date on every item. Flags (`list_flags` and the digest's open flags) return `target_type`/`target_id` and no `url`. `get_notes` returns no `source_post_ids`, so a conclusion can't be traced to its posts, although `save_note` accepts them. **Fix:** derive `url` for every flag target (topic, post or repo). Return `sources: [{post_id, url, date}]` on notes. Add the same to `model_summary.notes` and the digest's open flags. | `list_flags` keys: `id, target_type, target_id, label, reason, priority, created_at, resolved_at, resolution`. `get_notes` keys: no sources. |
| 37 | **Feature (needs a SPEC section; Run A, no LLM).** `global_digest`: hot topics across both forums since the last run or a given date, and the basis for a newsletter or regular post. See the design sketch below. | User request, 2026-11-08 |

### #37 design sketch: `global_digest` and a newsletter workflow
- **Tool:** `global_digest(since="cursor"|ISO date, cursor="global", forum_id?, limit=10, advance=True)`. Its cursor is separate from the `digest` cursor, so newsletter runs don't eat the user's catch-up.
  - **Window:** the same new/backfilled rules as the digest. Only posts *written* in the window count, and backfill is never "hot".
- **Hotness per topic, deterministic and reproducible.** Each component is shown in the output so the agent can explain the ranking. The terms are:
  - new posts in the window;
  - distinct new posters;
  - likes on the new posts (`posts.like_count`);
  - a bonus for a topic created in the window;
  - an accepted answer, or staff participation, in the window;
  - an optional relevance weight (`relevance_score`), so a busy off-topic thread doesn't top the list.
  - Caveat: likes are as of fetch time, so late likes on older posts aren't seen. Views can't be used, because `topics.views` is a snapshot. Deltas would need a `topic_stats` snapshot table; that's optional and can come later.
- **For each hot topic:** forum, title, url, created_at, new_posts, posters, likes, and the score with its components. Also:
  - the 2 most-liked new posts, each with url, date, author and a snippet;
  - repos and HF models first linked in the window;
  - the watch labels touching it, and open flags on it.
- **Extra sections:**
  - new topics with zero replies, as count plus top 3;
  - repos and HF models first linked anywhere in the window;
  - recipes, benchmarks and notes recorded in the window;
  - watched-repo releases.
- **Budget:** under 10 KB by default, with `next` to page further.
- **Newsletter workflow** (`get_workflow('newsletter')` plus a prompt). The steps:
  1. `sync_now`.
  2. `global_digest(since=<last issue>)`.
  3. `get_posts` on the top threads.
  4. Write the issue in sections: hot threads, new recipes and benchmarks, model watch, repo updates. Every claim gets a forum link; quote briefly and attribute by username.
  5. `save_newsletter(body, since, until, source_post_ids)`, or a note with subject `newsletter YYYY-MM-DD`, so the next issue starts where this one ended and can say "since last issue".
  6. Advance the `global` cursor only when the issue is saved.
- **Automation:** the tool needs no LLM. Writing the issue needs either an agent run headless on a timer (e.g. `claude -p "/newsletter"` with the MCP server) or, from Run B, the local LLM via a `sparkwatch newsletter --since … > issue.md` command.
- **Publishing note:** if issues are posted publicly, summarise and link rather than republish post text. Check NVIDIA's forum terms before reposting content verbatim.
| 38 | **Enhancement (after #37 is in use).** A newsletter: `save_newsletter`, a `'newsletter'` cursor and a `newsletter` workflow, built on `global_digest`. Split out from #37 at the user's request: `global_digest` is useful now, and the newsletter is an enhancement. | SPEC §5 "Newsletter (enhancement…)", build plan |
| 39 | **P1.** Post likes are never stored. The forum runs the Discourse reactions plugin: posts have no `like_count`, and `reactions` is a **list**, but `sync.py:262` expects a dict, so every post stores `like_count = 0`. Topic-level likes are fine. A second bug compounds it: a re-fetch with unchanged content only writes `fetched_at` (`sync.py:~303`), so `like_count` and `accepted_answer` freeze at first fetch. **Fix** per SPEC §1 "Post likes": take `reaction_users_count`, then `actions_summary[id=2].count`, then the sum of `reactions[].count`, and update both fields on unchanged re-fetches without touching `changed_at`. Never re-fetch history automatically. | `select count(*), sum(like_count>0) from posts` → `9117\|0`. In fixture post 1882979: `like_count: null, reaction_users_count: 4, actions_summary [{id:2,count:4}], reactions [{"id":"heart","count":4}]`. |

`global_digest` dry run on the live DB (default weights, since 2026-11-01; computed by hand from the SPEC formula): 85 active topics. The top 3 are the DeepSeek V4.1 EXL3 2×Spark thread (11.2), the GLM megathread (10.7) and "DGX Spark repeatedly rebooting after most recent update" (9.4, relevance factor 1.0). Sensible, with the likes term at 0 because of #39.

**SPEC:** #36, #37, #38 and #39 are specified in the 2026-11-08 SPEC revision (§1 Post likes, §5 Hot topics, §5 Newsletter, §5 Knowledge flags and notes, config `[global_digest]`, build plan "Run A addendum", Verification).

## #39 fix: post likes (2026-11-08 17:55, `fix/run-a-issues` @ 6e1b401 + working tree)

Fixed as specified in SPEC §1 "Post likes".

- **Parsing.** `sync._post_like_count()` reads `reaction_users_count`, then `actions_summary[id=2].count`, then the sum of `reactions[].count`. An older Discourse's dict-shaped `reactions` (`{"like_count": 4}`) is still read, and a plain `like_count` is the last resort; otherwise 0. `accepted_answer` is read from the post as before.
- **Unchanged re-fetches.** The `"same"` branch now updates `like_count`, `accepted_answer` and `fetched_at`. `content_hash`, `changed_at`, `raw` and `cooked` are untouched, and the outcome stays `"same"`, so `posts_changed` is 0 and the post is never reported as edited.
- **No new fetching.** Target selection is unchanged: only posts from the last `refetch_recent_days` in topics that were bumped, plus the OP and due tombstone re-checks. Older posts keep `like_count = 0` until their topic is active again; that is expected, and there is no automatic history re-fetch.
- **Tests:** `tests/test_post_likes.py`, 6 tests: the three parse orders, the legacy dict shape, zero, and an unchanged re-fetch that raises likes 1 → 7 and sets `accepted_answer` with `posts_changed = 0`, an unchanged `changed_at`, and no edit in the digest. Full suite: 312 passed, 1 skipped. Ruff clean.
- **Live rehearsal** on a `.backup` copy, then live: `sparkwatch sync --forum 721 --max-pages 1 --budget 120` (5 requests, one listing page — not a full 721 sync). Result: `requests 5, posts_new 3, posts_changed 0, partial false`. Topic 384438 was re-fetched, so 9 of its recent posts now carry likes (max 5); every other stored post stays 0. Spot check: post 1868123 stores `like_count = 5`, and the API returns `reaction_users_count: 5`.
- **Live counts:** `select count(*), sum(like_count>0) from posts` → `9120|9`. Edits: `select count(*) from posts where changed_at >= <sync start> and first_fetched_at < <sync start>` → `0`.

## #36 fix: flag URLs and note sources (2026-11-08 18:05, `fix/run-a-issues` @ 16e929e + working tree)

Fixed as specified in SPEC §5 "Knowledge" (flags and notes).

- **New `src/sparkwatch/knowledge.py`.** `flag_target()` derives a flag's `url` from its target: `topic` → `/t/<id>` (with `title`), `post` → `/t/<topic_id>/<post_number>` (with the topic title), `repo` → its GitHub URL (by URL or `owner/name`), `hf_model` → its Hugging Face URL, `recipe` → its first evidence post's URL, `model` → no URL but `model: <canonical>`. A target that no longer resolves gets `url: null, target_missing: true` and is still returned. `note_sources()` reads `evidence` (`kind='note'`) into `[{post_id, url, date}]` (post `created_at`), and `note_payload()` adds `sources` to a note.
- **Where it is wired in:** `list_flags`, the digest's `open_flags` (and its CLI rendering, which now prints the URL), `model_summary.flags` and `model_summary.notes`, `get_topic_overview.flags`, `get_notes`, `sparkwatch://flags/open` and `sparkwatch://notes/latest`.
- **Tests:** `tests/test_knowledge.py`, 7 tests: every target type's URL, the model case, deleted targets (`url: null` + `target_missing`), note sources with URL and date, a note with no sources, digest flags, and `get_topic_overview` / `model_summary` flags. Full suite: 319 passed, 1 skipped. Ruff clean.
- **Live check:** both open flags resolve — 382140 → `https://forums.developer.nvidia.com/t/382140`, 382939 → `/t/382939`, each with its title. `get_notes` now returns sources: note 1 has 2, note 2 has 5 (e.g. post 1882249 → `/t/382939/192`, 2026-11-05), note 3 ("frontier dual-spark") has none.

## #37 feature: `global_digest`, the `global-digest` CLI and `[global_digest]` config (2026-11-08 18:20, `fix/run-a-issues` @ 698f4ad + working tree)

Built per SPEC §5 "Hot topics" and the build plan's Run A addendum. The newsletter (#38) is left for later.

- **Library (`digest.global_digest`).** Window `[since, until)` using the digest's new/backfilled rules; `since` is `"cursor"` (the `'global'` cursor, epoch if unset) or an ISO date; `until` defaults to now and is echoed back. Only new posts count; backfilled posts are one `{posts, topics}` line and edits are not counted. Score = `base · factor`, where `factor = 0.5 + min(relevance_score, 40)/40` and a topic marked irrelevant gets 0.5 with `relevant: false`. Ties break on score, then newest new post, then `topic_id`.
- **Components add up.** Each `components` value is the weighted term *already multiplied by `factor`*; `base` (pre-factor) and `factor` are returned alongside, and `score = round(sum(components), 3)`, so the identity holds to 3 dp for every topic.
- **Response.** `since`, `until`, `cursor`, `weights`, `hot_topics_total`, `hot_topics[]`, `unanswered`, `first_linked`, `knowledge`, `repo_events`, `backfilled`, and `next` (`{since, until, offset}`) while more remain. Every topic's flags carry their `url` (ISSUES #36); a `knowledge` note line carries `sources: [{post_id, url, date}]`, and recipe/benchmark lines carry their evidence `url`s.
- **Size decision (SPEC updated).** Ten topics with the full per-topic detail came to 15.7 KB compact on the live DB, over the 10 KB budget. So `top_posts` is now returned only with `detail=True` (2 most-liked new posts, 160-character snippets); the default response is lean and **9.8 KB compact**. `knowledge` is capped at 5 lines and unanswered OP snippets are 120 characters. `limit` stays 10, so both forums appear in the default page.
- **Cursors.** Its own `'global'` cursor, forward-only (`until` must be at or after the stored value). `advance=True` moves it to `until`; `advance=False` moves nothing; a paged call (`next`) never advances. `'default'` is never touched.
- **CLI + config.** `sparkwatch global-digest [--since] [--until] [--cursor] [--forum] [--limit] [--relevant-only] [--detail] [--no-advance] [--json]` calls the same function with readable text by default. `[global_digest]` in `config.toml` holds `weights` and `relevance_cap`, loaded by `GlobalDigestSettings`.
- **Tests.** `tests/test_global_digest.py` (17 tests): hand-computed score and components, components sum to score for every topic, factor 0.5/1.5 edges, backfilled and edited posts excluded, tie-break order, both forums plus `forum_id` filter, identical rerun, cursor behaviour (no `'global'` row with `advance=False`, `'default'` untouched, `advance=True` moves to `until`), `next` paging with no gaps or overlaps, `detail` top posts, unanswered/first_linked/knowledge sections, a 30-topic fixture staying under 10 KB compact, config weights, and the CLI in text and JSON. Snapshot regenerated: 42 tools, `global_digest` added with every argument optional. Full suite: 336 passed, 1 skipped. Ruff clean.
- **Live acceptance (read-only, `advance=False`, since 2026-11-01):** 10 hot topics of 86, forums **721 and 723** both present; components sum to score for all 10; **9,851 bytes compact**; rerun with the echoed `until` is byte-identical; `backfilled: {posts: 8591, topics: 399}`; `first_linked` counts 64 links with in-window mentions; all 25 URLs in the response (topics, posts and note sources) resolve to a stored topic or post; `digest_cursors` has **no `global` row** and `default`, `tester` and `claude-review` are unchanged. Top 3: 386125 (11.24), 381350 (10.70), 384438 (10.25, the only one with likes so far: 12); the first 723 topic is rank 10 (384765, 6.68).

### What to test

Run these against the live DB (`sqlite3 -readonly data/sparkwatch.db …`):

1. **#39 likes.** `select count(*), sum(like_count>0) from posts;` → **`9120|9`**. The 9 are all in topic **384438** (`select topic_id, count(*), sum(like_count>0), max(like_count) from posts where like_count>0 group by topic_id;` → `384438|9|9|5`). Everything else stays 0 until its topic is bumped again — expected, not a bug. Spot check: post **1868123** has `like_count = 5`, and the API's `reaction_users_count` is 5.
2. **#39 no phantom edits.** `select count(*) from posts where changed_at >= '2026-11-08T17:52:25+00:00' and first_fetched_at < '2026-11-08T17:52:25+00:00';` → **`0`**, and the sync run recorded `posts_changed = 0` (`select posts_new, posts_changed from sync_runs order by id desc limit 1;` → `3|0`).
3. **#36 flag URLs.** `list_flags` returns both open flags with `url` `https://forums.developer.nvidia.com/t/382140` and `/t/382939` and their titles; `sparkwatch://flags/open` and the digest's `open_flags` show the same. A flag on a deleted topic/post returns `url: null, target_missing: true`.
4. **#36 note sources.** `get_notes` → note 2 has **5** sources, note 1 has **2** (e.g. post 1882249 → `https://forums.developer.nvidia.com/t/382939/192`, 2026-11-05), note 3 ("frontier dual-spark") has **0**. `model_summary.notes[].sources` and the note lines in `global_digest.knowledge[].sources` carry the same links.
5. **#37 hot topics.** `global_digest(since="2026-11-01", advance=False)` (or `uv run sparkwatch global-digest --since 2026-11-01 --no-advance --json`) → 10 topics of 86, forums 721 and 723, every `round(sum(components.values()),3) == score`, compact JSON **9,851 bytes** (under 10 KB).
6. **#37 reproducible.** Re-run with the returned `until` (`--since 2026-11-01 --until <until> --no-advance`) → identical `hot_topics`.
7. **#37 cursors.** `select name, last_seen_at from digest_cursors order by name;` → **no `global` row**, and `claude-review` `2026-11-08T15:09:10+00:00`, `default` `2026-11-08T13:28:40+00:00`, `tester` `2026-11-08T13:21:22+00:00` unchanged. Passing `--detail` or `advance=True` is the only thing that adds/moves `global`.
8. **#37 backfill is not news.** The response's `backfilled` is `{posts: 8591, topics: 399}` (a count, no URLs), and `new_posts` sums to 221 across the 10 shown topics. The whole window holds **529** new posts in **86** topics (`select count(*), count(distinct topic_id) from posts where first_fetched_at >= '2026-11-01' and created_at >= '2026-10-31';` → `529|86`); the 3 posts the 17:52 sync fetched are in it.

## Code-review polish pass (2026-11-08 18:50, `fix/run-a-issues` @ fd2a623 + working tree)

All 11 findings from the review of the #36/#37/#39 commits are addressed.

- **Bad dates fail loudly (1).** `global_digest()` raises `ValueError` for an unparseable `since` (anything but `cursor`) or `until`; the CLI catches it and prints `error: invalid until date: 'last week'; use an ISO date`, exit 1, no traceback. Tests cover both the library and the CLI.
- **Readable CLI text (2).** `_render_global_topic` now prints `N new posts by M posters, L likes, factor F (base B)` and a separate `terms: posts +5.32, posters +5.93` line with zero terms dropped; the CLI test asserts the labels instead of an always-true condition.
- **`first_linked` counts only real news (3).** Links need at least one mention on a window post (`HAVING mentions > 0`); the live count drops from the misleading 556 to **64**, and a test pins a repo whose only mention is backfilled to `count: 0`.
- **HF first post URL (4).** `_first_mention_url()` serves both `repo_mentions` and `hf_mentions` (ordered by post date), so HF entries carry `first_post_url` too; test added.
- **Recipe flags (5).** A recipe that exists but has no evidence now returns `{"url": null}` with no `target_missing`; only a genuinely deleted recipe/post/topic is `target_missing: true`. Test added.
- **One home for forum URLs (6).** `knowledge.post_link()` / `knowledge.topic_url()` build every forum URL; `digest._post_url/_topic_url` and `mcp_server._post_url/_topic_url` delegate to them, and the duplicated `FORUM_URL` constants in those modules are gone. The canonical forum host stays fixed (SPEC §5 requires a forum URL on every item; `forum.base_url` is the request host, and tests/evals assert the canonical form).
- **Timing rules written once (7).** `GLOBAL_POSTS`/`GLOBAL_BACKFILLED` are composed from `NEW_POSTS`/`BACKFILLED_POSTS` plus the `until` bound (`NEW_POSTS_UNTIL`/`BACKFILLED_POSTS_UNTIL`), and `filters` is a module import again.
- **CLI headers and paging hint (8).** Every capped section prints `shown/total` (`Unanswered (3/9)`, `First linked (5/64)`), and the hint is now `N more hot topic(s): raise --limit (cap 25)` instead of suggesting an unavailable `--next`.
- **`repo_events` capped (9).** Returns `{count, top: [5]}` like the other sections, so a long window cannot blow the 10 KB budget. Test added.
- **Config validated (10).** `GlobalDigestSettings.relevance_cap` is `Field(40.0, gt=0)` and unknown weight keys are rejected with a named error; `_hotness` no longer silently maps a 0 cap to 1.0. Tests added.
- **Trimmed payloads and test quality (11).** `_post_like_count()` returns `None` when the payload carries no like field, and the `"same"` branch then leaves the stored `like_count` (and `accepted_answer`) alone; a test re-fetches a trimmed payload and keeps likes 4 / accepted true. The CLI text assertion now checks the rendered labels.

Full suite: **344 passed, 1 skipped**; ruff check and format clean. Live re-check unchanged apart from the numbers above: 10 of 86 topics, forums 721 + 723, components sum to score, **9,851 bytes compact**, identical rerun, 25 URLs resolve, no `global` cursor row.

## #36/#37/#39 re-test (2026-11-08 19:15, `fix/run-a-issues` @ b643351)

Ran on the live DB, read-only, except for one mark on a `.backup` copy. Live requests: 2 (`/t/384438.json`, plus one `posts.json` batch), 1 s apart.

**Passed:**
- 344 passed, 1 skipped. ruff is clean.
- **#39 likes:** the DB shows 9120 posts, 9 with likes, all in topic 384438.
  - Checked against the live `reaction_users_count` for the OP and posts 134–143, they all match. The exception is post 138, which was 5 in the DB and 6 live: a like added after the 17:52 sync.
  - Older posts (2–21) are still 0 while live has 1–8. That's expected, since they're outside `refetch_recent_days`.
  - Sync run 24 shows `posts_new=3` and `posts_changed=0`.
  - Of the 15 posts fetched at 17:52, only the 3 new ones have `changed_at` after 17:50, each equal to its `first_fetched_at`. So there were no phantom edits.
- **#37 scores:** I recomputed them independently from SQL: new posts in the window with `created_at >= since-24h`, distinct posters, summed likes, new/accepted/staff indicators, and the factor `0.5+min(rel,40)/40`.
  - All 86 topics are found, and the top 10 and their order match exactly, with scores within 0.001 (the rounding of the components).
  - The components sum to `score` for each topic.
  - Running again with the echoed `until` gives an identical ranking and identical scores.
  - Paging via `next` (offset 10, then 20, and so on up to 86) covers all 86 topics with no overlap.
  - The 'global' cursor was never written, and default, tester and claude-review are unchanged.
  - The compact payload is 9,851 B. With `detail=True` it's 16.1 KB, which is opt-in as documented.
  - `--until "last week"` prints a one-line error and exits with code 1.
- **#37 vs my hand ranking (SPEC dry run):** the order is the same: 386125 at 11.24, 381350 at 10.70, 387397 at 9.39. The exception is 384438, which comes in at #3 (10.25) solely through its likes term (+3.58). That topic is the only one re-fetched since the #39 fix, so likes are currently uneven across topics.
  - This is transient. The next ordinary incremental sync of both forums refreshes likes for recent posts in bumped topics.
  - Until then, read the likes term with caution.
- **#37 irrelevant mark (on a copy):** with `relevance_override=0` on 381350, its factor is 0.5 and it shows `relevant:false`, scoring 3.657 at rank 34 of 86. With `--relevant-only` it's absent and the total is 85.
- **#36:** `list_flags` gives both flags `url` and `title` (/t/382939, /t/382140).
  - `get_notes` returns sources for notes 3/2/1 of 0/5/2, and the post URLs resolve to the right `/t/topic/n`. For example, 1881645 is /t/382140/20.
  - `global_digest.knowledge` notes carry `sources`.

### #40 (P2): `global-digest --detail` text lists every top post after all the topics, not under each one
- **Repro:** `uv run sparkwatch global-digest --since 2026-11-01 --no-advance --detail`.
- **Observed:** the ten topic blocks print first. Then all twenty `· N likes …/t/<topic>/<n>` lines follow as one block (lines 34–53), so you have to match each URL's topic id by eye.
- **Expected:** two indented post lines under each topic's `terms:` line, which is what the fix summary described.
- **Cause:** in `digest.py:render_global_digest` (around line 933), the `for item in payload["hot_topics"]: for post in item["top_posts"]` loop runs after `lines.extend(_render_global_topic(...))`. It should be inside `_render_global_topic`, or run per item. The JSON is correct.

## #40 fix: `global-digest --detail` nests each topic's posts under that topic (2026-11-08 19:26, `fix/run-a-issues` @ a33e807 + working tree)

Rendering only. `global_digest()` and its JSON payload are untouched.

- **Repro.** `uv run sparkwatch global-digest --since 2026-11-01 --no-advance --detail` printed the ten topic blocks, then all twenty `· N likes …/t/<topic>/<n>` lines as one trailing block (lines 34–53).
- **Cause.** `render_global_digest()` extended the topic blocks from `_render_global_topic()` and then ran a second `for item in payload["hot_topics"]: for post in item["top_posts"]` loop afterwards, so every post line landed at the end instead of inside its topic.
- **Fix.** `_render_global_topic(item)` now returns `list[str]`: the heading, the counts line, `terms:` when any term is non-zero, then one `    · {likes} likes {url} {date} — {snippet}` line per `top_posts` entry. The posts are indented 4 spaces, one level under the 2-space counts and terms lines. `render_global_digest()` runs a single loop, `for item in payload["hot_topics"]: lines.extend(_render_global_topic(item))`, and the separate post loop is gone.
- **Test.** `tests/test_global_digest.py::test_detail_renders_each_topics_posts_under_that_topic`: two topics (relevance 40 and 20) with two liked posts each. It asserts the four post lines in full (likes-desc within each topic), the order counts → `terms:` → posts under the first heading, the first topic's second post before the second heading, and the second topic's posts after it. A lean payload renders no post lines. Full suite: **345 passed, 1 skipped**. Ruff check and format are clean, and `test_cli_global_digest_renders_text_and_json` still passes.
- **Payload unchanged.** `--json` for `--since 2026-11-01 --until 2026-11-08T15:09:10+00:00 --no-advance` is byte-identical to the `a33e807` build (13,970 bytes; `cmp` clean).
- **Live check (read-only, `--no-advance`).** With `--detail` there are 20 post lines, all inside `## Hot topics (10/86)` (the last is line 115, and `## Unanswered` starts at 121), each topic's two lines directly under its `terms:` line. Without `--detail` there are 0 post lines. The rest of the text is unchanged: `Unanswered (3/9)`, `First linked (5/64)`, `Recorded knowledge (5/5)`, `Watched repo events (0/0)`, `backfilled: 8591 posts in 399 topics`, and `76 more hot topic(s): raise --limit (cap 25) to see them`.

### What to test

1. `uv run sparkwatch global-digest --since 2026-11-01 --no-advance --detail` → each topic's two `· N likes …` lines sit under that topic's `terms:` line and before the next `- <score> …` heading; 20 post lines in total, none after the last topic.
2. The same command without `--detail` → no `·` post lines.
3. `uv run sparkwatch global-digest --since 2026-11-01 --until 2026-11-08T15:09:10+00:00 --no-advance --json` → identical bytes to the `a33e807` build.
4. `uv run pytest -q` → 345 passed, 1 skipped; `uv run ruff check` and `uv run ruff format --check` clean.

## #40 re-test (2026-11-08, `fix/run-a-issues` @ 24b1142)

**Passed.**
- 345 passed, 1 skipped. ruff check and ruff format are clean.
- With `--detail` at a fixed `until`, there are 20 post lines, each indented 4 spaces under its own topic. A script check found 0 lines misplaced and none after the last topic.
- `--limit 3 --detail` gives 6 post lines.
- Without `--detail` there are no post lines. The only ` · ` lines are recipe lines in the Recorded knowledge section.
- The `--detail --json` output from worktrees at a33e807 and at HEAD, run against the same DB, is byte-identical (22,042 B).
- The digest cursors are unchanged.

### #41 (spec gap, not a bug): the spec doesn't say what happens to pre-floor posts in old topics
- **Observed (sync run 25, 2026-11-08 19:27):**
  - Topic 365302 (created 2026-04-01, 365 posts) got one new post.
  - The sync stored 93 posts from May 2026, before `backfill_floor` (2026-09-09).
  - 284 of the topic's 311 stored posts now predate the floor.
- **Why:** §1 step 4.3 says to stop "once a whole batch is older than the floor". A batch's age is only known after it's been fetched, so the batch that triggers the stop is stored anyway. Each bump of an old topic therefore brings in about one more pre-floor batch.
- **The implementation follows the spec as written.** What the spec leaves undefined is whether the floor is a hard boundary on what's *stored*, or only a limit on how much is *fetched*.
- **Resolved (SPEC §1 step 4.3):** the floor limits fetching, not storage. Pre-floor posts stored this way are accepted as thread history and are not pruned. No code change.

### #42 (P2): CLI text output passes forum text through rich markup: BBCode gets eaten, and a stray closing tag crashes the command
- **Repro:** `uv run sparkwatch global-digest --since 2026-11-01 --until 2026-11-08T19:40:40+00:00 --detail --no-advance`, then look at the line for /t/382733/335.
  - The JSON snippet is `[quote="azampatti, post:331, topic:382733"] \`git pull && ./setup.sh.\` [/quote] Many thanks…`.
  - The text output shows `—  \`git pull && ./setup.sh.\`  Many thanks…`. The quote tags have silently disappeared, which is why there are double spaces.
- **Crash path:** `Console().print("stray [/quote] closing tag")` raises `rich.errors.MarkupError: closing tag '[/quote]' … doesn't match any open tag`.
  - `_snippet_around`, which the `digest` watch-hit snippets use, starts mid-post, so it can begin after a `[quote=…]` or `[details=…]` opener and still contain the `[/quote]`. Snippet truncation can produce the same thing.
  - That would crash `sparkwatch digest` / `global-digest` with a traceback.
  - Forum text such as `[bold]` or `[red]` would also be applied as styling.
- **Cause:** `cli.py:139` and `cli.py:181` call `console.print(render_…(payload))` with markup enabled.
- **Fix:** print the rendered text with `markup=False` (and `highlight=False`), or `rich.markup.escape()` the forum-sourced fields. Add a test that renders a snippet containing `[/quote]` and `[bold]`, and checks they come out verbatim.
- **Note:** the `&amp;&amp;` seen in one terminal paste comes from the paste path, not sparkwatch. The DB `raw`, the JSON and a re-run all show `&&`.

### #43 (P3): a recipe line in Recorded knowledge repeats a URL when two evidence quotes come from the same post
- **Observed:** `glm-5.3-flash · other · other · dual-spark · works — …/t/386738/1 …/t/386738/1`. Recipe 2 has two evidence rows, both from post 1877754.
- **Fix:** dedupe source URLs by post in the CLI line. The JSON `sources` can keep both, since they're distinct quotes.

### #44 (P2): `digest` "Repos first linked" reports backfilled links as news, and lists a repo once per mention
- **Repro:** `uv run sparkwatch digest` (cursor 'default' at 13:28:40).
  - Six repos are listed, all dated `2026-11-08T15:57:36`, and `deepseek-ai/deepseek-harness` appears twice.
- **Evidence:**
  - Every `repo_mentions` row dated 15:57:36 points to a post created 2026-08-07 to 2026-08-21, first fetched at 15:57:25. That's the one-off 721 gap-closing sync in the P1/P2 re-test.
  - deepseek-harness (repo 410) has two mentions, posts 1850462 and 1851362, both in topic 372268.
- **Cause:** in `digest.py:_repos_first_linked` (around line 349):
  - The query filters on `repos.first_seen_at >= since`, which is when sparkwatch discovered the repo, not when the forum post linking it was written. Links in backfilled posts therefore count as new. That goes against the digest rule that backfills aren't news; CLAUDE.md says the digest "keys on when sparkwatch *saw* a change, not on post `created_at`, so backfills aren't reported as news", and the "new" definition requires `created_at >= since-24h`.
  - It `LEFT JOIN`s `repo_mentions` with no grouping, so it returns one row per mention, despite the docstring "One line per repo". `LIMIT` is applied to those mention rows.
- **Expected:** do what `global_digest._window_first_linked` already does after the polish pass, which counts only real in-window links. That is: a repo whose earliest mention is in a post that's *new* under the digest definition, one line per repo, with the earliest such post as `post_url`. Links found only in backfilled posts go in the `backfilled` count, or are left out.
- **Test:** add a fixture repo linked only from a backfilled post, which should be absent, and one linked twice from new posts, which should appear once.

### #45 (P2): repo metadata (renames, archived, default branch) never refreshes when `repos check` runs more often than once a day
- **Observed:**
  - The watched repo 69 is still `gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-v1.1-testing`.
  - GitHub returns `301` for that name. `/repositories/1424520842` gives `full_name = gilesco/GLM-5.3-Flash-EXL3-2x-GB10-TensorFold-ZIG`.
  - `repos check` at 19:46 made 4 requests: head, compare, tags and so on, but no metadata request.
  - There's no URL clash: the new name is `…-tensorfold-zig`, while row 71 is the different repo `…-zig-testing`.
- **Cause:** `repos.py:282` fetches metadata only `if last_checked < now - 1 day`. But `repos.py:438` sets `last_checked_at = now` on *every* check.
  - So after the baseline, metadata is fetched again only when the gap between two checks exceeds 24 h.
  - On the scheduled 2-hourly run it never is. Renames (#25), `archived` and `default_branch` changes are never picked up.
  - A changed default branch would leave the head check on the wrong branch.
  - #25 passed earlier only because the scratch test forced the metadata check.
- **Expected:** SPEC §4b step 3 says "at most once a day". That should be measured from the last *metadata* fetch, not the last check.
- **Fix:** track the metadata fetch time separately.
  - Option 1: an `etags`-JSON key like `meta_checked_at`, which needs no migration.
  - Option 2: a new column. That needs a migration, and Run B's triage is planned as migration 6, so coordinate the number.
  - A 304 on the metadata ETag should also count as checked.
- **Test:** check a repo twice 2 h apart, then again 25 h after the first. Metadata should be requested on the 1st and 3rd checks only, even though `last_checked_at` moved on the 2nd.

### #46 (P3): `set_installed` with a full SHA skips resolution, so the response has no commit subject or date and the SHA is never checked
- **Repro (2026-11-08 19:57, `fix/run-a-issues` @ 24b1142):** `set_installed(repo="hacibeyoglu/deepseek-v4.1-flash-tensorfold-spark", ref="f98a3f4f9bc9de396ba1ad678876cda27aaea95f")`. The repo was not watched at the time.
- **Observed:**
  - The response has `"subject": null, "commit_date": null`.
  - The install row has `branch: null`, and the status reports `"branch": "HEAD"` until the repo is watched.
  - Status `identical` turned out to be correct: after `watch_repo`, `main` head = f98a3f4.
- **Expected:** SPEC §MCP tools says the response "confirms the resolved SHA, the commit subject and date". SPEC §4b step 1 says every `ref` is resolved via `GET /repos/{o}/{n}/commits/{ref}`.
- **Cause:** `repos.py:493`. `_resolve_ref` returns early when `SHA_RE` matches, so a 40-char SHA never hits the commits endpoint.
  - A mistyped or nonexistent full SHA is stored as-is, and is only noticed later as a compare error.
- **Fix:** always call `commits/{ref}`, which is one request; a full SHA can use the cache. Reject a 404/422 with a clear error instead of storing it.
- **Test:** add a respx case in which a full SHA returns subject and date, and a missing SHA raises an error rather than inserting a row.
- **Related, worth a line in the response:** `set_installed` on an unwatched repo works, but `repo_status()` with no `repo` only covers watched repos, so that install is never reported. Add a `hint` suggesting `watch_repo` when `repos.watch=0`.

### #47 (feature, Run A): `sync --refresh-engagement`, an explicit, budgeted refresh of likes and accepted answers on recent posts
- **Why:** after #39, likes are only correct for posts re-fetched in topics bumped since the fix (60 of 9,215 posts so far).
  - `global_digest` currently favours topics that happened to be re-fetched. 384438 is #3 purely on likes, while 386125 and 381350 show 0.
  - A full resync would be about 600 requests, mostly for history nothing uses.
- **Spec:** SPEC §1 "Engagement refresh" (added 2026-11-08), and the build plan "Run A follow-ups":
  - one `posts.json` call per topic for posts from the last N days (default 14);
  - it writes only `like_count`, `accepted_answer` and `fetched_at`;
  - it never moves floors or cursors, and is never automatic.
  - CLI `sync --refresh-engagement` plus an MCP tool `refresh_engagement`, to be called only when the user asks.
- **Acceptance:** first rehearse on a copy, then run `--days 14 --budget 180` live. That's about 90 requests, with `posts_changed` 0 and likes present across many topics.

## #42 fix: CLI text output no longer parses forum text as rich markup (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** `cli.py` printed the rendered digest and global digest with `console.print(...)`, which parses `[...]` as rich markup. So `[quote]`/`[/quote]` disappeared and an unmatched `[/quote]` raised `rich.errors.MarkupError`.
- **Fix.** Both text paths (`digest` and `global-digest`) now call `console.print(rendered, markup=False, highlight=False)`. `--json` is unchanged.
- **Test.** `tests/test_digest.py::test_cli_digest_prints_forum_markup_verbatim` and `tests/test_global_digest.py::test_cli_global_digest_prints_forum_markup_verbatim` seed `[/quote] [bold]GLM 5.3 Flash[/bold] …` and assert both tags come out verbatim with exit code 0.

## #44 fix: "Repos first linked" counts only links on new posts, one row per repo (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** `digest._repos_first_linked` filtered on `repos.first_seen_at >= since` and LEFT JOINed `repo_mentions` without grouping, so links in backfilled posts counted as news and a repo appeared once per mention.
- **Fix.** The query now mirrors `global_digest._window_first_linked`: a mention only counts when its post is **new** (`NEW_POSTS`: `first_fetched_at >= since`, `created_at >= since - 24h`), results are `GROUP BY r.id HAVING mentions > 0`, and each item carries `mentions` plus the earliest such post as `post_url` (new helper `_first_new_mention_url`). Links seen only in backfilled posts are left out; the section stays a list, and the CLI line now shows the repo, the earliest new post URL and `mentions` when there's more than one.
- **Test.** `test_repos_first_linked_ignores_links_from_backfilled_posts` (absent) and `test_repos_first_linked_is_one_row_per_repo` (two new mentions → one row, `mentions == 2`, earliest `post_url`); `test_repos_first_linked_and_open_flags` now gives its repo a new-post mention.

## #45 fix: metadata refreshes once a day from its own timestamp (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** The metadata call was gated on `last_checked_at < now - 1 day`, but every check writes `last_checked_at`, so on a 2-hourly schedule metadata was never re-fetched and renames, `archived` and `default_branch` changes were missed.
- **Fix.** The metadata fetch time is tracked separately as `repos.etags['meta_checked_at']` (option 1: no migration, so Run B keeps migration 6). `_save_meta_checked` stamps it after a 200 **or a 304**, and the gate measures a day from it. The `archived` event now fires only on a 0/None → 1 transition, so daily metadata refreshes can't duplicate it.
- **Test.** `test_metadata_refreshes_once_a_day_not_once_a_check` (checks at t0, +2 h, +25 h request metadata on the 1st and 3rd only), `test_metadata_304_counts_as_a_metadata_check`, `test_archived_event_is_created_once`. Existing rename/archived tests now call `expire_metadata()` instead of moving `last_checked_at`.

## #46 fix: `set_installed` resolves full SHAs and hints on unwatched repos (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** `_resolve_ref` returned early for a 40-character SHA, so no commits request was made: the response had `subject: null` and `commit_date: null`, and a mistyped SHA was stored.
- **Fix.** Every ref now goes through `GET /repos/{o}/{n}/commits/{ref}` (one request; no new cache). A 404/422 becomes `ValueError("cannot resolve '<ref>' in owner/name: not found")` and no install row is written. `set_installed` adds `hint` when `repos.watch=0`, pointing at `watch_repo` (documented in SPEC §5).
- **Test.** `test_set_installed_resolves_a_full_sha_and_returns_its_subject`, `test_set_installed_rejects_an_unknown_full_sha` (no row), `test_set_installed_hints_when_the_repo_is_not_watched`. `prepare()` now serves `commits/{OLD}` for the other install tests.

## #43 fix: recipe lines no longer repeat a source URL (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** `render_global_digest` joined every evidence URL, so two quotes from one post printed `/t/386738/1` twice.
- **Fix.** The CLI line dedupes URLs with `dict.fromkeys` (order preserved). The JSON `urls`/`sources` still list every quote.
- **Test.** `test_knowledge_line_dedupes_source_urls`: two evidence rows on post 11 and one on post 12 render one `/t/1/11` and one `/t/1/12`, while the payload still has 3 URLs.

## #47 feature: `refresh_engagement`, an explicit budgeted engagement refresh (2026-11-08, `fix/run-a-issues` working tree)
- **Library.** `sync.refresh_engagement(settings, days=14, forum_ids=None, time_budget_s=None, …)` selects stored posts with `created_at >= now - days` (capped at `lookback_days`), skips `missing_posts` and deleted/hidden/`not_content` posts, and makes one `posts.json` call per topic per 50 IDs, newest topics first.
  - Writes go through `_upsert_post`: the unchanged path refreshes `like_count`, `accepted_answer` and `fetched_at`; a genuine content change takes the normal edit path and counts in `posts_changed`. It never inserts a post, never runs watch matching, and never touches `listing_floor`, `last_synced_at`, `backfill_floor` or digest cursors.
  - It returns `{requests, topics, posts_checked, likes_changed, accepted_changed, posts_changed, partial, errors}` and writes a `sync_runs` row with `trigger='engagement'` and `posts_new=0`. It stops after the current batch with `partial=true` when the budget runs out.
- **Wiring.** CLI `sparkwatch sync --refresh-engagement [--days N] [--forum ID] [--budget S]` under the `engagement` write lock. It rejects `--repair-coverage`/`--apply`/`--max-pages`, defaults `--budget` to 180 s, and exits 1 with a one-line error for `--days < 1`. MCP tool `refresh_engagement(days=14, forum_id?, time_budget_s?)` whose description says to use it **only when the user asks**, never as part of `catch_up`/`sync_now`. `tests/snapshots/tools.json` was regenerated. README lists the CLI form.
- **Tests.** `test_refresh_engagement_updates_likes_and_leaves_content_alone` (1 request, 3 posts, likes 0→5/3, accepted 0→1, `changed_at` untouched, floors/cursors identical, run row), `…only_targets_recent_posts`, `…skips_missing_posts`, `…records_a_genuine_edit`, `…stops_at_the_budget`, `…rejects_a_zero_day_window`, `…continues_after_a_bad_topic` (one topic raising `RuntimeError` doesn't stop the rest), `test_cli_refresh_engagement`, `…rejects_repair_coverage`, `…rejects_zero_days`, and the MCP tool + busy + description tests.
- **Rehearsal (copy only, live DB untouched).** `sqlite3 data/sparkwatch.db ".backup data/sparkwatch.db.backup-20261108"`, then `SPARKWATCH_GENERAL__DB_PATH=data/sparkwatch.db.backup-20261108 uv run sparkwatch sync --refresh-engagement --days 14 --budget 180`.
  - Result: **150 requests**, 146 topics, 1,030 posts checked, **386 likes changed**, 0 accepted changed, **0 posts changed**, `partial: false`, no errors. 2 min 30 s at 1 request/s.
  - The copy's `likes_positive` rose 60 → 445 across 80 topics; `listing_floor`, `last_synced_at`, `backfill_floor` and all digest cursors were unchanged; no `changed_at` moved.
  - 150 requests for 146 topics: four topics needed a second 50-ID batch. That is above the spec's "about 90" estimate, because 146 topics currently have posts from the last 14 days.
  - The live DB still shows 60 posts with likes, 26 sync runs and no `engagement` run.

### What to test
1. `uv run pytest -q` → 367 passed, 1 skipped; `uv run ruff check` and `uv run ruff format --check` clean.
2. **#42:** `uv run sparkwatch global-digest --since 2026-11-01 --until 2026-11-08T19:40:40+00:00 --detail --no-advance` → the /t/382733/335 line keeps `[quote="azampatti, post:331, topic:382733"] … [/quote]` verbatim; no `MarkupError`. `uv run sparkwatch digest --no-advance` also renders any `[bold]`/`[/quote]` verbatim.
3. **#44:** `uv run sparkwatch digest --since 2026-11-08T13:28:40+00:00 --no-advance --json` → `repos_first_linked` has no backfilled-only repos and `deepseek-ai/deepseek-harness` appears once with `mentions: 2`; the text section prints one line per repo.
4. **#45:** `uv run sparkwatch repos check` twice within a day → metadata is requested once; after a day without a metadata fetch, the next check follows repo 69's 301 to `gilesco/GLM-5.3-Flash-EXL3-2x-GB10-TensorFold-ZIG`. `uv run pytest tests/test_repos.py -q`.
5. **#46:** MCP `set_installed(repo=…, ref="<40-char SHA>")` → `subject` and `commit_date` filled; a nonexistent SHA raises a clear error and adds no install; an unwatched repo returns `hint`. `uv run pytest tests/test_installs.py -q`.
6. **#43:** `uv run sparkwatch global-digest --since 2026-11-01 --no-advance` → the recipe 2 line shows `/t/386738/1` once.
7. **#47 (live, after the re-test):** `uv run sparkwatch sync --refresh-engagement --days 14 --budget 180` → about 150 requests, `posts_changed: 0` apart from real edits, `likes_positive` rising well beyond topic 384438, and `listing_floor`/`last_synced_at`/cursors unchanged. Expect `partial: true` only if 180 s runs out.

## #42–#47 re-test (2026-11-08 21:15, `fix/run-a-issues` @ d47dcfb, tested on the working tree before it was committed)

**All passed.**
- 367 passed, 1 skipped. ruff check and ruff format are clean.
- **#42:** the `/t/382733/335` line prints `[quote="azampatti, …"] … [/quote]` verbatim.
  - On a `.backup` copy I set a new post's raw to `[/quote] stray close and [bold]not bold[/bold] [red]x`. `global-digest --detail` printed it verbatim, exit 0.
- **#44:** since 13:28:40 the section is empty. That's correct: an independent query finds 0 repo mentions on new posts in that window.
  - Since 2026-11-01 it shows 6 rows (the cap) out of 55 repos with new-post mentions, with 0 duplicate repos.
  - Each `post_url` is a window post, e.g. /t/372662/46 created 2026-11-01T00:10.
- **#43:** the recipe 2 line shows /t/386738/1 once.
- **#45 (on a copy, live GitHub):**
  - The first `repos check` after the upgrade (no `meta_checked_at`) made 5 requests, adopted `gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-zig` and stamped `meta_checked_at`.
  - An immediate second check made 3 requests, with no metadata request.
- **#46 (on a copy, live GitHub):**
  - A full SHA (97bda06…) returned its subject and `commit_date`.
  - `'0'*40` raised `ValueError: cannot resolve … not found`, and 0 rows were added.
  - An unwatched repo returned the `watch_repo` hint.
- **#47 CLI:** `--help` lists the option. Combining it with `--repair-coverage` gives exit 1 with "cannot be combined", and `--days 0` gives exit 1 with "days must be at least 1".
- **#47 live run (run by the user, 21:09–21:11):**
  - Result: `requests 150` (149 s, so 1/s), `topics 146`, `posts_checked 1030`, `likes_changed 386`, `posts_changed 0`, `partial false`.
  - It wrote a `sync_runs` row with trigger `engagement`.
  - `last_synced_at`, `backfill_floor`, `listing_floor` and all digest cursors are unchanged, and 0 posts have `changed_at` after the run started.
  - Likes now cover 445 posts in 80 topics, up from 60 posts.
  - Spot check against the live API: the newest 20 posts of 386125 and of 381350, 40/40 `reaction_users_count` matches (2 requests).
  - `global_digest` since 11-01 is now led by 386125 (15.75), 381350 (15.57) and 382733 (13.24). 384438 drops from #3 to #8.

### #48 (P2, spec gap): `global_digest` can't rank a past date range, so reports and blog posts get nothing
- **Repro:** `uv run sparkwatch global-digest --since 2026-10-25 --until 2026-11-08 --no-advance --json`.
  - It returns `hot_topics_total: 0, backfilled: {posts: 0, topics: 0}`.
  - But `select count(*), count(distinct topic_id) from posts where created_at >= '2026-10-25' and created_at < '2026-11-08'` gives `1054|147`.
- **Cause:** the response follows the spec. "New" means `first_fetched_at` is in `[since, until)`, and the whole mirror was first fetched from 2026-11-08T12:27. Even with an older mirror, a seen-based window leaves out every post that arrived by backfill.
  - That's right for catch-up ("what's new to me"). It's wrong for a report ("what was written in this period").
  - In the blog-post trial, the agent had to rank by `created_at` through `sql_query` instead.
- **Spec:** SPEC §5 Hot topics, **Basis**: `basis="written"` ranks by `created_at` in `[since, until)`, counts backfilled posts, ignores the cursor, requires an ISO `since`, and applies the same window to all sections. CLI: `--basis`. Build plan: "Run A follow-up (ISSUES #48)".
- **Acceptance:** for 2026-10-25 → 2026-11-08 with `basis="written"`, about 147 topics.
  - An independent ranking by `created_at` with the default weights (before the #47 refresh) gave 384438 at 14.4, 386125 at 14.0 and 387397 at 11.3. With the refreshed likes the numbers will shift, so recompute rather than reuse them.
  - `basis="seen"` still returns 0, and no cursor row is written.
- **Unblocks:** an on-demand `/blogpost` command, held until this lands. The #38 newsletter will be revised to be on-demand, with no cursor.

### #49 (P3): one GitHub repo is stored as two rows under different old names
- **Observed:** row 69 (`…-tensorfold-v1.1-testing`, watched) and row 71 (`…-tensorfold-zig-testing`, unwatched) both 301 to `api.github.com/repositories/1424520842`, which is now `…-tensorfold-zig`.
  - #45 renames row 69 at its next metadata check.
  - Row 71 is never checked, because it's unwatched. It keeps its own `repo_mentions` (3 posts), which `repo_status` for the watched install doesn't see.
- **Fix:** store GitHub's numeric `id` from the metadata response. When a repo resolves to an id another row already has, merge the rows: move `repo_mentions` and installs to the watched (or older) row and drop the other one.
  - Discovery could also resolve unwatched repos lazily on first `watch_repo` / `set_installed`.
  - A new column needs a migration, so coordinate with Run B's 6, or keep the id in the `etags` JSON as #45 did.

### #50 (P3): `save_note` stores `as_of` exactly as given, so one note has a date-only timestamp
- **Observed:** `research_notes` id 5 has `as_of = '2026-11-08'`. Every other row is `YYYY-MM-DDTHH:MM:SS+00:00`, and the timestamp-format scanner flags `research_notes.as_of` as MIXED.
  - Comparing it as a string against full timestamps is wrong for that whole day (#27's rule).
- **Fix:** normalise `as_of` on write with the same helper as other timestamps, so a bare date becomes `T00:00:00+00:00`, and reject unparseable values. A one-off fix for row 5 can go in the next migration or be a documented `UPDATE`.

## #48 feature: `global_digest(basis="written")`, a written-date ranking for reports (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** Every section of `global_digest` counted only posts sparkwatch first saw in the window (`GLOBAL_POSTS`): the aggregates, `top_posts`, `watch_labels` and `first_linked`. The whole mirror was first fetched on 2026-11-08, so any earlier window returned 0 topics, and the blog-post trial had to rank by `created_at` through `sql_query`.
- **Fix.** New `BASES`, `WRITTEN_POSTS` and `posts_predicate(basis)`, threaded through `_global_aggregates`, `_topic_top_posts`, `_topic_watch_labels`, `_hot_topic` and `_window_first_linked`. `_first_mention_url` and `_first_new_mention_url` became one basis-aware `_first_in_window_mention_url`, which the digest's own `_repos_first_linked` now uses with `NEW_POSTS`.
  - `basis="written"` counts a post when `created_at` is in `[since, until)`, whenever sparkwatch fetched it; edits are still not counted.
  - It omits `backfilled`, returns `basis: "written"` and `cursor: null`, rejects `since="cursor"` with `ValueError("basis='written' needs an ISO 'since' …")`, and neither reads nor moves a cursor, so `advance` is ignored.
  - `unanswered`, `first_linked`, `knowledge` and `repo_events` use the same written window. `first_linked` under `written` needs a mention on a written-window post; it no longer also requires the repo's `first_seen_at` to fall in the window, which would have hidden every repo discovered during the 2026-11-08 backfill.
  - `next` carries `basis`, so a page continues on the same basis; an unknown basis raises `ValueError("invalid basis …")`.
  - Wiring: CLI `--basis seen|written` (default `seen`, a bad value exits 1 with one line), MCP `global_digest(basis=…)` with a description saying when to use `written`, and the text header now reads `(basis seen, cursor global)` or `(basis written)`, with the backfilled line only for `seen`.
- **Tests.** `test_written_basis_counts_backfilled_posts` (a post written in the window but fetched in March counts; the reverse does not; no `backfilled` key), `test_written_basis_rejects_the_cursor_and_writes_no_cursor` (both errors, `cursor is None`, the stored cursor value untouched), `test_written_basis_first_linked_counts_backfilled_mentions` (repo first seen outside the window), `test_written_basis_pages_with_the_same_basis`, and `test_cli_global_digest_basis_written_and_rejects_a_bad_basis`. `tests/test_mcp_protocol.py` now requires the `basis` argument.
- **Live check (read-only, `--no-advance`).** `uv run sparkwatch global-digest --since 2026-10-25 --until 2026-11-08 --basis written --no-advance --json` → `hot_topics_total: 147`, `cursor: null`, no `backfilled` key, `unanswered 18`, `first_linked 113`.
  - Top 3, recomputed with the refreshed likes: **386125 19.945** (159 new posts, 30 posters, 78 likes), **384438 17.056** (73/11/66), **381350 16.087** (37/14/26); 382733 14.329 and 387397 14.159 follow.
  - The same window with `--basis seen` → `hot_topics_total: 0`, `backfilled {posts: 0, topics: 0}`. `digest_cursors` still holds only `claude-review`, `default` and `tester`.

## #50 fix: `save_note` normalises `as_of`, so a bare date means midnight UTC (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** `save_note` wrote `as_of or now` verbatim, so a date-only value stayed `2026-11-08` (row 5) and string comparisons against full timestamps were wrong for that whole day.
- **Fix.** `save_note` now runs `as_of` through `parse_dt` → `to_iso`: `2026-11-08` → `2026-11-08T00:00:00+00:00`, an offset is converted to UTC, `None` still defaults to the write time, and an unparseable value raises `ValueError("invalid as_of: …; use an ISO date, e.g. 2026-11-08")` before any row is written. The response echoes the normalised value. No migration: row 5 is a documented `UPDATE` (SPEC §5 `save_note`, build plan), since the etags route for #49 keeps Run B's migration 6 free.
- **Tests.** `test_save_note_normalises_as_of` (bare date and a `+02:00` offset), `test_save_note_defaults_as_of_to_the_write_time`, `test_save_note_rejects_an_unparseable_as_of` (no row added).
- **Rehearsal on `data/sparkwatch.db.backup-50`.** The `UPDATE` changed 1 row; all five notes then match `YYYY-MM-DDTHH:MM:SS+00:00`. A `save_note(as_of="2026-11-08")` on the copy returned `2026-11-08T00:00:00+00:00`, and one without `as_of` returned the write time.
- **Live repair (2026-11-08 21:45, run after the rehearsal).** Backed up to `data/sparkwatch.db.backup-before-asof-20261108`, then `sqlite3 data/sparkwatch.db "UPDATE research_notes SET as_of='2026-11-08T00:00:00+00:00' WHERE id=5 AND as_of='2026-11-08';"` → `changes()` 1. Row 5 now reads `2026-11-08T00:00:00+00:00` and 0 rows fail the format check, so `research_notes.as_of` is one format.

## #49 fix: repos store GitHub's numeric id, and rows sharing one are merged (2026-11-08, `fix/run-a-issues` working tree)
- **Cause.** A repo's identity was its URL, so the same repository stayed in the mirror twice under two old names: row 69 (`…-tensorfold-v1.1-testing`, watched) and row 71 (`…-tensorfold-zig-testing`, unwatched) both 301 to `repositories/1424520842`. Row 71 is never checked because it is unwatched, so its 3 mentions never reached `repo_status` for the watched install, and `_follow_rename` refused the rename whenever the canonical URL was already held by another row.
- **Fix.** The metadata response's numeric `id` is stored as `repos.etags['github_id']` (`_github_id` / `_save_github_id`), so no migration and Run B keeps migration 6.
  - `_merge_by_github_id` collects every row with that id and `_merge_repos` folds the losers into the winner: **watched row first, else older `first_seen_at`, else lower row id**.
  - Children move with `INSERT OR IGNORE` (a shared post or tag keeps the existing row) and the loser's rows are deleted: `repo_mentions`, `repo_installs`, `repo_events`, `compare_cache`. Repo flags that named the loser (`url` or `owner/name`, any case) are retargeted at the survivor, and `_follow_rename` retargets them again when the canonical name lands.
  - Fields combine: `watch` is the OR, `note`/`branch`/`latest_release_tag`/`latest_tag` fall back to the loser's, the newer `head_seen_at` wins, `first_seen_at` is the earlier, `watched_at` the earlier, `last_checked_at` the later, `origin` is `forum` if either was, and the etags dicts merge.
  - Triggers: any metadata fetch (`check_repos`, `watch_repo`'s baseline, `repos check --repo <alias>`) plus `set_installed`, which makes one metadata request when `github_id` is missing so a stale alias is merged on first touch. Ids that differ never merge, and a rename onto a URL held by a row with an unknown or different id is still refused.
  - `check_repos` returns `merged: [{from, to}]`, and both `check_repos` and `watch_repo` carry on against the survivor. `repo_status` now also finds a repo's flags by URL, not just `owner/name`.
- **Tests.** `test_metadata_stores_githubs_numeric_id`, `test_rows_sharing_a_github_id_merge_into_the_watched_row` (watched-but-newer beats older; 3+3 mentions, install, tag event and compare entry move; the flag is retargeted; loser gone; `merged` reported), `test_merge_prefers_the_older_row_when_neither_is_watched`, `test_rows_with_different_github_ids_are_not_merged`, `test_set_installed_resolves_the_id_and_merges_an_alias`.
- **Rehearsal on `data/sparkwatch.db.backup-49` (live GitHub, 25 requests).** `repos check` checked 5 repos in 21 requests, adopted `gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-zig` for row 69 and stored `github_id: 1424520842`; then `repos check --repo gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-zig-testing` returned `merged: [{from: 71, to: 69}]` in 4 requests.
  - End state: one row (69, watched, canonical `-zig` URL) with **6 mentions** (3 + 3, no shared post), no row 71, etags keeping `github_id`. The live DB was not touched: it still has both rows.

## Review fixes (2026-11-08, `fix/run-a-issues` working tree, after the #48–#50 review)
- **#48, bug: `basis="written"` ignored `forum_id` in `first_linked`.** `_window_first_linked` built `forum_clause` as `" AND t.forum_id = :forum_id"` and assumed a `WHERE`; the written basis passed `""`, so the AND bound onto the `LEFT JOIN topics` and filtered nothing (repos from 721 and 723 both came back).
  - **Fix.** Both bases now start a `WHERE` (`WHERE 1` for written), so the forum clause always binds. Test `test_written_basis_honours_the_forum_filter` (two forums, disjoint repos; the written run keeps one topic and one repo, the seen run none).
- **#49, bug: a merge could delete a row still queued in `check_repos`'s snapshot.** With two watched rows that already shared an id, the first row's metadata fetch merged the second away, and its turn then hit `loads(None["etags"])` → `TypeError`.
  - **Fix.** The loop re-reads each row by id and `continue`s past a `removed` set, so a merged-away row is skipped instead of dereferenced. Test `test_two_watched_rows_sharing_an_id_merge_without_a_repo_filter` (no `repo=` filter, `checked: 1`, one row left).
- **#49, minor: winner-side merges were unreported.** `merged` was appended only when the checked row was the loser.
  - **Fix.** `_merge_by_github_id(…, merged=…)` records every fold as `{from, to}`, whichever side the caller is on; `check_repos` passes its list and derives `removed` from it, and `watch_repo` still remaps the checked id.
- **#49, minor: `set_installed` bypassed the once-a-day metadata rule.** `_resolve_github_id` fetched `/repos/{o}/{n}` without an ETag and never stamped `meta_checked_at`, so the next `check_repos` repeated the request.
  - **Fix.** It now stores `description`, `default_branch`, `archived` and `pushed_at` from that response, plus `meta_checked_at` and the ETag, so the next check is conditional (and a 304). Test `test_set_installed_records_the_metadata_check` (fields and etags written; a following `check_repos` makes no `/repos/{o}/{n}` request).
- **#47, UX: the MCP `refresh_engagement` had no default budget.** `time_budget_s=None` meant unbounded while the CLI defaults to 180 s, and the tool called itself budgeted.
  - **Fix.** The tool now defaults `time_budget_s=180` (the library keeps `None` so callers choose), and the docstring says so. Test `test_refresh_engagement_defaults_to_the_cli_time_budget`; SPEC §1/§5 updated.
- **#47, UX: the refresh never said what it refreshed.** `days` was clamped to `lookback_days` silently and the result had no window.
  - **Fix.** `EngagementResult` now carries the effective `days` and `since`, plus a `hint` naming the cap (`days capped to 60: the mirror's lookback is 60 days; backfill to widen it`). Tests: the main engagement test asserts `days == 14` and no hint; `test_refresh_engagement_echoes_a_capped_window` asks for 120 and gets `days: 60`, `since`, and the hint. SPEC §1 result line updated.
- **Not changed:** `sync --days`/`--budget` still mean different things under `--refresh-engagement`. The help text says so and `--repair-coverage` sets the precedent; a separate command is a naming preference, not a bug.

### What to test
1. `uv run pytest -q` → 385 passed, 1 skipped; `uv run ruff check` and `uv run ruff format --check` clean.
2. **#48 (read-only).** `uv run sparkwatch global-digest --since 2026-10-25 --until 2026-11-08 --basis written --no-advance --json` → `hot_topics_total 147`, `cursor null`, no `backfilled` key; top 3 `386125` 19.945, `384438` 17.056, `381350` 16.087. The same window with `--basis seen` → `hot_topics_total 0`; `sqlite3 -readonly data/sparkwatch.db "select name, last_seen_at from digest_cursors"` is unchanged (no `global` row).
3. **#48 CLI/MCP.** `uv run sparkwatch global-digest --since 2026-11-01 --basis sideways` → exit 1, `error: invalid basis …`; `--basis written` text shows `(basis written)` and no `backfilled:` line. In MCP Inspector, `global_digest(since="2026-10-25", until="2026-11-08", basis="written")` returns the same 147, and `global_digest(since="cursor", basis="written")` is an error.
4. **#50 row 5, already repaired live (2026-11-08 21:45).** Confirm it: `sqlite3 -readonly data/sparkwatch.db "select id, as_of from research_notes order by id"` → all five rows full timestamps, row 5 `2026-11-08T00:00:00+00:00`; `select count(*) from research_notes where as_of not glob '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'` → 0.
   - The rehearsal copy `data/sparkwatch.db.backup-50` shows the same before/after, and `data/sparkwatch.db.backup-before-asof-20261108` is the pre-repair backup if you want to compare.
   - To re-check the write path: `save_note(subject=…, body=…, as_of="2026-11-08")` returns and stores `2026-11-08T00:00:00+00:00`, and `as_of="last tuesday"` is an `invalid as_of` error with no row added.
5. **#49, copy first.** Before: rows 69 (watched, `-v1.1-testing`, 3 mentions) and 71 (unwatched, `-zig-testing`, 3 mentions), neither with a `github_id`.
   - `sqlite3 data/sparkwatch.db ".backup data/sparkwatch.db.backup-49"`, then `SPARKWATCH_GENERAL__DB_PATH=data/sparkwatch.db.backup-49 uv run sparkwatch repos check` → 21 requests, `merged: []`, row 69 renamed to `…-tensorfold-zig` with `github_id 1424520842`; then `SPARKWATCH_GENERAL__DB_PATH=data/sparkwatch.db.backup-49 uv run sparkwatch repos check --repo gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-zig-testing` → 4 requests, `merged: [{from: 71, to: 69}]`.
   - After: `select id, url, watch from repos where id in (69,71)` → one row 69, canonical URL, `watch 1`; `select repo_id, count(*) from repo_mentions where repo_id=69` → **6**.
6. **#49 live, same two commands in that order** (`uv run sparkwatch repos check`, then `uv run sparkwatch repos check --repo gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-zig-testing`) → the same end state on the live DB; `uv run sparkwatch repos check` afterwards reports one repo and makes no merge. `get_repo("gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-zig")` then lists all 6 mentions.
7. **#49 MCP.** `set_installed(repo="gilesco/glm-5.3-flash-exl3-2x-gb10-tensorfold-zig-testing", ref="<SHA>")` on a copy resolves the id, merges into the watched row, and the response names the canonical repo; a second call for the same repo makes no metadata request because the id is stored.
8. **#48 review fix, forum filter under `written`.** `uv run sparkwatch global-digest --since 2026-10-25 --until 2026-11-08 --basis written --forum 721 --no-advance --json` → 114 topics, every `hot_topics[].forum_id` 721, `first_linked.count` 88 (repos/HF linked from 721 posts only); `--forum 723` → 33 topics, `first_linked.count` 32. The two topic counts add up to the unfiltered 147, and neither section shows the other forum's repos.
9. **#49 review fix, queued rows.** `uv run pytest tests/test_repos.py -q -k "watched_rows_sharing or records_the_metadata_check"` → 2 passed; the two-watched-rows case finishes with `checked: 1`, `merged: [{from: 2, to: 1}]` and one row left instead of a `TypeError`.
10. **#47 review fixes.** `uv run pytest tests/test_sync.py -q -k engagement` and `uv run pytest tests/test_mcp_tools.py -q -k refresh_engagement` → all pass. On a copy, `SPARKWATCH_GENERAL__DB_PATH=data/sparkwatch.db.backup-50 uv run sparkwatch sync --refresh-engagement --days 120 --budget 5` → `days: 60`, a `since` about 60 days back, `hint: "days capped to 60: …"`, and `partial: true` if 5 s runs out; the MCP tool called with only `days` uses a 180 s budget instead of running unbounded.

## #48–#50 re-test (2026-11-08 21:25, `fix/run-a-issues` @ 1947cdc)

**All passed.**
- 385 passed, 1 skipped. ruff check and ruff format are clean.
- **#48 (live DB, read-only):** `--since 2026-10-25 --until 2026-11-08 --basis written` returns 147 topics.
  - The top 3 are 386125 at 19.945, 384438 at 17.056 and 381350 at 16.087.
  - My independent SQL ranking by `created_at` gives 19.945, 17.056 and 16.086, and the 0.001 difference is component rounding.
  - The components sum to the score for every topic. The response has `cursor: null` and no `backfilled` key, `next` carries `basis: written`, and it's 8.2 KB compact.
  - `--basis seen` for the same window returns 0.
  - `--forum 721` gives 114 topics and `--forum 723` gives 33, which matches SQL (147 = 114 + 33), with every row in the requested forum.
  - `--basis sideways` and `--since cursor --basis written` each print a one-line error and exit 1.
  - The text header reads `(basis written)` and has no backfilled line.
  - `digest_cursors` still has 3 rows and no `global`.
- **#50:** all five `research_notes.as_of` values are full timestamps, with row 5 now `2026-11-08T00:00:00+00:00`.
  - The fix session wrote this live (commit 6d01c14), although live writes had been left to the user. The value is correct and a backup was taken, so no action is needed.
- **#49 (on a `.backup` copy, live GitHub):**
  - Before: rows 69 (watched, 3 mentions, 2 events) and 71 (unwatched, 3 mentions), 414 repos.
  - `repos check` made 12 requests. Row 69 became `…-tensorfold-zig` with `github_id 1424520842`, and nothing merged.
  - `repos check --repo …-zig-testing` made 1 request and returned `merged: [{from: 71, to: 69}]`. After it there's one row: 69, with 6 mentions and 3 events. That leaves 413 repos, the 1131 mentions unchanged, and the 4 installs intact. `foreign_key_check` is clean and `integrity_check` returns ok.
  - A third `repos check` returned `stopped_early: true` after 1 request, because GitHub's anonymous limit was exhausted (`remaining 0/60`, reset 22:07). It stopped cleanly with no errors, which is the intended behaviour.

**Note, not a bug:** five watched repos at about 4–5 requests each is roughly 25 requests per `repos check`. Anonymous GitHub allows 60 per hour, shared by everything on this IP; today that included rehearsals by two sessions. `GITHUB_TOKEN` is already supported (`repos.py:63`). A fine-grained token with no scopes, used read-only for public repos, raises the limit to 5000 per hour.

### #51 (enhancement, small): read secrets from a `.env` file
- **Why:** `GITHUB_TOKEN` currently has to be exported in the shell that starts Claude Code, so the MCP server inherits it. A `.env` in the project works the same for the CLI and MCP, with no shell setup and no token copied into `~/.claude.json`.
  - A token is now recommended: on 2026-11-08 the anonymous 60/hour limit ran out (see the #48–#50 re-test).
- **Spec:** SPEC Configuration, "Secrets" (revised 2026-11-08).
  - `load_dotenv(PROJECT_ROOT/.env, override=False)` in `config.py`, so environment variables win. Add the dependency with `uv add python-dotenv`.
  - `SPARKWATCH_ENV_FILE` overrides the path, and tests point it at a missing file.
  - `.env` goes in `.gitignore`, with a committed `.env.example`.
  - Secret values are never output. `repos check` reports `auth: token|anonymous`.
  - Also fixes the SPEC line that said anonymous is plenty.
- **Acceptance:** the build plan's "Run A follow-up (ISSUES #51)". Do not create a real `.env`; the user will add the token.

## #51 fix: secrets from `.env` (2026-11-08, implemented by the review session at the user's request)
- **Dependency:** `uv add python-dotenv`.
- **`config.py`:**
  - `env_file_path()` returns `SPARKWATCH_ENV_FILE`, else `PROJECT_ROOT/.env`.
  - `load_env_file()` calls `load_dotenv(path, override=False)` only if the file exists.
  - `get_settings()` calls it first, so the CLI and the MCP server load it before any `GitHubClient` reads `GITHUB_TOKEN`.
- **`repos.check_repos`** reports `auth: "token"|"anonymous"`.
- **`.gitignore`** has `.env`; `.env.example` lists `GITHUB_TOKEN`, `BRAVE_API_KEY` and `SPARKWATCH_LLM__API_KEY`, empty. README updated.
- **Tests:**
  - An autouse `_no_env_file` fixture points `SPARKWATCH_ENV_FILE` at a missing file.
  - New tests check that the env file loads and an exported variable wins, that a missing file is fine, that `get_settings` loads the file, and that `auth` is reported in both modes without the token appearing in the result.
  - 390 passed, 1 skipped. ruff is clean. `git check-ignore .env` succeeds.
- **What to test:**
  1. `cp .env.example .env` and fill in `GITHUB_TOKEN`.
  2. `uv run sparkwatch repos check` should show `"auth": "token"`.
  3. Run `/mcp` and reconnect sparkwatch, then `check_repos_now` (or `sync_now`) should also show `auth: token`.

### #52 (P2): `repos check` reports every listed tag as new whenever the tags response changes
- **Observed (2026-11-08 22:12, the first authenticated `repos check`):**
  - `eugr/spark-vllm-docker` produced 9 `tag` events, `staging-current-1787746223` through `…-1772668553`.
  - Their names encode push dates from 2026-03-04 to 2026-08-26, but every event has `occurred_at = 22:12:13`.
  - None of them is new. The live `/tags?per_page=10` still lists `staging-current-1790687448` (2026-09-29) first, which is the stored `latest_tag`, followed by exactly those 9.
- **Cause:** `repos.py` (around line 632) computes `new_tags = [tag for tag in tags if tag != known]`, where `known` is the single stored `latest_tag`.
  - Any non-304 tags response therefore emits every listed tag except the latest as new.
  - Here the response changed only because its ETag differed. That probably comes from switching from anonymous to token auth, but any ETag change would do it, and a single genuinely new tag would also bring 9 false ones with it.
  - `occurred_at` is set to the check time, not the tag's date.
- **Impact:**
  - Noise in the `repos check` output, the digest's watched-repo events and `global_digest.repo_events`.
  - `repo_status().releases` (`kind IN ('release','tag')`) now lists 9 bogus "since install" tags for this repo, which misleads `should_i_update`.
- **Fix:**
  - Store the names seen in the last tags page, e.g. `etags['seen_tags']` (no migration), and only emit names not seen before.
  - The baseline records the set and emits nothing.
  - `occurred_at` should be the tag's commit date if it's worth one request per genuinely new tag; otherwise leave it null rather than claiming now.
- **Cleanup:** a documented one-off, run by the user after the fix: `DELETE FROM repo_events WHERE repo_id=87 AND kind='tag' AND detected_at='2026-11-08T22:12:13+00:00';` (9 rows).
- **Test:** two checks whose tag lists differ only in ETag should emit 0 events. Adding one new tag at the head should emit exactly 1.

## #52 fix: tag events keyed on the names already seen (2026-11-08)
- **`repos.py`:**
  - `_update_etags()` is the one read-modify-write on `repos.etags` (a dict of patches, `None` removes a key); `_save_etag`, `_save_meta_checked` and `_save_github_id` now delegate to it, and the tags branch patches `tags` and `seen_tags` in a single call. `seen_tags` holds the names listed by the last tags page, written on every non-304 tags response. No migration; `_etags()` is now typed `dict[str, Any]` because the value is a list.
  - A `tag` event is emitted only for a name absent from `seen_tags`. `latest_tag` still records `tags[0]`.
  - `_has_event()` skips a name that already has a `repo_events` row before the date request, so `changed` never lists a tag the `INSERT OR IGNORE` would drop (a name that leaves the page of 10 and returns), and no request is spent on it.
  - If `seen_tags` is missing — the `watch_repo` baseline, or a row watched before this fix — the check records the names and emits nothing. Repo 87 therefore does not re-emit its 9 tags on its next check.
  - `_tag_commit_date()` spends one `GET /repos/{o}/{n}/commits/{sha}` per genuinely new tag (the `commit.sha` from the tags payload, else the tag name) and returns `commit.committer.date`, then `commit.author.date`. A 404, any `GitHubError` (including `RateLimitStop`), or a payload without a date gives `null`; the event is still created. No tag is new → no extra request.
  - `repo_status` now filters and orders its `releases` by `COALESCE(occurred_at, detected_at)`, so a tag with an unknown commit date still shows since the install; its stored `occurred_at` stays `null`.
- **Tests (395 passed, 1 skipped; ruff check and format clean):**
  - ETag-only change with the same three names → `changed: []`, 0 tag events, `seen_tags` refreshed.
  - One new tag at the head → exactly 1 event, `occurred_at` from the mocked commit, exactly one `/commits/<new sha>` call and none for the older tags.
  - A new tag whose commit lookup 404s → 1 event with `occurred_at: null`.
  - A row with `latest_tag` but no `seen_tags` (the repo 87 shape) → 0 events, 10 names recorded.
  - The baseline test now also asserts `etags['seen_tags'] == ['v0.11.0']`.
  - `repo_status` lists a tag with `occurred_at` NULL when it was detected after the install date.
  - A tag that returns to the page after dropping off (its event already exists) → no second event, `changed: []`, and no `/commits/` request.
- **Review follow-up (code review of 14a0ba6):** the payload could claim a tag whose `INSERT OR IGNORE` was dropped, and the tags branch wrote `repos.etags` three times; `_has_event()` and `_update_etags()` fix both, and the returning-tag test pins the first. The last-page semantics stay as specified: a name that leaves the page and comes back is "new" again, but its single `repo_events` row means at most one event and no repeat in `changed`.
- **Rehearsal (on `data/sparkwatch.db.backup-52`, a `sqlite3.backup()` copy; live DB untouched):**
  - `repos check --repo eugr/spark-vllm-docker` → 3 requests, `auth: token`, `changed: []`. The tags ETag matched, so nothing was written.
  - With the copy's tags ETag removed, the next check fetched all 10 names → `changed: []`, `seen_tags` = the 10 `staging-current-*` names, tag events still 9.
- **#51 review (c0b7757), read at 041cf50:**
  - The `.env` support is sound. `load_dotenv(path, override=False)` runs from `get_settings()`, so both the CLI and the MCP server pick it up before `GitHubClient` reads `GITHUB_TOKEN`; the lazy `from dotenv import …` keeps the dependency soft; the autouse `_no_env_file` fixture stops a real `.env` leaking into the suite; `auth` comes from `client.token`, so no secret reaches the payload. The `.env` in the worktree is the user's own.
  - Noted, not changed: `_merge_by_github_id` unions etags with the winner's values winning, so a survivor with a stale `seen_tags` can emit one tag after a merge. Same class as #52, harmless while tags only come from the page of 10.
- **What to test (user, on the live DB):**
  1. `SELECT COUNT(*) FROM repo_events WHERE repo_id=87 AND kind='tag' AND detected_at='2026-11-08T22:12:13+00:00';` → **9**.
  2. `DELETE FROM repo_events WHERE repo_id=87 AND kind='tag' AND detected_at='2026-11-08T22:12:13+00:00';` → **9 rows deleted**. Then the same `SELECT` returns 0 and `repo_status --repo eugr/spark-vllm-docker` no longer lists those 9 tags. (Not run here; live writes stay with the user.)
  3. `uv run sparkwatch repos check --repo eugr/spark-vllm-docker` → `changed: []`, `auth: token`, and `repos.etags['seen_tags']` holds the 10 current names.
  4. `uv run sparkwatch repos check` → no `tag` entries in `changed` for repos whose tags page only changed ETag.
  5. Optionally `check_repos_now` from the MCP session gives the same result.

## #52 re-test (2026-11-08 22:35, `fix/run-a-issues` @ 23a5539)
- 396 passed, 1 skipped. ruff is clean. `-k "tag or seen_tags"` gives 6 passed.
- **On a `.backup` copy, with the token:**
  - The cleanup `DELETE` removed 9 rows.
  - `repos check --repo eugr/spark-vllm-docker` made 3 requests with `changed: []`, and the 9 tags didn't re-emit.
  - A full `repos check` made 15 requests with no tag entries.
  - The repo merge `repos check --repo …-zig-testing` made 4 requests and returned `merged: [{from: 71, to: 69}]`. Row 69 now has 6 mentions, `foreign_key_check` is clean and `integrity_check` returns ok.
- **But see #53:** after those checks, repo 87 still has no `seen_tags`.

### #53 (P2): watched repos from before #52 never get `seen_tags` while their tags page is unchanged, so the first real new tag is silently swallowed
- **Observed (on the copy):** after `repos check --repo eugr/spark-vllm-docker` and a full `repos check`, `etags` for repo 87 has `tags` (an ETag) but no `seen_tags` key. The tags request returned 304, so the branch at `repos.py:674` (`if status != 304 …`) never ran.
- **Why it matters:**
  - All 5 watched repos were watched before #52, so none has `seen_tags`.
  - Their stored tags ETags keep returning 304 until the tag list actually changes, and the usual reason it changes is a **new tag**.
  - On that response `seen` is `None`, so the code records the page and emits nothing (`isinstance(seen, list)` is false). The first genuine new tag on every existing watched repo is lost.
  - The digest, `repo_status` and `should_i_update` never hear about it.
- **Fix (either):**
  - **(a) Seed on the next check:** when `seen_tags` is missing on a non-baseline row, send the tags request **without** `If-None-Match` once. That costs one request per legacy repo, once, and records the set while nothing has changed.
  - **(b) Seed from `latest_tag`** when `seen_tags` is missing: names listed *before* the stored `latest_tag` are new, and the rest are seen. If `latest_tag` isn't on the page, record and emit nothing.
  - (a) is simpler and exact. (b) needs no extra request but is wrong if GitHub's ordering isn't by recency (`/tags` order isn't guaranteed chronological).
- **Test:**
  - A legacy row with no `seen_tags` whose tags endpoint 304s seeds `seen_tags` on the next check under (a), or stays correct under (b).
  - A legacy row whose next tags response adds one tag at the head emits exactly that tag.

**Housekeeping, not a bug:** `data/` now holds 5 full-size backups from the rehearsals (`sparkwatch.db.backup-*`, about 62 MB each). It's gitignored. The user can delete the ones they don't want; nothing reads them.

## #53 fix: a legacy row seeds `seen_tags` with one unconditional tags request (2026-11-08, option (a))
- **`repos.py` (tags block):**
  - `seen = _etags(row).get('seen_tags')` is now read **before** the request. The request carries `If-None-Match` only when `seen` is a list; otherwise it goes out unconditional, so a 200 always arrives and the set is recorded. A 304 could never record it, which is what swallowed every legacy repo's first new tag.
  - The seed costs one extra request per legacy row, once. After it, `seen_tags` exists — `[]` included — and the check is conditional again.
  - Nothing else changes: a row with no recorded set records the page and emits nothing (baseline semantics), so repo 87's 9 deleted tags stay deleted. A tag published while the row was legacy is still swallowed on the seeding response; that is the price of (a), and (b)'s `/tags` ordering assumption was rejected.
- **`tests/conftest.py`:** `GitHubMock.add()` accepts a callable payload, `(request) -> httpx.Response`, so a test can emulate GitHub's ETag matching. Existing payloads are untouched.
- **Tests (399 passed, 1 skipped; ruff check and format clean):**
  - `test_a_legacy_row_behind_a_304_seeds_seen_tags`: the row's ETag would 304 a conditional request; the fix sends none, the page is recorded, `changed: []`, 0 events, the ETag is kept.
  - `test_a_seeded_legacy_row_emits_one_new_tag_at_the_head`: seed check emits nothing, the next response (new ETag) adds one tag at the head → exactly 1 event with its commit date, and the tags request is conditional again.
  - `test_an_empty_tags_page_still_goes_conditional_next_time`: `seen_tags: []` counts as seeded, so a tagless repo doesn't fetch unconditionally forever.
  - `test_a_tag_published_while_the_row_was_legacy_is_swallowed_by_the_seed`: the seeding response records a tag it cannot diff and emits nothing; the next genuinely new tag is emitted with its date. Pins the documented price of (a).
  - `test_a_row_without_seen_tags_treats_the_tags_page_as_a_baseline` now also asserts the tags request carries no `If-None-Match`.
- **Rehearsal (on `data/sparkwatch.db.backup-53`, a `sqlite3.backup()` copy; live DB untouched):**
  - The copy's repo 87 had `tags` but no `seen_tags`, exactly the live state. One `repos check --repo eugr/spark-vllm-docker` → 3 requests, `auth: token`, `changed: []`, and `seen_tags` recorded with the 10 `staging-current-*` names. The second check left it unchanged.
  - Live repo 87 is still `has_seen: 0`, `last_checked_at 22:12:13`, 9 tag events: the live writes are the user's.
- **What to test (user, live):**
  1. Run the cleanup from #52 if you haven't: `DELETE FROM repo_events WHERE repo_id=87 AND kind='tag' AND detected_at='2026-11-08T22:12:13+00:00';` → 9 rows.
  2. `uv run sparkwatch repos check --repo eugr/spark-vllm-docker` → `changed: []`, `auth: token`, and repo 87's `etags` now has `seen_tags` with 10 names.
  3. `uv run sparkwatch repos check` → every watched repo gains `seen_tags`; no `tag` entries in `changed`.
  4. `uv run sparkwatch repo_status --repo eugr/spark-vllm-docker` → `releases` no longer lists the 9 deleted tags.
  5. When a genuinely new tag appears, `changed` lists exactly that tag with the tag's commit date as `occurred_at`.

## #53 re-test (2026-11-08 22:50, `fix/run-a-issues` @ 8ce887c)
- 400 passed, 1 skipped. ruff is clean.
- **On a `.backup` copy, with the token:**
  - The cleanup `DELETE` removed 9 rows.
  - A full `repos check` made 15 requests with `changed: []`. Every watched repo now has `seen_tags`: 87 has 10 names, and the four tagless repos have `[]`.
  - **Simulated real new tag:** I removed the head name from 87's `seen_tags` and invalidated its tags ETag. The next check made 4 requests (3 plus 1 for the tag's commit date) and emitted exactly `staging-current-1790687448` with `occurred_at 2026-09-28T17:39:45Z`.
  - An immediate re-check made 3 requests with `changed: []`.

### #54 (P3): repo event `occurred_at` stores GitHub's `…Z` format, not the canonical `+00:00`
- **Observed:** the tag event above stored `2026-09-28T17:39:45Z`. The timestamp scanner flags `repo_events.occurred_at` as MIXED on the copy. The live DB is still clean, because no real tag or release has been stored since migration 4.
- **Cause:** `_tag_commit_date` returns GitHub's `committer.date` verbatim, and the release path passes `release["published_at"]` verbatim (`repos.py`, `occurred_at=release.get("published_at")`). This breaks SPEC §2's single timestamp format (#27), and `repo_status` compares `COALESCE(occurred_at, detected_at)` as strings.
- **Fix:** normalise both with `to_iso(parse_dt(...))` before `_event`. Add a test that a `…Z` release or tag date is stored as `+00:00`.
