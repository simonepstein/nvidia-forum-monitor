from __future__ import annotations


def add_topic(conn, topic_id: int, title: str) -> None:
    conn.execute("INSERT OR IGNORE INTO forums (id, path, name) VALUES (721, 'p', 'n')")
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, first_fetched_at, fetched_at)"
        " VALUES (?, 721, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (topic_id, title),
    )


def add_post(conn, post_id: int, topic_id: int, raw: str, post_number: int = 1) -> None:
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " first_fetched_at, changed_at, fetched_at)"
        " VALUES (?, ?, ?, 1, ?, ?, ?, '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (post_id, topic_id, post_number, raw, f"<p>{raw}</p>", f"hash-{post_id}-{len(raw)}"),
    )


def search(conn, query: str) -> list[int]:
    return [
        row[0]
        for row in conn.execute("SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?", (query,))
    ]


def test_inserted_post_is_searchable(conn):
    add_topic(conn, 1, "Serving GLM on two Sparks")
    add_post(conn, 10, 1, "we run nvfp4 with vllm and get 30 tok/s")
    assert 10 in search(conn, "nvfp4")
    assert 10 in search(conn, "vllm")
    assert 10 in search(conn, "tok")


def test_updated_post_reindexes(conn):
    add_topic(conn, 2, "Quant notes")
    add_post(conn, 20, 2, "nvfp4 works fine here")
    assert 20 in search(conn, "nvfp4")

    conn.execute("UPDATE posts SET raw='fp8 works fine here' WHERE id=20")
    assert 20 in search(conn, "fp8")
    assert search(conn, "nvfp4") == []


def test_deleted_post_leaves_the_index(conn):
    add_topic(conn, 3, "Deleted")
    add_post(conn, 30, 3, "temporary note about tensor parallel")
    assert 30 in search(conn, "tensor")
    conn.execute("DELETE FROM posts WHERE id=30")
    assert search(conn, "tensor") == []


def test_topic_title_is_indexed_and_updated(conn):
    add_topic(conn, 4, "DeepSeek V4.1 Flash benchmarks")
    add_post(conn, 40, 4, "some body text")
    assert 40 in search(conn, "title:deepseek")

    conn.execute("UPDATE topics SET title='Qwen3.8 Flash Next benchmarks' WHERE id=4")
    assert 40 in search(conn, "title:qwen3")
    assert search(conn, "title:deepseek") == []


def test_search_is_stemmed(conn):
    add_topic(conn, 5, "Benchmarking")
    add_post(conn, 50, 5, "we benchmarked the cluster")
    assert 50 in search(conn, "benchmark")
