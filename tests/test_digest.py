from __future__ import annotations

from sparkwatch import digest as digest_module
from sparkwatch.digest import build_digest

PAST = "2026-01-01T00:00:00+00:00"
OLDER = "2025-12-01T00:00:00+00:00"


def seed_topic(
    conn, topic_id: int, title: str, created_at: str = PAST, relevance: float = 5.0
) -> None:
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, posts_count, like_count,"
        " relevance_score, first_fetched_at) VALUES (?, 723, ?, ?, ?, 1, 1, ?, ?)",
        (topic_id, title, created_at, created_at, relevance, created_at),
    )


def seed_post(
    conn,
    post_id: int,
    topic_id: int,
    raw: str,
    created_at: str = PAST,
    first_fetched_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw, cooked,"
        " content_hash, first_fetched_at, changed_at)"
        " VALUES (?, ?, ?, 1, 'alice', ?, ?, '<p>x</p>', ?, ?, ?)",
        (
            post_id,
            topic_id,
            post_id % 1000,
            created_at,
            raw,
            f"h{post_id}",
            first_fetched_at or created_at,
            first_fetched_at or created_at,
        ),
    )


def test_sections_are_all_present_and_ordered(conn, settings):
    seed_topic(conn, 1, "GLM 5.3 Flash on two sparks")
    seed_post(conn, 11, 1, "glm 5.3 flash runs at 30 tok/s")
    from sparkwatch import watch

    watch.match_post(conn, 11, 1, "GLM 5.3 Flash on two sparks", "glm 5.3 flash runs at 30 tok/s")
    digest = build_digest(conn, settings, since=OLDER, advance=False)
    assert list(digest["sections"]) == [
        "watch_sightings",
        "watchlist_activity",
        "new_topics",
        "updated_topics",
        "watched_repos",
        "repos_first_linked",
        "open_flags",
    ]
    assert digest["sections"]["new_topics"][0]["topic_id"] == 1
    assert digest["sections"]["watchlist_activity"][0]["label"] == "GLM 5.3 Flash"


def test_cursor_advances_and_repeat_digest_is_empty(conn, settings):
    seed_topic(conn, 2, "DeepSeek V4.1 Flash notes")
    seed_post(conn, 21, 2, "deepseek v4.1 flash numbers")

    first = build_digest(conn, settings, advance=True)
    assert first["sections"]["new_topics"]

    second = build_digest(conn, settings, advance=True)
    assert second["sections"]["new_topics"] == []
    assert second["sections"]["updated_topics"] == []
    assert second["sections"]["watchlist_activity"] == []


def test_backfilled_posts_are_a_count_not_news(conn, settings):
    seed_topic(conn, 3, "old thread", created_at=OLDER)
    seed_post(conn, 31, 3, "an old post", created_at=OLDER, first_fetched_at=PAST)

    digest = build_digest(conn, settings, since=PAST, advance=False)
    assert digest["backfilled"] == {"posts": 1, "topics": 1}
    assert all(topic["topic_id"] != 3 for topic in digest["sections"]["new_topics"])
    assert all(topic["topic_id"] != 3 for topic in digest["sections"]["updated_topics"])


def test_updated_topics_counts_only_new_posts(conn, settings):
    seed_topic(conn, 10, "GLM 5.3 Flash megathread", created_at=OLDER, relevance=9.0)
    for index in range(40):
        seed_post(
            conn,
            1000 + index,
            10,
            f"history post {index} about glm 5.3 flash",
            created_at=OLDER,
            first_fetched_at=PAST,
        )
    seed_post(
        conn,
        2041,
        10,
        "a genuinely new reply about glm 5.3 flash",
        created_at=PAST,
        first_fetched_at=PAST,
    )

    digest = build_digest(conn, settings, since=PAST, advance=False)
    updated = {entry["topic_id"]: entry for entry in digest["sections"]["updated_topics"]}
    assert updated[10]["new_posts"] == 1
    assert updated[10]["edits"] == 0
    assert [post["post_id"] for post in updated[10]["latest"]] == [2041]
    assert digest["backfilled"] == {"posts": 40, "topics": 1}


