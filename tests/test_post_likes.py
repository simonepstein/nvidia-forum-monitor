from __future__ import annotations

from conftest import make_post, make_topic, topic_payload

from sparkwatch.digest import build_digest
from sparkwatch.discourse import DiscourseClient
from sparkwatch.sync import _upsert_post, sync
from sparkwatch.util import utcnow_iso

PAST = "2026-01-01T00:00:00+00:00"


def _store_post(conn, post_id: int, extra: dict) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO topics (id, forum_id, title, created_at) VALUES (901, 723, 't', ?)",
        (PAST,),
    )
    post = {
        "id": post_id,
        "post_number": post_id % 100,
        "post_type": 1,
        "username": "alice",
        "created_at": PAST,
        "updated_at": PAST,
        "raw": "dual spark vllm",
        "cooked": "<p>dual spark vllm</p>",
        **extra,
    }
    _upsert_post(conn, 901, post, PAST)
    return conn.execute("SELECT like_count FROM posts WHERE id=?", (post_id,)).fetchone()[0]


def test_post_likes_come_from_reaction_users_count(settings, conn):
    likes = _store_post(
        conn,
        9001,
        {
            "reaction_users_count": 4,
            "actions_summary": [{"id": 2, "count": 4}],
            "reactions": [{"id": "heart", "type": "emoji", "count": 4}],
        },
    )
    assert likes == 4


def test_post_likes_fall_back_to_actions_summary(settings, conn):
    likes = _store_post(
        conn,
        9002,
        {
            "actions_summary": [{"id": 1, "count": 0}, {"id": 2, "count": 5}],
            "reactions": [{"id": "heart", "type": "emoji", "count": 5}],
        },
    )
    assert likes == 5


def test_post_likes_fall_back_to_the_reaction_sum(settings, conn):
    likes = _store_post(
        conn,
        9003,
        {"reactions": [{"id": "heart", "count": 2}, {"id": "hugs", "count": 3}]},
    )
    assert likes == 5


def test_post_likes_read_the_legacy_dict_reactions(settings, conn):
    likes = _store_post(conn, 9004, {"reactions": {"like_count": 4}})
    assert likes == 4


def test_post_without_likes_stores_zero(settings, conn):
    likes = _store_post(conn, 9005, {"reactions": []})
    assert likes == 0


def test_post_likes_update_on_unchanged_refetch_without_an_edit(settings, conn, router, forum):
    topic = make_topic(109, bumped_days_ago=2, posts_count=2, highest_post_number=2)
    posts = [
        make_post(
            1091,
            109,
            1,
            "dual spark vllm fp8 tok/s 30",
            days_ago=2,
            extra={
                "reaction_users_count": 1,
                "reactions": [{"id": "heart", "type": "emoji", "count": 1}],
            },
        ),
        make_post(1092, 109, 2, "same here", days_ago=2),
    ]
    forum.add_page(723, {"topic_list": {"topics": [topic]}})
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    before = conn.execute(
        "SELECT content_hash, changed_at, like_count FROM posts WHERE id=1091"
    ).fetchone()
    assert before["like_count"] == 1

    bumped = make_topic(109, bumped_days_ago=1, posts_count=2, highest_post_number=2)
    liked = dict(
        posts[0],
        reaction_users_count=7,
        reactions=[{"id": "heart", "type": "emoji", "count": 7}],
        accepted_answer=True,
    )
    forum.add_topic(topic_payload(bumped, [liked, posts[1]]), [liked, posts[1]])
    forum.pages[723][0] = {"topic_list": {"topics": [bumped]}}

    since = utcnow_iso()
    second = sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    after = conn.execute(
        "SELECT content_hash, changed_at, like_count, accepted_answer, fetched_at"
        " FROM posts WHERE id=1091"
    ).fetchone()
    assert second.posts_changed == 0
    assert after["content_hash"] == before["content_hash"]
    assert after["changed_at"] == before["changed_at"]
    assert after["like_count"] == 7
    assert after["accepted_answer"] == 1

    digest = build_digest(conn, settings, since=since, advance=False)
    assert all(item["edits"] == 0 for item in digest["sections"]["updated_topics"])


def test_a_trimmed_refetch_payload_keeps_stored_likes(settings, conn):
    liked = {
        "reaction_users_count": 4,
        "reactions": [{"id": "heart", "type": "emoji", "count": 4}],
        "accepted_answer": True,
    }
    assert _store_post(conn, 9101, liked) == 4

    trimmed = {
        key: value
        for key, value in {
            "id": 9101,
            "post_number": 1,
            "post_type": 1,
            "username": "alice",
            "created_at": PAST,
            "updated_at": PAST,
            "raw": "dual spark vllm",
            "cooked": "<p>dual spark vllm</p>",
        }.items()
    }
    _upsert_post(conn, 901, trimmed, PAST)

    row = conn.execute("SELECT like_count, accepted_answer FROM posts WHERE id=9101").fetchone()
    assert row["like_count"] == 4
    assert row["accepted_answer"] == 1
