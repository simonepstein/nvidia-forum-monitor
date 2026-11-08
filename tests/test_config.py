from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sparkwatch.config import (
    LIVE_FORUM_HOST,
    MAX_LIVE_REQUESTS_PER_SECOND,
    ForumSettings,
    get_settings,
    reset_settings,
)


def test_toml_values_are_loaded(settings):
    assert settings.forum.user_agent == "sparkwatch/0.1"
    assert settings.sync.lookback_days == 60
    assert settings.sync.refetch_recent_days == 7
    assert settings.sync.missing_post_recheck_days == 30
    assert settings.hardware.dual_spark_usable_gb == 240
    assert settings.hardware.single_spark_usable_gb == 120
    assert settings.mcp.default_limit == 20
    assert settings.mcp.max_limit == 200
    assert settings.global_digest.weights["posters"] == 1.5
    assert settings.global_digest.weights["staff"] == 0.5
    assert settings.global_digest.relevance_cap == 40.0
    assert settings.mcp.max_chars == 40000
    assert settings.forum_ids() == [721, 723]
    assert settings.relevance.weights["watch"] == 4.0
    assert "qwen" in settings.relevance.families
    assert "dual spark" in settings.relevance.keywords["dual"]


def test_global_digest_settings_validate_the_cap_and_weight_names():
    from sparkwatch.config import GlobalDigestSettings

    with pytest.raises(ValidationError):
        GlobalDigestSettings(relevance_cap=0)
    with pytest.raises(ValidationError):
        GlobalDigestSettings(relevance_cap=-5)
    with pytest.raises(ValidationError, match="unknown global_digest weight"):
        GlobalDigestSettings(weights={"posts": 1.0, "postz": 2.0})


def test_toml_defaults_point_at_the_live_forum(monkeypatch, tmp_path):
    monkeypatch.delenv("SPARKWATCH_FORUM__BASE_URL", raising=False)
    monkeypatch.delenv("SPARKWATCH_FORUM__REQUESTS_PER_SECOND", raising=False)
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(tmp_path / "toml.db"))
    reset_settings()
    try:
        value = get_settings()
        assert value.forum.base_url == f"https://{LIVE_FORUM_HOST}"
        assert value.forum.requests_per_second == MAX_LIVE_REQUESTS_PER_SECOND
    finally:
        reset_settings()


def test_env_overrides_toml(settings):
    assert settings.forum.requests_per_second == 1000
    assert Path(settings.db_path).name == "sparkwatch.db"


def test_nested_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(tmp_path / "custom.db"))
    monkeypatch.setenv("SPARKWATCH_SYNC__LOOKBACK_DAYS", "14")
    reset_settings()
    value = get_settings()
    assert value.general.db_path.endswith("custom.db")
    assert value.sync.lookback_days == 14
    reset_settings()


def test_categories_carry_paths(settings):
    paths = {category.id: category.path for category in settings.forum.categories}
    assert paths[721] == "accelerated-computing/dgx-spark-gb10/dgx-spark-gb10"
    assert paths[723].endswith("dgx-spark-gb10-projects")


@pytest.mark.parametrize("rate", ["1000", "1.5", "0", "-1"])
def test_live_forum_rejects_an_unsafe_rate(rate, monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(tmp_path / "live.db"))
    monkeypatch.setenv("SPARKWATCH_FORUM__BASE_URL", f"https://{LIVE_FORUM_HOST}")
    monkeypatch.setenv("SPARKWATCH_FORUM__REQUESTS_PER_SECOND", rate)
    reset_settings()
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        reset_settings()


@pytest.mark.parametrize("rate", ["1.0", "0.5"])
def test_live_forum_accepts_a_polite_rate(rate, monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(tmp_path / "polite.db"))
    monkeypatch.setenv("SPARKWATCH_FORUM__BASE_URL", f"https://{LIVE_FORUM_HOST}")
    monkeypatch.setenv("SPARKWATCH_FORUM__REQUESTS_PER_SECOND", rate)
    reset_settings()
    try:
        assert get_settings().forum.requests_per_second == float(rate)
    finally:
        reset_settings()


def test_non_live_host_may_run_fast():
    fast = ForumSettings(base_url="http://forum.test", requests_per_second=1000)
    assert fast.requests_per_second == 1000
    with pytest.raises(ValidationError):
        ForumSettings(base_url=f"https://{LIVE_FORUM_HOST}", requests_per_second=1000)


def test_cli_reports_an_unsafe_config_without_a_traceback(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from sparkwatch.cli import app

    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("SPARKWATCH_FORUM__BASE_URL", f"https://{LIVE_FORUM_HOST}")
    monkeypatch.setenv("SPARKWATCH_FORUM__REQUESTS_PER_SECOND", "1000")
    reset_settings()
    try:
        result = CliRunner().invoke(app, ["digest"])
    finally:
        reset_settings()

    assert result.exit_code == 1
    assert "error: forum.requests_per_second must be at most 1.0" in result.stdout
    assert "Traceback" not in result.stdout


def test_mcp_server_refuses_to_start_with_an_unsafe_rate(monkeypatch, tmp_path):
    from sparkwatch import mcp_server

    monkeypatch.setenv("SPARKWATCH_GENERAL__DB_PATH", str(tmp_path / "unsafe.db"))
    monkeypatch.setenv("SPARKWATCH_FORUM__BASE_URL", f"https://{LIVE_FORUM_HOST}")
    monkeypatch.setenv("SPARKWATCH_FORUM__REQUESTS_PER_SECOND", "1000")
    reset_settings()
    started: list[dict[str, object]] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kwargs: started.append(kwargs))
    try:
        with pytest.raises(SystemExit) as excinfo:
            mcp_server.main()
        assert started == []
        assert "must be at most 1.0" in str(excinfo.value)
    finally:
        reset_settings()


def test_env_file_supplies_secrets_but_the_environment_wins(tmp_path, monkeypatch):
    from sparkwatch.config import load_env_file

    env_file = tmp_path / ".env"
    env_file.write_text("SW_TEST_FILE_ONLY=from-file\nSW_TEST_BOTH=from-file\n")
    monkeypatch.setenv("SPARKWATCH_ENV_FILE", str(env_file))
    monkeypatch.delenv("SW_TEST_FILE_ONLY", raising=False)
    monkeypatch.setenv("SW_TEST_BOTH", "from-env")

    assert load_env_file() is True
    import os

    assert os.environ["SW_TEST_FILE_ONLY"] == "from-file"
    assert os.environ["SW_TEST_BOTH"] == "from-env"
    monkeypatch.delenv("SW_TEST_FILE_ONLY")


def test_missing_env_file_is_fine(tmp_path, monkeypatch):
    from sparkwatch.config import load_env_file

    monkeypatch.setenv("SPARKWATCH_ENV_FILE", str(tmp_path / "absent.env"))
    assert load_env_file() is False
    reset_settings()
    assert get_settings() is not None
    reset_settings()


def test_get_settings_loads_the_env_file(tmp_path, monkeypatch, settings):
    import os

    env_file = tmp_path / ".env"
    env_file.write_text("SW_TEST_VIA_SETTINGS=yes\n")
    monkeypatch.setenv("SPARKWATCH_ENV_FILE", str(env_file))
    monkeypatch.delenv("SW_TEST_VIA_SETTINGS", raising=False)
    reset_settings()
    get_settings()
    assert os.environ.get("SW_TEST_VIA_SETTINGS") == "yes"
    monkeypatch.delenv("SW_TEST_VIA_SETTINGS")
