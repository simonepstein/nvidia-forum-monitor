from __future__ import annotations

import hashlib

from conftest import listing, make_post, make_topic, topic_payload

from sparkwatch.discourse import DiscourseClient
from sparkwatch.sync import refresh_engagement, sync
from sparkwatch.util import parse_dt, utcnow_iso

GAP_FLOOR = "2026-09-09T12:27:53+00:00"
SEEDED_AT = "2026-01-01T00:00:00+00:00"
CHANGED_AT = "2026-01-02T00:00:00+00:00"


def build(forum, topics, category: int = 723, more: bool = False) -> None:
    forum.add_page(category, listing([topic for topic, _posts in topics], more=more))
    for topic, posts in topics:
        forum.add_topic(topic_payload(topic, posts), posts)


def test_first_sync_creates_topics_and_posts(settings, conn, router, forum):
    topic = make_topic(101, bumped_days_ago=2, posts_count=2, highest_post_number=2)
    posts = [
        make_post(1001, 101, 1, "dual spark vllm fp8 tok/s 30 on GLM 5.3 Flash"),
        make_post(1002, 101, 2, "second post"),
    ]
    build(forum, [(topic, posts)])
    forum.install(router)
    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])

    assert result.new_topic_ids == [101]
    assert result.posts_new == 2
    assert result.posts_changed == 0
    assert result.partial is False
    assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
    assert result.watch_hits_new == 1

    row = conn.execute("SELECT * FROM posts WHERE id=1001").fetchone()
    assert row["content_hash"] == hashlib.sha256(row["raw"].encode()).hexdigest()
    assert row["first_fetched_at"] and row["changed_at"]
    assert conn.execute("SELECT last_synced_at FROM forums WHERE id=723").fetchone()[0] is not None


def test_second_sync_with_unchanged_fixtures_adds_nothing(settings, conn, router, forum):
    topic = make_topic(102, bumped_days_ago=2, posts_count=1)
    posts = [make_post(1021, 102, 1, "vllm on two sparks")]
    build(forum, [(topic, posts)])
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    first_requests = client.request_count
    second = sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    assert second.posts_new == 0
    assert second.posts_changed == 0
    assert client.request_count - first_requests == 1
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


def test_pagination_stops_at_cutoff(settings, conn, router, forum):
    old_topic = make_topic(103, bumped_days_ago=5)
    build(forum, [(old_topic, [make_post(1031, 103, 1, "old")])])
    forum.add_page(723, listing([make_topic(104, bumped_days_ago=4)], more=True))
    forum.install(router)
    conn.execute("UPDATE forums SET last_synced_at=? WHERE id=723", (utcnow_iso(),))
    sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])

    listing_calls = [call for call in forum.calls if call[0] == "listing"]
    assert len(listing_calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM topics WHERE id=104").fetchone()[0] == 0


def test_pinned_topics_do_not_stop_pagination(settings, conn, router, forum):
    pinned = make_topic(105, bumped_days_ago=500, pinned=True)
    forum.add_page(723, listing([pinned], more=True))
    forum.add_page(723, listing([make_topic(106, bumped_days_ago=1)]))
    forum.add_topic(
        topic_payload(pinned, [make_post(1051, 105, 1, "pinned readme")]),
        [make_post(1051, 105, 1, "pinned readme")],
    )
    forum.add_topic(
        topic_payload(make_topic(106, bumped_days_ago=1), [make_post(1061, 106, 1, "fresh")]),
        [make_post(1061, 106, 1, "fresh")],
    )
    forum.install(router)
    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])

    listing_calls = [call for call in forum.calls if call[0] == "listing"]
    assert len(listing_calls) == 2
    assert 106 in result.new_topic_ids
    assert 105 in result.new_topic_ids


def test_edited_post_updates_hash_and_changed_at(settings, conn, router, forum):
    topic = make_topic(107, bumped_days_ago=2)
    posts = [make_post(1071, 107, 1, "original text about fp8")]
    build(forum, [(topic, posts)])
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    before = conn.execute("SELECT content_hash, changed_at FROM posts WHERE id=1071").fetchone()

    edited = make_post(1071, 107, 1, "original text about fp8, now with nvfp4 numbers", version=2)
    bumped = make_topic(107, bumped_days_ago=0.5)
    forum.pages[723] = [listing([bumped])]
    forum.add_topic(topic_payload(bumped, [edited]), [edited])

    result = sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    after = conn.execute("SELECT content_hash, changed_at FROM posts WHERE id=1071").fetchone()

    assert result.posts_changed == 1
    assert after["content_hash"] != before["content_hash"]
    assert after["changed_at"] >= before["changed_at"]


