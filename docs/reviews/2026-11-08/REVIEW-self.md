# sparkwatch Run A — interactive test pass (`REVIEW-self.md`)

Agent-driven pass over the `sparkwatch` MCP server against the live 60-day mirror.
Session date: 2026-11-08. Nothing was fixed: no code, tests, SPEC or config were edited.
Writes made to `data/sparkwatch.db`: 1 recipe (id 2), 3 notes (ids 2, 3), 2 flags (ids 1, 2),
1 watched repo + install, digest cursor `default` and `tester`, 1 acked sighting (Qwen4).

Provenance check (before anything else):

```
sql_query: select (select count(*) from topics) t, (select count(*) from posts) p,
                  (select count(*) from missing_posts) m
-> t=369  p=7810  m=84
```

Matches the expected 369 / 7,810 / 84, so the server is on the synced database, not
`tests/evals/frozen.db`.

---

## Findings

### F1 — `sync_now` can never succeed with its default `include_repos=true` (high)

- **Tool:** `sync_now`
- **Repro:**
  ```
  sync_now()                                  -> {"busy": true, "job": "sync", "started_at": "<now>"}  (twice)
  sync_now(include_repos=false)               -> {"requests": 2, "partial": false, ...}               OK
  ```
- **Observed:** the default call returns `busy` immediately, every time, and reports the holder as
  job `sync` started at the moment of the call — i.e. itself. `include_repos=false` works.
- **Expected:** one sync, then a repo check, returning posts and repo changes.
- **Cause (code):** `mcp_server.py:183` takes `write_lock("sync", db_path)`, then `:197` takes
  `write_lock("repos", db_path)` inside it. `lock.py:lock_path()` maps *every* job to the same
  `data/sparkwatch.lock`, and `flock(LOCK_EX|LOCK_NB)` on a second fd of the same file fails even in
  the same process, so the nested acquire raises `LockBusy` and the whole tool returns busy.
- **Impact:** the documented entry point for "catch me up" (and the `catch_up` prompt, step 1) is
  dead; the agent must guess `include_repos=false` and then call `check_repos_now` separately.

### F2 — `repo_status` inverts ahead/behind; contradicts the digest (high)

- **Tool:** `repo_status`
- **Repro:**
  ```
  watch_repo("gilesco/GLM-5.3-Flash-EXL3-2x-GB10-TensorFold-v1.1-testing")
  set_installed(repo, ref="b4c93e0bb043", installed_at="2026-11-08T04:00:00Z")
  repo_status(repo) -> "status": "ahead", "ahead_by": 4, "behind_by": 0
  digest()          -> watched_repos[0].installed.commits_behind = 4
  ```
- **Observed:** the install is 4 commits *behind* head, but `repo_status` says `ahead`, `ahead_by: 4`.
- **Expected:** `behind_by: 4` / `status: "behind"` from the installer's point of view, agreeing with
  the digest's `commits_behind: 4`.
- **Cause (code):** `repos.py:659-661` copies GitHub's `compare/{base}...{head}` fields verbatim.
  GitHub's `ahead_by` counts commits head has that base lacks — from the installed SHA that is
  "how far behind am I". No relabelling happens.
- **Impact:** the tool that exists to answer "should I update?" reports the opposite direction, and the
  two sparkwatch surfaces disagree about the same repo.

### F3 — `model_summary` mention windows are wrong (high)

- **Tool:** `model_summary("GLM 5.3 Flash")`
- **Repro:** `model_summary("GLM 5.3 Flash")` → `mentions: {"7d": 1163, "30d": 1163, "60d": 1163}`
- **Observed:** all three windows identical, 1,163 each.
- **Expected:** counts of posts written in each window. Ground truth from `posts`:
  ```
  select sum(created_at >= datetime('now','-7 days')), sum(created_at >= datetime('now','-30 days')),
         sum(created_at >= datetime('now','-60 days')), count(*)
    from posts where lower(raw) like '%glm-5.3-flash%'
  -> 13 / 83 / 245 / 277
  ```
- **Cause (code):** the windows are counted over `watch_hits.created_at` (when sparkwatch recorded the
  hit), not `posts.created_at`; every hit was written by the same seed/backfill run, so the three
  windows collapse onto one number, and the number is hit rows (1,163), not posts (277).
- **Impact:** the only recency signal in the tool is unusable; the same defect shows in
  `list_watchlist` (`hits_7d == hits_30d` for all four watches).

