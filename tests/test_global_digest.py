from __future__ import annotations

import json

from sparkwatch import digest as digest_module
from sparkwatch.digest import global_digest, render_global_digest

SINCE = "2026-01-01T00:00:00+00:00"
UNTIL = "2026-02-01T00:00:00+00:00"
IN_WINDOW = "2026-01-10T12:00:00+00:00"
BEFORE_GRACE = "2025-12-01T00:00:00+00:00"
OLD_FETCH = "2025-12-20T00:00:00+00:00"


def seed_topic(
    conn,
    topic_id: int,
    *,
    forum_id: int = 721,
    title: str = "topic",
    created_at: str = BEFORE_GRACE,
    relevance: float = 20.0,
    override: int | None = None,
) -> None:
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (?, 'p', 'n')", (forum_id,))
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, bumped_at, posts_count, like_count,"
        " relevance_score, relevance_override, first_fetched_at)"
        " VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)",
        (topic_id, forum_id, title, created_at, created_at, relevance, override, IN_WINDOW),
    )


def seed_post(
    conn,
    post_id: int,
    topic_id: int,
    *,
    created_at: str = IN_WINDOW,
    first_fetched_at: str | None = None,
    likes: int = 0,
    accepted: int = 0,
    staff: int = 0,
    username: str = "alice",
    raw: str = "a post about two sparks",
) -> None:
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, username, staff, created_at,"
        " raw, cooked, content_hash, like_count, accepted_answer, first_fetched_at, changed_at)"
        " VALUES (?, ?, ?, 1, ?, ?, ?, ?, '<p>x</p>', ?, ?, ?, ?, ?)",
        (
            post_id,
            topic_id,
            post_id % 1000,
            username,
            staff,
            created_at,
            raw,
            f"h{post_id}",
            likes,
            accepted,
            first_fetched_at or created_at,
            first_fetched_at or created_at,
        ),
    )


def test_components_add_up_to_the_score_and_factor_is_reported(settings, conn):
    seed_topic(conn, 1, relevance=20.0)
    seed_post(conn, 11, 1, likes=3, username="alice")
    seed_post(conn, 12, 1, likes=1, username="bob")

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    topic = payload["hot_topics"][0]
    assert topic["topic_id"] == 1
    assert topic["new_posts"] == 2
    assert topic["posters"] == 2
    assert topic["likes"] == 4
    assert topic["factor"] == 1.0
    assert topic["base"] == 4.356
    assert topic["score"] == 4.356
    assert topic["components"] == {
        "posts": 1.099,
        "posters": 1.648,
        "likes": 1.609,
        "new_topic": 0.0,
        "accepted": 0.0,
        "staff": 0.0,
    }
    assert round(sum(topic["components"].values()), 3) == topic["score"]


def test_every_topic_has_components_that_sum_to_its_score(settings, conn):
    seed_topic(conn, 1, relevance=5.0)
    seed_post(conn, 11, 1, likes=2, accepted=1, staff=1, username="alice")
    seed_post(conn, 12, 1, username="bob")
    seed_topic(conn, 2, forum_id=723, title="new question", created_at=IN_WINDOW, relevance=0.0)
    seed_post(conn, 21, 2, username="carol")
    seed_topic(conn, 3, relevance=80.0)
    seed_post(conn, 31, 3, likes=9, username="dave")

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    assert len(payload["hot_topics"]) == 3
    for topic in payload["hot_topics"]:
        assert round(sum(topic["components"].values()), 3) == topic["score"]
    scores = [topic["score"] for topic in payload["hot_topics"]]
    assert scores == sorted(scores, reverse=True)


def test_relevance_factor_doubles_as_an_off_topic_penalty(settings, conn):
    seed_topic(conn, 1, relevance=0.0)
    seed_post(conn, 11, 1)

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    topic = payload["hot_topics"][0]
    assert topic["factor"] == 0.5
    assert topic["relevant"] is False
    assert topic["score"] == round(topic["base"] * 0.5, 3)