def test_time_budget_returns_partial_and_resumes(settings, conn, router, forum):
    topic = make_topic(108, bumped_days_ago=2)
    build(forum, [(topic, [make_post(1081, 108, 1, "budget test")])])
    forum.install(router)
    client = DiscourseClient(settings)

    partial = sync(
        settings=settings,
        conn=conn,
        client=client,
        forum_ids=[723],
        time_budget_s=0.000001,
    )
    assert partial.partial is True
    assert conn.execute("SELECT last_synced_at FROM forums WHERE id=723").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0

    completed = sync(settings=settings, conn=conn, client=client, forum_ids=[723])
    assert completed.partial is False
    assert completed.posts_new == 1
    assert conn.execute("SELECT last_synced_at FROM forums WHERE id=723").fetchone()[0] is not None


def test_backfill_widens_floor_and_fetches_only_the_gap(settings, conn, router, forum):
    recent = make_topic(109, bumped_days_ago=2)
    older = make_topic(110, bumped_days_ago=45)
    build(forum, [(recent, [make_post(1091, 109, 1, "recent")])])
    forum.install(router)
    client = DiscourseClient(settings)
    sync(settings=settings, conn=conn, client=client, forum_ids=[723], lookback_days=30)
    floor_after_first = conn.execute("SELECT backfill_floor FROM forums WHERE id=723").fetchone()[0]

    forum.pages[723] = [listing([recent, older])]
    forum.add_topic(
        topic_payload(older, [make_post(1101, 110, 1, "older post")]),
        [make_post(1101, 110, 1, "older post")],
    )
    result = sync(settings=settings, conn=conn, client=client, forum_ids=[723], lookback_days=60)

    floor_after_second = conn.execute("SELECT backfill_floor FROM forums WHERE id=723").fetchone()[
        0
    ]
    assert floor_after_second < floor_after_first
    assert result.posts_new == 1
    assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 2


def test_sync_run_row_is_written(settings, conn, router, forum):
    build(forum, [(make_topic(111), [make_post(1111, 111, 1, "x")])])
    forum.install(router)
    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])
    row = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["requests"] == result.requests
    assert row["" if False else "trigger"] == "cli"
    assert "[111]" in row["new_topic_ids"]
    assert row["partial"] == 0
    assert row["forum_ids"] == "[723]"


def test_sync_normalises_timestamps_on_write(settings, conn, router, forum):
    topic = make_topic(112, bumped_days_ago=2, posts_count=1)
    posts = [make_post(1121, 112, 1, "post")]
    posts[0]["created_at"] = "2026-11-06T20:16:02.859Z"
    posts[0]["updated_at"] = "2026-11-06T20:16:02.859Z"
    topic["bumped_at"] = "2026-11-06T20:16:02.859Z"
    topic["created_at"] = "2026-11-01T09:00:00.500Z"
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)
    sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])

    stored_topic = conn.execute("SELECT created_at, bumped_at FROM topics WHERE id=112").fetchone()
    stored_post = conn.execute("SELECT created_at, updated_at FROM posts WHERE id=1121").fetchone()
    assert stored_topic["created_at"] == "2026-11-01T09:00:00+00:00"
    assert stored_topic["bumped_at"] == "2026-11-06T20:16:02+00:00"
    assert stored_post["created_at"] == "2026-11-06T20:16:02+00:00"
    assert stored_post["updated_at"] == "2026-11-06T20:16:02+00:00"


def _two_page_forum(forum) -> tuple[list[dict], list[dict]]:
    page0 = [make_topic(301, bumped_days_ago=2), make_topic(302, bumped_days_ago=3)]
    page1 = [make_topic(303, bumped_days_ago=10), make_topic(304, bumped_days_ago=11)]
    forum.add_page(723, listing(page0, more=True))
    forum.add_page(723, listing(page1))
    for topic in page0 + page1:
        posts = [make_post(topic["id"] * 10 + 1, topic["id"], 1, "post")]
        forum.add_topic(topic_payload(topic, posts), posts)
    return page0, page1


