from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from . import evidence, fit, normalize
from . import watch as watch_mod
from .config import Settings, describe_validation_error, get_settings
from .db import open_db
from .digest import (
    BACKFILLED_POSTS,
    EDITED_POSTS,
    NEW_POSTS,
    build_digest,
    resolve_since,
    watch_activity,
    window_params,
)
from .lock import LockBusy, write_lock
from .repos import (
    check_repos,
)
from .repos import (
    clear_installed as _clear_installed,
)
from .repos import (
    get_repo as _get_repo,
)
from .repos import (
    list_installs as _list_installs,
)
from .repos import (
    list_repos as _list_repos,
)
from .repos import (
    repo_events as _repo_events,
)
from .repos import (
    repo_status as _repo_status,
)
from .repos import (
    set_installed as _set_installed,
)
from .repos import (
    watch_repo as _watch_repo,
)
from .util import parse_dt, utcnow, utcnow_iso

FORUM_URL = "https://forums.developer.nvidia.com"
EPOCH = "1970-01-01T00:00:00+00:00"

mcp = FastMCP("sparkwatch")

TOOLS: list[Any] = []


def tool(fn):
    TOOLS.append(fn)
    mcp.add_tool(fn)
    return fn


def _settings() -> Settings:
    return get_settings()


def _conn(settings: Settings | None = None) -> sqlite3.Connection:
    return open_db(settings or _settings())


def _limit(value: int | None, settings: Settings) -> int:
    if value is None:
        return settings.mcp.default_limit
    return max(1, min(int(value), settings.mcp.max_limit))


def _max_chars(value: int | None, settings: Settings) -> int:
    return int(value) if value else settings.mcp.max_chars


def _post_url(topic_id: int, post_number: int | None) -> str:
    return f"{FORUM_URL}/t/{topic_id}/{post_number or 1}"


def _topic_url(topic_id: int) -> str:
    return f"{FORUM_URL}/t/{topic_id}"


def _snippet(text: str | None, length: int = 300) -> str | None:
    if not text:
        return None
    return " ".join(text.split())[:length]


def _page(text: str, next_token: dict[str, Any] | None, max_chars: int) -> dict[str, Any]:
    offset = 0
    if next_token:
        offset = int(next_token.get("offset", 0) or 0)
    offset = max(0, min(offset, len(text)))
    chunk = text[offset : offset + max_chars]
    end = offset + len(chunk)
    payload: dict[str, Any] = {"text": chunk, "offset": offset, "total_chars": len(text)}
    if end < len(text):
        payload["truncated"] = True
        payload["next"] = {"offset": end}
    else:
        payload["truncated"] = False
        payload["next"] = None
    return payload


def _model_pattern(conn: sqlite3.Connection, model: str) -> re.Pattern[str]:
    row = conn.execute(
        "SELECT match_regex FROM watchlist WHERE canonical=? OR label=?", (model, model)
    ).fetchone()
    if row is not None:
        try:
            return re.compile(row["match_regex"], re.I)
        except re.error:
            pass
    canonical, _is_new = normalize.canonical_model(conn, model)
    parts = [re.escape(part) for part in re.split(r"[\s._-]+", canonical) if part]
    return re.compile(r"[\s._-]*".join(parts), re.I)


