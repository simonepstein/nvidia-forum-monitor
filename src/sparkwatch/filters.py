from __future__ import annotations

import math
import re
import sqlite3
from typing import Any

from .config import Settings, get_settings

MODEL_SIZE_RE = re.compile(r"\b\d{1,4}(\.\d)?\s?b\b")
ACTIVE_PARAMS_RE = re.compile(r"\ba\d{1,3}b\b")


def _topic_text(conn: sqlite3.Connection, topic: Any) -> str:
    parts = [topic["title"] or "", topic["excerpt"] or ""]
    for row in conn.execute("SELECT raw FROM posts WHERE topic_id=?", (topic["id"],)):
        parts.append(row["raw"] or "")
    return "\n".join(parts).lower()


def relevance_score(
    conn: sqlite3.Connection,
    topic_id: int,
    settings: Settings | None = None,
) -> float:
    settings = settings or get_settings()
    topic = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if topic is None:
        return 0.0
    text = _topic_text(conn, topic)
    weights = settings.relevance.weights
    score = 0.0

    for group, keywords in settings.relevance.keywords.items():
        matched = sum(1 for keyword in keywords if keyword.lower() in text)
        score += weights.get(group, 0.0) * min(matched, 3)

    if MODEL_SIZE_RE.search(text) or ACTIVE_PARAMS_RE.search(text):
        score += weights.get("model_size", 0.0)

    families = {
        family
        for family in settings.relevance.families
        if re.search(rf"\b{re.escape(family)}", text)
    }
    score += weights.get("family", 0.0) * min(len(families), 3)

    if conn.execute("SELECT 1 FROM watch_hits WHERE topic_id=? LIMIT 1", (topic_id,)).fetchone():
        score += weights.get("watch", 0.0)

    score += math.log1p(topic["like_count"] or 0) * 0.5
    score += math.log1p(topic["posts_count"] or 0) * 0.25
    return round(score, 3)


def recompute_topic(
    conn: sqlite3.Connection, topic_id: int, settings: Settings | None = None
) -> float:
    score = relevance_score(conn, topic_id, settings)
    conn.execute("UPDATE topics SET relevance_score=? WHERE id=?", (score, topic_id))
    return score


def is_relevant(topic: Any) -> bool:
    override = topic["relevance_override"]
    if override is not None:
        return bool(override)
    return (topic["relevance_score"] or 0.0) > 0.0
