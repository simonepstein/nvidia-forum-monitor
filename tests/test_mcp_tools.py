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


def test_record_recipe_records_fp4_as_its_own_quant(settings, conn):
    seed(conn)
    m.upsert_model("glm-5.3-flash", family="glm", params_total_b=355)
    result = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[],
        quant="fp4",
    )
    assert result["quant"] == "fp4"
    assert result["fit"]["estimate_gb"] is not None
    row = conn.execute(
        "SELECT quant_format FROM recipes WHERE id=?", (result["recipe_id"],)
    ).fetchone()
    assert row["quant_format"] == "fp4"


def test_record_recipe_keeps_the_raw_engine_searchable(settings, conn):
    seed(conn)
    first = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[],
        engine="vLLM nightly 2026-11-05",
    )
    m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[],
        engine="SGLang",
    )
    row = conn.execute(
        "SELECT engine, engine_raw FROM recipes WHERE id=?", (first["recipe_id"],)
    ).fetchone()
    assert row["engine"] == "vllm"
    assert row["engine_raw"] == "vLLM nightly 2026-11-05"

    by_raw = m.list_recipes(engine="vLLM nightly")
    assert [recipe["id"] for recipe in by_raw["recipes"]] == [first["recipe_id"]]
    assert len(m.list_recipes(engine="vllm")["recipes"]) == 1
    assert len(m.list_recipes(engine="sglang")["recipes"]) == 1
    raw_only = m.list_recipes(engine="nightly 2026-11-05")
    assert [recipe["id"] for recipe in raw_only["recipes"]] == [first["recipe_id"]]


def test_record_recipe_hints_when_fit_is_unknown(settings, conn):
    seed(conn)
    result = m.record_recipe(
        model="Brand New 900B",
        topology="dual-spark",
        status="works",
        evidence=[],
        quant="fp8",
    )
    assert result["fit"]["estimate_gb"] is None
    assert result["fit"]["fit_ok"] is None
    assert "fit unknown" in result["fit"]["hint"]
    assert "upsert_model" in result["fit"]["hint"]


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


def test_save_note_does_not_create_junk_models(settings, conn):
    seed(conn)
    before = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]

    note = m.save_note("a note", "body", model="Totally Unknown 900B", as_of=PAST)

    assert note["model"] is None
    assert "upsert_model" in note["hint"]
    assert conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] == before
    assert (
        conn.execute(
            "SELECT model_canonical FROM research_notes WHERE id=?", (note["note_id"],)
        ).fetchone()[0]
        is None
    )

    known = m.save_note("another note", "body", model="GLM 5.3 Flash", as_of=PAST)
    assert known["model"] == "glm-5.3-flash"
    assert "hint" not in known


def test_save_note_normalises_as_of(settings, conn):
    seed(conn)

    note = m.save_note("a note", "body", as_of="2026-11-08")
    assert note["as_of"] == "2026-11-08T00:00:00+00:00"
    assert (
        conn.execute("SELECT as_of FROM research_notes WHERE id=?", (note["note_id"],)).fetchone()[
            0
        ]
        == "2026-11-08T00:00:00+00:00"
    )

    offset = m.save_note("another note", "body", as_of="2026-11-08T13:00:00+02:00")
    assert offset["as_of"] == "2026-11-08T11:00:00+00:00"


def test_save_note_defaults_as_of_to_the_write_time(settings, conn):
    seed(conn)

    note = m.save_note("a note", "body")
    row = conn.execute(
        "SELECT as_of, created_at FROM research_notes WHERE id=?", (note["note_id"],)
    ).fetchone()

    assert note["as_of"] == row["as_of"] == row["created_at"]


def test_save_note_rejects_an_unparseable_as_of(settings, conn):
    seed(conn)
    before = conn.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0]

    with pytest.raises(ValueError, match="invalid as_of"):
        m.save_note("a note", "body", as_of="last tuesday")

    assert conn.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0] == before


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
    # 7002 only matches the topic title, so it is thread activity, not a mention.
    assert summary["mentions"] == {"7d": 1, "30d": 1, "60d": 1}
    thread = summary["threads"][0]
    assert thread["topic_id"] == 700
    assert thread["body_posts"] == 1
    assert thread["title_posts"] == 1


