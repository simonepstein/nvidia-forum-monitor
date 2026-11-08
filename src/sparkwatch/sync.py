from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import filters, links, watch
from .config import CategorySettings, Settings, get_settings
from .db import open_db
from .discourse import DiscourseClient, DiscourseError, ForbiddenError, NotFoundError
from .util import dumps, parse_dt, to_iso, utcnow, utcnow_iso

STORED_POST_TYPES = {1, 2}


@dataclass
class SyncResult:
    requests: int = 0
    new_topic_ids: list[int] = field(default_factory=list)
    updated_topic_ids: list[int] = field(default_factory=list)
    posts_new: int = 0
    posts_changed: int = 0
    watch_hits_new: int = 0
    tombstones_new: int = 0
    partial: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "new_topic_ids": self.new_topic_ids,
            "updated_topic_ids": self.updated_topic_ids,
            "posts_new": self.posts_new,
            "posts_changed": self.posts_changed,
            "watch_hits_new": self.watch_hits_new,
            "tombstones_new": self.tombstones_new,
            "partial": self.partial,
            "errors": self.errors,
        }


def _window(
    conn: sqlite3.Connection, category: CategorySettings, lookback_days: int, now: datetime
):
    forum = conn.execute("SELECT * FROM forums WHERE id=?", (category.id,)).fetchone()
    if forum is None:
        conn.execute(
            "INSERT INTO forums (id, path, name) VALUES (?, ?, ?)",
            (category.id, category.path, category.name),
        )
        forum = conn.execute("SELECT * FROM forums WHERE id=?", (category.id,)).fetchone()
    previous_floor = parse_dt(forum["backfill_floor"])
    last_synced = parse_dt(forum["last_synced_at"])
    window_floor = now - timedelta(days=lookback_days)
    floor = min(previous_floor, window_floor) if previous_floor is not None else window_floor
    backfill = previous_floor is not None and floor < previous_floor
    first_run = last_synced is None
    cutoff = window_floor if (first_run or backfill) else last_synced
    return floor, cutoff, backfill, first_run


def _upsert_topic(
    conn: sqlite3.Connection, forum_id: int, topic: dict[str, Any], backfill: bool
) -> str | None:
    now = utcnow_iso()
    existing = conn.execute(
        "SELECT id, highest_post_number, bumped_at FROM topics WHERE id=?", (topic["id"],)
    ).fetchone()
    payload = {
        "id": topic["id"],
        "forum_id": forum_id,
        "slug": topic.get("slug"),
        "title": topic.get("title") or "",
        "tags": dumps(topic.get("tags") or []),
        "excerpt": topic.get("excerpt"),
        "created_at": topic.get("created_at"),
        "bumped_at": topic.get("bumped_at"),
        "last_posted_at": topic.get("last_posted_at"),
        "posts_count": topic.get("posts_count"),
        "highest_post_number": topic.get("highest_post_number"),
        "views": topic.get("views"),
        "like_count": topic.get("like_count"),
        "pinned": 1 if topic.get("pinned") else 0,
        "has_accepted_answer": 1 if topic.get("has_accepted_answer") else 0,
    }
    if existing is None:
        conn.execute(
            "INSERT INTO topics (id, forum_id, slug, title, tags, excerpt, created_at, bumped_at,"
            " last_posted_at, posts_count, highest_post_number, views, like_count, pinned,"
            " has_accepted_answer, relevance_score, first_fetched_at, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (
                payload["id"],
                payload["forum_id"],
                payload["slug"],
                payload["title"],
                payload["tags"],
                payload["excerpt"],
                payload["created_at"],
                payload["bumped_at"],
                payload["last_posted_at"],
                payload["posts_count"],
                payload["highest_post_number"],
                payload["views"],
                payload["like_count"],
                payload["pinned"],
                payload["has_accepted_answer"],
                now,
                now,
            ),
        )
        return "new"

    conn.execute(
        "UPDATE topics SET forum_id=?, slug=?, title=?, tags=?, excerpt=?, created_at=?, bumped_at=?,"
        " last_posted_at=?, posts_count=?, highest_post_number=?, views=?, like_count=?, pinned=?,"
        " has_accepted_answer=?, fetched_at=? WHERE id=?",
        (
            payload["forum_id"],
            payload["slug"],
            payload["title"],
            payload["tags"],
            payload["excerpt"],
            payload["created_at"],
            payload["bumped_at"],
            payload["last_posted_at"],
            payload["posts_count"],
            payload["highest_post_number"],
            payload["views"],
            payload["like_count"],
            payload["pinned"],
            payload["has_accepted_answer"],
            now,
            payload["id"],
        ),
    )
    bumped_changed = payload["bumped_at"] != existing["bumped_at"]
    highest_up = (payload["highest_post_number"] or 0) > (existing["highest_post_number"] or 0)
    return "updated" if (bumped_changed or highest_up or backfill) else None


