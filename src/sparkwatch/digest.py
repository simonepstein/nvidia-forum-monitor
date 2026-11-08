from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from .config import Settings, get_settings
from .util import parse_dt, to_iso, utcnow_iso

EPOCH = "1970-01-01T00:00:00+00:00"
FORUM_URL = "https://forums.developer.nvidia.com"
GRACE = timedelta(hours=24)

# The digest's three kinds of post (SPEC §5, "Timing"), written once and reused by
# whats_new_for and find_model_mentions. They take the named parameters :since and :grace.
NEW_POSTS = (
    "julianday(p.first_fetched_at) >= julianday(:since)"
    " AND julianday(p.created_at) >= julianday(:grace)"
)
EDITED_POSTS = (
    "julianday(p.changed_at) >= julianday(:since)"
    " AND julianday(p.first_fetched_at) < julianday(:since)"
)
BACKFILLED_POSTS = (
    "julianday(p.first_fetched_at) >= julianday(:since)"
    " AND julianday(p.created_at) < julianday(:grace)"
)


def grace_floor(since_iso: str) -> str:
    """The 24-hour grace before `since`: posts written shortly before a sync still count."""
    moment = parse_dt(since_iso)
    if moment is None:
        return since_iso
    return to_iso(moment - GRACE)


def window_params(since_iso: str) -> dict[str, str]:
    return {"since": since_iso, "grace": grace_floor(since_iso)}


def _post_url(topic_id: int, post_number: int | None) -> str:
    return f"{FORUM_URL}/t/{topic_id}/{post_number or 1}"


def _topic_url(topic_id: int, slug: str | None = None) -> str:
    return f"{FORUM_URL}/t/{topic_id}"


def _snippet(text: str | None, length: int = 300) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat[:length]


def resolve_since(conn: sqlite3.Connection, since: str, cursor: str) -> str:
    if since != "cursor":
        parsed = parse_dt(since)
        return to_iso(parsed) if parsed else since
    row = conn.execute("SELECT last_seen_at FROM digest_cursors WHERE name=?", (cursor,)).fetchone()
    return row["last_seen_at"] if row else EPOCH


def _sightings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT w.label, w.first_seen_post_id, w.first_seen_at, p.topic_id, p.post_number, t.title"
        " FROM watchlist w LEFT JOIN posts p ON p.id = w.first_seen_post_id"
        " LEFT JOIN topics t ON t.id = p.topic_id"
        " WHERE w.kind='anticipated' AND w.first_seen_post_id IS NOT NULL"
        " AND w.sighting_acked_at IS NULL ORDER BY w.first_seen_at DESC"
    ).fetchall()
    return [
        {
            "label": row["label"],
            "title": row["title"],
            "first_seen_at": row["first_seen_at"],
            "post_id": row["first_seen_post_id"],
            "url": _post_url(row["topic_id"], row["post_number"]) if row["topic_id"] else None,
        }
        for row in rows
    ]


def watch_activity(conn: sqlite3.Connection, since_iso: str, limit: int) -> list[dict[str, Any]]:
    params = window_params(since_iso)
    rows = conn.execute(
        "SELECT w.label, h.post_id, h.topic_id, h.matched_text, p.post_number, p.raw,"
        " p.created_at, t.title FROM watch_hits h JOIN watchlist w ON w.id = h.watch_id"
        " JOIN posts p ON p.id = h.post_id JOIN topics t ON t.id = h.topic_id"
        f" WHERE w.kind='tracking' AND ({NEW_POSTS})"
        " ORDER BY julianday(p.created_at) DESC LIMIT :limit",
        {**params, "limit": limit},
    ).fetchall()
    edited = {
        row["label"]: row["n"]
        for row in conn.execute(
            "SELECT w.label, COUNT(DISTINCT h.post_id) AS n FROM watch_hits h"
            " JOIN watchlist w ON w.id = h.watch_id JOIN posts p ON p.id = h.post_id"
            f" WHERE w.kind='tracking' AND ({EDITED_POSTS}) GROUP BY w.label",
            params,
        )
    }
    grouped: dict[str, list[dict[str, Any]]] = {label: [] for label in edited}
    for row in rows:
        grouped.setdefault(row["label"], []).append(
            {
                "title": row["title"],
                "url": _post_url(row["topic_id"], row["post_number"]),
                "matched": row["matched_text"],
                "date": row["created_at"],
                "snippet": _snippet(row["raw"]),
            }
        )
    return [
        {"label": label, "hits": hits, "edits": edited.get(label, 0)}
        for label, hits in grouped.items()
        if hits or edited.get(label)
    ]


