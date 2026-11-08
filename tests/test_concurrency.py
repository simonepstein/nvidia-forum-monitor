from __future__ import annotations

import sqlite3
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from sparkwatch import mcp_server as m
from sparkwatch.cli import app
from sparkwatch.lock import LockBusy, write_lock


def test_sync_now_returns_busy_while_locked(settings, conn):
    with write_lock("sync", settings.db_path):
        payload = m.sync_now()
    assert payload["busy"] is True
    assert payload["job"] == "sync"


def test_check_repos_now_returns_busy_while_locked(settings, conn):
    with write_lock("repos", settings.db_path):
        payload = m.check_repos_now()
    assert payload["busy"] is True
    assert payload["job"] == "repos"


def test_cli_sync_exits_busy(settings, monkeypatch):
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(settings.db_path))
    runner = CliRunner()
    with write_lock("sync", settings.db_path):
        result = runner.invoke(app, ["sync", "--max-pages", "1"])
    assert result.exit_code == 2
    assert "busy" in result.stdout


def test_read_tools_work_during_a_write_transaction(settings, conn):
    conn.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, first_fetched_at)"
        " VALUES (950, 723, 'wal topic', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO posts (id, topic_id, post_number, post_type, raw, cooked, content_hash,"
        " first_fetched_at, changed_at) VALUES (9501, 950, 1, 1, 'tensor parallel over connectx',"
        " '<p>x</p>', 'h', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )

    writer = sqlite3.connect(settings.db_path, isolation_level=None)
    writer.execute("PRAGMA busy_timeout=500")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO topics (id, forum_id, title, created_at, first_fetched_at)"
        " VALUES (951, 723, 'in flight', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    try:
        started = time.monotonic()
        found = m.search_posts("connectx")
        sql = m.sql_query("SELECT COUNT(*) AS n FROM topics")
        elapsed = time.monotonic() - started
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert found["count"] == 1
    assert sql["rows"][0]["n"] == 1
    assert elapsed < 2.0


def test_lock_is_not_stale_after_a_crash(settings, tmp_path):
    db_path = settings.db_path
    code = (
        "import sys, time\n"
        "from sparkwatch.lock import write_lock\n"
        "with write_lock('sync', sys.argv[1]):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(db_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusy):
            with write_lock("sync", db_path):
                pass
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

    with write_lock("sync", db_path):
        pass