### F4 — `search_posts` rejects the most natural model-name queries (high)

- **Tool:** `search_posts`
- **Repro:**
  ```
  search_posts("GLM 5.3 Flash")            -> Error: bad query: fts5: syntax error near "."
  search_posts("GLM 5.3 Flash dual spark tok/s NVFP4") -> ... syntax error near "/"
  search_posts("glm 53 flash")             -> works
  ```
- **Observed:** any query containing `.` or `/` is rejected before reaching SQLite; the query string is
  passed to FTS5 unescaped/quoted.
- **Expected:** the tool description says "full-text search the mirrored forum posts"; `GLM 5.3 Flash`
  and `tok/s` are exactly what an agent types.
- **Impact:** the agent must pre-sanitise model names (and loses precision: `glm 53 flash` matches table
  rows containing `53`). This is a workaround every session will have to rediscover.

### F5 — the live drift tests cannot pass (high)

- **Command:** `SPARKWATCH_LIVE=1 uv run pytest -m live`
- **Observed:** `3 failed in 0.54s` — every test raises
  `RuntimeError: network access is blocked in tests` from `tests/conftest.py:30`.
- **Expected:** 3 passing drift checks against the real forum/GitHub.
- **Cause (code):** `tests/conftest.py:21-30` installs an **autouse** `_block_network` fixture that
  monkeypatches `socket.socket.connect`; it applies to `tests/live/` as well.
- **Impact:** the documented drift detection (`README.md`, `CLAUDE.md`) has never been able to run.
  (Side effect: the live run made 0 network requests, so nothing was hammered.)

### F6 — `updated_topics.new_posts` counts posts *seen*, not new (high)

- **Tool:** `digest`
- **Repro:** `digest(limit=30)` on cursor `default` at `since=2026-11-08T12:28:40` →
  topic 381350 reports `new_posts: 570`; topic 381911 `370`; 386125 `367`.
- **Ground truth:**
  ```
  select (select count(*) from posts where topic_id=381350 and first_fetched_at >= '2026-11-08T12:28:40+00:00'),
         (select count(*) from posts where topic_id=381350 and created_at      >= '2026-11-08T12:28:40+00:00')
  -> 570 / 1
  ```
- **Expected:** `new_posts: 1` for that topic.
- **Cause (code):** `digest.py:_updated_topics` counts rows matching
  `(p.first_fetched_at >= since OR p.changed_at >= since)`; after a backfill every stored post of a
  bumped topic matches, so a topic with one new reply advertises its whole history.
- **Impact:** "what changed" is dominated by topics that claim hundreds of new posts; an agent will
  either waste tokens or wrongly conclude the thread exploded.

### F7 — digest output is machine-repr, not readable, and far too large (medium)

- **Tools/commands:** `uv run sparkwatch digest --cursor tester`, `digest(limit=30)`
- **Observed:**
  - CLI render: **41,853 bytes**, every item printed as a Python dict, e.g.
    `- {'label': 'Qwen4', 'title': 'Qwen3.8-27B at 34–38 tok/s ...', 'post_id': 1882087, ...}`
  - MCP JSON at `limit=30`: **74,617 bytes** (measured on the same cursor state); `repos_first_linked`
    alone contributes 30 entries with 300-char snippets, `watchlist_activity` 26 hits with snippets.
- **Expected:** compact markdown/JSON bullets well under the ~10 KB guidance in the brief.
- **Cause (code):** `digest.py:render_digest` does `lines.append(f"- {item}")` on dicts.
- **Impact:** the CLI digest is unusable by a human and the MCP digest is ~7x the size budget; the agent
  has to `sql_query` instead.

### F8 — watch hits carry the wrong date and an unrelated snippet (medium)

- **Tool:** `digest` (section `watchlist_activity`), `list_watchlist`
- **Repro:** `digest(limit=30)` → the Qwen3.8 Flash Next hits all show
  `"date": "2026-11-08T13:12:00+00:00"`; the GLM 5.3 Flash hit points at
  `https://forums.developer.nvidia.com/t/382476/3` with a snippet about a Qwen SGLang setup.
- **Observed:** the date is the hit-record timestamp (`watch_hits.created_at`, all written in one seed
  run), and the snippet is `posts.raw[:300]` — the post's opening words.
