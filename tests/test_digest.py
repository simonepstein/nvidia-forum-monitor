from __future__ import annotations

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
    assert digest["backfilled_posts"] == 1
    assert all(topic["topic_id"] != 3 for topic in digest["sections"]["new_topics"])


def test_anticipated_sighting_tops_digest_until_acked(conn, settings):
    seed_topic(conn, 4, "Qwen4 spotted")
    seed_post(conn, 41, 4, "Qwen4-72B is out")
    conn.execute(
        "UPDATE watchlist SET first_seen_post_id=41, first_seen_at=? WHERE label='Qwen4'", (PAST,)
    )
    digest = build_digest(conn, settings, since=OLDER, advance=False)
    assert digest["sections"]["watch_sightings"][0]["label"] == "Qwen4"
    assert digest["sections"]["watch_sightings"][0]["url"].endswith("/t/4/41")

    conn.execute("UPDATE watchlist SET sighting_acked_at=? WHERE label='Qwen4'", (PAST,))
    digest = build_digest(conn, settings, since=OLDER, advance=False)
    assert digest["sections"]["watch_sightings"] == []


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
