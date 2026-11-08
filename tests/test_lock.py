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


def test_cli_exits_busy(settings, monkeypatch):
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(settings.db_path))
    runner = CliRunner()
    with write_lock("sync", settings.db_path):
        result = runner.invoke(app, ["sync", "--max-pages", "1"])
    assert result.exit_code == 2
    assert '"busy"' in result.stdout
