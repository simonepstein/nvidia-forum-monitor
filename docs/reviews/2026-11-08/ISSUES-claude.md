# Issues found in interactive testing

Found while testing the Run A build at `2f8342c`, on 2026-11-08, against the live mirror (`data/sparkwatch.db`) and over MCP stdio with a small client script.

These are for the originating model to **verify and fix**. Each issue gives a repro, what was expected, and the evidence. Severity: **high** means wrong results, **medium** means wasted tokens or misleading output, **low** means polish.

---

## ISSUE-1 (high): time-window counts use when sparkwatch saw a post, not when it was written

**Where:**
- `list_watchlist`: `hits_7d` and `hits_30d`
- `model_summary`: `mentions.7d`, `mentions.30d` and `mentions.60d`

**Repro:**
1. Do a fresh sync (or a backfill).
2. Call `list_watchlist()`, or `model_summary("GLM 5.3 Flash")`.

**Observed:** every window has the same count.
- `list_watchlist`: GLM 5.3 Flash `hits_7d=676, hits_30d=676`; DeepSeek V4.1 Flash `568/568`; Qwen3.8 Flash Next `506/506`.
- `model_summary("GLM 5.3 Flash")`: `mentions = {"7d": 942, "30d": 942, "60d": 942}`.

**Expected:** the windows should be based on the post's `created_at`, so they show whether a model is gaining or losing attention.
- The digest is right to use `first_fetched_at` / `changed_at` for *novelty* ("what's new since I looked").
- For *activity* windows, the right field is post time.

**Probable cause:** the counts filter on `watch_hits.created_at`, which is when the hit was recorded.

---

## ISSUE-2 (medium): `model_summary` response is too large, because of an unranked repo list

**Repro:** `model_summary({"model": "GLM 5.3 Flash"})`

**Observed:**
- The response is about 35 KB.
- Most of it is `repos`, with **190 entries**, mostly unrelated to GLM serving (e.g. `swe-agent/mini-swe-agent`), in no useful order.

**Expected:**
- This tool is the starting point of `serve_model`, so it should be compact.
- Rank repos by the number of mentions in this model's topics, and cap the list at about 10. Watched repos always go first, with their install status. Report the total count.
- Apply the same thinking to the other sections, so a summary fits in a few KB.

---

## ISSUE-3 (medium): `model_summary` leaves out flags on the model's topics

**Repro:** `model_summary({"model": "GLM 5.3 Flash"})`

**Observed:**
- `flags: []`.
- But open flag #1 is on topic **382140**, a GLM 5.3 Flash thread: "Thread title claims 60 tok/s … another user reports ~26.4 tok/s".
- That's exactly the warning an agent needs before quoting performance figures.

**Expected:** include open flags whose target is the model, its recipes, or any of its `top_topics` / topics with mentions.

---

## ISSUE-4 (medium): the Qwen4 watch regex matches architecture code names

**Seed regex:** `qwen[\s._-]*4(?![\d.])`

**Observed:** 38 hits across 12 topics (as of the partial sync of 721). Some come from vLLM internals that belong to **Qwen3.8 Flash Next**, not Qwen4:
- `Resolved architecture: Qwen4ExpForConditionalGeneration`: topic 382733
- `vllm/models/qwen4_exp/nvidia/b12x_qsa.py`: topic 382733
- `**Qwen3.8-Flash-Next (Qwen4Exp)**`: topic 382733
- `qwen4_exp HyperConnection classes`: topic 388002

The pattern would also match "Qwen 4B", a parameter size, if anyone writes it.

**Suggested fix:**
- Change the regex to `qwen[\s._-]*4(?![\d.])(?![\s_-]*exp)(?!\s*b\b)`.
- Add test strings: `Qwen4ExpForConditionalGeneration`, `qwen4_exp`, `Qwen 4B` (none of these should match); `Qwen4 Flash`, `Qwen 4 27B` (both should match).

---

## ISSUE-5 (medium, design): the "first sighting" alert for anticipated models fires on speculation

**Observed:**
- The Qwen4 sighting pinned at the top of the digest is post 1882087 (topic 380257): "looking ahead, I'm betting that the arrival of Qwen 4 27B…".
- Most of the 38 Qwen4 hits are rumours, e.g. "confirmed - Qwen 4 releasing next week" (386125, 2026-11-01) and "Qwen 4 27B has been recently leaked" (386125, 2026-10-26).
- None is a release.

**Problem:** for a much-anticipated model there's speculation all the time, so the one-off "first sighting" alert says little. Once it's acknowledged, later posts showing a *real* release get no special treatment.

**Options for the fix** (spec owner to decide):
- Keep the alert, but have `catch_up` tell the agent to classify anticipated-model hits as rumour or release, and to `flag` only a release. That's prompt text only.
- Add `ack_watch_sighting(label, rearm=True)` so the alert fires again on the next hit after an acknowledgement.
- Optionally, add a stricter `release_regex` per anticipated entry, e.g. a Hugging Face URL matching the model name, or "weights are out/released".

---

## ISSUE-6 (low): seeded watchlist entries have `canonical = null`

**Repro:** `list_watchlist()`

**Observed:** all four entries have `"canonical": null`, even though `models` and `model_aliases` contain `glm-5.3-flash` and the others.

**Expected:** the seed loader, or the first match, should set `watchlist.canonical` when the label resolves through the alias table. The spec says `canonical` is "set once it resolves to a models row". This tie makes it reliable to join a watch entry to its recipes, notes and summaries.

---

## ISSUE-7 (low): the CLI `digest` prints Python dict reprs