def test_max_pages_does_not_mark_the_window_covered(settings, conn, router, forum):
    page0, page1 = _two_page_forum(forum)
    forum.install(router)
    client = DiscourseClient(settings)

    first = sync(settings=settings, conn=conn, client=client, forum_ids=[723], max_pages=1)

    row = conn.execute("SELECT * FROM forums WHERE id=723").fetchone()
    assert sorted(first.new_topic_ids) == [301, 302]
    assert row["last_synced_at"] is None
    assert parse_dt(row["listing_floor"]) == parse_dt(page0[-1]["bumped_at"])
    assert row["backfill_floor"] is None

    second = sync(settings=settings, conn=conn, client=client, forum_ids=[723])

    assert sorted(second.new_topic_ids) == [303, 304]
    row = conn.execute("SELECT * FROM forums WHERE id=723").fetchone()
    assert row["last_synced_at"] is not None
    assert parse_dt(row["listing_floor"]) <= parse_dt(row["backfill_floor"])
    assert conn.execute("SELECT COUNT(*) FROM topics WHERE forum_id=723").fetchone()[0] == 4


def test_reaching_the_cutoff_records_the_window_floor(settings, conn, router, forum):
    build(forum, [(make_topic(305, bumped_days_ago=2), [make_post(3051, 305, 1, "x")])])
    forum.install(router)
    sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])

    row = conn.execute("SELECT * FROM forums WHERE id=723").fetchone()
    assert row["last_synced_at"] is not None
    assert parse_dt(row["listing_floor"]) == parse_dt(row["backfill_floor"])


def test_repair_coverage_moves_the_floor_to_the_oldest_stored_topic(settings, conn):
    from sparkwatch.sync import repair_coverage

    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (721, 'p', 'n')")
    conn.execute(
        "UPDATE forums SET backfill_floor=?, listing_floor=? WHERE id=723",
        (GAP_FLOOR, GAP_FLOOR),
    )
    conn.execute(
        "UPDATE forums SET backfill_floor=?, listing_floor=? WHERE id=721",
        (GAP_FLOOR, GAP_FLOOR),
    )
    for topic_id, bumped in ((401, "2026-10-25T17:59:55.686Z"), (402, "2026-11-06T20:16:02.859Z")):
        conn.execute(
            "INSERT INTO topics (id, forum_id, title, bumped_at, pinned, first_fetched_at)"
            " VALUES (?, 723, 't', ?, 0, ?)",
            (topic_id, bumped, bumped),
        )
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, bumped_at, pinned, first_fetched_at)"
        " VALUES (403, 723, 'pinned', '2026-01-01T00:00:00+00:00', 1, ?)",
        (GAP_FLOOR,),
    )

    payload = repair_coverage(conn, 723)

    assert payload["applied"] is False
    assert payload["would_create_gap"] is True
    assert "later than the old one" in payload["warning"]
    row = conn.execute("SELECT listing_floor FROM forums WHERE id=723").fetchone()
    assert row["listing_floor"] == GAP_FLOOR

    payload = repair_coverage(conn, 723, apply=True)

    assert payload["changed"] is True
    assert payload["applied"] is True
    assert payload["old_listing_floor"] == GAP_FLOOR
    assert payload["new_listing_floor"] == "2026-10-25T17:59:55+00:00"
    row = conn.execute("SELECT listing_floor FROM forums WHERE id=723").fetchone()
    assert row["listing_floor"] == "2026-10-25T17:59:55+00:00"
    untouched = conn.execute("SELECT listing_floor FROM forums WHERE id=721").fetchone()
    assert untouched["listing_floor"] == GAP_FLOOR


def test_gap_in_the_listing_widens_the_cutoff(settings, conn, router, forum):
    page0, page1 = _two_page_forum(forum)
    forum.install(router)
    conn.execute(
        "UPDATE forums SET last_synced_at=?, backfill_floor=?, listing_floor=? WHERE id=723",
        (utcnow_iso(), "2026-09-09T12:27:53+00:00", page0[-1]["bumped_at"]),
    )

    result = sync(settings=settings, conn=conn, client=DiscourseClient(settings), forum_ids=[723])

    assert sorted(result.new_topic_ids) == [301, 302, 303, 304]
    assert any(call == ("listing", 723, 1) for call in forum.calls)
    row = conn.execute("SELECT * FROM forums WHERE id=723").fetchone()
    assert parse_dt(row["listing_floor"]) <= parse_dt("2026-09-09T12:27:53+00:00")


