"""Record one small live sample into tests/fixtures/.

Run this once, at the start of Stage 2, and never as part of the test suite:

    uv run python scripts/record_fixtures.py

It goes through DiscourseClient and GitHubClient, so it uses the real rate limiter and
User-Agent. GitHub requests are anonymous. Fixtures are trimmed to the posts the tests
need, but the full `stream` array is kept so pagination is still exercised. Existing
fixture files are reused, so a re-run only fetches what is missing.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
LONG_TOPIC = 381350
LONG_TOPIC_BATCHES = 3
SMALL_TOPIC_BATCHES = 2
FALLBACK_REPO = "vllm-project/vllm"


def write_json(name: str, payload: Any) -> None:
    path = FIXTURES / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")


def cached_json(name: str, fetch: Callable[[], Any]) -> Any:
    path = FIXTURES / name
    if path.is_file():
        print(f"  cached {path.relative_to(ROOT)}")
        return json.loads(path.read_text())
    payload = fetch()
    write_json(name, payload)
    return payload


def trim_topic(data: dict[str, Any], keep: int) -> dict[str, Any]:
    post_stream = data.get("post_stream") or {}
    posts = post_stream.get("posts") or []
    trimmed = dict(data)
    trimmed["post_stream"] = {
        "posts": posts[:keep],
        "stream": post_stream.get("stream") or [],
    }
    return trimmed


def trim_batch(data: dict[str, Any], keep: int) -> dict[str, Any]:
    post_stream = data.get("post_stream") or {}
    trimmed = dict(data)
    trimmed["post_stream"] = {"posts": (post_stream.get("posts") or [])[:keep]}
    return trimmed


def pick_topics(listing: dict[str, Any], wanted: int) -> list[int]:
    topics = (listing.get("topic_list") or {}).get("topics") or []
    ids: list[int] = []
    for topic in topics:
        if topic.get("pinned"):
            continue
        ids.append(int(topic["id"]))
        if len(ids) >= wanted:
            break
    return ids


def find_repo(posts: list[dict[str, Any]]) -> str:
    from sparkwatch.links import normalize_github

    for post in posts:
        for token in (post.get("raw") or "").split():
            url = normalize_github(token)
            if url:
                return url.removeprefix("https://github.com/")
    return FALLBACK_REPO


def record_github(repo: str, settings: Any) -> bool:
    from sparkwatch.repos import GitHubClient

    print(f"recording github fixtures for {repo}")
    github = GitHubClient(settings)
    try:
        meta = cached_json(
            f"github_{repo.replace('/', '__')}_repo.json", lambda: _meta(github, repo)
        )
        if not isinstance(meta, dict) or "message" in meta:
            return False
        branch = meta.get("default_branch") or "main"

        cached_json(
            f"github_{repo.replace('/', '__')}_head.json",
            lambda: {"sha": _head(github, repo, branch), "branch": branch},
        )
        cached_json(
            f"github_{repo.replace('/', '__')}_release.json", lambda: _release(github, repo)
        )
        cached_json(f"github_{repo.replace('/', '__')}_tags.json", lambda: _tags(github, repo))
        commits = cached_json(
            f"github_{repo.replace('/', '__')}_commits.json", lambda: _commits(github, repo, branch)
        )
        if not isinstance(commits, list) or len(commits) < 2:
            return False
        base = commits[-1]["sha"]
        head_sha = commits[0]["sha"]
        cached_json(
            f"github_{repo.replace('/', '__')}_compare.json",
            lambda: _compare(github, repo, base, head_sha),
        )
        write_json(
            f"github_{repo.replace('/', '__')}_compare_pair.json", {"base": base, "head": head_sha}
        )
        cached_json(
            f"github_{repo.replace('/', '__')}_commits_until.json",
            lambda: _commits_until(github, repo, branch),
        )
        write_json("github_repo_name.json", {"repo": repo})
        return True
    finally:
        github.close()


def _meta(github: Any, repo: str) -> Any:
    _status, response, _etag = github.get(f"/repos/{repo}")
    return response.json() if response is not None else {}


def _head(github: Any, repo: str, branch: str) -> str:
    _status, response, _etag = github.get(
        f"/repos/{repo}/commits/{branch}", accept="application/vnd.github.sha"
    )
    return (response.text if response is not None else "").strip()


def _release(github: Any, repo: str) -> Any:
    try:
        _status, response, _etag = github.get(f"/repos/{repo}/releases/latest")
        return response.json() if response is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def _tags(github: Any, repo: str) -> Any:
    _status, response, _etag = github.get(f"/repos/{repo}/tags", params={"per_page": 10})
    return response.json() if response is not None else []


def _commits(github: Any, repo: str, branch: str) -> Any:
    _status, response, _etag = github.get(
        f"/repos/{repo}/commits", params={"sha": branch, "per_page": 3}
    )
    return response.json() if response is not None else []


def _compare(github: Any, repo: str, base: str, head: str) -> Any:
    _status, response, _etag = github.get(f"/repos/{repo}/compare/{base}...{head}")
    return response.json() if response is not None else {}


def _commits_until(github: Any, repo: str, branch: str) -> Any:
    _status, response, _etag = github.get(
        f"/repos/{repo}/commits",
        params={"sha": branch, "until": "2026-01-01T00:00:00Z", "per_page": 1},
    )
    return response.json() if response is not None else []


def main() -> int:
    from sparkwatch.config import get_settings
    from sparkwatch.discourse import DiscourseClient

    settings = get_settings()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    client = DiscourseClient(settings)
    print("recording discourse fixtures")
    try:
        listings: dict[int, dict[str, Any]] = {}
        for category in settings.forum.categories:
            listings[category.id] = cached_json(
                f"category_{category.id}.json",
                lambda category=category: client.list_category_page(category.id, 0),
            )

        topic_ids: list[int] = [LONG_TOPIC]
        for listing in listings.values():
            for topic_id in pick_topics(listing, 2):
                if topic_id not in topic_ids:
                    topic_ids.append(topic_id)
        topic_ids = topic_ids[:3]

        all_posts: list[dict[str, Any]] = []
        for index, topic_id in enumerate(topic_ids):
            topic = cached_json(
                f"topic_{topic_id}.json",
                lambda topic_id=topic_id: trim_topic(client.get_topic(topic_id), keep=20),
            )
            stream = (topic.get("post_stream") or {}).get("stream") or []
            batch_count = LONG_TOPIC_BATCHES if topic_id == LONG_TOPIC else SMALL_TOPIC_BATCHES
            ordered = list(reversed(stream))
            batch_size = settings.forum.posts_batch_size
            for batch_index in range(batch_count):
                ids = ordered[batch_index * batch_size : (batch_index + 1) * batch_size]
                if not ids:
                    break
                payload = cached_json(
                    f"posts_{topic_id}_{batch_index}.json",
                    lambda topic_id=topic_id, ids=ids, keep=batch_size: trim_batch(
                        client.get_posts(topic_id, ids), keep=keep
                    ),
                )
                all_posts.extend((payload.get("post_stream") or {}).get("posts") or [])
            if index == 0 and all_posts and not (FIXTURES / "edited_post.json").is_file():
                edited = dict(all_posts[0])
                edited["raw"] = (
                    edited.get("raw") or ""
                ) + "\n\nedited: fp8 works, nvfp4 OOMs at 128k context."
                edited["version"] = (edited.get("version") or 1) + 1
                write_json("edited_post.json", edited)
    finally:
        client.close()

    repo = find_repo(all_posts)
    if not record_github(repo, settings):
        print(f"  {repo} was unusable, falling back to {FALLBACK_REPO}")
        if not record_github(FALLBACK_REPO, settings):
            print("  github recording failed for both repos")
            return 1

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
