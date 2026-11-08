from __future__ import annotations

from sparkwatch import pacing
from sparkwatch.discourse import DiscourseClient
from sparkwatch.pacing import RateLimiter
from sparkwatch.repos import GITHUB_REQUESTS_PER_SECOND, GitHubClient


def test_zero_rate_is_not_unlimited():
    assert RateLimiter(0).min_interval == 1.0
    assert RateLimiter(-5).min_interval == 1.0


def test_rate_limiter_uses_the_pacing_hook(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(pacing, "_sleep", slept.append)
    limiter = RateLimiter(1.0)
    limiter.wait()
    limiter.wait()
    assert slept and slept[0] > 0


def test_github_client_paces_at_one_per_second(settings):
    client = GitHubClient(settings)
    try:
        assert GITHUB_REQUESTS_PER_SECOND == 1.0
        assert client.limiter.min_interval == 1.0
    finally:
        client.close()


def test_discourse_client_uses_the_configured_rate(settings):
    client = DiscourseClient(settings)
    try:
        assert client.limiter.min_interval == 1.0 / settings.forum.requests_per_second
    finally:
        client.close()
