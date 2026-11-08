from __future__ import annotations

import sqlite3
from typing import Any

FORUM_URL = "https://forums.developer.nvidia.com"


def _post_url(topic_id: int, post_number: int | None) -> str:
    return f"{FORUM_URL}/t/{topic_id}/{post_number or 1}"


def _topic_url(topic_id: int) -> str:
    return f"{FORUM_URL}/t/{topic_id}"


def _post_target(conn: sqlite3.Connection, post_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT p.topic_id, p.post_number, t.title FROM posts p"
        " LEFT JOIN topics t ON t.id = p.topic_id WHERE p.id=?",
        (post_id,),
    ).fetchone()
    if row is None:
        return None
    return {"url": _post_url(row["topic_id"], row["post_number"]), "title": row["title"]}


def post_url(conn: sqlite3.Connection, post_id: int) -> str | None:
    """A stored post's forum URL, or None when the post is not in the mirror."""
    target = _post_target(conn, post_id)
    return target["url"] if target is not None else None


def flag_target(conn: sqlite3.Connection, target_type: str, target_id: Any) -> dict[str, Any]:
    """The `url` (and title or model name) for a flag's target, per SPEC §5 Knowledge.

    A target that no longer resolves gets `url: null` and `target_missing: true`, so it is
    reported rather than dropped. A model has no URL of its own; its canonical name is
    returned instead.
    """
    value = str(target_id or "").strip()
    if target_type == "topic":
        row = None
        if value.isdigit():
            row = conn.execute("SELECT id, title FROM topics WHERE id=?", (int(value),)).fetchone()
        if row is None:
            return {"url": None, "target_missing": True}
        return {"url": _topic_url(row["id"]), "title": row["title"]}
    if target_type == "post":
        target = _post_target(conn, int(value)) if value.isdigit() else None
        return target or {"url": None, "target_missing": True}
    if target_type == "repo":
        row = conn.execute(
            "SELECT url FROM repos WHERE url=? OR lower(owner || '/' || name)=lower(?)",
            (value, value),
        ).fetchone()
        if row is None:
            return {"url": None, "target_missing": True}
        return {"url": row["url"]}
    if target_type == "hf_model":
        row = conn.execute(
            "SELECT repo_id FROM hf_models WHERE lower(repo_id)=lower(?)", (value,)
        ).fetchone()
        if row is None:
            return {"url": None, "target_missing": True}
        return {"url": f"https://huggingface.co/{row['repo_id']}"}
    if target_type == "recipe":
        row = None
        if value.isdigit():
            row = conn.execute(
                "SELECT post_id FROM evidence WHERE target_type='recipe' AND target_id=?"
                " ORDER BY id LIMIT 1",
                (int(value),),
            ).fetchone()
        target = _post_target(conn, row["post_id"]) if row is not None else None
        return target or {"url": None, "target_missing": True}
    if target_type == "model":
        return {"url": None, "model": value}
    return {"url": None, "target_missing": True}


def flag_payload(conn: sqlite3.Connection, row: Any) -> dict[str, Any]:
    """One flag with its target's `url`, ready for a tool response."""
    payload = dict(row)
    payload.update(flag_target(conn, row["target_type"], row["target_id"]))
    return payload


def flags_payload(conn: sqlite3.Connection, rows: list[Any]) -> list[dict[str, Any]]:
    return [flag_payload(conn, row) for row in rows]


def note_sources(conn: sqlite3.Connection, note_id: int) -> list[dict[str, Any]]:
    """A note's posts as `[{post_id, url, date}]`, read from `evidence` (kind='note')."""
    rows = conn.execute(
        "SELECT e.post_id, p.topic_id, p.post_number, p.created_at FROM evidence e"
        " JOIN posts p ON p.id = e.post_id"
        " WHERE e.target_type='note' AND e.target_id=? ORDER BY p.created_at, e.post_id",
        (note_id,),
    ).fetchall()
    return [
        {
            "post_id": row["post_id"],
            "url": _post_url(row["topic_id"], row["post_number"]),
            "date": row["created_at"],
        }
        for row in rows
    ]


def note_payload(conn: sqlite3.Connection, row: Any) -> dict[str, Any]:
    """A research note with its `sources`, so a conclusion traces back to its posts."""
    payload = dict(row)
    payload["sources"] = note_sources(conn, row["id"])
    return payload
