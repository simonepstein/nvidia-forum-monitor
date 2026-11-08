from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta
from typing import Any

from . import filters, knowledge
from .config import Settings, get_settings
from .util import parse_dt, to_iso, utcnow_iso

EPOCH = "1970-01-01T00:00:00+00:00"
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
# The same definitions with the window's exclusive upper bound (global_digest, SPEC §5).
NEW_POSTS_UNTIL = (
    f"({NEW_POSTS})"
    " AND julianday(p.first_fetched_at) < julianday(:until)"
    " AND julianday(p.created_at) < julianday(:until)"
)
BACKFILLED_POSTS_UNTIL = (
    f"({BACKFILLED_POSTS}) AND julianday(p.first_fetched_at) < julianday(:until)"
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
    return knowledge.post_link(topic_id, post_number)


def _topic_url(topic_id: int, slug: str | None = None) -> str:
    return knowledge.topic_url(topic_id)


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


# --------------------------------------------------------------------------
# Hot topics: global_digest (SPEC §5, ISSUES #37)
# --------------------------------------------------------------------------

GLOBAL_LIMIT_CAP = 25
GLOBAL_SNIPPET_CHARS = 160
GLOBAL_UNANSWERED_SNIPPET_CHARS = 120
GLOBAL_TOP_POSTS = 2
GLOBAL_FLAGS_PER_TOPIC = 3
GLOBAL_UNANSWERED = 3
GLOBAL_FIRST_LINKED = 5
GLOBAL_KNOWLEDGE_ITEMS = 5
GLOBAL_REPO_EVENTS = 5

# The digest's own new/backfilled definitions with the window's exclusive upper bound.
GLOBAL_POSTS = NEW_POSTS_UNTIL
GLOBAL_BACKFILLED = BACKFILLED_POSTS_UNTIL


def _global_window(since_iso: str, until_iso: str) -> dict[str, str]:
    return {"since": since_iso, "until": until_iso, "grace": grace_floor(since_iso)}


def _stamp(value: str | None) -> float:
    moment = parse_dt(value)
    return moment.timestamp() if moment is not None else 0.0


def _hotness(row: Any, settings: Settings, since_iso: str, until_iso: str) -> dict[str, Any]:
    """Score one topic from its window aggregates (SPEC §5, Hotness score).

    `components` are the weighted terms already multiplied by the relevance factor, so they
    add up to `score`; `base` is the pre-factor total and `factor` is reported alongside.
    """
    weights = settings.global_digest.weights
    cap = float(settings.global_digest.relevance_cap)
    relevant = filters.is_relevant(row)
    relevance = float(row["relevance_score"] or 0.0)
    factor = 0.5 if not relevant else 0.5 + min(relevance, cap) / cap
    created = row["created_at"]
    is_new = created is not None and since_iso <= created < until_iso
    terms = {
        "posts": weights.get("posts", 1.0) * math.log1p(row["new_posts"]),
        "posters": weights.get("posters", 1.5) * math.log1p(row["posters"]),
        "likes": weights.get("likes", 1.0) * math.log1p(row["likes"] or 0),
        "new_topic": weights.get("new_topic", 1.0) * (1.0 if is_new else 0.0),
        "accepted": weights.get("accepted", 1.0) * (1.0 if row["accepted"] else 0.0),
        "staff": weights.get("staff", 0.5) * (1.0 if row["staff"] else 0.0),
    }
    components = {key: round(value * factor, 3) for key, value in terms.items()}
    return {
        "is_new_topic": is_new,
        "relevant": relevant,
        "factor": round(factor, 4),
        "base": round(sum(terms.values()), 3),
        "components": components,
        "score": round(sum(components.values()), 3),
    }


def _global_aggregates(
    conn: sqlite3.Connection,
    params: dict[str, Any],
    forum_id: int | None,
) -> list[Any]:
    sql = (
        "SELECT t.id AS topic_id, t.forum_id, t.title, t.created_at, t.relevance_score,"
        " t.relevance_override, COUNT(p.id) AS new_posts,"
        " COUNT(DISTINCT p.username) AS posters, COALESCE(SUM(p.like_count), 0) AS likes,"
        " MAX(CASE WHEN p.accepted_answer = 1 THEN 1 ELSE 0 END) AS accepted,"
        " MAX(CASE WHEN p.staff = 1 THEN 1 ELSE 0 END) AS staff,"
        " MAX(p.created_at) AS newest_post"
        f" FROM topics t JOIN posts p ON p.topic_id = t.id WHERE ({GLOBAL_POSTS})"
    )
    values = dict(params)
    if forum_id is not None:
        sql += " AND t.forum_id = :forum_id"
        values["forum_id"] = forum_id
    sql += " GROUP BY t.id"
    return conn.execute(sql, values).fetchall()


def _topic_top_posts(
    conn: sqlite3.Connection, params: dict[str, Any], topic_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.id, p.post_number, p.username, p.created_at, p.raw, p.like_count"
        f" FROM posts p WHERE p.topic_id = :topic_id AND ({GLOBAL_POSTS})"
        " ORDER BY p.like_count DESC, julianday(p.created_at) DESC, p.id DESC LIMIT :n",
        {**params, "topic_id": topic_id, "n": GLOBAL_TOP_POSTS},
    ).fetchall()
    return [
        {
            "post_id": row["id"],
            "url": _post_url(topic_id, row["post_number"]),
            "date": row["created_at"],
            "author": row["username"],
            "likes": row["like_count"] or 0,
            "snippet": _snippet(row["raw"], GLOBAL_SNIPPET_CHARS),
        }
        for row in rows
    ]


def _topic_watch_labels(
    conn: sqlite3.Connection, params: dict[str, Any], topic_id: int
) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT w.label FROM watch_hits h JOIN watchlist w ON w.id = h.watch_id"
        " JOIN posts p ON p.id = h.post_id"
        f" WHERE h.topic_id = :topic_id AND h.matched_in = 'body' AND ({GLOBAL_POSTS})"
        " ORDER BY w.label",
        {**params, "topic_id": topic_id},
    ).fetchall()
    return [row["label"] for row in rows]