def test_model_summary_counts_only_body_mentions(settings, conn):
    from conftest import iso_days_ago

    recent = iso_days_ago(2)
    seed(conn)
    conn.execute("UPDATE posts SET created_at=? WHERE id IN (7001, 7002)", (recent,))
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw,"
        " cooked, content_hash, first_fetched_at, changed_at) VALUES (7003, 700, 3, 1, 'carol',"
        " ?, 'glm 5.3 flash again at 33 tok/s', '<p>x</p>', 'h3', ?, ?)",
        (recent, recent, recent),
    )
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, posts_count, like_count,"
        " relevance_score, first_fetched_at) VALUES (701, 723, 'GLM 5.3 Flash mega thread', ?, ?,"
        " 40, 2, 9.0, ?)",
        (recent, recent, recent),
    )
    for index, raw in enumerate(("unrelated chatter", "more chatter", "GLM 5.3 Flash is fast")):
        conn.execute(
            "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw,"
            " cooked, content_hash, first_fetched_at, changed_at) VALUES (?, 701, ?, 1, 'dave',"
            " ?, ?, '<p>x</p>', ?, ?, ?)",
            (7011 + index, index + 1, recent, raw, f"h{7011 + index}", recent, recent),
        )

    summary = m.model_summary("GLM 5.3 Flash")
    # 7002 only matches its topic title, so it is a reply, not a mention.
    assert summary["mentions"] == {"7d": 3, "30d": 3, "60d": 3}
    assert [topic["topic_id"] for topic in summary["top_topics"]] == [700, 701]
    threads = {thread["topic_id"]: thread for thread in summary["threads"]}
    assert threads[700]["body_posts"] == 2
    assert threads[700]["title_posts"] == 1
    assert threads[701]["body_posts"] == 1
    assert threads[701]["title_posts"] == 2


def test_model_summary_repos_are_ranked_and_capped(settings, conn):
    seed(conn)
    for index in range(14):
        conn.execute(
            "INSERT INTO repos (id, owner, name, url, watch, first_seen_at, head_sha)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                100 + index,
                f"owner{index}",
                f"repo{index}",
                f"https://github.com/owner{index}/repo{index}",
                1 if index == 13 else 0,
                PAST,
                "a" * 40,
            ),
        )
        conn.execute(
            "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, mentioned_at)"
            " VALUES (?, 7001, 700, ?, ?)",
            (100 + index, f"https://github.com/owner{index}/repo{index}", PAST),
        )
    conn.execute(
        "INSERT INTO repo_installs (repo_id, label, sha, installed_at, updated_at)"
        " VALUES (113, 'default', ?, ?, ?)",
        ("b" * 40, PAST, PAST),
    )
    conn.execute(
        "INSERT INTO compare_cache (repo_id, base_sha, head_sha, response, fetched_at)"
        " VALUES (113, ?, ?, ?, ?)",
        ("b" * 40, "a" * 40, '{"status": "ahead", "ahead_by": 2, "behind_by": 0}', PAST),
    )

    summary = m.model_summary("GLM 5.3 Flash")

    repos = summary["repos"]
    assert summary["repos_total"] == 14
    assert len(repos) == m.REPO_CAP == 10
    assert repos[0]["repo"] == "owner13/repo13"
    assert repos[0]["watched"] is True
    assert repos[0]["installed"]["commits_behind"] == 2
    assert all("mentions" in entry for entry in repos)


def test_model_summary_is_small(settings, conn):
    import json

    seed(conn)
    for index in range(20):
        conn.execute(
            "INSERT INTO repos (id, owner, name, url, watch, first_seen_at)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (
                200 + index,
                f"o{index}",
                f"r{index}",
                f"https://github.com/o{index}/r{index}",
                PAST,
            ),
        )
        conn.execute(
            "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, mentioned_at)"
            " VALUES (?, 7001, 700, ?, ?)",
            (200 + index, f"https://github.com/o{index}/r{index}", PAST),
        )
    size = len(json.dumps(m.model_summary("GLM 5.3 Flash")))
    assert size < 10_000, size


def test_model_summary_includes_flags_on_its_topics(settings, conn):
    seed(conn)
    recipe = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": QUOTE}],
    )
    m.flag("topic", "700", "perf-claim", "60 tok/s claim vs a 26 tok/s report", priority=1)
    m.flag("recipe", str(recipe["recipe_id"]), "recipe", "quant unclear", priority=2)
    m.flag("model", "glm-5.3-flash", "new-model", "watch this one", priority=3)
    m.flag("topic", "999", "perf-claim", "unrelated topic", priority=1)

    summary = m.model_summary("GLM 5.3 Flash")

    labels = {flag["label"] for flag in summary["flags"]}
    assert labels == {"perf-claim", "recipe", "new-model"}
    assert all(flag["target_id"] != "999" for flag in summary["flags"])