def test_a_topic_marked_irrelevant_gets_factor_0_5_and_relevant_false(settings, conn):
    seed_topic(conn, 1, relevance=30.0, override=0)
    seed_post(conn, 11, 1, likes=2)

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)
    filtered = global_digest(
        conn, settings, since=SINCE, until=UNTIL, relevant_only=True, advance=False
    )

    topic = payload["hot_topics"][0]
    assert topic["factor"] == 0.5
    assert topic["relevant"] is False
    assert filtered["hot_topics"] == []


def test_relevance_cap_limits_the_factor(settings, conn):
    seed_topic(conn, 1, relevance=1000.0)
    seed_post(conn, 11, 1)

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    assert payload["hot_topics"][0]["factor"] == 1.5


def test_backfilled_and_edited_posts_are_not_hot(settings, conn):
    seed_topic(conn, 1)
    seed_post(conn, 11, 1)
    seed_topic(conn, 2)
    seed_post(conn, 21, 2, created_at=BEFORE_GRACE, first_fetched_at=IN_WINDOW)
    seed_topic(conn, 3)
    seed_post(conn, 31, 3, first_fetched_at=OLD_FETCH)

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    assert [topic["topic_id"] for topic in payload["hot_topics"]] == [1]
    assert payload["backfilled"] == {"posts": 1, "topics": 1}


def test_ties_break_on_newest_post_then_topic_id(settings, conn):
    seed_topic(conn, 5, relevance=10.0)
    seed_post(conn, 51, 5, created_at="2026-01-11T00:00:00+00:00")
    seed_topic(conn, 4, relevance=10.0)
    seed_post(conn, 41, 4, created_at="2026-01-11T00:00:00+00:00")
    seed_topic(conn, 6, relevance=10.0)
    seed_post(conn, 61, 6, created_at="2026-01-20T00:00:00+00:00")

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    assert [topic["topic_id"] for topic in payload["hot_topics"]] == [6, 4, 5]


def test_both_forums_are_included_and_forum_filter_narrows(settings, conn):
    seed_topic(conn, 1, forum_id=721, relevance=30.0)
    seed_post(conn, 11, 1)
    seed_topic(conn, 2, forum_id=723, relevance=10.0)
    seed_post(conn, 21, 2)

    both = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)
    only_723 = global_digest(conn, settings, since=SINCE, until=UNTIL, forum_id=723, advance=False)

    assert {topic["forum_id"] for topic in both["hot_topics"]} == {721, 723}
    assert [topic["topic_id"] for topic in only_723["hot_topics"]] == [2]


def test_the_same_window_gives_the_same_ranking(settings, conn):
    for topic_id in (1, 2, 3):
        seed_topic(conn, topic_id, relevance=10.0 * topic_id)
        seed_post(conn, topic_id * 10 + 1, topic_id, likes=topic_id)

    first = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)
    again = global_digest(conn, settings, since=first["since"], until=first["until"], advance=False)

    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_cursors_are_separate_and_advance_moves_only_global(settings, conn):
    seed_topic(conn, 1)
    seed_post(conn, 11, 1)
    conn.execute(
        "INSERT INTO digest_cursors (name, last_seen_at) VALUES ('default', ?)", (OLD_FETCH,)
    )

    global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)
    assert (
        conn.execute("SELECT COUNT(*) FROM digest_cursors WHERE name='global'").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT last_seen_at FROM digest_cursors WHERE name='default'").fetchone()[0]
        == OLD_FETCH
    )

    global_digest(conn, settings, since=SINCE, until=UNTIL, advance=True)
    assert (
        conn.execute("SELECT last_seen_at FROM digest_cursors WHERE name='global'").fetchone()[0]
        == UNTIL
    )
    assert (
        conn.execute("SELECT last_seen_at FROM digest_cursors WHERE name='default'").fetchone()[0]
        == OLD_FETCH
    )


def test_cursor_since_resolves_from_the_global_cursor(settings, conn):
    seed_topic(conn, 1)
    seed_post(conn, 11, 1)
    conn.execute("INSERT INTO digest_cursors (name, last_seen_at) VALUES ('global', ?)", (SINCE,))

    payload = global_digest(conn, settings, until=UNTIL, advance=False)

    assert payload["since"] == SINCE
    assert payload["cursor"] == "global"