def _flag_rows(conn: sqlite3.Connection, open_only: bool = True) -> list[dict[str, Any]]:
    clause = " WHERE resolved_at IS NULL" if open_only else ""
    rows = conn.execute(
        f"SELECT * FROM flags{clause} ORDER BY priority, created_at DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def _recipe_row(conn: sqlite3.Connection, recipe_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if row is None:
        return None
    payload = dict(row)
    payload["evidence"] = [
        {
            "post_id": item["post_id"],
            "quote": item["quote"],
            "verified": bool(item["quote_verified"]),
            "url": _post_url(
                conn.execute(
                    "SELECT topic_id FROM posts WHERE id=?", (item["post_id"],)
                ).fetchone()["topic_id"],
                conn.execute(
                    "SELECT post_number FROM posts WHERE id=?", (item["post_id"],)
                ).fetchone()["post_number"],
            ),
        }
        for item in conn.execute(
            "SELECT post_id, quote, quote_verified FROM evidence"
            " WHERE target_type='recipe' AND target_id=?",
            (recipe_id,),
        )
    ]
    return payload


# --------------------------------------------------------------------------
# Keeping up to date
# --------------------------------------------------------------------------


@tool
def sync_now(
    forum_id: int | None = None,
    include_repos: bool = True,
    time_budget_s: int | None = None,
) -> dict[str, Any]:
    """Fetch new and updated forum posts now, then (optionally) check watched repos.

    Use this first when the user says "catch me up" or "pull the latest posts". It stops
    cleanly at the time budget and returns partial=true; call it again to continue. The
    repo check shares what is left of the budget and reports repos_partial=true if it was
    cut short. Returns titles for new and updated topics, plus watched repos whose head
    moved or that got a release.
    """
    from .sync import sync as run_sync

    settings = _settings()
    budget = time_budget_s if time_budget_s is not None else settings.sync.sync_now_time_budget_s
    started = time.monotonic()
    job = "sync+repos" if include_repos else "sync"
    try:
        with write_lock(job, settings.db_path):
            conn = _conn(settings)
            result = run_sync(
                settings=settings,
                forum_ids=[forum_id] if forum_id else None,
                time_budget_s=budget,
                trigger="mcp",
            )
            payload = result.as_dict()
            payload["new_topics"] = _topic_titles(conn, result.new_topic_ids)
            payload["updated_topics"] = _topic_titles(conn, result.updated_topic_ids)
            payload["repos"] = []
            if include_repos:
                remaining = max(0.0, budget - (time.monotonic() - started))
                payload["repos_budget_s"] = round(remaining, 1)
                repos_result = check_repos(conn, settings, deadline=time.monotonic() + remaining)
                payload["repos"] = repos_result.get("changed", [])
                payload["repos_checked"] = repos_result.get("checked", 0)
                payload["repos_partial"] = bool(repos_result.get("stopped_early"))
    except LockBusy as exc:
        return exc.as_dict()
    return payload


def _topic_titles(conn: sqlite3.Connection, topic_ids: list[int]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for topic_id in topic_ids:
        row = conn.execute(
            "SELECT id, title, bumped_at FROM topics WHERE id=?", (topic_id,)
        ).fetchone()
        if row is not None:
            output.append(
                {
                    "topic_id": row["id"],
                    "title": row["title"],
                    "url": _topic_url(row["id"]),
                    "bumped_at": row["bumped_at"],
                }
            )
    return output


@tool
def digest(
    since: str = "cursor",
    cursor: str = "default",
    relevant_only: bool = False,
    limit: int = 10,
    advance: bool = True,
) -> dict[str, Any]:
    """Show what changed since the last time the agent looked ("since I last asked").

    Sections 1-6 are events since the cursor: watchlist activity, new topics, updated topics,
    watched repo events, and repos first linked from posts. Sections 1 and 7 of the spec
    (unacknowledged watch sightings and open flags) are open state: they appear whenever
    they are open, so a digest is never empty even when nothing changed. Timing is based on
    when sparkwatch first saw a post, so backfilled posts are reported as a count, not as news.
    """
    settings = _settings()
    conn = _conn(settings)
    return build_digest(
        conn,
        settings,
        since=since,
        cursor=cursor,
        relevant_only=relevant_only,
        limit=_limit(limit, settings),
        advance=advance,
    )


@tool
def whats_new_for(model: str, since: str | None = None) -> dict[str, Any]:
    """Show everything new about one model: watch hits and matching posts.

    `since` applies to every section, using the digest's timing: only posts that are new
    or edited since then are listed, and older posts picked up by a backfill are a count.
    Use this before answering a "what's the latest on X" question.
    """
    settings = _settings()
    conn = _conn(settings)
    since_iso = resolve_since(conn, since or "cursor", "default")
    pattern = _model_pattern(conn, model)
    limit = settings.mcp.default_limit
    mentions = find_model_mentions(model, since=since_iso, limit=limit)
    mentions.pop("since", None)
    watch_rows = [
        {"label": row["label"], "hits": row["hits"], "edits": row["edits"]}
        for row in watch_activity(conn, since_iso, limit)
        if pattern.search(row["label"])
    ]
    return {
        "model": model,
        "since": since_iso,
        "watch": watch_rows,
        "mentions": mentions,
    }


@tool
def ack_watch_sighting(label: str) -> dict[str, Any]:
    """Acknowledge a first sighting of an anticipated model so it stops topping the digest."""
    conn = _conn()
    row = watch_mod.ack_sighting(conn, label)
    if row is None:
        return {"error": f"unknown watch label: {label}"}
    return {"label": label, "sighting_acked_at": row["sighting_acked_at"]}


# --------------------------------------------------------------------------
# Reading and research
# --------------------------------------------------------------------------


def _fts_plain_query(query: str) -> str:
    """Turn free text into a safe FTS5 query: quote each term, then AND them."""
    terms: list[str] = []
    for term in (query or "").split():
        cleaned = term.strip("\"'()[]{},;:")
        if not any(character.isalnum() for character in cleaned):
            continue
        terms.append('"' + cleaned.replace('"', '""') + '"')
    return " AND ".join(terms)


@tool
def search_posts(
    query: str,
    topic_id: int | None = None,
    forum_id: int | None = None,
    since: str | None = None,
    username: str | None = None,
    order: str = "rank",
    raw_fts: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Full-text search the mirrored forum posts (FTS5, porter stemming).

    Plain text by default: each whitespace-separated term is quoted and the terms are
    ANDed, so "GLM 5.3 Flash" and "tok/s" work as typed. Pass raw_fts=true to send FTS5
    operators (OR, NEAR, title:, quotes) unchanged; a syntax error then comes back as a
    tool error. Use order="recent" to prefer the newest matches. Returns a snippet with
    the match highlighted in [brackets].
    """
    settings = _settings()
    conn = _conn(settings)
    match = query if raw_fts else _fts_plain_query(query)
    if not match:
        return {"error": f"no searchable terms in query: {query!r}"}
    order_sql = "p.first_fetched_at DESC" if order == "recent" else "bm25(posts_fts)"
    clauses = ["posts_fts MATCH ?"]
    params: list[Any] = [match]
    if topic_id is not None:
        clauses.append("p.topic_id = ?")
        params.append(topic_id)
    if forum_id is not None:
        clauses.append("t.forum_id = ?")
        params.append(forum_id)
    if since is not None:
        clauses.append("(p.first_fetched_at >= ? OR p.created_at >= ?)")
        params.extend([since, since])
    if username is not None:
        clauses.append("p.username = ?")
        params.append(username)
    sql = (
        "SELECT p.id, p.topic_id, p.post_number, p.username, p.created_at, p.first_fetched_at,"
        " t.title, snippet(posts_fts, 1, '[', ']', '…', 24) AS snippet"
        " FROM posts_fts JOIN posts p ON p.id = posts_fts.rowid"
        " JOIN topics t ON t.id = p.topic_id WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY {order_sql} LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (*params, _limit(limit, settings))).fetchall()
    except sqlite3.OperationalError as exc:
        return {"error": f"bad query: {exc}"}
    payload: dict[str, Any] = {
        "query": query,
        "match": match,
        "raw_fts": raw_fts,
        "count": len(rows),
        "results": [
            {
                "post_id": row["id"],
                "topic_id": row["topic_id"],
                "title": row["title"],
                "author": row["username"],
                "date": row["created_at"],
                "seen_at": row["first_fetched_at"],
                "url": _post_url(row["topic_id"], row["post_number"]),
                "snippet": row["snippet"],
            }
            for row in rows
        ],
    }
    if not rows and not raw_fts:
        payload["hint"] = (
            "no post matched every term (terms are ANDed); pass raw_fts=true"
            " for OR/NEAR, or drop a term"
        )
    return payload


@tool
def find_model_mentions(
    model: str, since: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    """Find every post that mentions a model, grouped by topic, newest first.

    With `since`, only posts that are new since then are listed (the digest's timing rule,
    with its 24-hour grace); edits and backfilled posts are counted, not listed.
    Use this when recipes are missing for a model people keep talking about.
    """
    settings = _settings()
    conn = _conn(settings)
    pattern = _model_pattern(conn, model)
    params = window_params(since) if since else {}
    where = f" WHERE ({NEW_POSTS})" if since else ""
    rows = conn.execute(
        "SELECT p.id, p.topic_id, p.post_number, p.username, p.created_at, p.raw, t.title"
        " FROM posts p JOIN topics t ON t.id = p.topic_id"
        + where
        + " ORDER BY julianday(p.created_at) DESC",
        params,
    ).fetchall()

    edits = 0
    backfilled = 0
    if since:
        for row in conn.execute(
            f"SELECT p.raw, t.title, CASE WHEN ({EDITED_POSTS}) THEN 'edited'"
            f" ELSE 'backfilled' END AS timing"
            " FROM posts p JOIN topics t ON t.id = p.topic_id"
            f" WHERE ({EDITED_POSTS}) OR ({BACKFILLED_POSTS})",
            params,
        ):
            if not (pattern.search(row["raw"] or "") or pattern.search(row["title"] or "")):
                continue
            if row["timing"] == "edited":
                edits += 1
            else:
                backfilled += 1

    grouped: dict[int, dict[str, Any]] = {}
    total = 0
    for row in rows:
        if not (pattern.search(row["raw"] or "") or pattern.search(row["title"] or "")):
            continue
        total += 1
        bucket = grouped.setdefault(
            row["topic_id"],
            {
                "topic_id": row["topic_id"],
                "title": row["title"],
                "url": _topic_url(row["topic_id"]),
                "posts": [],
            },
        )
        if len(bucket["posts"]) < 5:
            bucket["posts"].append(
                {
                    "post_id": row["id"],
                    "author": row["username"],
                    "date": row["created_at"],
                    "url": _post_url(row["topic_id"], row["post_number"]),
                    "snippet": _snippet(row["raw"], 300),
                }
            )
    topics = list(grouped.values())[: _limit(limit, settings)]
    return {
        "model": model,
        "since": since,
        "total_posts": total,
        "edits": edits,
        "backfilled": backfilled,
        "topics": topics,
    }


@tool
def list_topics(
    forum_id: int | None = None,
    since: str | None = None,
    relevant_only: bool = False,
    order: str = "bumped",
    limit: int | None = None,
) -> dict[str, Any]:
    """List topics, ordered by bump time, relevance, likes or creation date."""
    settings = _settings()
    conn = _conn(settings)
    clauses: list[str] = []
    params: list[Any] = []
    if forum_id is not None:
        clauses.append("forum_id = ?")
        params.append(forum_id)
    if since is not None:
        clauses.append("bumped_at >= ?")
        params.append(since)
    if relevant_only:
        clauses.append(
            "(relevance_override = 1 OR (relevance_override IS NULL AND relevance_score > 0))"
        )
    order_sql = {
        "bumped": "bumped_at DESC",
        "relevance": "relevance_score DESC",
        "likes": "like_count DESC",
        "created": "created_at DESC",
    }.get(order, "bumped_at DESC")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM topics{where} ORDER BY {order_sql} LIMIT ?",
        (*params, _limit(limit, settings)),
    ).fetchall()
    return {
        "count": len(rows),
        "topics": [
            {
                "topic_id": row["id"],
                "title": row["title"],
                "url": _topic_url(row["id"]),
                "forum_id": row["forum_id"],
                "created_at": row["created_at"],
                "bumped_at": row["bumped_at"],
                "posts_count": row["posts_count"],
                "like_count": row["like_count"],
                "relevance_score": row["relevance_score"],
                "pinned": bool(row["pinned"]),
                "has_accepted_answer": bool(row["has_accepted_answer"]),
            }
            for row in rows
        ],
    }


@tool
def get_topic_overview(topic_id: int) -> dict[str, Any]:
    """Summarise a topic: metadata, the OP, participants, linked repos and recorded knowledge.

    Use this to decide whether a thread is worth reading in full.
    """
    settings = _settings()
    conn = _conn(settings)
    topic = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if topic is None:
        return {"error": f"unknown topic: {topic_id}"}
    op = conn.execute(
        "SELECT * FROM posts WHERE topic_id=? ORDER BY post_number LIMIT 1", (topic_id,)
    ).fetchone()
    participants = conn.execute(
        "SELECT COUNT(DISTINCT username) AS n FROM posts WHERE topic_id=?", (topic_id,)
    ).fetchone()["n"]
    repos = conn.execute(
        "SELECT DISTINCT r.owner, r.name, r.url FROM repo_mentions m JOIN repos r ON r.id = m.repo_id"
        " WHERE m.topic_id=?",
        (topic_id,),
    ).fetchall()
    hf = conn.execute(
        "SELECT DISTINCT h.repo_id FROM hf_mentions m JOIN hf_models h ON h.id = m.hf_model_id"
        " WHERE m.topic_id=?",
        (topic_id,),
    ).fetchall()
    hits = conn.execute(
        "SELECT w.label, COUNT(*) AS n FROM watch_hits h"
        " JOIN watchlist w ON w.id = h.watch_id WHERE h.topic_id=? GROUP BY w.label"
        " ORDER BY n DESC",
        (topic_id,),
    ).fetchall()
    watch_hits = []
    for row in hits:
        latest = conn.execute(
            "SELECT p.id, p.post_number, p.created_at FROM watch_hits h"
            " JOIN posts p ON p.id = h.post_id"
            " WHERE h.topic_id=? AND h.watch_id = (SELECT id FROM watchlist WHERE label=?)"
            " ORDER BY julianday(p.created_at) DESC LIMIT 1",
            (topic_id, row["label"]),
        ).fetchone()
        watch_hits.append(
            {
                "label": row["label"],
                "count": row["n"],
                "latest": {
                    "post_id": latest["id"],
                    "date": latest["created_at"],
                    "url": _post_url(topic_id, latest["post_number"]),
                }
                if latest is not None
                else None,
            }
        )
    return {
        "topic_id": topic["id"],
        "title": topic["title"],
        "url": _topic_url(topic["id"]),
        "forum_id": topic["forum_id"],
        "created_at": topic["created_at"],
        "bumped_at": topic["bumped_at"],
        "posts_count": topic["posts_count"],
        "participants": participants,
        "has_accepted_answer": bool(topic["has_accepted_answer"]),
        "relevance_score": topic["relevance_score"],
        "op": {
            "post_id": op["id"],
            "author": op["username"],
            "date": op["created_at"],
            "url": _post_url(topic_id, op["post_number"]),
            "text": _snippet(op["raw"], 1500),
        }
        if op is not None
        else None,
        "repos": [{"repo": f"{row['owner']}/{row['name']}", "url": row["url"]} for row in repos],
        "hf_models": [row["repo_id"] for row in hf],
        "watch_hits": watch_hits,
        "recipes": [
            dict(row)
            for row in conn.execute(
                "SELECT id, model_canonical, quant_format, engine, topology, status FROM recipes"
                " WHERE id IN (SELECT target_id FROM evidence WHERE target_type='recipe'"
                " AND post_id IN (SELECT id FROM posts WHERE topic_id=?))",
                (topic_id,),
            )
        ],
        "benchmarks": [
            dict(row)
            for row in conn.execute(
                "SELECT id, model_canonical, benchmark, score FROM benchmark_results"
                " WHERE id IN (SELECT target_id FROM evidence WHERE target_type='benchmark'"
                " AND post_id IN (SELECT id FROM posts WHERE topic_id=?))",
                (topic_id,),
            )
        ],
        "notes": [
            dict(row)
            for row in conn.execute(
                "SELECT id, subject, as_of FROM research_notes WHERE id IN"
                " (SELECT target_id FROM evidence WHERE target_type='note'"
                " AND post_id IN (SELECT id FROM posts WHERE topic_id=?))",
                (topic_id,),
            )
        ],
        "flags": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM flags WHERE target_type='topic' AND target_id=?",
                (str(topic_id),),
            )
        ],
    }


@tool
def get_posts(
    topic_id: int,
    from_post: int | None = None,
    to_post: int | None = None,
    since: str | None = None,
    last_n: int | None = None,
    username: str | None = None,
    max_chars: int | None = None,
    next: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the raw text of a topic's posts.

    Use last_n for "what's the current state of this thread" and since for catching up.
    Long output is truncated; pass the returned `next` back to continue with no gaps or overlap.
    """
    settings = _settings()
    conn = _conn(settings)
    topic = conn.execute("SELECT id, title FROM topics WHERE id=?", (topic_id,)).fetchone()
    if topic is None:
        raise ValueError(f"unknown topic: {topic_id}")
    clauses = ["topic_id = ?"]
    params: list[Any] = [topic_id]
    if from_post is not None:
        clauses.append("post_number >= ?")
        params.append(from_post)
    if to_post is not None:
        clauses.append("post_number <= ?")
        params.append(to_post)
    if since is not None:
        clauses.append("(first_fetched_at >= ? OR created_at >= ?)")
        params.extend([since, since])
    if username is not None:
        clauses.append("username = ?")
        params.append(username)
    rows = conn.execute(
        f"SELECT * FROM posts WHERE {' AND '.join(clauses)} ORDER BY post_number",
        params,
    ).fetchall()
    if last_n is not None and last_n > 0:
        rows = rows[-last_n:]

    blocks = []
    for row in rows:
        blocks.append(
            {
                "post_id": row["id"],
                "post_number": row["post_number"],
                "author": row["username"],
                "date": row["created_at"],
                "url": _post_url(topic_id, row["post_number"]),
                "text": row["raw"] or "",
            }
        )
    rendered = "\n\n".join(
        f"--- post {block['post_number']} by {block['author']} ({block['date']})\n{block['text']}"
        for block in blocks
    )
    page = _page(rendered, next, _max_chars(max_chars, settings))
    return {
        "topic_id": topic_id,
        "title": topic["title"],
        "url": _topic_url(topic_id),
        "post_count": len(blocks),
        **page,
    }


@tool
def get_post(post_id: int, with_parent: bool = True, with_replies: bool = True) -> dict[str, Any]:
    """Return one post, optionally with the post it replies to and its direct replies."""
    settings = _settings()
    conn = _conn(settings)
    row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        return {"error": f"unknown post: {post_id}"}
    payload: dict[str, Any] = {
        "post_id": row["id"],
        "topic_id": row["topic_id"],
        "post_number": row["post_number"],
        "author": row["username"],
        "date": row["created_at"],
        "url": _post_url(row["topic_id"], row["post_number"]),
        "text": row["raw"],
    }
    if with_parent and row["reply_to_post_number"]:
        parent = conn.execute(
            "SELECT * FROM posts WHERE topic_id=? AND post_number=?",
            (row["topic_id"], row["reply_to_post_number"]),
        ).fetchone()
        if parent is not None:
            payload["parent"] = {
                "post_id": parent["id"],
                "author": parent["username"],
                "date": parent["created_at"],
                "url": _post_url(parent["topic_id"], parent["post_number"]),
                "text": _snippet(parent["raw"], 1500),
            }
    if with_replies:
        replies = conn.execute(
            "SELECT * FROM posts WHERE topic_id=? AND reply_to_post_number=? ORDER BY post_number",
            (row["topic_id"], row["post_number"]),
        ).fetchall()
        payload["replies"] = [
            {
                "post_id": reply["id"],
                "author": reply["username"],
                "date": reply["created_at"],
                "url": _post_url(reply["topic_id"], reply["post_number"]),
                "text": _snippet(reply["raw"], 800),
            }
            for reply in replies
        ]
    return payload


@tool
def list_repos(
    watched: bool | None = None, order: str = "mentions", limit: int | None = None
) -> dict[str, Any]:
    """List GitHub repos linked from forum posts, with mention counts.

    Use this to find repos worth watching.
    """
    settings = _settings()
    conn = _conn(settings)
    return {"repos": _list_repos(conn, watched=watched, order=order, limit=_limit(limit, settings))}


@tool
def get_repo(repo: str) -> dict[str, Any]:
    """Return one repo's metadata, watch settings, installs, forum mentions and recent events."""
    conn = _conn()
    payload = _get_repo(conn, repo)
    if payload is None:
        return {"error": f"unknown repo: {repo}"}
    return payload


@tool
def repo_status(
    repo: str | None = None, label: str = "default", since: str | None = None
) -> dict[str, Any]:
    """Compare what you have installed with the branch head, and list what changed.

    Answers "anything new since ...?" and "should I update?". Includes commits, releases,
    forum posts mentioning the repo since the install date, and open flags. Omit repo to
    cover every watched repo.
    """
    settings = _settings()
    conn = _conn(settings)
    return _repo_status(conn, settings, repo=repo, label=label, since=since)


@tool
def repo_events(since: str | None = None, repo: str | None = None) -> dict[str, Any]:
    """Return the raw repo event log (head moved, releases, tags, archived, branch gone)."""
    settings = _settings()
    conn = _conn(settings)
    return {"events": _repo_events(conn, since=since, repo=repo, limit=_limit(None, settings))}


@tool
def check_repos_now(repo: str | None = None) -> dict[str, Any]:
    """Check watched repos for a moved head, new releases and new tags right now."""
    settings = _settings()
    try:
        with write_lock("repos", settings.db_path):
            conn = _conn(settings)
            return check_repos(conn, settings, repo=repo)
    except LockBusy as exc:
        return exc.as_dict()


@tool
def list_hf_models(limit: int | None = None) -> dict[str, Any]:
    """List Hugging Face repos linked from posts, with mention counts.

    HF is recorded only and never polled: the forum is the curator for new models.
    """
    settings = _settings()
    conn = _conn(settings)
    rows = conn.execute(
        "SELECT h.repo_id, COUNT(m.post_id) AS mentions, MAX(m.mentioned_at) AS last_mention,"
        " h.first_seen_at FROM hf_models h LEFT JOIN hf_mentions m ON m.hf_model_id = h.id"
        " GROUP BY h.id ORDER BY mentions DESC, h.first_seen_at DESC LIMIT ?",
        (_limit(limit, settings),),
    ).fetchall()
    return {
        "hf_models": [
            {
                "repo_id": row["repo_id"],
                "url": f"https://huggingface.co/{row['repo_id']}",
                "mentions": row["mentions"],
                "last_mention": row["last_mention"],
                "first_seen_at": row["first_seen_at"],
            }
            for row in rows
        ]
    }


SQL_FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "load_extension",
)


def validate_sql(sql: str) -> str:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise ValueError("empty SQL")
    if ";" in text:
        raise ValueError("only a single statement is allowed")
    lowered = text.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("only SELECT or WITH statements are allowed")
    for word in SQL_FORBIDDEN:
        if re.search(rf"\b{word}\b", lowered):
            raise ValueError(f"{word.upper()} is not allowed")
    return text


@tool
def sql_query(sql: str, limit: int = 200) -> dict[str, Any]:
    """Run one read-only SELECT/WITH statement against the sparkwatch database.

    Use this for questions the other tools don't cover. Writes, PRAGMA and multiple
    statements are rejected, and queries time out after 2 seconds.
    """
    settings = _settings()
    statement = validate_sql(sql)
    cap = min(max(1, int(limit)), settings.mcp.max_limit)
    uri = f"file:{settings.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + 2.0
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        cursor = conn.execute(statement)
        rows = cursor.fetchmany(cap)
        columns = [column[0] for column in cursor.description or []]
        return {
            "columns": columns,
            "rows": [dict(zip(columns, tuple(row), strict=False)) for row in rows],
            "count": len(rows),
            "limit": cap,
        }
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            return {"error": "query timed out after 2s"}
        return {"error": str(exc)}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Knowledge: agent write-back
# --------------------------------------------------------------------------


@tool
def record_recipe(
    model: str,
    topology: str,
    status: str,
    evidence: list[dict[str, Any]] | None = None,
    quant: str | None = None,
    quant_repo: str | None = None,
    engine: str | None = None,
    engine_version: str | None = None,
    container_image: str | None = None,
    tensor_parallel: int | None = None,
    pipeline_parallel: int | None = None,
    context_len: int | None = None,
    tok_s_decode: float | None = None,
    tok_s_prefill: float | None = None,
    concurrency: int | None = None,
    memory_gb: float | None = None,
    setup: str | None = None,
    gotchas: str | None = None,
    supersedes: int | None = None,
) -> dict[str, Any]:
    """Record a serving recipe extracted from forum posts.

    Every quote in `evidence` is checked against the source post; if any quote is not found
    verbatim the recipe is stored with review='proposed'. The response reports normalised
    values, the fit warning, which quotes verified, and any similar existing recipes so you
    can supersede instead of duplicating.
    """
    settings = _settings()
    conn = _conn(settings)
    canonical, is_new = normalize.canonical_model(conn, model)
    quant_format = normalize.canonical_quant(quant)
    engine_name = normalize.canonical_engine(engine)
    topology_name = normalize.canonical_topology(topology)

    verified: list[dict[str, Any]] = []
    all_verified = True
    evidence = evidence or []
    newest_date: str | None = None
    for item in evidence:
        post_id = int(item.get("post_id"))
        quote = item.get("quote")
        ok = evidence_verify(conn, post_id, quote)
        verified.append({"post_id": post_id, "verified": ok})
        if not ok:
            all_verified = False
        row = conn.execute("SELECT created_at FROM posts WHERE id=?", (post_id,)).fetchone()
        if (
            row is not None
            and row["created_at"]
            and (newest_date is None or row["created_at"] > newest_date)
        ):
            newest_date = row["created_at"]

    model_row = conn.execute(
        "SELECT params_total_b FROM models WHERE canonical=?", (canonical,)
    ).fetchone()
    params = model_row["params_total_b"] if model_row is not None else None
    estimate, fit_ok, warning = fit.fit_check(
        conn, params, quant_format, topology_name, memory_gb, status, settings
    )
    now = utcnow_iso()
    review = "accepted" if all_verified else "proposed"
    cursor = conn.execute(
        "INSERT INTO recipes (model_canonical, model_raw, quant_format, quant_raw, quant_repo, engine,"
        " engine_version, container_image, topology, tensor_parallel, pipeline_parallel, context_len,"
        " tok_s_decode, tok_s_prefill, concurrency, memory_gb, status, setup, gotchas, fit_estimate_gb,"
        " fit_ok, reported_at, review, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'agent',?)",
        (
            canonical,
            model,
            quant_format,
            quant,
            quant_repo,
            engine_name,
            engine_version,
            container_image,
            topology_name,
            tensor_parallel,
            pipeline_parallel,
            context_len,
            tok_s_decode,
            tok_s_prefill,
            concurrency,
            memory_gb,
            status,
            setup,
            gotchas,
            estimate,
            fit_ok,
            newest_date or now,
            review,
            now,
        ),
    )
    recipe_id = cursor.lastrowid
    for item, _check in zip(evidence, verified, strict=False):
        evidence_record(conn, "recipe", recipe_id, int(item.get("post_id")), item.get("quote"))
    if supersedes:
        conn.execute("UPDATE recipes SET superseded_by=? WHERE id=?", (recipe_id, supersedes))

    similar = conn.execute(
        "SELECT id, model_canonical, quant_format, engine, topology, status, reported_at FROM recipes"
        " WHERE model_canonical=? AND topology=? AND COALESCE(quant_format,'')=COALESCE(?,'')"
        " AND COALESCE(engine,'')=COALESCE(?,'') AND id<>? ORDER BY reported_at DESC LIMIT 5",
        (canonical, topology_name, quant_format, engine_name, recipe_id),
    ).fetchall()
    return {
        "recipe_id": recipe_id,
        "model": canonical,
        "model_is_new": is_new,
        "quant": quant_format,
        "engine": engine_name,
        "topology": topology_name,
        "status": status,
        "review": review,
        "reported_at": newest_date or now,
        "fit": {"estimate_gb": estimate, "fit_ok": fit_ok, "warning": warning},
        "evidence": verified,
        "similar_recipes": [dict(row) for row in similar],
    }


def evidence_verify(conn: sqlite3.Connection, post_id: int, quote: str | None) -> bool:
    return evidence.verify_quote(conn, post_id, quote) if quote else False


def evidence_record(
    conn: sqlite3.Connection, target_type: str, target_id: int, post_id: int, quote: str | None
) -> bool:
    return evidence.record_evidence(conn, target_type, target_id, post_id, quote)


@tool
def record_benchmark(
    model: str,
    benchmark: str,
    score: float,
    source_type: str,
    quant: str | None = None,
    score_unit: str | None = None,
    measured_on: str | None = None,
    engine: str | None = None,
    source_url: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
    measured_at: str | None = None,
) -> dict[str, Any]:
    """Record a benchmark result. Quotes are verified when source_type='forum'."""
    settings = _settings()
    conn = _conn(settings)
    canonical, is_new = normalize.canonical_model(conn, model)
    benchmark_name = normalize.canonical_benchmark(benchmark)
    quant_format = normalize.canonical_quant(quant)
    engine_name = normalize.canonical_engine(engine)
    verified: list[dict[str, Any]] = []
    all_verified = True
    for item in evidence or []:
        post_id = int(item.get("post_id"))
        ok = evidence_verify(conn, post_id, item.get("quote"))
        verified.append({"post_id": post_id, "verified": ok})
        if not ok:
            all_verified = False
    review = "accepted" if (all_verified or source_type != "forum") else "proposed"
    now = utcnow_iso()
    cursor = conn.execute(
        "INSERT INTO benchmark_results (model_canonical, quant_format, benchmark, score, score_unit,"
        " higher_is_better, source_type, source_url, measured_on, engine, measured_at, review,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'agent',?)",
        (
            canonical,
            quant_format,
            benchmark_name,
            float(score),
            score_unit,
            1,
            source_type,
            source_url,
            measured_on,
            engine_name,
            measured_at,
            review,
            now,
        ),
    )
    benchmark_id = cursor.lastrowid
    for item, _check in zip(evidence or [], verified, strict=False):
        evidence_record(
            conn, "benchmark", benchmark_id, int(item.get("post_id")), item.get("quote")
        )
    return {
        "benchmark_id": benchmark_id,
        "model": canonical,
        "model_is_new": is_new,
        "benchmark": benchmark_name,
        "quant": quant_format,
        "score": score,
        "review": review,
        "evidence": verified,
    }


@tool
def list_recipes(
    model: str | None = None,
    engine: str | None = None,
    topology: str | None = None,
    status: str | None = None,
    since: str | None = None,
    include_superseded: bool = False,
    review: str = "accepted",
    limit: int | None = None,
) -> dict[str, Any]:
    """List recorded recipes, newest first, each with its evidence URLs.

    Call this (and get_notes) before researching a model, so you build on earlier conclusions.
    """
    settings = _settings()
    conn = _conn(settings)
    clauses: list[str] = []
    params: list[Any] = []
    if model:
        canonical, _ = normalize.canonical_model(conn, model)
        clauses.append("model_canonical = ?")
        params.append(canonical)
    if engine:
        clauses.append("engine = ?")
        params.append(normalize.canonical_engine(engine))
    if topology:
        clauses.append("topology = ?")
        params.append(normalize.canonical_topology(topology))
    if status:
        clauses.append("status = ?")
        params.append(status)
    if since:
        clauses.append("reported_at >= ?")
        params.append(since)
    if not include_superseded:
        clauses.append("superseded_by IS NULL")
    if review:
        clauses.append("review = ?")
        params.append(review)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM recipes{where} ORDER BY reported_at DESC, id DESC LIMIT ?",
        (*params, _limit(limit, settings)),
    ).fetchall()
    return {"count": len(rows), "recipes": [_recipe_row(conn, row["id"]) for row in rows]}


@tool
def list_benchmarks(
    model: str | None = None,
    benchmark: str | None = None,
    quant: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List recorded benchmark results."""
    settings = _settings()
    conn = _conn(settings)
    clauses: list[str] = []
    params: list[Any] = []
    if model:
        canonical, _ = normalize.canonical_model(conn, model)
        clauses.append("model_canonical = ?")
        params.append(canonical)
    if benchmark:
        clauses.append("benchmark = ?")
        params.append(normalize.canonical_benchmark(benchmark))
    if quant:
        clauses.append("quant_format = ?")
        params.append(normalize.canonical_quant(quant))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM benchmark_results{where} ORDER BY created_at DESC LIMIT ?",
        (*params, _limit(limit, settings)),
    ).fetchall()
    return {"count": len(rows), "benchmarks": [dict(row) for row in rows]}


@tool
def model_summary(model: str) -> dict[str, Any]:
    """The starting point for a "best way to serve X" question.

    Returns metadata, aliases, mention counts over 7/30/60 days, active recipes newest first,
    benchmarks, latest research notes, open flags, repos mentioned in the model's topics and
    the topics with the most mentions.
    """
    settings = _settings()
    conn = _conn(settings)
    canonical, is_new = normalize.canonical_model(conn, model)
    pattern = _model_pattern(conn, canonical)
    meta = conn.execute("SELECT * FROM models WHERE canonical=?", (canonical,)).fetchone()
    aliases = [
        row["alias"]
        for row in conn.execute("SELECT alias FROM model_aliases WHERE canonical=?", (canonical,))
    ]
    posts = conn.execute(
        "SELECT p.id, p.topic_id, p.post_number, p.raw, p.created_at, t.title FROM posts p"
        " JOIN topics t ON t.id = p.topic_id"
    ).fetchall()
    now = utcnow()
    counts = {"7d": 0, "30d": 0, "60d": 0}
    topic_counts: dict[int, dict[str, Any]] = {}
    for row in posts:
        if not (pattern.search(row["raw"] or "") or pattern.search(row["title"] or "")):
            continue
        written = parse_dt(row["created_at"])
        if written is not None:
            age_days = (now - written).total_seconds() / 86400
            for key, days in (("7d", 7), ("30d", 30), ("60d", 60)):
                if age_days <= days:
                    counts[key] += 1
        bucket = topic_counts.setdefault(
            row["topic_id"],
            {
                "topic_id": row["topic_id"],
                "title": row["title"],
                "mentions": 0,
                "url": _topic_url(row["topic_id"]),
            },
        )
        bucket["mentions"] += 1

    recipes = list_recipes(model=canonical, limit=10)
    benchmarks = list_benchmarks(model=canonical, limit=10)
    notes = get_notes(model=canonical, include_superseded=False)
    flags = [flag for flag in _flag_rows(conn) if flag["target_id"] in (canonical, model)]
    repo_rows = conn.execute(
        "SELECT DISTINCT r.owner, r.name, r.url, r.watch, r.head_sha FROM repos r"
        " JOIN repo_mentions m ON m.repo_id = r.id"
        " JOIN posts p ON p.id = m.post_id WHERE p.topic_id IN ("
        "   SELECT id FROM topics WHERE id IN (SELECT topic_id FROM posts WHERE id IN"
        "   (SELECT post_id FROM repo_mentions)))"
    ).fetchall()
    return {
        "model": canonical,
        "model_is_new": is_new,
        "metadata": dict(meta) if meta is not None else None,
        "aliases": aliases,
        "mentions": counts,
        "recipes": recipes["recipes"],
        "benchmarks": benchmarks["benchmarks"],
        "notes": notes["notes"],
        "flags": flags,
        "repos": [
            {
                "repo": f"{row['owner']}/{row['name']}",
                "url": row["url"],
                "watched": bool(row["watch"]),
                "head_sha": row["head_sha"],
            }
            for row in repo_rows
        ],
        "top_topics": sorted(
            topic_counts.values(), key=lambda item: item["mentions"], reverse=True
        )[:10],
    }


@tool
def save_note(
    subject: str,
    body: str,
    model: str | None = None,
    as_of: str | None = None,
    source_post_ids: list[int] | None = None,
    supersedes: int | None = None,
) -> dict[str, Any]:
    """Save a research note so the next session starts from your conclusions.

    Use it after answering a "best way to serve X" or "should I update" question.
    """
    conn = _conn()
    canonical = None
    if model:
        canonical, _ = normalize.canonical_model(conn, model)
    now = utcnow_iso()
    cursor = conn.execute(
        "INSERT INTO research_notes (subject, model_canonical, body, as_of, created_at, superseded_by)"
        " VALUES (?, ?, ?, ?, ?, NULL)",
        (subject, canonical, body, as_of or now, now),
    )
    note_id = cursor.lastrowid
    for post_id in source_post_ids or []:
        row = conn.execute("SELECT raw FROM posts WHERE id=?", (post_id,)).fetchone()
        quote = _snippet(row["raw"], 200) if row is not None else None
        evidence_record(conn, "note", note_id, post_id, quote)
    if supersedes:
        conn.execute("UPDATE research_notes SET superseded_by=? WHERE id=?", (note_id, supersedes))
    return {"note_id": note_id, "subject": subject, "model": canonical, "as_of": as_of or now}


@tool
def get_notes(
    model: str | None = None,
    subject_query: str | None = None,
    include_superseded: bool = False,
) -> dict[str, Any]:
    """Return saved research notes, newest first.

    Call this before researching a model, so you build on earlier conclusions instead of
    repeating work.
    """
    conn = _conn()
    clauses: list[str] = []
    params: list[Any] = []
    if model:
        canonical, _ = normalize.canonical_model(conn, model)
        clauses.append("model_canonical = ?")
        params.append(canonical)
    if subject_query:
        clauses.append("subject LIKE ?")
        params.append(f"%{subject_query}%")
    if not include_superseded:
        clauses.append("superseded_by IS NULL")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM research_notes{where} ORDER BY created_at DESC LIMIT 50", params
    ).fetchall()
    return {"count": len(rows), "notes": [dict(row) for row in rows]}


@tool
def flag(
    target_type: str,
    target_id: str,
    label: str,
    reason: str,
    priority: int = 2,
) -> dict[str, Any]:
    """Raise a flag on anything worth the user's attention.

    Labels: new-model, perf-claim, breaking-change, recipe, question-for-me. Priority 1 is
    urgent, 2 normal, 3 low.
    """
    conn = _conn()
    if target_type not in ("topic", "post", "repo", "hf_model", "recipe", "model"):
        return {"error": f"invalid target_type: {target_type}"}
    priority = max(1, min(int(priority), 3))
    cursor = conn.execute(
        "INSERT INTO flags (target_type, target_id, label, reason, priority, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (target_type, str(target_id), label, reason, priority, utcnow_iso()),
    )
    return {"flag_id": cursor.lastrowid, "label": label, "priority": priority}


@tool
def list_flags(open_only: bool = True) -> dict[str, Any]:
    """List flags, open by default, most important first."""
    conn = _conn()
    rows = _flag_rows(conn, open_only=open_only)
    return {"count": len(rows), "flags": rows}


@tool
def resolve_flag(flag_id: int, resolution: str) -> dict[str, Any]:
    """Close a flag with a note on how it was resolved."""
    conn = _conn()
    cursor = conn.execute(
        "UPDATE flags SET resolved_at=?, resolution=? WHERE id=? AND resolved_at IS NULL",
        (utcnow_iso(), resolution, flag_id),
    )
    return {"flag_id": flag_id, "resolved": bool(cursor.rowcount)}


@tool
def review_proposed(kind: str = "recipe") -> dict[str, Any]:
    """List items waiting for review: proposed recipes, benchmarks or unreviewed aliases."""
    conn = _conn()
    if kind == "alias":
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM model_aliases WHERE source<>'seed' AND reviewed=0 ORDER BY alias"
            )
        ]
    elif kind == "benchmark":
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM benchmark_results WHERE review='proposed' ORDER BY created_at DESC"
            )
        ]
    else:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM recipes WHERE review='proposed' ORDER BY created_at DESC"
            )
        ]
    return {"kind": kind, "count": len(rows), "items": rows}


@tool
def set_review(kind: str, target_id: int, review: str) -> dict[str, Any]:
    """Accept or reject a proposed recipe or benchmark."""
    conn = _conn()
    if review not in ("proposed", "accepted", "rejected"):
        return {"error": f"invalid review: {review}"}
    table = {"recipe": "recipes", "benchmark": "benchmark_results"}.get(kind)
    if table is None:
        return {"error": f"invalid kind: {kind}"}
    cursor = conn.execute(f"UPDATE {table} SET review=? WHERE id=?", (review, target_id))
    return {
        "kind": kind,
        "target_id": target_id,
        "review": review,
        "updated": bool(cursor.rowcount),
    }


@tool
def add_model_alias(alias: str, canonical: str) -> dict[str, Any]:
    """Teach sparkwatch that an alias means a canonical model name."""
    conn = _conn()
    key = normalize.clean_model_key(alias)
    target = normalize.clean_model_key(canonical)
    conn.execute(
        "INSERT INTO model_aliases (alias, canonical, source, reviewed) VALUES (?, ?, 'agent', 1)"
        " ON CONFLICT(alias) DO UPDATE SET canonical=excluded.canonical, source='agent', reviewed=1",
        (key, target),
    )
    return {"alias": key, "canonical": target}


@tool
def upsert_model(
    canonical: str,
    family: str | None = None,
    params_total_b: float | None = None,
    params_active_b: float | None = None,
    released_at: str | None = None,
    hf_repo_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Create or update a model's metadata (family, parameter counts, HF repo)."""
    conn = _conn()
    key = normalize.clean_model_key(canonical)
    conn.execute(
        "INSERT INTO models (canonical, family, params_total_b, params_active_b, released_at,"
        " hf_repo_id, note, updated_at) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(canonical) DO UPDATE SET"
        " family=COALESCE(excluded.family, models.family),"
        " params_total_b=COALESCE(excluded.params_total_b, models.params_total_b),"
        " params_active_b=COALESCE(excluded.params_active_b, models.params_active_b),"
        " released_at=COALESCE(excluded.released_at, models.released_at),"
        " hf_repo_id=COALESCE(excluded.hf_repo_id, models.hf_repo_id),"
        " note=COALESCE(excluded.note, models.note), updated_at=excluded.updated_at",
        (key, family, params_total_b, params_active_b, released_at, hf_repo_id, note, utcnow_iso()),
    )
    return dict(conn.execute("SELECT * FROM models WHERE canonical=?", (key,)).fetchone())


@tool
def list_watchlist() -> dict[str, Any]:
    """List watched models with hit counts over 7 and 30 days and first-seen dates."""
    conn = _conn()
    return {"watchlist": watch_mod.list_watchlist(conn)}


@tool
def add_watch(
    label: str,
    match_regex: str,
    kind: str = "tracking",
    canonical: str | None = None,
) -> dict[str, Any]:
    """Watch a model. kind='anticipated' alerts on the first sighting, before release."""
    conn = _conn()
    try:
        return watch_mod.add_watch(conn, label, match_regex, kind=kind, canonical=canonical)
    except ValueError as exc:
        return {"error": str(exc)}


@tool
def update_watch(
    label: str,
    match_regex: str | None = None,
    kind: str | None = None,
    canonical: str | None = None,
    active: bool | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Change a watch entry's regex, kind, canonical name or active state.

    Changing the regex re-matches stored posts locally; no fetching happens.
    """
    conn = _conn()
    try:
        row = watch_mod.update_watch(
            conn,
            label,
            match_regex=match_regex,
            kind=kind,
            canonical=canonical,
            active=active,
            note=note,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return row if row is not None else {"error": f"unknown watch label: {label}"}


@tool
def watch_repo(
    repo: str, watch: bool = True, branch: str | None = None, note: str | None = None
) -> dict[str, Any]:
    """Watch (or stop watching) a GitHub repo. Accepts owner/name or a URL.

    Watching runs a baseline check straight away: the first check records the current head,
    release and tags without creating events.
    """
    settings = _settings()
    conn = _conn(settings)
    return _watch_repo(conn, repo, watch=watch, branch=branch, note=note, settings=settings)


@tool
def set_installed(
    repo: str,
    ref: str,
    label: str = "default",
    installed_at: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record the SHA you are actually running, so repo_status can compare against it.

    ref may be a full or short SHA, a tag or a branch name; it is resolved to a full SHA.
    """
    settings = _settings()
    conn = _conn(settings)
    return _set_installed(
        conn, repo, ref, label=label, installed_at=installed_at, note=note, settings=settings
    )


@tool
def clear_installed(repo: str, label: str = "default") -> dict[str, Any]:
    """Forget a recorded install."""
    conn = _conn()
    return _clear_installed(conn, repo, label=label)


@tool
def list_installs() -> dict[str, Any]:
    """List every recorded install with its distance from the branch head."""
    settings = _settings()
    conn = _conn(settings)
    return {"installs": _list_installs(conn, settings)}


@tool
def mark_topic(topic_id: int, relevant: bool | None = None) -> dict[str, Any]:
    """Force a topic to be treated as relevant (True), irrelevant (False) or automatic (None)."""
    conn = _conn()
    value = None if relevant is None else (1 if relevant else 0)
    cursor = conn.execute("UPDATE topics SET relevance_override=? WHERE id=?", (value, topic_id))
    if not cursor.rowcount:
        return {"error": f"unknown topic: {topic_id}"}
    return {"topic_id": topic_id, "relevance_override": value}


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------


@mcp.resource("sparkwatch://watchlist", description="Watched models with recent hit counts")
def resource_watchlist() -> str:
    conn = _conn()
    return json.dumps(watch_mod.list_watchlist(conn), default=str)


@mcp.resource("sparkwatch://flags/open", description="Open flags")
def resource_flags() -> str:
    conn = _conn()
    return json.dumps(_flag_rows(conn, open_only=True), default=str)


@mcp.resource("sparkwatch://notes/latest", description="Latest research notes")
def resource_notes() -> str:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM research_notes WHERE superseded_by IS NULL ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    return json.dumps([dict(row) for row in rows], default=str)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


@mcp.prompt(title="catch_up")
def catch_up() -> str:
    """Catch me up on the DGX Spark forums."""
    return (
        "Catch up on the NVIDIA DGX Spark / GB10 forums.\n"
        "1. Call sync_now, and call it again while it returns partial=true.\n"
        "2. Call digest to see what changed since your last look.\n"
        "3. For promising items, read them with get_posts(last_n=...) or get_post.\n"
        "4. Flag what matters with flag: new models or quants, performance claims, breaking engine"
        " changes, new recipes, watch sightings.\n"
        "5. Record clear recipes and benchmarks with record_recipe / record_benchmark, quoting the"
        " posts verbatim.\n"
        "6. Reply with a short briefing and a source link for every claim."
    )


@mcp.prompt(title="serve_model")
def serve_model(model: str, topology: str = "dual-spark") -> str:
    """Work out the best way to serve a model on DGX Spark hardware."""
    return (
        f"What is the best way to serve {model} on {topology} today?\n"
        "1. Call model_summary and get_notes first, so you build on earlier conclusions.\n"
        "2. Call whats_new_for for the model, since the latest note's as_of.\n"
        "3. Read the newest relevant posts first with get_posts / get_post. Prefer recent reports,"
        " multiple users and accepted answers. Watch for contradictions and note which engine"
        " versions were used.\n"
        "4. Answer with the recommended quant, engine and version, container, launch command and"
        " flags, expected tok/s, gotchas, alternatives, and a source link and date per claim.\n"
        "5. Then call record_recipe / record_benchmark with verbatim quotes, and save_note"
        " (superseding the previous note)."
    )


@mcp.prompt(title="should_i_update")
def should_i_update(repo: str = "", label: str = "default") -> str:
    """Decide whether to update a watched repo."""
    target = repo or "all watched repos"
    return (
        f"Should I update {target} (install label {label})?\n"
        "1. Call repo_status.\n"
        "2. For each repo that is behind, read the commit subjects and release notes. Pick out"
        " fixes and features relevant to GB10/Spark, your models and your engine; note breaking"
        " changes, dependency or CUDA bumps, and reverts.\n"
        "3. Read the forum posts mentioning the repo since the installed date, looking for reports"
        " that newer commits fix or break things.\n"
        "4. Recommend one of: update now to <sha or tag>, wait for <reason>, or no relevant changes."
        " Give the reasoning and links.\n"
        "5. flag anything urgent, and save_note(subject='update check <repo>')."
    )


@mcp.prompt(title="frontier")
def frontier(topology: str = "dual-spark") -> str:
    """Ask what the strongest models on two Sparks are right now."""
    return (
        f"What are the strongest models people run on {topology} right now?\n"
        "1. Call list_watchlist, list_recipes(status='works'), list_benchmarks and get_notes.\n"
        "2. Fill gaps with find_model_mentions for heavily mentioned models with no recipes yet.\n"
        "3. Answer with a ranked list and its reasoning, showing quant and benchmark evidence.\n"
        "4. Save the conclusion with save_note(subject='frontier " + topology + "')."
    )


def main() -> None:
    # Refuse to start on an unsafe configuration (e.g. an unlimited rate against the live
    # forum) rather than failing on the first tool call.
    try:
        _settings()
    except ValidationError as exc:
        raise SystemExit(f"sparkwatch: {describe_validation_error(exc)}") from None
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
