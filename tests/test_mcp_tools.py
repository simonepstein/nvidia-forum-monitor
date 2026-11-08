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


def test_model_summary_mention_windows_use_post_dates(settings, conn):
    from conftest import iso_days_ago

    seed(conn)
    conn.execute(
        "UPDATE posts SET first_fetched_at=?, changed_at=? WHERE id IN (7001, 7002)",
        (iso_days_ago(1), iso_days_ago(1)),
    )
    conn.execute("UPDATE posts SET created_at=? WHERE id=7001", (iso_days_ago(3),))
    conn.execute("UPDATE posts SET created_at=? WHERE id=7002", (iso_days_ago(40),))

    summary = m.model_summary("GLM 5.3 Flash")
    assert summary["mentions"] == {"7d": 1, "30d": 1, "60d": 2}


def test_whats_new_for_applies_since_to_mentions(settings, conn):
    from conftest import iso_days_ago

    seed(conn)
    conn.execute("UPDATE posts SET created_at=? WHERE id=7001", (iso_days_ago(20),))
    conn.execute("UPDATE posts SET created_at=? WHERE id=7002", (iso_days_ago(1),))
    conn.execute(
        "UPDATE posts SET first_fetched_at=?, changed_at=? WHERE id IN (7001, 7002)",
        (iso_days_ago(1), iso_days_ago(1)),
    )

    fresh = m.whats_new_for("GLM 5.3 Flash", since=iso_days_ago(2))
    listed = [post["post_id"] for topic in fresh["mentions"]["topics"] for post in topic["posts"]]
    assert listed == [7002]
    assert fresh["mentions"]["total_posts"] == 1
    assert fresh["mentions"]["backfilled"] == 1

    everything = m.whats_new_for("GLM 5.3 Flash", since=iso_days_ago(60))
    assert everything["mentions"]["total_posts"] == 2
    assert everything["mentions"]["backfilled"] == 0


def test_whats_new_for_filters_watch_hits_by_post_date(settings, conn):
    from conftest import iso_days_ago

    from sparkwatch import watch

    seed(conn)
    conn.execute(
        "UPDATE posts SET created_at=?, first_fetched_at=?, changed_at=? WHERE id=7001",
        (iso_days_ago(1), iso_days_ago(1), iso_days_ago(1)),
    )
    watch.match_post(conn, 7001, 700, "GLM 5.3 Flash on two Sparks", QUOTE)

    fresh = m.whats_new_for("GLM 5.3 Flash", since=iso_days_ago(2))
    assert fresh["watch"] and fresh["watch"][0]["label"] == "GLM 5.3 Flash"
    assert len(fresh["watch"][0]["hits"]) == 1
    assert (
        fresh["watch"][0]["hits"][0]["date"]
        == conn.execute("SELECT created_at FROM posts WHERE id=7001").fetchone()[0]
    )

    later = m.whats_new_for("GLM 5.3 Flash", since=iso_days_ago(0.5))
    assert later["watch"] == []


def test_list_watchlist_windows_use_post_dates(settings, conn):
    from conftest import iso_days_ago

    from sparkwatch import watch

    seed(conn)
    conn.execute("UPDATE posts SET created_at=? WHERE id=7001", (iso_days_ago(2),))
    conn.execute("UPDATE posts SET created_at=? WHERE id=7002", (iso_days_ago(20),))
    watch.match_post(conn, 7001, 700, "GLM 5.3 Flash on two Sparks", QUOTE)
    watch.match_post(conn, 7002, 700, "GLM 5.3 Flash on two Sparks", "fp8 instead")

    rows = {row["label"]: row for row in m.list_watchlist()["watchlist"]}
    assert rows["GLM 5.3 Flash"]["hits_7d"] == 1
    assert rows["GLM 5.3 Flash"]["hits_30d"] == 2
    assert (
        rows["GLM 5.3 Flash"]["latest_hit"]
        == conn.execute("SELECT created_at FROM posts WHERE id=7001").fetchone()[0]
    )


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


def test_search_posts_accepts_plain_model_names_and_units(settings, conn):
    seed(conn)
    for query in ("GLM 5.3 Flash", "glm 5.3 flash", "GLM-5.3-Flash"):
        result = m.search_posts(query)
        assert "error" not in result, query
        assert 7001 in [row["post_id"] for row in result["results"]], query

    for query in ("tok/s", "31 tok/s", "28 tok/s"):
        result = m.search_posts(query)
        assert "error" not in result, query
        assert result["count"] >= 1, query