- **Expected:** the post's `created_at` (e.g. 2026-11-08T13:11:59Z / 2026-09-… for older posts) and a
  window of text around `matched_text`.
- **Cause (code):** `digest.py:_watch_activity` emits `"date": row["created_at"]` from `watch_hits` and
  `_snippet(row["raw"])` without using the match offset.
- **Impact:** an agent cannot date a sighting from the digest, and the snippet does not show why the post
  matched.

### F9 — `model_summary` is 91% repo noise (medium)

- **Tool:** `model_summary("GLM 5.3 Flash")`
- **Observed:** **52,599 bytes**, of which **47,886 bytes (335 entries)** are the `repos` array — every
  repo mentioned anywhere in the model's topics, e.g. `nvidia/cutlass`, `syllo/nvtop`,
  `aristocratos/btop`, plus gist misparses (`jetnet/15878adefe1f613be188d01bea918038`).
- **Expected:** a handful of relevant repos (or none), and a payload an agent can hold.
- **Impact:** the tool that the `serve_model` prompt tells the agent to call *first* spends most of its
  budget on unrelated tooling; the useful parts (recipes, notes, top_topics) are buried.

### F10 — `get_topic_overview.watch_hits` is a bare label list (medium)

- **Tool:** `get_topic_overview(386108)`
- **Observed:** 22 entries, 21 of them `{"label": "GLM 5.3 Flash", "matched": "GLM-5.3-Flash"}` — no
  post id, post number, date or URL; the same pair repeated once per matching post.
- **Expected:** the distinct watch hits with a post reference, or a count.
- **Impact:** ~600 bytes of the 2,330-byte payload say nothing actionable; it reads as "22 hits" without
  letting the agent jump to any of them.

### F11 — normalisation drops valid quant/engine values, so fit never fires (medium)

- **Tool:** `record_recipe`, `upsert_model`
- **Repro:**
  ```
  record_recipe(model="GLM 5.3 Flash", topology="dual-spark", status="works",
                quant="fp4", quant_repo="nvidia/GLM-5.3-Flash-FP4-Local",
                engine="flare-local-inference", ...)
  -> "quant": "other", "engine": "other", "fit": {"estimate_gb": null, "fit_ok": null, "warning": null}
  ```
- **Observed:** `fp4` (only `nvfp4`/`mxfp4`/`fp8`/… are mapped) and `flare-local-inference` both fall to
  `other`; `fit_estimate_gb` is null for *both* GLM recipes because `models.params_total_b` is null,
  although the thread title says "320B total parameters / 18B active".
- **Expected:** quant/engine usable as filters (`list_recipes(engine=...)`) and a fit estimate/warning
  for a 320B model on a 240 GB pair.
- **Impact:** recipe rows record the right raw text but are invisible to every structured filter, and the
  hardware-fit warning (a Run A feature) can never trigger for the mirror's flagship model.

### F12 — `missing_posts` reasons are uniform and imprecise (medium)

- **Tool/table:** `missing_posts` via `sql_query`
- **Observed:** all 84 rows `reason='hidden'`. Spot-checks:
  - post 1882484 (topic 388102): `post_type: 3`, `username: discourse_ai_spam`,
    `cooked: ""` — a spam-scanner small action, not hidden content.
  - post 1775329 (topic 363819): `post_type: 3`, `username: TomNVIDIA` — a moderator/notice action.
- **Expected:** the reason to distinguish "hidden by staff" from "not a content post"
  (`sync.py:365` labels every `post_type not in {1,2}` as `hidden`).
- **Impact:** the reasons are believable in the sense that these posts genuinely carry no content, but
  the single label makes the column useless for diagnosis; `tombstones_new` will also silently grow
  whenever Discourse records a small action.

### F13 — `busy` payload usually cannot name the holder (low)

- **Command:** two concurrent `uv run sparkwatch sync --forum 723 --max-pages 1`
- **Observed:** one exits 2 with `{"busy": true, "job": null, "started_at": null}`; a later `sync_now`
  collision reported `{"job": "sync", "started_at": "..."}` correctly.
- **Cause (code):** `lock.py:42-45` writes the holder record *after* taking the lock, so a reader that
  loses the race reads an empty file.
- **Impact:** the exit-2 message cannot say what is running or since when — the one thing it is for.

### F14 — gist URLs are stored as GitHub repos (low)

