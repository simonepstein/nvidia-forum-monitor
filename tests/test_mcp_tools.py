from __future__ import annotations

import pytest

from sparkwatch import mcp_server as m

PAST = "2026-01-01T00:00:00+00:00"
QUOTE = "GLM 5.3 Flash runs at 31 tok/s on two Sparks with vLLM 0.11 and nvfp4"


def seed(conn) -> None:
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, posts_count, like_count,"
        " relevance_score, first_fetched_at) VALUES (700, 723, 'GLM 5.3 Flash on two Sparks', ?, ?, 2, 5, 12.5, ?)",
        (PAST, PAST, PAST),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw, cooked,"
        " content_hash, first_fetched_at, changed_at) VALUES (7001, 700, 1, 1, 'alice', ?, ?, '<p>x</p>', 'h1', ?, ?)",
        (PAST, QUOTE, PAST, PAST),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw, cooked,"
        " content_hash, first_fetched_at, changed_at) VALUES (7002, 700, 2, 1, 'bob', ?, ?, '<p>x</p>', 'h2', ?, ?)",
        (PAST, "I get 28 tok/s with fp8 instead", PAST, PAST),
    )


def test_record_recipe_accepts_verified_quote(settings, conn):
    seed(conn)
    m.upsert_model("glm-5.3-flash", family="glm", params_total_b=355)
    result = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": QUOTE}],
        quant="nvfp4",
        engine="vLLM 0.11",
        tok_s_decode=31,
    )
    assert result["review"] == "accepted"
    assert result["model"] == "glm-5.3-flash"
    assert result["quant"] == "nvfp4"
    assert result["engine"] == "vllm"
    assert result["evidence"] == [{"post_id": 7001, "verified": True}]
    assert result["reported_at"] == PAST
    assert result["fit"]["estimate_gb"] is not None


def test_record_recipe_with_fabricated_quote_is_proposed(settings, conn):
    seed(conn)
    result = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": "this sentence is not in the post"}],
    )
    assert result["review"] == "proposed"
    assert result["evidence"] == [{"post_id": 7001, "verified": False}]


def test_record_recipe_returns_similar_recipes(settings, conn):
    seed(conn)
    first = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": QUOTE}],
        quant="nvfp4",
        engine="vllm",
    )
    second = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7002, "quote": "I get 28 tok/s with fp8 instead"}],
        quant="nvfp4",
        engine="vllm",
    )
    assert [row["id"] for row in second["similar_recipes"]] == [first["recipe_id"]]


def test_record_recipe_supersedes(settings, conn):
    seed(conn)
    old = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="partial",
        evidence=[{"post_id": 7001, "quote": QUOTE}],
    )
    new = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": QUOTE}],
        supersedes=old["recipe_id"],
    )
    row = conn.execute(
        "SELECT superseded_by FROM recipes WHERE id=?", (old["recipe_id"],)
    ).fetchone()
    assert row["superseded_by"] == new["recipe_id"]
    listed = m.list_recipes(model="GLM 5.3 Flash")
    assert [row["id"] for row in listed["recipes"]] == [new["recipe_id"]]


def test_record_recipe_fit_warning_on_single_spark(settings, conn):
    seed(conn)
    m.upsert_model("qwen3-235b-a22b", family="qwen", params_total_b=235, params_active_b=22)
    result = m.record_recipe(
        model="qwen3-235b-a22b",
        topology="single-spark",
        status="unknown",
        evidence=[],
        quant="nvfp4",
    )
    assert result["fit"]["fit_ok"] == 0
    assert "exceeds" in result["fit"]["warning"]


def test_record_benchmark_verifies_quotes(settings, conn):
    seed(conn)
    ok = m.record_benchmark(
        model="GLM 5.3 Flash",
        benchmark="GPQA",
        score=71.5,
        source_type="forum",
        evidence=[{"post_id": 7001, "quote": QUOTE}],
    )
    assert ok["benchmark"] == "gpqa-diamond"
    assert ok["review"] == "accepted"

    bad = m.record_benchmark(
        model="GLM 5.3 Flash",
        benchmark="gpqa",
        score=80.0,
        source_type="forum",
        evidence=[{"post_id": 7001, "quote": "invented"}],
    )
    assert bad["review"] == "proposed"