def test_whats_new_for_applies_since_to_mentions(settings, conn):
    from conftest import iso_days_ago

    seed(conn)
    conn.execute("UPDATE posts SET raw='GLM 5.3 Flash at 28 tok/s with fp8' WHERE id=7002")
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
    # 7001 mentions GLM in its body; 7002 only matches the topic title, so it is a reply.
    assert rows["GLM 5.3 Flash"]["hits_7d"] == 1
    assert rows["GLM 5.3 Flash"]["hits_30d"] == 1
    assert rows["GLM 5.3 Flash"]["replies_7d"] == 0
    assert rows["GLM 5.3 Flash"]["replies_30d"] == 1
    assert (
        rows["GLM 5.3 Flash"]["latest_hit"]
        == conn.execute("SELECT created_at FROM posts WHERE id=7001").fetchone()[0]
    )
    assert (
        rows["GLM 5.3 Flash"]["latest_reply"]
        == conn.execute("SELECT created_at FROM posts WHERE id=7002").fetchone()[0]
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
    assert 0 < chunk["next"]["offset"] <= 40
    assert chunk["text"].endswith(("\n", " "))

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


def test_get_posts_pages_break_at_word_and_line_boundaries(settings, conn):
    seed(conn)
    conn.execute("UPDATE posts SET raw=? WHERE id=7001", ("word " * 60,))
    conn.execute("UPDATE posts SET raw=? WHERE id=7002", ("alpha beta gamma " * 20,))

    page = m.get_posts(700, max_chars=50)
    assert page["truncated"] is True
    assert page["text"].endswith(("\n", " "))
    assert page["topic_total"] == 2
    assert page["selected_total"] == 2
    assert page["post_count"] >= 1

    last = m.get_posts(700, last_n=1)
    assert last["selected_total"] == 1
    assert last["topic_total"] == 2

    collected = ""
    token = None
    pages = 0
    while True:
        chunk = m.get_posts(700, max_chars=50, next=token)
        collected += chunk["text"]
        pages += 1
        if not chunk["next"]:
            break
        token = chunk["next"]
    assert pages > 2
    assert collected == m.get_posts(700, max_chars=100_000)["text"]
    assert "wordalpha" not in collected.replace(" ", "").replace("\n", "")


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


def test_find_model_mentions_splits_threads_from_mentions(settings, conn):
    seed(conn)
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, posts_count, first_fetched_at)"
        " VALUES (702, 723, 'GLM 5.3 Flash release thread', ?, 20, ?)",
        (PAST, PAST),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw,"
        " cooked, content_hash, first_fetched_at, changed_at) VALUES (7021, 702, 1, 1, 'eve',"
        " ?, 'opening post with no model name', '<p>x</p>', 'h7021', ?, ?)",
        (PAST, PAST, PAST),
    )

    found = m.find_model_mentions("GLM 5.3 Flash")

    # 7002 and 7021 only match a topic title, so they are replies, not mentions.
    assert found["total_posts"] == 1
    assert [topic["topic_id"] for topic in found["topics"]] == [700]
    threads = {thread["topic_id"]: thread for thread in found["threads"]}
    assert threads[700]["replies"] == 1
    assert threads[702]["replies"] == 1
    assert threads[702]["latest"][0]["snippet"].startswith("opening post")


def test_whats_new_for_includes_thread_activity(settings, conn):
    from conftest import iso_days_ago

    from sparkwatch import watch

    seed(conn)
    recent = iso_days_ago(1)
    conn.execute(
        "UPDATE posts SET created_at=?, first_fetched_at=?, changed_at=? WHERE id IN (7001, 7002)",
        (recent, recent, recent),
    )
    watch.match_post(conn, 7001, 700, "GLM 5.3 Flash on two Sparks", QUOTE)
    watch.match_post(conn, 7002, 700, "GLM 5.3 Flash on two Sparks", "fp8 instead")

    payload = m.whats_new_for("GLM 5.3 Flash", since=iso_days_ago(2))

    row = payload["watch"][0]
    assert row["total"] == 1
    assert row["threads"][0]["topic_id"] == 700
    assert row["threads"][0]["new_replies"] == 1
    assert payload["mentions"]["threads"][0]["topic_id"] == 700


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
    assert result["match"] == '"nvfp4 works"'
    assert result["count"] >= 1
    assert "error" not in m.search_posts("fp8 - nvfp4")
    assert "error" not in m.search_posts("GLM 5.3 Flash (dual-spark)")


def test_search_posts_keeps_a_quoted_phrase_whole(settings, conn):
    seed(conn)
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw, cooked,"
        " content_hash, first_fetched_at, changed_at)"
        " VALUES (7004, 700, 4, 1, 'dave', ?, ?, '<p>x</p>', 'h4', ?, ?)",
        (PAST, "tensor parallel across the pair, not pipeline parallel", PAST, PAST),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw, cooked,"
        " content_hash, first_fetched_at, changed_at)"
        " VALUES (7005, 700, 5, 1, 'erin', ?, ?, '<p>x</p>', 'h5', ?, ?)",
        (PAST, "tensor and parallel appear apart here", PAST, PAST),
    )

    phrase = m.search_posts('"tensor parallel"')
    assert phrase["match"] == '"tensor parallel"'
    assert [row["post_id"] for row in phrase["results"]] == [7004]

    loose = m.search_posts("tensor parallel")
    assert loose["match"] == '"tensor" AND "parallel"'
    assert {row["post_id"] for row in loose["results"]} == {7004, 7005}


