from __future__ import annotations

import re
import sqlite3
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .util import utcnow_iso

GITHUB_RE = re.compile(r"github\.com/([\w.\-]+)/([\w.\-]+)", re.I)
HF_RE = re.compile(r"huggingface\.co/([\w.\-]+)/([\w.\-]+)", re.I)

RESERVED_GITHUB_OWNERS = {"orgs", "features", "sponsors", "settings", "marketplace"}
RESERVED_HF_SEGMENTS = {"datasets", "spaces", "docs", "blog", "papers"}

GITHUB_PATH_STOPS = (
    "/tree/",
    "/blob/",
    "/issues",
    "/pull",
    "/releases",
    "/commit",
    "/actions",
    "/wiki",
    "/discussions",
    "/pulls",
    "/tags",
)

_TRAILING = ".,;:)]}'\""


def _clean_name(name: str) -> str:
    name = name.split("?")[0].split("#")[0]
    for stop in GITHUB_PATH_STOPS:
        if stop in name:
            name = name.split(stop)[0]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name.rstrip(_TRAILING)


def normalize_github(url: str) -> str | None:
    if re.search(r"gist\.github\.com", url or "", re.I):
        return None
    match = GITHUB_RE.search(url or "")
    if not match:
        return None
    owner = match.group(1).lower().rstrip(_TRAILING)
    name = _clean_name(match.group(2)).lower()
    if not owner or not name or owner in RESERVED_GITHUB_OWNERS:
        return None
    if owner.startswith("www"):
        return None
    return f"https://github.com/{owner}/{name}"


def normalize_hf(url: str) -> str | None:
    match = HF_RE.search(url or "")
    if not match:
        return None
    owner = match.group(1).lower().rstrip(_TRAILING)
    name = _clean_name(match.group(2))
    if not owner or not name or owner in RESERVED_HF_SEGMENTS:
        return None
    return f"https://huggingface.co/{owner}/{name}"


def _hrefs(cooked: str | None) -> list[str]:
    if not cooked:
        return []
    try:
        tree = HTMLParser(cooked)
    except Exception:
        return []
    return [node.attrs.get("href", "") for node in tree.css("a") if node.attrs.get("href")]


def extract_urls(post: Any) -> list[str]:
    urls: list[str] = []
    raw = post["raw"] if post["raw"] is not None else ""
    for href in _hrefs(post["cooked"]):
        urls.append(href)
    for match in re.finditer(r"https?://[^\s<>()\[\]\"']+", raw):
        urls.append(match.group(0))
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _context(raw: str | None, needle: str, span: int = 150) -> str | None:
    if not raw:
        return None
    index = raw.lower().find(needle.lower())
    if index < 0:
        return None
    start = max(0, index - span)
    end = min(len(raw), index + len(needle) + span)
    return raw[start:end].strip()


def discover(conn: sqlite3.Connection, post: Any) -> dict[str, list[str]]:
    topic_id = post["topic_id"]
    post_id = post["id"]
    raw = post["raw"] or ""
    now = utcnow_iso()
    repos: list[str] = []
    hf_models: list[str] = []

    for url in extract_urls(post):
        github = normalize_github(url)
        if github:
            owner, name = github.removeprefix("https://github.com/").split("/", 1)
            conn.execute(
                "INSERT INTO repos (owner, name, url, origin, watch, first_seen_at)"
                " VALUES (?, ?, ?, 'forum', 0, ?) ON CONFLICT(url) DO NOTHING",
                (owner, name, github, now),
            )
            row = conn.execute("SELECT id FROM repos WHERE url=?", (github,)).fetchone()
            if row is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO repo_mentions"
                " (repo_id, post_id, topic_id, url, context_snippet, mentioned_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    post_id,
                    topic_id,
                    github,
                    _context(raw, f"github.com/{owner}/{name}"),
                    now,
                ),
            )
            repos.append(github)
            continue

        hf = normalize_hf(url)
        if hf:
            repo_key = hf.removeprefix("https://huggingface.co/")
            conn.execute(
                "INSERT INTO hf_models (repo_id, first_seen_at) VALUES (?, ?)"
                " ON CONFLICT(repo_id) DO NOTHING",
                (repo_key, now),
            )
            row = conn.execute("SELECT id FROM hf_models WHERE repo_id=?", (repo_key,)).fetchone()
            if row is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO hf_mentions (hf_model_id, post_id, topic_id, url, mentioned_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (row["id"], post_id, topic_id, hf, now),
            )
            hf_models.append(hf)

    return {"repos": repos, "hf_models": hf_models}
