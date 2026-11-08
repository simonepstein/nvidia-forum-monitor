from __future__ import annotations

import sqlite3

import pytest

from sparkwatch.db import connect, load_seeds, open_db


def _table_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','trigger')"
    ).fetchone()[0]


def test_migration_applies_once(settings, tmp_path):
    path = tmp_path / "mig.db"
    first = connect(path)
    assert first.execute("PRAGMA user_version").fetchone()[0] == 4
    tables = _table_count(first)
    first.close()

    second = connect(path)
    assert second.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _table_count(second) == tables
    second.close()


def _v2_connection(path):
    import sqlite3

    from sparkwatch.db import MIGRATION_1, MIGRATION_2

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(MIGRATION_1)
    conn.executescript(MIGRATION_2)
    conn.execute("PRAGMA user_version=2")
    return conn


TIMESTAMP_COLUMNS = (
    ("forums", ("last_synced_at", "backfill_floor", "listing_floor")),
    ("topics", ("created_at", "bumped_at", "last_posted_at", "first_fetched_at", "fetched_at")),
    ("posts", ("created_at", "updated_at", "first_fetched_at", "changed_at", "fetched_at")),
    ("missing_posts", ("first_seen_at", "last_checked_at")),
    ("sync_runs", ("started_at", "finished_at")),
    ("watchlist", ("first_seen_at", "sighting_at", "sighting_acked_at", "created_at")),
    ("watch_hits", ("created_at",)),
    ("digest_cursors", ("last_seen_at",)),
    ("repos", ("pushed_at", "head_seen_at", "first_seen_at", "watched_at", "last_checked_at")),
    ("repo_mentions", ("mentioned_at",)),
    ("repo_installs", ("installed_at", "updated_at")),
    ("repo_events", ("occurred_at", "detected_at")),
    ("compare_cache", ("fetched_at",)),
    ("hf_models", ("first_seen_at",)),
    ("hf_mentions", ("mentioned_at",)),
    ("models", ("released_at", "updated_at")),
    ("recipes", ("reported_at", "created_at")),
    ("benchmark_results", ("measured_at", "created_at")),
    ("research_notes", ("as_of", "created_at")),
    ("flags", ("created_at", "resolved_at")),
)


def test_migration_four_leaves_one_timestamp_format(settings, tmp_path):
    from sparkwatch.db import migrate

    conn = _v2_connection(tmp_path / "mig4.db")
    conn.execute(
        "INSERT INTO forums (id, path, name, last_synced_at, backfill_floor)"
        " VALUES (723, 'p', 'n', '2026-11-08T13:23:51+00:00', '2026-09-09T12:27:53.000Z')"
    )
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, first_fetched_at)"
        " VALUES (1, 723, 't', '2026-10-25T17:59:55.686Z', '2026-11-06T20:16:02.859Z',"
        " '2026-11-08T13:03:49+00:00')"
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, created_at, updated_at,"
        " content_hash, first_fetched_at, changed_at)"
        " VALUES (11, 1, 1, 1, '2025-10-10T23:03:58.609Z', '2025-10-10T23:04:00Z', 'h',"
        " '2026-11-08T13:03:49+00:00', '2026-11-08T13:03:49+00:00')"
    )
    conn.execute(
        "INSERT INTO benchmark_results (model_canonical, benchmark, score, source_type,"
        " measured_at, created_by, created_at)"
        " VALUES ('m', 'gpqa', 1.0, 'paper', 'late October 2026', 'agent',"
        " '2026-11-08T13:03:49.500Z')"
    )
    migrate(conn)

    mixed: list[str] = []
    for table, columns in TIMESTAMP_COLUMNS:
        for column in columns:
            rows = [
                row[0]
                for row in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
            ]
            z_form = [value for value in rows if str(value).endswith("Z")]
            plus_form = [value for value in rows if str(value).endswith("+00:00")]
            if z_form and plus_form:
                mixed.append(f"{table}.{column}")
    assert mixed == []

    assert (
        conn.execute("SELECT backfill_floor FROM forums WHERE id=723").fetchone()[0]
        == "2026-09-09T12:27:53+00:00"
    )
    assert (
        conn.execute("SELECT bumped_at FROM topics WHERE id=1").fetchone()[0]
        == "2026-11-06T20:16:02+00:00"
    )
    assert (
        conn.execute("SELECT created_at FROM posts WHERE id=11").fetchone()[0]
        == "2025-10-10T23:03:58+00:00"
    )
    # Unparseable free text is kept, not nulled.
    assert (
        conn.execute("SELECT measured_at FROM benchmark_results").fetchone()[0]
        == "late October 2026"
    )
    # Plain string comparisons are now correct.
    ordered = [row[0] for row in conn.execute("SELECT id FROM topics ORDER BY created_at DESC")]
    assert ordered == [1]
    newer = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE created_at >= '2025-10-10T23:03:58+00:00'"
    ).fetchone()[0]
    assert newer == 1
    conn.close()


def test_migration_four_adds_sync_run_forums_and_engine_raw(settings, tmp_path):
    from sparkwatch.db import migrate

    conn = _v2_connection(tmp_path / "mig4b.db")
    migrate(conn)
    sync_columns = {info["name"] for info in conn.execute("PRAGMA table_info(sync_runs)")}
    assert "forum_ids" in sync_columns
    recipe_columns = {info["name"] for info in conn.execute("PRAGMA table_info(recipes)")}
    assert "engine_raw" in recipe_columns
    conn.close()


