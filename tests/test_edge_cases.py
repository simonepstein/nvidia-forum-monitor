from __future__ import annotations

import time

from conftest import (
    fixture,
    iso_days_ago,
    listing,
    make_post,
    make_topic,
    synthetic,
    topic_payload,
    topic_stub,
)

from sparkwatch.discourse import DiscourseClient
from sparkwatch.sync import sync


def topic_with_stream(topic, posts, stream):
    payload = topic_payload(topic, posts)
    payload["post_stream"]["stream"] = stream
    return payload


def test_deleted_post_is_tombstoned_and_not_requested_again(settings, conn, router, forum):
    payload = synthetic("topic_deleted_post.json")
    topic_id = int(payload["id"])
    deleted_id = int(payload["synthetic_deleted_post_id"])
    batch = synthetic("posts_deleted_post.json")["post_stream"]["posts"]
    stub = topic_stub(721, topic_id)
    stub["bumped_at"] = iso_days_ago(2)
    forum.add_page(721, listing([stub]))
    forum.add_topic(payload, batch)
    forum.install(router)
    client = DiscourseClient(settings)

    sync(settings=settings, conn=conn, client=client, forum_ids=[721])
    row = conn.execute(
        "SELECT * FROM missing_posts WHERE topic_id=? AND post_id=?", (topic_id, deleted_id)
    ).fetchone()
    assert row is not None
    assert row["reason"] == "absent_from_batch"

    forum.calls.clear()
    bumped = dict(stub)
    bumped["bumped_at"] = iso_days_ago(1)
    forum.pages[721] = [listing([bumped])]
    sync(settings=settings, conn=conn, client=client, forum_ids=[721])

    requested = [post_id for batch_ids in forum.post_requests() for post_id in batch_ids]
    assert deleted_id not in requested


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
    forum.add_topic_responses(203, synthetic("errors_404.json")["responses"])
    forum.install(router)

    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])
    assert result.errors and "203" in result.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE id=2041").fetchone()[0] == 1


def test_403_topic_is_skipped(settings, conn, router, forum):
    bad = make_topic(205, bumped_days_ago=2)
    forum.add_page(723, listing([bad]))
    forum.add_topic_responses(205, synthetic("errors_403.json")["responses"])
    forum.install(router)

    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])
    assert result.errors and "403" in result.errors[0]


def test_429_then_success(settings, conn, router, forum):
    topic = make_topic(206, bumped_days_ago=2)
    posts = [make_post(2061, 206, 1, "rate limited once")]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.add_topic_responses(206, synthetic("errors_429.json")["responses"])
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
    forum.add_topic_responses(207, synthetic("errors_500.json")["responses"])
    forum.install(router)

    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])
    assert result.errors and "207" in result.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE id=2081").fetchone()[0] == 1


def test_midthread_edit_inside_window_is_caught(settings, conn, router, forum):
    topic_id = 388371
    original = fixture("posts_388371_0.json")["post_stream"]["posts"]
    edited = synthetic("posts_midthread_edit.json")["post_stream"]["posts"]
    stub = topic_stub(721, topic_id)
    stub["bumped_at"] = iso_days_ago(2)
    forum.add_page(721, listing([stub]))
    forum.add_topic(topic_payload(stub, original), original)
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[721])

    edited_id = int(edited[1]["id"])
    before = conn.execute("SELECT raw FROM posts WHERE id=?", (edited_id,)).fetchone()["raw"]
    assert "fp8 on the second Spark" not in before

    bumped = dict(stub)
    bumped["bumped_at"] = iso_days_ago(1)
    forum.pages[721] = [listing([bumped])]
    forum.add_topic(topic_payload(bumped, edited), edited)
    result = sync(settings=settings, conn=conn, client=client, forum_ids=[721])

    after = conn.execute("SELECT raw FROM posts WHERE id=?", (edited_id,)).fetchone()["raw"]
    assert result.posts_changed == 1
    assert "fp8 on the second Spark" in after


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


def test_tombstone_is_rechecked_after_thirty_days(settings, conn, router, forum):
    topic = make_topic(212, bumped_days_ago=2, posts_count=2, highest_post_number=2)
    posts = [make_post(2121, 212, 1, "op")]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_with_stream(topic, posts, [2121, 2122]), posts)
    forum.install(router)
    client = DiscourseClient(settings)

    sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    assert conn.execute("SELECT COUNT(*) FROM missing_posts WHERE post_id=2122").fetchone()[0] == 1

    conn.execute(
        "UPDATE missing_posts SET last_checked_at=? WHERE topic_id=212 AND post_id=2122",
        (iso_days_ago(40),),
    )
    forum.calls.clear()
    bumped = make_topic(212, bumped_days_ago=1, posts_count=2, highest_post_number=2)
    forum.pages[723] = [listing([bumped])]
    forum.add_topic(topic_with_stream(bumped, posts, [2121, 2122]), posts)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    requested = [post_id for batch in forum.post_requests() for post_id in batch]
    assert 2122 in requested
    refreshed = conn.execute(
        "SELECT last_checked_at FROM missing_posts WHERE topic_id=212 AND post_id=2122"
    ).fetchone()
    assert refreshed is not None
    assert refreshed["last_checked_at"] > iso_days_ago(1)