def _new_topics(
    conn: sqlite3.Connection, since_iso: str, limit: int, relevant_only: bool
) -> list[dict[str, Any]]:
    clause = " AND relevance_score > 0" if relevant_only else ""
    rows = conn.execute(
        "SELECT * FROM topics WHERE julianday(created_at) >= julianday(:since)"
        + clause
        + " ORDER BY relevance_score DESC LIMIT :limit",
        {"since": since_iso, "limit": limit},
    ).fetchall()
    return [
        {
            "topic_id": row["id"],
            "title": row["title"],
            "url": _topic_url(row["id"]),
            "excerpt": _snippet(row["excerpt"], 300),
            "created_at": row["created_at"],
            "relevance_score": row["relevance_score"],
            "posts_count": row["posts_count"],
        }
        for row in rows
    ]


def _updated_topics(
    conn: sqlite3.Connection, since_iso: str, limit: int, relevant_only: bool
) -> list[dict[str, Any]]:
    params = window_params(since_iso)
    clause = " AND t.relevance_score > 0" if relevant_only else ""
    rows = conn.execute(
        "SELECT * FROM ("
        "  SELECT t.id AS topic_id, t.title, t.relevance_score, t.bumped_at,"
        f"    SUM(CASE WHEN ({NEW_POSTS}) THEN 1 ELSE 0 END) AS new_posts,"
        f"    SUM(CASE WHEN ({EDITED_POSTS}) THEN 1 ELSE 0 END) AS edits"
        "  FROM topics t JOIN posts p ON p.topic_id = t.id"
        f"  WHERE (({NEW_POSTS}) OR ({EDITED_POSTS}))"
        "    AND julianday(t.created_at) < julianday(:since)" + clause + "  GROUP BY t.id"
        ") WHERE new_posts > 0 OR edits > 0"
        " ORDER BY relevance_score * (new_posts + edits) DESC LIMIT :limit",
        {**params, "limit": limit},
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        posts = conn.execute(
            "SELECT p.id, p.post_number, p.username, p.created_at, p.raw FROM posts p"
            f" WHERE p.topic_id = :topic_id AND ({NEW_POSTS})"
            " ORDER BY julianday(p.created_at) DESC LIMIT 3",
            {**params, "topic_id": row["topic_id"]},
        ).fetchall()
        output.append(
            {
                "topic_id": row["topic_id"],
                "title": row["title"],
                "url": _topic_url(row["topic_id"]),
                "new_posts": row["new_posts"],
                "edits": row["edits"],
                "relevance_score": row["relevance_score"],
                "bumped_at": row["bumped_at"],
                "latest": [
                    {
                        "post_id": post["id"],
                        "author": post["username"],
                        "date": post["created_at"],
                        "url": _post_url(row["topic_id"], post["post_number"]),
                        "snippet": _snippet(post["raw"], 300),
                    }
                    for post in posts
                ],
            }
        )
    return output


def _watched_repos(conn: sqlite3.Connection, since_iso: str) -> list[dict[str, Any]]:
    repos = conn.execute("SELECT * FROM repos WHERE watch=1 ORDER BY owner, name").fetchall()
    output: list[dict[str, Any]] = []
    for repo in repos:
        events = conn.execute(
            "SELECT * FROM repo_events WHERE repo_id=? AND detected_at >= ?"
            " ORDER BY detected_at DESC",
            (repo["id"], since_iso),
        ).fetchall()
        install = conn.execute(
            "SELECT * FROM repo_installs WHERE repo_id=? AND label='default'", (repo["id"],)
        ).fetchone()
        if not events and install is None:
            continue
        entry: dict[str, Any] = {
            "repo": f"{repo['owner']}/{repo['name']}",
            "url": repo["url"],
            "head_sha": repo["head_sha"],
            "events": [
                {
                    "kind": event["kind"],
                    "ref": event["ref"],
                    "prev_ref": event["prev_ref"],
                    "commits_ahead": event["commits_ahead"],
                    "title": event["title"],
                    "url": event["url"],
                    "detected_at": event["detected_at"],
                }
                for event in events
            ],
        }
        if install is not None:
            behind = None
            if repo["head_sha"] and install["sha"] != repo["head_sha"]:
                cached = conn.execute(
                    "SELECT response FROM compare_cache WHERE repo_id=? AND base_sha=? AND head_sha=?",
                    (repo["id"], install["sha"], repo["head_sha"]),
                ).fetchone()
                if cached is not None:
                    behind = (json.loads(cached["response"]) or {}).get("ahead_by")
            entry["installed"] = {
                "sha": install["sha"],
                "short_sha": install["sha"][:12],
                "installed_at": install["installed_at"],
                "commits_behind": behind,
            }
        output.append(entry)
    return output


def _repos_first_linked(
    conn: sqlite3.Connection, since_iso: str, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.owner, r.name, r.url, r.first_seen_at, m.post_id, m.topic_id, m.context_snippet,"
        " p.post_number, t.title FROM repos r LEFT JOIN repo_mentions m ON m.repo_id = r.id"
        " LEFT JOIN posts p ON p.id = m.post_id LEFT JOIN topics t ON t.id = m.topic_id"
        " WHERE r.first_seen_at >= ? ORDER BY r.first_seen_at DESC LIMIT ?",
        (since_iso, limit),
    ).fetchall()
    return [
        {
            "repo": f"{row['owner']}/{row['name']}",
            "url": row["url"],
            "first_seen_at": row["first_seen_at"],
            "topic": row["title"],
            "post_url": _post_url(row["topic_id"], row["post_number"]) if row["topic_id"] else None,
            "snippet": row["context_snippet"],
        }
        for row in rows
    ]


def _open_flags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM flags WHERE resolved_at IS NULL ORDER BY priority, created_at DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def build_digest(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    since: str = "cursor",
    cursor: str = "default",
    relevant_only: bool = False,
    limit: int = 30,
    advance: bool = True,
) -> dict[str, Any]:
    settings = settings or get_settings()
    since_iso = resolve_since(conn, since, cursor)
    built_at = utcnow_iso()

    backfilled_row = conn.execute(
        f"SELECT COUNT(*) AS posts, COUNT(DISTINCT p.topic_id) AS topics FROM posts p"
        f" WHERE ({BACKFILLED_POSTS})",
        window_params(since_iso),
    ).fetchone()

    digest = {
        "since": since_iso,
        "built_at": built_at,
        "cursor": cursor,
        "sections": {
            "watch_sightings": _sightings(conn),
            "watchlist_activity": watch_activity(conn, since_iso, limit),
            "new_topics": _new_topics(conn, since_iso, limit, relevant_only),
            "updated_topics": _updated_topics(conn, since_iso, limit, relevant_only),
            "watched_repos": _watched_repos(conn, since_iso),
            "repos_first_linked": _repos_first_linked(conn, since_iso, limit),
            "open_flags": _open_flags(conn),
        },
        "backfilled": {
            "posts": backfilled_row["posts"],
            "topics": backfilled_row["topics"],
        },
    }

    if advance:
        conn.execute(
            "INSERT INTO digest_cursors (name, last_seen_at) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (cursor, built_at),
        )
    return digest


def render_digest(digest: dict[str, Any]) -> str:
    lines = [f"sparkwatch digest — since {digest['since']} (built {digest['built_at']})"]
    sections = digest["sections"]
    for label, key in (
        ("Watch sightings", "watch_sightings"),
        ("Watchlist activity", "watchlist_activity"),
        ("New topics", "new_topics"),
        ("Updated topics", "updated_topics"),
        ("Watched repos", "watched_repos"),
        ("Repos first linked", "repos_first_linked"),
        ("Open flags", "open_flags"),
    ):
        items = sections[key]
        lines.append(f"\n## {label} ({len(items)})")
        for item in items:
            lines.append(f"- {item}")
    if digest["backfilled"]["posts"]:
        lines.append(
            f"\nbackfilled: {digest['backfilled']['posts']} posts"
            f" in {digest['backfilled']['topics']} topics"
        )
    return "\n".join(lines)


def _unused() -> None:
    parse_dt(utcnow_iso())