def _list_topics(
    conn: sqlite3.Connection,
    client: DiscourseClient,
    category: CategorySettings,
    max_pages: int | None,
    floor: datetime,
    cutoff: datetime,
    backfill: bool,
    result: SyncResult,
) -> list[int]:
    queued: list[int] = []
    page = 0
    while True:
        if max_pages is not None and page >= max_pages:
            break
        data = client.list_category_page(category.id, page)
        topic_list = data.get("topic_list") or {}
        topics = topic_list.get("topics") or []
        more = topic_list.get("more_topics_url")
        non_pinned = [topic for topic in topics if not topic.get("pinned")]

        for topic in topics:
            bumped = parse_dt(topic.get("bumped_at"))
            if not topic.get("pinned") and bumped is not None and bumped < floor:
                continue
            outcome = _upsert_topic(conn, category.id, topic, backfill)
            if outcome == "new":
                result.new_topic_ids.append(int(topic["id"]))
                queued.append(int(topic["id"]))
            elif outcome == "updated":
                result.updated_topic_ids.append(int(topic["id"]))
                queued.append(int(topic["id"]))
            else:
                topic_id = int(topic["id"])
                complete = conn.execute(
                    "SELECT posts_complete FROM topics WHERE id=?", (topic_id,)
                ).fetchone()
                if complete is None or not complete["posts_complete"]:
                    result.updated_topic_ids.append(topic_id)
                    queued.append(topic_id)

        if non_pinned:
            oldest = parse_dt(non_pinned[-1].get("bumped_at"))
            if oldest is not None and oldest < cutoff:
                break
        if not more:
            break
        page += 1
    return queued


def _tombstone(
    conn: sqlite3.Connection,
    topic_id: int,
    post_id: int,
    reason: str,
    now_iso: str,
    result: SyncResult,
) -> None:
    existing = conn.execute(
        "SELECT 1 FROM missing_posts WHERE topic_id=? AND post_id=?", (topic_id, post_id)
    ).fetchone()
    conn.execute(
        "INSERT INTO missing_posts (topic_id, post_id, reason, first_seen_at, last_checked_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(topic_id, post_id) DO UPDATE SET reason=excluded.reason,"
        " last_checked_at=excluded.last_checked_at",
        (topic_id, post_id, reason, now_iso, now_iso),
    )
    if existing is None:
        result.tombstones_new += 1


