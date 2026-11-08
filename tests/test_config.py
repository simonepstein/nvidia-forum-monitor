from __future__ import annotations

from pathlib import Path

from sparkwatch.config import get_settings, reset_settings


def test_toml_values_are_loaded(settings):
    assert settings.forum.base_url == "https://forums.developer.nvidia.com"
    assert settings.forum.user_agent == "sparkwatch/0.1"
    assert settings.sync.lookback_days == 60
    assert settings.sync.refetch_recent_days == 7
    assert settings.sync.missing_post_recheck_days == 30
    assert settings.hardware.dual_spark_usable_gb == 240
    assert settings.hardware.single_spark_usable_gb == 120
    assert settings.mcp.default_limit == 20
    assert settings.mcp.max_limit == 200
    assert settings.mcp.max_chars == 40000
    assert settings.forum_ids() == [721, 723]
    assert settings.relevance.weights["watch"] == 4.0
    assert "qwen" in settings.relevance.families
    assert "dual spark" in settings.relevance.keywords["dual"]


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
    assert value.forum.base_url == "https://forums.developer.nvidia.com"
    reset_settings()


def test_categories_carry_paths(settings):
    paths = {category.id: category.path for category in settings.forum.categories}
    assert paths[721] == "accelerated-computing/dgx-spark-gb10/dgx-spark-gb10"
    assert paths[723].endswith("dgx-spark-gb10-projects")
