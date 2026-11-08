from __future__ import annotations

import sqlite3


def normalize_ws(text: str | None) -> str:
    return " ".join((text or "").split())


def verify_quote(conn: sqlite3.Connection, post_id: int, quote: str) -> bool:
    if not quote or not quote.strip():
        return False
    row = conn.execute("SELECT raw FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        return False
    return normalize_ws(quote) in normalize_ws(row["raw"])


def record_evidence(
    conn: sqlite3.Connection,
    target_type: str,
    target_id: int,
    post_id: int,
    quote: str | None,
) -> bool:
    verified = verify_quote(conn, post_id, quote) if quote else False
    conn.execute(
        "INSERT INTO evidence (target_type, target_id, post_id, quote, quote_verified)"
        " VALUES (?, ?, ?, ?, ?)",
        (target_type, target_id, post_id, quote, 1 if verified else 0),
    )
    return verified