**Repro:** `uv run sparkwatch digest --no-advance`

**Observed:** section items are printed as raw `{'label': 'Qwen4', 'title': …}` dicts, wrapped across lines. The output isn't valid JSON and isn't readable markdown either.

**Expected:** either readable lines (e.g. `- [Qwen4] <title> — <url>`), or a `--json` flag that prints the same JSON the MCP tool returns. Ideally both.

---

## ISSUE-8 (low, verify): every tombstone has `reason = 'hidden'`

**Context:** the full 60-day sync of forum 721 made 475 requests in 8 minutes 12 seconds, with 0 errors and `partial=false`. It added 82 tombstones, giving 84 in `missing_posts` in total.

**Observed:** `select reason, count(*) from missing_posts group by reason` returns only `('hidden', 84)`. None are `absent_from_batch`, `deleted` or `forbidden`.

**To verify:**
- Were these posts really returned with `hidden: true`?
- Or does the code fall back to `hidden` for IDs missing from `posts.json` batches? Those should be `absent_from_batch` (SPEC §1, step 4.5).

Spot-check a few IDs against the recorded responses or the live API.

---

## ISSUE-9 (high, safety): the rate limit can be switched off by config

**Where:** `src/sparkwatch/discourse.py:41`: `self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0`

**Observed:**
- `requests_per_second <= 0` means **no limit at all**.
- There's no upper clamp, so `1000` is accepted (see `tests/test_config.py:26`).
- The OpenFox registration left by the build set it to 1000, with `sync_now` and `check_repos_now` still pointed at the live forum and GitHub (see the process notes).
- I checked `sync_runs` in both databases: it never actually ran at that rate. All live syncs were CLI runs at about 1 request per second.

**Expected:**
- When `forum.base_url` is the real forum, validate `0 < requests_per_second <= 1.0`, or perhaps up to 2.0, and refuse to start otherwise.
- Tests and evals that need speed should mock the network, which they already do in-process. Alternatively, an explicit `SPARKWATCH_ALLOW_FAST=1` could apply only when `base_url` isn't the live host.
- Add a test that the settings reject 1000 and 0 for the live base URL.

---

## ISSUE-10 (high): `--max-pages` marks the full window as covered, leaving a gap that's never filled

**Repro:**
1. Start with a fresh database.
2. Run `sparkwatch sync --forum 723 --max-pages 1`.
3. Run any later incremental sync.

**Observed:**
- Forum 723 has only 30 topics, with `bumped_at` from 2026-10-25 to 2026-11-07 (listing page 0).
- But `forums.backfill_floor = 2026-09-09T12:27:53` (a full 60 days), and `last_synced_at` keeps moving forward on every later sync.
- Incremental syncs stop at `last_synced_at`, so topics in 723 last active between 2026-09-09 and 2026-10-25 are **never fetched**.
- Compare forum 721, synced without `--max-pages`: 339 topics over the full window.

**Expected:** when a sync stops because of `max_pages` (or the time budget) before reaching the floor, treat it like `partial`:
- don't advance `backfill_floor` past the oldest `bumped_at` actually listed;
- don't advance `last_synced_at`, or keep a separate "listing reached" marker;
- the next sync should carry on paging back until it reaches the floor.

The same reasoning applies to `sync_now`'s time budget during listing.

**Test:**
1. Sync with `max_pages=1` against fixtures that have 2 or more listing pages inside the window.
2. Run a second sync with no page limit.
3. The second sync should fetch page 1's topics.

---

## Process notes (not code)

- **The build went straight onto `main`,** not onto a `build/<model>` branch from the `spec` tag. It left 5 commits that aren't pushed to `origin/main`. One commit message is "test commit: scaffold project metadata".
- **There's no record of review pause 1** (after Stage 2). `tests/evals/pause2_report.md` covers pause 2 only.
- **The OpenFox MCP registration left by the build is unsafe for live use.**
  - `~/.config/openfox/config.json` points `SPARKWATCH_GENERAL__DB_PATH` at `tests/evals/frozen.db`, and sets `SPARKWATCH_FORUM__REQUESTS_PER_SECOND=1000`.
  - `sync_now` and `check_repos_now` still reach the live forum and GitHub, so the first `catch_up` would make requests with effectively no throttling.
  - Evals that need a high rate should mock the network, or override `forum.base_url`, never just raise the limit.
  - The config also arguably shouldn't let `requests_per_second` go above 1.0 without an explicit opt-in.
- **The checkpoint run wrote to the live database:** recipe #1, note #1 and flag #1 in `data/sparkwatch.db`. That's harmless and the content is reasonable, but agent eval runs should normally use `tests/evals/frozen.db`, or the evals stop being repeatable.

---

## Verified OK in this session

| Check | Result |
|---|---|
| `uv run pytest -m "not live"` | 192 passed, 1 skipped, about 13 s, no network |
| Re-sync of forum 723, one page | 1 request, 0 new posts, 1.3 s |
| Concurrent `sync` while another runs | `{"busy": true}`, exit code 2, 0.25 s, no requests |
| Read (`digest --no-advance`) during an active sync | 0.24 s, not blocked |
| MCP over stdio | 39 tools, 4 prompts and 3 resources, matching SPEC §5 |
| `catch_up` prompt text | matches the spec |
| `sql_query` aggregate over `watch_hits` | correct, with columns and rows |
| `model_summary` top topics for GLM 5.3 Flash | relevant: 381350 ("320B total / 18B active"), 382939 ("…NVFP4 for 2× DGX Spark") |