def test_review_proposed_and_set_review(settings, conn):
    seed(conn)
    m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": "nope"}],
    )
    proposed = m.review_proposed("recipe")
    assert proposed["count"] == 1
    target = proposed["items"][0]["id"]
    m.set_review("recipe", target, "accepted")
    assert m.review_proposed("recipe")["count"] == 0


def test_notes_and_flags(settings, conn):
    seed(conn)
    note = m.save_note(
        "serving GLM 5.3 Flash on dual-spark", "use vllm 0.11", model="GLM 5.3 Flash", as_of=PAST
    )
    notes = m.get_notes(model="GLM 5.3 Flash")
    assert notes["notes"][0]["id"] == note["note_id"]

    flag = m.flag("topic", "700", "recipe", "worth recording", priority=1)
    assert m.list_flags()["count"] == 1
    m.resolve_flag(flag["flag_id"], "recorded")
    assert m.list_flags()["count"] == 0


def test_model_summary(settings, conn):
    seed(conn)
    m.upsert_model("glm-5.3-flash", family="glm", params_total_b=355)
    m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": QUOTE}],
    )
    summary = m.model_summary("GLM 5.3 Flash")
    assert summary["model"] == "glm-5.3-flash"
    assert summary["metadata"]["params_total_b"] == 355
    assert summary["recipes"]
    assert summary["top_topics"][0]["topic_id"] == 700


def test_add_model_alias_and_upsert_model(settings, conn):
    m.add_model_alias("glm 5.3 flash fp8", "glm-5.3-flash")
    from sparkwatch import normalize

    assert normalize.canonical_model(conn, "glm 5.3 flash fp8") == ("glm-5.3-flash", False)
    row = m.upsert_model("new-model", family="glm", params_total_b=30)
    assert row["canonical"] == "new-model"


def test_get_posts_pagination_has_no_gaps_or_overlap(settings, conn):
    seed(conn)
    full = m.get_posts(700, max_chars=10_000)
    assert full["truncated"] is False
    chunk = m.get_posts(700, max_chars=40)
    assert chunk["truncated"] is True
    assert chunk["next"]["offset"] == 40

    collected = chunk["text"]
    token = chunk["next"]
    while token:
        page = m.get_posts(700, max_chars=40, next=token)
        collected += page["text"]
        token = page["next"]
    assert collected == full["text"]


def test_get_posts_unknown_topic_raises(settings, conn):
    with pytest.raises(ValueError):
        m.get_posts(424242)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM posts",
        "SELECT 1; DROP TABLE posts",
        "PRAGMA table_info(posts)",
        "UPDATE posts SET raw='x'",
        "ATTACH DATABASE 'x' AS y",
    ],
)
def test_sql_query_rejects_writes(settings, conn, sql):
    with pytest.raises(ValueError):
        m.sql_query(sql)


def test_sql_query_runs_select(settings, conn):
    seed(conn)
    result = m.sql_query("SELECT id, title FROM topics", limit=5)
    assert result["columns"] == ["id", "title"]
    assert result["rows"][0]["id"] == 700


def test_search_and_find_model_mentions(settings, conn):
    seed(conn)
    found = m.search_posts("nvfp4")
    assert found["count"] >= 1
    mentions = m.find_model_mentions("GLM 5.3 Flash")
    assert mentions["total_posts"] >= 1
    assert mentions["topics"][0]["topic_id"] == 700


def test_topic_overview_and_mark_topic(settings, conn):
    seed(conn)
    overview = m.get_topic_overview(700)
    assert overview["topic_id"] == 700
    assert overview["op"]["post_id"] == 7001
    m.mark_topic(700, False)
    row = conn.execute("SELECT relevance_override FROM topics WHERE id=700").fetchone()
    assert row["relevance_override"] == 0


def test_watchlist_tools(settings, conn):
    created = m.add_watch("Kimi K2", r"kimi[\s._-]*k2", kind="tracking")
    assert created["label"] == "Kimi K2"
    labels = [row["label"] for row in m.list_watchlist()["watchlist"]]
    assert "Kimi K2" in labels
    updated = m.update_watch("Kimi K2", active=False)
    assert updated["active"] == 0
    assert m.ack_watch_sighting("nope")["error"]