- **Tools:** `digest.repos_first_linked`, `list_repos`, `model_summary.repos`
- **Evidence:** `interactivetech/4f206bcaa6e3d70a3343ecf1090ffc66` (from
  `https://gist.github.com/interactivetech/4f206bcaa6e3d70a3343ecf1090ffc66`, post 383553/1),
  `0rand/646f0ceb931a8d8632f95f207d4701a5`, `jetnet/15878adefe1f613be188d01bea918038`.
- **Expected:** gists excluded, or flagged as gists.
- **Impact:** fake repos inflate every repo list and would fail `watch_repo`.

### F15 — `get_posts` metadata is page-scoped and splits posts mid-token (low)

- **Tool:** `get_posts`
- **Observed:** `post_count: 1` for `get_posts(386125, from_post=1, to_post=1)` although the topic has
  367 posts — the field is the filtered count, with no topic total. Pagination is char-offset based, so
  pages break mid-word (`…--- post 3 by 0rand (2026-0` | `8-26T15:08:18.249Z)`).
- **Expected:** `post_count` documented/named as the page's count plus a topic total; boundaries aligned
  to post or line breaks.
- **Impact:** low — contiguity is correct (see verified table) — but the field name misleads and every
  page re-renders the whole 1.09 M-char topic (570 rows) to slice 400 chars.

### F16 — state sections never empty out (low)

- **Command:** `digest --no-advance --cursor tester`, then `--cursor tester`, then again
- **Observed:** third run still lists `Watch sightings (1)` and `Open flags (1)`; only the event sections
  are empty. After `ack_watch_sighting("Qwen4")` the sighting disappears.
- **Expected:** arguably correct by design (the brief says the last run "should show empty sections"), but
  the digest mixes events with open state, so it can never be fully empty while a flag is open.
- **Impact:** low — but it means "nothing new" and "nothing in the digest" are different things, and an
  agent must remember to ack/resolve.

### F17 — free-text model names create model rows (low)

- **Tool:** `save_note`, `record_recipe`
- **Observed:** `save_note(model="dual-spark frontier", ...)` created canonical model
  `dual-spark-frontier`; the same path will happily create junk for any string.
- **Expected:** a model reference validated against `models`/aliases, or left null.
- **Impact:** low, and self-inflicted here — but the `frontier` prompt tells the agent to save
  `subject='frontier dual-spark'`, so the same junk is one careless `model=` argument away.

### F18 — renamed upstream repo leaves inconsistent URLs (low)

- **Tool:** `repo_status`
- **Observed:** repo url stays
  `https://github.com/gilesco/GLM-5.3-Flash-EXL3-2x-GB10-TensorFold-v1.1-testing` while every commit
  `url` points at `.../GLM-5.3-Flash-EXL3-2x-GB10-TensorFold-ZIG-testing/commit/...`; the GitHub API
  itself answers `301 Moved Permanently` for the old name (repository id 1424520842).
- **Expected:** one canonical URL (follow the rename) per repo.
- **Impact:** low; links in the same payload point at two different repos.

### F19 — GitHub requests are not paced (low)

- **Observation:** `DiscourseClient` enforces `requests_per_second` (1.0) via `RateLimiter`
  (`discourse.py:40-49`); `GitHubClient.get` (`repos.py:75-90`) has no interval, only the
  "rate limit nearly exhausted" stop. `watch_repo` baseline made 4 requests back-to-back.
- **Expected:** the same 1 req/s courtesy on GitHub, or an explicit note that it is unnecessary.
- **Impact:** low (a handful of requests fits the anonymous limit), but the politeness rule in
  `CLAUDE.md` reads as applying to both hosts.

---

## Verified OK

