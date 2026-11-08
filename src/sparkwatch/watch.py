from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any

from .util import parse_dt, to_iso, utcnow, utcnow_iso

WATCH_COLUMNS = (
    "id, match_regex, kind, release_regex, first_seen_post_id, sighting_post_id, sighting_acked_at"
)
STORED_POSTS = (
    "SELECT p.id, p.topic_id, p.raw, t.title FROM posts p JOIN topics t ON t.id = p.topic_id"
    " ORDER BY julianday(p.created_at)"
)


def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return None


def _match(pattern: re.Pattern[str], title: str | None, raw: str | None) -> tuple[str, str] | None:
    """The one matcher every path uses: what matched, and where.

    The body is checked first, so a post that mentions the model in its own text counts as a
    mention even when the topic title also matches. A title-only match is thread activity
    (SPEC §1, Watch matching).
    """
    body = pattern.search(raw or "")
    if body is not None:
        return "body", body.group(0)
    heading = pattern.search(title or "")
    if heading is not None:
        return "title", heading.group(0)
    return None


def _record_hit(
    conn: sqlite3.Connection,
    row: Any,
    post_id: int,
    topic_id: int,
    title: str | None,
    raw: str | None,
    now: str,
    is_new: bool,
) -> tuple[bool, bool]:
    """Match one post against one watch entry. Shared by sync and both rematch paths.

    Returns `(matched, inserted)`: `matched` says the regex found something, `inserted` says
    the hit is new. A re-match of an existing hit still refreshes `matched_in` and
    `matched_text`, so an edit can move a hit from the title to the body.
    """
    pattern = _compile(row["match_regex"])
    if pattern is None:
        return False, False
    found = _match(pattern, title, raw)
    if found is None:
        return False, False
    matched_in, matched_text = found
    cursor = conn.execute(
        "INSERT OR IGNORE INTO watch_hits"
        " (watch_id, post_id, topic_id, matched_in, matched_text, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], post_id, topic_id, matched_in, matched_text, now),
    )
    inserted = bool(cursor.rowcount)
    if not inserted:
        conn.execute(
            "UPDATE watch_hits SET matched_in=?, matched_text=? WHERE watch_id=? AND post_id=?",
            (matched_in, matched_text, row["id"], post_id),
        )
    conn.execute(
        "UPDATE watchlist SET first_seen_post_id=?, first_seen_at=?"
        " WHERE id=? AND first_seen_post_id IS NULL",
        (post_id, now, row["id"]),
    )
    if row["kind"] == "anticipated":
        _raise_sighting(conn, row, post_id, title, raw, now, is_new)
    return True, inserted


def match_post(
    conn: sqlite3.Connection,
    post_id: int,
    topic_id: int,
    title: str | None,
    raw: str | None,
    is_new: bool = True,
) -> list[int]:
    """Match one stored post against every active watch entry.

    `is_new` is False for a post sparkwatch already had, i.e. an edit. An edit updates
    thread activity but never raises a sighting (SPEC §1, "Edits raise nothing").
    """
    now = utcnow_iso()
    hits: list[int] = []
    rows = conn.execute(f"SELECT {WATCH_COLUMNS} FROM watchlist WHERE active=1").fetchall()
    for row in rows:
        matched, inserted = _record_hit(conn, row, post_id, topic_id, title, raw, now, is_new)
        if matched and inserted:
            hits.append(row["id"])
    return hits


