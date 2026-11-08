from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import timedelta
from typing import Any

import httpx

from .config import Settings, get_settings
from .pacing import RateLimiter
from .util import dumps, loads, parse_dt, to_iso, utcnow, utcnow_iso

GITHUB_API = "https://api.github.com"
GITHUB_REQUESTS_PER_SECOND = 1.0
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SLUG_RE = re.compile(r"^(?P<owner>[\w.\-]+)/(?P<name>[\w.\-]+?)(?:\.git)?/?$")
URL_RE = re.compile(r"github\.com/(?P<owner>[\w.\-]+)/(?P<name>[\w.\-]+)", re.I)
RATE_LIMIT_FLOOR = 5
MAX_COMPARE_COMMITS = 250
COMPARE_STATUS_FROM_BASE = {
    "ahead": "behind",
    "behind": "ahead",
    "identical": "identical",
    "diverged": "diverged",
}


class GitHubError(Exception):
    pass


class GitHubNotFound(GitHubError):
    pass


class RateLimitStop(GitHubError):
    pass


def parse_repo(repo: str) -> tuple[str, str]:
    text = (repo or "").strip()
    match = URL_RE.search(text)
    if match:
        return match.group("owner").lower(), match.group("name").removesuffix(".git").lower()
    match = SLUG_RE.match(text)
    if match:
        return match.group("owner").lower(), match.group("name").lower()
    raise ValueError(f"cannot parse repo: {repo!r}")