def test_migration_four_marks_legacy_hidden_tombstones_for_recheck(settings, tmp_path):
    from sparkwatch.db import migrate

    conn = _v2_connection(tmp_path / "mig4c.db")
    conn.execute("INSERT INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute("INSERT INTO topics (id, forum_id, title) VALUES (900, 723, 't')")
    for post_id, reason in ((9001, "hidden"), (9002, "deleted")):
        conn.execute(
            "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
            " VALUES (900, ?, ?, '2026-11-01T00:00:00+00:00', '2026-11-08T13:03:49.000Z')",
            (post_id, reason),
        )
    migrate(conn)

    rows = {
        row["post_id"]: row["last_checked_at"]
        for row in conn.execute("SELECT post_id, last_checked_at FROM missing_posts")
    }
    assert rows[9001] == "1970-01-01T00:00:00+00:00"
    assert rows[9002] == "2026-11-08T13:03:49+00:00"
    conn.close()


def test_migration_three_initialises_listing_floor_and_keeps_coverage(settings, tmp_path):
    from sparkwatch.db import migrate

    conn = _v2_connection(tmp_path / "mig3.db")
    conn.execute(
        "INSERT INTO forums (id, path, name, last_synced_at, backfill_floor)"
        " VALUES (723, 'p', 'n', '2026-11-08T13:23:51+00:00', '2026-09-09T12:27:53+00:00')"
    )
    assert migrate(conn) == 4

    row = conn.execute("SELECT * FROM forums WHERE id=723").fetchone()
    assert row["listing_floor"] == "2026-09-09T12:27:53+00:00"
    assert row["backfill_floor"] == "2026-09-09T12:27:53+00:00"
    assert row["last_synced_at"] == "2026-11-08T13:23:51+00:00"
    columns = {info["name"] for info in conn.execute("PRAGMA table_info(watchlist)")}
    assert {"release_regex", "sighting_post_id", "sighting_at"} <= columns
    conn.close()


def test_migration_three_reclassifies_non_content_tombstones(settings, tmp_path):
    from sparkwatch.db import migrate

    conn = _v2_connection(tmp_path / "mig3b.db")
    conn.execute("INSERT INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute("INSERT INTO topics (id, forum_id, title) VALUES (900, 723, 't')")
    for post_id, post_type in ((9001, 1), (9002, 3), (9003, 4)):
        conn.execute(
            "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
            " first_fetched_at, changed_at) VALUES (?, 900, ?, ?, 'x', '<p>x</p>', 'h',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (post_id, post_id % 100, post_type),
        )
        conn.execute(
            "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
            " VALUES (900, ?, 'hidden', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (post_id,),
        )
    migrate(conn)

    reasons = {
        row["post_id"]: row["reason"]
        for row in conn.execute("SELECT post_id, reason FROM missing_posts ORDER BY post_id")
    }
    assert reasons == {9001: "hidden", 9002: "not_content", 9003: "not_content"}
    conn.execute(
        "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
        " VALUES (900, 9004, 'not_content', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')"
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM missing_posts WHERE reason='not_content'"
    ).fetchone()[0]
    assert count == 3
    conn.close()


def test_wal_and_foreign_keys(settings, tmp_path):
    conn = connect(tmp_path / "wal.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_missing_posts_reason_is_constrained(conn):
    conn.execute("INSERT INTO forums (id, path, name) VALUES (1, 'p', 'n')")
    conn.execute("INSERT INTO topics (id, forum_id, title) VALUES (10, 1, 't')")
    conn.execute(
        "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
        " VALUES (10, 99, 'deleted', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
            " VALUES (10, 100, 'bogus', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )


def test_seeds_are_idempotent(settings):
    conn = open_db(settings)
    first = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    aliases = conn.execute("SELECT COUNT(*) FROM model_aliases").fetchone()[0]
    models = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    load_seeds(conn)
    load_seeds(conn)
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == first == 4
    assert conn.execute("SELECT COUNT(*) FROM model_aliases").fetchone()[0] == aliases
    assert conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] == models


def test_seeds_do_not_overwrite_agent_rows(settings):
    conn = open_db(settings)
    conn.execute(
        "UPDATE models SET note='agent wrote this', params_total_b=355 WHERE canonical='qwen4'"
    )
    conn.execute(
        "UPDATE watchlist SET first_seen_post_id=4242, first_seen_at='2026-01-02T00:00:00+00:00',"
        " sighting_acked_at='2026-01-03T00:00:00+00:00' WHERE label='Qwen4'"
    )
    conn.execute(
        "UPDATE model_aliases SET canonical='qwen-4-agent', source='agent' WHERE alias='qwen4'"
    )
    load_seeds(conn)

    model = conn.execute("SELECT * FROM models WHERE canonical='qwen4'").fetchone()
    assert model["note"] == "agent wrote this"
    assert model["params_total_b"] == 355

    watch_row = conn.execute("SELECT * FROM watchlist WHERE label='Qwen4'").fetchone()
    assert watch_row["first_seen_post_id"] == 4242
    assert watch_row["first_seen_at"] == "2026-01-02T00:00:00+00:00"
    assert watch_row["sighting_acked_at"] == "2026-01-03T00:00:00+00:00"

    alias = conn.execute("SELECT * FROM model_aliases WHERE alias='qwen4'").fetchone()
    assert alias["canonical"] == "qwen-4-agent"
    assert alias["source"] == "agent"


def test_forums_are_ensured(settings):
    conn = open_db(settings)
    ids = [row["id"] for row in conn.execute("SELECT id FROM forums ORDER BY id")]
    assert ids == [721, 723]