def test_cli_repair_coverage_reports_old_and_new(settings, conn):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    conn.execute(
        "UPDATE forums SET backfill_floor=?, listing_floor=? WHERE id=723",
        (GAP_FLOOR, GAP_FLOOR),
    )
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, bumped_at, pinned, first_fetched_at)"
        " VALUES (404, 723, 't', '2026-10-25T17:59:55.686Z', 0, ?)",
        (GAP_FLOOR,),
    )

    result = CliRunner().invoke(app, ["sync", "--repair-coverage", "--forum", "723"])

    assert result.exit_code == 0
    assert "old_listing_floor" in result.stdout
    assert "new_listing_floor" in result.stdout
    assert '"applied": false' in result.stdout
    assert conn.execute("SELECT listing_floor FROM forums WHERE id=723").fetchone()[0] == GAP_FLOOR

    applied = CliRunner().invoke(app, ["sync", "--repair-coverage", "--forum", "723", "--apply"])

    assert applied.exit_code == 0
    assert '"applied": true' in applied.stdout
    assert (
        conn.execute("SELECT listing_floor FROM forums WHERE id=723").fetchone()[0]
        == "2026-10-25T17:59:55+00:00"
    )


def test_cli_repair_coverage_needs_exactly_one_forum(settings, conn):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    result = CliRunner().invoke(app, ["sync", "--repair-coverage"])
    assert result.exit_code == 1
    assert "exactly one --forum" in result.stdout


def _seed_engagement_topic(conn, topic_id: int, posts: list[dict]) -> None:
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, posts_count,"
        " highest_post_number, relevance_score, first_fetched_at)"
        " VALUES (?, 723, 'engagement topic', ?, ?, ?, ?, 0, ?)",
        (
            topic_id,
            posts[0]["created_at"],
            posts[0]["created_at"],
            len(posts),
            len(posts),
            posts[0]["created_at"],
        ),
    )
    for post in posts:
        conn.execute(
            "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw,"
            " cooked, content_hash, like_count, accepted_answer, first_fetched_at, changed_at,"
            " fetched_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?,?)",
            (
                post["id"],
                topic_id,
                post["post_number"],
                post["post_type"],
                post["username"],
                post["created_at"],
                post["raw"],
                post["cooked"],
                hashlib.sha256(post["raw"].encode()).hexdigest(),
                SEEDED_AT,
                CHANGED_AT,
                SEEDED_AT,
            ),
        )


def test_refresh_engagement_updates_likes_and_leaves_content_alone(settings, conn, router, forum):
    posts = [
        make_post(2001, 200, 1, "first post", extra={"reaction_users_count": 5}),
        make_post(
            2002,
            200,
            2,
            "second post",
            extra={"reaction_users_count": 3, "accepted_answer": True},
        ),
        make_post(2003, 200, 3, "third post"),
    ]
    _seed_engagement_topic(conn, 200, posts)
    forum.add_posts(200, posts)
    forum.install(router)

    floors_before = [tuple(row) for row in conn.execute("SELECT * FROM forums ORDER BY id")]
    cursors_before = [tuple(row) for row in conn.execute("SELECT * FROM digest_cursors")]

    result = refresh_engagement(
        settings=settings,
        conn=conn,
        client=DiscourseClient(settings),
        days=14,
        forum_ids=[723],
    )

    assert result.requests == 1
    assert result.topics == 1
    assert result.posts_checked == 3
    assert result.likes_changed == 2
    assert result.accepted_changed == 1
    assert result.posts_changed == 0
    assert result.partial is False
    assert result.errors == []
    assert forum.post_requests() == [(2001, 2002, 2003)]

    rows = {row["id"]: row for row in conn.execute("SELECT * FROM posts WHERE topic_id=200")}
    assert rows[2001]["like_count"] == 5
    assert rows[2002]["like_count"] == 3
    assert rows[2002]["accepted_answer"] == 1
    assert rows[2003]["like_count"] == 0
    assert all(rows[post_id]["changed_at"] == CHANGED_AT for post_id in rows)
    assert all(rows[post_id]["fetched_at"] != SEEDED_AT for post_id in rows)

    assert [tuple(row) for row in conn.execute("SELECT * FROM forums ORDER BY id")] == floors_before
    assert [tuple(row) for row in conn.execute("SELECT * FROM digest_cursors")] == cursors_before

    run = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["trigger"] == "engagement"
    assert run["posts_new"] == 0
    assert run["requests"] == 1
    assert run["posts_changed"] == 0


def test_refresh_engagement_only_targets_recent_posts(settings, conn, router, forum):
    posts = [
        make_post(2101, 210, 1, "recent post", days_ago=1),
        make_post(2102, 210, 2, "old post", days_ago=30),
    ]
    _seed_engagement_topic(conn, 210, posts)
    forum.add_posts(210, posts)
    forum.install(router)

    result = refresh_engagement(
        settings=settings,
        conn=conn,
        client=DiscourseClient(settings),
        days=14,
        forum_ids=[723],
    )

    assert forum.post_requests() == [(2101,)]
    assert result.posts_checked == 1