def _upsert_post(
    conn: sqlite3.Connection, topic_id: int, post: dict[str, Any], now_iso: str
) -> str:
    raw = post.get("raw") or ""
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    reactions = post.get("reactions")
    if isinstance(reactions, dict):
        like_count = reactions.get("like_count", post.get("like_count", 0)) or 0
    else:
        like_count = post.get("like_count") or 0
    existing = conn.execute("SELECT content_hash FROM posts WHERE id=?", (post["id"],)).fetchone()
    values = (
        topic_id,
        post.get("post_number"),
        post.get("post_type", 1),
        post.get("username"),
        post.get("trust_level"),
        1 if post.get("staff") else 0,
        post.get("created_at"),
        post.get("updated_at"),
        post.get("version"),
        post.get("reply_to_post_number"),
        like_count,
        1 if post.get("accepted_answer") else 0,
        post.get("cooked"),
        raw,
        content_hash,
    )
    if existing is None:
        conn.execute(
            "INSERT INTO posts (id, topic_id, post_number, post_type, username, trust_level, staff,"
            " created_at, updated_at, version, reply_to_post_number, like_count, accepted_answer,"
            " cooked, raw, content_hash, first_fetched_at, changed_at, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (post["id"], *values, now_iso, now_iso, now_iso),
        )
        return "new"
    if existing["content_hash"] != content_hash:
        conn.execute(
            "UPDATE posts SET topic_id=?, post_number=?, post_type=?, username=?, trust_level=?,"
            " staff=?, created_at=?, updated_at=?, version=?, reply_to_post_number=?, like_count=?,"
            " accepted_answer=?, cooked=?, raw=?, content_hash=?, fetched_at=?, changed_at=?"
            " WHERE id=?",
            (*values, now_iso, now_iso, post["id"]),
        )
        return "changed"
    conn.execute("UPDATE posts SET fetched_at=? WHERE id=?", (now_iso, post["id"]))
    return "same"


def _sync_topic(
    conn: sqlite3.Connection,
    client: DiscourseClient,
    settings: Settings,
    topic_id: int,
    floor: datetime,
    backfill: bool,
    result: SyncResult,
    now: datetime,
    deadline: float | None,
) -> None:
    now_iso = to_iso(now)
    data = client.get_topic(topic_id)
    post_stream = data.get("post_stream") or {}
    topic_posts = post_stream.get("posts") or []
    stream = [int(pid) for pid in (post_stream.get("stream") or [])]
    if not stream:
        stream = [int(post["id"]) for post in topic_posts]

    stored = {
        row["id"]: row
        for row in conn.execute("SELECT id, created_at FROM posts WHERE topic_id=?", (topic_id,))
    }
    missing = {
        row["post_id"]: row
        for row in conn.execute(
            "SELECT post_id, last_checked_at FROM missing_posts WHERE topic_id=?", (topic_id,)
        )
    }
    refetch_cut = now - timedelta(days=settings.sync.refetch_recent_days)
    recheck_cut = now - timedelta(days=settings.sync.missing_post_recheck_days)

    targets: list[int] = []
    for post_id in stream:
        if post_id in stored:
            created = parse_dt(stored[post_id]["created_at"])
            if created is not None and created >= refetch_cut:
                targets.append(post_id)
        elif post_id in missing:
            last_checked = parse_dt(missing[post_id]["last_checked_at"])
            if backfill or last_checked is None or last_checked < recheck_cut:
                targets.append(post_id)
        else:
            targets.append(post_id)

    op_id = stream[0] if stream else None
    if op_id is not None and op_id not in targets:
        targets.append(op_id)
    target_set = set(targets)

    fetched: dict[int, dict[str, Any]] = {}
    for post in topic_posts:
        post_id = int(post["id"])
        if post_id in target_set:
            fetched[post_id] = post

    ordered = [post_id for post_id in reversed(stream) if post_id in target_set]
    batch_size = max(1, settings.forum.posts_batch_size)
    batches = [ordered[i : i + batch_size] for i in range(0, len(ordered), batch_size)]

    interrupted = False
    for batch in batches:
        if deadline is not None and time.monotonic() > deadline:
            interrupted = True
            break
        pending = [post_id for post_id in batch if post_id not in fetched]
        if not pending:
            continue
        try:
            payload = client.get_posts(topic_id, pending)
        except ForbiddenError:
            for post_id in pending:
                _tombstone(conn, topic_id, post_id, "forbidden", now_iso, result)
            continue
        returned = {
            int(post["id"]): post for post in (payload.get("post_stream") or {}).get("posts") or []
        }
        for post_id in pending:
            if post_id not in returned:
                _tombstone(conn, topic_id, post_id, "absent_from_batch", now_iso, result)
        fetched.update(returned)

        if op_id not in batch and returned:
            dates = [parse_dt(post.get("created_at")) for post in returned.values()]
            if dates and all(date is not None and date < floor for date in dates):
                break

    for post_id, post in fetched.items():
        if post.get("deleted_at"):
            _tombstone(conn, topic_id, post_id, "deleted", now_iso, result)
            continue
        if post.get("hidden"):
            _tombstone(conn, topic_id, post_id, "hidden", now_iso, result)
            continue
        if int(post.get("post_type", 1)) not in STORED_POST_TYPES:
            _tombstone(conn, topic_id, post_id, "hidden", now_iso, result)
            continue

        outcome = _upsert_post(conn, topic_id, post, now_iso)
        conn.execute(
            "DELETE FROM missing_posts WHERE topic_id=? AND post_id=?", (topic_id, post_id)
        )
        if outcome == "new":
            result.posts_new += 1
        elif outcome == "changed":
            result.posts_changed += 1
        else:
            continue

        stored_row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        links.discover(conn, stored_row)
        topic_row = conn.execute("SELECT title FROM topics WHERE id=?", (topic_id,)).fetchone()
        hits = watch.match_post(
            conn,
            post_id,
            topic_id,
            topic_row["title"] if topic_row else None,
            stored_row["raw"],
        )
        result.watch_hits_new += len(hits)

    conn.execute(
        "UPDATE topics SET posts_complete=? WHERE id=?",
        (0 if interrupted else 1, topic_id),
    )
    filters.recompute_topic(conn, topic_id, settings)


