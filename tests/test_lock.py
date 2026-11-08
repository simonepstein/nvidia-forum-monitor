from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sparkwatch.cli import app
from sparkwatch.lock import LockBusy, lock_path, write_lock


def test_lock_is_exclusive(settings):
    with write_lock("sync", settings.db_path):
        with pytest.raises(LockBusy) as excinfo:
            with write_lock("repos", settings.db_path):
                pass
    assert excinfo.value.job == "sync"
    assert excinfo.value.started_at is not None
    assert excinfo.value.as_dict()["busy"] is True


def test_lock_is_released(settings):
    with write_lock("sync", settings.db_path):
        pass
    with write_lock("sync", settings.db_path):
        pass
    assert lock_path(settings.db_path).exists()


def test_lock_file_records_holder(settings):
    with write_lock("sync", settings.db_path):
        text = lock_path(settings.db_path).read_text()
        assert '"job": "sync"' in text
        assert '"pid"' in text


def test_busy_payload_waits_for_the_holder_record(settings, monkeypatch):
    from sparkwatch import lock

    calls = {"count": 0}
    real = lock._read_holder

    def flaky(handle):
        calls["count"] += 1
        if calls["count"] == 1:
            return {}
        return real(handle)

    monkeypatch.setattr(lock, "_read_holder", flaky)
    monkeypatch.setattr(lock.time, "sleep", lambda seconds: None)

    with write_lock("sync+repos", settings.db_path):
        with pytest.raises(LockBusy) as excinfo:
            with write_lock("repos", settings.db_path):
                pass

    assert calls["count"] >= 2
    assert excinfo.value.job == "sync+repos"
    assert excinfo.value.started_at is not None


def test_busy_payload_names_a_subprocess_holder(settings):
    import subprocess
    import sys

    code = (
        "import sys, time\n"
        "from sparkwatch.lock import write_lock\n"
        "with write_lock('repos check', sys.argv[1]):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(settings.db_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusy) as excinfo:
            with write_lock("sync", settings.db_path):
                pass
        assert excinfo.value.job == "repos check"
    finally:
        process.kill()
        process.wait(timeout=10)


def test_cli_exits_busy(settings, monkeypatch):
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(settings.db_path))
    runner = CliRunner()
    with write_lock("sync", settings.db_path):
        result = runner.invoke(app, ["sync", "--max-pages", "1"])
    assert result.exit_code == 2
    assert '"busy"' in result.stdout
