from __future__ import annotations

import time

from conftest import listing, make_post, make_topic, topic_payload

from sparkwatch.discourse import DiscourseClient
from sparkwatch.sync import sync


def topic_with_stream(topic, posts, stream):
    payload = topic_payload(topic, posts)
    payload["post_stream"]["stream"] = stream
    return payload


def test_deleted_post_is_tombstoned_and_not_requested_again(settings, conn, router, forum):
    topic = make_topic(201, bumped_days_ago=2, posts_count=3, highest_post_number=3)
    posts = [make_post(2011, 201, 1, "op"), make_post(2013, 201, 3, "third")]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_with_stream(topic, posts, [2011, 2012, 2013]), posts)
    forum.install(router)
    client = DiscourseClient(settings)

    sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    row = conn.execute("SELECT * FROM missing_posts WHERE topic_id=201 AND post_id=2012").fetchone()
    assert row is not None
    assert row["reason"] == "absent_from_batch"

    forum.calls.clear()
    bumped = make_topic(201, bumped_days_ago=1, posts_count=3, highest_post_number=3)
    forum.pages[723] = [listing([bumped])]
    forum.add_topic(topic_with_stream(bumped, posts, [2011, 2012, 2013]), posts)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    requested = [post_id for batch in forum.post_requests() for post_id in batch]
    assert 2012 not in requested


def test_backfill_rechecks_tombstones_and_restores_returning_posts(settings, conn, router, forum):
    topic = make_topic(202, bumped_days_ago=2, posts_count=2, highest_post_number=2)
    posts = [make_post(2021, 202, 1, "op")]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_with_stream(topic, posts, [2021, 2022]), posts)
    forum.install(router)
    client = DiscourseClient(settings)

    sync(settings=settings, conn=conn, client=client, forum_ids=[723], lookback_days=30)
    assert conn.execute("SELECT COUNT(*) FROM missing_posts").fetchone()[0] == 1

    forum.calls.clear()
    forum.pages[723] = [listing([topic])]
    sync(settings=settings, conn=conn, client=client, forum_ids=[723], lookback_days=60)
    requested = [post_id for batch in forum.post_requests() for post_id in batch]
    assert 2022 in requested
    assert conn.execute("SELECT COUNT(*) FROM missing_posts").fetchone()[0] == 1

    restored = make_post(2022, 202, 2, "the post came back")
    forum.pages[723] = [listing([topic])]
    forum.add_topic(
        topic_with_stream(topic, [posts[0], restored], [2021, 2022]), [posts[0], restored]
    )
    sync(settings=settings, conn=conn, client=client, forum_ids=[723], lookback_days=90)

    assert conn.execute("SELECT COUNT(*) FROM missing_posts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE id=2022").fetchone()[0] == 1


def test_404_topic_is_skipped_and_sync_continues(settings, conn, router, forum):
    bad = make_topic(203, bumped_days_ago=3)
    good = make_topic(204, bumped_days_ago=2)
    forum.add_page(723, listing([bad, good]))
    forum.add_topic(
        topic_payload(good, [make_post(2041, 204, 1, "fine")]), [make_post(2041, 204, 1, "fine")]
    )
    forum.topic_status[203] = [404]
    forum.install(router)

    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])
    assert result.errors and "203" in result.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE id=2041").fetchone()[0] == 1


def test_403_topic_is_skipped(settings, conn, router, forum):
    bad = make_topic(205, bumped_days_ago=2)
    forum.add_page(723, listing([bad]))
    forum.topic_status[205] = [403]
    forum.install(router)

    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])
    assert result.errors and "403" in result.errors[0]


def test_429_then_success(settings, conn, router, forum):
    topic = make_topic(206, bumped_days_ago=2)
    posts = [make_post(2061, 206, 1, "rate limited once")]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.topic_status[206] = [429, 200]
    forum.install(router)
    client = DiscourseClient(settings)

    started = time.monotonic()
    result = sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    assert time.monotonic() - started >= 0.9
    assert result.errors == []
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE id=2061").fetchone()[0] == 1


def test_persistent_5xx_fails_only_that_topic(settings, conn, router, forum):
    settings.forum.max_retries = 1
    bad = make_topic(207, bumped_days_ago=3)
    good = make_topic(208, bumped_days_ago=2)
    forum.add_page(723, listing([bad, good]))
    forum.add_topic(
        topic_payload(good, [make_post(2081, 208, 1, "ok")]), [make_post(2081, 208, 1, "ok")]
    )
    forum.topic_status[207] = [503]
    forum.install(router)

    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])
    assert result.errors and "207" in result.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE id=2081").fetchone()[0] == 1


def test_midthread_edit_inside_window_is_caught(settings, conn, router, forum):
    topic = make_topic(209, bumped_days_ago=2, posts_count=3, highest_post_number=3)
    posts = [
        make_post(2091, 209, 1, "op"),
        make_post(2092, 209, 2, "middle post about exl3", days_ago=2),
        make_post(2093, 209, 3, "last post"),
    ]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    edited = make_post(2092, 209, 2, "middle post about fp8 instead", days_ago=2, version=2)
    bumped = make_topic(209, bumped_days_ago=1, posts_count=3, highest_post_number=3)
    forum.pages[723] = [listing([bumped])]
    forum.add_topic(
        topic_payload(bumped, [posts[0], edited, posts[2]]), [posts[0], edited, posts[2]]
    )
    result = sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    assert result.posts_changed == 1
    assert "fp8 instead" in conn.execute("SELECT raw FROM posts WHERE id=2092").fetchone()[0]


def test_edit_older_than_refetch_window_is_not_caught(settings, conn, router, forum):
    topic = make_topic(
        210, bumped_days_ago=40, created_days_ago=40, posts_count=2, highest_post_number=2
    )
    posts = [
        make_post(2101, 210, 1, "op", days_ago=40),
        make_post(2102, 210, 2, "old post about nvfp4", days_ago=40),
    ]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    edited = make_post(2102, 210, 2, "old post edited much later", days_ago=40, version=2)
    bumped = make_topic(
        210, bumped_days_ago=1, created_days_ago=40, posts_count=2, highest_post_number=2
    )
    forum.pages[723] = [listing([bumped])]
    forum.add_topic(topic_payload(bumped, [posts[0], edited]), [posts[0], edited])
    result = sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    assert result.posts_changed == 0
    assert "nvfp4" in conn.execute("SELECT raw FROM posts WHERE id=2102").fetchone()[0]


def test_deleted_and_hidden_posts_are_tombstoned(settings, conn, router, forum):
    topic = make_topic(211, bumped_days_ago=2, posts_count=3, highest_post_number=3)
    posts = [
        make_post(2111, 211, 1, "op"),
        make_post(2112, 211, 2, "gone", extra={"deleted_at": "2026-01-01T00:00:00+00:00"}),
        make_post(2113, 211, 3, "hidden", extra={"hidden": True}),
    ]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)
    sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])

    reasons = {
        row["post_id"]: row["reason"]
        for row in conn.execute("SELECT post_id, reason FROM missing_posts WHERE topic_id=211")
    }
    assert reasons == {2112: "deleted", 2113: "hidden"}
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE topic_id=211").fetchone()[0] == 1
