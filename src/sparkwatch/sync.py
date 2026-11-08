from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import filters, links, watch
from .config import CategorySettings, Settings, get_settings
from .db import open_db
from .discourse import DiscourseClient, DiscourseError, ForbiddenError, NotFoundError
from .util import dumps, normalize_iso, parse_dt, to_iso, utcnow, utcnow_iso

STORED_POST_TYPES = {1, 2}
NOT_CONTENT = "not_content"

logger = logging.getLogger(__name__)


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
    listing_floor = parse_dt(forum["listing_floor"])
    last_synced = parse_dt(forum["last_synced_at"])
    window_floor = now - timedelta(days=lookback_days)
    floor = min(previous_floor, window_floor) if previous_floor is not None else window_floor
    backfill = previous_floor is not None and floor < previous_floor
    first_run = last_synced is None
    # A listing_floor later than the window floor means the listing never covered the gap.
    gap = listing_floor is not None and listing_floor > window_floor
    cutoff = window_floor if (first_run or backfill or gap) else last_synced
    return floor, cutoff, backfill, first_run


def _due_tombstone_topics(
    conn: sqlite3.Connection, forum_id: int, recheck_cut: datetime
) -> list[int]:
    """Topics holding a tombstoned post that is due a re-check (never `not_content`).

    This is the SPEC §1 re-check policy: a tombstoned ID is requested again during a
    backfill, or once its last check is older than `missing_post_recheck_days`.
    """
    rows = conn.execute(
        "SELECT DISTINCT t.id FROM topics t JOIN missing_posts m ON m.topic_id = t.id"
        " WHERE t.forum_id=? AND m.reason <> 'not_content'"
        " AND (m.last_checked_at IS NULL"
        "   OR julianday(m.last_checked_at) < julianday(?))",
        (forum_id, to_iso(recheck_cut)),
    ).fetchall()
    return [int(row["id"]) for row in rows]


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
        "created_at": normalize_iso(topic.get("created_at")),
        "bumped_at": normalize_iso(topic.get("bumped_at")),
        "last_posted_at": normalize_iso(topic.get("last_posted_at")),
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
    deadline: float | None = None,
) -> tuple[list[int], datetime | None, bool]:
    """Page through the listing. Returns (queued, oldest listed bumped_at, reached cutoff)."""
    queued: list[int] = []
    oldest_listed: datetime | None = None
    reached = False
    page = 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            result.partial = True
            break
        if max_pages is not None and page >= max_pages:
            break
        data = client.list_category_page(category.id, page)
        topic_list = data.get("topic_list") or {}
        topics = topic_list.get("topics") or []
        more = topic_list.get("more_topics_url")
        non_pinned = [topic for topic in topics if not topic.get("pinned")]

        for topic in non_pinned:
            bumped = parse_dt(topic.get("bumped_at"))
            if bumped is not None and (oldest_listed is None or bumped < oldest_listed):
                oldest_listed = bumped

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
                reached = True
                break
        if not more:
            reached = True
            break
        page += 1
    return queued, oldest_listed, reached


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
    if existing is None and reason != NOT_CONTENT:
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
        normalize_iso(post.get("created_at")),
        normalize_iso(post.get("updated_at")),
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
            "SELECT post_id, reason, last_checked_at FROM missing_posts WHERE topic_id=?",
            (topic_id,),
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
            if missing[post_id]["reason"] == NOT_CONTENT:
                continue
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
            _tombstone(conn, topic_id, post_id, NOT_CONTENT, now_iso, result)
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
            is_new=outcome == "new",
        )
        result.watch_hits_new += len(hits)

    conn.execute(
        "UPDATE topics SET posts_complete=? WHERE id=?",
        (0 if interrupted else 1, topic_id),
    )
    filters.recompute_topic(conn, topic_id, settings)