class GitHubClient:
    def __init__(
        self,
        settings: Settings | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.request_count = 0
        self.remaining: int | None = None
        self.limiter = RateLimiter(GITHUB_REQUESTS_PER_SECOND)
        self._client = client or httpx.Client(
            base_url=GITHUB_API, timeout=30.0, follow_redirects=True
        )

    def close(self) -> None:
        self._client.close()

    def _headers(self, accept: str | None = None, etag: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": "sparkwatch/0.1",
            "Accept": accept or "application/vnd.github+json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
        etag: str | None = None,
    ) -> tuple[int, httpx.Response | None, str | None]:
        if self.remaining is not None and self.remaining < RATE_LIMIT_FLOOR:
            raise RateLimitStop("github rate limit nearly exhausted")
        self.limiter.wait()
        self.request_count += 1
        response = self._client.get(path, params=params, headers=self._headers(accept, etag))
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.strip().isdigit():
            self.remaining = int(remaining)
        if response.status_code == 304:
            return 304, None, etag
        if response.status_code in (404, 422):
            raise GitHubNotFound(f"{path}: {response.status_code}")
        if response.status_code >= 400:
            raise GitHubError(f"{path}: {response.status_code}")
        return response.status_code, response, response.headers.get("ETag")


def _etags(repo: Any) -> dict[str, str]:
    return loads(repo["etags"], {}) or {}


def _save_etag(conn: sqlite3.Connection, repo_id: int, key: str, etag: str | None) -> None:
    row = conn.execute("SELECT etags FROM repos WHERE id=?", (repo_id,)).fetchone()
    etags = loads(row["etags"], {}) or {}
    if etag:
        etags[key] = etag
    else:
        etags.pop(key, None)
    conn.execute("UPDATE repos SET etags=? WHERE id=?", (dumps(etags), repo_id))


def _save_meta_checked(conn: sqlite3.Connection, repo_id: int, when_iso: str) -> None:
    """Record when repo metadata was last fetched, separately from the check time.

    SPEC §4b step 3: metadata is fetched at most once a day, measured from the last metadata
    fetch, not from `last_checked_at` (which every check moves). A 304 counts as fetched.
    """
    row = conn.execute("SELECT etags FROM repos WHERE id=?", (repo_id,)).fetchone()
    etags = loads(row["etags"], {}) or {}
    etags["meta_checked_at"] = when_iso
    conn.execute("UPDATE repos SET etags=? WHERE id=?", (dumps(etags), repo_id))


GITHUB_ID_KEY = "github_id"


def _github_id(repo: Any) -> int | None:
    """GitHub's numeric repository id, kept in `etags` so no migration is needed.

    SPEC §4b: the id is the repo's identity, so two rows that resolve to the same id are one
    repo stored under two old names.
    """
    value = _etags(repo).get(GITHUB_ID_KEY)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _save_github_id(conn: sqlite3.Connection, repo_id: int, github_id: int) -> None:
    row = conn.execute("SELECT etags FROM repos WHERE id=?", (repo_id,)).fetchone()
    etags = loads(row["etags"], {}) or {}
    etags[GITHUB_ID_KEY] = int(github_id)
    conn.execute("UPDATE repos SET etags=? WHERE id=?", (dumps(etags), repo_id))


def _repo_row(conn: sqlite3.Connection, repo_id: int) -> Any:
    return conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()


def _merge_repos(conn: sqlite3.Connection, winner_id: int, loser_id: int) -> int:
    """Fold one row into the other, keeping every mention, install, event and cache entry.

    SPEC §4b: rows that share a GitHub id are one repo, so the loser's children move to the
    winner (INSERT OR IGNORE keeps the existing row on a key clash) and the loser is dropped.
    """
    if winner_id == loser_id:
        return winner_id
    winner = _repo_row(conn, winner_id)
    loser = _repo_row(conn, loser_id)
    if winner is None or loser is None:
        return winner_id
    moves = {
        "repo_mentions": "post_id, topic_id, url, context_snippet, mentioned_at",
        "repo_installs": "label, sha, ref_given, branch, installed_at, note, updated_at",
        "repo_events": (
            "kind, ref, prev_ref, commits_ahead, title, body_excerpt, url, occurred_at, detected_at"
        ),
        "compare_cache": "base_sha, head_sha, response, fetched_at",
    }
    for table, columns in moves.items():
        conn.execute(
            f"INSERT OR IGNORE INTO {table} (repo_id, {columns})"
            f" SELECT ?, {columns} FROM {table} WHERE repo_id=?",
            (winner_id, loser_id),
        )
        conn.execute(f"DELETE FROM {table} WHERE repo_id=?", (loser_id,))

    for name in (loser["url"], f"{loser['owner']}/{loser['name']}"):
        conn.execute(
            "UPDATE flags SET target_id=? WHERE target_type='repo' AND lower(target_id)=lower(?)",
            (winner["url"], name),
        )

    head_sha = winner["head_sha"] or loser["head_sha"]
    head_seen_at = winner["head_seen_at"] or loser["head_seen_at"]
    if loser["head_seen_at"] and (
        winner["head_seen_at"] is None or loser["head_seen_at"] > winner["head_seen_at"]
    ):
        head_sha, head_seen_at = loser["head_sha"], loser["head_seen_at"]
    etags = {**(loads(loser["etags"], {}) or {}), **(loads(winner["etags"], {}) or {})}
    conn.execute(
        "UPDATE repos SET watch=?, branch=?, note=?, head_sha=?, head_seen_at=?,"
        " latest_release_tag=?, latest_tag=?, first_seen_at=?, watched_at=?,"
        " last_checked_at=?, origin=?, description=?, default_branch=?, archived=?,"
        " pushed_at=?, etags=? WHERE id=?",
        (
            1 if (winner["watch"] or loser["watch"]) else 0,
            winner["branch"] or loser["branch"],
            winner["note"] or loser["note"],
            head_sha,
            head_seen_at,
            winner["latest_release_tag"] or loser["latest_release_tag"],
            winner["latest_tag"] or loser["latest_tag"],
            min(winner["first_seen_at"], loser["first_seen_at"]),
            min(
                [value for value in (winner["watched_at"], loser["watched_at"]) if value],
                default=None,
            ),
            max(
                [value for value in (winner["last_checked_at"], loser["last_checked_at"]) if value],
                default=None,
            ),
            "forum" if "forum" in (winner["origin"], loser["origin"]) else winner["origin"],
            winner["description"] if winner["description"] is not None else loser["description"],
            winner["default_branch"] or loser["default_branch"],
            winner["archived"] if winner["archived"] is not None else loser["archived"],
            winner["pushed_at"] or loser["pushed_at"],
            dumps(etags),
            winner_id,
        ),
    )
    conn.execute("DELETE FROM repos WHERE id=?", (loser_id,))
    return winner_id


def _merge_by_github_id(
    conn: sqlite3.Connection,
    repo_id: int,
    github_id: int,
    merged: list[dict[str, int]] | None = None,
) -> int:
    """Merge every row that shares a GitHub id, returning the surviving row's id.

    The watched row wins, then the older `first_seen_at`, then the lower id, so a repo keeps
    the row the user flagged and the row that has been in the mirror longest. Each fold is
    appended to `merged` as `{from, to}`, whichever side the caller was looking at.
    """
    matches = [
        row
        for row in conn.execute("SELECT * FROM repos").fetchall()
        if _github_id(row) == github_id
    ]
    if len(matches) < 2:
        return repo_id
    matches.sort(key=lambda row: (0 if row["watch"] else 1, row["first_seen_at"] or "", row["id"]))
    winner_id = matches[0]["id"]
    for loser in matches[1:]:
        _merge_repos(conn, winner_id, loser["id"])
        if merged is not None:
            merged.append({"from": loser["id"], "to": winner_id})
    return winner_id


def _resolve_github_id(conn: sqlite3.Connection, client: GitHubClient, row: Any) -> Any:
    """Learn GitHub's numeric id from one metadata request, then merge rows that share it.

    SPEC §4b: discovery stores repos from post links, so an unwatched row has no id until it
    is touched. `set_installed` resolves it here, once, so a stale alias is merged into the
    watched row instead of keeping its own mentions. The response is recorded as that day's
    metadata check (fields, `meta_checked_at` and the ETag), so the next `check_repos` does
    not repeat the request.
    """
    try:
        _status, response, etag = client.get(f"/repos/{row['owner']}/{row['name']}")
    except GitHubError:
        return row
    if response is None:
        return row
    try:
        meta = response.json()
    except ValueError:
        return row
    github_id = meta.get("id")
    if not isinstance(github_id, int):
        return row
    repo_id = row["id"]
    _save_github_id(conn, repo_id, github_id)
    repo_id = _merge_by_github_id(conn, repo_id, github_id)
    _follow_rename(conn, repo_id, _repo_row(conn, repo_id), meta)
    conn.execute(
        "UPDATE repos SET description=?, default_branch=?, archived=?, pushed_at=? WHERE id=?",
        (
            meta.get("description"),
            meta.get("default_branch"),
            1 if meta.get("archived") else 0,
            meta.get("pushed_at"),
            repo_id,
        ),
    )
    _save_meta_checked(conn, repo_id, utcnow_iso())
    _save_etag(conn, repo_id, "meta", etag)
    return _repo_row(conn, repo_id)


def get_or_create_repo(conn: sqlite3.Connection, repo: str) -> Any:
    owner, name = parse_repo(repo)
    url = f"https://github.com/{owner}/{name}"
    row = conn.execute("SELECT * FROM repos WHERE url=?", (url,)).fetchone()
    if row is not None:
        return row
    conn.execute(
        "INSERT INTO repos (owner, name, url, origin, watch, first_seen_at)"
        " VALUES (?, ?, ?, 'agent', 0, ?)",
        (owner, name, url, utcnow_iso()),
    )
    return conn.execute("SELECT * FROM repos WHERE url=?", (url,)).fetchone()


def _event(
    conn: sqlite3.Connection,
    repo_id: int,
    kind: str,
    ref: str | None,
    now_iso: str,
    prev_ref: str | None = None,
    commits_ahead: int | None = None,
    title: str | None = None,
    body_excerpt: str | None = None,
    url: str | None = None,
    occurred_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO repo_events (repo_id, kind, ref, prev_ref, commits_ahead, title,"
        " body_excerpt, url, occurred_at, detected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            repo_id,
            kind,
            ref,
            prev_ref,
            commits_ahead,
            title,
            body_excerpt,
            url,
            occurred_at,
            now_iso,
        ),
    )


def _fetch_head(
    conn: sqlite3.Connection,
    client: GitHubClient,
    repo: Any,
    branch: str,
) -> tuple[str | None, bool]:
    owner, name = repo["owner"], repo["name"]
    key = f"head:{branch}"
    etag = _etags(repo).get(key)
    try:
        status, response, new_etag = client.get(
            f"/repos/{owner}/{name}/commits/{branch}",
            accept="application/vnd.github.sha",
            etag=etag,
        )
    except GitHubNotFound:
        return None, True
    if status == 304:
        return repo["head_sha"], False
    _save_etag(conn, repo["id"], key, new_etag)
    sha = (response.text if response is not None else "").strip() or None
    return sha, False


def _compare(
    conn: sqlite3.Connection,
    client: GitHubClient,
    repo: Any,
    base_sha: str,
    head_sha: str,
) -> dict[str, Any] | None:
    if not base_sha or not head_sha or base_sha == head_sha:
        return None
    cached = conn.execute(
        "SELECT response FROM compare_cache WHERE repo_id=? AND base_sha=? AND head_sha=?",
        (repo["id"], base_sha, head_sha),
    ).fetchone()
    if cached is not None:
        return loads(cached["response"])
    owner, name = repo["owner"], repo["name"]
    try:
        _status, response, _etag = client.get(
            f"/repos/{owner}/{name}/compare/{base_sha}...{head_sha}"
        )
    except (GitHubError, httpx.HTTPError):
        return None
    if response is None:
        return None
    data = response.json()
    conn.execute(
        "INSERT OR REPLACE INTO compare_cache (repo_id, base_sha, head_sha, response, fetched_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (repo["id"], base_sha, head_sha, dumps(data), utcnow_iso()),
    )
    return data


def _release_excerpt(body: str | None) -> str | None:
    if not body:
        return None
    return body[:2000]


def _follow_rename(conn: sqlite3.Connection, repo_id: int, row: Any, meta: dict[str, Any]) -> bool:
    """After a 301, GitHub's metadata carries the new owner/name: adopt it."""
    full_name = str(meta.get("full_name") or "")
    if "/" not in full_name:
        return False
    owner, name = (part.strip().lower() for part in full_name.split("/", 1))
    if not owner or not name:
        return False
    url = f"https://github.com/{owner}/{name}"
    if url == row["url"]:
        return False
    clash = conn.execute("SELECT id FROM repos WHERE url=? AND id<>?", (url, repo_id)).fetchone()
    if clash is not None:
        return False
    for alias in (row["url"], f"{row['owner']}/{row['name']}"):
        conn.execute(
            "UPDATE flags SET target_id=? WHERE target_type='repo' AND lower(target_id)=lower(?)",
            (url, alias),
        )
    conn.execute("UPDATE repos SET owner=?, name=?, url=? WHERE id=?", (owner, name, url, repo_id))
    return True


def check_repos(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    repo: str | None = None,
    client: GitHubClient | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    owns_client = client is None
    client = client or GitHubClient(settings)
    now = utcnow()
    now_iso = to_iso(now)
    changed: list[dict[str, Any]] = []
    errors: list[str] = []
    merged: list[dict[str, int]] = []
    removed: set[int] = set()
    stopped = False
    checked = 0

    if repo:
        rows = [get_or_create_repo(conn, repo)]
    else:
        rows = conn.execute("SELECT * FROM repos WHERE watch=1 ORDER BY owner, name").fetchall()

    try:
        for row in rows:
            repo_id = row["id"]
            if repo_id in removed:
                continue
            row = _repo_row(conn, repo_id)
            if row is None:
                continue
            if deadline is not None and time.monotonic() > deadline:
                stopped = True
                break
            checked += 1
            try:
                baseline = row["last_checked_at"] is None
                meta_checked = parse_dt(_etags(row).get("meta_checked_at"))

                if meta_checked is None or meta_checked < now - timedelta(days=1):
                    meta_etag = _etags(row).get("meta")
                    try:
                        status, response, new_etag = client.get(
                            f"/repos/{row['owner']}/{row['name']}", etag=meta_etag
                        )
                    except GitHubNotFound as exc:
                        errors.append(f"{row['owner']}/{row['name']}: {exc}")
                        continue
                    meta = None
                    if status != 304 and response is not None:
                        try:
                            meta = response.json()
                        except ValueError:
                            meta = None
                        github_id = meta.get("id") if isinstance(meta, dict) else None
                        if isinstance(github_id, int):
                            _save_github_id(conn, repo_id, github_id)
                            survivor = _merge_by_github_id(conn, repo_id, github_id, merged)
                            removed.update(item["from"] for item in merged)
                            if survivor != repo_id:
                                repo_id = survivor
                                row = _repo_row(conn, repo_id)
                                baseline = row["last_checked_at"] is None
                    _save_meta_checked(conn, repo_id, now_iso)
                    if meta is not None:
                        _save_etag(conn, repo_id, "meta", new_etag)
                        _follow_rename(conn, repo_id, row, meta)
                        row = conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
                        was_archived = row["archived"]
                        archived = 1 if meta.get("archived") else 0
                        if was_archived and not archived:
                            _event(
                                conn, repo_id, "archived", None, now_iso, title="repo unarchived"
                            )
                        conn.execute(
                            "UPDATE repos SET description=?, default_branch=?, archived=?, pushed_at=?"
                            " WHERE id=?",
                            (
                                meta.get("description"),
                                meta.get("default_branch"),
                                archived,
                                meta.get("pushed_at"),
                                repo_id,
                            ),
                        )
                        row = conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
                        if archived and not was_archived and not baseline:
                            _event(
                                conn,
                                repo_id,
                                "archived",
                                None,
                                now_iso,
                                title="repo archived",
                                url=row["url"],
                            )

                branch = row["branch"] or row["default_branch"] or "HEAD"
                head_sha, gone = _fetch_head(conn, client, row, branch)
                if gone:
                    if not baseline:
                        _event(
                            conn,
                            repo_id,
                            "branch_gone",
                            branch,
                            now_iso,
                            title=f"branch {branch} is gone",
                            url=row["url"],
                        )
                        changed.append(
                            {"repo": f"{row['owner']}/{row['name']}", "branch_gone": branch}
                        )
                elif head_sha and head_sha != row["head_sha"]:
                    if baseline:
                        conn.execute(
                            "UPDATE repos SET head_sha=?, head_seen_at=? WHERE id=?",
                            (head_sha, now_iso, repo_id),
                        )
                    else:
                        compare = _compare(conn, client, row, row["head_sha"], head_sha)
                        ahead = compare.get("ahead_by") if compare else None
                        _event(
                            conn,
                            repo_id,
                            "head_moved",
                            head_sha,
                            now_iso,
                            prev_ref=row["head_sha"],
                            commits_ahead=ahead,
                            url=f"{row['url']}/compare/{row['head_sha']}...{head_sha}",
                            occurred_at=now_iso,
                        )
                        conn.execute(
                            "UPDATE repos SET head_sha=?, head_seen_at=? WHERE id=?",
                            (head_sha, now_iso, repo_id),
                        )
                        changed.append(
                            {
                                "repo": f"{row['owner']}/{row['name']}",
                                "head_moved": {
                                    "prev_ref": row["head_sha"],
                                    "ref": head_sha,
                                    "commits_ahead": ahead,
                                },
                            }
                        )

                try:
                    rel_etag = _etags(row).get("release")
                    status, response, new_etag = client.get(
                        f"/repos/{row['owner']}/{row['name']}/releases/latest", etag=rel_etag
                    )
                except GitHubNotFound:
                    response = None
                    status = 404
                if status != 304 and response is not None:
                    release = response.json()
                    tag = release.get("tag_name")
                    _save_etag(conn, repo_id, "release", new_etag)
                    if tag and tag != row["latest_release_tag"]:
                        if not baseline:
                            _event(
                                conn,
                                repo_id,
                                "release",
                                tag,
                                now_iso,
                                title=release.get("name") or tag,
                                body_excerpt=_release_excerpt(release.get("body")),
                                url=release.get("html_url"),
                                occurred_at=release.get("published_at"),
                            )
                            changed.append(
                                {"repo": f"{row['owner']}/{row['name']}", "release": tag}
                            )
                        conn.execute(
                            "UPDATE repos SET latest_release_tag=? WHERE id=?", (tag, repo_id)
                        )

                try:
                    tags_etag = _etags(row).get("tags")
                    status, response, new_etag = client.get(
                        f"/repos/{row['owner']}/{row['name']}/tags",
                        params={"per_page": 10},
                        etag=tags_etag,
                    )
                except GitHubNotFound:
                    response = None
                    status = 404
                if status != 304 and response is not None:
                    tags = [tag.get("name") for tag in response.json() if tag.get("name")]
                    _save_etag(conn, repo_id, "tags", new_etag)
                    known = row["latest_tag"]
                    new_tags = [tag for tag in tags if tag != known]
                    if not baseline:
                        for tag in new_tags:
                            _event(
                                conn,
                                repo_id,
                                "tag",
                                tag,
                                now_iso,
                                title=tag,
                                url=f"{row['url']}/releases/tag/{tag}",
                                occurred_at=now_iso,
                            )
                            changed.append({"repo": f"{row['owner']}/{row['name']}", "tag": tag})
                    if tags:
                        conn.execute("UPDATE repos SET latest_tag=? WHERE id=?", (tags[0], repo_id))

                conn.execute("UPDATE repos SET last_checked_at=? WHERE id=?", (now_iso, repo_id))
            except RateLimitStop:
                stopped = True
                break
            except GitHubError as exc:
                errors.append(f"{row['owner']}/{row['name']}: {exc}")
    finally:
        if owns_client:
            client.close()

    return {
        "checked": checked,
        "requests": client.request_count,
        "changed": changed,
        "errors": errors,
        "merged": merged,
        "stopped_early": stopped,
    }


def watch_repo(
    conn: sqlite3.Connection,
    repo: str,
    watch: bool = True,
    branch: str | None = None,
    note: str | None = None,
    settings: Settings | None = None,
    client: GitHubClient | None = None,
) -> dict[str, Any]:
    row = get_or_create_repo(conn, repo)
    repo_id = row["id"]
    now = utcnow_iso()
    conn.execute(
        "UPDATE repos SET watch=?, branch=?, note=COALESCE(?, note), watched_at=? WHERE id=?",
        (1 if watch else 0, branch, note, now, repo_id),
    )
    row = conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
    baseline = None
    if watch:
        baseline = check_repos(conn, settings, repo=row["url"], client=client)
        merged = {item["from"]: item["to"] for item in baseline.get("merged", [])}
        repo_id = merged.get(repo_id, repo_id)
    row = conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
    return {
        "repo": f"{row['owner']}/{row['name']}",
        "url": row["url"],
        "watch": bool(row["watch"]),
        "branch": row["branch"] or row["default_branch"],
        "head_sha": row["head_sha"],
        "baseline": baseline,
    }


def _resolve_ref(
    client: GitHubClient,
    repo: Any,
    ref: str,
) -> dict[str, Any]:
    """Resolve any ref (full or short SHA, tag, branch) through the commits endpoint.

    SPEC §4b step 1: every ref is resolved with `GET /repos/{o}/{n}/commits/{ref}`, so a
    full SHA is checked too and its subject and date come back. A 404/422 is rejected
    rather than stored.
    """
    text = (ref or "").strip()
    try:
        _status, response, _etag = client.get(
            f"/repos/{repo['owner']}/{repo['name']}/commits/{text}"
        )
    except GitHubNotFound:
        raise ValueError(
            f"cannot resolve {text!r} in {repo['owner']}/{repo['name']}: not found"
        ) from None
    data = response.json() if response is not None else {}
    commit = data.get("commit") or {}
    author = commit.get("author") or {}
    return {
        "sha": data.get("sha", text),
        "subject": (commit.get("message") or "").splitlines()[0] if commit.get("message") else None,
        "date": author.get("date"),
    }


def set_installed(
    conn: sqlite3.Connection,
    repo: str,
    ref: str,
    label: str = "default",
    installed_at: str | None = None,
    note: str | None = None,
    settings: Settings | None = None,
    client: GitHubClient | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    owns_client = client is None
    client = client or GitHubClient(settings)
    try:
        row = get_or_create_repo(conn, repo)
        if _github_id(row) is None:
            row = _resolve_github_id(conn, client, row)
        resolved = _resolve_ref(client, row, ref)
        now = utcnow_iso()
        installed = installed_at or now
        conn.execute(
            "INSERT INTO repo_installs (repo_id, label, sha, ref_given, branch, installed_at, note,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(repo_id, label) DO UPDATE SET sha=excluded.sha, ref_given=excluded.ref_given,"
            " branch=excluded.branch, installed_at=excluded.installed_at,"
            " note=COALESCE(excluded.note, repo_installs.note), updated_at=excluded.updated_at",
            (
                row["id"],
                label,
                resolved["sha"],
                ref,
                row["branch"] or row["default_branch"],
                installed,
                note,
                now,
            ),
        )
        status = repo_status(conn, settings, repo=row["url"], label=label, client=client)
    finally:
        if owns_client:
            client.close()
    return {
        "repo": f"{row['owner']}/{row['name']}",
        "label": label,
        "sha": resolved["sha"],
        "ref_given": ref,
        "subject": resolved["subject"],
        "commit_date": resolved["date"],
        "installed_at": installed_at or now,
        "status": status.get("repos", [{}])[0] if status.get("repos") else None,
        **(
            {
                "hint": (
                    "this repo is not watched, so repo_status() with no repo will not report"
                    " it; call watch_repo to include it"
                )
            }
            if not row["watch"]
            else {}
        ),
    }


def clear_installed(conn: sqlite3.Connection, repo: str, label: str = "default") -> dict[str, Any]:
    row = get_or_create_repo(conn, repo)
    cursor = conn.execute(
        "DELETE FROM repo_installs WHERE repo_id=? AND label=?", (row["id"], label)
    )
    return {
        "repo": f"{row['owner']}/{row['name']}",
        "label": label,
        "removed": bool(cursor.rowcount),
    }


def _resolve_since(
    client: GitHubClient,
    repo: Any,
    since: str,
    branch: str,
) -> str | None:
    if SHA_RE.match(since.strip()):
        return since.strip()
    moment = parse_dt(since)
    if moment is None:
        return None
    _status, response, _etag = client.get(
        f"/repos/{repo['owner']}/{repo['name']}/commits",
        params={"sha": branch, "until": to_iso(moment), "per_page": 1},
    )
    data = response.json() if response is not None else []
    if isinstance(data, list) and data:
        return data[0].get("sha")
    return None


def _changed_dirs(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in files:
        filename = item.get("filename") or ""
        parts = filename.split("/")
        key = parts[0] if len(parts) > 1 else "(root)"
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return [{"path": path, "files": count} for path, count in ordered[:10]]


def _repo_mentions_since(
    conn: sqlite3.Connection, repo: Any, since_iso: str | None, limit: int = 10
) -> list[dict[str, Any]]:
    params: list[Any] = [repo["id"]]
    clause = ""
    if since_iso:
        clause = " AND p.first_fetched_at >= ?"
        params.append(since_iso)
    rows = conn.execute(
        "SELECT p.id AS post_id, p.topic_id, p.post_number, p.first_fetched_at, p.created_at,"
        " t.title, m.context_snippet FROM repo_mentions m"
        " JOIN posts p ON p.id = m.post_id JOIN topics t ON t.id = p.topic_id"
        f" WHERE m.repo_id = ?{clause} ORDER BY p.first_fetched_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [
        {
            "post_id": row["post_id"],
            "topic_id": row["topic_id"],
            "title": row["title"],
            "date": row["created_at"],
            "url": f"https://forums.developer.nvidia.com/t/{row['topic_id']}/{row['post_number']}",
            "snippet": row["context_snippet"],
        }
        for row in rows
    ]


def repo_status(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    repo: str | None = None,
    label: str = "default",
    since: str | None = None,
    client: GitHubClient | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    owns_client = client is None
    client = client or GitHubClient(settings)
    results: list[dict[str, Any]] = []
    try:
        if repo:
            rows = [get_or_create_repo(conn, repo)]
        else:
            rows = conn.execute("SELECT * FROM repos WHERE watch=1 ORDER BY owner, name").fetchall()

        for row in rows:
            branch = row["branch"] or row["default_branch"] or "HEAD"
            install = conn.execute(
                "SELECT * FROM repo_installs WHERE repo_id=? AND label=?", (row["id"], label)
            ).fetchone()
            base_sha: str | None = None
            base_date: str | None = None
            if since:
                base_sha = _resolve_since(client, row, since, branch)
                base_date = since if parse_dt(since) else None
            if base_sha is None and install is not None:
                base_sha = install["sha"]
                base_date = install["installed_at"]

            head_sha = row["head_sha"]
            if head_sha is None:
                head_sha, _gone = _fetch_head(conn, client, row, branch)
                if head_sha:
                    conn.execute(
                        "UPDATE repos SET head_sha=?, head_seen_at=? WHERE id=?",
                        (head_sha, utcnow_iso(), row["id"]),
                    )

            compare = _compare(conn, client, row, base_sha, head_sha) if base_sha else None
            entry: dict[str, Any] = {
                "repo": f"{row['owner']}/{row['name']}",
                "url": row["url"],
                "branch": branch,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "installed": dict(install) if install is not None else None,
                "status": "identical",
                "ahead_by": 0,
                "behind_by": 0,
                "commits": [],
                "commits_total": 0,
                "truncated": False,
                "changed_dirs": [],
                "releases": [],
                "forum_mentions": _repo_mentions_since(conn, row, base_date),
                "flags": [
                    dict(flag)
                    for flag in conn.execute(
                        "SELECT * FROM flags WHERE target_type='repo' AND resolved_at IS NULL"
                        " AND target_id IN (?, ?)",
                        (row["url"], f"{row['owner']}/{row['name']}"),
                    )
                ],
            }
            if compare:
                entry["status"] = COMPARE_STATUS_FROM_BASE.get(
                    compare.get("status", "unknown"), "unknown"
                )
                entry["behind_by"] = compare.get("ahead_by", 0) or 0
                entry["ahead_by"] = compare.get("behind_by", 0) or 0
                entry["commits_total"] = compare.get(
                    "total_commits", entry["behind_by"] + entry["ahead_by"]
                )
                commits = list(reversed(compare.get("commits") or []))
                limit = max(1, settings.repos.max_compare_commits)
                for commit in commits[:limit]:
                    payload = commit.get("commit") or {}
                    author = payload.get("author") or {}
                    entry["commits"].append(
                        {
                            "sha": (commit.get("sha") or "")[:12],
                            "subject": (payload.get("message") or "").splitlines()[0]
                            if payload.get("message")
                            else None,
                            "author": author.get("name")
                            or (commit.get("author") or {}).get("login"),
                            "date": author.get("date"),
                            "url": commit.get("html_url"),
                        }
                    )
                entry["truncated"] = bool(
                    entry["commits_total"] > MAX_COMPARE_COMMITS or len(commits) > limit
                )
                entry["changed_dirs"] = _changed_dirs(compare.get("files") or [])

            release_rows = conn.execute(
                "SELECT kind, ref, title, body_excerpt, url, occurred_at FROM repo_events"
                " WHERE repo_id=? AND kind IN ('release','tag')"
                + (" AND occurred_at >= ?" if base_date else "")
                + " ORDER BY occurred_at DESC",
                (row["id"], base_date) if base_date else (row["id"],),
            ).fetchall()
            entry["releases"] = [dict(release) for release in release_rows]
            results.append(entry)
    finally:
        if owns_client:
            client.close()

    return {"repos": results}


def repo_events(
    conn: sqlite3.Connection,
    since: str | None = None,
    repo: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if repo:
        row = get_or_create_repo(conn, repo)
        clauses.append("e.repo_id = ?")
        params.append(row["id"])
    if since:
        clauses.append("e.detected_at >= ?")
        params.append(since)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT e.*, r.owner, r.name FROM repo_events e JOIN repos r ON r.id = e.repo_id"
        f"{where} ORDER BY e.detected_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [
        {
            "repo": f"{row['owner']}/{row['name']}",
            "kind": row["kind"],
            "ref": row["ref"],
            "prev_ref": row["prev_ref"],
            "commits_ahead": row["commits_ahead"],
            "title": row["title"],
            "url": row["url"],
            "detected_at": row["detected_at"],
            "occurred_at": row["occurred_at"],
        }
        for row in rows
    ]


def list_installs(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    client: GitHubClient | None = None,
) -> list[dict[str, Any]]:
    """Every recorded install with its distance from the branch head.

    The compare is read from the cache, and computed when the cache misses.
    """
    settings = settings or get_settings()
    owns_client = client is None
    client = client or GitHubClient(settings)
    rows = conn.execute(
        "SELECT i.*, r.owner, r.name, r.head_sha, r.branch, r.default_branch FROM repo_installs i"
        " JOIN repos r ON r.id = i.repo_id ORDER BY r.owner, r.name, i.label"
    ).fetchall()
    output: list[dict[str, Any]] = []
    try:
        for row in rows:
            behind = None
            ahead = None
            if row["head_sha"] and row["sha"] != row["head_sha"]:
                cached = conn.execute(
                    "SELECT response FROM compare_cache WHERE repo_id=? AND base_sha=? AND head_sha=?",
                    (row["repo_id"], row["sha"], row["head_sha"]),
                ).fetchone()
                data = loads(cached["response"], {}) if cached is not None else None
                if data is None:
                    repo_row = conn.execute(
                        "SELECT * FROM repos WHERE id=?", (row["repo_id"],)
                    ).fetchone()
                    data = _compare(conn, client, repo_row, row["sha"], row["head_sha"])
                if data is None:
                    behind = ahead = "unknown"
                else:
                    behind = data.get("ahead_by")
                    ahead = data.get("behind_by")
            output.append(
                {
                    "repo": f"{row['owner']}/{row['name']}",
                    "label": row["label"],
                    "sha": row["sha"],
                    "short_sha": row["sha"][:12],
                    "installed_at": row["installed_at"],
                    "head_sha": row["head_sha"],
                    "behind_by": behind,
                    "ahead_by": ahead,
                    "note": row["note"],
                }
            )
    finally:
        if owns_client:
            client.close()
    return output


def list_repos(
    conn: sqlite3.Connection,
    watched: bool | None = None,
    order: str = "mentions",
    limit: int = 20,
) -> list[dict[str, Any]]:
    clause = ""
    if watched is True:
        clause = " WHERE r.watch = 1"
    elif watched is False:
        clause = " WHERE r.watch = 0"
    order_sql = {
        "mentions": "mentions DESC, r.first_seen_at DESC",
        "recent_mention": "last_mention DESC",
        "first_seen": "r.first_seen_at DESC",
    }.get(order, "mentions DESC")
    rows = conn.execute(
        "SELECT r.*, COUNT(m.post_id) AS mentions, MAX(m.mentioned_at) AS last_mention"
        " FROM repos r LEFT JOIN repo_mentions m ON m.repo_id = r.id"
        f"{clause} GROUP BY r.id ORDER BY {order_sql} LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "repo": f"{row['owner']}/{row['name']}",
            "url": row["url"],
            "description": row["description"],
            "watch": bool(row["watch"]),
            "origin": row["origin"],
            "mentions": row["mentions"],
            "last_mention": row["last_mention"],
            "first_seen_at": row["first_seen_at"],
            "head_sha": row["head_sha"],
            "latest_release_tag": row["latest_release_tag"],
        }
        for row in rows
    ]


def get_repo(conn: sqlite3.Connection, repo: str) -> dict[str, Any] | None:
    row = get_or_create_repo(conn, repo)
    installs = conn.execute(
        "SELECT * FROM repo_installs WHERE repo_id=? ORDER BY label", (row["id"],)
    ).fetchall()
    return {
        "repo": f"{row['owner']}/{row['name']}",
        "url": row["url"],
        "description": row["description"],
        "default_branch": row["default_branch"],
        "branch": row["branch"],
        "archived": bool(row["archived"]) if row["archived"] is not None else None,
        "watch": bool(row["watch"]),
        "note": row["note"],
        "head_sha": row["head_sha"],
        "head_seen_at": row["head_seen_at"],
        "latest_release_tag": row["latest_release_tag"],
        "latest_tag": row["latest_tag"],
        "first_seen_at": row["first_seen_at"],
        "watched_at": row["watched_at"],
        "last_checked_at": row["last_checked_at"],
        "installs": [dict(install) for install in installs],
        "mentions": _repo_mentions_since(conn, row, None, limit=10),
        "events": repo_events(conn, repo=row["url"], limit=10),
    }


def parse_json_column(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None
