from __future__ import annotations

import time

import httpx
import pytest
from conftest import listing, make_post, make_topic

from sparkwatch.discourse import (
    DiscourseClient,
    ForbiddenError,
    NotFoundError,
    RateLimiter,
    RetryableError,
)


def test_list_category_page_builds_discourse_url(settings, router, forum):
    forum.add_page(721, listing([make_topic(11)]))
    forum.install(router)
    client = DiscourseClient(settings)
    payload = client.list_category_page(721, 0)
    assert payload["topic_list"]["topics"][0]["id"] == 11
    assert forum.calls == [("listing", 721, 0)]


def test_get_posts_sends_post_ids(settings, router, forum):
    posts = [make_post(1201, 12, 1, "hello"), make_post(1202, 12, 2, "world")]
    forum.add_topic(
        {"id": 12, "title": "t", "post_stream": {"posts": posts, "stream": [1201, 1202]}}, posts
    )
    forum.install(router)
    client = DiscourseClient(settings)
    payload = client.get_posts(12, [1202, 1201])
    assert [post["id"] for post in payload["post_stream"]["posts"]] == [1202, 1201]
    assert forum.post_requests() == [(1202, 1201)]


def test_rate_limiter_spaces_requests():
    limiter = RateLimiter(2.0)
    limiter.wait()
    started = time.monotonic()
    limiter.wait()
    assert time.monotonic() - started >= 0.4


def test_429_honours_retry_after(settings, router, forum):
    forum.add_topic({"id": 13, "title": "t", "post_stream": {"posts": [], "stream": []}})
    forum.topic_status[13] = [429, 200]
    forum.install(router)
    client = DiscourseClient(settings)
    started = time.monotonic()
    payload = client.get_topic(13)
    elapsed = time.monotonic() - started
    assert payload["id"] == 13
    assert elapsed >= 0.9
    assert client.request_count == 2


def test_5xx_retries_then_raises(settings, router, forum):
    settings.forum.max_retries = 1
    forum.topic_status[14] = [500]
    forum.install(router)
    client = DiscourseClient(settings)
    with pytest.raises(RetryableError):
        client.get_topic(14)
    assert client.request_count == 2


def test_404_and_403_are_not_retried(settings, router, forum):
    forum.topic_status[15] = [404]
    forum.topic_status[16] = [403]
    forum.install(router)
    client = DiscourseClient(settings)
    with pytest.raises(NotFoundError):
        client.get_topic(15)
    with pytest.raises(ForbiddenError):
        client.get_topic(16)
    assert client.request_count == 2


def test_user_agent_is_generic(settings, router):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"topic_list": {"topics": []}})

    router.get(url__regex=r".*/c/.*\.json.*").mock(side_effect=handler)
    client = DiscourseClient(settings)
    client.list_category_page(723, 0)
    assert captured["user-agent"] == "sparkwatch/0.1"
    assert "@" not in captured["user-agent"]
