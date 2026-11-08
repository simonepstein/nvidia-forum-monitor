from __future__ import annotations

import threading
import time
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .config import CategorySettings, Settings, get_settings


class DiscourseError(Exception):
    def __init__(
        self, status: int, url: str, message: str = "", retry_after: float | None = None
    ) -> None:
        self.status = status
        self.url = url
        self.retry_after = retry_after
        super().__init__(f"{status} {url} {message}".strip())


class RetryableError(DiscourseError):
    pass


class NonRetryableError(DiscourseError):
    pass


class ForbiddenError(NonRetryableError):
    pass


class NotFoundError(NonRetryableError):
    pass


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                time.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self.min_interval


def _wait_strategy(retry_state: Any) -> float:
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        return float(retry_after)
    return wait_exponential_jitter(initial=1, max=30, jitter=1)(retry_state)


class DiscourseClient:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        sleeper: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.request_count = 0
        self.limiter = RateLimiter(self.settings.forum.requests_per_second)
        self._sleeper = sleeper
        self._client = client or httpx.Client(
            base_url=self.settings.forum.base_url.rstrip("/"),
            headers={"User-Agent": self.settings.forum.user_agent},
            timeout=self.settings.forum.timeout_s,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DiscourseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _category_path(self, category_id: int) -> str:
        for category in self.settings.forum.categories:
            if category.id == category_id:
                return category.path
        raise KeyError(f"unknown category {category_id}")

    def _send(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self.limiter.wait()
        self.request_count += 1
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise RetryableError(0, path, str(exc)) from exc

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.strip().isdigit() else 1.0
            raise RetryableError(status, str(response.url), "rate limited", retry_after=delay)
        if status >= 500:
            raise RetryableError(status, str(response.url), "server error")
        if status in (401, 403):
            raise ForbiddenError(status, str(response.url), "forbidden")
        if status == 404:
            raise NotFoundError(status, str(response.url), "not found")
        if status >= 400:
            raise NonRetryableError(status, str(response.url), "client error")
        return response

    def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        retrying = Retrying(
            retry=retry_if_exception_type(RetryableError),
            wait=_wait_strategy,
            stop=stop_after_attempt(max(1, self.settings.forum.max_retries + 1)),
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                return self._send(path, params)
        raise AssertionError("unreachable")

    def list_category_page(self, category_id: int, page: int = 0) -> dict[str, Any]:
        path = f"/c/{self._category_path(category_id)}/{category_id}.json"
        return self._request(path, {"order": "activity", "page": page}).json()

    def get_topic(self, topic_id: int) -> dict[str, Any]:
        return self._request(f"/t/{topic_id}.json", {"include_raw": "true"}).json()

    def get_posts(self, topic_id: int, post_ids: list[int]) -> dict[str, Any]:
        return self._request(
            f"/t/{topic_id}/posts.json",
            {"post_ids[]": [str(pid) for pid in post_ids], "include_raw": "true"},
        ).json()

    def get_raw(self, topic_id: int, post_number: int) -> str:
        return self._request(f"/raw/{topic_id}/{post_number}").text


def category_by_id(settings: Settings, category_id: int) -> CategorySettings | None:
    for category in settings.forum.categories:
        if category.id == category_id:
            return category
    return None
