"""Build a frozen sparkwatch database from the recorded fixtures.

The eval harness needs a stable database with no network access:

    uv run python tests/evals/build_db.py

It replays the recorded Discourse fixtures straight into SQLite (no HTTP) and runs link
discovery, watch matching and relevance scoring, so the resulting database looks exactly
like one produced by a real sync.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
DEFAULT_DB = Path(__file__).resolve().parent / "frozen.db"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def main() -> int:
    os.environ.setdefault("SPARKWATCH_GENERAL__DB_PATH", str(DEFAULT_DB))
    sys.path.insert(0, str(ROOT / "src"))

    from sparkwatch import filters, links, watch
    from sparkwatch.config import get_settings, reset_settings
    from sparkwatch.db import open_db
    from sparkwatch.sync import _upsert_post, _upsert_topic
    from sparkwatch.util import utcnow_iso

    reset_settings()
    settings = get_settings()
    db_path = Path(settings.db_path)
    if db_path.exists():
        db_path.unlink()
    conn = open_db(settings)
    now = utcnow_iso()

    for category in settings.forum.categories:
        listing = load(f"category_{category.id}.json")
        for topic in listing["topic_list"]["topics"]:
            _upsert_topic(conn, category.id, topic, backfill=False)

    for topic_file in sorted(FIXTURES.glob("topic_*.json")):
        topic = load(topic_file.name)
        topic_id = int(topic["id"])
        for post in topic["post_stream"]["posts"]:
            _upsert_post(conn, topic_id, post, now)

    for batch_file in sorted(FIXTURES.glob("posts_*.json")):
        topic_id = int(batch_file.name.split("_")[1])
        payload = load(batch_file.name)
        for post in payload["post_stream"]["posts"]:
            outcome = _upsert_post(conn, topic_id, post, now)
            if outcome != "same":
                row = conn.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
                links.discover(conn, row)
                title = conn.execute("SELECT title FROM topics WHERE id=?", (topic_id,)).fetchone()
                watch.match_post(
                    conn, post["id"], topic_id, title["title"] if title else None, row["raw"]
                )

    for row in conn.execute("SELECT id FROM topics").fetchall():
        filters.recompute_topic(conn, row["id"], settings)

    conn.execute(
        "INSERT INTO digest_cursors (name, last_seen_at) VALUES ('default', ?)"
        " ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        ("2000-01-01T00:00:00+00:00",),
    )

    topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    print(f"built {db_path} with {topics} topics and {posts} posts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
