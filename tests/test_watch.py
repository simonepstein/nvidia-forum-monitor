from __future__ import annotations

import re

import pytest

from sparkwatch import watch


def _seed_post(
    conn, post_id: int, topic_id: int, title: str, raw: str, created_at: str | None = None
) -> None:
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT OR IGNORE INTO topics (id, forum_id, title, first_fetched_at)"
        " VALUES (?, 723, ?, '2026-01-01T00:00:00+00:00')",
        (topic_id, title),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " created_at, first_fetched_at, changed_at) VALUES (?, ?, 1, 1, ?, '<p>x</p>', ?, ?,"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (
            post_id,
            topic_id,
            raw,
            f"h{post_id}",
            created_at or "2026-01-01T00:00:00+00:00",
        ),
    )


def _regex(conn, label: str) -> str:
    return conn.execute("SELECT match_regex FROM watchlist WHERE label=?", (label,)).fetchone()[0]


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("GLM 5.3 Flash", "GLM-5.3-Flash"),
        ("GLM 5.3 Flash", "glm 5.3 flash"),
        ("GLM 5.3 Flash", "glm_5.3_flash"),
        ("DeepSeek V4.1 Flash", "DeepSeek-V4.1-Flash"),
        ("DeepSeek V4.1 Flash", "deepseek v4.1 flash"),
        ("Qwen3.8 Flash Next", "Qwen3.8-Flash-Next"),
        ("Qwen3.8 Flash Next", "qwen 3.8 next"),
        ("Qwen4", "Qwen4-72B"),
        ("Qwen4", "Qwen4 Flash"),
        ("Qwen4", "Qwen 4 27B"),
    ],
)
def test_seed_regexes_match(conn, label, text):
    assert re.search(_regex(conn, label), text, re.I)


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("Qwen4", "Qwen3.8"),
        ("Qwen4", "qwen 4.5"),
        ("Qwen4", "Qwen3.8-Flash-Next"),
        ("Qwen4", "Qwen4ExpForConditionalGeneration"),
        ("Qwen4", "vllm/models/qwen4_exp/nvidia/b12x_qsa.py"),
        ("Qwen4", "Qwen3.8-Flash-Next (Qwen4Exp)"),
        ("Qwen4", "Qwen 4B"),
        ("GLM 5.3 Flash", "glm 5.2 flash"),
    ],
)
def test_seed_regexes_do_not_match(conn, label, text):
    assert not re.search(_regex(conn, label), text, re.I)


def test_release_regex_is_seeded(conn):
    row = conn.execute("SELECT release_regex FROM watchlist WHERE label='Qwen4'").fetchone()
    assert row["release_regex"]
    assert re.search(row["release_regex"], "Qwen 4 27B weights are out", re.I)
    assert re.search(row["release_regex"], "https://huggingface.co/Qwen/Qwen4-27B", re.I)
    # Hopeful phrasing still matches by design (ISSUES #17): the agent makes the final call.
    assert re.search(row["release_regex"], "hoping Qwen 4 is released soon", re.I)
    assert not re.search(row["release_regex"], "Qwen 4 27B has been recently leaked", re.I)


def test_sighting_needs_a_release_match_when_a_release_regex_is_set(conn):
    _seed_post(conn, 20, 20, "Qwen4 rumours", "I am betting that Qwen 4 27B arrives next week")
    watch.match_post(conn, 20, 20, "Qwen4 rumours", "betting that Qwen 4 27B arrives next week")
    row = conn.execute("SELECT * FROM watchlist WHERE label='Qwen4'").fetchone()
    assert row["first_seen_post_id"] == 20
    assert row["sighting_post_id"] is None

    _seed_post(conn, 21, 21, "Qwen4 release", "Qwen 4 27B weights are out on the hub")
    watch.match_post(conn, 21, 21, "Qwen4 release", "Qwen 4 27B weights are out on the hub")
    row = conn.execute("SELECT * FROM watchlist WHERE label='Qwen4'").fetchone()
    assert row["sighting_post_id"] == 21
    assert row["sighting_acked_at"] is None

    watch.ack_sighting(conn, "Qwen4")
    assert (
        conn.execute("SELECT sighting_acked_at FROM watchlist WHERE label='Qwen4'").fetchone()[0]
        is not None
    )

    _seed_post(conn, 22, 22, "Qwen4 released", "Qwen 4 27B is out now")
    watch.match_post(conn, 22, 22, "Qwen4 released", "Qwen 4 27B is out now")
    row = conn.execute("SELECT * FROM watchlist WHERE label='Qwen4'").fetchone()
    assert row["sighting_post_id"] == 22
    assert row["sighting_acked_at"] is None


def test_first_hit_raises_the_alert_without_a_release_regex(conn):
    watch.add_watch(conn, "Kimi K2", r"kimi[\s._-]*k2", kind="anticipated")
    _seed_post(conn, 23, 23, "Kimi K2 rumour", "kimi k2 might be coming")
    watch.match_post(conn, 23, 23, "Kimi K2 rumour", "kimi k2 might be coming")
    row = conn.execute("SELECT * FROM watchlist WHERE label='Kimi K2'").fetchone()
    assert row["sighting_post_id"] == 23

    watch.ack_sighting(conn, "Kimi K2")
    _seed_post(conn, 24, 24, "Kimi K2 again", "kimi k2 spotted again")
    watch.match_post(conn, 24, 24, "Kimi K2 again", "kimi k2 spotted again")
    row = conn.execute("SELECT * FROM watchlist WHERE label='Kimi K2'").fetchone()
    assert row["sighting_post_id"] == 23
    assert row["sighting_acked_at"] is not None


