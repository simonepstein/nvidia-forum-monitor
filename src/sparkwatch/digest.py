from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from . import knowledge
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


SNIPPET_CHARS = 120
WATCH_HITS_PER_LABEL = 2
THREADS_PER_LABEL = 2
REPLIES_PER_THREAD = 2
LATEST_POSTS_PER_TOPIC = 2
SECTION_CAP = 6
FLAG_REASON_CHARS = 200


def _snippet(text: str | None, length: int = SNIPPET_CHARS) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    if len(flat) <= length:
        return flat
    return flat[:length].rstrip() + "…"


def _snippet_around(
    text: str | None, needle: str | None, length: int = SNIPPET_CHARS
) -> str | None:
    """A snippet centred on the matched text, so it shows what actually matched."""
    if not text:
        return None
    flat = " ".join(text.split())
    index = flat.lower().find((needle or "").strip().lower()) if needle else -1
    if index < 0:
        return _snippet(flat, length)
    start = max(0, index - length // 3)
    end = min(len(flat), start + length)
    start = max(0, end - length)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end].strip()}{suffix}"


def resolve_since(conn: sqlite3.Connection, since: str, cursor: str) -> str:
    if since != "cursor":
        parsed = parse_dt(since)
        return to_iso(parsed) if parsed else since
    row = conn.execute("SELECT last_seen_at FROM digest_cursors WHERE name=?", (cursor,)).fetchone()
    return row["last_seen_at"] if row else EPOCH