def test_search_posts_raw_fts_passes_operators_through(settings, conn):
    seed(conn)
    plain = m.search_posts("nvfp4 OR fp8")
    assert plain["count"] == 0
    assert plain["match"] == '"nvfp4" AND "OR" AND "fp8"'
    assert "OR" in plain["hint"]

    raw = m.search_posts("nvfp4 OR fp8", raw_fts=True)
    assert raw["count"] == 2
    assert raw["raw_fts"] is True
    assert "hint" not in raw

    with pytest.raises(ValueError, match="bad query"):
        m.search_posts("GLM 5.3 Flash", raw_fts=True)


def test_search_posts_without_terms_is_a_tool_error(settings, conn):
    seed(conn)
    with pytest.raises(ValueError, match="no searchable terms"):
        m.search_posts("   ")


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


def test_refresh_engagement_tool_updates_likes(settings, conn, router, forum):
    import hashlib

    from conftest import make_post

    from sparkwatch.lock import write_lock

    post = make_post(2601, 260, 1, "post", extra={"reaction_users_count": 6})
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, posts_count,"
        " highest_post_number, relevance_score, first_fetched_at)"
        " VALUES (260, 723, 't', ?, ?, 1, 1, 0, ?)",
        (post["created_at"], post["created_at"], post["created_at"]),
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw, cooked,"
        " content_hash, like_count, accepted_answer, first_fetched_at, changed_at, fetched_at)"
        " VALUES (2601, 260, 1, 1, 'alice', ?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (
            post["created_at"],
            post["raw"],
            post["cooked"],
            hashlib.sha256(post["raw"].encode()).hexdigest(),
            PAST,
            PAST,
            PAST,
        ),
    )
    forum.add_posts(260, [post])
    forum.install(router)

    payload = m.refresh_engagement(days=14, forum_id=723)

    assert payload["posts_checked"] == 1
    assert payload["likes_changed"] == 1
    assert conn.execute("SELECT like_count FROM posts WHERE id=2601").fetchone()[0] == 6

    with write_lock("engagement", settings.db_path):
        busy = m.refresh_engagement()
    assert busy["busy"] is True


def test_refresh_engagement_is_described_as_user_requested_only():
    from sparkwatch import mcp_server as m

    assert "only when the user asks" in m.refresh_engagement.__doc__.lower()


def test_topic_overview_and_mark_topic(settings, conn):
    seed(conn)
    overview = m.get_topic_overview(700)
    assert overview["topic_id"] == 700
    assert overview["op"]["post_id"] == 7001
    m.mark_topic(700, False)
    row = conn.execute("SELECT relevance_override FROM topics WHERE id=700").fetchone()
    assert row["relevance_override"] == 0


def test_topic_overview_groups_watch_hits_per_label(settings, conn):
    from sparkwatch import watch

    seed(conn)
    conn.execute("UPDATE posts SET created_at=? WHERE id IN (7001, 7002)", (PAST,))
    watch.match_post(conn, 7001, 700, "GLM 5.3 Flash on two Sparks", QUOTE)
    watch.match_post(conn, 7002, 700, "GLM 5.3 Flash on two Sparks", "fp8 instead")

    overview = m.get_topic_overview(700)

    hits = overview["watch_hits"]
    assert len(hits) == 1
    assert hits[0]["label"] == "GLM 5.3 Flash"
    # 7001 mentions the model in its body; 7002 only matches the topic title.
    assert hits[0]["count"] == 1
    assert hits[0]["replies"] == 1
    assert hits[0]["latest"]["url"].startswith("https://forums.developer.nvidia.com/t/700/")
    assert hits[0]["latest"]["date"] == PAST
    assert hits[0]["latest_reply"]["url"].endswith("/t/700/2")


def test_digest_description_mentions_open_state(settings, conn):
    description = m.digest.__doc__ or ""
    assert "open state" in description
    assert "never empty" in description


def test_watchlist_tools(settings, conn):
    created = m.add_watch("Kimi K2", r"kimi[\s._-]*k2", kind="tracking")
    assert created["label"] == "Kimi K2"
    labels = [row["label"] for row in m.list_watchlist()["watchlist"]]
    assert "Kimi K2" in labels
    updated = m.update_watch("Kimi K2", active=False)
    assert updated["active"] == 0
    assert m.ack_watch_sighting("nope")["error"]
