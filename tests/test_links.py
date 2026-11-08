from __future__ import annotations

import pytest

from sparkwatch import links


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Foo/Bar.git/tree/main#x", "https://github.com/foo/bar"),
        (
            "https://github.com/vllm-project/vllm/releases/tag/v0.11.0",
            "https://github.com/vllm-project/vllm",
        ),
        ("http://github.com/a/b?tab=readme", "https://github.com/a/b"),
        ("https://github.com/orgs/foo", None),
        ("https://github.com/features/copilot", None),
        ("https://github.com/sponsors/x", None),
        ("https://example.com/a/b", None),
        ("https://gist.github.com/alice/abc123", None),
        ("https://gist.github.com/alice/abc123#file-notes", None),
    ],
)
def test_github_normalisation(url, expected):
    assert links.normalize_github(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://huggingface.co/Qwen/Qwen3.8-Flash-Next-NVFP4",
            "https://huggingface.co/qwen/Qwen3.8-Flash-Next-NVFP4",
        ),
        ("https://huggingface.co/datasets/foo/bar", None),
        ("https://huggingface.co/spaces/foo/bar", None),
        ("https://huggingface.co/blog/foo", None),
    ],
)
def test_hf_normalisation(url, expected):
    assert links.normalize_hf(url) == expected


def _post(raw: str, cooked: str | None = None) -> dict:
    return {"id": 1, "topic_id": 2, "raw": raw, "cooked": cooked or f"<p>{raw}</p>"}


def test_discover_records_repos_without_watching(conn):
    raw = (
        "we serve from https://github.com/vllm-project/vllm and the quant at "
        "https://huggingface.co/qwen/qwen3.8-flash-next-nvfp4 for two sparks"
    )
    conn.execute(
        "INSERT INTO forums (id, path, name) VALUES (723, 'p', 'n') ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, first_fetched_at) VALUES (2, 723, 't', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " first_fetched_at, changed_at) VALUES (1, 2, 1, 1, ?, '<p>x</p>', 'h',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (raw,),
    )
    row = conn.execute("SELECT * FROM posts WHERE id=1").fetchone()

    found = links.discover(conn, row)
    assert found["repos"] == ["https://github.com/vllm-project/vllm"]
    assert found["hf_models"] == ["https://huggingface.co/qwen/qwen3.8-flash-next-nvfp4"]

    repo = conn.execute(
        "SELECT * FROM repos WHERE url='https://github.com/vllm-project/vllm'"
    ).fetchone()
    assert repo["watch"] == 0
    assert repo["origin"] == "forum"

    mention = conn.execute("SELECT * FROM repo_mentions WHERE repo_id=?", (repo["id"],)).fetchone()
    assert "vllm-project/vllm" in mention["context_snippet"]
    assert len(mention["context_snippet"]) <= 400


def test_discover_uses_cooked_anchors(conn):
    conn.execute(
        "INSERT INTO forums (id, path, name) VALUES (721, 'p', 'n') ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, first_fetched_at) VALUES (3, 721, 't', '2026-01-01T00:00:00+00:00')"
    )
    cooked = '<p>see <a href="https://github.com/sgl-project/sglang/pull/123">this PR</a></p>'
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " first_fetched_at, changed_at) VALUES (2, 3, 1, 1, 'see the PR', ?, 'h2',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (cooked,),
    )
    row = conn.execute("SELECT * FROM posts WHERE id=2").fetchone()
    found = links.discover(conn, row)
    assert found["repos"] == ["https://github.com/sgl-project/sglang"]


def test_discover_is_idempotent(conn):
    conn.execute(
        "INSERT INTO forums (id, path, name) VALUES (721, 'p', 'n') ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, first_fetched_at) VALUES (4, 721, 't', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " first_fetched_at, changed_at) VALUES (3, 4, 1, 1, 'https://github.com/a/b', '<p>x</p>', 'h3',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    row = conn.execute("SELECT * FROM posts WHERE id=3").fetchone()
    links.discover(conn, row)
    links.discover(conn, row)
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM repo_mentions").fetchone()[0] == 1
