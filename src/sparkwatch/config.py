from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def config_path() -> Path:
    explicit = os.environ.get("SPARKWATCH_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    local = Path.cwd() / "config.toml"
    if local.is_file():
        return local.resolve()
    return PROJECT_ROOT / "config.toml"


class GeneralSettings(BaseModel):
    db_path: str = "data/sparkwatch.db"
    log_level: str = "INFO"


class CategorySettings(BaseModel):
    id: int
    path: str
    name: str


class ForumSettings(BaseModel):
    base_url: str = "https://forums.developer.nvidia.com"
    user_agent: str = "sparkwatch/0.1"
    requests_per_second: float = 1.0
    max_retries: int = 5
    timeout_s: float = 30.0
    posts_batch_size: int = 50
    categories: list[CategorySettings] = []


class SyncSettings(BaseModel):
    lookback_days: int = 60
    refetch_recent_days: int = 7
    sync_now_time_budget_s: int = 90
    missing_post_recheck_days: int = 30


class RelevanceSettings(BaseModel):
    keywords: dict[str, list[str]] = {}
    weights: dict[str, float] = {}
    families: list[str] = []


class HardwareSettings(BaseModel):
    dual_spark_usable_gb: float = 240.0
    single_spark_usable_gb: float = 120.0
    kv_overhead_frac: float = 0.10


class RepoSettings(BaseModel):
    max_compare_commits: int = 50
    max_compare_pages: int = 3


class McpSettings(BaseModel):
    default_limit: int = 20
    max_limit: int = 200
    max_chars: int = 40000


class LlmSettings(BaseModel):
    base_url: str = "http://localhost:8000/v1"
    model: str = "local-model"
    api_key: str = "none"
    structured_output: str = "json_schema"
    chunk_tokens: int = 10000
    prompt_version: int = 1


class WebSearchSettings(BaseModel):
    enabled: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPARKWATCH_",
        env_nested_delimiter="__",
        extra="ignore",
        toml_file=config_path(),
    )

    general: GeneralSettings = GeneralSettings()
    forum: ForumSettings = ForumSettings()
    sync: SyncSettings = SyncSettings()
    relevance: RelevanceSettings = RelevanceSettings()
    hardware: HardwareSettings = HardwareSettings()
    repos: RepoSettings = RepoSettings()
    mcp: McpSettings = McpSettings()
    llm: LlmSettings = LlmSettings()
    websearch: WebSearchSettings = WebSearchSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source = TomlConfigSettingsSource(settings_cls, toml_file=config_path())
        return (init_settings, env_settings, toml_source)

    @property
    def db_path(self) -> Path:
        path = Path(self.general.db_path).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    def forum_ids(self) -> list[int]:
        return [category.id for category in self.forum.categories]


@lru_cache(maxsize=8)
def _cached_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    return _cached_settings()


def reset_settings() -> None:
    _cached_settings.cache_clear()
