from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

if os.environ.get("SPARKWATCH_LIVE") != "1":
    pytest.skip("set SPARKWATCH_LIVE=1 to run the live drift tests", allow_module_level=True)

TOPIC_FIELDS = (
    "id",
    "slug",
    "title",
    "created_at",
    "bumped_at",
    "last_posted_at",
    "posts_count",
    "highest_post_number",
    "views",
    "like_count",
    "pinned",
    "has_accepted_answer",
    "excerpt",
)

POST_FIELDS = (
    "id",
    "topic_id",
    "post_number",
    "post_type",
    "username",
    "created_at",
    "version",
    "cooked",
    "raw",
)


def test_listing_shape_has_not_drifted():
    from sparkwatch.config import get_settings
    from sparkwatch.discourse import DiscourseClient

    settings = get_settings()
    client = DiscourseClient(settings)
    try:
        payload = client.list_category_page(721, 0)
    finally:
        client.close()

    topics = payload["topic_list"]["topics"]
    assert topics, "category 721 returned no topics"
    first = topics[0]
    for field in TOPIC_FIELDS:
        assert field in first, f"listing field {field} is gone"
    assert isinstance(first["id"], int)
    assert isinstance(first["posts_count"], int)


def test_topic_and_posts_shape_has_not_drifted():
    from sparkwatch.config import get_settings
    from sparkwatch.discourse import DiscourseClient

    settings = get_settings()
    client = DiscourseClient(settings)
    try:
        listing = client.list_category_page(721, 0)
        topic_id = next(
            int(topic["id"]) for topic in listing["topic_list"]["topics"] if not topic.get("pinned")
        )
        topic = client.get_topic(topic_id)
        stream = topic["post_stream"]["stream"]
        assert stream and isinstance(stream[0], int)
        batch = client.get_posts(topic_id, stream[:5])
    finally:
        client.close()

    posts = batch["post_stream"]["posts"]
    assert posts
    for field in POST_FIELDS:
        assert field in posts[0], f"post field {field} is gone"
    assert isinstance(posts[0]["raw"], str)


def test_github_head_shape_has_not_drifted():
    from sparkwatch.config import get_settings
    from sparkwatch.repos import GitHubClient

    settings = get_settings()
    client = GitHubClient(settings)
    try:
        _status, response, _etag = client.get(
            "/repos/vllm-project/vllm/commits/main", accept="application/vnd.github.sha"
        )
    finally:
        client.close()

    sha = (response.text if response is not None else "").strip()
    assert len(sha) == 40
    assert all(character in "0123456789abcdef" for character in sha)