def _sightings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open sighting alerts: release matches for anticipated models, until acknowledged."""
    rows = conn.execute(
        "SELECT w.label, w.sighting_post_id, w.sighting_at, p.topic_id, p.post_number, t.title"
        " FROM watchlist w LEFT JOIN posts p ON p.id = w.sighting_post_id"
        " LEFT JOIN topics t ON t.id = p.topic_id"
        " WHERE w.kind='anticipated' AND w.sighting_post_id IS NOT NULL"
        " AND w.sighting_acked_at IS NULL ORDER BY w.sighting_at DESC"
    ).fetchall()
    return [
        {
            "label": row["label"],
            "title": row["title"],
            "sighting_at": row["sighting_at"],
            "post_id": row["sighting_post_id"],
            "url": _post_url(row["topic_id"], row["post_number"]) if row["topic_id"] else None,
        }
        for row in rows
    ]


def watch_activity(conn: sqlite3.Connection, since_iso: str, limit: int) -> list[dict[str, Any]]:
    """New watch activity since the cursor, split into mentions and thread replies.

    A hit whose text matched in the post body is a mention: it is counted in `total` and
    shown with a snippet centred on the match. A hit that matched only the topic title is
    thread activity: `replies` counts them all, `threads` lists the busiest topics (capped)
    with ordinary snippets, so a busy thread stays visible without inflating the mentions.
    """
    params = window_params(since_iso)
    rows = conn.execute(
        "SELECT w.label, w.kind, h.post_id, h.topic_id, h.matched_in, h.matched_text,"
        " p.post_number, p.username, p.raw, p.created_at, t.title"
        " FROM watch_hits h JOIN watchlist w ON w.id = h.watch_id"
        " JOIN posts p ON p.id = h.post_id JOIN topics t ON t.id = h.topic_id"
        f" WHERE ({NEW_POSTS})"
        " ORDER BY julianday(p.created_at) DESC",
        params,
    ).fetchall()
    edited = {
        row["label"]: row["n"]
        for row in conn.execute(
            "SELECT w.label, COUNT(DISTINCT h.post_id) AS n FROM watch_hits h"
            " JOIN watchlist w ON w.id = h.watch_id JOIN posts p ON p.id = h.post_id"
            f" WHERE ({EDITED_POSTS}) AND h.matched_in='body' GROUP BY w.label",
            params,
        )
    }
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        anticipated = row["kind"] == "anticipated"
        bucket = grouped.setdefault(
            row["label"],
            {
                "label": row["label"],
                "kind": row["kind"],
                "total": 0,
                "edits": edited.get(row["label"], 0),
                "hits": [],
                "threads": {},
            },
        )
        if row["matched_in"] == "title":
            add_thread_reply(bucket["threads"], row, "new_replies")
            continue
        bucket["total"] += 1
        cap = 3 if anticipated else WATCH_HITS_PER_LABEL
        if len(bucket["hits"]) < cap:
            bucket["hits"].append(
                {
                    "title": row["title"],
                    "url": _post_url(row["topic_id"], row["post_number"]),
                    "matched": row["matched_text"],
                    "date": row["created_at"],
                    "snippet": _snippet_around(row["raw"], row["matched_text"]),
                }
            )
    entries: list[dict[str, Any]] = []
    for entry in list(grouped.values())[:limit]:
        threads = list(entry["threads"].values())
        entry["replies"] = sum(thread["new_replies"] for thread in threads)
        entry["threads_total"] = len(threads)
        entry["threads"] = threads[:THREADS_PER_LABEL]
        if entry["hits"] or entry["edits"] or entry["threads"]:
            entries.append(entry)
    return entries


def add_thread_reply(
    threads: dict[int, dict[str, Any]],
    row: Any,
    counter: str,
    snippet_chars: int = SNIPPET_CHARS,
) -> None:
    """Count one title-only hit as a reply in its topic, with an ordinary snippet.

    Shared with the MCP tools so both surfaces agree on what a thread reply is. `threads` is
    keyed by topic and holds every matching topic; each caller caps the list it returns, so
    the counters stay complete.
    """
    thread = threads.get(row["topic_id"])
    if thread is None:
        thread = {
            "topic_id": row["topic_id"],
            "title": row["title"],
            "url": _topic_url(row["topic_id"]),
            counter: 0,
            "latest": [],
        }
        threads[row["topic_id"]] = thread
    thread[counter] += 1
    if len(thread["latest"]) < REPLIES_PER_THREAD:
        thread["latest"].append(
            {
                "post_id": row["post_id"],
                "author": row["username"],
                "date": row["created_at"],
                "url": _post_url(row["topic_id"], row["post_number"]),
                "snippet": _snippet(row["raw"], snippet_chars),
            }
        )


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
            "excerpt": _snippet(row["excerpt"]),
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
            " ORDER BY julianday(p.created_at) DESC LIMIT :posts",
            {**params, "topic_id": row["topic_id"], "posts": LATEST_POSTS_PER_TOPIC},
        ).fetchall()
        output.append(
            {
                "topic_id": row["topic_id"],
                "title": row["title"],
                "url": _topic_url(row["topic_id"]),
                "new_posts": row["new_posts"],
                "edits": row["edits"],
                "latest": [
                    {
                        "post_id": post["id"],
                        "author": post["username"],
                        "date": post["created_at"],
                        "url": _post_url(row["topic_id"], post["post_number"]),
                        "snippet": _snippet(post["raw"]),
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
    """One line per repo first linked since the cursor: candidate for watch_repo."""
    rows = conn.execute(
        "SELECT r.owner, r.name, r.url, r.first_seen_at, m.post_id, m.topic_id,"
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
            "post_url": _post_url(row["topic_id"], row["post_number"]) if row["topic_id"] else None,
        }
        for row in rows
    ]


def _open_flags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, target_type, target_id, label, priority, reason, created_at FROM flags"
        " WHERE resolved_at IS NULL ORDER BY priority, created_at DESC"
    ).fetchall()
    return [
        {**flag, "reason": _snippet(flag["reason"], FLAG_REASON_CHARS)}
        for flag in knowledge.flags_payload(conn, rows)
    ]


def build_digest(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    since: str = "cursor",
    cursor: str = "default",
    relevant_only: bool = False,
    limit: int = 6,
    advance: bool = True,
) -> dict[str, Any]:
    """Build the "since I last asked" view.

    Sections 1-7 are events since the cursor, except that unacknowledged watch sightings
    and open flags are open *state*: they appear whenever they are open, so a digest is
    never empty even when nothing changed.
    """
    settings = settings or get_settings()
    since_iso = resolve_since(conn, since, cursor)
    built_at = utcnow_iso()
    cap = max(1, min(limit, SECTION_CAP))

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
            "watchlist_activity": watch_activity(conn, since_iso, cap),
            "new_topics": _new_topics(conn, since_iso, cap, relevant_only),
            "updated_topics": _updated_topics(conn, since_iso, cap, relevant_only),
            "watched_repos": _watched_repos(conn, since_iso),
            "repos_first_linked": _repos_first_linked(conn, since_iso, cap),
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


def _render_sighting(item: dict[str, Any]) -> str:
    return f"- [sighting] {item['label']}: {item['title']} — {item['url']}"


def _render_watch(entry: dict[str, Any]) -> list[str]:
    head = f"- {entry['label']} ({entry['kind']}): {entry['total']} new mention(s)"
    if entry["edits"]:
        head += f", {entry['edits']} edit(s)"
    if entry["replies"]:
        threads_word = "thread" if entry["threads_total"] == 1 else "threads"
        head += f", {entry['replies']} new replies in {entry['threads_total']} {threads_word}"
        hidden = entry["threads_total"] - len(entry["threads"])
        if hidden > 0:
            head += f" (+{hidden} more)"
    lines = [head]
    for hit in entry["hits"]:
        lines.append(f"  - {hit['date']} {hit['url']} — {hit['snippet']}")
    for thread in entry["threads"]:
        lines.append(
            f"  - thread {thread['title']} — {thread['url']}: {thread['new_replies']} new replies"
        )
        for reply in thread["latest"]:
            lines.append(f"    - {reply['date']} {reply['url']} — {reply['snippet']}")
    return lines


def _render_new_topic(item: dict[str, Any]) -> str:
    return f"- {item['title']} — {item['url']} ({item['created_at']}, {item['posts_count']} posts)"


def _render_updated_topic(item: dict[str, Any]) -> list[str]:
    head = f"- {item['title']} — {item['url']}: {item['new_posts']} new post(s)"
    if item["edits"]:
        head += f", {item['edits']} edit(s)"
    lines = [head]
    for post in item["latest"]:
        lines.append(f"  - {post['date']} {post['url']} — {post['snippet']}")
    return lines


def _render_repo(entry: dict[str, Any]) -> list[str]:
    lines = [f"- {entry['repo']} — {entry['url']}"]
    for event in entry["events"]:
        detail = event["title"] or event["ref"] or ""
        lines.append(f"  - {event['kind']} {detail} ({event['detected_at']})")
    install = entry.get("installed")
    if install:
        behind = install.get("commits_behind")
        where = f"{behind} commits behind" if behind is not None else "status unknown"
        lines.append(f"  - installed {install['short_sha']}, now {where}")
    return lines


def render_digest(digest: dict[str, Any]) -> str:
    """Readable CLI rendering: one line per item, with its URL and date."""
    lines = [f"sparkwatch digest — since {digest['since']} (built {digest['built_at']})"]
    sections = digest["sections"]

    lines.append(f"\n## Watch sightings ({len(sections['watch_sightings'])})")
    lines.extend(_render_sighting(item) for item in sections["watch_sightings"])

    lines.append(f"\n## Watchlist activity ({len(sections['watchlist_activity'])})")
    for entry in sections["watchlist_activity"]:
        lines.extend(_render_watch(entry))

    lines.append(f"\n## New topics ({len(sections['new_topics'])})")
    lines.extend(_render_new_topic(item) for item in sections["new_topics"])

    lines.append(f"\n## Updated topics ({len(sections['updated_topics'])})")
    for item in sections["updated_topics"]:
        lines.extend(_render_updated_topic(item))

    lines.append(f"\n## Watched repos ({len(sections['watched_repos'])})")
    for entry in sections["watched_repos"]:
        lines.extend(_render_repo(entry))

    lines.append(f"\n## Repos first linked ({len(sections['repos_first_linked'])})")
    for item in sections["repos_first_linked"]:
        lines.append(f"- {item['repo']} — {item['url']} ({item['first_seen_at']})")

    lines.append(f"\n## Open flags ({len(sections['open_flags'])})")
    for item in sections["open_flags"]:
        where = item.get("url") or item.get("model") or "(target missing)"
        lines.append(
            f"- [P{item['priority']}] {item['label']} on {item['target_type']}"
            f" {item['target_id']} — {where}: {item['reason']}"
        )

    if digest["backfilled"]["posts"]:
        lines.append(
            f"\nbackfilled: {digest['backfilled']['posts']} posts"
            f" in {digest['backfilled']['topics']} topics"
        )
    return "\n".join(lines)


def _unused() -> None:
    parse_dt(utcnow_iso())