def test_next_pages_without_gaps_or_overlaps(settings, conn):
    for topic_id in (1, 2, 3):
        seed_topic(conn, topic_id, relevance=10.0 * topic_id)
        seed_post(conn, topic_id * 10 + 1, topic_id, likes=topic_id)

    first = global_digest(conn, settings, since=SINCE, until=UNTIL, limit=2, advance=False)
    second = global_digest(conn, settings, next=first["next"], advance=False)

    first_ids = [topic["topic_id"] for topic in first["hot_topics"]]
    second_ids = [topic["topic_id"] for topic in second["hot_topics"]]
    assert first_ids == [3, 2]
    assert second_ids == [1]
    assert not set(first_ids) & set(second_ids)
    assert "next" not in second


def test_detail_adds_the_most_liked_new_posts(settings, conn):
    seed_topic(conn, 1)
    seed_post(conn, 11, 1, likes=1, raw="low", username="alice")
    seed_post(conn, 12, 1, likes=5, raw="high", username="bob")
    seed_post(conn, 13, 1, likes=3, raw="middle", username="carol")

    lean = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)
    detailed = global_digest(conn, settings, since=SINCE, until=UNTIL, detail=True, advance=False)

    assert lean["hot_topics"][0]["top_posts"] == []
    posts = detailed["hot_topics"][0]["top_posts"]
    assert [post["post_id"] for post in posts] == [12, 13]
    assert posts[0]["author"] == "bob"
    assert posts[0]["url"] == "https://forums.developer.nvidia.com/t/1/12"
    assert len(posts[0]["snippet"]) <= 160


def test_unanswered_first_linked_and_knowledge_sections(settings, conn):
    seed_topic(conn, 1, title="how do I serve GLM?", created_at=IN_WINDOW, relevance=3.0)
    seed_post(conn, 11, 1, username="alice")
    seed_topic(conn, 2, title="busy thread")
    seed_post(conn, 21, 2)
    seed_post(conn, 22, 2, username="bob")
    conn.execute(
        "INSERT INTO repos (id, owner, name, url, first_seen_at)"
        " VALUES (1, 'o', 'r', 'https://github.com/o/r', ?)",
        (IN_WINDOW,),
    )
    conn.execute(
        "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, mentioned_at)"
        " VALUES (1, 21, 2, 'https://github.com/o/r', ?)",
        (IN_WINDOW,),
    )
    conn.execute(
        "INSERT INTO research_notes (id, subject, model_canonical, body, as_of, created_at)"
        " VALUES (1, 'serving GLM', 'glm-5.3-flash', 'body', ?, ?)",
        (IN_WINDOW, IN_WINDOW),
    )
    conn.execute(
        "INSERT INTO evidence (target_type, target_id, post_id, quote, quote_verified)"
        " VALUES ('note', 1, 21, 'quote', 1)"
    )

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    assert payload["unanswered"]["count"] == 1
    assert payload["unanswered"]["top"][0]["url"] == "https://forums.developer.nvidia.com/t/1"
    assert payload["first_linked"]["count"] == 1
    assert payload["first_linked"]["top"][0]["url"] == "https://github.com/o/r"
    knowledge = payload["knowledge"]["items"]
    assert knowledge[0]["kind"] == "note"
    assert knowledge[0]["sources"] == [
        {
            "post_id": 21,
            "url": "https://forums.developer.nvidia.com/t/2/21",
            "date": IN_WINDOW,
        }
    ]