def _topic_flags(conn: sqlite3.Connection, topic_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM flags WHERE resolved_at IS NULL AND ("
        " (target_type = 'topic' AND target_id = :target)"
        " OR (target_type = 'post' AND target_id IN"
        "   (SELECT CAST(id AS TEXT) FROM posts WHERE topic_id = :topic_id)))"
        " ORDER BY priority, created_at DESC LIMIT :n",
        {"target": str(topic_id), "topic_id": topic_id, "n": GLOBAL_FLAGS_PER_TOPIC},
    ).fetchall()
    return [
        {
            "id": flag["id"],
            "label": flag["label"],
            "url": knowledge.flag_payload(conn, flag)["url"],
        }
        for flag in rows
    ]


def _hot_topic(
    conn: sqlite3.Connection,
    row: Any,
    settings: Settings,
    params: dict[str, Any],
    detail: bool,
) -> dict[str, Any]:
    topic_id = int(row["topic_id"])
    hotness = _hotness(row, settings, params["since"], params["until"])
    return {
        "topic_id": topic_id,
        "forum_id": row["forum_id"],
        "title": row["title"],
        "url": _topic_url(topic_id),
        "created_at": row["created_at"],
        "is_new_topic": hotness["is_new_topic"],
        "relevance_score": row["relevance_score"],
        "relevant": hotness["relevant"],
        "new_posts": row["new_posts"],
        "posters": row["posters"],
        "likes": row["likes"] or 0,
        "accepted_answer": bool(row["accepted"]),
        "staff": bool(row["staff"]),
        "base": hotness["base"],
        "factor": hotness["factor"],
        "score": hotness["score"],
        "components": hotness["components"],
        "top_posts": _topic_top_posts(conn, params, topic_id) if detail else [],
        "watch_labels": _topic_watch_labels(conn, params, topic_id),
        "flags": _topic_flags(conn, topic_id),
    }


def _unanswered(
    conn: sqlite3.Connection, params: dict[str, Any], forum_id: int | None
) -> dict[str, Any]:
    """New topics in the window with no replies: often questions worth surfacing."""
    clause = ""
    values = dict(params)
    if forum_id is not None:
        clause = " AND t.forum_id = :forum_id"
        values["forum_id"] = forum_id
    where = (
        " WHERE julianday(t.created_at) >= julianday(:since)"
        " AND julianday(t.created_at) < julianday(:until)"
        " AND (SELECT COUNT(*) FROM posts p WHERE p.topic_id = t.id) <= 1" + clause
    )
    count = conn.execute(f"SELECT COUNT(*) AS n FROM topics t{where}", values).fetchone()["n"]
    rows = conn.execute(
        "SELECT t.id, t.title, t.created_at, t.relevance_score,"
        " (SELECT p.raw FROM posts p WHERE p.topic_id = t.id"
        "  ORDER BY p.post_number LIMIT 1) AS op_raw"
        f" FROM topics t{where}"
        " ORDER BY t.relevance_score DESC, julianday(t.created_at) DESC LIMIT :n",
        {**values, "n": GLOBAL_UNANSWERED},
    ).fetchall()
    return {
        "count": count,
        "top": [
            {
                "topic_id": row["id"],
                "title": row["title"],
                "url": _topic_url(row["id"]),
                "date": row["created_at"],
                "snippet": _snippet(row["op_raw"], GLOBAL_UNANSWERED_SNIPPET_CHARS),
            }
            for row in rows
        ],
    }


def _first_mention_url(
    conn: sqlite3.Connection, table: str, id_column: str, model_id: int
) -> str | None:
    """The URL of the post that first mentioned a repo or HF model, by post date."""
    row = conn.execute(
        f"SELECT m.post_id FROM {table} m JOIN posts p ON p.id = m.post_id"
        f" WHERE m.{id_column} = ? ORDER BY p.created_at, m.post_id LIMIT 1",
        (model_id,),
    ).fetchone()
    return knowledge.post_url(conn, row["post_id"]) if row is not None else None


def _window_first_linked(
    conn: sqlite3.Connection, params: dict[str, Any], forum_id: int | None
) -> dict[str, Any]:
    """Repos and HF models first linked anywhere in the window, by mention count.

    Only links with at least one mention on a new post in the window count, so a repo first
    recorded during a backfill is not reported as news.
    """
    forum_clause = " AND t.forum_id = :forum_id" if forum_id is not None else ""
    values = dict(params)
    if forum_id is not None:
        values["forum_id"] = forum_id
    repos = conn.execute(
        "SELECT r.id, r.owner, r.name, r.url, r.first_seen_at, COUNT(m.post_id) AS mentions"
        " FROM repos r LEFT JOIN repo_mentions m ON m.repo_id = r.id"
        f" AND EXISTS (SELECT 1 FROM posts p WHERE p.id = m.post_id AND ({GLOBAL_POSTS}))"
        " LEFT JOIN topics t ON t.id = m.topic_id"
        " WHERE r.first_seen_at >= :since AND r.first_seen_at < :until"
        + forum_clause
        + " GROUP BY r.id HAVING mentions > 0"
        " ORDER BY mentions DESC, r.first_seen_at DESC",
        values,
    ).fetchall()
    hf = conn.execute(
        "SELECT h.id, h.repo_id, h.first_seen_at, COUNT(m.post_id) AS mentions"
        " FROM hf_models h LEFT JOIN hf_mentions m ON m.hf_model_id = h.id"
        f" AND EXISTS (SELECT 1 FROM posts p WHERE p.id = m.post_id AND ({GLOBAL_POSTS}))"
        " LEFT JOIN topics t ON t.id = m.topic_id"
        " WHERE h.first_seen_at >= :since AND h.first_seen_at < :until"
        + forum_clause
        + " GROUP BY h.id HAVING mentions > 0"
        " ORDER BY mentions DESC, h.first_seen_at DESC",
        values,
    ).fetchall()
    items = [
        {
            "type": "repo",
            "name": f"{row['owner']}/{row['name']}",
            "url": row["url"],
            "mentions": row["mentions"],
            "first_post_url": _first_mention_url(conn, "repo_mentions", "repo_id", row["id"]),
        }
        for row in repos
    ]
    items.extend(
        {
            "type": "hf_model",
            "name": row["repo_id"],
            "url": f"https://huggingface.co/{row['repo_id']}",
            "mentions": row["mentions"],
            "first_post_url": _first_mention_url(conn, "hf_mentions", "hf_model_id", row["id"]),
        }
        for row in hf
    )
    items.sort(key=lambda item: (-item["mentions"], item["name"]))
    return {"count": len(items), "top": items[:GLOBAL_FIRST_LINKED]}


def _evidence_urls(conn: sqlite3.Connection, kind: str, target_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT post_id FROM evidence WHERE target_type = ? AND target_id = ? ORDER BY id",
        (kind, target_id),
    ).fetchall()
    return [url for url in (knowledge.post_url(conn, row["post_id"]) for row in rows) if url]


def _window_knowledge(conn: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    """Recipes, benchmarks and notes the agent recorded in the window, with evidence URLs."""
    items: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, model_canonical, quant_format, engine, topology, status, created_at"
        " FROM recipes WHERE created_at >= :since AND created_at < :until"
        " ORDER BY created_at DESC",
        params,
    ):
        items.append(
            {
                "kind": "recipe",
                "id": row["id"],
                "model": row["model_canonical"],
                "summary": " · ".join(
                    part
                    for part in (
                        row["model_canonical"],
                        row["quant_format"],
                        row["engine"],
                        row["topology"],
                        row["status"],
                    )
                    if part
                ),
                "date": row["created_at"],
                "urls": _evidence_urls(conn, "recipe", row["id"]),
            }
        )
    for row in conn.execute(
        "SELECT id, model_canonical, benchmark, score, score_unit, created_at"
        " FROM benchmark_results WHERE created_at >= :since AND created_at < :until"
        " ORDER BY created_at DESC",
        params,
    ):
        unit = f" {row['score_unit']}" if row["score_unit"] else ""
        items.append(
            {
                "kind": "benchmark",
                "id": row["id"],
                "model": row["model_canonical"],
                "summary": f"{row['model_canonical']} · {row['benchmark']} {row['score']}{unit}",
                "date": row["created_at"],
                "urls": _evidence_urls(conn, "benchmark", row["id"]),
            }
        )
    for row in conn.execute(
        "SELECT id, subject, model_canonical, created_at FROM research_notes"
        " WHERE created_at >= :since AND created_at < :until"
        " AND lower(subject) NOT LIKE 'newsletter %'"
        " ORDER BY created_at DESC",
        params,
    ):
        items.append(
            {
                "kind": "note",
                "id": row["id"],
                "model": row["model_canonical"],
                "summary": row["subject"],
                "date": row["created_at"],
                "sources": knowledge.note_sources(conn, row["id"]),
            }
        )
    items.sort(key=lambda item: item["date"], reverse=True)
    return {"count": len(items), "items": items[:GLOBAL_KNOWLEDGE_ITEMS]}


def _window_repo_events(
    conn: sqlite3.Connection, params: dict[str, Any], forum_id: int | None
) -> dict[str, Any]:
    """Releases and tags of watched repos in the window, newest first, capped like the rest."""
    rows = conn.execute(
        "SELECT e.kind, e.ref, e.title, e.url, e.detected_at, r.owner, r.name"
        " FROM repo_events e JOIN repos r ON r.id = e.repo_id"
        " WHERE r.watch = 1 AND e.kind IN ('release', 'tag')"
        " AND e.detected_at >= :since AND e.detected_at < :until"
        " ORDER BY e.detected_at DESC",
        params,
    ).fetchall()
    events = [
        {
            "repo": f"{row['owner']}/{row['name']}",
            "kind": row["kind"],
            "ref": row["ref"],
            "title": row["title"],
            "url": row["url"],
            "date": row["detected_at"],
        }
        for row in rows
    ]
    return {"count": len(events), "top": events[:GLOBAL_REPO_EVENTS]}


def global_digest(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    since: str = "cursor",
    until: str | None = None,
    cursor: str = "global",
    forum_id: int | None = None,
    limit: int = 10,
    relevant_only: bool = False,
    detail: bool = False,
    advance: bool = True,
    next: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ "What is the forum talking about": hot topics across both forums, deterministically.

    The window is `[since, until)`. Only posts sparkwatch first saw in the window count, and
    they must have been written in it (24-hour grace); backfilled posts are a count, and edits
    are not counted. `until` is echoed back, so the same `since`/`until` gives the same
    ranking. Its cursor (`'global'`) is separate from the digest's `'default'`.

    `detail=True` adds each topic's 2 most-liked new posts with snippets; the default stays
    lean so ten topics still fit the 10 KB response budget.
    """
    settings = settings or get_settings()
    if next:
        since = str(next.get("since") or since)
        until = str(next.get("until") or until or "")
        offset = int(next.get("offset") or 0)
    else:
        offset = 0
    since_iso = resolve_since(conn, since, cursor)
    if parse_dt(since_iso) is None:
        raise ValueError(f"invalid since date: {since!r}; use an ISO date or 'cursor'")
    if until:
        parsed_until = parse_dt(until)
        if parsed_until is None:
            raise ValueError(f"invalid until date: {until!r}; use an ISO date")
        until_iso = to_iso(parsed_until)
    else:
        until_iso = utcnow_iso()
    params = _global_window(since_iso, until_iso)
    cap = max(1, min(int(limit), GLOBAL_LIMIT_CAP))

    ranked = []
    for row in _global_aggregates(conn, params, forum_id):
        hotness = _hotness(row, settings, since_iso, until_iso)
        if relevant_only and not hotness["relevant"]:
            continue
        ranked.append((hotness["score"], row["newest_post"], int(row["topic_id"]), row))
    ranked.sort(key=lambda item: (-item[0], -_stamp(item[1]), item[2]))

    page = ranked[offset : offset + cap]
    hot_topics = [
        _hot_topic(conn, row, settings, params, detail) for _score, _new, _id, row in page
    ]
    next_token = None
    if offset + len(page) < len(ranked):
        next_token = {"since": since_iso, "until": until_iso, "offset": offset + len(page)}

    backfilled = conn.execute(
        f"SELECT COUNT(*) AS posts, COUNT(DISTINCT p.topic_id) AS topics FROM posts p"
        f" WHERE ({GLOBAL_BACKFILLED})",
        params,
    ).fetchone()

    payload: dict[str, Any] = {
        "since": since_iso,
        "until": until_iso,
        "cursor": cursor,
        "weights": settings.global_digest.weights,
        "hot_topics": hot_topics,
        "hot_topics_total": len(ranked),
        "unanswered": _unanswered(conn, params, forum_id),
        "first_linked": _window_first_linked(conn, params, forum_id),
        "knowledge": _window_knowledge(conn, params),
        "repo_events": _window_repo_events(conn, params, forum_id),
        "backfilled": {"posts": backfilled["posts"], "topics": backfilled["topics"]},
    }
    if next_token is not None:
        payload["next"] = next_token

    if advance and next is None:
        current = conn.execute(
            "SELECT last_seen_at FROM digest_cursors WHERE name=?", (cursor,)
        ).fetchone()
        if current is None or until_iso >= current["last_seen_at"]:
            conn.execute(
                "INSERT INTO digest_cursors (name, last_seen_at) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (cursor, until_iso),
            )
    return payload


def _render_global_topic(item: dict[str, Any]) -> str:
    counts = (
        f"{item['new_posts']} new posts by {item['posters']} posters,"
        f" {item['likes']} likes, factor {item['factor']:.2f} (base {item['base']:.2f})"
    )
    terms = ", ".join(f"{key} +{value:.2f}" for key, value in item["components"].items() if value)
    lines = [f"- {item['score']:.2f} {item['title']} — {item['url']}", f"  {counts}"]
    if terms:
        lines.append(f"  terms: {terms}")
    return "\n".join(lines)


def _capped_header(shown: int, total: int) -> str:
    return f"{shown}/{total}"


def render_global_digest(payload: dict[str, Any]) -> str:
    """Readable CLI rendering: one line per hot topic, then the extra sections."""
    lines = [
        f"sparkwatch global-digest — {payload['since']} → {payload['until']}"
        f" (cursor {payload['cursor']})"
    ]
    lines.append(
        f"\n## Hot topics ({_capped_header(len(payload['hot_topics']), payload['hot_topics_total'])})"
    )
    lines.extend(_render_global_topic(item) for item in payload["hot_topics"])
    for item in payload["hot_topics"]:
        for post in item["top_posts"]:
            lines.append(
                f"  · {post['likes']} likes {post['url']} {post['date']} — {post['snippet']}"
            )

    unanswered = payload["unanswered"]
    lines.append(f"\n## Unanswered ({_capped_header(len(unanswered['top']), unanswered['count'])})")
    for item in unanswered["top"]:
        lines.append(f"- {item['title']} — {item['url']} ({item['date']})")

    first_linked = payload["first_linked"]
    lines.append(
        f"\n## First linked ({_capped_header(len(first_linked['top']), first_linked['count'])})"
    )
    for item in first_linked["top"]:
        lines.append(f"- {item['name']} — {item['url']} ({item['mentions']} mentions)")

    knowledge = payload["knowledge"]
    lines.append(
        f"\n## Recorded knowledge ({_capped_header(len(knowledge['items']), knowledge['count'])})"
    )
    for item in knowledge["items"]:
        urls = item.get("urls") or [source["url"] for source in item.get("sources") or []]
        lines.append(f"- [{item['kind']}] {item['summary']} — {' '.join(urls) or '(no evidence)'}")

    repo_events = payload["repo_events"]
    lines.append(
        f"\n## Watched repo events ({_capped_header(len(repo_events['top']), repo_events['count'])})"
    )
    for item in repo_events["top"]:
        lines.append(f"- {item['repo']} {item['kind']} {item['ref']} — {item['url']}")

    if payload["backfilled"]["posts"]:
        lines.append(
            f"\nbackfilled: {payload['backfilled']['posts']} posts"
            f" in {payload['backfilled']['topics']} topics"
        )
    if "next" in payload:
        remaining = payload["hot_topics_total"] - (payload["next"]["offset"])
        lines.append(
            f"\n{remaining} more hot topic(s): raise --limit (cap {GLOBAL_LIMIT_CAP}) to see them"
        )
    return "\n".join(lines)


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