| # | Check | Result |
|---|-------|--------|
| 1 | Provenance (`sql_query` counts) | 369 topics / 7,810 posts / 84 missing — live DB, not `frozen.db` |
| 2 | `uv run sparkwatch sync --forum 723 --max-pages 1` | `requests: 1`, `posts_new: 0`, `partial: false` |
| 3 | Lock under concurrency | second process: `{"busy": true}` and exit code **2** |
| 4 | `digest --no-advance --cursor tester` → `--cursor tester` → again | first two identical (since 1970), third `since=2026-11-08T13:21:22` with event sections empty; cursor advanced exactly once |
| 5 | `uv run pytest -m "not live"` | **192 passed, 1 skipped** in 11.64 s |
| 6 | `sql_query("DELETE FROM posts")` | rejected: `only SELECT or WITH statements are allowed` |
| 7 | `get_posts(381350, max_chars=400)` + `next` | offsets 0→400→800→1200 contiguous (`2026-0`+`8-26T15:08:18`), final page `offset=1093153, truncated=false, next=null`, no overlap, no gap |
| 8 | `record_recipe` quote verification | 2/2 quotes verified against post 1877754; evidence URLs returned |
| 9 | `ack_watch_sighting("Qwen4")` | sighting removed from the next digest |
| 10 | `watch_repo` baseline | head `7dc3020…`, 4 requests, no events created |
| 11 | `flag` | ids 1 and 2 written and returned in `digest.open_flags` |
| 12 | `save_note` / `get_notes` | notes 2 and 3 round-trip with `as_of` and source ids |
| 13 | Politeness | forum requests went through the 1 req/s limiter; live tests made 0 requests |

---

## Workflow runs

### `catch_up` (MCP prompts are not exposed by this client — steps followed by hand)

`sync_now(include_repos=false)` → 2 requests, 0 new posts (F1 blocks the default form). Digest read,
three threads investigated: 382939/192+193 (MiaAI 1104 regression), 381350/973–976 (TF/Hermes issue,
512k hallucination ceiling), 384438/138 (`spark-ds41f-1107` qualified on eight topologies). One flag
raised (id 2, perf-claim on topic 382939).

Briefing:
- **MiaAI's new GLM-5.3-Flash-DGX-Sparks-1104 recipe looks like a regression on two Sparks.** Post 1882249
  measures 63.9 agg / 33.5 tok/s single-stream prose and 69.4 agg on code for the new tree, against
  80.5 / 41.1 and 112.5 for the older TensorFold recipe — https://forums.developer.nvidia.com/t/382939/192
  (2026-11-05). The reply at https://forums.developer.nvidia.com/t/382939/193 (2026-11-05) explains the
  move to the TensorFold ZIG tree and freezes the Python one. → **flag 2**.
- **DeepSeek V4.1 Flash recipe keeps shipping:** "spark-ds41f-1107 is out, qualified and in production …
  on all eight topologies I support" — https://forums.developer.nvidia.com/t/384438/138 (2026-11-07).
- **Context ceiling datapoint:** users converge on ~512k as the practical limit before hallucination
  climbs — https://forums.developer.nvidia.com/t/381350/976 (2026-11-08).
- **Suspected TensorFold/Hermes bug** under investigation by the stack author —
  https://forums.developer.nvidia.com/t/381350/973 (2026-11-08).

### `serve_model("GLM 5.3 Flash", topology="dual-spark")`

First pass: `model_summary` (F3, F9) → `get_notes` (1 prior note, single-Spark only) → `whats_new_for` →
read 386738/1, 382120/1, 382628/74, 382939/192. Answer, recipe id 2 and note id 2 saved:
recommended **NVIDIA spark-local-inference (flare) + nvidia/GLM-5.3-Flash-FP4-Local, TP=2** — 38.2 tok/s
tg128, 1,493 tok/s pp2048, 3.44 M KV tokens, TEB 91/100, with the vLLM/NVFP4 stack (29–51 tok/s,
~2,000 tok/s prefill) and the EXL3 4 bpw kit (2.2 M KV pool, 20–24 tok/s) as alternatives, and MiaAI
1104 flagged as a regression. Sources:
https://forums.developer.nvidia.com/t/386738/1, /t/382628/74, /t/382120/1 (2026-10-24, 2026-09-19,
2026-09-02).

Second ask: `get_notes` returned note 2 first, so the answer started from it. The "only what's new" step
did not hold: `whats_new_for("GLM 5.3 Flash", since=note.as_of)` returned posts dated 2026-10-17 to
2026-10-26 (topics 386108, 386738, 387013), i.e. **`since` is not applied to the mentions list** — see
F3/F9 family; this is the same date-blindness, reported here rather than as a separate finding.

### Watchlist reality check