def test_default_response_stays_under_10_kb_on_a_busy_window(settings, conn):
    for topic_id in range(1, 31):
        seed_topic(
            conn,
            topic_id,
            forum_id=721 if topic_id % 2 else 723,
            title=f"a long topic title number {topic_id} about two sparks",
            relevance=float(topic_id),
        )
        for number in range(1, 5):
            seed_post(
                conn,
                topic_id * 100 + number,
                topic_id,
                likes=number,
                username=f"user{number}",
                raw="a long post body about vLLM, nvfp4 and tok/s on two Sparks " * 3,
            )
    for index in range(6):
        conn.execute(
            "INSERT INTO repos (id, owner, name, url, first_seen_at) VALUES (?, 'o', ?, ?, ?)",
            (index + 1, f"r{index}", f"https://github.com/o/r{index}", IN_WINDOW),
        )
        conn.execute(
            "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, mentioned_at)"
            " VALUES (?, ?, 1, ?, ?)",
            (index + 1, 101, f"https://github.com/o/r{index}", IN_WINDOW),
        )

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)
    detailed = global_digest(conn, settings, since=SINCE, until=UNTIL, detail=True, advance=False)

    assert len(payload["hot_topics"]) == 10
    assert len(json.dumps(payload, separators=(",", ":"))) < 10_000
    assert {topic["forum_id"] for topic in payload["hot_topics"]} == {721, 723}
    assert len(json.dumps(detailed, separators=(",", ":"))) > len(
        json.dumps(payload, separators=(",", ":"))
    )


def test_weights_come_from_config(settings, conn, monkeypatch):
    seed_topic(conn, 1, relevance=20.0)
    seed_post(conn, 11, 1, likes=4)

    from sparkwatch.config import GlobalDigestSettings

    tweaked = settings.model_copy(
        update={
            "global_digest": GlobalDigestSettings(
                weights={
                    "posts": 2.0,
                    "posters": 1.5,
                    "likes": 1.0,
                    "new_topic": 1.0,
                    "accepted": 1.0,
                    "staff": 0.5,
                },
                relevance_cap=40.0,
            )
        }
    )
    payload = global_digest(conn, tweaked, since=SINCE, until=UNTIL, advance=False)

    topic = payload["hot_topics"][0]
    assert topic["components"]["posts"] == round(digest_module.math.log1p(1) * 2.0, 3)
    assert payload["weights"]["posts"] == 2.0


def test_cli_global_digest_renders_text_and_json(settings, conn):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    seed_topic(conn, 1, title="GLM on two Sparks")
    seed_post(conn, 11, 1, likes=2)
    runner = CliRunner()

    text = runner.invoke(app, ["global-digest", "--since", SINCE, "--until", UNTIL, "--no-advance"])
    assert text.exit_code == 0
    assert "Hot topics (1/1)" in text.stdout
    assert "GLM on two Sparks" in text.stdout
    assert "1 new posts by 1 posters" in text.stdout
    assert "terms: posts +" in text.stdout
    assert "new_topic +" not in text.stdout

    as_json = runner.invoke(
        app,
        ["global-digest", "--since", SINCE, "--until", UNTIL, "--no-advance", "--json"],
    )
    assert as_json.exit_code == 0
    assert '"hot_topics"' in as_json.stdout
    assert '"components"' in as_json.stdout


def test_invalid_dates_raise_a_clear_error(settings, conn):
    import pytest

    seed_topic(conn, 1)
    seed_post(conn, 11, 1)

    with pytest.raises(ValueError, match="invalid since date"):
        global_digest(conn, settings, since="last week", advance=False)
    with pytest.raises(ValueError, match="invalid until date"):
        global_digest(conn, settings, since=SINCE, until="last week", advance=False)


def test_cli_rejects_a_bad_date_with_one_line(settings, conn):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    seed_topic(conn, 1)
    seed_post(conn, 11, 1)
    result = CliRunner().invoke(app, ["global-digest", "--since", SINCE, "--until", "last week"])

    assert result.exit_code == 1
    assert "invalid until date" in result.stdout
    assert "Traceback" not in result.stdout


def test_first_linked_ignores_links_with_no_in_window_mentions(settings, conn):
    seed_topic(conn, 1)
    seed_post(conn, 11, 1)
    seed_topic(conn, 2)
    seed_post(conn, 21, 2, created_at=BEFORE_GRACE, first_fetched_at=IN_WINDOW)
    conn.execute(
        "INSERT INTO repos (id, owner, name, url, first_seen_at)"
        " VALUES (1, 'o', 'old', 'https://github.com/o/old', ?)",
        (IN_WINDOW,),
    )
    conn.execute(
        "INSERT INTO repo_mentions (repo_id, post_id, topic_id, url, mentioned_at)"
        " VALUES (1, 21, 2, 'https://github.com/o/old', ?)",
        (IN_WINDOW,),
    )

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    assert payload["first_linked"] == {"count": 0, "top": []}


