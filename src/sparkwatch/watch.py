from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any

from .util import to_iso, utcnow, utcnow_iso


def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return None


def match_post(
    conn: sqlite3.Connection,
    post_id: int,
    topic_id: int,
    title: str | None,
    raw: str | None,
) -> list[int]:
    now = utcnow_iso()
    hits: list[int] = []
    rows = conn.execute(
        "SELECT id, match_regex, kind, release_regex, first_seen_post_id, sighting_post_id,"
        " sighting_acked_at FROM watchlist WHERE active=1"
    ).fetchall()
    for row in rows:
        pattern = _compile(row["match_regex"])
        if pattern is None:
            continue
        match = pattern.search(title or "") or pattern.search(raw or "")
        if match is None:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO watch_hits (watch_id, post_id, topic_id, matched_text, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (row["id"], post_id, topic_id, match.group(0), now),
        )
        if cursor.rowcount:
            hits.append(row["id"])
        conn.execute(
            "UPDATE watchlist SET first_seen_post_id=?, first_seen_at=?"
            " WHERE id=? AND first_seen_post_id IS NULL",
            (post_id, now, row["id"]),
        )
        if row["kind"] == "anticipated":
            _raise_sighting(conn, row, post_id, title, raw, now)
    return hits


def _raise_sighting(
    conn: sqlite3.Connection,
    row: Any,
    post_id: int,
    title: str | None,
    raw: str | None,
    now: str,
) -> None:
    """Record a sighting alert for an anticipated model.

    With a `release_regex`, only a release match raises it, and an acknowledgement re-arms
    it for the next release. Without one, the first hit raises it, as before.
    """
    release_regex = row["release_regex"]
    if release_regex:
        release = _compile(release_regex)
        if release is None:
            return
        if not (release.search(title or "") or release.search(raw or "")):
            return
        rearm = row["sighting_post_id"] is None or row["sighting_acked_at"] is not None
        if not rearm or row["sighting_post_id"] == post_id:
            return
    elif row["sighting_post_id"] is not None:
        return
    conn.execute(
        "UPDATE watchlist SET sighting_post_id=?, sighting_at=?, sighting_acked_at=NULL WHERE id=?",
        (post_id, now, row["id"]),
    )


def rematch_all(conn: sqlite3.Connection) -> int:
    total = 0
    rows = conn.execute(
        "SELECT p.id, p.topic_id, p.raw, t.title FROM posts p JOIN topics t ON t.id = p.topic_id"
    ).fetchall()
    for row in rows:
        total += len(match_post(conn, row["id"], row["topic_id"], row["title"], row["raw"]))
    return total


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
    rematch_all(conn)
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
    if match_regex is not None:
        rematch_all(conn)
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
    """
    now = utcnow()
    rows = conn.execute(
        """
        SELECT w.*,
          (SELECT COUNT(DISTINCT h.post_id) FROM watch_hits h JOIN posts p ON p.id = h.post_id
             WHERE h.watch_id = w.id AND julianday(p.created_at) >= julianday(:cut_7)) AS hits_7d,
          (SELECT COUNT(DISTINCT h.post_id) FROM watch_hits h JOIN posts p ON p.id = h.post_id
             WHERE h.watch_id = w.id AND julianday(p.created_at) >= julianday(:cut_30)) AS hits_30d,
          (SELECT MAX(p.created_at) FROM watch_hits h JOIN posts p ON p.id = h.post_id
             WHERE h.watch_id = w.id) AS latest_hit
        FROM watchlist w ORDER BY w.label
        """,
        {"cut_7": to_iso(now - timedelta(days=7)), "cut_30": to_iso(now - timedelta(days=30))},
    ).fetchall()
    return [dict(row) for row in rows]