def test_posts_written_shortly_before_since_are_new(conn, settings):
    seed_topic(conn, 11, "Qwen thread", created_at=OLDER)
    seed_post(
        conn,
        1101,
        11,
        "written ten hours before the cursor but fetched after it",
        created_at="2025-12-31T14:00:00+00:00",
        first_fetched_at=PAST,
    )

    digest = build_digest(conn, settings, since=PAST, advance=False)
    assert digest["backfilled"] == {"posts": 0, "topics": 0}
    updated = {entry["topic_id"]: entry for entry in digest["sections"]["updated_topics"]}
    assert updated[11]["new_posts"] == 1


def test_edited_posts_are_counted_separately_from_new_ones(conn, settings):
    seed_topic(conn, 12, "DeepSeek thread", created_at=OLDER)
    seed_post(conn, 1201, 12, "edited later", created_at=OLDER, first_fetched_at=OLDER)
    conn.execute("UPDATE posts SET changed_at=? WHERE id=1201", (PAST,))

    digest = build_digest(conn, settings, since=PAST, advance=False)
    updated = {entry["topic_id"]: entry for entry in digest["sections"]["updated_topics"]}
    assert updated[12]["new_posts"] == 0
    assert updated[12]["edits"] == 1
    assert updated[12]["latest"] == []
    assert digest["sections"]["new_topics"] == []
    assert digest["backfilled"] == {"posts": 0, "topics": 0}


def test_watch_hit_dates_come_from_the_post(conn, settings):
    from sparkwatch import watch
    from sparkwatch.util import utcnow_iso

    seed_topic(conn, 13, "GLM 5.3 Flash thread", created_at=PAST)
    seed_post(conn, 1301, 13, "glm 5.3 flash at 30 tok/s", created_at=PAST)
    watch.match_post(conn, 1301, 13, "GLM 5.3 Flash thread", "glm 5.3 flash at 30 tok/s")
    conn.execute("UPDATE watch_hits SET created_at=?", (utcnow_iso(),))

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    hit = digest["sections"]["watchlist_activity"][0]["hits"][0]
    assert hit["date"] == PAST


def test_watch_hit_snippet_shows_the_match(conn, settings):
    from sparkwatch import watch

    seed_topic(conn, 14, "Thread with a long OP", created_at=PAST)
    filler = "we spent the morning on a Qwen SGLang setup. " * 8
    raw = f"{filler}then GLM 5.3 Flash hit 31 tok/s with nvfp4. {filler}"
    seed_post(conn, 1401, 14, raw, created_at=PAST)
    watch.match_post(conn, 1401, 14, "Thread with a long OP", raw)

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    snippet = digest["sections"]["watchlist_activity"][0]["hits"][0]["snippet"]
    assert "GLM 5.3 Flash" in snippet
    assert len(snippet) <= digest_module.SNIPPET_CHARS + 2


def test_default_digest_stays_under_the_size_budget(conn, settings):
    import json

    for index in range(12):
        topic_id = 2000 + index
        seed_topic(conn, topic_id, f"GLM 5.3 Flash topic {index}", created_at=OLDER, relevance=9.0)
        seed_post(
            conn,
            20000 + index,
            topic_id,
            f"glm 5.3 flash on two sparks with vllm nvfp4, run {index} " + "detail " * 40,
            created_at=PAST,
            first_fetched_at=PAST,
        )
        conn.execute(
            "INSERT INTO repos (owner, name, url, watch, first_seen_at) VALUES (?, ?, ?, 0, ?)",
            (f"owner{index}", f"repo{index}", f"https://github.com/owner{index}/repo{index}", PAST),
        )
        conn.execute(
            "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, mentioned_at)"
            " SELECT id, ?, ?, url, ? FROM repos WHERE owner=?",
            (20000 + index, topic_id, PAST, f"owner{index}"),
        )
        conn.execute(
            "INSERT INTO flags (target_type, target_id, label, reason, priority, created_at)"
            " VALUES ('topic', ?, 'perf-claim', ?, 2, ?)",
            (str(topic_id), "reason " * 30, PAST),
        )
    from sparkwatch import watch

    watch.match_post(
        conn, 20000, 2000, "GLM 5.3 Flash topic 0", "glm 5.3 flash on two sparks with vllm nvfp4"
    )

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    size = len(json.dumps(digest))
    assert size < 10_000, size
    assert len(digest["sections"]["new_topics"]) <= digest_module.SECTION_CAP
    assert len(digest["sections"]["repos_first_linked"]) <= digest_module.SECTION_CAP
    assert "snippet" not in digest["sections"]["repos_first_linked"][0]


