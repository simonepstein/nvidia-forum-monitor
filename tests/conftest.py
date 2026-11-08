from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"
TEST_FORUM_URL = "http://forum.test"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def synthetic(name: str) -> Any:
    return json.loads((SYNTHETIC / name).read_text())


def live_opted_in(node: pytest.Item) -> bool:
    """Live tests may use the network, but only when the operator opted in."""
    return node.get_closest_marker("live") is not None and os.environ.get("SPARKWATCH_LIVE") == "1"


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any request that respx has not mocked must fail the test.

    Live tests are exempt when SPARKWATCH_LIVE=1 is set.
    """
    if live_opted_in(request.node):
        return

    def guard(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("network access is blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", guard, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", guard, raising=True)


@pytest.fixture(autouse=True)
def _no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real .env must never leak into the test suite."""
    monkeypatch.setenv("SPARKWATCH_ENV_FILE", str(tmp_path / "no-such.env"))


@pytest.fixture(autouse=True)
def _no_pacing_sleep(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise request pacing so mocked runs are fast; live tests keep real pacing."""
    if live_opted_in(request.node):
        return

    from sparkwatch import pacing

    monkeypatch.setattr(pacing, "_sleep", lambda seconds: None)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(tmp_path / "sparkwatch.db"))
    monkeypatch.setenv("SPARKWATCH_FORUM__BASE_URL", TEST_FORUM_URL)
    monkeypatch.setenv("SPARKWATCH_FORUM__REQUESTS_PER_SECOND", "1000")
    from sparkwatch.config import get_settings, reset_settings

    reset_settings()
    value = get_settings()
    yield value
    reset_settings()


@pytest.fixture
def conn(settings):
    from sparkwatch.db import open_db

    return open_db(settings)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


def topic_stub(category_id: int, topic_id: int) -> dict[str, Any]:
    listing = fixture(f"category_{category_id}.json")
    for topic in listing["topic_list"]["topics"]:
        if int(topic["id"]) == topic_id:
            return json.loads(json.dumps(topic))
    raise KeyError(f"topic {topic_id} not in category {category_id} listing")


def iso_days_ago(days: float) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()


def make_topic(
    topic_id: int,
    *,
    bumped_days_ago: float = 1.0,
    created_days_ago: float | None = None,
    posts_count: int = 1,
    highest_post_number: int = 1,
    pinned: bool = False,
    title: str | None = None,
    forum_id: int = 723,
) -> dict[str, Any]:
    bumped = iso_days_ago(bumped_days_ago)
    return {
        "id": topic_id,
        "slug": f"topic-{topic_id}",
        "title": title or f"Topic {topic_id}",
        "tags": [],
        "created_at": iso_days_ago(
            created_days_ago if created_days_ago is not None else bumped_days_ago
        ),
        "bumped_at": bumped,
        "last_posted_at": bumped,
        "posts_count": posts_count,
        "highest_post_number": highest_post_number,
        "views": 10,
        "like_count": 1,
        "pinned": pinned,
        "has_accepted_answer": False,
        "excerpt": f"excerpt for {topic_id}",
        "forum_id": forum_id,
    }


def make_post(
    post_id: int,
    topic_id: int,
    post_number: int,
    raw: str,
    *,
    days_ago: float = 1.0,
    post_type: int = 1,
    username: str = "alice",
    version: int = 1,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created = iso_days_ago(days_ago)
    payload = {
        "id": post_id,
        "topic_id": topic_id,
        "post_number": post_number,
        "post_type": post_type,
        "username": username,
        "trust_level": 2,
        "staff": False,
        "created_at": created,
        "updated_at": created,
        "version": version,
        "reply_to_post_number": None,
        "reaction_users_count": 0,
        "actions_summary": [],
        "reactions": [],
        "accepted_answer": False,
        "cooked": f"<p>{raw}</p>",
        "raw": raw,
    }
    if extra:
        payload.update(extra)
    return payload


def topic_payload(topic: dict[str, Any], posts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": topic["id"],
        "title": topic["title"],
        "post_stream": {
            "posts": posts,
            "stream": [int(post["id"]) for post in posts],
        },
    }


def listing(topics: list[dict[str, Any]], more: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"topic_list": {"topics": topics}}
    if more:
        payload["topic_list"]["more_topics_url"] = "/c/next?page=1"
    return payload


class ForumMock:
    """A tiny stand-in for the Discourse JSON API, driven by recorded fixtures.

    Error scenarios come from tests/fixtures/synthetic/: a list of response specs, served
    in order and repeating the last one.
    """

    def __init__(self) -> None:
        self.pages: dict[int, list[dict[str, Any]]] = {}
        self.topics: dict[int, dict[str, Any]] = {}
        self.posts: dict[int, dict[int, dict[str, Any]]] = {}
        self.topic_responses: dict[int, list[dict[str, Any]]] = {}
        self.posts_responses: dict[int, list[dict[str, Any]]] = {}
        self.counters: dict[tuple[str, int], int] = {}
        self.calls: list[tuple[str, int, Any]] = []

    def add_page(self, category_id: int, payload: dict[str, Any]) -> None:
        self.pages.setdefault(category_id, []).append(payload)

    def add_topic(self, payload: dict[str, Any], posts: list[dict[str, Any]] | None = None) -> None:
        topic_id = int(payload["id"])
        self.topics[topic_id] = payload
        store = self.posts.setdefault(topic_id, {})
        for post in posts if posts is not None else payload["post_stream"]["posts"]:
            store[int(post["id"])] = post

    def add_posts(self, topic_id: int, posts: list[dict[str, Any]]) -> None:
        store = self.posts.setdefault(topic_id, {})
        for post in posts:
            store[int(post["id"])] = post

    def add_topic_responses(self, topic_id: int, specs: list[dict[str, Any]]) -> None:
        self.topic_responses.setdefault(int(topic_id), []).extend(specs)

    def add_posts_responses(self, topic_id: int, specs: list[dict[str, Any]]) -> None:
        self.posts_responses.setdefault(int(topic_id), []).extend(specs)

    def install(self, router: respx.Router) -> None:
        router.get(url__regex=r".*/c/.+\.json.*").mock(side_effect=self._listing)
        router.get(url__regex=r".*/t/\d+\.json.*").mock(side_effect=self._topic)
        router.get(url__regex=r".*/t/\d+/posts\.json.*").mock(side_effect=self._posts)

    def _spec(self, key: tuple[str, int], specs: list[dict[str, Any]]) -> dict[str, Any]:
        index = self.counters.get(key, 0)
        spec = specs[min(index, len(specs) - 1)]
        self.counters[key] = index + 1
        return spec

    @staticmethod
    def _error_response(spec: dict[str, Any]) -> httpx.Response:
        status = int(spec["status"])
        body = spec.get("body") or {"error": status}
        return httpx.Response(status, json=body, headers=spec.get("headers") or {})

    def _listing(self, request: httpx.Request) -> httpx.Response:
        category_id = int(str(request.url.path).rsplit("/", 1)[-1].removesuffix(".json"))
        page = int(request.url.params.get("page", 0))
        self.calls.append(("listing", category_id, page))
        pages = self.pages.get(category_id) or []
        if page >= len(pages):
            return httpx.Response(200, json={"topic_list": {"topics": []}})
        return httpx.Response(200, json=pages[page])

    def _topic(self, request: httpx.Request) -> httpx.Response:
        topic_id = int(re.search(r"/t/(\d+)\.json", str(request.url)).group(1))
        self.calls.append(("topic", topic_id, None))
        specs = self.topic_responses.get(topic_id)
        if specs:
            spec = self._spec(("topic", topic_id), specs)
            if int(spec["status"]) >= 400:
                return self._error_response(spec)
        payload = self.topics.get(topic_id)
        if payload is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=payload)

    def _posts(self, request: httpx.Request) -> httpx.Response:
        topic_id = int(re.search(r"/t/(\d+)/posts\.json", str(request.url)).group(1))
        requested = [int(value) for value in request.url.params.get_list("post_ids[]")]
        self.calls.append(("posts", topic_id, tuple(requested)))
        specs = self.posts_responses.get(topic_id)
        if specs:
            spec = self._spec(("posts", topic_id), specs)
            if int(spec["status"]) >= 400:
                return self._error_response(spec)
        store = self.posts.get(topic_id, {})
        payload = {"post_stream": {"posts": [store[pid] for pid in requested if pid in store]}}
        return httpx.Response(200, json=payload)

    def post_requests(self) -> list[tuple[int, ...]]:
        return [call[2] for call in self.calls if call[0] == "posts"]


def add_github_spec(github: GitHubMock, path: str, name: str, times: int = 1) -> None:
    """Queue a synthetic GitHub response (tests/fixtures/synthetic/<name>) for a path."""
    spec = synthetic(name)
    for _ in range(times):
        github.add(
            path,
            spec.get("body"),
            status=int(spec["status"]),
            headers=spec.get("headers") or {},
        )


class GitHubMock:
    def __init__(self) -> None:
        self.routes: dict[str, list[tuple[int, Any, dict[str, str]]]] = {}
        self.counters: dict[str, int] = {}
        self.calls: list[str] = []
        self.requests: list[httpx.Request] = []

    def add(
        self,
        path: str,
        payload: Any,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Queue a response for `path`; a callable payload gets the request and returns one."""
        self.routes.setdefault(path, []).append((status, payload, headers or {}))

    def install(self, router: respx.Router) -> None:
        router.get(url__regex=r"https://api\.github\.com/.*").mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        self.requests.append(request)
        queued = self.routes.get(path)
        if not queued:
            return httpx.Response(404, json={"message": "Not Found"})
        index = self.counters.get(path, 0)
        status, payload, headers = queued[min(index, len(queued) - 1)]
        self.counters[path] = index + 1
        if callable(payload):
            return payload(request)
        if status == 304:
            return httpx.Response(304, headers=headers)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)


@pytest.fixture
def forum() -> ForumMock:
    return ForumMock()


@pytest.fixture
def github() -> GitHubMock:
    return GitHubMock()