def _raise_sighting(
    conn: sqlite3.Connection,
    row: Any,
    post_id: int,
    title: str | None,
    raw: str | None,
    now: str,
    is_new: bool,
) -> None:
    """Record a sighting alert for an anticipated model.

    With a `release_regex`, a release raises it from a post body, or from the OP of a topic
    whose title matches it. A reply that only matches the topic title is thread activity, and
    an edit to an old post raises nothing. After an acknowledgement, only a post created
    after it re-arms the alert. Without a `release_regex`, the first hit raises it, as before.
    """
    if not is_new:
        return
    release_regex = row["release_regex"]
    if release_regex:
        release = _compile(release_regex)
        if release is None:
            return
        post = conn.execute(
            "SELECT post_number, created_at FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        if post is None:
            return
        body_release = release.search(raw or "") is not None
        op_title_release = post["post_number"] == 1 and release.search(title or "") is not None
        if not (body_release or op_title_release):
            return
        if row["sighting_post_id"] == post_id:
            return
        if row["sighting_post_id"] is not None and row["sighting_acked_at"] is None:
            return
        if row["sighting_acked_at"] is not None:
            acked = parse_dt(row["sighting_acked_at"])
            created = parse_dt(post["created_at"])
            if acked is None or created is None or created <= acked:
                return
    elif row["sighting_post_id"] is not None:
        return
    conn.execute(
        "UPDATE watchlist SET sighting_post_id=?, sighting_at=?, sighting_acked_at=NULL WHERE id=?",
        (post_id, now, row["id"]),
    )


def rematch_all(conn: sqlite3.Connection) -> int:
    """Re-match every active entry against every stored post, as a regex change would.

    Returns the number of matches, not new hits: a rematch refreshes existing hits too.
    """
    now = utcnow_iso()
    rows = conn.execute(f"SELECT {WATCH_COLUMNS} FROM watchlist WHERE active=1").fetchall()
    total = 0
    for post in conn.execute(STORED_POSTS).fetchall():
        for row in rows:
            matched, _inserted = _record_hit(
                conn, row, post["id"], post["topic_id"], post["title"], post["raw"], now, False
            )
            total += matched
    return total


def rematch_watch(conn: sqlite3.Connection, watch_id: int) -> int:
    """Re-match one entry against every stored post, dropping hits its old regex made.

    Used when a watch entry is added or its regex changes: local only, no fetching, and
    never a sighting.
    """
    row = conn.execute(f"SELECT {WATCH_COLUMNS} FROM watchlist WHERE id=?", (watch_id,)).fetchone()
    if row is None:
        return 0
    conn.execute("DELETE FROM watch_hits WHERE watch_id=?", (watch_id,))
    if _compile(row["match_regex"]) is None:
        return 0
    now = utcnow_iso()
    total = 0
    for post in conn.execute(STORED_POSTS).fetchall():
        matched, _inserted = _record_hit(
            conn, row, post["id"], post["topic_id"], post["title"], post["raw"], now, False
        )
        total += matched
    return total


def recompute_matched_in(conn: sqlite3.Connection) -> int:
    """Re-derive `matched_in` and `matched_text` for stored hits (migration 5).

    Local only: no fetching, no hits deleted, and each hit keeps its `created_at`. A hit
    whose current regex no longer matches the stored post is left at the column default
    (`body`), so it keeps counting as a mention; SPEC §6 records that choice. In practice
    there are none: a changed regex rematches its entry, which drops the hits it can no
    longer make.
    """
    rows = conn.execute(
        "SELECT h.watch_id, h.post_id, w.match_regex FROM watch_hits h"
        " JOIN watchlist w ON w.id = h.watch_id"
    ).fetchall()
    updated = 0
    for row in rows:
        pattern = _compile(row["match_regex"])
        if pattern is None:
            continue
        post = conn.execute(
            "SELECT p.raw, t.title FROM posts p JOIN topics t ON t.id = p.topic_id WHERE p.id=?",
            (row["post_id"],),
        ).fetchone()
        if post is None:
            continue
        found = _match(pattern, post["title"], post["raw"])
        if found is None:
            continue
        conn.execute(
            "UPDATE watch_hits SET matched_in=?, matched_text=? WHERE watch_id=? AND post_id=?",
            (found[0], found[1], row["watch_id"], row["post_id"]),
        )
        updated += 1
    return updated


def add_watch(
    conn: sqlite3.Connection,
    label: str,
    match_regex: str,
    kind: str = "tracking",
    canonical: str | None = None,
    note: str | None = None,
    release_regex: str | None = None,
) -> dict[str, Any]:
    if _compile(match_regex) is None:
        raise ValueError(f"invalid regex: {match_regex}")
    if release_regex is not None and _compile(release_regex) is None:
        raise ValueError(f"invalid release_regex: {release_regex}")
    if kind not in ("tracking", "anticipated"):
        raise ValueError(f"invalid kind: {kind}")
    conn.execute(
        "INSERT INTO watchlist (label, canonical, match_regex, kind, active, note, release_regex,"
        " created_at)"
        " VALUES (?, ?, ?, ?, 1, ?, ?, ?)"
        " ON CONFLICT(label) DO UPDATE SET match_regex=excluded.match_regex, kind=excluded.kind,"
        " canonical=COALESCE(excluded.canonical, watchlist.canonical),"
        " note=COALESCE(excluded.note, watchlist.note),"
        " release_regex=COALESCE(excluded.release_regex, watchlist.release_regex)",
        (label, canonical, match_regex, kind, note, release_regex, utcnow_iso()),
    )
    row = conn.execute("SELECT id FROM watchlist WHERE label=?", (label,)).fetchone()
    rematch_watch(conn, row["id"])
    return dict(conn.execute("SELECT * FROM watchlist WHERE label=?", (label,)).fetchone())


def update_watch(
    conn: sqlite3.Connection,
    label: str,
    match_regex: str | None = None,
    kind: str | None = None,
    canonical: str | None = None,
    active: bool | None = None,
    note: str | None = None,
    release_regex: str | None = None,
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM watchlist WHERE label=?", (label,)).fetchone()
    if row is None:
        return None
    if match_regex is not None and _compile(match_regex) is None:
        raise ValueError(f"invalid regex: {match_regex}")
    if release_regex is not None and _compile(release_regex) is None:
        raise ValueError(f"invalid release_regex: {release_regex}")
    conn.execute(
        "UPDATE watchlist SET match_regex=?, kind=?, canonical=?, active=?, note=?,"
        " release_regex=? WHERE id=?",
        (
            match_regex if match_regex is not None else row["match_regex"],
            kind if kind is not None else row["kind"],
            canonical if canonical is not None else row["canonical"],
            int(active) if active is not None else row["active"],
            note if note is not None else row["note"],
            release_regex if release_regex is not None else row["release_regex"],
            row["id"],
        ),
    )
    if match_regex is not None and match_regex != row["match_regex"]:
        rematch_watch(conn, row["id"])
    return dict(conn.execute("SELECT * FROM watchlist WHERE id=?", (row["id"],)).fetchone())


def ack_sighting(conn: sqlite3.Connection, label: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM watchlist WHERE label=?", (label,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE watchlist SET sighting_acked_at=? WHERE id=? AND sighting_acked_at IS NULL",
        (utcnow_iso(), row["id"]),
    )
    return dict(conn.execute("SELECT * FROM watchlist WHERE id=?", (row["id"],)).fetchone())


def list_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Watch entries with hit counts over 7 and 30 days, counted by post date.

    The windows use `posts.created_at` and count distinct posts: activity, not novelty.
    `hits_*` count mentions (hits whose text matched in the post body); `replies_*` count
    title-only hits, i.e. replies in a topic whose title matches.
    """
    now = utcnow()

    def window(matched_in: str, column: str, days: int) -> str:
        return (
            f"(SELECT COUNT(DISTINCT h.post_id) FROM watch_hits h JOIN posts p ON p.id = h.post_id"
            f" WHERE h.watch_id = w.id AND h.matched_in = '{matched_in}'"
            f" AND julianday(p.created_at) >= julianday(:cut_{days})) AS {column}_{days}d"
        )

    def latest(matched_in: str, alias: str) -> str:
        return (
            f"(SELECT MAX(p.created_at) FROM watch_hits h JOIN posts p ON p.id = h.post_id"
            f" WHERE h.watch_id = w.id AND h.matched_in = '{matched_in}') AS {alias}"
        )

    rows = conn.execute(
        f"""
        SELECT w.*,
          {window("body", "hits", 7)},
          {window("body", "hits", 30)},
          {window("title", "replies", 7)},
          {window("title", "replies", 30)},
          {latest("body", "latest_hit")},
          {latest("title", "latest_reply")}
        FROM watchlist w ORDER BY w.label
        """,
        {"cut_7": to_iso(now - timedelta(days=7)), "cut_30": to_iso(now - timedelta(days=30))},
    ).fetchall()
    return [dict(row) for row in rows]