def test_render_digest_is_readable_lines(conn, settings):
    from sparkwatch.digest import render_digest

    seed_topic(conn, 15, "GLM 5.3 Flash on two Sparks", created_at="2025-11-01T00:00:00+00:00")
    seed_post(conn, 1501, 15, "glm 5.3 flash numbers", created_at=PAST, first_fetched_at=PAST)
    conn.execute(
        "INSERT INTO flags (target_type, target_id, label, reason, priority, created_at)"
        " VALUES ('topic', '15', 'perf-claim', 'two numbers disagree', 1, ?)",
        (PAST,),
    )

    text = render_digest(build_digest(conn, settings, since=OLDER, advance=False))

    assert "{'" not in text
    assert "## Updated topics (1)" in text
    assert "https://forums.developer.nvidia.com/t/15" in text
    assert "[P1] perf-claim on topic 15" in text


def test_anticipated_sighting_tops_digest_until_acked(conn, settings):
    from sparkwatch import watch

    seed_topic(conn, 4, "Qwen4 spotted")
    seed_post(conn, 41, 4, "Qwen4-72B weights are out", created_at=PAST, first_fetched_at=PAST)
    watch.match_post(conn, 41, 4, "Qwen4 spotted", "Qwen4-72B weights are out")

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    assert digest["sections"]["watch_sightings"][0]["label"] == "Qwen4"
    assert digest["sections"]["watch_sightings"][0]["url"].endswith("/t/4/41")

    conn.execute("UPDATE watchlist SET sighting_acked_at=? WHERE label='Qwen4'", (PAST,))
    digest = build_digest(conn, settings, since=OLDER, advance=False)
    assert digest["sections"]["watch_sightings"] == []


def test_anticipated_hits_show_as_a_count_plus_three_newest(conn, settings):
    from sparkwatch import watch

    seed_topic(conn, 30, "Qwen4 chatter", created_at="2025-11-01T00:00:00+00:00")
    for index in range(5):
        raw = f"Qwen 4 rumour number {index}"
        seed_post(conn, 3000 + index, 30, raw, created_at=PAST, first_fetched_at=PAST)
        watch.match_post(conn, 3000 + index, 30, "Qwen4 chatter", raw)

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    entry = next(row for row in digest["sections"]["watchlist_activity"] if row["label"] == "Qwen4")
    assert entry["kind"] == "anticipated"
    assert entry["total"] == 5
    assert len(entry["hits"]) == 3


def test_title_only_hits_are_thread_activity_not_mentions(conn, settings):
    from sparkwatch import watch

    title = "Qwen4 is coming"
    seed_topic(conn, 40, title, created_at=PAST)
    filler = "we spent the morning on a Qwen SGLang setup. " * 4
    bodies = {
        4011: f"{filler}Qwen4-72B weights are out on the hub",
        4012: f"{filler}still waiting for news",
        4013: f"{filler}no numbers yet",
    }
    for post_id, raw in bodies.items():
        seed_post(conn, post_id, 40, raw)
        watch.match_post(conn, post_id, 40, title, raw)

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    entry = next(row for row in digest["sections"]["watchlist_activity"] if row["label"] == "Qwen4")

    # One body mention, two replies that only match the topic title.
    assert entry["total"] == 1
    assert len(entry["hits"]) == 1
    assert "Qwen4-72B" in entry["hits"][0]["snippet"]

    thread = entry["threads"][0]
    assert thread["topic_id"] == 40
    assert thread["new_replies"] == 2
    assert len(thread["latest"]) == 2
    # Ordinary snippets: the start of the reply, not text around a match.
    assert thread["latest"][0]["snippet"].startswith("we spent the morning")
    assert thread["latest"][0]["url"].startswith("https://forums.developer.nvidia.com/t/40/")


