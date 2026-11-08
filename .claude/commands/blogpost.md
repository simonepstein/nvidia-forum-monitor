---
description: Draft a blog post about the DGX Spark forums for a date range, using the sparkwatch MCP tools
argument-hint: <since YYYY-MM-DD> [until YYYY-MM-DD] [words=900] [refresh]
---

Write a blog post about the NVIDIA DGX Spark / GB10 forums, for people running local LLMs on one or two Sparks.

Arguments: `$ARGUMENTS`
- The first date is `since`. The optional second date is `until`; if it's absent, use now.
- `words=N` sets the target length (default 900). Stay within ±10%.
- `refresh` means the user is asking you to refresh likes before ranking. Without it, don't refresh.
- If `since` is missing or isn't an ISO date, ask for it and stop.

## Steps

1. **Optional likes refresh.** Do this only if `refresh` was given. Call `refresh_engagement(days=N)`, where N is the number of days from `since` to today (capped at 60). If it returns `partial: true`, call it once more. Report the request count in one line.
   - If `refresh` wasn't given and the range is longer than 14 days, add one line at the end of the post's notes saying likes may be stale beyond the last refresh.
2. **Rank.** Call `global_digest(since=<since>, until=<until>, basis="written", detail=True, advance=False)`.
   - Page once with `next`, so you have about 20 topics.
   - `basis="written"` ranks by when posts were written. Never use `sql_query` to rank instead.
3. **Read before choosing.**
   - Read the **top 8 by score** with `get_posts`: the OP, plus the newest posts inside the window.
   - Don't drop any of the top 8 on its title alone. Topics below the top 8 may be dropped on their titles, but say so.
4. **Choose 4–6 stories that matter to the audience.**
   - Good stories: serving recipes with measured tok/s, model and quant releases, notable repos, and firmware, driver or hardware problems with fixes.
   - Skip chatter, opinion threads and individual support or RMA cases, even when they rank highly.
   - Setups that need **more than two Sparks** go under "Also noted", never as a main story.
5. **Check before writing.** Every number, version and claim must appear in the post you link. If you can't find it in the post text, leave it out.
6. **Write** `words` words (default 900):
   - a title, plus a two-line intro;
   - one section per story;
   - "Also noted": 3–6 bullets;
   - "Open questions": from `unanswered`, plus anything the threads left unresolved.
7. **Style rules:**
   - Link every claim to its post as `https://forums.developer.nvidia.com/t/<topic>/<post_number>`, and attribute by username.
   - Quote at most one sentence at a time. Summarise and link; don't republish post text.
   - Say when a number is one user's report rather than reproduced, and don't compare numbers from different workloads or recipes as if they were like for like.
8. **Save** the draft to `reports/blog-<since>_<until>.md`. The `reports/` folder is gitignored. Print the post in full.
9. **End with "Topics considered and dropped"**, kept separate from the post: a short table of topic ID, decision and why, split into "read" and "dropped on title". This is for the user's editorial check, not for publishing.

Don't advance any digest cursor, and don't save notes or flags unless the user asks.