def _record_coverage(
    conn: sqlite3.Connection,
    categories: list[CategorySettings],
    floors: dict[int, tuple[datetime, datetime]],
    coverage: dict[int, tuple[datetime | None, bool]],
    started: datetime,
    result: SyncResult,
) -> None:
    """Record how far back the listing was read, and only then advance the cutoff."""
    for category in categories:
        floor, _cutoff = floors.get(category.id, (None, None))
        if floor is None:
            continue
        oldest_listed, reached = coverage.get(category.id, (None, False))
        row = conn.execute("SELECT listing_floor FROM forums WHERE id=?", (category.id,)).fetchone()
        previous = parse_dt(row["listing_floor"]) if row is not None else None
        if reached:
            listing_floor = floor if previous is None else min(floor, previous)
        elif oldest_listed is not None:
            listing_floor = oldest_listed if previous is None else min(previous, oldest_listed)
        else:
            listing_floor = previous
        listing_value = to_iso(listing_floor) if listing_floor is not None else None
        if result.partial or not reached:
            # Stopped early: the listing did not cover the window, so the cutoff stays put
            # and the next sync carries on paging back.
            conn.execute(
                "UPDATE forums SET listing_floor=? WHERE id=?", (listing_value, category.id)
            )
            continue
        conn.execute(
            "UPDATE forums SET last_synced_at=?, backfill_floor=?, listing_floor=? WHERE id=?",
            (to_iso(started), to_iso(floor), listing_value, category.id),
        )


def repair_coverage(
    conn: sqlite3.Connection, forum_id: int | None = None, apply: bool = False
) -> dict[str, Any]:
    """Set a forum's listing_floor to the oldest non-pinned bumped_at actually stored.

    Dry run unless `apply` is set, and explicit and per forum only: run automatically, this
    would invent gaps on forums that were fully synced.
    """
    if forum_id is None:
        return {"error": "repair_coverage needs a forum id"}
    forum = conn.execute("SELECT * FROM forums WHERE id=?", (forum_id,)).fetchone()
    if forum is None:
        return {"error": f"unknown forum: {forum_id}"}
    row = conn.execute(
        "SELECT MIN(bumped_at) AS oldest FROM topics"
        " WHERE forum_id=? AND pinned=0 AND bumped_at IS NOT NULL",
        (forum_id,),
    ).fetchone()
    oldest = normalize_iso(row["oldest"]) if row is not None else None
    previous = normalize_iso(forum["listing_floor"])
    previous_at = parse_dt(previous)
    oldest_at = parse_dt(oldest)
    changed = previous_at != oldest_at
    would_create_gap = previous_at is not None and oldest_at is not None and oldest_at > previous_at
    if apply and changed:
        conn.execute("UPDATE forums SET listing_floor=? WHERE id=?", (oldest, forum_id))
    payload: dict[str, Any] = {
        "forum_id": forum_id,
        "old_listing_floor": previous,
        "new_listing_floor": oldest,
        "changed": changed,
        "applied": bool(apply and changed),
    }
    if would_create_gap:
        payload["would_create_gap"] = True
        payload["warning"] = (
            "the new listing_floor is later than the old one: if this forum was fully"
            " covered, the next sync will re-page the listing to heal the gap it creates"
        )
    return payload


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
    coverage: dict[int, tuple[datetime | None, bool]] = {}
    try:
        for category in categories:
            floor, cutoff, backfill, _first_run = _window(conn, category, lookback, started)
            floors[category.id] = (floor, cutoff)
            queued, oldest_listed, reached = _list_topics(
                conn, client, category, max_pages, floor, cutoff, backfill, result, deadline
            )
            coverage[category.id] = (oldest_listed, reached)
            recheck_cut = started - timedelta(days=settings.sync.missing_post_recheck_days)
            for topic_id in _due_tombstone_topics(conn, category.id, recheck_cut):
                if topic_id not in queued:
                    if topic_id not in result.updated_topic_ids:
                        result.updated_topic_ids.append(topic_id)
                    queued.append(topic_id)
            for topic_id in queued:
                if deadline is not None and time.monotonic() > deadline:
                    result.partial = True
                    break
                try:
                    _sync_topic(
                        conn, client, settings, topic_id, floor, backfill, result, started, deadline
                    )
                except (NotFoundError, ForbiddenError) as exc:
                    logger.warning("skipping topic %s: %s", topic_id, exc)
                    result.errors.append(f"topic {topic_id}: {exc}")
                except DiscourseError as exc:
                    logger.warning("skipping topic %s: %s", topic_id, exc)
                    result.errors.append(f"topic {topic_id}: {exc}")
                except Exception as exc:  # noqa: BLE001 - one topic must not stop the sync
                    result.errors.append(f"topic {topic_id}: {exc!r}")
            if result.partial:
                break
    finally:
        result.requests = client.request_count
        if owns_client:
            client.close()

    _record_coverage(conn, categories, floors, coverage, started, result)

    conn.execute(
        'INSERT INTO sync_runs (started_at, finished_at, "trigger", requests, new_topic_ids,'
        " updated_topic_ids, posts_new, posts_changed, partial, errors, forum_ids)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            dumps([category.id for category in categories]),
        ),
    )
    return result