def test_thread_activity_is_capped_and_digest_stays_small(conn, settings):
    from sparkwatch import watch

    for index in range(3):
        topic_id = 50 + index
        title = f"GLM 5.3 Flash thread {index}"
        seed_topic(conn, topic_id, title, created_at=PAST)
        for reply in range(3):
            post_id = 5000 + index * 10 + reply
            raw = f"reply number {reply} in thread {index}"
            seed_post(conn, post_id, topic_id, raw)
            watch.match_post(conn, post_id, topic_id, title, raw)

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    entry = next(
        row for row in digest["sections"]["watchlist_activity"] if row["label"] == "GLM 5.3 Flash"
    )
    assert entry["total"] == 0
    assert entry["hits"] == []
    # Every reply is counted; only the busiest threads are listed, and the rest are flagged.
    assert entry["replies"] == 9
    assert entry["threads_total"] == 3
    assert len(entry["threads"]) == digest_module.THREADS_PER_LABEL
    assert all(
        len(thread["latest"]) <= digest_module.REPLIES_PER_THREAD for thread in entry["threads"]
    )
    assert digest_module.THREADS_PER_LABEL == 2

    rendered = digest_module.render_digest(digest)
    assert "0 new mention(s), 9 new replies in 3 threads (+1 more)" in rendered


def test_cli_renders_thread_activity(conn, settings):
    from sparkwatch import watch

    title = "GLM 5.3 Flash thread"
    seed_topic(conn, 60, title, created_at=PAST)
    seed_post(conn, 6011, 60, "plain reply with no model name")
    watch.match_post(conn, 6011, 60, title, "plain reply with no model name")

    digest = build_digest(conn, settings, since=OLDER, advance=False)
    rendered = digest_module.render_digest(digest)
    assert "thread GLM 5.3 Flash thread" in rendered
    assert "1 new replies" in rendered
    assert "0 new mention(s)" in rendered


def test_watched_repo_without_mentions_still_appears(conn, settings):
    conn.execute(
        "INSERT INTO repos (id, owner, name, url, watch, first_seen_at, head_sha, last_checked_at)"
        " VALUES (1, 'vllm-project', 'vllm', 'https://github.com/vllm-project/vllm', 1, ?, 'abc', ?)",
        (OLDER, PAST),
    )
    conn.execute(
        "INSERT INTO repo_events (repo_id, kind, ref, prev_ref, commits_ahead, detected_at)"
        " VALUES (1, 'head_moved', 'def', 'abc', 3, ?)",
        (PAST,),
    )
    digest = build_digest(conn, settings, since=OLDER, advance=False)
    watched = digest["sections"]["watched_repos"]
    assert watched and watched[0]["repo"] == "vllm-project/vllm"
    assert watched[0]["events"][0]["kind"] == "head_moved"


def test_repos_first_linked_and_open_flags(conn, settings):
    conn.execute(
        "INSERT INTO repos (id, owner, name, url, watch, first_seen_at)"
        " VALUES (2, 'a', 'b', 'https://github.com/a/b', 0, ?)",
        (PAST,),
    )
    conn.execute(
        "INSERT INTO flags (target_type, target_id, label, reason, priority, created_at)"
        " VALUES ('repo', 'a/b', 'breaking-change', 'nccl breaks', 1, ?)",
        (PAST,),
    )
    digest = build_digest(conn, settings, since=OLDER, advance=False)
    assert digest["sections"]["repos_first_linked"][0]["repo"] == "a/b"
    assert digest["sections"]["open_flags"][0]["label"] == "breaking-change"
