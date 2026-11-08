from __future__ import annotations

from sparkwatch import mcp_server as m
from sparkwatch.digest import build_digest

PAST = "2026-01-01T00:00:00+00:00"
FORUM = "https://forums.developer.nvidia.com"


def seed(conn) -> None:
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (723, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, posts_count, like_count,"
        " relevance_score, first_fetched_at) VALUES (700, 723, 'GLM 5.3 Flash on two Sparks',"
        " ?, ?, 2, 5, 12.5, ?)",
        (PAST, PAST, PAST),
    )
    for number in (1, 2):
        conn.execute(
            "INSERT INTO posts (id, topic_id, post_number, post_type, username, created_at, raw,"
            " cooked, content_hash, first_fetched_at, changed_at)"
            " VALUES (?, 700, ?, 1, 'alice', ?, ?, '<p>x</p>', ?, ?, ?)",
            (
                7000 + number,
                number,
                PAST,
                f"post {number} about two sparks",
                f"h{number}",
                PAST,
                PAST,
            ),
        )
    conn.execute(
        "INSERT INTO repos (id, owner, name, url, first_seen_at) VALUES"
        " (1, 'gilesco', 'glm-5.3-flash-exl3', 'https://github.com/gilesco/glm-5.3-flash-exl3', ?)",
        (PAST,),
    )
    conn.execute(
        "INSERT INTO hf_models (id, repo_id, first_seen_at) VALUES (1, 'nvidia/DeepSeek-V4.1-Flash', ?)",
        (PAST,),
    )


def test_every_flag_target_type_gets_its_url(settings, conn):
    seed(conn)
    recipe = m.record_recipe(
        model="GLM 5.3 Flash",
        topology="dual-spark",
        status="works",
        evidence=[{"post_id": 7001, "quote": "post 1 about two sparks"}],
    )
    m.flag("topic", "700", "perf-claim", "60 tok/s claim", priority=1)
    m.flag("post", "7002", "recipe", "worth recording")
    m.flag("repo", "gilesco/glm-5.3-flash-exl3", "breaking-change", "new quant layout")
    m.flag("hf_model", "nvidia/DeepSeek-V4.1-Flash", "new-model", "weights are out")
    m.flag("recipe", str(recipe["recipe_id"]), "recipe", "quant unclear")

    by_type = {flag["target_type"]: flag for flag in m.list_flags()["flags"]}

    assert by_type["topic"]["url"] == f"{FORUM}/t/700"
    assert by_type["topic"]["title"] == "GLM 5.3 Flash on two Sparks"
    assert by_type["post"]["url"] == f"{FORUM}/t/700/2"
    assert by_type["repo"]["url"] == "https://github.com/gilesco/glm-5.3-flash-exl3"
    assert by_type["hf_model"]["url"] == "https://huggingface.co/nvidia/DeepSeek-V4.1-Flash"
    assert by_type["recipe"]["url"] == f"{FORUM}/t/700/1"


def test_model_flag_has_no_url_but_names_the_canonical_model(settings, conn):
    seed(conn)
    m.flag("model", "glm-5.3-flash", "new-model", "watch this one")

    flag = m.list_flags()["flags"][0]

    assert flag["url"] is None
    assert flag["model"] == "glm-5.3-flash"
    assert "target_missing" not in flag


def test_deleted_target_gets_a_null_url_and_target_missing(settings, conn):
    seed(conn)
    m.flag("topic", "999", "perf-claim", "gone")
    m.flag("post", "9999999", "perf-claim", "gone")
    m.flag("recipe", "424242", "recipe", "gone")

    flags = m.list_flags()["flags"]

    assert len(flags) == 3
    for flag in flags:
        assert flag["url"] is None
        assert flag["target_missing"] is True


def test_get_notes_returns_sources_with_urls_and_dates(settings, conn):
    seed(conn)
    m.save_note(
        subject="serving GLM 5.3 Flash on two Sparks",
        body="Two Sparks, nvfp4, vLLM.",
        source_post_ids=[7001, 7002],
    )

    notes = m.get_notes()["notes"]

    assert len(notes) == 1
    assert notes[0]["sources"] == [
        {"post_id": 7001, "url": f"{FORUM}/t/700/1", "date": PAST},
        {"post_id": 7002, "url": f"{FORUM}/t/700/2", "date": PAST},
    ]


def test_a_note_without_sources_returns_an_empty_list(settings, conn):
    seed(conn)
    m.save_note(subject="no sources", body="nothing cited")

    assert m.get_notes()["notes"][0]["sources"] == []


def test_digest_open_flags_carry_urls(settings, conn):
    seed(conn)
    m.flag("topic", "700", "perf-claim", "60 tok/s claim")

    digest = build_digest(conn, settings, since=PAST, advance=False)

    flag = digest["sections"]["open_flags"][0]
    assert flag["url"] == f"{FORUM}/t/700"
    assert flag["title"] == "GLM 5.3 Flash on two Sparks"


def test_topic_overview_and_model_summary_carry_flag_urls(settings, conn):
    seed(conn)
    conn.execute("UPDATE posts SET raw='GLM 5.3 Flash at 31 tok/s on two Sparks' WHERE id=7001")
    m.upsert_model("glm-5.3-flash", family="glm")
    m.flag("topic", "700", "perf-claim", "60 tok/s claim")

    overview = m.get_topic_overview(700)
    summary = m.model_summary("GLM 5.3 Flash")

    assert overview["flags"][0]["url"] == f"{FORUM}/t/700"
    assert summary["flags"][0]["url"] == f"{FORUM}/t/700"