def test_non_content_posts_are_tombstoned_once_and_never_requested_again(
    settings, conn, router, forum
):
    topic = make_topic(213, bumped_days_ago=2, posts_count=3, highest_post_number=3)
    posts = [
        make_post(2131, 213, 1, "op"),
        make_post(2132, 213, 2, "spam scanner notice", post_type=3),
        make_post(2133, 213, 3, "moderator whisper", post_type=4),
    ]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)
    client = DiscourseClient(settings)

    result = sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    reasons = {
        row["post_id"]: row["reason"]
        for row in conn.execute("SELECT post_id, reason FROM missing_posts WHERE topic_id=213")
    }
    assert reasons == {2132: "not_content", 2133: "not_content"}
    assert result.tombstones_new == 0

    forum.calls.clear()
    forum.pages[723] = [listing([topic])]
    sync(settings=settings, conn=conn, client=client, forum_ids=[723], lookback_days=90)

    requested = [post_id for batch in forum.post_requests() for post_id in batch]
    assert 2132 not in requested
    assert 2133 not in requested


def test_missing_posts_reason_is_constrained(settings, conn):
    import sqlite3

    import pytest

    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute("INSERT INTO topics (id, forum_id, title) VALUES (214, 723, 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
            " VALUES (214, 2141, 'bogus', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')"
        )
    conn.execute(
        "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
        " VALUES (214, 2142, 'not_content', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')"
    )


def test_rechecked_non_content_post_is_reclassified_and_then_left_alone(
    settings, conn, router, forum
):
    topic = make_topic(215, bumped_days_ago=2, posts_count=2, highest_post_number=2)
    posts = [make_post(2151, 215, 1, "op"), make_post(2152, 215, 2, "notice", post_type=3)]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_with_stream(topic, [posts[0]], [2151, 2152]), posts)
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    conn.execute(
        "UPDATE missing_posts SET reason='hidden', last_checked_at=? WHERE post_id=2152",
        (iso_days_ago(40),),
    )
    forum.calls.clear()
    bumped = make_topic(215, bumped_days_ago=1, posts_count=2, highest_post_number=2)
    forum.pages[723] = [listing([bumped])]
    forum.add_topic(topic_with_stream(bumped, [posts[0]], [2151, 2152]), posts)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    assert 2152 in [post_id for batch in forum.post_requests() for post_id in batch]
    reason = conn.execute("SELECT reason FROM missing_posts WHERE post_id=2152").fetchone()[0]
    assert reason == "not_content"

    forum.calls.clear()
    bumped_again = make_topic(215, bumped_days_ago=0.5, posts_count=2, highest_post_number=2)
    forum.pages[723] = [listing([bumped_again])]
    forum.add_topic(topic_with_stream(bumped_again, [posts[0]], [2151, 2152]), posts)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723], lookback_days=90)
    assert 2152 not in [post_id for batch in forum.post_requests() for post_id in batch]


def test_due_tombstones_requeue_their_topic(settings, conn, router, forum):
    topic = make_topic(216, bumped_days_ago=2, posts_count=2, highest_post_number=2)
    posts = [make_post(2161, 216, 1, "op"), make_post(2162, 216, 2, "gone", post_type=3)]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_with_stream(topic, [posts[0]], [2161, 2162]), posts)
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    # A legacy tombstone marked due by migration 4 is re-checked on the next sync,
    # even though the topic was not bumped.
    conn.execute(
        "UPDATE missing_posts SET reason='hidden', last_checked_at='1970-01-01T00:00:00+00:00'"
        " WHERE post_id=2162"
    )
    forum.calls.clear()
    result = sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    assert 216 in result.updated_topic_ids
    assert 2162 in [post_id for batch in forum.post_requests() for post_id in batch]
    reason = conn.execute("SELECT reason FROM missing_posts WHERE post_id=2162").fetchone()[0]
    assert reason == "not_content"

    # Once reclassified, the topic is no longer re-queued for that reason.
    forum.calls.clear()
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    assert ("topic", 216, None) not in forum.calls


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