def sync(
    settings: Settings | None = None,
    forum_ids: list[int] | None = None,
    max_pages: int | None = None,
    lookback_days: int | None = None,
    time_budget_s: float | None = None,
    client: DiscourseClient | None = None,
    conn: sqlite3.Connection | None = None,
    trigger: str = "cli",
) -> SyncResult:
    settings = settings or get_settings()
    conn = conn if conn is not None else open_db(settings)
    owns_client = client is None
    client = client or DiscourseClient(settings)
    result = SyncResult()
    started = utcnow()
    started_iso = to_iso(started)
    lookback = lookback_days if lookback_days is not None else settings.sync.lookback_days
    deadline = time.monotonic() + time_budget_s if time_budget_s else None

    categories = [
        category
        for category in settings.forum.categories
        if forum_ids is None or category.id in forum_ids
    ]

    floors: dict[int, tuple[datetime, datetime]] = {}
    try:
        for category in categories:
            floor, cutoff, backfill, _first_run = _window(conn, category, lookback, started)
            floors[category.id] = (floor, cutoff)
            queued = _list_topics(
                conn, client, category, max_pages, floor, cutoff, backfill, result
            )
            for topic_id in queued:
                if deadline is not None and time.monotonic() > deadline:
                    result.partial = True
                    break
                try:
                    _sync_topic(
                        conn, client, settings, topic_id, floor, backfill, result, started, deadline
                    )
                except (NotFoundError, ForbiddenError) as exc:
                    result.errors.append(f"topic {topic_id}: {exc}")
                except DiscourseError as exc:
                    result.errors.append(f"topic {topic_id}: {exc}")
                except Exception as exc:  # noqa: BLE001 - one topic must not stop the sync
                    result.errors.append(f"topic {topic_id}: {exc!r}")
            if result.partial:
                break
    finally:
        result.requests = client.request_count
        if owns_client:
            client.close()

    if not result.partial:
        for category in categories:
            floor, _cutoff = floors.get(category.id, (None, None))
            if floor is None:
                continue
            conn.execute(
                "UPDATE forums SET last_synced_at=?, backfill_floor=? WHERE id=?",
                (to_iso(started), to_iso(floor), category.id),
            )

    conn.execute(
        'INSERT INTO sync_runs (started_at, finished_at, "trigger", requests, new_topic_ids,'
        " updated_topic_ids, posts_new, posts_changed, partial, errors)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            started_iso,
            utcnow_iso(),
            trigger,
            result.requests,
            dumps(result.new_topic_ids),
            dumps(result.updated_topic_ids),
            result.posts_new,
            result.posts_changed,
            1 if result.partial else 0,
            dumps(result.errors),
        ),
    )
    return result
