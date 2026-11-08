from __future__ import annotations

from typing import Any

import pytest
from conftest import live_opted_in


class FakeNode:
    def __init__(self, marker: Any) -> None:
        self._marker = marker

    def get_closest_marker(self, name: str) -> Any:
        return self._marker if name == "live" else None


def test_live_marker_with_opt_in_bypasses_the_network_block(monkeypatch):
    node = FakeNode(object())
    monkeypatch.setenv("SPARKWATCH_LIVE", "1")
    assert live_opted_in(node) is True


def test_live_marker_without_opt_in_stays_blocked(monkeypatch):
    monkeypatch.delenv("SPARKWATCH_LIVE", raising=False)
    assert live_opted_in(FakeNode(object())) is False
    monkeypatch.setenv("SPARKWATCH_LIVE", "0")
    assert live_opted_in(FakeNode(object())) is False


def test_unmarked_tests_are_always_blocked(monkeypatch):
    monkeypatch.setenv("SPARKWATCH_LIVE", "1")
    assert live_opted_in(FakeNode(None)) is False


def test_non_live_test_still_cannot_reach_the_network(settings):
    import socket

    with pytest.raises(RuntimeError, match="network access is blocked"):
        socket.create_connection(("127.0.0.1", 9), timeout=1.0)
