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
    assert first.execute("PRAGMA user_version").fetchone()[0] == 2
    tables = _table_count(first)
    first.close()

    second = connect(path)
    assert second.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _table_count(second) == tables
    second.close()


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