def test_refresh_engagement_skips_missing_posts(settings, conn, router, forum):
    posts = [make_post(2201, 220, 1, "a"), make_post(2202, 220, 2, "b")]
    _seed_engagement_topic(conn, 220, posts)
    conn.execute(
        "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
        " VALUES (220, 2202, 'deleted', ?, ?)",
        (SEEDED_AT, SEEDED_AT),
    )
    forum.add_posts(220, posts)
    forum.install(router)

    result = refresh_engagement(
        settings=settings,
        conn=conn,
        client=DiscourseClient(settings),
        days=14,
        forum_ids=[723],
    )

    assert forum.post_requests() == [(2201,)]
    assert result.posts_checked == 1


def test_refresh_engagement_records_a_genuine_edit(settings, conn, router, forum):
    original = make_post(2301, 230, 1, "original text")
    edited = make_post(2301, 230, 1, "edited text", extra={"reaction_users_count": 2})
    _seed_engagement_topic(conn, 230, [original])
    forum.add_posts(230, [edited])
    forum.install(router)

    result = refresh_engagement(
        settings=settings,
        conn=conn,
        client=DiscourseClient(settings),
        days=14,
        forum_ids=[723],
    )

    assert result.posts_changed == 1
    assert result.likes_changed == 1
    row = conn.execute(
        "SELECT raw, content_hash, changed_at, like_count FROM posts WHERE id=2301"
    ).fetchone()
    assert row["raw"] == "edited text"
    assert row["content_hash"] == hashlib.sha256(b"edited text").hexdigest()
    assert row["changed_at"] != CHANGED_AT
    assert row["like_count"] == 2


def test_refresh_engagement_stops_at_the_budget(settings, conn, router, forum):
    posts = [make_post(2401, 240, 1, "post")]
    _seed_engagement_topic(conn, 240, posts)
    forum.add_posts(240, posts)
    forum.install(router)

    result = refresh_engagement(
        settings=settings,
        conn=conn,
        client=DiscourseClient(settings),
        days=14,
        forum_ids=[723],
        time_budget_s=0,
    )

    assert result.partial is True
    assert result.requests == 0
    assert forum.calls == []


def test_cli_refresh_engagement(settings, conn, router, forum):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    posts = [make_post(2501, 250, 1, "post", extra={"reaction_users_count": 4})]
    _seed_engagement_topic(conn, 250, posts)
    forum.add_posts(250, posts)
    forum.install(router)

    result = CliRunner().invoke(
        app,
        ["sync", "--refresh-engagement", "--days", "14", "--forum", "723", "--budget", "60"],
    )

    assert result.exit_code == 0
    assert '"posts_checked": 1' in result.stdout
    assert '"likes_changed": 1' in result.stdout
    assert '"partial": false' in result.stdout


def test_refresh_engagement_rejects_a_zero_day_window(settings, conn):
    import pytest

    with pytest.raises(ValueError, match="days must be at least 1"):
        refresh_engagement(settings=settings, conn=conn, days=0)


def test_refresh_engagement_continues_after_a_bad_topic(settings, conn, forum):
    first = [make_post(2701, 270, 1, "a")]
    second = [make_post(2711, 271, 1, "b", extra={"reaction_users_count": 2})]
    _seed_engagement_topic(conn, 270, first)
    _seed_engagement_topic(conn, 271, second)

    class FlakyClient:
        request_count = 0

        def get_posts(self, topic_id, post_ids):
            self.request_count += 1
            if topic_id == 270:
                raise RuntimeError("boom")
            return {"post_stream": {"posts": [post for post in second if post["id"] in post_ids]}}

    client = FlakyClient()
    result = refresh_engagement(
        settings=settings, conn=conn, client=client, days=14, forum_ids=[723]
    )

    assert result.requests == 2
    assert result.topics == 1
    assert result.posts_checked == 1
    assert result.errors and "270" in result.errors[0]


def test_cli_refresh_engagement_rejects_repair_coverage(settings, conn):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    result = CliRunner().invoke(
        app, ["sync", "--refresh-engagement", "--repair-coverage", "--forum", "723"]
    )

    assert result.exit_code == 1
    assert "cannot be combined" in result.stdout


def test_cli_refresh_engagement_rejects_zero_days(settings, conn):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    result = CliRunner().invoke(app, ["sync", "--refresh-engagement", "--days", "0"])

    assert result.exit_code == 1
    assert "days must be at least 1" in result.stdout