- **Qwen4: no release, all rumour.** 91 hits, e.g. "I'm betting that the arrival of Qwen 4 27B will make
  it even more efficient" (https://forums.developer.nvidia.com/t/380257/115, 2026-11-04), "we want you
  for Qwen 4!" (https://forums.developer.nvidia.com/t/382733/289, 2026-10-08), "Qwen 4 releasing next
  week" (https://forums.developer.nvidia.com/t/386125/349, 2026-11-01). The topic literally titled
  "Qwen4" (388002) is a meta-thread about AI-written posts. The only "Qwen4" that looks like a product
  name is vLLM's internal `Qwen3.8-Flash-Next (Qwen4Exp)` codename
  (https://forums.developer.nvidia.com/t/382733/291, 2026-10-09) — not a release.
- **First-sighting alert:** fires on the first *mention*, which for an anticipated model is a rumour by
  construction, and it points at a Qwen3.8-27B thread — so the title reads as "Qwen4 spotted" when it is
  a passing wish. It is honest but noisy; acking it (as done here) clears it.
- **The three tracked models:** hit counts are internally consistent (GLM 5.3 Flash 1,160, DeepSeek V4.1
  Flash 1,023, Qwen3.8 Flash Next 1,204) and matches are genuine (the single GLM hit in the digest is a
  post that really lists a GLM 5.3 Flash stack). The counts and the `7d`/`30d` split are not: see F3/F8.

### `should_i_update`

`watch_repo` on `gilesco/GLM-5.3-Flash-EXL3-2x-GB10-TensorFold-v1.1-testing` (linked from 382939/193 and
381350/922), `set_installed` at `b4c93e0bb043` (2026-11-08T03:56Z), then `repo_status` (6,244 bytes):
4 commits with subjects, `changed_dirs` (scripts 5, docs 3, root 1), no releases, 3 forum mentions since
the install. Enough to decide **update** — e.g. 11d1e66 says "the unoffered-tool-call leak — the fix
(be8bccbec, upstream PR #415) is in v1.0.0..v1.2.1 and MISSING from v1.2.2/1.2.3/1.2.4 (our promoted
engine included)" — *except* that the direction fields (F2) say `ahead`, which would read as "you are
ahead of the branch, nothing to do". Verdict: content sufficient, verdict fields misleading.

### `frontier("dual-spark")`

Ranked, from post-level measurements (no benchmark rows exist — `list_benchmarks` = 0):
1. **Qwen3.8 Flash Next** NVFP4, MiaAI dual-Spark recipe, vLLM 0.30, TP2+EP, MTP=3, fp8 KV — 43–46 t/s
   single-stream, 174 t/s agg at 8–16 concurrent, 158/164 HumanEval, 74.2% aider polyglot
   (https://forums.developer.nvidia.com/t/385731/1, 2026-10-13).
2. **DeepSeek-V4.1-Flash-1017** EXL3 2.9 bpw, TensorFold TP=2 — 56.8 tok/s text, 92.2 tok/s code,
   140 tok/s on 4 concurrent code requests (https://forums.developer.nvidia.com/t/386125/1, 2026-10-18).
3. **GLM 5.3 Flash** FP4 on flare, TP=2 — 38.2 tok/s, TEB 91/100
   (https://forums.developer.nvidia.com/t/386738/1, 2026-10-24).
4. **MiMo-V2.6-Flash** 309B/15B, vLLM TP=2 — 1 M context, 3.19 M-token NVFP4 KV
   (https://forums.developer.nvidia.com/t/384124/1, 2026-09-22).
Saved as note id 3.

---

## Request counts (every sync and network call this session)

| Call | Requests | Notes |
|------|----------|-------|
| `sparkwatch sync --forum 723 --max-pages 1` (C2) | 1 | 0 new posts |
| concurrent pair (C3) | 1 + 0 | loser took no network action |
| `sync_now()` default, twice (F1) | 0 + 0 | returned busy before any fetch |
| `sync_now(include_repos=false)` | 2 | one listing page per category |
| `watch_repo` baseline | 4 | head + meta + release + tags |
| `set_installed(b4c93e0bb043)` | 1 | ref resolution |
| `repo_status` | 1 | compare (cached afterwards in `compare_cache`) |
| `SPARKWATCH_LIVE=1 pytest -m live` | 0 | blocked by `conftest` guard (F5) |
| CLI digests (×5), MCP digest (×2) | 0 | local only |
| direct `curl` spot-checks (not sparkwatch) | 5 | 2× `posts.json`, 1× topic JSON, commits API + its 301 follow |

No backfill was run, no `--days`/`lookback_days` was passed, and every CLI sync was forum 723 with
`--max-pages 1`.