def test_hf_models_get_their_first_post_url(settings, conn):
    seed_topic(conn, 1)
    seed_post(conn, 11, 1)
    conn.execute(
        "INSERT INTO hf_models (id, repo_id, first_seen_at)"
        " VALUES (1, 'nvidia/DeepSeek-V4.1-Flash', ?)",
        (IN_WINDOW,),
    )
    conn.execute(
        "INSERT INTO hf_mentions (hf_model_id, post_id, topic_id, url, mentioned_at)"
        " VALUES (1, 11, 1, 'https://huggingface.co/nvidia/DeepSeek-V4.1-Flash', ?)",
        (IN_WINDOW,),
    )

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    item = payload["first_linked"]["top"][0]
    assert item["type"] == "hf_model"
    assert item["url"] == "https://huggingface.co/nvidia/DeepSeek-V4.1-Flash"
    assert item["first_post_url"] == "https://forums.developer.nvidia.com/t/1/11"


def test_repo_events_are_capped_with_a_total(settings, conn):
    seed_topic(conn, 1)
    seed_post(conn, 11, 1)
    conn.execute(
        "INSERT INTO repos (id, owner, name, url, watch, first_seen_at)"
        " VALUES (1, 'o', 'r', 'https://github.com/o/r', 1, ?)",
        (IN_WINDOW,),
    )
    for index in range(7):
        conn.execute(
            "INSERT INTO repo_events (repo_id, kind, ref, title, url, detected_at)"
            " VALUES (1, 'release', ?, ?, 'https://github.com/o/r/releases', ?)",
            (f"v{index}", f"release {index}", IN_WINDOW),
        )

    payload = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)

    assert payload["repo_events"]["count"] == 7
    assert len(payload["repo_events"]["top"]) == 5


def test_detail_renders_each_topics_posts_under_that_topic(settings, conn):
    seed_topic(conn, 1, title="GLM on two Sparks", relevance=40.0)
    seed_post(conn, 11, 1, likes=1, raw="quiet", username="alice")
    seed_post(conn, 12, 1, likes=5, raw="loud", username="bob")
    seed_topic(conn, 2, title="DeepSeek on two Sparks", relevance=20.0)
    seed_post(conn, 21, 2, likes=2, raw="calm", username="carol")
    seed_post(conn, 22, 2, likes=4, raw="brash", username="dave")

    detailed = global_digest(conn, settings, since=SINCE, until=UNTIL, detail=True, advance=False)
    lines = render_global_digest(detailed).splitlines()

    first_heading = next(
        i for i, line in enumerate(lines) if line.startswith("- ") and "GLM" in line
    )
    second_heading = next(
        i for i, line in enumerate(lines) if line.startswith("- ") and "DeepSeek" in line
    )
    post_lines = [(i, line) for i, line in enumerate(lines) if "·" in line]

    assert [line for _index, line in post_lines] == [
        f"    · 5 likes https://forums.developer.nvidia.com/t/1/12 {IN_WINDOW} — loud",
        f"    · 1 likes https://forums.developer.nvidia.com/t/1/11 {IN_WINDOW} — quiet",
        f"    · 4 likes https://forums.developer.nvidia.com/t/2/22 {IN_WINDOW} — brash",
        f"    · 2 likes https://forums.developer.nvidia.com/t/2/21 {IN_WINDOW} — calm",
    ]
    assert lines[first_heading + 1].strip().startswith("2 new posts by 2 posters")
    assert lines[first_heading + 2].strip().startswith("terms: ")
    assert first_heading + 2 < post_lines[0][0]
    assert post_lines[1][0] < second_heading < post_lines[2][0]

    lean = global_digest(conn, settings, since=SINCE, until=UNTIL, advance=False)
    assert "·" not in render_global_digest(lean)