def test_search_posts_quotes_terms_so_punctuation_is_safe(settings, conn):
    seed(conn)
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw, cooked,"
        " content_hash, first_fetched_at, changed_at)"
        " VALUES (7003, 700, 3, 1, 'carol', ?, ?, '<p>x</p>', 'h3', ?, ?)",
        (PAST, 'he said "nvfp4 works" - and it does', PAST, PAST),
    )
    result = m.search_posts('"nvfp4 works"')
    assert result["match"] == '"nvfp4" AND "works"'
    assert result["count"] >= 1
    assert "error" not in m.search_posts("fp8 - nvfp4")
    assert "error" not in m.search_posts("GLM 5.3 Flash (dual-spark)")


def test_search_posts_raw_fts_passes_operators_through(settings, conn):
    seed(conn)
    plain = m.search_posts("nvfp4 OR fp8")
    assert plain["count"] == 0
    assert plain["match"] == '"nvfp4" AND "OR" AND "fp8"'

    raw = m.search_posts("nvfp4 OR fp8", raw_fts=True)
    assert raw["count"] == 2
    assert raw["raw_fts"] is True

    broken = m.search_posts("GLM 5.3 Flash", raw_fts=True)
    assert "bad query" in broken["error"]


def test_search_posts_without_terms_is_a_clear_error(settings, conn):
    seed(conn)
    result = m.search_posts("   ")
    assert "no searchable terms" in result["error"]


def test_search_posts_hints_when_every_term_is_required(settings, conn):
    seed(conn)
    empty = m.search_posts("vllm or sglang")
    assert empty["count"] == 0
    assert "ANDed" in empty["hint"]
    assert "raw_fts=true" in empty["hint"]

    assert "hint" not in m.search_posts("nvfp4")


def test_sync_now_runs_the_sync_and_the_repo_check(settings, conn, router, forum, github):
    from conftest import listing, make_post, make_topic, topic_payload

    from sparkwatch.lock import write_lock
    from sparkwatch.util import utcnow_iso

    topic = make_topic(120, bumped_days_ago=1)
    posts = [make_post(1201, 120, 1, "dual spark glm 5.3 flash notes")]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)

    conn.execute(
        "INSERT INTO repos (owner, name, url, origin, watch, default_branch, head_sha,"
        " first_seen_at, watched_at, last_checked_at)"
        " VALUES ('o', 'n', 'https://github.com/o/n', 'agent', 1, 'main', ?, ?, ?, ?)",
        ("a" * 40, utcnow_iso(), utcnow_iso(), utcnow_iso()),
    )
    for path in ("/repos/o/n/commits/main", "/repos/o/n/releases/latest", "/repos/o/n/tags"):
        github.add(path, None, status=304)
    github.install(router)

    payload = m.sync_now()

    assert payload.get("busy") is not True
    assert payload["posts_new"] == 1
    assert payload["new_topics"][0]["topic_id"] == 120
    assert payload["repos"] == []
    assert payload["repos_checked"] == 1
    assert payload["repos_partial"] is False
    assert payload["repos_budget_s"] > 0

    with write_lock("sync", settings.db_path):
        busy = m.sync_now()
    assert busy["busy"] is True


def test_sync_now_skips_the_repo_check_when_the_budget_is_gone(
    settings, conn, router, forum, github
):
    from conftest import listing, make_post, make_topic, topic_payload

    from sparkwatch.util import utcnow_iso

    topic = make_topic(121, bumped_days_ago=1)
    posts = [make_post(1211, 121, 1, "post")]
    forum.add_page(723, listing([topic]))
    forum.add_topic(topic_payload(topic, posts), posts)
    forum.install(router)
    conn.execute(
        "INSERT INTO repos (owner, name, url, origin, watch, default_branch, head_sha,"
        " first_seen_at, watched_at, last_checked_at)"
        " VALUES ('o', 'n', 'https://github.com/o/n', 'agent', 1, 'main', ?, ?, ?, ?)",
        ("a" * 40, utcnow_iso(), utcnow_iso(), utcnow_iso()),
    )
    github.install(router)

    payload = m.sync_now(time_budget_s=0)

    assert payload["repos_checked"] == 0
    assert payload["repos_partial"] is True
    assert payload["repos"] == []


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