def test_first_hit_sets_first_seen_for_anticipated(conn):
    _seed_post(conn, 1, 1, "Qwen4 first report", "Qwen4-72B runs at 12 tok/s on two sparks")
    hits = watch.match_post(conn, 1, 1, "Qwen4 first report", "Qwen4-72B runs at 12 tok/s")
    row = conn.execute("SELECT * FROM watchlist WHERE label='Qwen4'").fetchone()
    assert row["id"] in hits
    assert row["first_seen_post_id"] == 1
    assert row["first_seen_at"] is not None


def test_first_seen_is_not_moved_by_later_hits(conn):
    _seed_post(conn, 1, 1, "Qwen4 report", "Qwen4-72B")
    _seed_post(conn, 2, 2, "Qwen4 again", "Qwen4-72B more numbers")
    watch.match_post(conn, 1, 1, "Qwen4 report", "Qwen4-72B")
    watch.match_post(conn, 2, 2, "Qwen4 again", "Qwen4-72B more numbers")
    row = conn.execute("SELECT * FROM watchlist WHERE label='Qwen4'").fetchone()
    assert row["first_seen_post_id"] == 1


def test_hits_are_unique(conn):
    _seed_post(conn, 3, 2, "GLM 5.3 Flash", "GLM-5.3-Flash on vllm")
    watch.match_post(conn, 3, 2, "GLM 5.3 Flash", "GLM-5.3-Flash on vllm")
    watch.match_post(conn, 3, 2, "GLM 5.3 Flash", "GLM-5.3-Flash on vllm")
    count = conn.execute("SELECT COUNT(*) FROM watch_hits WHERE post_id=3").fetchone()[0]
    assert count == 1


def test_ack_sighting_clears_it(conn):
    _seed_post(conn, 4, 3, "Qwen4", "Qwen4-72B")
    watch.match_post(conn, 4, 3, "Qwen4", "Qwen4-72B")
    row = watch.ack_sighting(conn, "Qwen4")
    assert row["sighting_acked_at"] is not None


def test_add_watch_rematches_stored_posts(conn):
    _seed_post(conn, 5, 4, "Kimi K2 thread", "kimi k2 thinking is fast on two sparks")
    created = watch.add_watch(conn, "Kimi K2", r"kimi[\s._-]*k2", kind="tracking")
    assert created["label"] == "Kimi K2"
    assert conn.execute("SELECT COUNT(*) FROM watch_hits WHERE post_id=5").fetchone()[0] == 1


def test_update_watch_regex_rematches(conn):
    _seed_post(conn, 6, 5, "MiniMax M2 thread", "minimax m2 runs on vllm")
    watch.update_watch(conn, "GLM 5.3 Flash", match_regex=r"minimax[\s._-]*m2")
    hits = conn.execute(
        "SELECT COUNT(*) FROM watch_hits h JOIN watchlist w ON w.id = h.watch_id"
        " WHERE w.label='GLM 5.3 Flash'"
    ).fetchone()[0]
    assert hits == 1


def test_update_watch_drops_hits_the_old_regex_made(conn):
    _seed_post(
        conn, 25, 25, "Qwen4Exp notes", "Resolved architecture: Qwen4ExpForConditionalGeneration"
    )
    watch.update_watch(conn, "Qwen4", match_regex=r"qwen[\s._-]*4")
    before = conn.execute(
        "SELECT COUNT(*) FROM watch_hits h JOIN watchlist w ON w.id = h.watch_id"
        " WHERE w.label='Qwen4'"
    ).fetchone()[0]
    assert before == 1

    watch.update_watch(conn, "Qwen4", match_regex=r"qwen[\s._-]*4(?![\d.])(?![\s_-]*exp)(?!\s*b\b)")
    after = conn.execute(
        "SELECT COUNT(*) FROM watch_hits h JOIN watchlist w ON w.id = h.watch_id"
        " WHERE w.label='Qwen4'"
    ).fetchone()[0]
    assert after == 0


def test_invalid_regex_is_rejected(conn):
    with pytest.raises(ValueError):
        watch.add_watch(conn, "Broken", "([unclosed")


def test_list_watchlist_reports_counts(conn):
    from conftest import iso_days_ago

    _seed_post(conn, 7, 6, "GLM 5.3 Flash", "glm 5.3 flash", created_at=iso_days_ago(2))
    watch.match_post(conn, 7, 6, "GLM 5.3 Flash", "glm 5.3 flash")
    rows = {row["label"]: row for row in watch.list_watchlist(conn)}
    assert rows["GLM 5.3 Flash"]["hits_7d"] == 1
    assert rows["GLM 5.3 Flash"]["hits_30d"] == 1
    assert rows["GLM 5.3 Flash"]["latest_hit"] == iso_days_ago(2)


def test_list_watchlist_ignores_old_posts_and_backfill_time(conn):
    from conftest import iso_days_ago

    _seed_post(conn, 8, 7, "GLM 5.3 Flash", "glm 5.3 flash", created_at=iso_days_ago(45))
    watch.match_post(conn, 8, 7, "GLM 5.3 Flash", "glm 5.3 flash")
    rows = {row["label"]: row for row in watch.list_watchlist(conn)}
    assert rows["GLM 5.3 Flash"]["hits_7d"] == 0
    assert rows["GLM 5.3 Flash"]["hits_30d"] == 0
    assert rows["GLM 5.3 Flash"]["latest_hit"] == iso_days_ago(45)
