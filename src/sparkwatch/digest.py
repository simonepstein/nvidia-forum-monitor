from __future__ import annotations

import sqlite3
from typing import Any

from .config import Settings, get_settings
from .util import parse_dt, to_iso, utcnow_iso

EPOCH = "1970-01-01T00:00:00+00:00"
FORUM_URL = "https://forums.developer.nvidia.com"


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


def _watch_activity(conn: sqlite3.Connection, since_iso: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT w.label, h.post_id, h.topic_id, h.matched_text, h.created_at, p.post_number,"
        " p.raw, t.title FROM watch_hits h JOIN watchlist w ON w.id = h.watch_id"
        " JOIN posts p ON p.id = h.post_id JOIN topics t ON t.id = h.topic_id"
        " WHERE w.kind='tracking' AND h.created_at >= ? ORDER BY h.created_at DESC LIMIT ?",
        (since_iso, limit),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
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
    return [{"label": label, "hits": hits} for label, hits in grouped.items()]


def _new_topics(
    conn: sqlite3.Connection, since_iso: str, limit: int, relevant_only: bool
) -> list[dict[str, Any]]:
    clause = " AND relevance_score > 0" if relevant_only else ""
    rows = conn.execute(
        "SELECT * FROM topics WHERE created_at >= ?"
        + clause
        + " ORDER BY relevance_score DESC LIMIT ?",
        (since_iso, limit),
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
    clause = " AND t.relevance_score > 0" if relevant_only else ""
    rows = conn.execute(
        "SELECT t.id, t.title, t.slug, t.relevance_score, t.bumped_at, COUNT(p.id) AS new_posts,"
        " t.created_at FROM topics t JOIN posts p ON p.topic_id = t.id"
        " WHERE (p.first_fetched_at >= ? OR p.changed_at >= ?) AND t.created_at < ?"
        + clause
        + " GROUP BY t.id ORDER BY t.relevance_score * COUNT(p.id) DESC LIMIT ?",
        (since_iso, since_iso, since_iso, limit),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        posts = conn.execute(
            "SELECT id, post_number, username, created_at, raw, first_fetched_at FROM posts"
            " WHERE topic_id=? ORDER BY post_number DESC LIMIT 3",
            (row["id"],),
        ).fetchall()
        output.append(
            {
                "topic_id": row["id"],
                "title": row["title"],
                "url": _topic_url(row["id"]),
                "new_posts": row["new_posts"],
                "relevance_score": row["relevance_score"],
                "bumped_at": row["bumped_at"],
                "latest": [
                    {
                        "post_id": post["id"],
                        "author": post["username"],
                        "date": post["created_at"],
                        "url": _post_url(row["id"], post["post_number"]),
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
            ahead = None
            if repo["head_sha"] and install["sha"] != repo["head_sha"]:
                cached = conn.execute(
                    "SELECT response FROM compare_cache WHERE repo_id=? AND base_sha=? AND head_sha=?",
                    (repo["id"], install["sha"], repo["head_sha"]),
                ).fetchone()
                if cached is not None:
                    import json

                    ahead = (json.loads(cached["response"]) or {}).get("ahead_by")
            entry["installed"] = {
                "sha": install["sha"],
                "short_sha": install["sha"][:12],
                "installed_at": install["installed_at"],
                "commits_behind": ahead,
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

    backfilled = conn.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE first_fetched_at >= ? AND created_at < ?",
        (since_iso, since_iso),
    ).fetchone()["n"]

    digest = {
        "since": since_iso,
        "built_at": built_at,
        "cursor": cursor,
        "sections": {
            "watch_sightings": _sightings(conn),
            "watchlist_activity": _watch_activity(conn, since_iso, limit),
            "new_topics": _new_topics(conn, since_iso, limit, relevant_only),
            "updated_topics": _updated_topics(conn, since_iso, limit, relevant_only),
            "watched_repos": _watched_repos(conn, since_iso),
            "repos_first_linked": _repos_first_linked(conn, since_iso, limit),
            "open_flags": _open_flags(conn),
        },
        "backfilled_posts": backfilled,
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
    if digest["backfilled_posts"]:
        lines.append(f"\nBackfilled posts seen in this window: {digest['backfilled_posts']}")
    return "\n".join(lines)


def _unused() -> None:
    parse_dt(utcnow_iso())
